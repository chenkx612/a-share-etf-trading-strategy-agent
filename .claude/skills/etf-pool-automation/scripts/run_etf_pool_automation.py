#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from select_etf_candidates import main as select_main  # noqa: E402
from quant_agent.data.provider import AkshareETFProvider, merge_incremental, validate_daily  # noqa: E402
from quant_agent.paths import ProjectPaths  # noqa: E402
from quant_agent.storage import read_table, write_table  # noqa: E402
from quant_agent.cli import command_automation_etf_pool, command_factor_compute, command_recommend_today  # noqa: E402


DEFAULT_RUN_ID = "current"
DEFAULT_TOP_N = "4,5,6"
DEFAULT_FEE_RATE = "0.0003"
DEFAULT_SHARPE_WINDOW = "15,20,25,30,35"
DEFAULT_FACTOR_LOWER_BOUND = "-1.0,-0.5,0.0,0.5,1.0"
DEFAULT_CORR_WINDOW = "100"
DEFAULT_CORR_THRESHOLD = "0.9"
DEFAULT_STOP_LOSS_PCT = "0.1"
DEFAULT_OBJECTIVE = "sortino"
DEFAULT_CONSTRAINT = "drawdown-lt-return"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ETF pool automation end to end.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--root", default="workspaces/sector_rotation")
    parser.add_argument("--start", help="Backtest start date. Defaults to three years before --date.")
    parser.add_argument("--run-id")
    parser.add_argument("--candidates", help="Optional reviewed comma-separated symbol override.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--candidate-only", action="store_true")
    parser.add_argument("--top-n", default=DEFAULT_TOP_N)
    parser.add_argument("--fee-rate", default=DEFAULT_FEE_RATE)
    parser.add_argument("--sharpe-window", default=DEFAULT_SHARPE_WINDOW)
    parser.add_argument("--factor-lower-bound", default=DEFAULT_FACTOR_LOWER_BOUND)
    parser.add_argument("--corr-window", default=DEFAULT_CORR_WINDOW)
    parser.add_argument("--corr-threshold", default=DEFAULT_CORR_THRESHOLD)
    parser.add_argument("--stop-loss-pct", default=DEFAULT_STOP_LOSS_PCT)
    parser.add_argument("--objective", default=DEFAULT_OBJECTIVE)
    parser.add_argument("--constraint", default=DEFAULT_CONSTRAINT)
    return parser.parse_args()


def run_dir(root: Path, run_id: str) -> Path:
    return root / "outputs" / "automations" / "etf_pool" / run_id


def default_start(trade_date: str) -> str:
    parsed = datetime.strptime(trade_date, "%Y-%m-%d").date()
    try:
        return parsed.replace(year=parsed.year - 3).isoformat()
    except ValueError:
        return parsed.replace(year=parsed.year - 3, day=28).isoformat()


def reset_run_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def log_step(message: str) -> None:
    print(f"[etf-pool] {message}", flush=True)


def run_command(args: list[str], label: str) -> None:
    start = time.monotonic()
    log_step(f"START {label}")
    print("+ " + " ".join(args))
    try:
        subprocess.run(args, cwd=REPO_ROOT, check=True)
    finally:
        elapsed = time.monotonic() - start
        log_step(f"END {label} ({elapsed:.1f}s)")


def run_selector(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    start = time.monotonic()
    log_step("START candidate discovery")
    selector_argv = [
        "select_etf_candidates.py",
        "--date",
        args.date,
        "--root",
        args.root,
        "--output-dir",
        str(output_dir),
    ]
    if args.candidates:
        selector_argv.extend(["--candidates", args.candidates])

    old_argv = sys.argv
    try:
        sys.argv = selector_argv
        select_main()
    finally:
        sys.argv = old_argv
        elapsed = time.monotonic() - start
        log_step(f"END candidate discovery ({elapsed:.1f}s)")
    payload = json.loads((output_dir / "candidate_selected.json").read_text(encoding="utf-8"))
    log_step(f"selected candidates: {payload['candidate_arg']}")
    return payload


def prepare_recommend_workspace(root: Path, automation_dir: Path) -> Path:
    start = time.monotonic()
    log_step("START prepare dry-run recommendation workspace")
    workspace = automation_dir / "recommend_workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    for relative in [
        Path("data") / "raw",
        Path("data") / "processed",
        Path("data") / "universe",
        Path("outputs") / "factors",
    ]:
        source = root / relative
        if source.exists():
            shutil.copytree(source, workspace / relative)
    universe_dir = workspace / "data" / "universe"
    universe_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(automation_dir / "selected_universe.csv", universe_dir / "sector_rotation_universe.csv")
    elapsed = time.monotonic() - start
    log_step(f"END prepare dry-run recommendation workspace ({elapsed:.1f}s)")
    return workspace


def best_int(best: dict[str, Any], key: str) -> str:
    return str(int(float(best[key])))


