from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from quant_core.commands.research import resolve_research_task_reference
from quant_core.recommendation import run_recommendation


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
    recommendation = pd.read_csv(
        root / summary["recommendation_path"],
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
            formatters={"target_weight": lambda value: f"{float(value):.2%}"},
        )
    )
    print(f"recent causal return curve: {root / summary['curve_png_path']}")
