#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from quant_core.data.market_data import (  # noqa: E402
    AkshareMarketDataClient,
    ProjectPaths,
    load_universe,
    read_daily,
    replace_symbol_history,
    write_table,
)


TASK_ID = "active-etf-rerank-topk"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".agents" / "skills" / TASK_ID / "outputs"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE = time(15, 0)
OUTPUT_COLUMNS = [
    "record_type",
    "signal_date",
    "holding_for",
    "execution_date",
    "symbol",
    "name",
    "score",
    "rank",
    "target_weight",
]


def shanghai_today() -> date:
    return datetime.now(SHANGHAI).date()


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    task_path: Path
    strategy_name: str
    strategy_module: str
    strategy_path: Path
    strategy_relative_path: str
    universe_path: Path
    universe_relative_path: str
    champion_json_path: Path
    champion_code_path: Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate production Active ETF Rerank Top-K target holdings for "
            "the next trading day."
        ),
    )
    parser.add_argument("--date", default=shanghai_today().isoformat())
    parser.add_argument("--data-root", default=str(REPO_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Use only local qfq data; intended for offline verification.",
    )
    return parser.parse_args(argv)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_file(repo_root: Path, raw_path: str, label: str) -> tuple[Path, str]:
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} must be a repository-relative path: {raw_path}")
    resolved = (repo_root / relative).resolve()
    try:
        normalized = resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"{label} escapes repository root: {raw_path}") from exc
    return resolved, normalized


def load_task_contract(
    repo_root: Path = REPO_ROOT,
    task_path: Path | None = None,
) -> TaskContract:
    task_path = (task_path or repo_root / "tasks" / "active_etf_rerank_topk.toml").resolve()
    if not task_path.is_file():
        raise FileNotFoundError(f"Task configuration does not exist: {task_path}")
    with task_path.open("rb") as handle:
        task = tomllib.load(handle)
    task_id = str(task.get("id", ""))
    if task_id != TASK_ID:
        raise ValueError(f"Task id must be {TASK_ID!r}, got {task_id!r}")

    strategy = task.get("strategy", {})
    strategy_module = str(strategy.get("module", ""))
    strategy_name = str(strategy.get("name", ""))
    if not strategy_module or not strategy_name:
        raise ValueError("Task configuration must define strategy.name and strategy.module")
    expected_relative = f"src/{strategy_module.replace('.', '/')}.py"
    editable = [str(value) for value in task.get("scope", {}).get("editable", [])]
    if expected_relative not in editable:
        raise ValueError(
            "Task strategy module is not the task's editable production strategy path: "
            f"{expected_relative}"
        )
    strategy_path, strategy_relative = _repo_file(
        repo_root,
        expected_relative,
        "strategy path",
    )

    universe_raw = str(task.get("data", {}).get("universe", ""))
    if not universe_raw:
        raise ValueError("Task configuration must define data.universe")
    universe_path, universe_relative = _repo_file(repo_root, universe_raw, "universe path")
    research_root = repo_root / ".research" / task_id
    return TaskContract(
        task_id=task_id,
        task_path=task_path,
        strategy_name=strategy_name,
        strategy_module=strategy_module,
        strategy_path=strategy_path,
        strategy_relative_path=strategy_relative,
        universe_path=universe_path,
        universe_relative_path=universe_relative,
        champion_json_path=research_root / "champion.json",
        champion_code_path=research_root / "champion.py",
    )


