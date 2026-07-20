#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from quant_core.data.market_data import (  # noqa: E402
    expanded_universe as build_expanded_universe,
    load_universe,
    parse_symbol_list,
    to_tencent_symbol,
)


DEFAULT_MIN_FUND_SIZE_CNY = 10_000_000_000
DEFAULT_BASE_POOL = REPO_ROOT / "universes" / "sector_rotation.csv"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
QUOTE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://gu.qq.com/",
}

THEME_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("semiconductor", ("半导体", "芯片", "集成电路", "科创芯片", "科创半导体")),
    ("ai_software", ("人工智能", "AI", "软件", "云计算", "计算机", "信创", "大数据")),
    ("robotics", ("机器人", "智能制造", "工业母机")),
    ("defense", ("军工", "国防", "航空航天", "卫星")),
    ("new_energy", ("新能源", "光伏", "电池", "锂电", "储能", "碳中和")),
    ("ev_auto", ("汽车", "新能源车", "智能车")),
    ("medicine", ("医药", "医疗", "创新药", "生物", "疫苗", "港股通医药")),
    ("consumer", ("消费", "食品", "酒", "家电", "农业", "养殖")),
    ("finance", ("银行", "证券", "券商", "保险", "金融", "红利")),
    ("real_estate", ("地产", "房地产", "基建", "建材")),
    ("energy_materials", ("煤炭", "石油", "油气", "能源", "有色", "钢铁", "化工", "稀土")),
    ("broad_index", ("沪深", "中证", "上证", "创业板", "科创", "A50", "500", "1000", "300")),
    ("hong_kong", ("恒生", "港股", "香港", "中概", "H股")),
    ("overseas", ("纳斯达克", "标普", "德国", "法国", "日经", "印度", "美国", "QDII")),
    ("bond_cash", ("债", "货币", "现金", "添利", "短融")),
    ("commodity", ("黄金", "白银", "豆粕", "商品")),
]


@dataclass(frozen=True)
class CandidateRow:
    symbol: str
    name: str
    fund_size: float | None
    date: str
    latest_price: float | None
    return_pct: float
    theme: str
    in_base_pool: bool
    base_theme_overlap: bool = False
    base_theme_matches: str = ""
    base_name_duplicate: bool = False


def normalized_chinese_name(name: object) -> str:
    return re.sub(r"\s+", "", str(name)).strip()


def normalized_etf_exposure_name(name: object) -> str:
    normalized = normalized_chinese_name(name)
    match = re.search(r"ETF", normalized, flags=re.IGNORECASE)
    if match:
        return normalized[: match.start()]
    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select ETF pool candidate symbols from large A-share ETFs ranked by date return.",
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="Trade date, YYYY-MM-DD.")
    parser.add_argument("--min-fund-size", type=float, default=DEFAULT_MIN_FUND_SIZE_CNY)
    parser.add_argument("--top-shortlist", type=int, default=30)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--base-universe", default=str(DEFAULT_BASE_POOL))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--candidates",
        help="Optional comma-separated symbol override after semantic de-duplication review.",
    )
    return parser.parse_args()


def load_base_pool(base_universe: str) -> pd.DataFrame:
    frame = load_universe(Path(base_universe))
    frame["name"] = frame["name"].astype(str)
    return frame


def theme_for(name: str) -> str:
    upper_name = name.upper()
    for theme, keywords in THEME_KEYWORDS:
        if any(keyword.upper() in upper_name for keyword in keywords):
            return theme
    return "other"


def base_theme_matches(base_pool: pd.DataFrame) -> dict[str, str]:
    if base_pool.empty:
        return {}
    frame = base_pool.copy()
    frame["name"] = frame["name"].astype(str)
    frame["theme"] = frame["name"].map(theme_for)
    frame = frame[frame["theme"] != "other"]
    matches: dict[str, list[str]] = {}
    for row in frame.itertuples(index=False):
        matches.setdefault(str(row.theme), []).append(f"{row.symbol}:{row.name}")
    return {theme: ";".join(values) for theme, values in matches.items()}


def to_json_number(value: object) -> float | None:
    if pd.isna(value):
        return None
    return float(value)


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
    import requests

    ths = ak.fund_etf_spot_ths()
    symbols = ths["基金代码"].astype(str).drop_duplicates().tolist()
    rows: list[dict[str, object]] = []
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start : start + batch_size]
        query = ",".join(to_tencent_symbol(symbol) for symbol in batch)
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


