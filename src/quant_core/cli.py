from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import tomllib
import uuid
from datetime import date, datetime
from pathlib import Path
from types import FrameType
from typing import Iterable

import pandas as pd

from quant_core.backtest.engine import run_backtest
from quant_core.config import BacktestConfig
from quant_core.data.market_data import (
    AkshareMarketDataClient,
    DEFAULT_ADJUST,
    ProjectPaths,
    fetch_daily_if_stale,
    load_universe,
    read_daily,
    read_table,
    replace_symbol_history,
    universe_symbols,
    validate_daily,
    write_table,
)
from quant_core.factors import compute_factors, normalize_sharpe_windows
from quant_core.production import run_recommendation
from quant_core.research import (
    ResearchTask,
    regenerate_loop_report,
    run_loop,
    run_once,
)
from quant_core.research.environment import (
    capture_evaluation_environment,
    persist_evaluation_environment,
)
from quant_core.research.workspace import (
    ResearchWorkspace,
    copy_runtime_inputs,
    runtime_inputs_manifest,
    workspace_python_env,
    write_json_atomic,
)
from quant_core.strategy.sharpe_corr_threshold import (
    STRATEGY_NAME,
    SharpeCorrThresholdParams,
    select_sharpe_corr_threshold,
)


STRATEGY_CHOICES = [STRATEGY_NAME]
OPTIMIZATION_CONSTRAINT_CHOICES = ["none", "drawdown-lt-return"]


