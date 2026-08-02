from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

from quant_core.data.market_data import (
    AkshareMarketDataClient,
    ProjectPaths,
    latest_trade_date_on_or_before,
    missing_symbols_for_date,
    read_daily,
    replace_symbol_history,
    validate_daily,
    write_table,
)


FINAL_COLUMNS = ["symbol", "name", "fund_size"]
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
QUOTE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://gu.qq.com/",
}


def to_tencent_symbol(symbol: str) -> str:
    return f"sh{symbol}" if symbol.startswith(("5", "6", "9")) else f"sz{symbol}"


def parse_tencent_quote(line: str) -> dict[str, object] | None:
    if '="' not in line:
        return None
    payload = line.split('="', 1)[1].rstrip('";\n')
    parts = payload.split("~")
    if len(parts) <= 73:
        return None
    return {
        "代码": parts[2],
        "名称": parts[1],
        "总市值": parts[72],
        "流通市值": parts[73],
    }


def fetch_spot_with_tencent(batch_size: int = 80) -> pd.DataFrame:
    import akshare as ak
    import requests

    product_list = ak.fund_etf_spot_ths()
    symbols = product_list["基金代码"].astype(str).drop_duplicates().tolist()
    rows: list[dict[str, object]] = []
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start : start + batch_size]
        query = ",".join(to_tencent_symbol(symbol) for symbol in batch)
        response = requests.get(
            f"{TENCENT_QUOTE_URL}{query}",
            headers=QUOTE_HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        for line in response.text.strip().splitlines():
            parsed = parse_tencent_quote(line)
            if parsed is not None:
                rows.append(parsed)
    if not rows:
        raise RuntimeError("Tencent quote fallback returned no ETF rows")
    return pd.DataFrame(rows)


def fetch_spot() -> pd.DataFrame:
    import akshare as ak

    try:
        return ak.fund_etf_spot_em()
    except Exception as exc:
        print(
            f"AKShare fund_etf_spot_em failed; falling back to Tencent quote API: {exc}",
            file=sys.stderr,
        )
        return fetch_spot_with_tencent()


def normalized_etf_group_key(name: object) -> str:
    normalized = re.sub(r"\s+", "", str(name)).strip()
    match = re.search("ETF", normalized, flags=re.IGNORECASE)
    grouped = normalized[: match.start()] if match else normalized
    return grouped.casefold()


def normalize_spot_frame(spot: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "代码": "symbol",
        "基金代码": "symbol",
        "名称": "name",
        "基金名称": "name",
        "总市值": "total_market_value",
        "流通市值": "float_market_value",
    }
    frame = spot.rename(
        columns={key: value for key, value in aliases.items() if key in spot.columns},
    ).copy()
    missing = {"symbol", "name"} - set(frame.columns)
    if missing:
        raise RuntimeError(f"ETF spot data missing columns: {sorted(missing)}")
    total = (
        pd.to_numeric(frame["total_market_value"], errors="coerce")
        if "total_market_value" in frame.columns
        else pd.Series(pd.NA, index=frame.index, dtype="Float64")
    )
    floating = (
        pd.to_numeric(frame["float_market_value"], errors="coerce")
        if "float_market_value" in frame.columns
        else pd.Series(pd.NA, index=frame.index, dtype="Float64")
    )
    if total.isna().all() and floating.isna().all():
        raise RuntimeError("ETF spot data missing market value column")

    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame["name"] = frame["name"].astype(str).str.strip()
    frame["fund_size"] = total.fillna(floating)
    frame = frame[frame["symbol"].ne("") & frame["name"].ne("")].copy()
    frame["group_key"] = frame["name"].map(normalized_etf_group_key)
    return frame[["symbol", "name", "fund_size", "group_key"]]


def five_years_before(value: date) -> date:
    try:
        return value.replace(year=value.year - 5)
    except ValueError:
        return value.replace(year=value.year - 5, day=28)


def atomic_write_csv(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        frame.to_csv(temporary, index=False)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def apply_universe(
    selected: pd.DataFrame,
    *,
    apply: bool,
    output_dir: Path,
    destination: Path,
) -> Path | None:
    if not apply:
        return None
    backup: Path | None = None
    if destination.exists():
        backup = output_dir / "universe_before.csv"
        shutil.copy2(destination, backup)
    atomic_write_csv(selected[FINAL_COLUMNS], destination)
    return backup


def json_number(value: object) -> float | None:
    return None if pd.isna(value) else float(value)
