from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from quant_core.data.provider import AkshareETFProvider, merge_incremental, validate_daily
from quant_core.paths import ProjectPaths
from quant_core.storage import read_table, write_table


def load_universe(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path) if path.suffix == ".csv" else read_table(path)
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].astype(str)
    return frame


def universe_symbols(universe: pd.DataFrame) -> set[str]:
    return set(universe["symbol"].astype(str))


def expanded_universe(base: pd.DataFrame, additions: pd.DataFrame) -> pd.DataFrame:
    out = base.copy()
    out["symbol"] = out["symbol"].astype(str)
    base_symbols = set(out["symbol"])
    rows = additions.copy()
    rows["symbol"] = rows["symbol"].astype(str)
    rows = rows[~rows["symbol"].isin(base_symbols)]
    if rows.empty:
        return out.reset_index(drop=True)
    return pd.concat([out, rows], ignore_index=True)


def read_daily(paths: ProjectPaths) -> pd.DataFrame:
    return read_table(paths.data_daily, parse_dates=["date"])


def latest_trade_date_on_or_before(target: date) -> date:
    try:
        import akshare as ak

        calendar = ak.tool_trade_date_hist_sina()
        date_column = "trade_date" if "trade_date" in calendar.columns else calendar.columns[0]
        dates = pd.to_datetime(calendar[date_column]).dt.date
        available = dates[dates <= target]
        if not available.empty:
            return available.max()
    except Exception:
        pass

    candidate = target
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def universe_has_date(daily: pd.DataFrame | None, universe: pd.DataFrame, target: date) -> bool:
    return not missing_symbols_for_date(daily, universe, target)


def missing_symbols_for_date(daily: pd.DataFrame | None, universe: pd.DataFrame, target: date) -> list[str]:
    symbols = set(universe["symbol"].astype(str))
    if daily is None or daily.empty:
        return sorted(symbols)
    df = daily.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["symbol"] = df["symbol"].astype(str)
    covered = set(df[df["date"] == target]["symbol"])
    return sorted(symbols - covered)


def refresh_start_date(start: date, end: date) -> date:
    try:
        five_year_start = end.replace(year=end.year - 5)
    except ValueError:
        five_year_start = end.replace(year=end.year - 5, day=28)
    return max(start, five_year_start)


def fetch_daily_if_stale(
    provider: AkshareETFProvider,
    universe: pd.DataFrame,
    start: date,
    end: date,
    *,
    existing: pd.DataFrame | None,
    fetch_one: Callable[[pd.DataFrame, date, date], pd.DataFrame],
    log: Callable[[str], None] | None = None,
) -> tuple[pd.DataFrame, date]:
    target_trade_date = latest_trade_date_on_or_before(end)
    stale_symbols = missing_symbols_for_date(existing, universe, target_trade_date)
    if not stale_symbols:
        if log is not None:
            log(f"local daily data already covers latest trade date {target_trade_date}; skip fetch")
        return pd.DataFrame(), target_trade_date

    effective_start = refresh_start_date(start, end)
    if log is not None and effective_start != start:
        log(f"refresh start adjusted from {start} to {effective_start} for five-year qfq refresh window")
    stale_universe = universe[universe["symbol"].astype(str).isin(stale_symbols)].copy()
    if log is not None:
        log(f"refresh stale symbols for latest trade date {target_trade_date}: {stale_symbols}")
    incoming = fetch_one(stale_universe, effective_start, end)
    missing = missing_symbols_for_date(incoming, stale_universe, target_trade_date)
    if missing:
        raise RuntimeError(
            "Provider refresh did not return complete data for latest trade date "
            f"{target_trade_date}; missing symbols={missing}; local daily data was not updated"
        )
    return incoming, target_trade_date


def fetch_daily_for_universe(
    provider: AkshareETFProvider,
    universe: pd.DataFrame,
    start: date,
    end: date,
    *,
    log: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for row in universe.itertuples(index=False):
        symbol = str(row.symbol)
        name = str(row.name)
        if log is not None:
            log(f"fetch Tencent daily for {symbol}")
        frame = provider._fetch_daily_tencent(symbol, name, start, end)
        if frame.empty:
            if log is not None:
                log(f"no Tencent daily rows for {symbol} in {start}..{end}")
            continue
        frames.append(frame)
        if log is not None:
            log(f"fetched {len(frame)} rows for {symbol}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_tencent_daily_if_stale(
    provider: AkshareETFProvider,
    universe: pd.DataFrame,
    start: date,
    end: date,
    *,
    paths: ProjectPaths,
    log: Callable[[str], None] | None = None,
) -> tuple[pd.DataFrame, date]:
    try:
        existing = read_daily(paths)
    except FileNotFoundError:
        existing = None
    return fetch_daily_if_stale(
        provider,
        universe,
        start,
        end,
        existing=existing,
        fetch_one=lambda frame, range_start, range_end: fetch_daily_for_universe(
            provider,
            frame,
            range_start,
            range_end,
            log=log,
        ),
        log=log,
    )


def merge_and_store_daily(paths: ProjectPaths, incoming: pd.DataFrame, label: str) -> pd.DataFrame:
    if incoming.empty:
        raise RuntimeError(f"{label} returned no daily rows")
    try:
        existing_raw = read_daily(paths)
    except FileNotFoundError:
        existing_raw = None
    merged = merge_incremental(existing_raw, incoming)
    problems = validate_daily(merged)
    write_table(merged, paths.data_daily)
    if problems:
        print(f"data warnings after {label}:")
        for problem in problems:
            print(f"- {problem}")
    return read_daily(paths)


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


def resolve_complete_universe_date(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    requested_date: str,
) -> str:
    symbols = set(universe["symbol"].astype(str))
    dates = complete_universe_dates(daily, symbols, requested_date)
    if dates:
        return dates[-1].date().isoformat()

    requested = pd.Timestamp(requested_date)
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
        f"on or before {requested_date}; missing={missing}; latest_by_symbol={stale}"
    )
