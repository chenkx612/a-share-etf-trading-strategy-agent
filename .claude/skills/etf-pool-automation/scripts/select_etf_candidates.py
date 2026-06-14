#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import requests


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quant_agent.config import SECTOR_ROTATION_UNIVERSE_NAME, get_universe_config  # noqa: E402
from quant_agent.paths import ProjectPaths  # noqa: E402
from quant_agent.storage import read_table  # noqa: E402


DEFAULT_MIN_FUND_SIZE_CNY = 10_000_000_000

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

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
QUOTE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://gu.qq.com/",
}


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select ETF pool candidate symbols from large A-share ETFs ranked by date return.",
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="Trade date, YYYY-MM-DD.")
    parser.add_argument("--min-fund-size", type=float, default=DEFAULT_MIN_FUND_SIZE_CNY)
    parser.add_argument("--top-shortlist", type=int, default=30)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--root", default="workspaces/sector_rotation")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--candidates",
        help="Optional comma-separated symbol override after semantic de-duplication review.",
    )
    return parser.parse_args()


def theme_for(name: str) -> str:
    upper_name = name.upper()
    for theme, keywords in THEME_KEYWORDS:
        if any(keyword.upper() in upper_name for keyword in keywords):
            return theme
    return "other"


def load_base_pool(root: str) -> pd.DataFrame:
    paths = ProjectPaths(Path(root))
    try:
        frame = read_table(paths.data_universe / "sector_rotation_universe")
    except FileNotFoundError:
        config = get_universe_config(SECTOR_ROTATION_UNIVERSE_NAME)
        frame = config.to_frame()
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].astype(str)
    return frame


def base_pool_symbols(root: str) -> set[str]:
    return set(load_base_pool(root)["symbol"].astype(str))


def tencent_symbol(symbol: str) -> str:
    return f"sh{symbol}" if symbol.startswith(("5", "6", "9")) else f"sz{symbol}"


def tencent_query_symbol(symbol: str) -> str:
    return tencent_symbol(symbol)


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


def fetch_spot_with_tencent() -> pd.DataFrame:
    import akshare as ak

    ths = ak.fund_etf_spot_ths()
    symbols = ths["基金代码"].astype(str).drop_duplicates().tolist()
    rows: list[dict[str, object]] = []
    batch_size = 80
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start : start + batch_size]
        query = ",".join(tencent_query_symbol(symbol) for symbol in batch)
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


def candidate_rows(args: argparse.Namespace) -> list[CandidateRow]:
    spot = fetch_spot()
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
        raise RuntimeError(f"AKShare ETF spot data missing columns: {sorted(missing)}")

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
        raise RuntimeError("AKShare ETF spot data missing market value column")

    frame = frame[(frame["fund_size"] >= args.min_fund_size) & frame["return_pct"].notna()].copy()
    frame["theme"] = frame["name"].astype(str).map(theme_for)
    if "data_date" not in frame.columns:
        frame["data_date"] = args.date
    frame["data_date"] = frame["data_date"].fillna(args.date).astype(str)

    base_symbols = base_pool_symbols(args.root)

    rows: list[CandidateRow] = []
    for item in frame.itertuples(index=False):
        symbol = str(item.symbol)
        rows.append(
            CandidateRow(
                symbol=symbol,
                name=str(item.name),
                fund_size=to_json_number(item.fund_size),
                date=str(item.data_date),
                latest_price=to_json_number(item.latest_price),
                return_pct=float(item.return_pct),
                theme=str(item.theme),
                in_base_pool=symbol in base_symbols,
            )
        )
    return sorted(rows, key=lambda row: row.return_pct, reverse=True)


def to_json_number(value: object) -> float | None:
    if pd.isna(value):
        return None
    return float(value)


def select_diversified(rows: list[CandidateRow], count: int) -> list[CandidateRow]:
    selected: list[CandidateRow] = []
    used_themes: set[str] = set()
    eligible = [row for row in rows if not row.in_base_pool]

    for row in eligible:
        if row.theme in used_themes:
            continue
        selected.append(row)
        used_themes.add(row.theme)
        if len(selected) == count:
            return selected

    for row in eligible:
        if row.symbol in {selected_row.symbol for selected_row in selected}:
            continue
        selected.append(row)
        if len(selected) == count:
            return selected

    return selected


def parse_candidates(value: str) -> list[str]:
    return [part.strip().split(":", 1)[0] for part in value.split(",") if part.strip()]


def resolve_selected(args: argparse.Namespace, rows: list[CandidateRow]) -> list[CandidateRow]:
    if not args.candidates:
        return select_diversified(rows[: args.top_shortlist], args.count)

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
    return [by_symbol[symbol] for symbol in symbols]


def expanded_universe(root: str, selected: list[CandidateRow]) -> pd.DataFrame:
    base = load_base_pool(root)
    base_symbols = set(base["symbol"].astype(str))
    additions = [
        {"symbol": row.symbol, "name": row.name, "fund_size": row.fund_size}
        for row in selected
        if row.symbol not in base_symbols
    ]
    if not additions:
        return base.reset_index(drop=True)
    return pd.concat([base, pd.DataFrame(additions)], ignore_index=True)


def write_outputs(args: argparse.Namespace, rows: list[CandidateRow]) -> None:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    shortlist = rows[: args.top_shortlist]
    selected = resolve_selected(args, rows)
    if len(selected) != args.count:
        raise RuntimeError(f"Only selected {len(selected)} candidates; expected {args.count}")

    shortlist_frame = pd.DataFrame([asdict(row) for row in shortlist])
    selected_frame = pd.DataFrame([asdict(row) for row in selected])
    expanded = expanded_universe(args.root, selected)
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
