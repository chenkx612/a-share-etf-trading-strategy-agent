from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_core.strategy import etf_rerank_topk


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / ".agents"
    / "skills"
    / "active-etf-rerank-topk"
    / "scripts"
    / "recommend_next_holdings.py"
)


def load_script():
    module_name = "active_etf_rerank_topk_recommendation"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def sample_daily(
    dates: pd.DatetimeIndex,
    symbols: tuple[str, ...] = ("A", "B", "C"),
) -> pd.DataFrame:
    rows = []
    x = np.arange(len(dates), dtype=float)
    for index, symbol in enumerate(symbols):
        drift = 0.0010 - 0.00035 * index
        wave = 0.015 - 0.0025 * index
        closes = 100.0 * np.exp(
            drift * x + wave * np.sin(x / (9.0 + 2.0 * index) + index),
        )
        for signal_date, close in zip(dates, closes):
            rows.append({
                "date": signal_date,
                "symbol": symbol,
                "name": f"ETF {symbol}",
                "close": close,
            })
    return pd.DataFrame(rows)


def random_daily(
    dates: pd.DatetimeIndex,
    symbols: tuple[str, ...],
    *,
    seed: int,
) -> pd.DataFrame:
    returns = np.random.default_rng(seed).normal(0.0005, 0.012, (len(dates), len(symbols)))
    closes = 100.0 * np.cumprod(1.0 + returns, axis=0)
    return pd.DataFrame([
        {
            "date": signal_date,
            "symbol": symbol,
            "name": f"ETF {symbol}",
            "close": closes[date_index, symbol_index],
        }
        for date_index, signal_date in enumerate(dates)
        for symbol_index, symbol in enumerate(symbols)
    ])


def write_daily(root: Path, daily: pd.DataFrame) -> None:
    path = root / "data" / "etf_daily.csv"
    path.parent.mkdir(parents=True)
    daily.to_csv(path, index=False)


def args_for(data_root: Path, output_dir: Path, requested: str) -> argparse.Namespace:
    return argparse.Namespace(
        date=requested,
        data_root=str(data_root),
        output_dir=str(output_dir),
        skip_refresh=True,
    )


def make_contract(script, root: Path):
    strategy = root / "src" / "quant_core" / "strategy" / "etf_rerank_topk.py"
    universe = root / "universes" / "active_etf_rotation.csv"
    research = root / ".research" / script.TASK_ID
    strategy.parent.mkdir(parents=True)
    universe.parent.mkdir(parents=True)
    research.mkdir(parents=True)
    strategy.write_text("production\n", encoding="utf-8")
    universe.write_text("symbol,name\nA,ETF A\n", encoding="utf-8")
    (research / "champion.py").write_bytes(strategy.read_bytes())
    digest = hashlib.sha256(strategy.read_bytes()).hexdigest()
    (research / "champion.json").write_text(
        json.dumps({
            "task_id": script.TASK_ID,
            "strategy_path": "src/quant_core/strategy/etf_rerank_topk.py",
            "champion_sha256": digest,
            "champion_round_id": "001/001",
        }),
        encoding="utf-8",
    )
    task = root / "tasks" / "active_etf_rerank_topk.toml"
    task.parent.mkdir()
    task.write_text("id = 'active-etf-rerank-topk'\n", encoding="utf-8")
    return script.TaskContract(
        task_id=script.TASK_ID,
        task_path=task,
        strategy_name=script.TASK_ID,
        strategy_module="quant_core.strategy.etf_rerank_topk",
        strategy_path=strategy,
        strategy_relative_path="src/quant_core/strategy/etf_rerank_topk.py",
        universe_path=universe,
        universe_relative_path="universes/active_etf_rotation.csv",
        champion_json_path=research / "champion.json",
        champion_code_path=research / "champion.py",
    )


def test_task_contract_uses_only_configured_production_strategy_and_universe() -> None:
    script = load_script()
    contract = script.load_task_contract(REPO_ROOT)

    assert contract.task_id == script.TASK_ID
    assert contract.strategy_module == "quant_core.strategy.etf_rerank_topk"
    assert contract.strategy_path == REPO_ROOT / "src/quant_core/strategy/etf_rerank_topk.py"
    assert contract.universe_path == REPO_ROOT / "universes/active_etf_rotation.csv"


