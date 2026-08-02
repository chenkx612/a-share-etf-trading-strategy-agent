from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[3]

from quant_core.universe.common import (
    AkshareMarketDataClient,
    FINAL_COLUMNS,
    ProjectPaths,
    apply_universe,
    fetch_spot,
    five_years_before,
    json_number,
    latest_trade_date_on_or_before,
    missing_symbols_for_date,
    normalize_spot_frame,
    normalized_etf_group_key,
    read_daily,
    replace_symbol_history,
    validate_daily,
    write_table,
)


DEFAULT_MIN_FUND_SIZE = 10_000_000_000
# Bound the market-data refresh workload without excluding the normal set of
# large, name-deduplicated ETF themes from correlation selection.
DEFAULT_SHORTLIST_SIZE = 100
DEFAULT_LOOKBACK_DAYS = 252
DEFAULT_MIN_OBSERVATIONS = 120
DEFAULT_CORR_THRESHOLD = 0.90
DEFAULT_OUTPUT_DIR = REPO_ROOT / "outputs" / "liquid_etf_universe"
DEFAULT_DESTINATION = REPO_ROOT / "universes" / "liquid_etf_rotation.csv"
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a low-cost ETF rotation universe using current fund size, "
            "name grouping, and one-year return correlations."
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="As-of date, YYYY-MM-DD.")
    parser.add_argument("--min-fund-size", type=float, default=DEFAULT_MIN_FUND_SIZE)
    parser.add_argument("--shortlist-size", type=int, default=DEFAULT_SHORTLIST_SIZE)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--min-observations", type=int, default=DEFAULT_MIN_OBSERVATIONS)
    parser.add_argument("--corr-threshold", type=float, default=DEFAULT_CORR_THRESHOLD)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    datetime.strptime(args.date, "%Y-%m-%d")
    if args.min_fund_size < 0:
        raise ValueError("--min-fund-size must be non-negative")
    if args.shortlist_size <= 0:
        raise ValueError("--shortlist-size must be positive")
    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be positive")
    if args.min_observations <= 0 or args.min_observations > args.lookback_days:
        raise ValueError("--min-observations must be in [1, --lookback-days]")
    if not -1.0 <= args.corr_threshold <= 1.0:
        raise ValueError("--corr-threshold must be between -1 and 1")


