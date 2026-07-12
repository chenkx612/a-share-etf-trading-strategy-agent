from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent

from quant_core.backtest.engine import run_backtest  # noqa: E402
from quant_core.cli import (  # noqa: E402
    build_strategy_config,
    command_factor_compute,
    command_recommend_today,
    optimization_grid_results,
    parse_float_list,
    parse_int_list,
    parse_sharpe_windows,
    read_daily,
    sort_optimization_results,
)
from quant_core.config import STRATEGY_NAME  # noqa: E402
from quant_core.data.market_data import (  # noqa: E402
    ProjectPaths,
    expanded_universe as build_expanded_universe,
    load_universe,
    parse_symbol_list,
    resolve_complete_universe_date,
    write_table,
)
from quant_core.factors import compute_factors  # noqa: E402
from quant_core.strategy.sharpe_corr_threshold import select_sharpe_corr_threshold  # noqa: E402


DEFAULT_RUN_ID = "current"
DEFAULT_TOP_N = "4,5,6"
DEFAULT_FEE_RATE = "0.0003"
DEFAULT_SHARPE_WINDOW = "20,25,30"
DEFAULT_FACTOR_LOWER_BOUND = "0.0,0.5,1.0"
DEFAULT_CORR_WINDOW = "100"
DEFAULT_CORR_THRESHOLD = "0.9"
DEFAULT_STOP_LOSS_PCT = "0.1"
DEFAULT_OBJECTIVE = "sortino"
DEFAULT_CONSTRAINT = "drawdown-lt-return"
DEFAULT_ROOT = ".agents/skills/etf-sharpe-topk/outputs"
DEFAULT_DATA_ROOT = "."
DEFAULT_UNIVERSE_NAME = "sector-rotation"
DEFAULT_BASE_POOL = SCRIPT_DIR.parent / "references" / "sector_rotation_universe.csv"


def add_common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    parser.add_argument("--start", help="Backtest start date. Defaults to three years before --date.")
    parser.add_argument("--run-id")


def add_optimization_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--top-n", default=DEFAULT_TOP_N)
    parser.add_argument("--fee-rate", default=DEFAULT_FEE_RATE)
    parser.add_argument("--sharpe-window", default=DEFAULT_SHARPE_WINDOW)
    parser.add_argument("--factor-lower-bound", default=DEFAULT_FACTOR_LOWER_BOUND)
    parser.add_argument("--corr-window", default=DEFAULT_CORR_WINDOW)
    parser.add_argument("--corr-threshold", default=DEFAULT_CORR_THRESHOLD)
    parser.add_argument("--stop-loss-pct", default=DEFAULT_STOP_LOSS_PCT)
    parser.add_argument("--objective", default=DEFAULT_OBJECTIVE)
    parser.add_argument("--constraint", default=DEFAULT_CONSTRAINT)


def fill_default_start(args: argparse.Namespace) -> None:
    if args.start is None:
        args.start = default_start(args.date)


def require_reviewed_candidates(args: argparse.Namespace) -> None:
    if getattr(args, "candidates", None):
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


def automation_dir_from_args(args: argparse.Namespace) -> Path:
    return run_dir(Path(args.root), args.run_id or DEFAULT_RUN_ID)


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
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_ROOT), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    try:
        subprocess.run(args, cwd=REPO_ROOT, env=env, check=True)
    finally:
        elapsed = time.monotonic() - start
        log_step(f"END {label} ({elapsed:.1f}s)")


def parse_candidates(value: str) -> list[str]:
    return parse_symbol_list(value)


def load_base_pool() -> pd.DataFrame:
    return load_universe(DEFAULT_BASE_POOL)


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
            f"Missing {shortlist_path}; run select_etf_candidates.py before prepare_etf_pool_run.py."
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


def load_candidate_arg(automation_dir: Path) -> str:
    path = automation_dir / "candidate_selected.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; run prepare_etf_pool_run.py first.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload["candidate_arg"])


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


