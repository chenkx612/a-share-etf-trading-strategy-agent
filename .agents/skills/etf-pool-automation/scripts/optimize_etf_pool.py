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
    DEFAULT_RUN_ID,
    add_common_run_args,
    add_optimization_args,
    automation_dir_from_args,
    fill_default_start,
    load_candidate_arg,
    log_step,
    run_pool_optimization,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Optimize ETF pools, run pruning challenge, and write the final selected universe.",
    )
    add_common_run_args(parser)
    add_optimization_args(parser)
    parser.add_argument("--candidates", help="Reviewed comma-separated candidate symbols. Defaults to prepared run state.")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    whole_start = time.monotonic()
    args = parse_args()
    fill_default_start(args)
    automation_dir = automation_dir_from_args(args)
    candidates = args.candidates or load_candidate_arg(automation_dir)
    run_pool_optimization(args, candidates, args.run_id or DEFAULT_RUN_ID)
    log_step(f"optimize stage complete ({time.monotonic() - whole_start:.1f}s)")


if __name__ == "__main__":
    main()