def verify_champion(contract: TaskContract) -> dict[str, Any]:
    required = [
        contract.strategy_path,
        contract.universe_path,
        contract.champion_json_path,
        contract.champion_code_path,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Champion synchronization validation failed; missing files="
            f"{missing}. Synchronize Champion before generating a recommendation."
        )
    try:
        metadata = json.loads(contract.champion_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            "Champion synchronization validation failed; champion.json is invalid. "
            "Synchronize Champion before generating a recommendation."
        ) from exc

    expected_hash = metadata.get("champion_sha256")
    checks = {
        "task_id": metadata.get("task_id") == contract.task_id,
        "strategy_path": metadata.get("strategy_path") == contract.strategy_relative_path,
        "champion_sha256_present": isinstance(expected_hash, str) and len(expected_hash) == 64,
    }
    champion_hash = file_sha256(contract.champion_code_path)
    production_hash = file_sha256(contract.strategy_path)
    checks["champion_metadata_hash"] = checks["champion_sha256_present"] and (
        champion_hash == expected_hash
    )
    checks["production_metadata_hash"] = checks["champion_sha256_present"] and (
        production_hash == expected_hash
    )
    checks["champion_matches_production"] = (
        contract.champion_code_path.read_bytes() == contract.strategy_path.read_bytes()
    )
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(
            "Champion synchronization validation failed; failed checks="
            f"{failed}. Synchronize Champion before generating a recommendation."
        )
    return {
        "status": "passed",
        "checks": checks,
        "champion_sha256": champion_hash,
        "production_strategy_sha256": production_hash,
        "champion_round_id": metadata.get("champion_round_id"),
    }


def parse_requested_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--date must use YYYY-MM-DD: {value}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"--date must use YYYY-MM-DD: {value}")
    return parsed


def exchange_trade_dates() -> list[date]:
    import akshare as ak

    calendar = ak.tool_trade_date_hist_sina()
    if calendar.empty:
        raise RuntimeError("Exchange trading calendar returned no dates")
    column = "trade_date" if "trade_date" in calendar.columns else calendar.columns[0]
    return sorted(set(pd.to_datetime(calendar[column], errors="raise").dt.date))


def resolve_signal_date(
    requested: date,
    *,
    now: datetime | None = None,
    trade_dates: Iterable[date],
) -> date:
    local_now = now.astimezone(SHANGHAI) if now is not None else datetime.now(SHANGHAI)
    if requested > local_now.date():
        raise RuntimeError(f"Requested date {requested} is in the future")
    calendar = sorted(set(trade_dates))
    if not calendar:
        raise RuntimeError("No trading calendar is available to resolve the signal date")
    if (
        requested == local_now.date()
        and requested in calendar
        and local_now.time().replace(tzinfo=None) < MARKET_CLOSE
    ):
        raise RuntimeError(
            f"Requested trading day {requested} has not closed; run after 15:00 Asia/Shanghai"
        )
    closed = [
        value
        for value in calendar
        if value <= requested
        and (
            value < local_now.date()
            or local_now.time().replace(tzinfo=None) >= MARKET_CLOSE
        )
    ]
    if not closed:
        raise RuntimeError(f"No closed trading day is available on or before {requested}")
    return closed[-1]


def next_trade_date(signal_date: date, trade_dates: Iterable[date]) -> date | None:
    later = sorted(value for value in set(trade_dates) if value > signal_date)
    return later[0] if later else None


def five_year_start(end: date) -> date:
    try:
        return end.replace(year=end.year - 5)
    except ValueError:
        return end.replace(year=end.year - 5, day=28)


def read_local_daily(data_root: Path) -> pd.DataFrame | None:
    try:
        return read_daily(ProjectPaths(data_root))
    except FileNotFoundError:
        return None


def validate_universe(universe: pd.DataFrame) -> None:
    required = {"symbol", "name", "fund_size"}
    missing = sorted(required - set(universe.columns))
    if missing:
        raise ValueError(f"Task universe is missing required columns: {missing}")
    if universe.empty:
        raise ValueError("Task universe must not be empty")
    duplicates = sorted(
        universe.loc[
            universe["symbol"].astype(str).duplicated(keep=False),
            "symbol",
        ]
        .astype(str)
        .unique()
        .tolist(),
    )
    if duplicates:
        raise ValueError(f"Task universe contains duplicate symbols: {duplicates}")


