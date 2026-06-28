from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd


STANDARD_COLUMNS = [
    "date",
    "symbol",
    "name",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover",
]


def parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        try:
            import fastparquet  # noqa: F401
        except ImportError:
            return False
    return True


def table_path(base_path: Path) -> Path:
    if base_path.suffix:
        return base_path
    if parquet_available():
        return base_path.with_suffix(".parquet")
    return base_path.with_suffix(".csv")


def write_table(df: pd.DataFrame, base_path: Path) -> Path:
    path = table_path(base_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".parquet":
        df.to_parquet(path, index=False)
    elif path.suffix == ".csv":
        df.to_csv(path, index=False)
    else:
        raise ValueError(f"Unsupported table format: {path}")
    return path


def read_table(base_path: Path, parse_dates: list[str] | None = None) -> pd.DataFrame:
    candidates = [base_path] if base_path.suffix else [
        base_path.with_suffix(".parquet"),
        base_path.with_suffix(".csv"),
    ]
    for path in candidates:
        if path.exists():
            if path.suffix == ".parquet":
                return pd.read_parquet(path)
            if path.suffix == ".csv":
                return pd.read_csv(path, parse_dates=parse_dates)
    raise FileNotFoundError(f"No table found for {base_path}")


@dataclass(frozen=True)
class ProjectPaths:
    root: Path = Path(".")

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def data_daily(self) -> Path:
        return self.data / "etf_daily"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    def ensure_data(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)

    def ensure(self) -> None:
        self.ensure_data()
        for path in [
            self.outputs / "factors",
            self.outputs / "backtests",
            self.outputs / "recommendations",
            self.outputs / "reports",
        ]:
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class AkshareMarketDataClient:
    """AKShare-backed ETF daily data client with normalized output schema."""

    adjust: str = ""

    def fetch_daily(
        self,
        universe: pd.DataFrame,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for row in universe.itertuples(index=False):
            symbol = str(row.symbol)
            name = str(row.name)
            try:
                frame = self._fetch_daily_eastmoney(symbol, name, start, end)
            except Exception:
                frame = pd.DataFrame(columns=STANDARD_COLUMNS)
            if frame.empty:
                try:
                    frame = self._fetch_daily_tencent(symbol, name, start, end)
                except Exception:
                    frame = pd.DataFrame(columns=STANDARD_COLUMNS)
            if frame.empty:
                continue
            frames.append(frame)
        if not frames:
            return pd.DataFrame(columns=STANDARD_COLUMNS)
        return pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"])

    def fetch_daily_tencent(
        self,
        universe: pd.DataFrame,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for row in universe.itertuples(index=False):
            symbol = str(row.symbol)
            name = str(row.name)
            frame = self._fetch_daily_tencent(symbol, name, start, end)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame(columns=STANDARD_COLUMNS)
        return pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"])

    def _fetch_daily_eastmoney(
        self,
        symbol: str,
        name: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        import akshare as ak

        raw = ak.fund_etf_hist_em(
            symbol=symbol,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=self.adjust,
        )
        if raw.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)
        return normalize_daily(raw, symbol, name)

    def _fetch_daily_tencent(
        self,
        symbol: str,
        name: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        import akshare as ak

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                ak.stock_zh_a_hist_tx,
                symbol=to_tencent_symbol(symbol),
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=self.adjust,
            )
            raw = future.result(timeout=15)
        if raw.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)
        return normalize_tencent_daily(raw, symbol, name)


def load_universe(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path) if path.suffix == ".csv" else read_table(path)
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].astype(str)
    return frame


def universe_symbols(universe: pd.DataFrame) -> set[str]:
    return set(universe["symbol"].astype(str))


def parse_symbol_list(value: str) -> list[str]:
    return [part.strip().split(":", 1)[0] for part in value.split(",") if part.strip()]


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


def to_tencent_symbol(symbol: str) -> str:
    return f"sh{symbol}" if symbol.startswith(("5", "6", "9")) else f"sz{symbol}"


def normalize_daily(raw: pd.DataFrame, symbol: str, name: str) -> pd.DataFrame:
    mapping = {
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
    }
    df = raw.rename(columns={k: v for k, v in mapping.items() if k in raw.columns}).copy()
    missing = [col for col in ["date", "open", "high", "low", "close"] if col not in df.columns]
    if missing:
        raise ValueError(f"ETF daily data for {symbol} missing columns: {missing}")
    return _complete_standard_daily(df, symbol, name)


def normalize_tencent_daily(raw: pd.DataFrame, symbol: str, name: str) -> pd.DataFrame:
    df = raw.rename(columns={"amount": "volume"}).copy()
    missing = [col for col in ["date", "open", "high", "low", "close"] if col not in df.columns]
    if missing:
        raise ValueError(f"Tencent ETF daily data for {symbol} missing columns: {missing}")
    return _complete_standard_daily(df, symbol, name)


def _complete_standard_daily(df: pd.DataFrame, symbol: str, name: str) -> pd.DataFrame:
    df["symbol"] = symbol
    df["name"] = name
    for optional in ["volume", "amount", "turnover"]:
        if optional not in df.columns:
            df[optional] = pd.NA
    df["date"] = pd.to_datetime(df["date"])
    numeric_cols = ["open", "high", "low", "close", "volume", "amount", "turnover"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[STANDARD_COLUMNS]


def validate_daily(df: pd.DataFrame) -> list[str]:
    problems: list[str] = []
    missing = set(STANDARD_COLUMNS) - set(df.columns)
    if missing:
        problems.append(f"missing columns: {sorted(missing)}")
    if not df.empty:
        duplicated = df.duplicated(["date", "symbol"]).sum()
        if duplicated:
            problems.append(f"duplicated date/symbol rows: {duplicated}")
        null_close = df["close"].isna().sum()
        if null_close:
            problems.append(f"rows with null close: {null_close}")
    return problems


def merge_incremental(existing: pd.DataFrame | None, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        merged = incoming.copy()
    else:
        merged = pd.concat([existing, incoming], ignore_index=True)
    return (
        merged.drop_duplicates(["date", "symbol"], keep="last")
        .sort_values(["date", "symbol"])
        .reset_index(drop=True)
    )


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
            "Market data refresh did not return complete data for latest trade date "
            f"{target_trade_date}; missing symbols={missing}; local daily data was not updated"
        )
    return incoming, target_trade_date


def fetch_daily_for_universe(
    client: AkshareMarketDataClient,
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
        frame = client._fetch_daily_tencent(symbol, name, start, end)
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
    client: AkshareMarketDataClient,
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
        universe,
        start,
        end,
        existing=existing,
        fetch_one=lambda frame, range_start, range_end: fetch_daily_for_universe(
            client,
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
