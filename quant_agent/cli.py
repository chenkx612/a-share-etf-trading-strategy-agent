from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quant_agent.backtest.engine import factor_ic, run_backtest
from quant_agent.config import StrategyConfig
from quant_agent.data.provider import AkshareETFProvider, merge_incremental, validate_daily
from quant_agent.factors import compute_factors
from quant_agent.paths import ProjectPaths
from quant_agent.reporting import build_markdown_report
from quant_agent.storage import read_table, write_table
from quant_agent.strategy.selection import score_and_select, score_factors


def parse_int_list(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def build_strategy_config(args: argparse.Namespace) -> StrategyConfig:
    if getattr(args, "strategy", "multifactor") == "sharpe-single":
        config = StrategyConfig.sharpe_single_factor()
    else:
        config = StrategyConfig()
    if getattr(args, "top_n", None) is not None:
        config = replace(config, top_n=args.top_n)
    if getattr(args, "fee_rate", None) is not None:
        config = replace(config, fee_rate=args.fee_rate)
    return config


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def command_data_update(args: argparse.Namespace) -> None:
    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    config = StrategyConfig()
    provider = AkshareETFProvider(adjust=args.adjust)
    universe_path = paths.data_universe / "etf_universe"
    if args.universe and Path(args.universe).exists():
        universe = pd.read_csv(args.universe)
    else:
        universe = provider.fetch_universe(config.min_fund_size_cny)
        write_table(universe, universe_path)
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
    try:
        daily = read_table(paths.data_processed / "etf_daily", parse_dates=["date"])
    except FileNotFoundError:
        daily = read_table(paths.data_raw / "etf_daily", parse_dates=["date"])
    factors = compute_factors(daily)
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
    try:
        daily = read_table(paths.data_processed / "etf_daily", parse_dates=["date"])
    except FileNotFoundError:
        daily = read_table(paths.data_raw / "etf_daily", parse_dates=["date"])
    factors = read_table(paths.outputs / "factors" / "factors", parse_dates=["date"])
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    selected = score_and_select(factors, config, start=start, end=end)
    result = run_backtest(daily, selected, fee_rate=config.fee_rate)
    scored = score_factors(factors, config)
    metrics = {**result.metrics, **factor_ic(scored)}
    run_id = args.run_id or f"{args.start}_{args.end}_{args.strategy}_top{config.top_n}"
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
    try:
        daily = read_table(paths.data_processed / "etf_daily", parse_dates=["date"])
    except FileNotFoundError:
        daily = read_table(paths.data_raw / "etf_daily", parse_dates=["date"])
    factors = read_table(paths.outputs / "factors" / "factors", parse_dates=["date"])
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    rows = []
    for top_n in parse_int_list(args.top_n):
        for fee_rate in parse_float_list(args.fee_rate):
            config_args = argparse.Namespace(strategy=args.strategy, top_n=top_n, fee_rate=fee_rate)
            config = build_strategy_config(config_args)
            selected = score_and_select(factors, config, start=start, end=end)
            result = run_backtest(daily, selected, fee_rate=config.fee_rate)
            scored = score_factors(factors, config)
            metrics = {**result.metrics, **factor_ic(scored)}
            rows.append({
                "strategy": args.strategy,
                "top_n": top_n,
                "fee_rate": fee_rate,
                **metrics,
            })
    results = pd.DataFrame(rows)
    if results.empty:
        raise ValueError("No optimization results generated")
    if args.objective not in results.columns:
        raise ValueError(f"Objective {args.objective!r} is not available in results")
    results = results.sort_values(args.objective, ascending=False, na_position="last").reset_index(drop=True)
    run_id = args.run_id or f"{args.start}_{args.end}_{args.strategy}_grid"
    run_dir = paths.outputs / "optimizations" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(run_dir / "results.csv", index=False)
    (run_dir / "best.json").write_text(json.dumps(results.iloc[0].to_dict(), indent=2), encoding="utf-8")
    print(f"wrote optimization results to {run_dir}")
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
    factors = read_table(paths.outputs / "factors" / "factors", parse_dates=["date"])
    target_date = pd.Timestamp(args.date)
    selected = score_and_select(factors, config, start=target_date, end=target_date)
    out = paths.outputs / "recommendations" / f"{args.date}.csv"
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
    data_update.add_argument("--adjust", default="")
    data_update.set_defaults(func=command_data_update)

    factor = sub.add_parser("factor")
    factor_sub = factor.add_subparsers(dest="command", required=True)
    factor_compute = factor_sub.add_parser("compute")
    factor_compute.add_argument("--start")
    factor_compute.add_argument("--end")
    factor_compute.set_defaults(func=command_factor_compute)

    backtest = sub.add_parser("backtest")
    backtest_sub = backtest.add_subparsers(dest="command", required=True)
    backtest_run = backtest_sub.add_parser("run")
    backtest_run.add_argument("--start", required=True)
    backtest_run.add_argument("--end", required=True)
    backtest_run.add_argument("--strategy", choices=["multifactor", "sharpe-single"], default="multifactor")
    backtest_run.add_argument("--top-n", type=int)
    backtest_run.add_argument("--fee-rate", type=float)
    backtest_run.add_argument("--run-id")
    backtest_run.set_defaults(func=command_backtest_run)

    optimize = sub.add_parser("optimize")
    optimize_sub = optimize.add_subparsers(dest="command", required=True)
    optimize_grid = optimize_sub.add_parser("grid")
    optimize_grid.add_argument("--start", required=True)
    optimize_grid.add_argument("--end", required=True)
    optimize_grid.add_argument("--strategy", choices=["multifactor", "sharpe-single"], default="sharpe-single")
    optimize_grid.add_argument("--top-n", default="3,5,10")
    optimize_grid.add_argument("--fee-rate", default="0.0003,0.001")
    optimize_grid.add_argument("--objective", default="sharpe")
    optimize_grid.add_argument("--run-id")
    optimize_grid.add_argument("--show", type=int, default=5)
    optimize_grid.set_defaults(func=command_optimize_grid)

    recommend = sub.add_parser("recommend")
    recommend_sub = recommend.add_subparsers(dest="command", required=True)
    recommend_today = recommend_sub.add_parser("today")
    recommend_today.add_argument("--date", required=True)
    recommend_today.add_argument("--strategy", choices=["multifactor", "sharpe-single"], default="multifactor")
    recommend_today.add_argument("--top-n", type=int)
    recommend_today.add_argument("--fee-rate", type=float)
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
