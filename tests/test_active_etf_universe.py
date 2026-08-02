from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import pytest

from quant_core.universe import active as active_builder


def load_builder():
    return active_builder


def make_args(output_dir: Path, *, apply: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        date="2026-06-12",
        min_fund_size=10.0,
        shortlist_size=100,
        liquidity_lookback_days=60,
        min_amount_observations=50,
        min_median_amount=50_000_000,
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
    amount: float,
) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-06-12", periods=len(returns) + 1)
    closes = [100.0]
    for value in returns:
        closes.append(closes[-1] * (1.0 + value))
    return pd.DataFrame(
        {
            "date": dates,
            "symbol": symbol,
            "name": name,
            "close": closes,
            "amount": amount,
        },
    )


def test_shortlist_applies_size_floor_before_top_n_without_name_deduplication() -> None:
    builder = load_builder()
    spot = pd.DataFrame(
        [
            {"代码": "A", "名称": "主题ETF甲", "总市值": 30.0},
            {"代码": "B", "名称": "主题ETF乙", "总市值": 20.0},
            {"代码": "C", "名称": "其他ETF", "总市值": 10.0},
        ],
    )

    shortlist, audit = builder.make_shortlist(
        spot,
        min_fund_size=15.0,
        shortlist_size=1,
    )

    assert shortlist["symbol"].tolist() == ["A"]
    assert audit["size_preselection"]["outside_top_n_count"] == 1
    assert audit["size_filter"]["below_minimum_count"] == 1
    assert audit["size_filter"]["excluded"][0]["symbol"] == "C"
    assert audit["size_filter"]["excluded"][0]["reason"] == "fund_size_below_minimum"


def test_liquidity_filter_rejects_low_median_amount() -> None:
    builder = load_builder()
    alternating = [0.01 if index % 2 == 0 else -0.008 for index in range(130)]
    shortlist = pd.DataFrame(
        [
            {"size_rank": 1, "symbol": "A", "name": "活跃ETF", "fund_size": 30.0},
            {"size_rank": 2, "symbol": "B", "name": "冷清ETF", "fund_size": 20.0},
        ],
    )
    daily = pd.concat(
        [
            daily_from_returns("A", "活跃ETF", alternating, amount=100_000_000),
            daily_from_returns("B", "冷清ETF", alternating, amount=10_000_000),
        ],
        ignore_index=True,
    )
    returns, observations, liquidity = builder.market_metrics(
        daily,
        shortlist,
        trade_date="2026-06-12",
        lookback_days=252,
        liquidity_lookback_days=60,
    )

    eligible, metrics, audit = builder.filter_candidates(
        shortlist,
        observations,
        liquidity,
        min_observations=120,
        min_amount_observations=50,
        min_median_amount=50_000_000,
    )

    assert not returns.empty
    assert eligible["symbol"].tolist() == ["A"]
    excluded = metrics.set_index("symbol").loc["B", "exclusion_reasons"]
    assert excluded == "median_amount_below_minimum"
    assert audit["liquidity_filter"]["excluded_count"] == 1


def test_liquidity_uses_volume_proxy_when_reported_amount_is_missing() -> None:
    builder = load_builder()
    returns = [0.01 if index % 2 == 0 else -0.008 for index in range(130)]
    shortlist = pd.DataFrame(
        [{"size_rank": 1, "symbol": "A", "name": "腾讯回退ETF", "fund_size": 30.0}],
    )
    daily = daily_from_returns("A", "腾讯回退ETF", returns, amount=float("nan"))
    daily["volume"] = 2_000_000

    _, _, liquidity = builder.market_metrics(
        daily,
        shortlist,
        trade_date="2026-06-12",
        lookback_days=252,
        liquidity_lookback_days=60,
    )

    row = liquidity.iloc[0]
    assert row.amount_observations == 60
    assert row.amount_proxy_observations == 60
    assert row.amount_median > 50_000_000


