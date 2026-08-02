from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[3]

from quant_core.universe.common import (
    AkshareMarketDataClient,
    FINAL_COLUMNS,
    ProjectPaths,
    apply_universe,
    fetch_spot,
    five_years_before,
    latest_trade_date_on_or_before,
    missing_symbols_for_date,
    normalize_spot_frame,
    read_daily,
    replace_symbol_history,
    validate_daily,
    write_table,
)


DEFAULT_SHORTLIST_SIZE = 100
DEFAULT_MIN_FUND_SIZE = 1_000_000_000
DEFAULT_LIQUIDITY_LOOKBACK_DAYS = 60
DEFAULT_MIN_AMOUNT_OBSERVATIONS = 50
DEFAULT_MIN_MEDIAN_AMOUNT = 50_000_000
DEFAULT_LOOKBACK_DAYS = 252
DEFAULT_MIN_OBSERVATIONS = 120
DEFAULT_CORR_THRESHOLD = 0.90
DEFAULT_DESTINATION = REPO_ROOT / "universes" / "active_etf_rotation.csv"


DESCRIPTION = (
    "Build an active ETF rotation universe using fund-size preselection, "
    "trading-amount filters, and greedy pairwise correlation selection."
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", default=date.today().isoformat(), help="As-of date, YYYY-MM-DD.")
    parser.add_argument("--min-fund-size", type=float, default=DEFAULT_MIN_FUND_SIZE)
    parser.add_argument("--shortlist-size", type=int, default=DEFAULT_SHORTLIST_SIZE)
    parser.add_argument(
        "--liquidity-lookback-days",
        type=int,
        default=DEFAULT_LIQUIDITY_LOOKBACK_DAYS,
    )
    parser.add_argument(
        "--min-amount-observations",
        type=int,
        default=DEFAULT_MIN_AMOUNT_OBSERVATIONS,
    )
    parser.add_argument(
        "--min-median-amount",
        type=float,
        default=DEFAULT_MIN_MEDIAN_AMOUNT,
    )
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--min-observations", type=int, default=DEFAULT_MIN_OBSERVATIONS)
    parser.add_argument("--corr-threshold", type=float, default=DEFAULT_CORR_THRESHOLD)
    parser.add_argument("--output-dir")
    parser.add_argument("--apply", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    add_arguments(parser)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    datetime.strptime(args.date, "%Y-%m-%d")
    if args.min_fund_size < 0:
        raise ValueError("--min-fund-size must be non-negative")
    if args.shortlist_size <= 0:
        raise ValueError("--shortlist-size must be positive")
    if args.liquidity_lookback_days <= 0:
        raise ValueError("--liquidity-lookback-days must be positive")
    if not 0 < args.min_amount_observations <= args.liquidity_lookback_days:
        raise ValueError(
            "--min-amount-observations must be in [1, --liquidity-lookback-days]",
        )
    if args.min_median_amount < 0:
        raise ValueError("--min-median-amount must be non-negative")
    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be positive")
    if not 0 < args.min_observations <= args.lookback_days:
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
    missing_size = frame[frame["fund_size"].isna()].copy()
    below_minimum = frame[
        frame["fund_size"].notna() & frame["fund_size"].lt(min_fund_size)
    ].copy()
    ranked = (
        frame[frame["fund_size"].ge(min_fund_size)]
        .sort_values(["fund_size", "symbol"], ascending=[False, True], kind="stable")
        .reset_index(drop=True)
    )
    shortlist = ranked.head(shortlist_size).copy()
    shortlist.insert(0, "size_rank", range(1, len(shortlist) + 1))
    outside = ranked.iloc[shortlist_size:]
    audit = {
        "spot_product_count": int(len(frame)),
        "size_filter": {
            "minimum_fund_size": float(min_fund_size),
            "eligible_count": int(len(ranked)),
            "excluded_count": int(len(missing_size) + len(below_minimum)),
            "missing_size_count": int(len(missing_size)),
            "below_minimum_count": int(len(below_minimum)),
            "excluded": [
                {
                    "symbol": str(row.symbol),
                    "name": str(row.name),
                    "fund_size": None,
                    "reason": "missing_fund_size",
                }
                for row in missing_size.itertuples(index=False)
            ]
            + [
                {
                    "symbol": str(row.symbol),
                    "name": str(row.name),
                    "fund_size": float(row.fund_size),
                    "reason": "fund_size_below_minimum",
                }
                for row in below_minimum.itertuples(index=False)
            ],
        },
        "size_preselection": {
            "limit": int(shortlist_size),
            "eligible_size_count": int(len(ranked)),
            "selected_count": int(len(shortlist)),
            "outside_top_n_count": int(len(outside)),
            "ordering": "fund_size_desc_symbol_asc",
        },
    }
    return shortlist[["size_rank", *FINAL_COLUMNS]], audit


def market_metrics(
    daily: pd.DataFrame,
    shortlist: pd.DataFrame,
    *,
    trade_date: str,
    lookback_days: int,
    liquidity_lookback_days: int,
) -> tuple[pd.DataFrame, dict[str, int], pd.DataFrame]:
    symbols = shortlist["symbol"].astype(str).tolist()
    empty_liquidity = pd.DataFrame(
        {
            "symbol": symbols,
            "amount_observations": [0] * len(symbols),
            "amount_proxy_observations": [0] * len(symbols),
            "amount_median": [np.nan] * len(symbols),
        },
    )
    if daily.empty:
        return pd.DataFrame(columns=symbols), dict.fromkeys(symbols, 0), empty_liquidity

    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "amount" not in frame.columns:
        frame["amount"] = np.nan
    if "volume" not in frame.columns:
        frame["volume"] = np.nan
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame = frame[
        frame["symbol"].isin(set(symbols))
        & frame["date"].notna()
        & frame["date"].le(pd.Timestamp(trade_date))
    ].copy()

    series_by_symbol: dict[str, pd.Series] = {}
    return_observations: dict[str, int] = {}
    liquidity_rows: list[dict[str, object]] = []
    liquidity_dates = (
        frame["date"].drop_duplicates().sort_values().tail(liquidity_lookback_days)
    )
    for symbol in symbols:
        symbol_frame = (
            frame[frame["symbol"].eq(symbol)]
            .drop_duplicates("date", keep="last")
            .sort_values("date")
        )
        prices = (
            symbol_frame[symbol_frame["close"].gt(0)]
            .set_index("date")["close"]
            .tail(lookback_days + 1)
        )
        returns = prices.pct_change(fill_method=None).dropna().tail(lookback_days)
        series_by_symbol[symbol] = returns
        return_observations[symbol] = int(len(returns))

        recent = symbol_frame.set_index("date").reindex(liquidity_dates)
        reported_amount = recent["amount"].where(lambda values: values.gt(0))
        # Tencent's ETF history fallback exposes volume in lots but not turnover.
        # Use a clearly auditable close * volume * 100 proxy only where reported
        # turnover is unavailable; reported turnover always takes precedence.
        amount_proxy = (recent["close"] * recent["volume"] * 100).where(
            lambda values: values.gt(0),
        )
        proxy_used = reported_amount.isna() & amount_proxy.notna()
        recent_amount = reported_amount.fillna(amount_proxy).dropna()
        liquidity_rows.append(
            {
                "symbol": symbol,
                "amount_observations": int(len(recent_amount)),
                "amount_proxy_observations": int(proxy_used.sum()),
                "amount_median": (
                    float(recent_amount.median()) if not recent_amount.empty else np.nan
                ),
            },
        )

    return (
        pd.DataFrame(series_by_symbol),
        return_observations,
        pd.DataFrame(liquidity_rows),
    )


def refresh_shared_daily(
    shortlist: pd.DataFrame,
    *,
    trade_date: str,
    lookback_days: int,
    min_observations: int,
    liquidity_lookback_days: int,
    min_amount_observations: int,
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
    latest_date_stale = missing_symbols_for_date(existing, universe, target)
    _, cached_return_counts, cached_liquidity = market_metrics(
        existing,
        shortlist,
        trade_date=trade_date,
        lookback_days=lookback_days,
        liquidity_lookback_days=liquidity_lookback_days,
    )
    cached_amount_counts = cached_liquidity.set_index("symbol")[
        "amount_observations"
    ].to_dict()
    insufficient_cache = sorted(
        symbol
        for symbol in universe["symbol"].astype(str)
        if cached_return_counts.get(symbol, 0) < min_observations
        or cached_amount_counts.get(symbol, 0) < min_amount_observations
    )
    stale_symbols = sorted(set(latest_date_stale) | set(insufficient_cache))
    audit: dict[str, Any] = {
        "cache": str(paths.data_daily),
        "adjust": "qfq",
        "target_trade_date": target.isoformat(),
        "requested_symbols": universe["symbol"].astype(str).tolist(),
        "stale_symbols": stale_symbols,
        "latest_date_stale_symbols": latest_date_stale,
        "insufficient_cache_symbols": insufficient_cache,
        "refreshed_symbols": [],
        "refresh_failures": [],
    }
    if not stale_symbols:
        return existing, audit

    stale_universe = universe[universe["symbol"].astype(str).isin(stale_symbols)]
    log(f"refresh shared qfq cache for {len(stale_universe)} shortlisted ETFs")
    incoming = AkshareMarketDataClient(adjust="qfq").fetch_daily(
        stale_universe,
        five_years_before(end),
        end,
    )
    if incoming.empty:
        audit["refresh_failures"] = stale_symbols
        return existing, audit

    incoming["symbol"] = incoming["symbol"].astype(str)
    incoming["date"] = pd.to_datetime(incoming["date"])
    complete_symbols = set(
        incoming.loc[incoming["date"].dt.date.eq(target), "symbol"].astype(str),
    )
    complete_incoming = incoming[incoming["symbol"].isin(complete_symbols)].copy()
    merged = (
        replace_symbol_history(existing, complete_incoming)
        if not complete_incoming.empty
        else existing
    )
    if not complete_incoming.empty:
        problems = validate_daily(merged)
        if problems:
            raise RuntimeError(f"Shared qfq cache validation failed: {problems}")
        paths.ensure_data()
        write_table(merged, paths.data_daily)
        merged = read_daily(paths)

    # A current bar proves the refresh succeeded. ETFs that are too new for the
    # return or liquidity windows are normal filter exclusions, not refresh
    # failures that should prevent promotion of the rest of the universe.
    failures = sorted(
        symbol
        for symbol in stale_symbols
        if symbol not in complete_symbols
    )
    audit["refreshed_symbols"] = sorted(set(stale_symbols) - set(failures))
    audit["refresh_failures"] = failures
    return merged, audit


def filter_candidates(
    shortlist: pd.DataFrame,
    return_observations: dict[str, int],
    liquidity: pd.DataFrame,
    *,
    min_observations: int,
    min_amount_observations: int,
    min_median_amount: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    metrics = shortlist.merge(liquidity, on="symbol", how="left")
    metrics["return_observations"] = (
        metrics["symbol"].map(return_observations).fillna(0).astype(int)
    )
    reasons: list[list[str]] = []
    for row in metrics.itertuples(index=False):
        row_reasons: list[str] = []
        if row.return_observations < min_observations:
            row_reasons.append("insufficient_return_history")
        if row.amount_observations < min_amount_observations:
            row_reasons.append("insufficient_amount_history")
        if pd.isna(row.amount_median) or row.amount_median < min_median_amount:
            row_reasons.append("median_amount_below_minimum")
        reasons.append(row_reasons)
    metrics["eligible"] = [not row_reasons for row_reasons in reasons]
    metrics["exclusion_reasons"] = ["|".join(row_reasons) for row_reasons in reasons]
    eligible = metrics[metrics["eligible"]].copy()
    eligible = eligible.sort_values(
        ["amount_median", "fund_size", "symbol"],
        ascending=[False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    audit = {
        "history_filter": {
            "minimum_observations": int(min_observations),
            "excluded_count": int(
                metrics["exclusion_reasons"].str.contains(
                    "insufficient_return_history",
                    regex=False,
                ).sum(),
            ),
        },
        "liquidity_filter": {
            "lookback_metric": "median_daily_amount",
            "amount_proxy": "close_times_volume_times_100_when_amount_missing",
            "minimum_amount_observations": int(min_amount_observations),
            "minimum_median_amount": float(min_median_amount),
            "eligible_count": int(len(eligible)),
            "excluded_count": int((~metrics["eligible"]).sum()),
            "excluded": [
                {
                    "symbol": str(row.symbol),
                    "name": str(row.name),
                    "amount_observations": int(row.amount_observations),
                    "amount_proxy_observations": int(row.amount_proxy_observations),
                    "amount_median": (
                        None if pd.isna(row.amount_median) else float(row.amount_median)
                    ),
                    "return_observations": int(row.return_observations),
                    "reasons": str(row.exclusion_reasons).split("|"),
                }
                for row in metrics[~metrics["eligible"]].itertuples(index=False)
            ],
        },
    }
    return eligible, metrics, audit


def correlation_select(
    eligible: pd.DataFrame,
    returns: pd.DataFrame,
    *,
    min_observations: int,
    corr_threshold: float,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, Any],
]:
    if eligible.empty:
        empty_decisions = eligible.assign(
            selected=pd.Series(dtype="bool"),
            exclusion_reason=pd.Series(dtype="object"),
            blocking_symbol=pd.Series(dtype="object"),
            blocking_correlation=pd.Series(dtype="float64"),
            pair_observations=pd.Series(dtype="Int64"),
        )
        return (
            eligible[FINAL_COLUMNS].copy(),
            pd.DataFrame(columns=["symbol"]),
            pd.DataFrame(columns=["symbol"]),
            empty_decisions,
            {
                "method": "liquidity_ordered_greedy_pairwise",
                "comparison": "pearson_return_correlation_without_absolute_value",
                "threshold": float(corr_threshold),
                "minimum_pair_observations": int(min_observations),
                "selected_count": 0,
                "excluded_count": 0,
                "excluded": [],
            },
        )

    symbols = eligible["symbol"].astype(str).tolist()
    aligned_returns = returns.reindex(columns=symbols)
    valid = aligned_returns.notna().astype("int64")
    pair_observations = valid.T.dot(valid)
    correlations = aligned_returns.corr(min_periods=min_observations)
    correlation_output = correlations.copy()
    correlation_output.index.name = "symbol"
    correlation_output = correlation_output.reset_index()
    pair_output = pair_observations.copy()
    pair_output.index.name = "symbol"
    pair_output = pair_output.reset_index()

    selected_symbols: list[str] = []
    decision_rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in eligible.itertuples(index=False):
        symbol = str(row.symbol)
        missing_pairs = [
            kept_symbol
            for kept_symbol in selected_symbols
            if int(pair_observations.loc[symbol, kept_symbol]) < min_observations
            or pd.isna(correlations.loc[symbol, kept_symbol])
        ]
        conflicts = [
            (kept_symbol, float(correlations.loc[symbol, kept_symbol]))
            for kept_symbol in selected_symbols
            if kept_symbol not in missing_pairs
            and float(correlations.loc[symbol, kept_symbol]) > corr_threshold
        ]
        decision: dict[str, Any] = {
            key: getattr(row, key)
            for key in eligible.columns
        }
        decision.update(
            {
                "selected": False,
                "exclusion_reason": "",
                "blocking_symbol": "",
                "blocking_correlation": np.nan,
                "pair_observations": pd.NA,
            },
        )
        if missing_pairs:
            blocking_symbol = missing_pairs[0]
            observation_count = int(pair_observations.loc[symbol, blocking_symbol])
            decision.update(
                {
                    "exclusion_reason": "insufficient_pair_history",
                    "blocking_symbol": blocking_symbol,
                    "pair_observations": observation_count,
                },
            )
            excluded.append(
                {
                    "symbol": symbol,
                    "name": str(row.name),
                    "blocking_symbol": blocking_symbol,
                    "pair_observations": observation_count,
                    "minimum_pair_observations": int(min_observations),
                    "reason": "insufficient_pair_history",
                },
            )
        elif conflicts:
            blocking_symbol, correlation = sorted(
                conflicts,
                key=lambda item: (-item[1], item[0]),
            )[0]
            observation_count = int(pair_observations.loc[symbol, blocking_symbol])
            decision.update(
                {
                    "exclusion_reason": "correlation_above_threshold",
                    "blocking_symbol": blocking_symbol,
                    "blocking_correlation": correlation,
                    "pair_observations": observation_count,
                },
            )
            excluded.append(
                {
                    "symbol": symbol,
                    "name": str(row.name),
                    "blocking_symbol": blocking_symbol,
                    "correlation": correlation,
                    "pair_observations": observation_count,
                    "threshold": float(corr_threshold),
                    "reason": "correlation_above_threshold",
                },
            )
        else:
            selected_symbols.append(symbol)
            decision["selected"] = True
        decision_rows.append(decision)

    by_symbol = eligible.set_index("symbol", drop=False)
    selected = by_symbol.loc[selected_symbols, FINAL_COLUMNS].reset_index(drop=True)
    decisions = pd.DataFrame(decision_rows)
    audit = {
        "method": "liquidity_ordered_greedy_pairwise",
        "ordering": "amount_median_desc_fund_size_desc_symbol_asc",
        "comparison": "pearson_return_correlation_without_absolute_value",
        "threshold": float(corr_threshold),
        "minimum_pair_observations": int(min_observations),
        "selected_count": int(len(selected)),
        "excluded_count": int(len(excluded)),
        "excluded": excluded,
    }
    return selected, correlation_output, pair_output, decisions, audit


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
    returns, return_observations, liquidity = market_metrics(
        daily,
        shortlist,
        trade_date=args.date,
        lookback_days=args.lookback_days,
        liquidity_lookback_days=args.liquidity_lookback_days,
    )
    eligible, liquidity_output, filter_audit = filter_candidates(
        shortlist,
        return_observations,
        liquidity,
        min_observations=args.min_observations,
        min_amount_observations=args.min_amount_observations,
        min_median_amount=args.min_median_amount,
    )
    selected, correlations, pair_observations, decisions, correlation_audit = (
        correlation_select(
            eligible,
            returns,
            min_observations=args.min_observations,
            corr_threshold=args.corr_threshold,
        )
    )

    shortlist.to_csv(output_dir / "shortlist.csv", index=False)
    liquidity_output.to_csv(output_dir / "liquidity.csv", index=False)
    correlations.to_csv(output_dir / "correlation.csv", index=False)
    pair_observations.to_csv(output_dir / "pair_observations.csv", index=False)
    decisions.to_csv(output_dir / "selection.csv", index=False)
    selected.to_csv(output_dir / "selected_universe.csv", index=False)
    legacy_clusters = output_dir / "clusters.csv"
    if legacy_clusters.exists():
        legacy_clusters.unlink()

    refresh_failures = list((refresh_audit or {}).get("refresh_failures", []))
    apply_blockers: list[dict[str, Any]] = []
    if args.apply and refresh_failures:
        apply_blockers.append(
            {"reason": "market_data_refresh_failed", "symbols": refresh_failures},
        )
    if args.apply and selected.empty:
        apply_blockers.append({"reason": "selected_universe_empty", "symbols": []})
    applied = bool(args.apply and not apply_blockers)
    backup = apply_universe(
        selected,
        apply=applied,
        output_dir=output_dir,
        destination=destination,
    )
    summary = {
        "date": args.date,
        "historical_return_ranking_used": False,
        "parameters": {
            "min_fund_size": float(args.min_fund_size),
            "shortlist_size": int(args.shortlist_size),
            "liquidity_lookback_days": int(args.liquidity_lookback_days),
            "min_amount_observations": int(args.min_amount_observations),
            "min_median_amount": float(args.min_median_amount),
            "lookback_days": int(args.lookback_days),
            "min_observations": int(args.min_observations),
            "corr_threshold": float(args.corr_threshold),
        },
        **shortlist_audit,
        **filter_audit,
        "correlation_filter": correlation_audit,
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
            "liquidity_csv": str(output_dir / "liquidity.csv"),
            "correlation_csv": str(output_dir / "correlation.csv"),
            "pair_observations_csv": str(output_dir / "pair_observations.csv"),
            "selection_csv": str(output_dir / "selection.csv"),
            "selected_universe_csv": str(output_dir / "selected_universe.csv"),
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
            "Refusing to apply active ETF universe because "
            f"{reasons}; preview and audit outputs were retained in {output_dir}",
        )
    return summary


def command_build(args: argparse.Namespace) -> None:
    validate_args(args)
    root = Path(getattr(args, "root", REPO_ROOT))
    if args.output_dir is None:
        args.output_dir = str(root / "outputs" / "active_etf_universe")
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
        liquidity_lookback_days=args.liquidity_lookback_days,
        min_amount_observations=args.min_amount_observations,
        root=root,
    )
    summary = build_outputs(
        args,
        spot=spot,
        daily=daily,
        refresh_audit=refresh_audit,
        destination=root / "universes" / "active_etf_rotation.csv",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    command_build(parse_args())


if __name__ == "__main__":
    main()