def test_cli_exposes_only_date_and_offline_isolation_controls() -> None:
    script = load_script()

    parsed = script.parse_args([
        "--date",
        "2026-07-24",
        "--skip-refresh",
        "--data-root",
        "/tmp/data",
        "--output-dir",
        "/tmp/output",
    ])

    assert vars(parsed) == {
        "date": "2026-07-24",
        "data_root": "/tmp/data",
        "output_dir": "/tmp/output",
        "skip_refresh": True,
    }


def test_cli_default_date_uses_shanghai_calendar_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = load_script()
    monkeypatch.setattr(script, "shanghai_today", lambda: date(2026, 7, 27))

    assert script.parse_args([]).date == "2026-07-27"


def test_champion_validation_accepts_exact_three_way_match(tmp_path: Path) -> None:
    script = load_script()
    contract = make_contract(script, tmp_path)

    audit = script.verify_champion(contract)

    assert audit["status"] == "passed"
    assert all(audit["checks"].values())
    assert audit["champion_sha256"] == audit["production_strategy_sha256"]


@pytest.mark.parametrize("mutation", ["metadata_path", "metadata_hash", "production", "missing"])
def test_champion_validation_fails_closed(tmp_path: Path, mutation: str) -> None:
    script = load_script()
    contract = make_contract(script, tmp_path)
    metadata = json.loads(contract.champion_json_path.read_text(encoding="utf-8"))
    if mutation == "metadata_path":
        metadata["strategy_path"] = "src/other.py"
        contract.champion_json_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif mutation == "metadata_hash":
        metadata["champion_sha256"] = "0" * 64
        contract.champion_json_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif mutation == "production":
        contract.strategy_path.write_text("unsynchronized\n", encoding="utf-8")
    else:
        contract.champion_code_path.unlink()

    with pytest.raises(RuntimeError, match="Synchronize Champion"):
        script.verify_champion(contract)


def test_date_resolution_rejects_intraday_and_resolves_holiday() -> None:
    script = load_script()
    friday = date(2026, 7, 24)
    monday_holiday = date(2026, 7, 27)
    tuesday = date(2026, 7, 28)

    assert script.resolve_signal_date(
        monday_holiday,
        now=datetime(2026, 7, 27, 18, tzinfo=script.SHANGHAI),
        trade_dates=[friday, tuesday],
    ) == friday
    with pytest.raises(RuntimeError, match="has not closed"):
        script.resolve_signal_date(
            tuesday,
            now=datetime(2026, 7, 28, 14, 59, tzinfo=script.SHANGHAI),
            trade_dates=[friday, tuesday],
        )


