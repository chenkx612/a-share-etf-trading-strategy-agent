from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant_core.backtest.engine import BacktestResult, compute_metrics, run_backtest
from quant_core.data.market_data import ProjectPaths, load_universe, read_daily
from quant_core.research.candidate_evaluator import (
    Selector,
    evaluate_candidate,
    validate_selection,
)
from quant_core.research.contracts import ResearchTask
from quant_core.research.periods import bind_persisted_periods
from quant_core.research.workspace import write_json_atomic
from quant_core.schedule import latest_schedule_boundary, schedule_boundaries


ParameterizedSelector = Callable[[pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp, dict[str, object]], pd.DataFrame]
OFFICIAL_DEVELOPMENT_ATTEMPT_COMMAND = (
    "python3 -m quant_core.research.attempt evaluate"
)


class DevelopmentBudgetExceeded(RuntimeError):
    """Raised before an Agent development search can consume the Round."""


class HarnessExecutionRequired(RuntimeError):
    """Raised when a candidate container bypasses the Harness-owned Attempt."""


@dataclass
class WalkForwardExecutionControl:
    progress_path: Path
    round_clock_path: Path
    checkpoint_status_path: Path
    strategy_path: Path
    finalization_reserve_seconds: int = 300
    safety_factor: float = 1.25
    monotonic: Callable[[], float] = time.monotonic
    started_monotonic: float = field(init=False)
    started_at: str = field(init=False)

    def __post_init__(self) -> None:
        self.started_monotonic = self.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()

    def remaining_round_seconds(self) -> int:
        try:
            payload = json.loads(self.round_clock_path.read_text(encoding="utf-8"))
            remaining = payload["remaining_seconds"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise DevelopmentBudgetExceeded(
                "Development evaluation cannot read the live Round clock"
            ) from None
        if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < 0:
            raise DevelopmentBudgetExceeded(
                "Development evaluation found an invalid live Round clock"
            )
        return remaining

    def write_progress(self, status: str, **details: object) -> None:
        elapsed = max(0.0, self.monotonic() - self.started_monotonic)
        try:
            remaining: int | None = self.remaining_round_seconds()
        except DevelopmentBudgetExceeded:
            remaining = None
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.progress_path, {
            "schema_version": 1,
            "status": status,
            "started_at": self.started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": elapsed,
            "remaining_round_seconds": remaining,
            **details,
        })


