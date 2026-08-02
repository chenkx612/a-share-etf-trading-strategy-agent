from __future__ import annotations

import argparse

from quant_core.commands.analysis import (
    OPTIMIZATION_CONSTRAINT_CHOICES,
    STRATEGY_CHOICES,
    STRATEGY_NAME,
    build_markdown_report,
    build_strategy_params,
    command_backtest_run,
    command_data_update,
    command_factor_compute,
    command_optimize_grid,
    command_recommend_today,
    command_report_build,
    effective_adjust,
    ensure_sharpe_factor_columns,
    load_strategy_universe,
    metrics_satisfy_constraint,
    optimization_grid_results,
    parse_date,
    parse_float_list,
    parse_int_list,
    parse_sharpe_windows,
    recommendation_output_frame,
    read_daily,
    resolve_data_universe,
    sort_optimization_results,
)
from quant_core.commands.recommendation import command_recommend
from quant_core.commands.research import (
    command_loop,
    command_research_clean,
    command_research_loop,
    command_research_report,
    command_research_run_once,
    resolve_research_task_reference,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-agent")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="group", required=True)

    loop = sub.add_parser("loop", help="run a managed research loop")
    loop.add_argument("task", help="task file, tasks/<name>.toml stem, or task id")
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
    optimize_grid.add_argument(
        "--constraint",
        choices=OPTIMIZATION_CONSTRAINT_CHOICES,
        default="none",
    )
    optimize_grid.add_argument("--run-id")
    optimize_grid.add_argument("--show", type=int, default=5)
    optimize_grid.set_defaults(func=command_optimize_grid)

    recommend = sub.add_parser(
        "recommend",
        help="generate a production recommendation for a managed task",
    )
    recommend.add_argument("task", help="task file, tasks/<name>.toml stem, or task id")
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
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
