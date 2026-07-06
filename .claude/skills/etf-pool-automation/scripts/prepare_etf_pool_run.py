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
    automation_dir_from_args,
    fill_default_start,
    log_step,
    prepare_reviewed_candidates,
    require_reviewed_candidates,
    reset_run_dir,
    run_command,
    write_reviewed_candidate_outputs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare reviewed ETF candidates and refresh data for pool optimization.",
    )
    add_common_run_args(parser)
    parser.add_argument("--candidates", required=True, help="Reviewed comma-separated candidate symbols.")
    return parser.parse_args()


def main() -> None:
    whole_start = time.monotonic()
    args = parse_args()
    require_reviewed_candidates(args)
    fill_default_start(args)
    automation_dir = automation_dir_from_args(args)
    candidate_state = prepare_reviewed_candidates(args, automation_dir)

    log_step(f"reset run directory: {automation_dir}")
    reset_run_dir(automation_dir)
    write_reviewed_candidate_outputs(candidate_state, automation_dir)
    run_command(
        [
            "python3",
            "-m",
            "quant_core.cli",
            "--root",
            args.data_root,
            "data",
            "update",
            "--start",
            args.start,
            "--end",
            args.date,
            "--universe",
            str(automation_dir / "expanded_refresh_universe.csv"),
            "--universe-name",
            "sector-rotation",
            "--adjust",
            "qfq",
            "--force-refresh",
        ],
        "data update",
    )
    log_step(f"prepare stage complete ({time.monotonic() - whole_start:.1f}s)")


if __name__ == "__main__":
    main()