def test_refresh_replaces_successful_history_and_audits_failed_peer() -> None:
    script = load_script()
    target = date(2026, 7, 24)
    dates = pd.bdate_range("2026-01-01", target)
    universe = pd.DataFrame([
        {"symbol": "A", "name": "ETF A", "fund_size": 3.0},
        {"symbol": "B", "name": "ETF B", "fund_size": 2.0},
        {"symbol": "C", "name": "ETF C", "fund_size": 1.0},
    ])
    existing = sample_daily(dates[:-1])
    existing = pd.concat([
        existing,
        pd.DataFrame([{
            "date": pd.Timestamp(target),
            "symbol": "A",
            "name": "ETF A",
            "close": 123.0,
        }]),
    ], ignore_index=True)

    def fetch_one(one: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        symbol = one.iloc[0]["symbol"]
        assert start == date(2021, 7, 24)
        assert end == target
        if symbol == "C":
            raise ConnectionError("unavailable")
        refreshed_dates = pd.bdate_range("2021-07-26", target)
        return pd.DataFrame({
            "date": refreshed_dates,
            "symbol": symbol,
            "name": f"ETF {symbol}",
            "close": np.linspace(50.0, 100.0, len(refreshed_dates)),
        })

    refreshed, audit, changed = script.refresh_missing_symbols(
        existing,
        universe,
        target,
        fetch_one=fetch_one,
    )

    assert changed
    assert refreshed[refreshed["symbol"] == "B"]["date"].min() == pd.Timestamp("2021-07-26")
    assert audit == [
        {
            "symbol": "B",
            "status": "refreshed",
            "requested_start": "2021-07-24",
            "requested_end": "2026-07-24",
            "rows_replaced_with": len(pd.bdate_range("2021-07-26", target)),
        },
        {
            "symbol": "C",
            "status": "excluded",
            "requested_start": "2021-07-24",
            "requested_end": "2026-07-24",
            "reason": "refresh_failed",
            "error": "ConnectionError: unavailable",
        },
    ]


def test_dynamic_audit_excludes_invalid_close_and_insufficient_history() -> None:
    script = load_script()
    target = date(2026, 7, 24)
    dates = pd.bdate_range(end=target, periods=130)
    daily = sample_daily(dates)
    daily.loc[
        (daily["symbol"] == "B") & (daily["date"] == pd.Timestamp(target)),
        "close",
    ] = np.nan
    daily = daily[
        ~((daily["symbol"] == "C") & (daily["date"] < dates[-50]))
    ]
    universe = pd.DataFrame([
        {"symbol": "A", "name": "ETF A", "fund_size": 3.0},
        {"symbol": "B", "name": "ETF B", "fund_size": 2.0},
        {"symbol": "C", "name": "ETF C", "fund_size": 1.0},
    ])

    _, exclusions = script.build_universe_audit(
        daily,
        universe,
        target,
        etf_rerank_topk.EtfRerankTopKParams(),
        [],
        [value.date() for value in dates],
    )

    assert {item["symbol"]: item["reason"] for item in exclusions} == {
        "B": "missing_or_invalid_signal_date_close",
        "C": "insufficient_contiguous_history",
    }


def test_build_output_handles_overlap_empty_and_partial_weights() -> None:
    script = load_script()
    signal_date = date(2026, 7, 24)
    execution_date = date(2026, 7, 27)
    overlap = pd.DataFrame([{
        "date": pd.Timestamp(signal_date),
        "symbol": "A",
        "name": "ETF A",
        "score": 1.0,
        "rank": 1,
        "target_weight": 1.0,
    }])
    partial = overlap.copy()
    partial["target_weight"] = 0.3

    full_output, _, full_cash = script.build_output(overlap, signal_date, execution_date)
    empty_output, _, empty_cash = script.build_output(
        pd.DataFrame(columns=overlap.columns),
        signal_date,
        execution_date,
    )
    partial_output, _, partial_cash = script.build_output(
        partial,
        signal_date,
        execution_date,
    )

    assert full_cash == 0.0
    assert empty_cash == 1.0
    assert partial_cash == 0.7
    assert full_output["target_weight"].sum() == 1.0
    assert empty_output["target_weight"].sum() == 1.0
    assert partial_output["target_weight"].sum() == 1.0
    assert (partial_output["target_weight"] >= 0.0).all()


def test_offline_run_records_contract_hashes_dates_and_dynamic_exclusions(
    tmp_path: Path,
) -> None:
    script = load_script()
    dates = pd.bdate_range(end="2026-07-24", periods=180)
    universe = pd.read_csv(REPO_ROOT / "universes/active_etf_rotation.csv").head(3)
    daily = sample_daily(dates, tuple(universe["symbol"].astype(str)))
    write_daily(tmp_path, daily)
    output_dir = tmp_path / "outputs"
    run_args = args_for(tmp_path, output_dir, "2026-07-24")
    calendar = [value.date() for value in dates] + [date(2026, 7, 27)]

    summary = script.run(
        run_args,
        repo_root=REPO_ROOT,
        now=datetime(2026, 7, 24, 16, tzinfo=script.SHANGHAI),
        trade_dates=calendar,
    )

    assert summary["champion_validation"]["status"] == "passed"
    assert summary["signal_date"] == "2026-07-24"
    assert summary["replay_start"] == "2021-07-24"
    assert summary["execution_date"] == "2026-07-27"
    assert summary["hashes"]["strategy_sha256"] == summary["hashes"]["champion_sha256"]
    assert len(summary["dynamic_exclusions"]) == len(
        pd.read_csv(REPO_ROOT / "universes/active_etf_rotation.csv"),
    ) - 3


def test_production_replay_final_holdings_equal_direct_select(tmp_path: Path) -> None:
    script = load_script()
    universe = pd.read_csv(REPO_ROOT / "universes/active_etf_rotation.csv").head(3)
    symbols = tuple(universe["symbol"].astype(str))
    dates = pd.bdate_range(end="2026-07-24", periods=180)
    daily = random_daily(dates, symbols, seed=1)
    write_daily(tmp_path, daily)
    output_dir = tmp_path / "outputs"
    calendar = [value.date() for value in dates] + [date(2026, 7, 27)]

    summary = script.run(
        args_for(tmp_path, output_dir, "2026-07-24"),
        repo_root=REPO_ROOT,
        now=datetime(2026, 7, 24, 16, tzinfo=script.SHANGHAI),
        trade_dates=calendar,
    )
    canonical_universe = pd.read_csv(REPO_ROOT / "universes/active_etf_rotation.csv")
    expected = etf_rerank_topk.select(
        daily,
        canonical_universe,
        pd.Timestamp("2021-07-24"),
        pd.Timestamp("2026-07-24"),
    )
    expected = expected[pd.to_datetime(expected["date"]) == pd.Timestamp("2026-07-24")]
    without_replay = etf_rerank_topk.select(
        daily,
        canonical_universe,
        pd.Timestamp("2026-07-24"),
        pd.Timestamp("2026-07-24"),
    )
    actual = pd.read_csv(summary["recommendation_path"])
    actual = actual[actual["record_type"] == "holding"]

    assert without_replay["symbol"].astype(str).tolist() != expected["symbol"].astype(str).tolist()
    assert actual["symbol"].astype(str).tolist() == expected["symbol"].astype(str).tolist()
    assert actual["target_weight"].tolist() == pytest.approx(
        expected["target_weight"].tolist(),
    )


def test_no_valid_close_for_signal_date_fails_without_date_fallback(
    tmp_path: Path,
) -> None:
    script = load_script()
    dates = pd.bdate_range(end="2026-07-23", periods=130)
    universe = pd.read_csv(REPO_ROOT / "universes/active_etf_rotation.csv").head(3)
    daily = sample_daily(dates, tuple(universe["symbol"].astype(str)))
    write_daily(tmp_path, daily)

    with pytest.raises(RuntimeError, match="refusing to fall back"):
        script.run(
            args_for(tmp_path, tmp_path / "outputs", "2026-07-24"),
            repo_root=REPO_ROOT,
            now=datetime(2026, 7, 24, 16, tzinfo=script.SHANGHAI),
            trade_dates=[value.date() for value in dates] + [date(2026, 7, 24)],
        )


def test_cache_wide_missing_trading_session_excludes_all_affected_etfs(
    tmp_path: Path,
) -> None:
    script = load_script()
    universe = pd.read_csv(REPO_ROOT / "universes/active_etf_rotation.csv").head(3)
    symbols = tuple(universe["symbol"].astype(str))
    dates = pd.bdate_range(end="2026-07-24", periods=130)
    missing_session = dates[-20]
    daily = sample_daily(dates, symbols)
    daily = daily[daily["date"] != missing_session]
    write_daily(tmp_path, daily)

    summary = script.run(
        args_for(tmp_path, tmp_path / "outputs", "2026-07-24"),
        repo_root=REPO_ROOT,
        now=datetime(2026, 7, 24, 16, tzinfo=script.SHANGHAI),
        trade_dates=[value.date() for value in dates] + [date(2026, 7, 27)],
    )

    exclusions = {
        item["symbol"]: item["reason"]
        for item in summary["dynamic_exclusions"]
    }
    assert all(
        exclusions[symbol] == "insufficient_contiguous_history"
        for symbol in symbols
    )
    assert summary["holdings"] == []
    assert summary["cash_weight"] == 1.0