def dataframe_records_without_nulls(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records = frame.where(pd.notna(frame), None).to_dict(orient="records")
    return [
        {key: value for key, value in record.items() if value is not None}
        for record in records
    ]


def split_recommendation_output(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    if frame.empty or "record_type" not in frame.columns:
        return frame, []

    recommendation_rows = frame[frame["record_type"].eq("recommendation")].copy()
    filter_rows = frame[frame["record_type"].eq("filtered")].copy()
    recommendation_columns = [
        column
        for column in ["date", "symbol", "name", "score", "target_weight"]
        if column in recommendation_rows.columns
    ]
    recommendation_rows = recommendation_rows[recommendation_columns]
    if "record_type" in filter_rows.columns:
        filter_rows = filter_rows.drop(columns=["record_type"])
    return recommendation_rows, dataframe_records_without_nulls(filter_rows)


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
                sharpe_window=str(int(float(best["sharpe_window"]))),
            )
        )
        log_step(f"END factor compute for recommendation ({time.monotonic() - start:.1f}s)")

        start = time.monotonic()
        log_step("START generate recommendation")
        command_recommend_today(
            argparse.Namespace(
                root=str(rec_root),
                date=recommendation_date,
                strategy="sharpe-corr-threshold",
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
    run_dir_path.mkdir(parents=True, exist_ok=True)

    all_results = []
    evaluations = []
    candidate_specs: list[tuple[str, str | None]] = [("base", None)]
    candidate_specs.extend((f"add_{symbol}", symbol) for symbol in candidate_symbols)

    for pool_label, candidate_symbol in candidate_specs:
        pool = candidate_pool_frame(base_universe, candidate_symbol, names)
        results = optimize_pool(args, daily, factors, pool, start_date, end_date, sharpe_windows)
        results.insert(0, "pool_label", pool_label)
        results.insert(1, "added_symbol", candidate_symbol)
        all_results.append(results)

        best = results.iloc[0].to_dict()
        best["pool_size"] = len(pool)
        evaluations.append(best)
        pool.to_csv(run_dir_path / f"{pool_label}_universe.csv", index=False)

    evaluation_frame = sort_optimization_results(pd.DataFrame(evaluations), args.objective, args.constraint)
    best = evaluation_frame.iloc[0].to_dict()
    selected_candidate = best.get("added_symbol")
    selected_candidate = None if pd.isna(selected_candidate) else str(selected_candidate)
    selected_universe = candidate_pool_frame(base_universe, selected_candidate, names)
    pruning_challenge = evaluate_pruned_pool_challenge(
        args=args,
        daily=daily,
        factors=factors,
        selected_universe=selected_universe,
        current_best=best,
        names=names,
        start_date=start_date,
        end_date=end_date,
        sharpe_windows=sharpe_windows,
    )
    if pruning_challenge["evaluated"]:
        pruned_results = pruning_challenge["results"]
        if not pruned_results.empty:
            all_results.append(pruned_results)
            evaluations.append(pruning_challenge["best"])
            pruned_results.to_csv(run_dir_path / f"{pruning_challenge['pool_label']}_results.csv", index=False)
        if pruning_challenge["accepted"]:
            best = pruning_challenge["best"]
            selected_universe = pruning_challenge["universe"]
            log_step(
                "accepted pruned pool: removed "
                f"{pruning_challenge['removed_symbol']} because sortino improved "
                f"from {pruning_challenge['base_sortino']:.6f} to {pruning_challenge['pruned_sortino']:.6f}"
            )
        else:
            log_step(
                "kept original best pool: removing "
                f"{pruning_challenge['removed_symbol']} did not improve sortino "
                f"({pruning_challenge['pruned_sortino']:.6f} <= {pruning_challenge['base_sortino']:.6f})"
            )

    evaluation_frame = sort_optimization_results(pd.DataFrame(evaluations), args.objective, args.constraint)
    details = pd.concat(all_results, ignore_index=True)
    details.to_csv(run_dir_path / "all_results.csv", index=False)
    evaluation_frame.to_csv(run_dir_path / "evaluations.csv", index=False)
    selected_universe.to_csv(run_dir_path / "selected_universe.csv", index=False)
    (run_dir_path / "best.json").write_text(json.dumps(best, indent=2), encoding="utf-8")
    (run_dir_path / "pruning_challenge.json").write_text(
        json.dumps(pruning_challenge_summary(pruning_challenge), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.apply:
        current_path = write_table(base_universe, run_dir_path / "universe_before")
        selected_path = DEFAULT_BASE_POOL
        selected_universe.to_csv(selected_path, index=False)
        print(f"backed up previous universe to {current_path}")
        print(f"updated universe to {selected_path}")
    (run_dir_path / "apply_status.json").write_text(
        json.dumps({"apply": bool(args.apply)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log_step(f"END pool optimization grid ({time.monotonic() - start:.1f}s)")


def optimize_pool(
    args: argparse.Namespace,
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    pool: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    sharpe_windows: list[int],
) -> pd.DataFrame:
    symbols = set(pool["symbol"].astype(str))
    pool_factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    results = optimization_grid_results(
        strategy="sharpe-corr-threshold",
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
        raise ValueError("No optimization results generated for pool")
    return sort_optimization_results(results, args.objective, args.constraint)


def best_strategy_selection(
    best: dict[str, Any],
    factors: pd.DataFrame,
    symbols: set[str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    config = build_strategy_config(
        argparse.Namespace(
            strategy=STRATEGY_NAME,
            top_n=int(float(best["top_n"])),
            fee_rate=float(best["fee_rate"]),
            sharpe_window=int(float(best["sharpe_window"])),
        )
    )
    pool_factors = factors[factors["symbol"].astype(str).isin(symbols)].copy()
    return select_sharpe_corr_threshold(
        pool_factors,
        config,
        start=start_date,
        end=end_date,
        universe_symbols=symbols,
        factor_lower_bound=float(best["factor_lower_bound"]),
        corr_window=int(float(best["corr_window"])),
        corr_threshold=float(best["corr_threshold"]),
        stop_loss_pct=float(best["stop_loss_pct"]),
    )


def symbol_return_contributions(
    daily: pd.DataFrame,
    selected: pd.DataFrame,
    symbols: set[str],
    fee_rate: float,
) -> pd.DataFrame:
    result = run_backtest(daily, selected, fee_rate=fee_rate)
    symbol_list = sorted(str(symbol) for symbol in symbols)
    if result.positions.empty:
        return pd.DataFrame({"symbol": symbol_list, "contribution": [0.0] * len(symbol_list)})

    prices = daily.copy()
    prices["symbol"] = prices["symbol"].astype(str)
    prices = prices[prices["symbol"].isin(symbol_list)]
    open_prices = prices.pivot(index="date", columns="symbol", values="open").sort_index().ffill()
    forward_returns = open_prices.shift(-1) / open_prices - 1.0
    contribution_lookup = forward_returns.stack().rename("forward_return").reset_index()

    positions = result.positions.copy()
    positions["symbol"] = positions["symbol"].astype(str)
    positions["date"] = pd.to_datetime(positions["date"])
    contribution_lookup["date"] = pd.to_datetime(contribution_lookup["date"])
    merged = positions.merge(contribution_lookup, on=["date", "symbol"], how="left")
    merged["contribution"] = merged["weight"].astype(float) * merged["forward_return"].fillna(0.0).astype(float)
    contributions = merged.groupby("symbol", as_index=False)["contribution"].sum()
    contributions = pd.DataFrame({"symbol": symbol_list}).merge(contributions, on="symbol", how="left")
    contributions["contribution"] = contributions["contribution"].fillna(0.0)
    return contributions.sort_values(["contribution", "symbol"], ascending=[True, True]).reset_index(drop=True)


def evaluate_pruned_pool_challenge(
    *,
    args: argparse.Namespace,
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    selected_universe: pd.DataFrame,
    current_best: dict[str, Any],
    names: dict[str, str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    sharpe_windows: list[int],
) -> dict[str, Any]:
    selected_universe = selected_universe.copy()
    selected_universe["symbol"] = selected_universe["symbol"].astype(str)
    symbols = set(selected_universe["symbol"])
    if len(symbols) <= 1:
        return {"evaluated": False, "reason": "selected pool has one or fewer ETF"}

    selected = best_strategy_selection(current_best, factors, symbols, start_date, end_date)
    contributions = symbol_return_contributions(
        daily,
        selected,
        symbols,
        fee_rate=float(current_best["fee_rate"]),
    )
    if contributions.empty:
        return {"evaluated": False, "reason": "no contribution rows generated"}

    worst = contributions.iloc[0].to_dict()
    removed_symbol = str(worst["symbol"])
    pruned_universe = selected_universe[selected_universe["symbol"] != removed_symbol].reset_index(drop=True)
    if pruned_universe.empty:
        return {"evaluated": False, "reason": "pruned pool would be empty"}

    pool_label = f"remove_{removed_symbol}"
    pruned_results = optimize_pool(args, daily, factors, pruned_universe, start_date, end_date, sharpe_windows)
    pruned_results.insert(0, "pool_label", pool_label)
    pruned_results.insert(1, "added_symbol", current_best.get("added_symbol"))
    pruned_results.insert(2, "removed_symbol", removed_symbol)
    pruned_results.insert(3, "removed_name", names.get(removed_symbol, removed_symbol))
    pruned_best = pruned_results.iloc[0].to_dict()
    pruned_best["pool_size"] = len(pruned_universe)
    pruned_best["pruning_accepted"] = False

    base_sortino = float(current_best.get("sortino", float("nan")))
    pruned_sortino = float(pruned_best.get("sortino", float("nan")))
    accepted = pd.notna(pruned_sortino) and pd.notna(base_sortino) and pruned_sortino > base_sortino
    pruned_best["pruning_accepted"] = bool(accepted)
    pruned_best["base_pool_label"] = current_best.get("pool_label")
    pruned_best["base_sortino"] = base_sortino

    return {
        "evaluated": True,
        "accepted": bool(accepted),
        "pool_label": pool_label,
        "removed_symbol": removed_symbol,
        "removed_name": names.get(removed_symbol, removed_symbol),
        "removed_contribution": float(worst["contribution"]),
        "base_pool_label": current_best.get("pool_label"),
        "base_sortino": base_sortino,
        "pruned_sortino": pruned_sortino,
        "contributions": dataframe_records_without_nulls(contributions),
        "results": pruned_results,
        "best": pruned_best,
        "universe": pruned_universe,
    }


def pruning_challenge_summary(challenge: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in challenge.items()
        if key not in {"results", "universe"}
    }


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
    apply_status_path = automation_dir / "apply_status.json"
    apply_status = (
        json.loads(apply_status_path.read_text(encoding="utf-8"))
        if apply_status_path.exists()
        else {"apply": bool(args.apply)}
    )
    pruning_challenge_path = automation_dir / "pruning_challenge.json"
    pruning_challenge = (
        json.loads(pruning_challenge_path.read_text(encoding="utf-8"))
        if pruning_challenge_path.exists()
        else {"evaluated": False, "reason": "missing pruning challenge output"}
    )
    evaluations = pd.read_csv(automation_dir / "evaluations.csv")
    all_results = pd.read_csv(automation_dir / "all_results.csv")
    recommendation_output = (
        pd.read_csv(
            recommendation_path,
            dtype={"symbol": str, "selected_symbol": str},
        )
        if recommendation_path.exists()
        else pd.DataFrame()
    )
    recommendations, recommendation_filters = split_recommendation_output(recommendation_output)
    payload = {
        "date": args.date,
        "recommendation_date": recommendation_date,
        "objective": args.objective,
        "constraint": args.constraint,
        "apply": bool(apply_status.get("apply", False)),
        "automation_dir": str(automation_dir),
        "data_root": args.data_root,
        "recommendation_path": str(recommendation_path),
        "candidates": candidate_payload["selected"],
        "candidate_arg": candidate_payload["candidate_arg"],
        "manual_override": candidate_payload["manual_override"],
        "grid_values_by_pool": grid_values_by_pool(all_results),
        "best": best,
        "pruning_challenge": pruning_challenge,
        "evaluations": evaluations.to_dict(orient="records"),
        "recommendations": recommendations.to_dict(orient="records"),
        "recommendation_filters": recommendation_filters,
    }
    (automation_dir / "automation_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_step("END write automation summary")
