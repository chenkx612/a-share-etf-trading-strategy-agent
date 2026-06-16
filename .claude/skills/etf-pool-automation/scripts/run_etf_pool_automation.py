#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent

from quant_core.data.provider import AkshareETFProvider  # noqa: E402
from quant_core.data.universe import (  # noqa: E402
    expanded_universe as build_expanded_universe,
    fetch_tencent_daily_if_stale,
    merge_and_store_daily,
    resolve_complete_universe_date,
)
from quant_core.paths import ProjectPaths  # noqa: E402
from quant_core.cli import (  # noqa: E402
    command_factor_compute,
    command_recommend_today,
    optimization_grid_results,
    parse_float_list,
    parse_int_list,
    parse_sharpe_windows,
    read_daily,
    sort_optimization_results,
)
from quant_core.factors import compute_factors  # noqa: E402
from quant_core.storage import read_table, write_table  # noqa: E402


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
DEFAULT_ROOT = ".claude/skills/etf-pool-automation/outputs"
DEFAULT_DATA_ROOT = "."
DEFAULT_UNIVERSE_NAME = "sector-rotation"
DEFAULT_BASE_POOL = SCRIPT_DIR.parent / "references" / "sector_rotation_universe.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ETF pool automation end to end.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    parser.add_argument("--start", help="Backtest start date. Defaults to three years before --date.")
    parser.add_argument("--run-id")
    parser.add_argument("--candidates", required=True, help="Reviewed comma-separated candidate symbols.")
    parser.add_argument("--apply", action="store_true")
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


def require_reviewed_candidates(args: argparse.Namespace) -> None:
    if args.candidates:
        return
    raise SystemExit(
        "Full automation requires reviewed candidates. "
        "Run select_etf_candidates.py first, review candidate_shortlist.csv, "
        "then rerun with --candidates SYMBOL1,SYMBOL2,SYMBOL3."
    )


def run_dir(root: Path, run_id: str) -> Path:
    if run_id == DEFAULT_RUN_ID:
        return root
    return root / run_id


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


def parse_candidates(value: str) -> list[str]:
    return [part.strip().split(":", 1)[0] for part in value.split(",") if part.strip()]


def load_base_pool() -> pd.DataFrame:
    frame = pd.read_csv(DEFAULT_BASE_POOL, dtype={"symbol": str})
    frame["symbol"] = frame["symbol"].astype(str)
    return frame


def csv_bool(value: object) -> bool:
    if pd.isna(value):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def prepare_reviewed_candidates(args: argparse.Namespace, automation_dir: Path) -> dict[str, Any]:
    shortlist_path = automation_dir / "candidate_shortlist.csv"
    original_selected_path = automation_dir / "candidate_selected.csv"
    if not shortlist_path.exists():
        raise RuntimeError(
            f"Missing {shortlist_path}; run select_etf_candidates.py before run_etf_pool_automation.py."
        )

    symbols = parse_candidates(args.candidates)
    if len(symbols) != 3:
        raise RuntimeError(f"Expected exactly 3 reviewed candidate symbols, got {len(symbols)}")
    if len(set(symbols)) != len(symbols):
        raise RuntimeError(f"Reviewed candidate symbols contain duplicates: {symbols}")

    shortlist = pd.read_csv(shortlist_path, dtype={"symbol": str})
    shortlist["symbol"] = shortlist["symbol"].astype(str)
    by_symbol = shortlist.set_index("symbol", drop=False)
    missing = [symbol for symbol in symbols if symbol not in by_symbol.index]
    if missing:
        raise RuntimeError(f"Reviewed candidate symbols not found in candidate_shortlist.csv: {missing}")
    in_base = [
        symbol
        for symbol in symbols
        if csv_bool(by_symbol.loc[symbol].get("in_base_pool", False))
    ]
    if in_base:
        raise RuntimeError(f"Reviewed candidate symbols already exist in the original sector-rotation pool: {in_base}")
    base_name_duplicates = [
        symbol
        for symbol in symbols
        if "base_name_duplicate" in by_symbol.columns and csv_bool(by_symbol.loc[symbol].get("base_name_duplicate", False))
    ]
    if base_name_duplicates:
        raise RuntimeError(
            "Reviewed candidate symbols duplicate original sector-rotation pool ETF Chinese names: "
            f"{base_name_duplicates}"
        )

    selected = by_symbol.loc[symbols].reset_index(drop=True)
    selected_records = selected.to_dict(orient="records")
    original_symbols: list[str] = []
    if original_selected_path.exists():
        original = pd.read_csv(original_selected_path, dtype={"symbol": str})
        original_symbols = original["symbol"].astype(str).tolist()

    additions = selected[["symbol", "name", "fund_size"]].copy()
    expanded = build_expanded_universe(load_base_pool(), additions)
    payload = {
        "date": args.date,
        "candidate_symbols": symbols,
        "candidate_arg": ",".join(symbols),
        "manual_override": symbols != original_symbols,
        "selected": selected_records,
        "shortlist_csv": str(automation_dir / "candidate_shortlist.csv"),
        "selected_csv": str(automation_dir / "candidate_selected.csv"),
        "expanded_refresh_universe_csv": str(automation_dir / "expanded_refresh_universe.csv"),
    }
    return {
        "payload": payload,
        "selected": selected,
        "shortlist": shortlist,
        "expanded": expanded,
    }


