from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "build_liquid_etf_universe.py"
)


def load_builder():
    module_name = "build_liquid_etf_universe"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def make_args(output_dir: Path, *, apply: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        date="2026-06-12",
        min_fund_size=10_000_000_000,
        shortlist_size=100,
        lookback_days=252,
        min_observations=120,
        corr_threshold=0.90,
        output_dir=str(output_dir),
        apply=apply,
    )


def daily_from_returns(
    symbol: str,
    name: str,
    returns: list[float],
    *,
    start: str = "2025-12-01",
) -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=len(returns) + 1)
    closes = [100.0]
    for value in returns:
        closes.append(closes[-1] * (1.0 + value))
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": symbol,
            "name": name,
            "close": closes,
        }
    )


def standard_daily(
    symbol: str,
    name: str,
    *,
    periods: int,
    end: str = "2026-06-12",
) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=periods)
    closes = [100.0 + index for index in range(periods)]
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": symbol,
            "name": name,
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": 1.0,
            "amount": 1.0,
            "turnover": 1.0,
        }
    )


def test_normalized_etf_group_key_removes_spaces_and_ignores_etf_case() -> None:
    builder = load_builder()

    assert builder.normalized_etf_group_key(" 通 信 eTf 华夏 ") == "通信"
    assert builder.normalized_etf_group_key("Gold EtF Sponsor") == "gold"
    assert builder.normalized_etf_group_key("创业板") == "创业板"


def test_default_shortlist_size_bounds_refresh_workload_at_one_hundred() -> None:
    builder = load_builder()

    assert builder.DEFAULT_SHORTLIST_SIZE == 100


def test_shortlist_filters_size_deduplicates_by_largest_and_uses_symbol_tie_break() -> None:
    builder = load_builder()
    spot = pd.DataFrame(
        [
            {"代码": "000003", "名称": "主题CETF", "总市值": 13_000_000_000, "涨跌幅": 99.0},
            {"代码": "000004", "名称": "主题DETF", "总市值": 11_000_000_000, "涨跌幅": -9.0},
            {"代码": "000002", "名称": "主题A ETF 乙", "总市值": 12_000_000_000, "涨跌幅": 8.0},
            {"代码": "000001", "名称": "主题Aetf甲", "总市值": 12_000_000_000, "涨跌幅": -8.0},
            {"代码": "000005", "名称": "主题EETF", "总市值": 9_000_000_000, "涨跌幅": 100.0},
        ]
    )

    shortlist, audit = builder.make_shortlist(
        spot,
        min_fund_size=10_000_000_000,
        shortlist_size=2,
    )

    assert shortlist["symbol"].tolist() == ["000003", "000001"]
    assert shortlist["fund_size"].tolist() == [13_000_000_000, 12_000_000_000]
    assert "return_pct" not in shortlist.columns
    assert audit["size_filter"]["excluded"][0]["symbol"] == "000005"
    duplicate = audit["name_grouping"]["excluded"][0]
    assert duplicate["symbol"] == "000002"
    assert duplicate["kept_symbol"] == "000001"


def test_correlation_filter_keeps_larger_and_does_not_drop_negative_correlation() -> None:
    builder = load_builder()
    alternating = [0.01 if index % 2 == 0 else -0.008 for index in range(130)]
    same = [value * 0.99 for value in alternating]
    negative = [-value for value in alternating]
    daily = pd.concat(
        [
            daily_from_returns("A", "大ETF", alternating),
            daily_from_returns("B", "小ETF", same),
            daily_from_returns("C", "负相关ETF", negative),
        ],
        ignore_index=True,
    )
    shortlist = pd.DataFrame(
        [
            {"size_rank": 1, "symbol": "A", "name": "大ETF", "fund_size": 30.0, "group_key": "大"},
            {"size_rank": 2, "symbol": "B", "name": "小ETF", "fund_size": 20.0, "group_key": "小"},
            {"size_rank": 3, "symbol": "C", "name": "负相关ETF", "fund_size": 10.0, "group_key": "负相关"},
        ]
    )
    returns, observations = builder.return_matrix(
        daily,
        shortlist,
        trade_date="2026-12-31",
        lookback_days=252,
    )

    selected, correlations, audit = builder.correlation_select(
        shortlist,
        returns,
        observations,
        min_observations=120,
        corr_threshold=0.90,
    )

    assert selected["symbol"].tolist() == ["A", "C"]
    assert audit["correlation_filter"]["excluded"][0]["symbol"] == "B"
    assert audit["correlation_filter"]["excluded"][0]["kept_symbol"] == "A"
    correlation_by_symbol = correlations.set_index("symbol")
    assert correlation_by_symbol.loc["A", "C"] == pytest.approx(-1.0)


