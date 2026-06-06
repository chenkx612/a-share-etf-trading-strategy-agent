from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

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


@dataclass
class AkshareETFProvider:
    """AKShare-backed ETF data provider with normalized output schema."""

    adjust: str = ""

    def fetch_universe(self, min_fund_size_cny: float) -> pd.DataFrame:
        import akshare as ak

        spot = ak.fund_etf_spot_em()
        column_map = {
            "代码": "symbol",
            "名称": "name",
            "总市值": "fund_size",
            "流通市值": "fund_size",
        }
        df = spot.rename(columns={k: v for k, v in column_map.items() if k in spot.columns})
        required = {"symbol", "name"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"AKShare ETF universe missing columns: {sorted(missing)}")
        if "fund_size" in df.columns:
            df["fund_size"] = pd.to_numeric(df["fund_size"], errors="coerce")
            df = df[df["fund_size"] >= min_fund_size_cny]
        else:
            df["fund_size"] = pd.NA
        return df[["symbol", "name", "fund_size"]].drop_duplicates("symbol")

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


def to_tencent_symbol(symbol: str) -> str:
    if symbol.startswith(("5", "6", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


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


def normalize_tencent_daily(raw: pd.DataFrame, symbol: str, name: str) -> pd.DataFrame:
    df = raw.rename(columns={"amount": "volume"}).copy()
    missing = [col for col in ["date", "open", "high", "low", "close"] if col not in df.columns]
    if missing:
        raise ValueError(f"Tencent ETF daily data for {symbol} missing columns: {missing}")
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