def best_float(best: dict[str, Any], key: str) -> str:
    return str(float(best[key]))


def generate_recommendations(args: argparse.Namespace, automation_dir: Path, recommendation_date: str) -> Path:
    best = json.loads((automation_dir / "best.json").read_text(encoding="utf-8"))
    rec_root = Path(args.root) if args.apply else prepare_recommend_workspace(Path(args.root), automation_dir)

    start = time.monotonic()
    log_step("START factor compute for recommendation")
    command_factor_compute(
        argparse.Namespace(
            root=str(rec_root),
            start=args.start,
            end=recommendation_date,
            sharpe_window=best_int(best, "sharpe_window"),
        )
    )
    log_step(f"END factor compute for recommendation ({time.monotonic() - start:.1f}s)")

    start = time.monotonic()
    log_step("START generate recommendation")
    command_recommend_today(
        argparse.Namespace(
            root=str(rec_root),
            date=recommendation_date,
            strategy="sector-factor-threshold",
            universe_name="sector-rotation",
            top_n=int(float(best["top_n"])),
            fee_rate=float(best["fee_rate"]),
            sharpe_window=int(float(best["sharpe_window"])),
            factor_lower_bound=float(best["factor_lower_bound"]),
            corr_window=int(float(best["corr_window"])),
            corr_threshold=float(best["corr_threshold"]),
            stop_loss_pct=float(best["stop_loss_pct"]),
        )
    )
    log_step(f"END generate recommendation ({time.monotonic() - start:.1f}s)")
    recommendation_path = rec_root / "outputs" / "recommendations" / f"{recommendation_date}_sector-rotation.csv"
    recommendations = pd.read_csv(recommendation_path) if recommendation_path.exists() else pd.DataFrame()
    if recommendations.empty:
        raise RuntimeError(f"Recommendation output is empty for {recommendation_date}; refusing to report success")
    return recommendation_path


def run_pool_optimization(args: argparse.Namespace, candidates: str, run_id: str) -> None:
    start = time.monotonic()
    log_step("START pool optimization grid")
    command_automation_etf_pool(
        argparse.Namespace(
            root=args.root,
            date=args.date,
            candidates=candidates,
            start=args.start,
            end=args.date,
            universe_name="sector-rotation",
            top_n=args.top_n,
            fee_rate=args.fee_rate,
            sharpe_window=args.sharpe_window,
            factor_lower_bound=args.factor_lower_bound,
            corr_window=args.corr_window,
            corr_threshold=args.corr_threshold,
            stop_loss_pct=args.stop_loss_pct,
            objective=args.objective,
            constraint=args.constraint,
            run_id=run_id,
            show=4,
            apply=args.apply,
        )
    )
    log_step(f"END pool optimization grid ({time.monotonic() - start:.1f}s)")


def local_daily(paths: ProjectPaths) -> pd.DataFrame:
    try:
        return read_table(paths.data_processed / "etf_daily", parse_dates=["date"])
    except FileNotFoundError:
        return read_table(paths.data_raw / "etf_daily", parse_dates=["date"])