def candidate_rows(args: argparse.Namespace) -> list[CandidateRow]:
    frame = normalize_spot_frame(fetch_spot(), args.date)
    frame = frame[(frame["fund_size"] >= args.min_fund_size) & frame["return_pct"].notna()].copy()
    frame["theme"] = frame["name"].astype(str).map(theme_for)
    base_pool = load_base_pool(args.base_universe)
    base_symbols = set(base_pool["symbol"].astype(str))
    base_exposure_names = {
        normalized_etf_exposure_name(name)
        for name in base_pool["name"]
        if normalized_etf_exposure_name(name)
    }
    theme_matches = base_theme_matches(base_pool)

    rows: list[CandidateRow] = []
    for item in frame.itertuples(index=False):
        symbol = str(item.symbol)
        theme = str(item.theme)
        rows.append(
            CandidateRow(
                symbol=symbol,
                name=str(item.name),
                fund_size=to_json_number(item.fund_size),
                date=str(item.data_date),
                latest_price=to_json_number(item.latest_price),
                return_pct=float(item.return_pct),
                theme=theme,
                in_base_pool=symbol in base_symbols,
                base_theme_overlap=theme in theme_matches,
                base_theme_matches=theme_matches.get(theme, ""),
                base_name_duplicate=normalized_etf_exposure_name(item.name) in base_exposure_names,
            )
        )
    return sorted(rows, key=lambda row: row.return_pct, reverse=True)


def select_diversified(rows: list[CandidateRow], count: int) -> list[CandidateRow]:
    return script_deduplicated_rows(rows)[:count]


def script_deduplicated_rows(rows: list[CandidateRow]) -> list[CandidateRow]:
    selected: list[CandidateRow] = []
    used_names: set[str] = set()
    eligible = [row for row in rows if not row.in_base_pool and not row.base_name_duplicate]

    for row in eligible:
        name = normalized_etf_exposure_name(row.name)
        if name and name in used_names:
            continue
        selected.append(row)
        if name:
            used_names.add(name)

    return selected


def parse_candidates(value: str) -> list[str]:
    return parse_symbol_list(value)


def resolve_selected(args: argparse.Namespace, rows: list[CandidateRow]) -> list[CandidateRow]:
    if not args.candidates:
        return select_diversified(rows, args.count)

    symbols = parse_candidates(args.candidates)
    if len(symbols) != args.count:
        raise RuntimeError(f"Expected {args.count} candidate symbols, got {len(symbols)}")
    by_symbol = {row.symbol: row for row in rows}
    missing = [symbol for symbol in symbols if symbol not in by_symbol]
    if missing:
        raise RuntimeError(f"Candidate symbols not found in large ETF universe data: {missing}")
    in_base = [symbol for symbol in symbols if by_symbol[symbol].in_base_pool]
    if in_base:
        raise RuntimeError(f"Candidate symbols already exist in the original sector-rotation pool: {in_base}")
    base_name_duplicates = [symbol for symbol in symbols if by_symbol[symbol].base_name_duplicate]
    if base_name_duplicates:
        raise RuntimeError(
            "Candidate symbols duplicate original sector-rotation pool ETF Chinese names: "
            f"{base_name_duplicates}"
        )
    return [by_symbol[symbol] for symbol in symbols]


def expanded_universe(base_universe: str, selected: list[CandidateRow]) -> pd.DataFrame:
    base = load_base_pool(base_universe)
    additions = pd.DataFrame([
        {"symbol": row.symbol, "name": row.name, "fund_size": row.fund_size}
        for row in selected
    ])
    return build_expanded_universe(base, additions)


def write_outputs(args: argparse.Namespace, rows: list[CandidateRow]) -> None:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    shortlist = script_deduplicated_rows(rows)[: args.top_shortlist]
    selected = resolve_selected(args, rows)
    if len(selected) != args.count:
        raise RuntimeError(f"Only selected {len(selected)} candidates; expected {args.count}")

    shortlist_frame = pd.DataFrame([asdict(row) for row in shortlist])
    selected_frame = pd.DataFrame([asdict(row) for row in selected])
    expanded = expanded_universe(args.base_universe, selected)
    shortlist_frame.to_csv(output_dir / "candidate_shortlist.csv", index=False)
    selected_frame.to_csv(output_dir / "candidate_selected.csv", index=False)
    expanded.to_csv(output_dir / "expanded_refresh_universe.csv", index=False)

    payload = {
        "date": args.date,
        "min_fund_size_cny": args.min_fund_size,
        "candidate_symbols": [row.symbol for row in selected],
        "candidate_arg": ",".join(row.symbol for row in selected),
        "manual_override": bool(args.candidates),
        "selected": [asdict(row) for row in selected],
        "shortlist_csv": str(output_dir / "candidate_shortlist.csv"),
        "selected_csv": str(output_dir / "candidate_selected.csv"),
        "expanded_refresh_universe_csv": str(output_dir / "expanded_refresh_universe.csv"),
    }
    (output_dir / "candidate_selected.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    args = parse_args()
    rows = candidate_rows(args)
    write_outputs(args, rows)


if __name__ == "__main__":
    main()