def test_greedy_correlation_selection_keeps_most_active_member() -> None:
    builder = load_builder()
    alternating = [0.01 if index % 2 == 0 else -0.008 for index in range(130)]
    same = [value * 0.99 for value in alternating]
    negative = [-value for value in alternating]
    shortlist = pd.DataFrame(
        [
            {"size_rank": 1, "symbol": "A", "name": "较大ETF", "fund_size": 30.0},
            {"size_rank": 2, "symbol": "B", "name": "最活跃ETF", "fund_size": 20.0},
            {"size_rank": 3, "symbol": "C", "name": "负相关ETF", "fund_size": 10.0},
        ],
    )
    daily = pd.concat(
        [
            daily_from_returns("A", "较大ETF", alternating, amount=80_000_000),
            daily_from_returns("B", "最活跃ETF", same, amount=120_000_000),
            daily_from_returns("C", "负相关ETF", negative, amount=70_000_000),
        ],
        ignore_index=True,
    )
    returns, observations, liquidity = builder.market_metrics(
        daily,
        shortlist,
        trade_date="2026-06-12",
        lookback_days=252,
        liquidity_lookback_days=60,
    )
    eligible, _, _ = builder.filter_candidates(
        shortlist,
        observations,
        liquidity,
        min_observations=120,
        min_amount_observations=50,
        min_median_amount=50_000_000,
    )

    selected, _, pair_observations, decisions, audit = builder.correlation_select(
        eligible,
        returns,
        min_observations=120,
        corr_threshold=0.90,
    )

    assert selected["symbol"].tolist() == ["B", "C"]
    decisions = decisions.set_index("symbol")
    assert decisions.loc["A", "exclusion_reason"] == "correlation_above_threshold"
    assert decisions.loc["A", "blocking_symbol"] == "B"
    assert pair_observations.set_index("symbol").loc["A", "B"] == 130
    assert audit["method"] == "liquidity_ordered_greedy_pairwise"


def test_greedy_selection_rejects_candidate_with_insufficient_pair_history() -> None:
    builder = load_builder()
    eligible = pd.DataFrame(
        [
            {
                "size_rank": 1,
                "symbol": "A",
                "name": "先选ETF",
                "fund_size": 30.0,
                "amount_observations": 60,
                "amount_proxy_observations": 0,
                "amount_median": 100_000_000.0,
                "return_observations": 120,
                "eligible": True,
                "exclusion_reasons": "",
            },
            {
                "size_rank": 2,
                "symbol": "B",
                "name": "错位历史ETF",
                "fund_size": 20.0,
                "amount_observations": 60,
                "amount_proxy_observations": 0,
                "amount_median": 80_000_000.0,
                "return_observations": 120,
                "eligible": True,
                "exclusion_reasons": "",
            },
        ],
    )
    returns = pd.DataFrame(
        {
            "A": [0.01] * 120 + [float("nan")] * 120,
            "B": [float("nan")] * 120 + [0.01] * 120,
        },
    )

    selected, _, _, decisions, audit = builder.correlation_select(
        eligible,
        returns,
        min_observations=120,
        corr_threshold=0.90,
    )

    assert selected["symbol"].tolist() == ["A"]
    rejected = decisions.set_index("symbol").loc["B"]
    assert rejected.exclusion_reason == "insufficient_pair_history"
    assert rejected.blocking_symbol == "A"
    assert rejected.pair_observations == 0
    assert audit["excluded"][0]["reason"] == "insufficient_pair_history"


