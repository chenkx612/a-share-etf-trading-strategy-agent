from __future__ import annotations

import sys

import pandas as pd
import requests


TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
QUOTE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://gu.qq.com/",
}


def tencent_symbol(symbol: str) -> str:
    return f"sh{symbol}" if symbol.startswith(("5", "6", "9")) else f"sz{symbol}"


def parse_tencent_quote(line: str) -> dict[str, object] | None:
    if '="' not in line:
        return None
    payload = line.split('="', 1)[1].rstrip('";\n')
    parts = payload.split("~")
    if len(parts) <= 73:
        return None
    timestamp = parts[30]
    return {
        "代码": parts[2],
        "名称": parts[1],
        "最新价": parts[3],
        "涨跌幅": parts[32],
        "总市值": parts[72],
        "流通市值": parts[73],
        "数据日期": timestamp[:8] if len(timestamp) >= 8 else "",
    }


def fetch_spot_with_tencent(batch_size: int = 80) -> pd.DataFrame:
    import akshare as ak

    ths = ak.fund_etf_spot_ths()
    symbols = ths["基金代码"].astype(str).drop_duplicates().tolist()
    rows: list[dict[str, object]] = []
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start : start + batch_size]
        query = ",".join(tencent_symbol(symbol) for symbol in batch)
        response = requests.get(f"{TENCENT_QUOTE_URL}{query}", headers=QUOTE_HEADERS, timeout=20)
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
        print(f"AKShare fund_etf_spot_em failed; falling back to Tencent quote API: {exc}", file=sys.stderr)
        return fetch_spot_with_tencent()


def normalize_spot_frame(spot: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    column_map = {
        "代码": "symbol",
        "名称": "name",
        "最新价": "latest_price",
        "涨跌幅": "return_pct",
        "总市值": "total_market_value",
        "流通市值": "float_market_value",
        "数据日期": "data_date",
    }
    frame = spot.rename(columns={k: v for k, v in column_map.items() if k in spot.columns}).copy()
    required = {"symbol", "name", "return_pct"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"ETF spot data missing columns: {sorted(missing)}")

    frame["symbol"] = frame["symbol"].astype(str)
    frame["return_pct"] = pd.to_numeric(frame["return_pct"], errors="coerce")
    if "latest_price" in frame.columns:
        frame["latest_price"] = pd.to_numeric(frame["latest_price"], errors="coerce")
    else:
        frame["latest_price"] = pd.NA
    if "total_market_value" in frame.columns:
        frame["fund_size"] = pd.to_numeric(frame["total_market_value"], errors="coerce")
    elif "float_market_value" in frame.columns:
        frame["fund_size"] = pd.to_numeric(frame["float_market_value"], errors="coerce")
    else:
        raise RuntimeError("ETF spot data missing market value column")

    frame = frame[frame["return_pct"].notna()].copy()
    if "data_date" not in frame.columns:
        frame["data_date"] = trade_date
    frame["data_date"] = frame["data_date"].fillna(trade_date).astype(str)
    return frame
