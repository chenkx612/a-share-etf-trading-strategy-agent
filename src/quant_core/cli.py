from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from quant_core.backtest.engine import run_backtest
from quant_core.config import (
    SHARPE_CORR_THRESHOLD_CORR_THRESHOLD,
    SHARPE_CORR_THRESHOLD_CORR_WINDOW,
    SHARPE_CORR_THRESHOLD_LOWER_BOUND,
    SHARPE_CORR_THRESHOLD_STOP_LOSS_PCT,
    STRATEGY_NAME,
    StrategyConfig,
)
from quant_core.data.market_data import (
    AkshareMarketDataClient,
    DEFAULT_ADJUST,
    ProjectPaths,
    fetch_daily_if_stale,
    load_universe,
    merge_incremental,
    read_daily,
    read_table,
    universe_symbols,
    validate_daily,
    write_table,
)
from quant_core.factors import compute_factors, normalize_sharpe_windows
from quant_core.strategy.sharpe_corr_threshold import select_sharpe_corr_threshold


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
    fee_rates: Iterable[float],
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
    candidate_sharpe_windows = list(sharpe_windows or [None])
    for top_n in top_ns:
        for fee_rate in fee_rates:
            for sharpe_window in candidate_sharpe_windows:
                for factor_lower_bound in factor_lower_bounds:
                    for corr_window in corr_windows:
                        for corr_threshold in corr_thresholds:
                            for stop_loss_pct in stop_loss_pcts:
                                config = build_strategy_config(argparse.Namespace(
                                    strategy=strategy,
                                    top_n=top_n,
                                    fee_rate=fee_rate,
                                    sharpe_window=sharpe_window,
                                ))
                                selected = select_sharpe_corr_threshold(
                                    factors,
                                    config,
                                    start=start,
                                    end=end,
                                    universe_symbols=symbols,
                                    factor_lower_bound=(
                                        factor_lower_bound
                                        if factor_lower_bound is not None
                                        else SHARPE_CORR_THRESHOLD_LOWER_BOUND
                                    ),
                                    corr_window=(
                                        corr_window
                                        if corr_window is not None
                                        else SHARPE_CORR_THRESHOLD_CORR_WINDOW
                                    ),
                                    corr_threshold=(
                                        corr_threshold
                                        if corr_threshold is not None
                                        else SHARPE_CORR_THRESHOLD_CORR_THRESHOLD
                                    ),
                                    stop_loss_pct=(
                                        stop_loss_pct
                                        if stop_loss_pct is not None
                                        else SHARPE_CORR_THRESHOLD_STOP_LOSS_PCT
                                    ),
                                )
                                result = run_backtest(daily, selected, fee_rate=config.fee_rate)
                                metrics = result.metrics
                                rows.append({
                                    "strategy": STRATEGY_NAME,
                                    "top_n": top_n,
                                    "fee_rate": fee_rate,
                                    "sharpe_window": sharpe_window,
                                    "factor_lower_bound": factor_lower_bound,
                                    "corr_window": corr_window,
                                    "corr_threshold": corr_threshold,
                                    "stop_loss_pct": stop_loss_pct,
                                    "valid": metrics_satisfy_constraint(metrics, constraint),
                                    **metrics,
                                })
    return pd.DataFrame(rows)