def test_insufficient_history_is_audited_and_preview_does_not_modify_universe(
    tmp_path: Path,
) -> None:
    builder = load_builder()
    output_dir = tmp_path / "outputs"
    destination = tmp_path / "liquid_etf_rotation.csv"
    before = pd.DataFrame([{"symbol": "OLD", "name": "旧池", "fund_size": 1.0}])
    before.to_csv(destination, index=False)
    spot = pd.DataFrame(
        [
            {"代码": "A", "名称": "大ETF", "总市值": 30_000_000_000},
            {"代码": "B", "名称": "新ETF", "总市值": 20_000_000_000},
        ]
    )
    enough = [0.001 if index % 2 == 0 else -0.001 for index in range(130)]
    insufficient = [0.002 for _ in range(20)]
    daily = pd.concat(
        [
            daily_from_returns("A", "大ETF", enough),
            daily_from_returns("B", "新ETF", insufficient),
        ],
        ignore_index=True,
    )

    summary = builder.build_outputs(
        make_args(output_dir),
        spot=spot,
        daily=daily,
        destination=destination,
    )

    assert summary["selected_symbols"] == ["A"]
    assert summary["history_filter"]["excluded"][0] == {
        "symbol": "B",
        "name": "新ETF",
        "observations": 20,
        "minimum_observations": 120,
        "reason": "insufficient_return_history",
    }
    pd.testing.assert_frame_equal(pd.read_csv(destination), before)
    assert not (output_dir / "universe_before.csv").exists()
    assert (output_dir / "shortlist.csv").exists()
    assert (output_dir / "selected_universe.csv").exists()
    assert (output_dir / "correlation.csv").exists()
    persisted = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert persisted["historical_return_ranking_used"] is False


def test_refreshes_current_cache_when_return_history_is_too_shallow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = load_builder()
    shortlist = pd.DataFrame(
        [{"symbol": "A", "name": "大ETF", "fund_size": 30_000_000_000}]
    )
    state = {"daily": standard_daily("A", "大ETF", periods=20)}
    incoming = standard_daily("A", "大ETF", periods=140)
    fetched_symbols: list[str] = []

    class DummyClient:
        def __init__(self, adjust: str) -> None:
            assert adjust == "qfq"

        def fetch_daily(
            self,
            universe: pd.DataFrame,
            start: date,
            end: date,
        ) -> pd.DataFrame:
            fetched_symbols.extend(universe["symbol"].astype(str))
            return incoming.copy()

    monkeypatch.setattr(builder, "AkshareMarketDataClient", DummyClient)
    monkeypatch.setattr(
        builder,
        "latest_trade_date_on_or_before",
        lambda value: date(2026, 6, 12),
    )
    monkeypatch.setattr(builder, "read_daily", lambda paths: state["daily"].copy())

    def fake_write_table(frame: pd.DataFrame, path: Path) -> Path:
        state["daily"] = frame.copy()
        return path.with_suffix(".parquet")

    monkeypatch.setattr(builder, "write_table", fake_write_table)

    refreshed, audit = builder.refresh_shared_daily(
        shortlist,
        trade_date="2026-06-12",
        lookback_days=252,
        min_observations=120,
        root=tmp_path,
        log=lambda message: None,
    )

    assert audit["latest_date_stale_symbols"] == []
    assert audit["insufficient_cache_symbols"] == ["A"]
    assert audit["cached_observations_by_symbol"] == {"A": 19}
    assert audit["refresh_failures"] == []
    assert fetched_symbols == ["A"]
    assert len(refreshed) == 140


def test_refresh_failure_blocks_apply_but_keeps_preview_and_audit(
    tmp_path: Path,
) -> None:
    builder = load_builder()
    output_dir = tmp_path / "outputs"
    destination = tmp_path / "liquid_etf_rotation.csv"
    before = pd.DataFrame([{"symbol": "OLD", "name": "旧池", "fund_size": 1.0}])
    before.to_csv(destination, index=False)
    spot = pd.DataFrame(
        [
            {"代码": "A", "名称": "大ETF", "总市值": 30_000_000_000},
            {"代码": "B", "名称": "新ETF", "总市值": 20_000_000_000},
        ]
    )
    enough = [0.001 if index % 2 == 0 else -0.001 for index in range(130)]
    daily = daily_from_returns("A", "大ETF", enough)
    refresh_audit = {
        "requested_symbols": ["A", "B"],
        "refreshed_symbols": ["A"],
        "refresh_failures": ["B"],
    }

    with pytest.raises(RuntimeError, match="market_data_refresh_failed"):
        builder.build_outputs(
            make_args(output_dir, apply=True),
            spot=spot,
            daily=daily,
            refresh_audit=refresh_audit,
            destination=destination,
        )

    pd.testing.assert_frame_equal(pd.read_csv(destination), before)
    assert not (output_dir / "universe_before.csv").exists()
    assert (output_dir / "selected_universe.csv").exists()
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["apply_requested"] is True
    assert summary["applied"] is False
    assert summary["apply_blockers"] == [
        {
            "reason": "market_data_refresh_failed",
            "symbols": ["B"],
        }
    ]


def test_apply_backs_up_and_atomically_replaces_only_liquid_universe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = load_builder()
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    destination = tmp_path / "liquid_etf_rotation.csv"
    sector_rotation = tmp_path / "sector_rotation.csv"
    before = pd.DataFrame([{"symbol": "OLD", "name": "旧池", "fund_size": 1.0}])
    after = pd.DataFrame([{"symbol": "NEW", "name": "新池", "fund_size": 2.0}])
    before.to_csv(destination, index=False)
    sector_rotation.write_text("symbol,name\nKEEP,原池\n", encoding="utf-8")
    replacements: list[tuple[Path, Path]] = []
    real_replace = builder.os.replace

    def recording_replace(source: Path, target: Path) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(builder.os, "replace", recording_replace)
    backup = builder.apply_universe(
        after,
        apply=True,
        output_dir=output_dir,
        destination=destination,
    )

    assert backup == output_dir / "universe_before.csv"
    pd.testing.assert_frame_equal(pd.read_csv(backup), before)
    pd.testing.assert_frame_equal(pd.read_csv(destination), after)
    assert sector_rotation.read_text(encoding="utf-8") == "symbol,name\nKEEP,原池\n"
    assert replacements and replacements[0][1] == destination
    assert replacements[0][0].parent == destination.parent