def make_shortlist(
    spot: pd.DataFrame,
    *,
    min_fund_size: float,
    shortlist_size: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = normalize_spot_frame(spot)
    size_excluded = frame[frame["fund_size"].isna() | frame["fund_size"].lt(min_fund_size)].copy()
    eligible = frame[frame["fund_size"].ge(min_fund_size)].copy()
    eligible = eligible.sort_values(
        ["fund_size", "symbol"],
        ascending=[False, True],
        kind="stable",
    )

    kept = eligible.drop_duplicates("group_key", keep="first").copy()
    winners = kept.set_index("group_key")["symbol"].to_dict()
    name_excluded = eligible[eligible.duplicated("group_key", keep="first")].copy()
    shortlist = kept.head(shortlist_size).reset_index(drop=True)
    shortlist.insert(0, "size_rank", range(1, len(shortlist) + 1))

    audit = {
        "spot_product_count": int(len(frame)),
        "size_filter": {
            "minimum_fund_size": float(min_fund_size),
            "eligible_count": int(len(eligible)),
            "excluded_count": int(len(size_excluded)),
            "excluded": [
                {
                    "symbol": str(row.symbol),
                    "name": str(row.name),
                    "fund_size": json_number(row.fund_size),
                    "reason": "missing_fund_size"
                    if pd.isna(row.fund_size)
                    else "fund_size_below_minimum",
                }
                for row in size_excluded.itertuples(index=False)
            ],
        },
        "name_grouping": {
            "group_count": int(len(kept)),
            "excluded_count": int(len(name_excluded)),
            "excluded": [
                {
                    "symbol": str(row.symbol),
                    "name": str(row.name),
                    "fund_size": json_number(row.fund_size),
                    "group_key": str(row.group_key),
                    "kept_symbol": str(winners[row.group_key]),
                    "reason": "smaller_or_tie_broken_name_group_duplicate",
                }
                for row in name_excluded.itertuples(index=False)
            ],
        },
        "shortlist": {
            "limit": int(shortlist_size),
            "count": int(len(shortlist)),
            "ordering": "fund_size_desc_symbol_asc",
        },
    }
    return shortlist, audit


def refresh_shared_daily(
    shortlist: pd.DataFrame,
    *,
    trade_date: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    root: Path = REPO_ROOT,
    log: Callable[[str], None] = print,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    paths = ProjectPaths(root)
    try:
        existing = read_daily(paths)
    except FileNotFoundError:
        existing = pd.DataFrame()

    end = datetime.strptime(trade_date, "%Y-%m-%d").date()
    target = latest_trade_date_on_or_before(end)
    universe = shortlist[FINAL_COLUMNS].copy()
    latest_date_stale_symbols = missing_symbols_for_date(existing, universe, target)
    _, cached_observations = return_matrix(
        existing,
        shortlist,
        trade_date=trade_date,
        lookback_days=lookback_days,
    )
    insufficient_cache_symbols = sorted(
        symbol
        for symbol in universe["symbol"].astype(str)
        if cached_observations.get(symbol, 0) < min_observations
    )
    stale_symbols = sorted(
        set(latest_date_stale_symbols) | set(insufficient_cache_symbols)
    )
    refresh_audit: dict[str, Any] = {
        "cache": str(paths.data_daily),
        "adjust": "qfq",
        "target_trade_date": target.isoformat(),
        "requested_symbols": [str(symbol) for symbol in universe["symbol"]],
        "stale_symbols": stale_symbols,
        "latest_date_stale_symbols": latest_date_stale_symbols,
        "insufficient_cache_symbols": insufficient_cache_symbols,
        "cached_observations_by_symbol": cached_observations,
        "minimum_observations": int(min_observations),
        "lookback_days": int(lookback_days),
        "refreshed_symbols": [],
        "refresh_failures": [],
    }
    if not stale_symbols:
        return existing, refresh_audit

    stale_universe = universe[universe["symbol"].astype(str).isin(stale_symbols)].copy()
    log(f"refresh shared qfq cache for {len(stale_universe)} shortlisted ETFs")
    incoming = AkshareMarketDataClient(adjust="qfq").fetch_daily(
        stale_universe,
        five_years_before(end),
        end,
    )
    if incoming.empty:
        refresh_audit["refresh_failures"] = stale_symbols
        return existing, refresh_audit

    incoming["symbol"] = incoming["symbol"].astype(str)
    incoming["date"] = pd.to_datetime(incoming["date"])
    complete_symbols = set(
        incoming.loc[incoming["date"].dt.date.eq(target), "symbol"].astype(str)
    )
    refresh_audit["refreshed_symbols"] = sorted(complete_symbols)
    refresh_audit["refresh_failures"] = sorted(set(stale_symbols) - complete_symbols)
    complete_incoming = incoming[incoming["symbol"].isin(complete_symbols)].copy()
    if complete_incoming.empty:
        return existing, refresh_audit

    merged = replace_symbol_history(existing, complete_incoming)
    problems = validate_daily(merged)
    if problems:
        raise RuntimeError(f"Shared qfq cache validation failed: {problems}")
    paths.ensure_data()
    write_table(merged, paths.data_daily)
    return read_daily(paths), refresh_audit


def return_matrix(
    daily: pd.DataFrame,
    shortlist: pd.DataFrame,
    *,
    trade_date: str,
    lookback_days: int,
) -> tuple[pd.DataFrame, dict[str, int]]:
    if daily.empty:
        return pd.DataFrame(), {str(symbol): 0 for symbol in shortlist["symbol"]}
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    symbols = set(shortlist["symbol"].astype(str))
    frame = frame[
        frame["symbol"].isin(symbols)
        & frame["date"].notna()
        & frame["date"].le(pd.Timestamp(trade_date))
        & frame["close"].gt(0)
    ].copy()

    series_by_symbol: dict[str, pd.Series] = {}
    observations: dict[str, int] = {}
    for symbol in shortlist["symbol"].astype(str):
        prices = (
            frame[frame["symbol"].eq(symbol)]
            .drop_duplicates("date", keep="last")
            .sort_values("date")
            .set_index("date")["close"]
            .tail(lookback_days + 1)
        )
        returns = prices.pct_change(fill_method=None).dropna().tail(lookback_days)
        series_by_symbol[symbol] = returns
        observations[symbol] = int(len(returns))
    return pd.DataFrame(series_by_symbol), observations


def correlation_select(
    shortlist: pd.DataFrame,
    returns: pd.DataFrame,
    observations: dict[str, int],
    *,
    min_observations: int,
    corr_threshold: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    eligible_symbols: list[str] = []
    history_excluded: list[dict[str, Any]] = []
    by_symbol = shortlist.set_index("symbol", drop=False)
    for symbol in shortlist["symbol"].astype(str):
        count = int(observations.get(symbol, 0))
        if count < min_observations:
            row = by_symbol.loc[symbol]
            history_excluded.append(
                {
                    "symbol": symbol,
                    "name": str(row["name"]),
                    "observations": count,
                    "minimum_observations": int(min_observations),
                    "reason": "insufficient_return_history",
                }
            )
        else:
            eligible_symbols.append(symbol)

    correlations = returns.reindex(columns=eligible_symbols).corr()
    selected_symbols: list[str] = []
    correlation_excluded: list[dict[str, Any]] = []
    for symbol in eligible_symbols:
        conflicts = [
            (kept_symbol, float(correlations.loc[symbol, kept_symbol]))
            for kept_symbol in selected_symbols
            if pd.notna(correlations.loc[symbol, kept_symbol])
            and float(correlations.loc[symbol, kept_symbol]) > corr_threshold
        ]
        if conflicts:
            kept_symbol, correlation = max(conflicts, key=lambda item: item[1])
            row = by_symbol.loc[symbol]
            correlation_excluded.append(
                {
                    "symbol": symbol,
                    "name": str(row["name"]),
                    "kept_symbol": kept_symbol,
                    "correlation": correlation,
                    "threshold": float(corr_threshold),
                    "reason": "correlation_above_threshold",
                }
            )
        else:
            selected_symbols.append(symbol)

    selected = by_symbol.loc[selected_symbols, FINAL_COLUMNS].reset_index(drop=True)
    correlation_output = correlations.copy()
    correlation_output.index.name = "symbol"
    correlation_output = correlation_output.reset_index()
    audit = {
        "history_filter": {
            "lookback_return_limit": int(returns.shape[0]),
            "minimum_observations": int(min_observations),
            "excluded_count": len(history_excluded),
            "excluded": history_excluded,
            "observations_by_symbol": observations,
        },
        "correlation_filter": {
            "threshold": float(corr_threshold),
            "comparison": "pearson_return_correlation_without_absolute_value",
            "excluded_count": len(correlation_excluded),
            "excluded": correlation_excluded,
        },
    }
    return selected, correlation_output, audit


def build_outputs(
    args: argparse.Namespace,
    *,
    spot: pd.DataFrame,
    daily: pd.DataFrame,
    refresh_audit: dict[str, Any] | None = None,
    destination: Path = DEFAULT_DESTINATION,
) -> dict[str, Any]:
    validate_args(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    shortlist, shortlist_audit = make_shortlist(
        spot,
        min_fund_size=args.min_fund_size,
        shortlist_size=args.shortlist_size,
    )
    returns, observations = return_matrix(
        daily,
        shortlist,
        trade_date=args.date,
        lookback_days=args.lookback_days,
    )
    selected, correlations, selection_audit = correlation_select(
        shortlist,
        returns,
        observations,
        min_observations=args.min_observations,
        corr_threshold=args.corr_threshold,
    )

    shortlist.to_csv(output_dir / "shortlist.csv", index=False)
    selected.to_csv(output_dir / "selected_universe.csv", index=False)
    correlations.to_csv(output_dir / "correlation.csv", index=False)
    refresh_failures = list((refresh_audit or {}).get("refresh_failures", []))
    apply_blockers: list[dict[str, Any]] = []
    if args.apply and refresh_failures:
        apply_blockers.append(
            {
                "reason": "market_data_refresh_failed",
                "symbols": refresh_failures,
            }
        )
    if args.apply and selected.empty:
        apply_blockers.append(
            {
                "reason": "selected_universe_empty",
                "symbols": [],
            }
        )
    applied = bool(args.apply and not apply_blockers)
    backup = apply_universe(
        selected,
        apply=applied,
        output_dir=output_dir,
        destination=destination,
    )
    summary = {
        "date": args.date,
        "survivorship_bias_allowed": True,
        "lof_included": False,
        "historical_return_ranking_used": False,
        "parameters": {
            "min_fund_size": float(args.min_fund_size),
            "shortlist_size": int(args.shortlist_size),
            "lookback_days": int(args.lookback_days),
            "min_observations": int(args.min_observations),
            "corr_threshold": float(args.corr_threshold),
        },
        **shortlist_audit,
        **selection_audit,
        "data_refresh": refresh_audit or {},
        "selected_count": int(len(selected)),
        "selected_symbols": selected["symbol"].astype(str).tolist(),
        "apply_requested": bool(args.apply),
        "apply": applied,
        "applied": applied,
        "apply_blockers": apply_blockers,
        "destination": str(destination),
        "backup": str(backup) if backup else None,
        "outputs": {
            "shortlist_csv": str(output_dir / "shortlist.csv"),
            "selected_universe_csv": str(output_dir / "selected_universe.csv"),
            "correlation_csv": str(output_dir / "correlation.csv"),
            "summary_json": str(output_dir / "summary.json"),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if apply_blockers:
        reasons = ", ".join(blocker["reason"] for blocker in apply_blockers)
        raise RuntimeError(
            "Refusing to apply liquid ETF universe because "
            f"{reasons}; preview and audit outputs were retained in {output_dir}"
        )
    return summary


def main() -> None:
    args = parse_args()
    validate_args(args)
    spot = fetch_spot()
    shortlist, _ = make_shortlist(
        spot,
        min_fund_size=args.min_fund_size,
        shortlist_size=args.shortlist_size,
    )
    daily, refresh_audit = refresh_shared_daily(
        shortlist,
        trade_date=args.date,
        lookback_days=args.lookback_days,
        min_observations=args.min_observations,
    )
    summary = build_outputs(
        args,
        spot=spot,
        daily=daily,
        refresh_audit=refresh_audit,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
