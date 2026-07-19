#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_BENCHMARK = (
    REPO_ROOT
    / ".agents"
    / "skills"
    / "etf-vol-adaptive-topk"
    / "references"
    / "csi300_benchmark.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh the qfq CSI 300 ETF benchmark used by research charts.",
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--data-root", default=".")
    parser.add_argument("--benchmark-universe", default=str(DEFAULT_BENCHMARK))
    return parser.parse_args()


def five_year_start(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    try:
        return parsed.replace(year=parsed.year - 5).isoformat()
    except ValueError:
        return parsed.replace(year=parsed.year - 5, day=28).isoformat()


def main() -> None:
    args = parse_args()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "quant_core.cli",
            "--root",
            str(Path(args.data_root).resolve()),
            "data",
            "update",
            "--start",
            five_year_start(args.date),
            "--end",
            args.date,
            "--universe",
            str(Path(args.benchmark_universe).resolve()),
            "--universe-name",
            "csi300-benchmark",
            "--adjust",
            "qfq",
        ],
        cwd=REPO_ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