def write_reviewed_candidate_outputs(candidate_state: dict[str, Any], automation_dir: Path) -> dict[str, Any]:
    candidate_state["shortlist"].to_csv(automation_dir / "candidate_shortlist.csv", index=False)
    candidate_state["selected"].to_csv(automation_dir / "candidate_selected.csv", index=False)
    candidate_state["expanded"].to_csv(automation_dir / "expanded_refresh_universe.csv", index=False)
    payload = candidate_state["payload"]
    (automation_dir / "candidate_selected.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_step(f"reviewed candidates: {payload['candidate_arg']}")
    return payload


def prepare_recommend_workspace(data_root: Path, automation_dir: Path) -> Path:
    start = time.monotonic()
    log_step("START prepare dry-run recommendation workspace")
    workspace = Path(tempfile.mkdtemp(prefix="etf_pool_recommend_"))
    for source in data_root.glob("data/etf_daily.*"):
        if source.exists():
            destination = workspace / "data" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    elapsed = time.monotonic() - start
    log_step(f"END prepare dry-run recommendation workspace ({elapsed:.1f}s)")
    return workspace


def copy_recommend_outputs(rec_root: Path, automation_dir: Path, recommendation_date: str) -> Path:
    source = rec_root / "outputs" / "recommendations" / f"{recommendation_date}_sector-rotation.csv"
    destination = automation_dir / f"recommendation_{recommendation_date}_sector-rotation.csv"
    if source.exists():
        shutil.copy2(source, destination)
    return destination


def best_int(best: dict[str, Any], key: str) -> str:
    return str(int(float(best[key])))


def best_float(best: dict[str, Any], key: str) -> str:
    return str(float(best[key]))


def generate_recommendations(args: argparse.Namespace, automation_dir: Path, recommendation_date: str) -> Path:
    best = json.loads((automation_dir / "best.json").read_text(encoding="utf-8"))
    rec_root = prepare_recommend_workspace(Path(args.data_root), automation_dir)

    try:
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
                strategy="ranked-threshold-corr",
                universe=str(automation_dir / "selected_universe.csv"),
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
        recommendation_path = copy_recommend_outputs(rec_root, automation_dir, recommendation_date)
        recommendations = pd.read_csv(recommendation_path) if recommendation_path.exists() else pd.DataFrame()
        if recommendations.empty:
            raise RuntimeError(f"Recommendation output is empty for {recommendation_date}; refusing to report success")
        return recommendation_path
    finally:
        shutil.rmtree(rec_root, ignore_errors=True)


def run_pool_optimization(args: argparse.Namespace, candidates: str, run_id: str) -> None:
    start = time.monotonic()
    log_step("START pool optimization grid")
    paths = ProjectPaths(Path(args.data_root))
    daily = read_daily(paths)
    daily["symbol"] = daily["symbol"].astype(str)
    candidate_symbols = [part.strip() for part in candidates.split(",") if part.strip()]
    if len(candidate_symbols) != 3:
        raise ValueError("Exactly three candidate ETF symbols are required")

    available_symbols = set(daily["symbol"].astype(str))
    missing = [symbol for symbol in candidate_symbols if symbol not in available_symbols]
    if missing:
        raise ValueError(f"Candidate symbols are missing from local daily data: {missing}")

    base_universe = load_base_pool()
    base_universe["symbol"] = base_universe["symbol"].astype(str)
    names = latest_symbol_names(daily)
    sharpe_windows = parse_sharpe_windows(args.sharpe_window)
    factors = compute_factors(daily, sharpe_windows=sharpe_windows)
    start_date = pd.Timestamp(args.start)
    end_date = pd.Timestamp(args.date)
    run_dir_path = run_dir(Path(args.root), run_id)

    all_results = []
    evaluations = []
    candidate_specs: list[tuple[str, str | None]] = [("base", None)]
    candidate_specs.extend((f"add_{symbol}", symbol) for symbol in candidate_symbols)

    for pool_label, candidate_symbol in candidate_specs:
        pool = candidate_pool_frame(base_universe, candidate_symbol, names)
        symbols = set(pool["symbol"].astype(str))
        pool_factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
        results = optimization_grid_results(
            strategy="ranked-threshold-corr",
            daily=daily,
            factors=pool_factors,
            symbols=symbols,
            start=start_date,
            end=end_date,
            top_ns=parse_int_list(args.top_n),
            fee_rates=parse_float_list(args.fee_rate),
            sharpe_windows=sharpe_windows,
            factor_lower_bounds=parse_float_list(args.factor_lower_bound),
            corr_windows=parse_int_list(args.corr_window),
            corr_thresholds=parse_float_list(args.corr_threshold),
            stop_loss_pcts=parse_float_list(args.stop_loss_pct),
            constraint=args.constraint,
        )
        if results.empty:
            raise ValueError(f"No optimization results generated for pool {pool_label}")
        results = sort_optimization_results(results, args.objective, args.constraint)
        results.insert(0, "pool_label", pool_label)
        results.insert(1, "added_symbol", candidate_symbol)
        all_results.append(results)

        best = results.iloc[0].to_dict()
        best["pool_size"] = len(pool)
        evaluations.append(best)
        pool.to_csv(run_dir_path / f"{pool_label}_universe.csv", index=False)

    evaluation_frame = sort_optimization_results(pd.DataFrame(evaluations), args.objective, args.constraint)
    details = pd.concat(all_results, ignore_index=True)
    details.to_csv(run_dir_path / "all_results.csv", index=False)
    evaluation_frame.to_csv(run_dir_path / "evaluations.csv", index=False)

    best = evaluation_frame.iloc[0].to_dict()
    selected_candidate = best.get("added_symbol")
    selected_candidate = None if pd.isna(selected_candidate) else str(selected_candidate)
    selected_universe = candidate_pool_frame(base_universe, selected_candidate, names)
    selected_universe.to_csv(run_dir_path / "selected_universe.csv", index=False)
    (run_dir_path / "best.json").write_text(json.dumps(best, indent=2), encoding="utf-8")

    if args.apply:
        current_path = write_table(base_universe, run_dir_path / "universe_before")
        selected_path = DEFAULT_BASE_POOL
        selected_universe.to_csv(selected_path, index=False)
        print(f"backed up previous universe to {current_path}")
        print(f"updated universe to {selected_path}")

    log_step(f"END pool optimization grid ({time.monotonic() - start:.1f}s)")


def latest_symbol_names(daily: pd.DataFrame) -> dict[str, str]:
    if daily.empty or "name" not in daily.columns:
        return {}
    df = daily.copy()
    df["symbol"] = df["symbol"].astype(str)
    df = df.sort_values("date").dropna(subset=["name"]).drop_duplicates("symbol", keep="last")
    return df.set_index("symbol")["name"].astype(str).to_dict()


def candidate_pool_frame(base_universe: pd.DataFrame, candidate_symbol: str | None, names: dict[str, str]) -> pd.DataFrame:
    out = base_universe.copy()
    out["symbol"] = out["symbol"].astype(str)
    if candidate_symbol is None or candidate_symbol in set(out["symbol"]):
        return out.reset_index(drop=True)
    row = {
        "symbol": candidate_symbol,
        "name": names.get(candidate_symbol, candidate_symbol),
        "fund_size": pd.NA,
    }
    return pd.concat([out, pd.DataFrame([row])], ignore_index=True)


def strict_recent_universe_backfill(
    args: argparse.Namespace,
    universe_csv: Path,
    *,
    label: str,
    lookback_days: int = 10,
) -> pd.DataFrame:
    start_time = time.monotonic()
    log_step(f"START {label} Tencent recent backfill")
    paths = ProjectPaths(Path(args.data_root))
    universe = pd.read_csv(universe_csv)
    universe["symbol"] = universe["symbol"].astype(str)
    end = datetime.strptime(args.date, "%Y-%m-%d").date()
    start = max(datetime.strptime(args.start, "%Y-%m-%d").date(), end - timedelta(days=lookback_days))
    provider = AkshareETFProvider(adjust="qfq")
    incoming, target_trade_date = fetch_tencent_daily_if_stale(provider, universe, start, end, paths=paths, log=log_step)
    if incoming.empty:
        verified = read_daily(paths)
    else:
        verified = merge_and_store_daily(paths, incoming, label)
    verified["symbol"] = verified["symbol"].astype(str)
    log_step(
        f"END {label} Tencent recent backfill ({time.monotonic() - start_time:.1f}s): "
        f"target trade date={target_trade_date}; incoming rows={len(incoming)}; verified rows={len(verified)}"
    )
    return verified


def resolve_recommendation_date(args: argparse.Namespace, daily: pd.DataFrame, universe_csv: Path) -> str:
    universe = pd.read_csv(universe_csv)
    recommendation_date = resolve_complete_universe_date(daily, universe, args.date)
    if recommendation_date != args.date:
        log_step(f"recommendation date adjusted from {args.date} to latest complete date {recommendation_date}")
    return recommendation_date


def unique_values(frame: pd.DataFrame, column: str) -> list[Any]:
    if column not in frame.columns:
        return []
    values = frame[column].dropna().drop_duplicates().tolist()
    return sorted(values)


def grid_values_by_pool(results: pd.DataFrame) -> list[dict[str, Any]]:
    columns = [
        "top_n",
        "sharpe_window",
        "factor_lower_bound",
        "corr_window",
        "corr_threshold",
        "stop_loss_pct",
    ]
    rows: list[dict[str, Any]] = []
    for pool_label, group in results.groupby("pool_label", dropna=False):
        row = {"pool_label": pool_label}
        row.update({column: unique_values(group, column) for column in columns})
        rows.append(row)
    return rows


def cleanup_intermediate_outputs(automation_dir: Path) -> None:
    keep_names = {"automation_summary.json"}
    for path in automation_dir.iterdir():
        if path.name in keep_names or path.name.startswith("recommendation_") or path.name.startswith("universe_before"):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


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
    all_results = pd.read_csv(automation_dir / "all_results.csv")
    recommendations = pd.read_csv(recommendation_path) if recommendation_path.exists() else pd.DataFrame()
    payload = {
        "date": args.date,
        "recommendation_date": recommendation_date,
        "objective": args.objective,
        "constraint": args.constraint,
        "apply": args.apply,
        "automation_dir": str(automation_dir),
        "data_root": args.data_root,
        "recommendation_path": str(recommendation_path),
        "candidates": candidate_payload["selected"],
        "candidate_arg": candidate_payload["candidate_arg"],
        "manual_override": candidate_payload["manual_override"],
        "grid_values_by_pool": grid_values_by_pool(all_results),
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
    require_reviewed_candidates(args)
    if args.start is None:
        args.start = default_start(args.date)
    root = Path(args.root)
    run_id = args.run_id or DEFAULT_RUN_ID
    automation_dir = run_dir(root, run_id)
    candidate_state = prepare_reviewed_candidates(args, automation_dir)
    log_step(f"reset run directory: {automation_dir}")
    reset_run_dir(automation_dir)
    candidate_payload = write_reviewed_candidate_outputs(candidate_state, automation_dir)

    candidates = candidate_payload["candidate_arg"]
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
    cleanup_intermediate_outputs(automation_dir)
    log_step(f"wrote automation summary to {automation_dir / 'automation_summary.json'}")
    log_step(f"full run complete ({time.monotonic() - whole_start:.1f}s)")


if __name__ == "__main__":
    main()