def build_strategy_config(args: argparse.Namespace) -> StrategyConfig:
    strategy = getattr(args, "strategy", STRATEGY_NAME)
    if strategy != STRATEGY_NAME:
        raise ValueError(f"Unknown strategy: {strategy}")
    defaults = StrategyConfig()
    return StrategyConfig(
        top_n=getattr(args, "top_n", None) or defaults.top_n,
        fee_rate=getattr(args, "fee_rate", None) or defaults.fee_rate,
        sharpe_window=getattr(args, "sharpe_window", None) or defaults.sharpe_window,
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
        force_refresh=getattr(args, "force_refresh", False),
        log=print,
    )
    daily = merge_incremental(existing, incoming)
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
    config = build_strategy_config(args)
    daily = read_daily(paths)
    factors = read_table(paths.outputs / "factors" / "factors", parse_dates=["date"])
    factors = ensure_sharpe_factor_columns(factors, daily, [config.sharpe_window])
    universe = load_strategy_universe(args)
    symbols = universe_symbols(universe)
    factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    selected = select_sharpe_corr_threshold(
        factors,
        config,
        start=start,
        end=end,
        universe_symbols=symbols,
        factor_lower_bound=getattr(args, "factor_lower_bound", None),
        corr_window=getattr(args, "corr_window", None),
        corr_threshold=getattr(args, "corr_threshold", None),
        stop_loss_pct=getattr(args, "stop_loss_pct", None),
    )
    result = run_backtest(daily, selected, fee_rate=config.fee_rate)
    metrics = result.metrics
    run_id = args.run_id or f"{args.start}_{args.end}_{STRATEGY_NAME}_{args.universe_name}_top{config.top_n}"
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
        fee_rates=parse_float_list(args.fee_rate),
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
    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    config = build_strategy_config(args)
    target_date = pd.Timestamp(args.date)
    factors = read_table(paths.outputs / "factors" / "factors", parse_dates=["date"])
    daily = read_daily(paths)
    factors = ensure_sharpe_factor_columns(factors, daily, [config.sharpe_window])
    universe = load_strategy_universe(args)
    symbols = universe_symbols(universe)
    factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    selected = select_sharpe_corr_threshold(
        factors,
        config,
        start=target_date,
        end=target_date,
        universe_symbols=symbols,
        factor_lower_bound=getattr(args, "factor_lower_bound", None),
        corr_window=getattr(args, "corr_window", None),
        corr_threshold=getattr(args, "corr_threshold", None),
        stop_loss_pct=getattr(args, "stop_loss_pct", None),
    )
    out = paths.outputs / "recommendations" / f"{args.date}_{args.universe_name}.csv"
    recommendation_output_frame(selected).to_csv(out, index=False)
    print(f"wrote {len(selected)} recommendations to {out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-agent")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="group", required=True)

    data = sub.add_parser("data")
    data_sub = data.add_subparsers(dest="command", required=True)
    data_update = data_sub.add_parser("update")
    data_update.add_argument("--start", required=True)
    data_update.add_argument("--end", required=True)
    data_update.add_argument("--universe")
    data_update.add_argument("--universe-name", default="default")
    data_update.add_argument("--adjust")
    data_update.add_argument("--force-refresh", action="store_true")
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
    backtest_run.add_argument("--fee-rate", type=float)
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
    optimize_grid.add_argument("--fee-rate", default="0.0003,0.001")
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

    recommend = sub.add_parser("recommend")
    recommend_sub = recommend.add_subparsers(dest="command", required=True)
    recommend_today = recommend_sub.add_parser("today")
    recommend_today.add_argument("--date", required=True)
    recommend_today.add_argument("--strategy", choices=STRATEGY_CHOICES, default=STRATEGY_NAME)
    recommend_today.add_argument("--universe", required=True)
    recommend_today.add_argument("--universe-name", default="default")
    recommend_today.add_argument("--top-n", type=int)
    recommend_today.add_argument("--fee-rate", type=float)
    recommend_today.add_argument("--sharpe-window", type=int)
    recommend_today.add_argument("--factor-lower-bound", type=float)
    recommend_today.add_argument("--corr-window", type=int)
    recommend_today.add_argument("--corr-threshold", type=float)
    recommend_today.add_argument("--stop-loss-pct", type=float)
    recommend_today.set_defaults(func=command_recommend_today)

    report = sub.add_parser("report")
    report_sub = report.add_subparsers(dest="command", required=True)
    report_build = report_sub.add_parser("build")
    report_build.add_argument("--run-id", required=True)
    report_build.set_defaults(func=command_report_build)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
