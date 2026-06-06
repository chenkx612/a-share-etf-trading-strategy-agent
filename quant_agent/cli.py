from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quant_agent.backtest.engine import factor_ic, run_backtest
from quant_agent.config import (
    LARGE_ETF_UNIVERSE_NAME,
    SECTOR_FACTOR_THRESHOLD_CORR_WINDOW,
    SECTOR_FACTOR_THRESHOLD_CORR_THRESHOLD,
    SECTOR_FACTOR_THRESHOLD_LOWER_BOUND,
    SECTOR_FACTOR_THRESHOLD_STOP_LOSS_PCT,
    SECTOR_SHARPE_CORR_WINDOW,
    SECTOR_SHARPE_CORR_THRESHOLD,
    SECTOR_SHARPE_STOP_LOSS_PCT,
    StrategyConfig,
    available_universe_names,
    get_universe_config,
)
from quant_agent.data.provider import AkshareETFProvider, merge_incremental, validate_daily
from quant_agent.factors import compute_factors, normalize_sharpe_windows
from quant_agent.paths import ProjectPaths
from quant_agent.reporting import build_markdown_report
from quant_agent.storage import read_table, write_table
from quant_agent.strategy.sector_rotation import select_sector_factor_threshold, select_sector_sharpe
from quant_agent.strategy.selection import score_and_select, score_factors


STRATEGY_CHOICES = ["multifactor", "sharpe-single", "sector-sharpe", "sector-factor-threshold"]
OPTIMIZATION_CONSTRAINT_CHOICES = ["none", "drawdown-lt-return"]