def _require_current_checkpoint(control: WalkForwardExecutionControl) -> None:
    try:
        status = json.loads(
            control.checkpoint_status_path.read_text(encoding="utf-8")
        )
        strategy_path = status["strategy_path"]
        checkpoint = status["latest_checkpoint"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        control.write_progress(
            "rejected",
            reason="checkpoint_required",
            message="Submit an accepted checkpoint before Development evaluation",
        )
        raise DevelopmentBudgetExceeded(
            "Submit an accepted checkpoint before Development evaluation"
        ) from None
    if (
        not isinstance(strategy_path, str)
        or Path(strategy_path) != control.strategy_path
    ):
        control.write_progress(
            "rejected",
            reason="checkpoint_required",
            message="Checkpoint status does not match the configured strategy",
        )
        raise DevelopmentBudgetExceeded(
            "Checkpoint status does not match the configured strategy"
        )
    if (
        not isinstance(checkpoint, dict)
        or not isinstance(checkpoint.get("checkpoint_id"), str)
        or not isinstance(checkpoint.get("strategy_sha256"), str)
    ):
        control.write_progress(
            "rejected",
            reason="checkpoint_required",
            message="Submit an accepted checkpoint before Development evaluation",
        )
        raise DevelopmentBudgetExceeded(
            "Submit an accepted checkpoint before Development evaluation"
        )
    strategy_file = control.round_clock_path.parent / control.strategy_path
    try:
        digest = hashlib.sha256(strategy_file.read_bytes()).hexdigest()
    except OSError:
        control.write_progress(
            "rejected",
            reason="checkpoint_required",
            message="Configured strategy is unavailable for checkpoint verification",
        )
        raise DevelopmentBudgetExceeded(
            "Configured strategy is unavailable for checkpoint verification"
        ) from None
    if checkpoint["strategy_sha256"] == digest:
        return
    control.write_progress(
        "rejected",
        reason="checkpoint_required",
        strategy_sha256=digest,
        message="Current strategy does not have an accepted checkpoint",
    )
    raise DevelopmentBudgetExceeded(
        "Current strategy does not have an accepted checkpoint"
    )


def _selector(module_name: str) -> Selector:
    module = importlib.import_module(module_name)
    selector = getattr(module, "select", None)
    if not callable(selector):
        raise ValueError(f"{module_name} must define callable select(daily, universe, start, end)")
    return selector


def _parameterized_contract(module_name: str) -> tuple[list[dict[str, object]], ParameterizedSelector]:
    module = importlib.import_module(module_name)
    grid = getattr(module, "parameter_grid", None)
    selector = getattr(module, "select_with_params", None)
    if not callable(grid) or not callable(selector):
        raise ValueError(f"{module_name} must define parameter_grid() and select_with_params(..., params)")
    values = grid()
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("parameter_grid() must return a non-empty list")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for params in values:
        if not isinstance(params, dict):
            raise ValueError("parameter_grid() entries must be dictionaries")
        encoded = json.dumps(params, sort_keys=True, separators=(",", ":"))
        if encoded in seen:
            raise ValueError("parameter_grid() entries must be unique")
        seen.add(encoded)
        result.append(dict(params))
    return result, selector


def _passes(metrics: dict[str, float], constraints: dict[str, object]) -> bool:
    for name, rule in constraints.items():
        value = metrics.get(name)
        if not isinstance(value, (int, float)) or not np.isfinite(value):
            return False
        operator = rule["operator"]  # validated by ResearchTask
        threshold = float(rule["threshold"])
        if operator == ">=" and value < threshold:
            return False
        if operator == "<=" and value > threshold:
            return False
        if operator == "abs<=" and abs(value) > threshold:
            return False
    return True


def evaluate_walk_forward(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    period: dict[str, str],
    walk_forward: dict[str, object],
    constraints: dict[str, object],
    objective: str,
    grid: list[dict[str, object]],
    selector: ParameterizedSelector,
    *,
    execution: WalkForwardExecutionControl | None = None,
) -> tuple[pd.DataFrame, BacktestResult, list[dict[str, object]]]:
    if len(grid) > int(walk_forward["max_parameter_sets"]):
        raise ValueError("parameter_grid() exceeds walk_forward.max_parameter_sets")
    start, end = pd.Timestamp(period["start"]), pd.Timestamp(period["end"])
    history = daily.copy()
    history["date"] = pd.to_datetime(history["date"])
    folds: list[dict[str, object]] = []
    selections: list[pd.DataFrame] = []
    train_months = int(walk_forward["train_months"])
    schedule = walk_forward["schedule"]
    if not isinstance(schedule, dict):
        raise ValueError("walk_forward.schedule must be a mapping")
    symbols = set(universe["symbol"].astype(str))
    all_signal_dates = sorted(history.loc[history["date"].between(start, end), "date"].unique())
    if not all_signal_dates:
        raise ValueError("insufficient data for walk-forward evaluation period")
    trading_dates = pd.DatetimeIndex(
        history.loc[history["symbol"].astype(str).isin(symbols), "date"].unique()
    ).sort_values()
    replay_start = latest_schedule_boundary(start, schedule, trading_dates)
    boundaries = [
        replay_start,
        *(
            boundary
            for boundary in schedule_boundaries(trading_dates, schedule)
            if replay_start < boundary <= end
        ),
    ]
    fold_specs: list[dict[str, pd.Timestamp]] = []
    for index, boundary in enumerate(boundaries):
        next_start = boundaries[index + 1] if index + 1 < len(boundaries) else None
        fold_end = min(
            end,
            next_start - timedelta(days=1) if next_start is not None else end,
        )
        validation_start = max(start, boundary)
        if validation_start > fold_end:
            continue
        train_start = boundary - pd.DateOffset(months=train_months)
        fold_specs.append({
            "train_start": train_start,
            "train_end": boundary,
            "selection_start": boundary,
            "parameter_date": boundary,
            "validation_start": validation_start,
            "validation_end": fold_end,
        })

    cached: dict[tuple[int, int], tuple[pd.DataFrame, BacktestResult]] = {}
    completed_evaluations = 0
    projected_total_seconds: float | None = None
    available_seconds: int | None = None

    def training_evaluation(
        fold_index: int,
        parameter_index: int,
    ) -> tuple[pd.DataFrame, BacktestResult]:
        nonlocal completed_evaluations
        key = (fold_index, parameter_index)
        if key in cached:
            return cached[key]
        spec = fold_specs[fold_index]
        train_end = spec["train_end"]
        train_history = history[history["date"] <= train_end]
        started = execution.monotonic() if execution is not None else 0.0
        params = grid[parameter_index]
        evaluated = evaluate_candidate(
            train_history,
            universe,
            spec["train_start"],
            train_end,
            lambda d, u, s, e, p=params: selector(d, u, s, e, p),
        )
        completed_evaluations += 1
        cached[key] = evaluated
        if execution is not None:
            execution.write_progress(
                "running",
                reason=None,
                fold_index=fold_index + 1,
                fold_count=len(fold_specs),
                parameter_index=parameter_index + 1,
                parameter_count=len(grid),
                completed_evaluations=completed_evaluations,
                last_evaluation_seconds=max(0.0, execution.monotonic() - started),
                projected_total_seconds=projected_total_seconds,
                available_seconds=available_seconds,
            )
        return evaluated

    if execution is not None:
        _require_current_checkpoint(execution)
        remaining = execution.remaining_round_seconds()
        available_seconds = remaining - execution.finalization_reserve_seconds
        if available_seconds <= 0:
            execution.write_progress(
                "rejected",
                reason="finalization_reserve_reached",
                completed_evaluations=0,
                projected_total_seconds=None,
                available_seconds=max(0, available_seconds),
                message="Round has entered the finalization reserve",
            )
            raise DevelopmentBudgetExceeded(
                "Round has entered the finalization reserve; submit the checkpoint now"
            )
        calibration_durations: list[float] = []
        for fold_index in sorted({0, len(fold_specs) - 1}):
            calibration_started = execution.monotonic()
            training_evaluation(fold_index, 0)
            calibration_durations.append(
                max(0.0, execution.monotonic() - calibration_started)
            )
        total_evaluations = len(fold_specs) * (len(grid) + 1)
        projected_total_seconds = (
            max(calibration_durations) * total_evaluations * execution.safety_factor
        )
        available_seconds = (
            execution.remaining_round_seconds()
            - execution.finalization_reserve_seconds
        )
        if projected_total_seconds > available_seconds:
            execution.write_progress(
                "rejected",
                reason="projected_budget_exceeded",
                fold_count=len(fold_specs),
                parameter_count=len(grid),
                completed_evaluations=completed_evaluations,
                projected_total_seconds=projected_total_seconds,
                available_seconds=max(0, available_seconds),
                message="Projected Development evaluation exceeds the remaining budget",
            )
            raise DevelopmentBudgetExceeded(
                "Projected Development evaluation exceeds the remaining budget: "
                f"{projected_total_seconds:.1f}s required, "
                f"{max(0, available_seconds)}s available; shrink the parameter grid "
                "or optimize the selector"
            )

    for fold_index, spec in enumerate(fold_specs):
        train_start = spec["train_start"]
        train_end = spec["train_end"]
        train_history = history[history["date"] <= train_end]
        training_window = train_history[train_history["date"].between(train_start, train_end)]
        if training_window.empty:
            raise ValueError(f"insufficient data for walk-forward training ending {train_end.date()}")
        ranked: list[tuple[float, str, dict[str, object], dict[str, float]]] = []
        for parameter_index, params in enumerate(grid):
            selected, result = training_evaluation(fold_index, parameter_index)
            metrics = result.metrics
            value = metrics.get(objective)
            if _passes(metrics, constraints) and isinstance(value, (int, float)) and np.isfinite(value):
                ranked.append((-float(value), json.dumps(params, sort_keys=True, separators=(",", ":")), params, metrics))
        record: dict[str, object] = {
            "train_start": train_start.date().isoformat(), "train_end": train_end.date().isoformat(),
            "parameter_date": spec["parameter_date"].date().isoformat(),
            "validation_start": spec["validation_start"].date().isoformat(),
            "validation_end": spec["validation_end"].date().isoformat(),
        }
        if ranked:
            _, _, params, train_metrics = sorted(ranked)[0]
            fold_start = spec["selection_start"]
            fold_end = spec["validation_end"]
            validation_history = history[history["date"] <= fold_end]
            selected, _ = evaluate_candidate(
                validation_history, universe, fold_start, fold_end,
                lambda d, u, s, e, p=params: selector(d, u, s, e, p),
            )
            completed_evaluations += 1
            selections.append(selected)
            record.update({"status": "selected", "parameters": params, "training_metrics": train_metrics})
        else:
            record["status"] = "no_feasible_parameters"
        folds.append(record)
    selected_all = pd.concat(selections, ignore_index=True) if selections else pd.DataFrame(columns=["date", "symbol", "target_weight"])
    replay_signal_dates = sorted(
        history.loc[history["date"].between(replay_start, end), "date"].unique()
    )
    selected_all.attrs["signal_dates"] = replay_signal_dates
    selected_all.attrs["universe_symbols"] = sorted(symbols)
    oos_daily = history[
        history["date"].between(replay_start, end)
        & history["symbol"].astype(str).isin(symbols)
    ]
    replay_result = run_backtest(oos_daily, selected_all)
    scored_daily_returns = replay_result.daily_returns[
        replay_result.daily_returns["date"].between(start, end)
    ].reset_index(drop=True)
    scored_equity = scored_daily_returns[["date"]].copy()
    scored_equity["equity"] = (
        1.0 + scored_daily_returns["net_return"].astype(float)
    ).cumprod()
    scored_positions = replay_result.positions[
        replay_result.positions["date"].between(start, end)
    ].reset_index(drop=True)
    result = BacktestResult(
        scored_daily_returns,
        scored_equity,
        scored_positions,
        compute_metrics(scored_daily_returns),
    )
    for record in folds:
        left, right = pd.Timestamp(record["validation_start"]), pd.Timestamp(record["validation_end"])
        record["oos_metrics"] = compute_metrics(result.daily_returns[result.daily_returns["date"].between(left, right)])
    if execution is not None:
        execution.write_progress(
            "completed",
            reason=None,
            fold_index=len(fold_specs),
            fold_count=len(fold_specs),
            parameter_index=len(grid),
            parameter_count=len(grid),
            completed_evaluations=completed_evaluations,
            projected_total_seconds=projected_total_seconds,
            available_seconds=available_seconds,
        )
    selected_scored = selected_all[selected_all["date"].between(start, end)].copy()
    selected_scored.attrs["signal_dates"] = all_signal_dates
    selected_scored.attrs["universe_symbols"] = sorted(symbols)
    return selected_scored, result, folds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--universe", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-module", default="quant_core.strategy.research_candidate")
    parser.add_argument("--task")
    parser.add_argument("--walk-forward-config")
    parser.add_argument("--resolved-periods")
    parser.add_argument("--metrics-path")
    parser.add_argument("--stage", choices=["development", "gate", "test"])
    args = parser.parse_args()

    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    task = ResearchTask.load(args.task) if args.task else None
    if task is not None and args.resolved_periods:
        payload = json.loads(
            Path(args.resolved_periods).read_text(encoding="utf-8")
        )
        task = bind_persisted_periods(task, payload)
    config: dict[str, object] | None = None
    if args.walk_forward_config:
        config = json.loads(Path(args.walk_forward_config).read_text(encoding="utf-8"))
    if task is not None or config is not None:
        if task is not None and (task.evaluation_mode != "walk_forward" or args.stage is None):
            raise ValueError("--task requires walk_forward evaluation and --stage")
        if task is not None and args.stage == "test":
            test_period = task.test_period
            if test_period is None:
                raise ValueError("task.evaluation.test is required for test evaluation")
            period = dict(test_period)
        elif task is not None:
            period = dict(task.evaluation_periods[args.stage])
        else:
            period = dict(config["period"])
        if task is not None:
            assert task.parameter_selection is not None
            walk_forward = dict(task.parameter_selection)
            constraints = dict(task.constraints)
            objective = task.objective
        else:
            walk_forward = dict(config["walk_forward"])
            constraints = dict(config["constraints"])
            objective = str(config["objective"])
        grid, selector = _parameterized_contract(args.candidate_module)
        execution: WalkForwardExecutionControl | None = None
        if config is not None and isinstance(config.get("execution"), dict):
            root = Path(args.root)
            execution_config: dict[str, Any] = config["execution"]

            def execution_path(key: str) -> Path:
                value = execution_config.get(key)
                if not isinstance(value, str) or not value:
                    raise ValueError(f"walk-forward execution.{key} must be a path")
                path = Path(value)
                return path if path.is_absolute() else root / path

            strategy_path = execution_config.get("strategy_path")
            if not isinstance(strategy_path, str) or not strategy_path:
                raise ValueError("walk-forward execution.strategy_path must be a path")
            execution = WalkForwardExecutionControl(
                progress_path=execution_path("progress_path"),
                round_clock_path=execution_path("round_clock_path"),
                checkpoint_status_path=execution_path("checkpoint_status_path"),
                strategy_path=Path(strategy_path),
                finalization_reserve_seconds=int(
                    execution_config.get("finalization_reserve_seconds", 300)
                ),
                safety_factor=float(execution_config.get("safety_factor", 1.25)),
            )
        selected, result, folds = evaluate_walk_forward(
            read_daily(paths), load_universe(Path(args.universe)), period, walk_forward,
            constraints, objective, grid, selector, execution=execution,
        )
        metrics_payload: dict[str, object] = {
            "aggregate": result.metrics, "folds": folds,
            "no_feasible_parameter_folds": sum(f["status"] != "selected" for f in folds),
        }
    else:
        selected, result = evaluate_candidate(
            read_daily(paths), load_universe(Path(args.universe)), pd.Timestamp(args.start), pd.Timestamp(args.end),
            _selector(args.candidate_module),
        )
    run_dir = paths.outputs / "backtests" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    selected.to_csv(run_dir / "orders.csv", index=False)
    result.positions.to_csv(run_dir / "positions.csv", index=False)
    result.daily_returns.to_csv(run_dir / "daily_returns.csv", index=False)
    result.equity_curve.to_csv(run_dir / "equity_curve.csv", index=False)
    metrics = metrics_payload if task is not None or config is not None else result.metrics
    metrics_path = Path(args.metrics_path) if args.metrics_path else run_dir / "metrics.json"
    if not metrics_path.is_absolute():
        metrics_path = Path(args.root) / metrics_path
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