def test_greedy_selection_allows_threshold_equality_and_negative_correlation() -> None:
    builder = load_builder()
    x = pd.Series([float(index) for index in range(-65, 66)])
    centered = x - x.mean()
    alternating = pd.Series([1.0 if index % 2 == 0 else -1.0 for index in range(131)])
    orthogonal = alternating - alternating.mean()
    orthogonal -= centered * (centered @ orthogonal) / (centered @ centered)
    orthogonal *= (centered.std() / orthogonal.std())
    at_threshold = 0.9 * centered + (1.0 - 0.9**2) ** 0.5 * orthogonal
    eligible = pd.DataFrame(
        [
            {"symbol": "A", "name": "基准ETF", "fund_size": 30.0, "amount_median": 3.0},
            {"symbol": "B", "name": "阈值ETF", "fund_size": 20.0, "amount_median": 2.0},
            {"symbol": "C", "name": "负相关ETF", "fund_size": 10.0, "amount_median": 1.0},
        ],
    )
    returns = pd.DataFrame({"A": centered, "B": at_threshold, "C": -centered})
    threshold = float(returns.corr().loc["A", "B"])

    selected, correlations, _, _, _ = builder.correlation_select(
        eligible,
        returns,
        min_observations=120,
        corr_threshold=threshold,
    )

    matrix = correlations.set_index("symbol")
    assert matrix.loc["A", "B"] == pytest.approx(0.90)
    assert selected["symbol"].tolist() == ["A", "B", "C"]


def test_preview_and_apply_touch_only_active_universe(tmp_path: Path) -> None:
    builder = load_builder()
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / "clusters.csv").write_text("legacy\n", encoding="utf-8")
    destination = tmp_path / "active_etf_rotation.csv"
    other_pool = tmp_path / "liquid_etf_rotation.csv"
    before = pd.DataFrame([{"symbol": "OLD", "name": "旧池", "fund_size": 1.0}])
    before.to_csv(destination, index=False)
    other_pool.write_text("symbol,name\nKEEP,其他池\n", encoding="utf-8")
    spot = pd.DataFrame([{"代码": "A", "名称": "活跃ETF", "总市值": 30.0}])
    alternating = [0.01 if index % 2 == 0 else -0.008 for index in range(130)]
    daily = daily_from_returns("A", "活跃ETF", alternating, amount=100_000_000)

    preview = builder.build_outputs(
        make_args(output_dir),
        spot=spot,
        daily=daily,
        destination=destination,
    )

    assert preview["selected_symbols"] == ["A"]
    pd.testing.assert_frame_equal(pd.read_csv(destination), before)
    assert not (output_dir / "clusters.csv").exists()
    assert (output_dir / "selection.csv").exists()
    assert (output_dir / "pair_observations.csv").exists()

    applied = builder.build_outputs(
        make_args(output_dir, apply=True),
        spot=spot,
        daily=daily,
        destination=destination,
    )

    assert applied["applied"] is True
    assert pd.read_csv(destination)["symbol"].tolist() == ["A"]
    assert pd.read_csv(output_dir / "universe_before.csv")["symbol"].tolist() == ["OLD"]
    assert other_pool.read_text(encoding="utf-8") == "symbol,name\nKEEP,其他池\n"
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["correlation_filter"]["method"] == "liquidity_ordered_greedy_pairwise"


def test_refresh_failure_blocks_apply_and_keeps_audit(tmp_path: Path) -> None:
    builder = load_builder()
    output_dir = tmp_path / "outputs"
    destination = tmp_path / "active_etf_rotation.csv"
    before = pd.DataFrame([{"symbol": "OLD", "name": "旧池", "fund_size": 1.0}])
    before.to_csv(destination, index=False)
    spot = pd.DataFrame([{"代码": "A", "名称": "活跃ETF", "总市值": 30.0}])
    alternating = [0.01 if index % 2 == 0 else -0.008 for index in range(130)]
    daily = daily_from_returns("A", "活跃ETF", alternating, amount=100_000_000)

    with pytest.raises(RuntimeError, match="market_data_refresh_failed"):
        builder.build_outputs(
            make_args(output_dir, apply=True),
            spot=spot,
            daily=daily,
            refresh_audit={"refresh_failures": ["A"]},
            destination=destination,
        )

    pd.testing.assert_frame_equal(pd.read_csv(destination), before)
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["applied"] is False
    assert summary["apply_blockers"][0]["symbols"] == ["A"]
