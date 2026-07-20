from __future__ import annotations

import argparse
import importlib
import json
from datetime import timedelta
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from quant_core.backtest.engine import BacktestResult, compute_metrics, run_backtest
from quant_core.config import BacktestConfig
from quant_core.data.market_data import ProjectPaths, load_universe, read_daily
from quant_core.research.contracts import ResearchTask


Selector = Callable[[pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp], pd.DataFrame]
ParameterizedSelector = Callable[[pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp, dict[str, object]], pd.DataFrame]
REQUIRED_SELECTION_COLUMNS = {"date", "symbol", "target_weight"}


def evaluate_candidate(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    selector: Selector,
    *,
    backtest_config: BacktestConfig = BacktestConfig(),
) -> tuple[pd.DataFrame, BacktestResult]:
    history = daily.copy()
    history["date"] = pd.to_datetime(history["date"])
    history["symbol"] = history["symbol"].astype(str)
    symbols = set(universe["symbol"].astype(str))
    history = history[(history["symbol"].isin(symbols)) & (history["date"] <= end)]
    selected = selector(history.copy(), universe.copy(), start, end)
    selected = validate_selection(selected, history, symbols, start, end)
    backtest_daily = history[history["date"].between(start, end)].copy()
    if selected.empty:
        selected.attrs["signal_dates"] = sorted(backtest_daily["date"].unique())
    selected.attrs["universe_symbols"] = sorted(symbols)
    return selected, run_backtest(
        backtest_daily,
        selected,
        fee_rate=backtest_config.fee_rate,
        initial_capital=backtest_config.initial_capital,
        lot_size=backtest_config.lot_size,
    )


def validate_selection(
    selected: object,
    daily: pd.DataFrame,
    symbols: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    if not isinstance(selected, pd.DataFrame):
        raise ValueError("candidate select() must return a pandas DataFrame")
    missing = REQUIRED_SELECTION_COLUMNS - set(selected.columns)
    if missing:
        raise ValueError(f"candidate selection is missing columns: {', '.join(sorted(missing))}")
    result = selected.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    result["symbol"] = result["symbol"].astype(str)
    result["target_weight"] = pd.to_numeric(result["target_weight"], errors="raise")
    if result.duplicated(["date", "symbol"]).any():
        raise ValueError("candidate selection contains duplicate date/symbol rows")
    if not result["date"].between(start, end).all():
        raise ValueError("candidate selection contains dates outside the evaluation period")
    trading_dates = set(pd.to_datetime(daily["date"]))
    if not set(result["date"]).issubset(trading_dates):
        raise ValueError("candidate selection contains non-trading dates")
    if not set(result["symbol"]).issubset(symbols):
        raise ValueError("candidate selection contains symbols outside the universe")
    weights = result["target_weight"].astype(float)
    if not np.isfinite(weights).all() or (weights < 0.0).any():
        raise ValueError("candidate weights must be finite and non-negative")
    if (result.groupby("date")["target_weight"].sum() > 1.0 + 1e-9).any():
        raise ValueError("candidate weights must sum to at most one on each date")
    return result.sort_values(["date", "symbol"]).reset_index(drop=True)


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
) -> tuple[pd.DataFrame, BacktestResult, list[dict[str, object]]]:
    if len(grid) > int(walk_forward["max_parameter_sets"]):
        raise ValueError("parameter_grid() exceeds walk_forward.max_parameter_sets")
    start, end = pd.Timestamp(period["start"]), pd.Timestamp(period["end"])
    history = daily.copy()
    history["date"] = pd.to_datetime(history["date"])
    folds: list[dict[str, object]] = []
    selections: list[pd.DataFrame] = []
    fold_start = start
    train_months = int(walk_forward["train_months"])
    validation_months = int(walk_forward["validation_months"])
    symbols = set(universe["symbol"].astype(str))
    all_signal_dates = sorted(history.loc[history["date"].between(start, end), "date"].unique())
    while fold_start <= end:
        next_start = fold_start + pd.DateOffset(months=validation_months)
        fold_end = min(end, next_start - timedelta(days=1))
        train_start = fold_start - pd.DateOffset(months=train_months)
        train_end = fold_start - timedelta(days=1)
        train_history = history[history["date"] <= train_end]
        training_window = train_history[train_history["date"].between(train_start, train_end)]
        if training_window.empty:
            raise ValueError(f"insufficient data for walk-forward training ending {train_end.date()}")
        ranked: list[tuple[float, str, dict[str, object], dict[str, float]]] = []
        for params in grid:
            selected, result = evaluate_candidate(
                train_history, universe, train_start, train_end,
                lambda d, u, s, e, p=params: selector(d, u, s, e, p),
            )
            metrics = result.metrics
            value = metrics.get(objective)
            if _passes(metrics, constraints) and isinstance(value, (int, float)) and np.isfinite(value):
                ranked.append((-float(value), json.dumps(params, sort_keys=True, separators=(",", ":")), params, metrics))
        record: dict[str, object] = {
            "train_start": train_start.date().isoformat(), "train_end": train_end.date().isoformat(),
            "validation_start": fold_start.date().isoformat(), "validation_end": fold_end.date().isoformat(),
        }
        if ranked:
            _, _, params, train_metrics = sorted(ranked)[0]
            validation_history = history[history["date"] <= fold_end]
            selected, _ = evaluate_candidate(
                validation_history, universe, fold_start, fold_end,
                lambda d, u, s, e, p=params: selector(d, u, s, e, p),
            )
            selections.append(selected)
            record.update({"status": "selected", "parameters": params, "training_metrics": train_metrics})
        else:
            record["status"] = "no_feasible_parameters"
        folds.append(record)
        fold_start = next_start
    selected_all = pd.concat(selections, ignore_index=True) if selections else pd.DataFrame(columns=["date", "symbol", "target_weight"])
    selected_all.attrs["signal_dates"] = all_signal_dates
    selected_all.attrs["universe_symbols"] = sorted(symbols)
    oos_daily = history[history["date"].between(start, end) & history["symbol"].astype(str).isin(symbols)]
    result = run_backtest(oos_daily, selected_all)
    for record in folds:
        left, right = pd.Timestamp(record["validation_start"]), pd.Timestamp(record["validation_end"])
        record["oos_metrics"] = compute_metrics(result.daily_returns[result.daily_returns["date"].between(left, right)])
    return selected_all, result, folds


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
    parser.add_argument("--metrics-path")
    parser.add_argument("--stage", choices=["development", "gate", "test"])
    args = parser.parse_args()

    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    task = ResearchTask.load(args.task) if args.task else None
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
        walk_forward = dict(task.evaluation_periods) if task is not None else dict(config["walk_forward"])
        constraints = dict(task.raw["evaluation"]["constraints"]) if task is not None else dict(config["constraints"])
        objective = str(task.raw["evaluation"]["objective"]) if task is not None else str(config["objective"])
        grid, selector = _parameterized_contract(args.candidate_module)
        selected, result, folds = evaluate_walk_forward(
            read_daily(paths), load_universe(Path(args.universe)), period, walk_forward,
            constraints, objective, grid, selector,
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