def parse_int_list(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def parse_sharpe_windows(value: str) -> list[int]:
    return list(normalize_sharpe_windows(parse_int_list(value)))


def strategy_uses_sharpe_window(strategy: str) -> bool:
    return strategy in {"sharpe-single", "sector-sharpe", "sector-factor-threshold"}


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


def build_strategy_config(args: argparse.Namespace) -> StrategyConfig:
    strategy = getattr(args, "strategy", "multifactor")
    sharpe_window = getattr(args, "sharpe_window", None)
    if strategy == "sector-factor-threshold":
        config = StrategyConfig.sector_factor_threshold_rotation(
            **({"sharpe_window": sharpe_window} if sharpe_window is not None else {})
        )
    elif strategy == "sector-sharpe":
        config = StrategyConfig.sector_sharpe_rotation(
            **({"sharpe_window": sharpe_window} if sharpe_window is not None else {})
        )
    elif strategy == "sharpe-single":
        config = StrategyConfig.sharpe_single_factor(
            **({"sharpe_window": sharpe_window} if sharpe_window is not None else {})
        )
    else:
        config = StrategyConfig()
    if getattr(args, "top_n", None) is not None:
        config = replace(config, top_n=args.top_n)
    if getattr(args, "fee_rate", None) is not None:
        config = replace(config, fee_rate=args.fee_rate)
    return config


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


def config_sharpe_windows(config: StrategyConfig) -> list[int]:
    windows = []
    for factor in config.factor_weights:
        if factor.startswith("sharpe_"):
            windows.append(int(factor.removeprefix("sharpe_")))
    return windows


def select_for_strategy(
    strategy: str,
    factors: pd.DataFrame,
    config: StrategyConfig,
    start: pd.Timestamp,
    end: pd.Timestamp,
    universe_symbols_: set[str],
    factor_lower_bound: float | None = None,
    corr_window: int | None = None,
    corr_threshold: float | None = None,
    stop_loss_pct: float | None = None,
) -> tuple[pd.DataFrame, bool]:
    if strategy == "sector-factor-threshold":
        return (
            select_sector_factor_threshold(
                factors,
                config,
                start=start,
                end=end,
                universe_symbols=universe_symbols_,
                corr_window=SECTOR_FACTOR_THRESHOLD_CORR_WINDOW if corr_window is None else corr_window,
                corr_threshold=(
                    SECTOR_FACTOR_THRESHOLD_CORR_THRESHOLD if corr_threshold is None else corr_threshold
                ),
                stop_loss_pct=(
                    SECTOR_FACTOR_THRESHOLD_STOP_LOSS_PCT if stop_loss_pct is None else stop_loss_pct
                ),
                factor_lower_bound=(
                    SECTOR_FACTOR_THRESHOLD_LOWER_BOUND if factor_lower_bound is None else factor_lower_bound
                ),
            ),
            True,
        )
    if strategy == "sector-sharpe":
        return (
            select_sector_sharpe(
                factors,
                config,
                start=start,
                end=end,
                universe_symbols=universe_symbols_,
                corr_window=SECTOR_SHARPE_CORR_WINDOW if corr_window is None else corr_window,
                corr_threshold=SECTOR_SHARPE_CORR_THRESHOLD if corr_threshold is None else corr_threshold,
                stop_loss_pct=SECTOR_SHARPE_STOP_LOSS_PCT if stop_loss_pct is None else stop_loss_pct,
            ),
            True,
        )
    return score_and_select(factors, config, start=start, end=end), False


def universe_table_base(paths: ProjectPaths, universe_name: str) -> Path:
    return paths.data_universe / f"{universe_name.replace('-', '_')}_universe"


def resolve_data_universe(args: argparse.Namespace, paths: ProjectPaths) -> tuple[pd.DataFrame, str, bool]:
    if args.universe and Path(args.universe).exists():
        return pd.read_csv(args.universe), "custom", False

    universe_config = get_universe_config(args.universe_name)
    if universe_config.source == "static":
        return universe_config.to_frame(), universe_config.name, False
    if universe_config.source == "akshare_etf":
        provider = AkshareETFProvider(adjust=effective_adjust(args))
        if universe_config.min_fund_size_cny is None:
            raise ValueError(f"Universe {universe_config.name!r} is missing min_fund_size_cny")
        return provider.fetch_universe(universe_config.min_fund_size_cny), universe_config.name, True
    raise ValueError(f"Unsupported universe source: {universe_config.source}")


def effective_adjust(args: argparse.Namespace) -> str:
    if getattr(args, "adjust", None) is not None:
        return args.adjust
    return get_universe_config(args.universe_name).default_adjust


def load_strategy_universe(paths: ProjectPaths, universe_name: str) -> pd.DataFrame:
    universe_config = get_universe_config(universe_name)
    if universe_config.source == "static":
        return universe_config.to_frame()
    try:
        return read_table(universe_table_base(paths, universe_name))
    except FileNotFoundError:
        if universe_name == LARGE_ETF_UNIVERSE_NAME:
            return read_table(paths.data_universe / "etf_universe")
        raise


def universe_symbols(paths: ProjectPaths, universe_name: str) -> set[str]:
    universe = load_strategy_universe(paths, universe_name)
    return set(universe["symbol"].astype(str))


def filter_factors_by_universe(factors: pd.DataFrame, paths: ProjectPaths, universe_name: str) -> pd.DataFrame:
    symbols = universe_symbols(paths, universe_name)
    return factors[factors["symbol"].astype(str).isin(symbols)].copy()


def read_daily(paths: ProjectPaths) -> pd.DataFrame:
    try:
        return read_table(paths.data_processed / "etf_daily", parse_dates=["date"])
    except FileNotFoundError:
        return read_table(paths.data_raw / "etf_daily", parse_dates=["date"])


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def command_data_update(args: argparse.Namespace) -> None:
    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    provider = AkshareETFProvider(adjust=effective_adjust(args))
    universe, universe_name, write_compat = resolve_data_universe(args, paths)
    universe_path = universe_table_base(paths, universe_name)
    write_table(universe, universe_path)
    if write_compat:
        write_table(universe, paths.data_universe / "etf_universe")
    incoming = provider.fetch_daily(universe, parse_date(args.start), parse_date(args.end))
    try:
        existing = read_table(paths.data_raw / "etf_daily", parse_dates=["date"])
    except FileNotFoundError:
        existing = None
    daily = merge_incremental(existing, incoming)
    problems = validate_daily(daily)
    raw_path = write_table(daily, paths.data_raw / "etf_daily")
    processed_path = write_table(daily, paths.data_processed / "etf_daily")
    print(f"wrote {len(daily)} rows to {raw_path}")
    print(f"wrote {len(daily)} standardized rows to {processed_path}")
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
    factors = ensure_sharpe_factor_columns(factors, daily, config_sharpe_windows(config))
    symbols = universe_symbols(paths, args.universe_name)
    factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    selected, sector_rotation = select_for_strategy(
        args.strategy,
        factors,
        config,
        start,
        end,
        symbols,
        factor_lower_bound=getattr(args, "factor_lower_bound", None),
        corr_window=getattr(args, "corr_window", None),
        corr_threshold=getattr(args, "corr_threshold", None),
        stop_loss_pct=getattr(args, "stop_loss_pct", None),
    )
    result = run_backtest(daily, selected, fee_rate=config.fee_rate)
    if sector_rotation:
        metrics = result.metrics
    else:
        scored = score_factors(factors, config)
        metrics = {**result.metrics, **factor_ic(scored)}
    run_id = args.run_id or f"{args.start}_{args.end}_{args.strategy}_{args.universe_name}_top{config.top_n}"
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
    sharpe_windows = (
        parse_sharpe_windows(args.sharpe_window)
        if strategy_uses_sharpe_window(args.strategy)
        else []
    )
    if sharpe_windows:
        factors = ensure_sharpe_factor_columns(factors, daily, sharpe_windows)
    symbols = universe_symbols(paths, args.universe_name)
    factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    rows = []
    for top_n in parse_int_list(args.top_n):
        for fee_rate in parse_float_list(args.fee_rate):
            candidate_sharpe_windows = sharpe_windows or [None]
            factor_lower_bounds = [None]
            corr_windows = [None]
            corr_thresholds = [None]
            stop_loss_pcts = [None]
            if args.strategy == "sector-factor-threshold":
                factor_lower_bounds = parse_float_list(args.factor_lower_bound)
                corr_windows = parse_int_list(args.corr_window)
                corr_thresholds = parse_float_list(args.corr_threshold)
                stop_loss_pcts = parse_float_list(args.stop_loss_pct)
            elif args.strategy == "sector-sharpe":
                corr_windows = parse_int_list(args.corr_window)
                corr_thresholds = parse_float_list(args.corr_threshold)
                stop_loss_pcts = parse_float_list(args.stop_loss_pct)

            for sharpe_window in candidate_sharpe_windows:
                for factor_lower_bound in factor_lower_bounds:
                    for corr_window in corr_windows:
                        for corr_threshold in corr_thresholds:
                            for stop_loss_pct in stop_loss_pcts:
                                config_args = argparse.Namespace(
                                    strategy=args.strategy,
                                    top_n=top_n,
                                    fee_rate=fee_rate,
                                    sharpe_window=sharpe_window,
                                )
                                config = build_strategy_config(config_args)
                                selected, sector_rotation = select_for_strategy(
                                    args.strategy,
                                    factors,
                                    config,
                                    start,
                                    end,
                                    symbols,
                                    factor_lower_bound=factor_lower_bound,
                                    corr_window=corr_window,
                                    corr_threshold=corr_threshold,
                                    stop_loss_pct=stop_loss_pct,
                                )
                                result = run_backtest(daily, selected, fee_rate=config.fee_rate)
                                if sector_rotation:
                                    metrics = result.metrics
                                else:
                                    scored = score_factors(factors, config)
                                    metrics = {**result.metrics, **factor_ic(scored)}
                                rows.append({
                                    "strategy": args.strategy,
                                    "top_n": top_n,
                                    "fee_rate": fee_rate,
                                    "sharpe_window": sharpe_window,
                                    "factor_lower_bound": factor_lower_bound,
                                    "corr_window": corr_window,
                                    "corr_threshold": corr_threshold,
                                    "stop_loss_pct": stop_loss_pct,
                                    "valid": metrics_satisfy_constraint(metrics, args.constraint),
                                    **metrics,
                                })
    results = pd.DataFrame(rows)
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
    factors = ensure_sharpe_factor_columns(factors, daily, config_sharpe_windows(config))
    symbols = universe_symbols(paths, args.universe_name)
    factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    selected, _ = select_for_strategy(
        args.strategy,
        factors,
        config,
        target_date,
        target_date,
        symbols,
        factor_lower_bound=getattr(args, "factor_lower_bound", None),
        corr_window=getattr(args, "corr_window", None),
        corr_threshold=getattr(args, "corr_threshold", None),
        stop_loss_pct=getattr(args, "stop_loss_pct", None),
    )
    out = paths.outputs / "recommendations" / f"{args.date}_{args.universe_name}.csv"
    selected.to_csv(out, index=False)
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
    data_update.add_argument("--universe-name", choices=available_universe_names(), default=LARGE_ETF_UNIVERSE_NAME)
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
    backtest_run.add_argument("--strategy", choices=STRATEGY_CHOICES, default="multifactor")
    backtest_run.add_argument("--universe-name", choices=available_universe_names(), default=LARGE_ETF_UNIVERSE_NAME)
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
    optimize_grid.add_argument("--strategy", choices=STRATEGY_CHOICES, default="sharpe-single")
    optimize_grid.add_argument("--universe-name", choices=available_universe_names(), default=LARGE_ETF_UNIVERSE_NAME)
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
    recommend_today.add_argument("--strategy", choices=STRATEGY_CHOICES, default="multifactor")
    recommend_today.add_argument("--universe-name", choices=available_universe_names(), default=LARGE_ETF_UNIVERSE_NAME)
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
