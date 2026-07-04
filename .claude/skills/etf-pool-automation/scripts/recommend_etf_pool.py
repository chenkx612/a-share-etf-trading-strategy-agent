#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from utils import (  # noqa: E402
    add_common_run_args,
    add_optimization_args,
    automation_dir_from_args,
    cleanup_intermediate_outputs,
    fill_default_start,
    generate_recommendations,
    log_step,
    resolve_recommendation_date,
    strict_recent_universe_backfill,
    write_summary,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate recommendations and automation summary for the optimized ETF pool.",
    )
    add_common_run_args(parser)
    add_optimization_args(parser)
    parser.add_argument("--apply", action="store_true", help="Fallback summary value; optimize stage state wins.")
    return parser.parse_args()


def main() -> None:
    whole_start = time.monotonic()
    args = parse_args()
    fill_default_start(args)
    automation_dir = automation_dir_from_args(args)
    verified = strict_recent_universe_backfill(
        args,
        automation_dir / "selected_universe.csv",
        label="selected universe",
    )
    recommendation_date = resolve_recommendation_date(args, verified, automation_dir / "selected_universe.csv")
    recommendation_path = generate_recommendations(args, automation_dir, recommendation_date)
    write_summary(args, automation_dir, recommendation_path, recommendation_date)
    cleanup_intermediate_outputs(automation_dir)
    log_step(f"wrote automation summary to {automation_dir / 'automation_summary.json'}")
    log_step(f"recommend stage complete ({time.monotonic() - whole_start:.1f}s)")


if __name__ == "__main__":
    main()