def parse_int_list(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def parse_sharpe_windows(value: str) -> list[int]:
    return list(normalize_sharpe_windows(parse_int_list(value)))


def metrics_satisfy_constraint(metrics: dict[str, float], constraint: str) -> bool:
    if constraint == "none":
        return True
    if constraint == "drawdown-lt-return":
        return abs(metrics.get("max_drawdown", 1.0)) < metrics.get("annual_return", 0.0)
    raise ValueError(f"Unknown optimization constraint: {constraint}")


def sort_optimization_results(results: pd.DataFrame, objective: str, constraint: str) -> pd.DataFrame:
    if constraint == "none":
        return results.sort_values(objective, ascending=False, na_position="last").reset_index(drop=True)
    return results.sort_values(
        ["valid", objective],
        ascending=[False, False],
        na_position="last",
    ).reset_index(drop=True)


def optimization_grid_results(
    *,
    strategy: str,
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    symbols: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
    top_ns: Iterable[int],
    sharpe_windows: Iterable[int] | None,
    factor_lower_bounds: Iterable[float | None],
    corr_windows: Iterable[int | None],
    corr_thresholds: Iterable[float | None],
    stop_loss_pcts: Iterable[float | None],
    constraint: str,
) -> pd.DataFrame:
    rows = []
    if strategy != STRATEGY_NAME:
        raise ValueError(f"Unknown strategy: {strategy}")
    backtest_config = BacktestConfig()
    defaults = SharpeCorrThresholdParams()
    candidate_sharpe_windows = list(sharpe_windows or [None])
    for top_n in top_ns:
        for sharpe_window in candidate_sharpe_windows:
            for factor_lower_bound in factor_lower_bounds:
                for corr_window in corr_windows:
                    for corr_threshold in corr_thresholds:
                        for stop_loss_pct in stop_loss_pcts:
                            params = SharpeCorrThresholdParams(
                                top_n=top_n,
                                sharpe_window=(
                                    defaults.sharpe_window
                                    if sharpe_window is None
                                    else sharpe_window
                                ),
                                factor_lower_bound=(
                                    defaults.factor_lower_bound
                                    if factor_lower_bound is None
                                    else factor_lower_bound
                                ),
                                corr_window=(
                                    defaults.corr_window
                                    if corr_window is None
                                    else corr_window
                                ),
                                corr_threshold=(
                                    defaults.corr_threshold
                                    if corr_threshold is None
                                    else corr_threshold
                                ),
                                stop_loss_pct=(
                                    defaults.stop_loss_pct
                                    if stop_loss_pct is None
                                    else stop_loss_pct
                                ),
                            )
                            selected = select_sharpe_corr_threshold(
                                factors,
                                params,
                                start=start,
                                end=end,
                                universe_symbols=symbols,
                            )
                            result = run_backtest(
                                daily,
                                selected,
                                fee_rate=backtest_config.fee_rate,
                                initial_capital=backtest_config.initial_capital,
                                lot_size=backtest_config.lot_size,
                            )
                            metrics = result.metrics
                            rows.append({
                                "strategy": STRATEGY_NAME,
                                "top_n": params.top_n,
                                "fee_rate": backtest_config.fee_rate,
                                "sharpe_window": params.sharpe_window,
                                "factor_lower_bound": params.factor_lower_bound,
                                "corr_window": params.corr_window,
                                "corr_threshold": params.corr_threshold,
                                "stop_loss_pct": params.stop_loss_pct,
                                "valid": metrics_satisfy_constraint(metrics, constraint),
                                **metrics,
                            })
    return pd.DataFrame(rows)


def build_strategy_params(args: argparse.Namespace) -> SharpeCorrThresholdParams:
    strategy = getattr(args, "strategy", STRATEGY_NAME)
    if strategy != STRATEGY_NAME:
        raise ValueError(f"Unknown strategy: {strategy}")
    defaults = SharpeCorrThresholdParams()
    return SharpeCorrThresholdParams(
        top_n=getattr(args, "top_n", None) or defaults.top_n,
        sharpe_window=getattr(args, "sharpe_window", None) or defaults.sharpe_window,
        factor_lower_bound=(
            defaults.factor_lower_bound
            if getattr(args, "factor_lower_bound", None) is None
            else args.factor_lower_bound
        ),
        corr_window=(
            defaults.corr_window
            if getattr(args, "corr_window", None) is None
            else args.corr_window
        ),
        corr_threshold=(
            defaults.corr_threshold
            if getattr(args, "corr_threshold", None) is None
            else args.corr_threshold
        ),
        stop_loss_pct=(
            defaults.stop_loss_pct
            if getattr(args, "stop_loss_pct", None) is None
            else args.stop_loss_pct
        ),
    )


def ensure_sharpe_factor_columns(
    factors: pd.DataFrame,
    daily: pd.DataFrame,
    sharpe_windows: list[int] | tuple[int, ...],
) -> pd.DataFrame:
    if not sharpe_windows:
        return factors
    windows = normalize_sharpe_windows(sharpe_windows)
    missing = [f"sharpe_{window}" for window in windows if f"sharpe_{window}" not in factors.columns]
    if not missing:
        return factors

    computed = compute_factors(daily, sharpe_windows=windows)
    merge_columns = ["date", "symbol", *missing]
    out = factors.copy()
    out = out.merge(computed[merge_columns], on=["date", "symbol"], how="left")
    return out


def build_markdown_report(
    run_id: str,
    metrics: dict[str, float],
    recommendation: pd.DataFrame,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Backtest Report: {run_id}",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, value in metrics.items():
        lines.append(f"| {key} | {value:.6f} |")
    lines.extend([
        "",
        "## Latest Recommendation",
        "",
        "| Symbol | Name | Score | Target Weight |",
        "| --- | --- | ---: | ---: |",
    ])
    if not recommendation.empty:
        for row in recommendation.itertuples(index=False):
            lines.append(
                f"| {row.symbol} | {row.name} | {float(row.score):.6f} | {float(row.target_weight):.4f} |"
            )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def recommendation_output_frame(recommendation: pd.DataFrame) -> pd.DataFrame:
    output_columns = [
        "record_type",
        "date",
        "symbol",
        "name",
        "score",
        "target_weight",
        "filter",
        "condition",
        "daily_return",
        "stop_loss_pct",
        "correlation",
        "corr_threshold",
        "selected_symbol",
        "selected_name",
    ]
    recommended = recommendation.copy()
    recommended.insert(0, "record_type", "recommendation")
    filtered = pd.DataFrame(recommendation.attrs.get("filter_events", []))
    if not filtered.empty:
        filtered.insert(0, "record_type", "filtered")
        if "target_weight" not in filtered.columns:
            filtered["target_weight"] = 0.0
    combined = pd.concat([recommended, filtered], ignore_index=True, sort=False)
    for column in output_columns:
        if column not in combined.columns:
            combined[column] = pd.NA
    return combined[output_columns]


def resolve_data_universe(args: argparse.Namespace) -> tuple[pd.DataFrame, str]:
    if args.universe and Path(args.universe).exists():
        return load_universe(Path(args.universe)), args.universe_name
    raise FileNotFoundError("No universe was provided. Pass --universe PATH.")


def effective_adjust(args: argparse.Namespace) -> str:
    adjust = getattr(args, "adjust", None)
    return DEFAULT_ADJUST if adjust is None else adjust


def load_strategy_universe(args: argparse.Namespace) -> pd.DataFrame:
    if not getattr(args, "universe", None):
        raise FileNotFoundError("No universe was provided. Pass --universe PATH.")
    return load_universe(Path(args.universe))


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def command_data_update(args: argparse.Namespace) -> None:
    paths = ProjectPaths(Path(args.root))
    paths.ensure_data()
    market_data = AkshareMarketDataClient(adjust=effective_adjust(args))
    universe, _universe_name = resolve_data_universe(args)
    try:
        existing = read_daily(paths)
    except FileNotFoundError:
        existing = None
    incoming, target_trade_date = fetch_daily_if_stale(
        universe,
        parse_date(args.start),
        parse_date(args.end),
        existing=existing,
        fetch_one=market_data.fetch_daily,
        log=print,
    )
    if incoming.empty:
        print(f"local daily data unchanged through {target_trade_date}")
        return
    daily = replace_symbol_history(existing, incoming)
    problems = validate_daily(daily)
    daily_path = write_table(daily, paths.data_daily)
    print(f"wrote {len(daily)} rows to {daily_path}")
    print(f"latest trade date target: {target_trade_date}")
    if problems:
        print("data warnings:")
        for problem in problems:
            print(f"- {problem}")


def command_factor_compute(args: argparse.Namespace) -> None:
    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    daily = read_daily(paths)
    sharpe_windows = parse_sharpe_windows(args.sharpe_window)
    factors = compute_factors(daily, sharpe_windows=sharpe_windows)
    start = pd.Timestamp(args.start) if args.start else None
    end = pd.Timestamp(args.end) if args.end else None
    if start is not None:
        factors = factors[factors["date"] >= start]
    if end is not None:
        factors = factors[factors["date"] <= end]
    path = write_table(factors, paths.outputs / "factors" / "factors")
    print(f"wrote {len(factors)} factor rows to {path}")


def command_backtest_run(args: argparse.Namespace) -> None:
    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    params = build_strategy_params(args)
    backtest_config = BacktestConfig()
    daily = read_daily(paths)
    factors = read_table(paths.outputs / "factors" / "factors", parse_dates=["date"])
    factors = ensure_sharpe_factor_columns(factors, daily, [params.sharpe_window])
    universe = load_strategy_universe(args)
    symbols = universe_symbols(universe)
    factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    selected = select_sharpe_corr_threshold(
        factors,
        params,
        start=start,
        end=end,
        universe_symbols=symbols,
    )
    result = run_backtest(
        daily,
        selected,
        fee_rate=backtest_config.fee_rate,
        initial_capital=backtest_config.initial_capital,
        lot_size=backtest_config.lot_size,
    )
    metrics = result.metrics
    run_id = args.run_id or f"{args.start}_{args.end}_{STRATEGY_NAME}_{args.universe_name}_top{params.top_n}"
    run_dir = paths.outputs / "backtests" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    selected.to_csv(run_dir / "orders.csv", index=False)
    result.positions.to_csv(run_dir / "positions.csv", index=False)
    result.daily_returns.to_csv(run_dir / "daily_returns.csv", index=False)
    result.equity_curve.to_csv(run_dir / "equity_curve.csv", index=False)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    latest = selected[selected["date"] == selected["date"].max()] if not selected.empty else selected
    build_markdown_report(run_id, metrics, latest, paths.outputs / "reports" / f"{run_id}.md")
    print(f"wrote backtest outputs to {run_dir}")


def command_optimize_grid(args: argparse.Namespace) -> None:
    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    daily = read_daily(paths)
    factors = read_table(paths.outputs / "factors" / "factors", parse_dates=["date"])
    sharpe_windows = parse_sharpe_windows(args.sharpe_window)
    factors = ensure_sharpe_factor_columns(factors, daily, sharpe_windows)
    universe = load_strategy_universe(args)
    symbols = universe_symbols(universe)
    factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    factor_lower_bounds = parse_float_list(args.factor_lower_bound)
    corr_windows = parse_int_list(args.corr_window)
    corr_thresholds = parse_float_list(args.corr_threshold)
    stop_loss_pcts = parse_float_list(args.stop_loss_pct)
    results = optimization_grid_results(
        strategy=args.strategy,
        daily=daily,
        factors=factors,
        symbols=symbols,
        start=start,
        end=end,
        top_ns=parse_int_list(args.top_n),
        sharpe_windows=sharpe_windows,
        factor_lower_bounds=factor_lower_bounds,
        corr_windows=corr_windows,
        corr_thresholds=corr_thresholds,
        stop_loss_pcts=stop_loss_pcts,
        constraint=args.constraint,
    )
    if results.empty:
        raise ValueError("No optimization results generated")
    if args.objective not in results.columns:
        raise ValueError(f"Objective {args.objective!r} is not available in results")
    results = sort_optimization_results(results, args.objective, args.constraint)
    run_id = args.run_id or f"{args.start}_{args.end}_{args.strategy}_{args.universe_name}_grid"
    run_dir = paths.outputs / "optimizations" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(run_dir / "results.csv", index=False)
    (run_dir / "best.json").write_text(json.dumps(results.iloc[0].to_dict(), indent=2), encoding="utf-8")
    print(f"wrote optimization results to {run_dir}")
    if args.constraint != "none" and not bool(results.iloc[0]["valid"]):
        print(f"no parameter set satisfied constraint: {args.constraint}")
    print(results.head(min(len(results), args.show)).to_string(index=False))


def command_report_build(args: argparse.Namespace) -> None:
    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    run_dir = paths.outputs / "backtests" / args.run_id
    metrics_path = run_dir / "metrics.json"
    orders_path = run_dir / "orders.csv"
    if not metrics_path.exists() or not orders_path.exists():
        raise FileNotFoundError(f"Missing backtest outputs under {run_dir}")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    orders = pd.read_csv(orders_path, parse_dates=["date"])
    latest = orders[orders["date"] == orders["date"].max()] if not orders.empty else orders
    report_path = build_markdown_report(args.run_id, metrics, latest, paths.outputs / "reports" / f"{args.run_id}.md")
    print(f"wrote report to {report_path}")


def command_recommend_today(args: argparse.Namespace) -> None:
    """Compatibility helper for the legacy Sharpe-pool research skill.

    This is intentionally not registered as a public CLI command; production
    recommendations use ``quant-agent recommend <task>``.
    """
    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    params = build_strategy_params(args)
    target_date = pd.Timestamp(args.date)
    factors = read_table(paths.outputs / "factors" / "factors", parse_dates=["date"])
    daily = read_daily(paths)
    factors = ensure_sharpe_factor_columns(factors, daily, [params.sharpe_window])
    universe = load_strategy_universe(args)
    symbols = universe_symbols(universe)
    factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    selected = select_sharpe_corr_threshold(
        factors,
        params,
        start=target_date,
        end=target_date,
        universe_symbols=symbols,
    )
    out = paths.outputs / "recommendations" / f"{args.date}_{args.universe_name}.csv"
    recommendation_output_frame(selected).to_csv(out, index=False)
    print(f"wrote {len(selected)} recommendations to {out}")


def command_research_run_once(args: argparse.Namespace) -> None:
    environment = capture_evaluation_environment()
    output = Path(args.output).resolve()
    persist_evaluation_environment(output, environment)
    result_path = run_once(
        args.task,
        args.experiment_id,
        output,
        workspace=args.root,
        evaluation_environment=environment,
    )
    print(f"wrote experiment result to {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "completed":
        raise SystemExit(1)


def command_research_loop(args: argparse.Namespace) -> None:
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        state_path = run_loop(
            args.task,
            workspace=args.root,
            research_root=args.research_root,
            retain_diagnostics=args.retain_diagnostics,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print(f"wrote loop state to {state_path}")
    print(f"stop reason: {state['stop_reason']}")
    print(
        f"rounds: {state['rounds_completed']} "
        f"(accepted={state['accepted']}, rejected={state['rejected']}, failed={state['failed']})"
    )
    if state.get("report_status") == "completed":
        print(f"report: {state_path.parent / str(state['report_path'])}")
    elif state.get("report_status") == "failed":
        print(f"report failed: {state.get('report_error')}")
    if state["stop_reason"] == "interrupted":
        raise SystemExit(130)


def resolve_research_task_reference(reference: str, workspace: str | Path = ".") -> Path:
    """Resolve a task path, tasks/<stem>.toml, or task id."""
    explicit = Path(reference).expanduser()
    workspace_path = Path(workspace).resolve()
    tasks_dir = workspace_path / "tasks"
    if explicit.is_absolute() or len(explicit.parts) > 1:
        path = explicit if explicit.is_absolute() else Path.cwd() / explicit
        if not path.is_file():
            raise ValueError(f"task file does not exist: {reference}")
        return path.resolve()
    if explicit.suffix == ".toml":
        for path in (Path.cwd() / explicit, tasks_dir / explicit):
            if path.is_file():
                return path.resolve()
        raise ValueError(f"task file does not exist: {reference}")

    candidates: list[tuple[Path, str]] = []
    if tasks_dir.is_dir():
        for path in sorted(tasks_dir.glob("*.toml")):
            try:
                with path.open("rb") as handle:
                    payload = tomllib.load(handle)
                    task_id = payload.get("id")
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if isinstance(task_id, str) and task_id:
                candidates.append((path.resolve(), task_id))

    matches = {
        path
        for path, task_id in candidates
        if reference == path.stem or reference == task_id
    }
    if len(matches) == 1:
        return matches.pop()
    if len(matches) > 1:
        matched = ", ".join(str(path.relative_to(workspace_path)) for path in sorted(matches))
        raise ValueError(f"task reference is ambiguous: {reference} ({matched})")

    available = ", ".join(path.stem for path, _task_id in candidates) or "none"
    raise ValueError(f"unknown task: {reference}; available tasks: {available}")


def command_loop(args: argparse.Namespace) -> None:
    try:
        args.task = str(resolve_research_task_reference(args.task, args.root))
    except ValueError as exc:
        raise SystemExit(f"quant-agent loop: error: {exc}") from exc
    command_research_loop(args)


def command_recommend(args: argparse.Namespace) -> None:
    try:
        task_path = resolve_research_task_reference(args.task, args.root)
    except ValueError as exc:
        raise SystemExit(f"quant-agent recommend: error: {exc}") from exc
    requested_date = date.fromisoformat(args.date) if args.date is not None else None
    summary_path = run_recommendation(
        args.root,
        task_path,
        requested_date=requested_date,
        skip_refresh=args.skip_refresh,
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    root = Path(args.root).resolve()
    recommendation_path = root / summary["recommendation_path"]
    recommendation = pd.read_csv(
        recommendation_path,
        dtype={"symbol": str},
    )
    print(f"wrote production recommendation summary to {summary_path}")
    print(
        f"signal date: {summary['signal_date']}; trade date: {summary['trade_date']}; "
        f"parameter status: {summary['search_status']}"
    )
    schedule = summary["parameter_schedule"]
    print(
        f"parameter policy: {summary['parameter_train_months']}-month lookback; "
        f"{schedule['period']}/{schedule['trigger']} every {schedule['interval']} period(s)"
    )
    print(
        f"parameter search: actually searched on {summary['last_tuning_date']}; "
        f"next scheduled boundary {summary['next_tuning_date']}"
    )
    if schedule["period"] in {"calendar_month", "iso_week"}:
        print(
            "schedule note: the first successful run in each calendar period searches; "
            "a late first run therefore has a shorter reuse span"
        )
    print("next-day target holdings:")
    display_columns = [
        column
        for column in ("record_type", "symbol", "name", "target_weight")
        if column in recommendation
    ]
    print(
        recommendation[display_columns].to_string(
            index=False,
            formatters={
                "target_weight": lambda value: f"{float(value):.2%}",
            },
        )
    )
    print(f"recent causal return curve: {root / summary['curve_png_path']}")


def command_research_report(args: argparse.Namespace) -> None:
    report_path = regenerate_loop_report(
        args.task,
        workspace=args.root,
        research_root=args.research_root,
        run_number=args.run,
    )
    print(f"wrote loop report to {report_path}")


def command_research_clean(args: argparse.Namespace) -> None:
    task = ResearchTask.load(Path(args.task).resolve()) if args.task is not None else None
    task_id = task.task_id if task is not None else str(args.task_id)
    source = Path(args.root).resolve()
    research_root = Path(args.research_root)
    if not research_root.is_absolute():
        research_root = source / research_root
    manager = ResearchWorkspace(source, research_root, task_id)
    loop_state_paths = [
        manager.root / "loop-state.json",
        *(
            manager.for_run(run_number).loop_state_path
            for run_number in manager.run_numbers()
        ),
    ]
    for loop_state_path in loop_state_paths:
        if not loop_state_path.exists():
            continue
        loop_state = json.loads(loop_state_path.read_text(encoding="utf-8"))
        if loop_state.get("status") == "running":
            raise RuntimeError("cannot clean artifacts while a research loop is running")
    if manager.state_path.exists() or manager.legacy_state_path.exists():
        manager.load_state(task.strategy_path if task is not None else None)
    manager.migrate_legacy_loop()
    manager.cleanup_transient(remove_development_cache=True)
    manager.clear_diagnostics()
    summary = manager.compact_artifacts()
    print(
        f"removed {summary['removed_files']} redundant files "
        f"({summary['removed_bytes']} bytes) from {manager.root}"
    )


def command_research_test(args: argparse.Namespace) -> None:
    """Evaluate the immutable Champion without feeding results into promotion."""
    task_file = Path(args.task).resolve()
    task = ResearchTask.load(task_file)
    source = Path(args.root).resolve()
    research_root = Path(args.research_root)
    if not research_root.is_absolute():
        research_root = source / research_root
    environment = capture_evaluation_environment()
    manager = ResearchWorkspace(
        source,
        research_root,
        task.task_id,
        evaluation_environment_sha256=environment.sha256,
    )
    persist_evaluation_environment(manager.root, environment)
    state = manager.initialize(
        date.fromisoformat(task.evaluation_periods["development"]["end"]),
        task.baseline_mode, task.baseline_exclude, task.strategy_path,
    )
    if not isinstance(state.get("champion_sha256"), str):
        raise RuntimeError("research task does not have a champion yet")
    test = task.raw["evaluation"].get("test")
    if not isinstance(test, dict):
        raise ValueError("task.evaluation.test is required")
    test_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    evaluator = manager.create_champion_test_evaluator(test_id, state)
    output = manager.root / "tests" / test_id
    output.mkdir(parents=True)
    try:
        copy_runtime_inputs(source, evaluator)
        runtime_inputs = runtime_inputs_manifest(evaluator)
        run_id = f"test-{test_id}"
        values = {"python": sys.executable, "universe": str(task.raw["data"]["universe"]),
                  "workspace": str(evaluator), "start": str(test["start"]), "end": str(test["end"]), "run_id": run_id,
                  "strategy_name": task.strategy_name or "", "strategy_module": task.strategy_module or ""}
        metrics_relative = str(task.raw["commands"]["metrics_path"]).format_map(values)
        if task.evaluation_mode == "walk_forward":
            command = [sys.executable, "-m", "quant_core.research.evaluator", "--root", str(evaluator),
                       "--universe", str(task.raw["data"]["universe"]), "--start", str(test["start"]),
                       "--end", str(test["end"]), "--run-id", run_id, "--candidate-module", str(task.strategy_module),
                       "--task", str(task_file), "--stage", "test", "--metrics-path", metrics_relative]
        else:
            command = [part.format_map(values) for part in task.raw["commands"]["backtest"]]
        completed = subprocess.run(command, cwd=evaluator, env=workspace_python_env(evaluator), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        (output / "test.log").write_text(completed.stdout, encoding="utf-8")
        metrics_path = Path(metrics_relative)
        if not metrics_path.is_absolute():
            metrics_path = evaluator / metrics_path
        if completed.returncode != 0 or not metrics_path.exists():
            raise RuntimeError("test evaluation failed")
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        write_json_atomic(output / "result.json", {
            "test_id": test_id,
            "strategy_sha256": state["champion_sha256"],
            "evaluation_environment_sha256": environment.sha256,
            "runtime_inputs": runtime_inputs,
            "metrics": metrics,
        })
    finally:
        manager.remove_evaluator(evaluator)
    print(f"wrote test result to {output / 'result.json'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-agent")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="group", required=True)

    loop = sub.add_parser(
        "loop",
        help="run a managed research loop",
    )
    loop.add_argument(
        "task",
        help="task file, tasks/<name>.toml stem, or task id",
    )
    loop.add_argument("--research-root", default=".research")
    loop.add_argument(
        "-d",
        "--diagnostics",
        dest="retain_diagnostics",
        action="store_true",
        help="retain disposable post-run diagnostics under the research cache",
    )
    loop.set_defaults(func=command_loop)

    data = sub.add_parser("data")
    data_sub = data.add_subparsers(dest="command", required=True)
    data_update = data_sub.add_parser("update")
    data_update.add_argument("--start", required=True)
    data_update.add_argument("--end", required=True)
    data_update.add_argument("--universe")
    data_update.add_argument("--universe-name", default="default")
    data_update.add_argument("--adjust")
    data_update.set_defaults(func=command_data_update)

    factor = sub.add_parser("factor")
    factor_sub = factor.add_subparsers(dest="command", required=True)
    factor_compute = factor_sub.add_parser("compute")
    factor_compute.add_argument("--start")
    factor_compute.add_argument("--end")
    factor_compute.add_argument("--sharpe-window", default="20,25")
    factor_compute.set_defaults(func=command_factor_compute)

    backtest = sub.add_parser("backtest")
    backtest_sub = backtest.add_subparsers(dest="command", required=True)
    backtest_run = backtest_sub.add_parser("run")
    backtest_run.add_argument("--start", required=True)
    backtest_run.add_argument("--end", required=True)
    backtest_run.add_argument("--strategy", choices=STRATEGY_CHOICES, default=STRATEGY_NAME)
    backtest_run.add_argument("--universe", required=True)
    backtest_run.add_argument("--universe-name", default="default")
    backtest_run.add_argument("--top-n", type=int)
    backtest_run.add_argument("--sharpe-window", type=int)
    backtest_run.add_argument("--factor-lower-bound", type=float)
    backtest_run.add_argument("--corr-window", type=int)
    backtest_run.add_argument("--corr-threshold", type=float)
    backtest_run.add_argument("--stop-loss-pct", type=float)
    backtest_run.add_argument("--run-id")
    backtest_run.set_defaults(func=command_backtest_run)

    optimize = sub.add_parser("optimize")
    optimize_sub = optimize.add_subparsers(dest="command", required=True)
    optimize_grid = optimize_sub.add_parser("grid")
    optimize_grid.add_argument("--start", required=True)
    optimize_grid.add_argument("--end", required=True)
    optimize_grid.add_argument("--strategy", choices=STRATEGY_CHOICES, default=STRATEGY_NAME)
    optimize_grid.add_argument("--universe", required=True)
    optimize_grid.add_argument("--universe-name", default="default")
    optimize_grid.add_argument("--top-n", default="3,5,10")
    optimize_grid.add_argument("--sharpe-window", default="20,25,60,120")
    optimize_grid.add_argument("--factor-lower-bound", default="-0.5,0.0,0.5")
    optimize_grid.add_argument("--corr-window", default="100")
    optimize_grid.add_argument("--corr-threshold", default="0.8,0.9")
    optimize_grid.add_argument("--stop-loss-pct", default="0.08,0.1")
    optimize_grid.add_argument("--objective", default="sharpe")
    optimize_grid.add_argument("--constraint", choices=OPTIMIZATION_CONSTRAINT_CHOICES, default="none")
    optimize_grid.add_argument("--run-id")
    optimize_grid.add_argument("--show", type=int, default=5)
    optimize_grid.set_defaults(func=command_optimize_grid)

    recommend = sub.add_parser(
        "recommend",
        help="generate a production recommendation for a managed task",
    )
    recommend.add_argument(
        "task",
        help="task file, tasks/<name>.toml stem, or task id",
    )
    recommend.add_argument("--date", help="requested ISO date; defaults to today in Shanghai")
    recommend.add_argument(
        "--skip-refresh",
        action="store_true",
        help="use only the existing local market-data cache",
    )
    recommend.set_defaults(func=command_recommend)

    report = sub.add_parser("report")
    report_sub = report.add_subparsers(dest="command", required=True)
    report_build = report_sub.add_parser("build")
    report_build.add_argument("--run-id", required=True)
    report_build.set_defaults(func=command_report_build)

    research = sub.add_parser("research")
    research_sub = research.add_subparsers(dest="command", required=True)
    research_run_once = research_sub.add_parser("run-once")
    research_run_once.add_argument("--task", required=True)
    research_run_once.add_argument("--experiment-id", required=True)
    research_run_once.add_argument("--output", required=True)
    research_run_once.set_defaults(func=command_research_run_once)
    research_loop = research_sub.add_parser("loop")
    research_loop.add_argument("--task", required=True)
    research_loop.add_argument("--research-root", default=".research")
    research_loop.add_argument(
        "--retain-diagnostics",
        action="store_true",
        help="retain disposable post-run diagnostics under the research cache",
    )
    research_loop.set_defaults(func=command_research_loop)
    research_report = research_sub.add_parser("report")
    research_report.add_argument("--task", required=True)
    research_report.add_argument("--research-root", default=".research")
    research_report.add_argument("--run", type=int)
    research_report.set_defaults(func=command_research_report)
    research_clean = research_sub.add_parser("clean")
    research_clean_target = research_clean.add_mutually_exclusive_group(required=True)
    research_clean_target.add_argument("--task")
    research_clean_target.add_argument("--task-id")
    research_clean.add_argument("--research-root", default=".research")
    research_clean.set_defaults(func=command_research_clean)
    research_test = research_sub.add_parser("test")
    research_test.add_argument("--task", required=True)
    research_test.add_argument("--research-root", default=".research")
    research_test.set_defaults(func=command_research_test)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