def fetch_tencent_daily_for_universe(
    provider: AkshareETFProvider,
    universe: pd.DataFrame,
    start: date,
    end: date,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for row in universe.itertuples(index=False):
        symbol = str(row.symbol)
        name = str(row.name)
        symbol_start = time.monotonic()
        log_step(f"fetch Tencent daily for {symbol}")
        frame = provider._fetch_daily_tencent(symbol, name, start, end)
        if frame.empty:
            log_step(f"no Tencent daily rows for {symbol} in {start}..{end}")
            continue
        frames.append(frame)
        log_step(f"fetched {len(frame)} rows for {symbol} ({time.monotonic() - symbol_start:.1f}s)")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def merge_and_store_daily(paths: ProjectPaths, incoming: pd.DataFrame, label: str) -> pd.DataFrame:
    if incoming.empty:
        raise RuntimeError(f"{label} returned no daily rows")
    try:
        existing_raw = read_table(paths.data_raw / "etf_daily", parse_dates=["date"])
    except FileNotFoundError:
        existing_raw = None
    merged = merge_incremental(existing_raw, incoming)
    problems = validate_daily(merged)
    write_table(merged, paths.data_raw / "etf_daily")
    write_table(merged, paths.data_processed / "etf_daily")
    if problems:
        print(f"data warnings after {label}:")
        for problem in problems:
            print(f"- {problem}")
    return local_daily(paths)


def strict_recent_universe_backfill(
    args: argparse.Namespace,
    universe_csv: Path,
    *,
    label: str,
    lookback_days: int = 10,
) -> pd.DataFrame:
    start_time = time.monotonic()
    log_step(f"START {label} Tencent recent backfill")
    paths = ProjectPaths(Path(args.root))
    universe = pd.read_csv(universe_csv)
    universe["symbol"] = universe["symbol"].astype(str)
    end = datetime.strptime(args.date, "%Y-%m-%d").date()
    start = max(datetime.strptime(args.start, "%Y-%m-%d").date(), end - timedelta(days=lookback_days))
    provider = AkshareETFProvider(adjust="qfq")
    incoming = fetch_tencent_daily_for_universe(provider, universe, start, end)
    verified = merge_and_store_daily(paths, incoming, label)
    verified["symbol"] = verified["symbol"].astype(str)
    log_step(
        f"END {label} Tencent recent backfill ({time.monotonic() - start_time:.1f}s): "
        f"incoming rows={len(incoming)}; verified rows={len(verified)}"
    )
    return verified


def complete_universe_dates(
    daily: pd.DataFrame,
    symbols: set[str],
    requested_date: str,
) -> list[pd.Timestamp]:
    if daily.empty:
        return []
    df = daily.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype(str)
    requested = pd.Timestamp(requested_date)
    symbol_count = len(symbols)
    counts = (
        df[(df["date"] <= requested) & df["symbol"].isin(symbols)]
        .drop_duplicates(["date", "symbol"])
        .groupby("date")["symbol"]
        .nunique()
    )
    return counts[counts == symbol_count].index.sort_values().tolist()


def resolve_recommendation_date(args: argparse.Namespace, daily: pd.DataFrame, universe_csv: Path) -> str:
    universe = pd.read_csv(universe_csv)
    symbols = set(universe["symbol"].astype(str))
    dates = complete_universe_dates(daily, symbols, args.date)
    if not dates:
        requested = pd.Timestamp(args.date)
        df = daily.copy()
        df["date"] = pd.to_datetime(df["date"])
        df["symbol"] = df["symbol"].astype(str)
        latest_available = df[df["date"] <= requested].groupby("symbol")["date"].max()
        missing = sorted(symbol for symbol in symbols if symbol not in latest_available)
        stale = {
            symbol: latest_available[symbol].date().isoformat()
            for symbol in sorted(symbols - set(missing))
        }
        raise RuntimeError(
            "No complete recommendation date is available for the selected universe "
            f"on or before {args.date}; missing={missing}; latest_by_symbol={stale}"
        )
    recommendation_date = dates[-1].date().isoformat()
    if recommendation_date != args.date:
        log_step(f"recommendation date adjusted from {args.date} to latest complete date {recommendation_date}")
    return recommendation_date


def write_summary(
    args: argparse.Namespace,
    automation_dir: Path,
    recommendation_path: Path,
    recommendation_date: str,
) -> None:
    log_step("START write automation summary")
    candidate_payload = json.loads((automation_dir / "candidate_selected.json").read_text(encoding="utf-8"))
    best = json.loads((automation_dir / "best.json").read_text(encoding="utf-8"))
    evaluations = pd.read_csv(automation_dir / "evaluations.csv")
    recommendations = pd.read_csv(recommendation_path) if recommendation_path.exists() else pd.DataFrame()
    payload = {
        "date": args.date,
        "recommendation_date": recommendation_date,
        "apply": args.apply,
        "automation_dir": str(automation_dir),
        "recommendation_path": str(recommendation_path),
        "candidates": candidate_payload["selected"],
        "candidate_arg": candidate_payload["candidate_arg"],
        "best": best,
        "evaluations": evaluations.to_dict(orient="records"),
        "recommendations": recommendations.to_dict(orient="records"),
    }
    (automation_dir / "automation_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_step("END write automation summary")


def main() -> None:
    whole_start = time.monotonic()
    args = parse_args()
    if args.start is None:
        args.start = default_start(args.date)
    root = Path(args.root)
    run_id = args.run_id or DEFAULT_RUN_ID
    automation_dir = run_dir(root, run_id)
    log_step(f"reset run directory: {automation_dir}")
    reset_run_dir(automation_dir)

    candidate_payload = run_selector(args, automation_dir)
    if args.candidate_only:
        log_step(f"candidate-only run complete ({time.monotonic() - whole_start:.1f}s)")
        return

    candidates = candidate_payload["candidate_arg"]
    run_command(
        [
            "python3",
            "-m",
            "quant_agent.cli",
            "--root",
            args.root,
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
        ],
        "data update",
    )
    strict_recent_universe_backfill(
        args,
        automation_dir / "expanded_refresh_universe.csv",
        label="expanded universe",
    )
    run_pool_optimization(args, candidates, run_id)
    verified = strict_recent_universe_backfill(
        args,
        automation_dir / "selected_universe.csv",
        label="selected universe",
    )
    recommendation_date = resolve_recommendation_date(args, verified, automation_dir / "selected_universe.csv")
    recommendation_path = generate_recommendations(args, automation_dir, recommendation_date)
    write_summary(args, automation_dir, recommendation_path, recommendation_date)
    log_step(f"wrote automation summary to {automation_dir / 'automation_summary.json'}")
    log_step(f"full run complete ({time.monotonic() - whole_start:.1f}s)")


if __name__ == "__main__":
    main()