def _valid_close_symbols(daily: pd.DataFrame | None, target: date) -> set[str]:
    if daily is None or daily.empty:
        return set()
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame["symbol"] = frame["symbol"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    rows = frame[frame["date"] == target]
    valid = rows["close"].map(_is_valid_close)
    return set(rows.loc[valid, "symbol"])


def refresh_missing_symbols(
    daily: pd.DataFrame | None,
    universe: pd.DataFrame,
    signal_date: date,
    *,
    fetch_one: Callable[[pd.DataFrame, date, date], pd.DataFrame],
) -> tuple[pd.DataFrame, list[dict[str, Any]], bool]:
    working = daily.copy() if daily is not None else pd.DataFrame()
    valid_before = _valid_close_symbols(working, signal_date)
    audit: list[dict[str, Any]] = []
    changed = False
    refresh_start = five_year_start(signal_date)
    for row in universe.itertuples(index=False):
        symbol = str(row.symbol)
        if symbol in valid_before:
            continue
        entry: dict[str, Any] = {
            "symbol": symbol,
            "status": "excluded",
            "requested_start": refresh_start.isoformat(),
            "requested_end": signal_date.isoformat(),
        }
        one = universe[universe["symbol"].astype(str) == symbol].copy()
        try:
            incoming = fetch_one(one, refresh_start, signal_date)
        except Exception as exc:  # A single bad instrument must not block its peers.
            entry["reason"] = "refresh_failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            audit.append(entry)
            continue
        if incoming is None or incoming.empty:
            entry["reason"] = "refresh_returned_no_data"
            audit.append(entry)
            continue
        incoming = incoming.copy()
        incoming["symbol"] = incoming["symbol"].astype(str)
        incoming["date"] = pd.to_datetime(incoming["date"], errors="coerce")
        incoming = incoming[
            (incoming["symbol"] == symbol)
            & (incoming["date"].dt.date >= refresh_start)
            & (incoming["date"].dt.date <= signal_date)
        ]
        if symbol not in _valid_close_symbols(incoming, signal_date):
            entry["reason"] = "no_valid_signal_date_close_after_refresh"
            entry["rows_returned"] = int(len(incoming))
            audit.append(entry)
            continue
        working = replace_symbol_history(working if not working.empty else None, incoming)
        entry["status"] = "refreshed"
        entry["rows_replaced_with"] = int(len(incoming))
        audit.append(entry)
        changed = True
    return working, audit, changed


def _history_is_sufficient(
    daily: pd.DataFrame,
    symbol: str,
    required_dates: list[date],
) -> tuple[bool, int]:
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["symbol"] = frame["symbol"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    finite = frame["close"].map(_is_valid_close)
    frame = frame[finite]
    symbol_dates = set(frame.loc[frame["symbol"] == symbol, "date"].dt.date)
    available = sum(value in symbol_dates for value in required_dates)
    return available == len(required_dates), available


def _is_valid_close(value: Any) -> bool:
    try:
        return not pd.isna(value) and math.isfinite(float(value)) and float(value) > 0.0
    except (TypeError, ValueError):
        return False


def build_universe_audit(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    signal_date: date,
    params: Any,
    refresh_audit: list[dict[str, Any]],
    trade_dates: Iterable[date],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    universe_symbols = set(universe["symbol"].astype(str))
    audited_daily = daily[daily["symbol"].astype(str).isin(universe_symbols)].copy()
    valid_symbols = _valid_close_symbols(audited_daily, signal_date)
    refresh_by_symbol = {entry["symbol"]: entry for entry in refresh_audit}
    required_prices = max(
        int(params.min_history) + 1,
        int(params.momentum_window) + 1,
        int(params.vol_window) + 1,
    )
    required_dates = sorted(
        value for value in set(trade_dates) if value <= signal_date
    )[-required_prices:]
    calendar_has_required_history = len(required_dates) == required_prices
    rows: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for row in universe.to_dict(orient="records"):
        symbol = str(row["symbol"])
        sufficient, available = _history_is_sufficient(
            audited_daily,
            symbol,
            required_dates,
        )
        sufficient = calendar_has_required_history and sufficient
        item: dict[str, Any] = {
            "symbol": symbol,
            "name": str(row.get("name", symbol)),
            "fund_size": _json_scalar(row.get("fund_size")),
            "valid_signal_date_close": symbol in valid_symbols,
            "required_trailing_prices": required_prices,
            "available_trailing_prices": available,
            "history_sufficient": sufficient,
        }
        if symbol in refresh_by_symbol:
            item["refresh"] = refresh_by_symbol[symbol]
        if symbol not in valid_symbols:
            item["exclusion_reason"] = "missing_or_invalid_signal_date_close"
        elif not sufficient:
            item["exclusion_reason"] = "insufficient_contiguous_history"
        if "exclusion_reason" in item:
            exclusions.append({
                "symbol": symbol,
                "name": item["name"],
                "reason": item["exclusion_reason"],
                "refresh": item.get("refresh"),
            })
        rows.append(item)
    return rows, exclusions


def _json_scalar(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def build_output(
    selected: pd.DataFrame,
    signal_date: date,
    execution_date: date | None,
) -> tuple[pd.DataFrame, list[dict[str, Any]], float]:
    if selected.empty:
        final = selected
    else:
        dates = pd.to_datetime(selected["date"]).dt.date
        final = selected[dates == signal_date].copy()
    weights = (
        pd.to_numeric(final["target_weight"], errors="coerce").tolist()
        if not final.empty
        else []
    )
    if any(not math.isfinite(value) or value < 0.0 for value in weights):
        raise RuntimeError("Production strategy returned non-finite or negative target weights")
    raw_gross = float(sum(weights))
    if raw_gross > 1.0 + 1e-12:
        raise RuntimeError(f"Production strategy target weights exceed 1.0: {raw_gross}")

    common = {
        "signal_date": signal_date.isoformat(),
        "holding_for": "next_trading_day",
        "execution_date": execution_date.isoformat() if execution_date else None,
    }
    holdings: list[dict[str, Any]] = []
    for row in final.itertuples(index=False):
        holdings.append({
            "record_type": "holding",
            **common,
            "symbol": str(row.symbol),
            "name": str(row.name),
            "score": round(float(row.score), 12),
            "rank": int(row.rank),
            "target_weight": round(float(row.target_weight), 12),
        })
    target_gross = round(raw_gross, 12)
    if holdings:
        rounding_error = round(target_gross - sum(row["target_weight"] for row in holdings), 12)
        holdings[-1]["target_weight"] = round(
            holdings[-1]["target_weight"] + rounding_error,
            12,
        )
    cash_weight = round(1.0 - target_gross, 12)
    rows = holdings + [{
        "record_type": "cash",
        **common,
        "symbol": "CASH",
        "name": "现金",
        "score": None,
        "rank": None,
        "target_weight": cash_weight,
    }]
    output = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if round(float(output["target_weight"].sum()), 12) != 1.0:
        raise RuntimeError("ETF and cash target weights do not sum to 1.0")
    return output, holdings, cash_weight


def json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        records.append({
            key: _json_scalar(value)
            for key, value in raw.items()
        })
    return records


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run(
    args: argparse.Namespace,
    *,
    repo_root: Path = REPO_ROOT,
    now: datetime | None = None,
    trade_dates: Iterable[date] | None = None,
    fetch_one: Callable[[pd.DataFrame, date, date], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    contract = load_task_contract(repo_root)
    champion = verify_champion(contract)
    requested = parse_requested_date(args.date)
    data_root = Path(args.data_root).resolve()
    daily = read_local_daily(data_root)

    if trade_dates is None:
        try:
            calendar = exchange_trade_dates()
        except Exception as exc:
            raise RuntimeError("Cannot resolve the official trading calendar") from exc
    else:
        calendar = sorted(set(trade_dates))
    signal_date = resolve_signal_date(requested, now=now, trade_dates=calendar)
    execution_date = next_trade_date(signal_date, calendar)
    universe = load_universe(contract.universe_path)
    validate_universe(universe)

    refresh_audit: list[dict[str, Any]] = []
    if args.skip_refresh:
        refreshed = daily.copy() if daily is not None else pd.DataFrame()
    else:
        client = AkshareMarketDataClient(adjust="qfq")
        refreshed, refresh_audit, changed = refresh_missing_symbols(
            daily,
            universe,
            signal_date,
            fetch_one=fetch_one or client.fetch_daily,
        )
        if changed:
            write_table(refreshed, ProjectPaths(data_root).data_daily)
    if refreshed.empty:
        raise RuntimeError(f"No local market data is available for signal date {signal_date}")
    universe_symbols = set(universe["symbol"].astype(str))
    if not (_valid_close_symbols(refreshed, signal_date) & universe_symbols):
        raise RuntimeError(
            f"No ETF in the task universe has a valid close on signal date {signal_date}; "
            "refusing to fall back to an earlier date"
        )

    module = importlib.import_module(contract.strategy_module)
    module_path = Path(module.__file__).resolve()
    if module_path != contract.strategy_path.resolve():
        raise RuntimeError(
            "Imported strategy module does not resolve to the verified production source: "
            f"{module_path}"
        )
    params = module.EtfRerankTopKParams()
    replay_start = five_year_start(signal_date)
    input_daily = refreshed.copy()
    input_daily["date"] = pd.to_datetime(input_daily["date"])
    input_daily = input_daily[
        (input_daily["date"].dt.date >= replay_start)
        & (input_daily["date"].dt.date <= signal_date)
    ].copy()
    universe_audit, exclusions = build_universe_audit(
        input_daily,
        universe,
        signal_date,
        params,
        refresh_audit,
        calendar,
    )
    eligible_symbols = {
        str(item["symbol"])
        for item in universe_audit
        if "exclusion_reason" not in item
    }
    eligible_universe = universe[
        universe["symbol"].astype(str).isin(eligible_symbols)
    ].copy()
    selected = module.select(
        input_daily,
        eligible_universe,
        pd.Timestamp(replay_start),
        pd.Timestamp(signal_date),
    )
    output, holdings, cash_weight = build_output(selected, signal_date, execution_date)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    recommendation_path = (
        output_dir
        / f"recommendation_{signal_date.isoformat()}_active-etf-rotation.csv"
    )
    output.to_csv(recommendation_path, index=False)
    summary = {
        "strategy": contract.strategy_name,
        "requested_date": requested.isoformat(),
        "signal_date": signal_date.isoformat(),
        "holding_for": "next_trading_day",
        "execution_date": execution_date.isoformat() if execution_date else None,
        "parameters": asdict(params),
        "replay_start": replay_start.isoformat(),
        "replay_end": signal_date.isoformat(),
        "hashes": {
            "task_sha256": file_sha256(contract.task_path),
            "strategy_sha256": champion["production_strategy_sha256"],
            "champion_sha256": champion["champion_sha256"],
            "universe_sha256": file_sha256(contract.universe_path),
        },
        "champion_validation": champion,
        "task_contract": {
            "task_id": contract.task_id,
            "strategy_module": contract.strategy_module,
            "strategy_path": contract.strategy_relative_path,
            "universe_path": contract.universe_relative_path,
        },
        "holdings": holdings,
        "cash_weight": cash_weight,
        "portfolio": json_records(output),
        "dynamic_exclusions": exclusions,
        "refresh_audit": refresh_audit,
        "universe_audit": universe_audit,
        "recommendation_path": str(recommendation_path),
    }
    summary_path = output_dir / "recommendation_summary.json"
    write_json_atomic(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return summary


def main() -> int:
    try:
        run(parse_args())
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
