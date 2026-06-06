from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from quant_agent.backtest.engine import factor_ic, run_backtest
from quant_agent.config import StrategyConfig
from quant_agent.factors import compute_factors
from quant_agent.strategy.sector_rotation import select_sector_sharpe
from quant_agent.strategy.selection import score_and_select


def sample_daily() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=45, freq="D")
    rows = []
    for i, symbol in enumerate(["510300", "510500", "159915"]):
        for j, day in enumerate(dates):
            price = 1.0 + i * 0.2 + j * (0.01 + i * 0.002)
            rows.append({
                "date": day,
                "symbol": symbol,
                "name": f"ETF{symbol}",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1000 + j,
                "amount": (1000 + j) * price,
                "turnover": 1.0 + i,
            })
    return pd.DataFrame(rows)


def test_factor_selection_backtest_loop() -> None:
    daily = sample_daily()
    factors = compute_factors(daily)
    assert "sharpe_20" in factors.columns
    assert "sharpe_25" in factors.columns
    selected = score_and_select(
        factors,
        StrategyConfig(top_n=2),
        start=pd.Timestamp("2024-01-25"),
        end=pd.Timestamp("2024-02-10"),
    )
    assert not selected.empty
    assert selected.groupby("date")["symbol"].count().max() == 2
    assert selected.groupby("date")["target_weight"].sum().round(6).eq(1.0).all()

    result = run_backtest(daily, selected)
    assert not result.daily_returns.empty
    assert "total_return" in result.metrics
    assert result.positions["weight"].gt(0).all()


def test_sharpe_single_factor_config_selects_by_sharpe() -> None:
    daily = sample_daily()
    factors = compute_factors(daily)
    selected = score_and_select(
        factors,
        StrategyConfig.sharpe_single_factor(top_n=1),
        start=pd.Timestamp("2024-01-25"),
        end=pd.Timestamp("2024-02-10"),
    )
    assert not selected.empty
    assert selected.groupby("date")["symbol"].count().eq(1).all()
    assert selected["score"].notna().all()


def test_strategy_selection_can_filter_by_universe() -> None:
    daily = sample_daily()
    factors = compute_factors(daily)
    selected = score_and_select(
        factors,
        StrategyConfig(top_n=2),
        start=pd.Timestamp("2024-01-25"),
        end=pd.Timestamp("2024-02-10"),
        universe_symbols={"510300"},
    )
    assert not selected.empty
    assert set(selected["symbol"]) == {"510300"}
    assert selected.groupby("date")["target_weight"].sum().round(6).eq(1.0).all()


def test_sector_sharpe_rotation_runs_in_framework_backtest() -> None:
    daily = sample_daily()
    factors = compute_factors(daily)
    config = replace(StrategyConfig.sector_sharpe_rotation(), top_n=2)
    selected = select_sector_sharpe(
        factors,
        config,
        start=pd.Timestamp("2024-01-25"),
        end=pd.Timestamp("2024-02-10"),
        universe_symbols={"510300", "510500", "159915"},
        corr_window=10,
    )
    assert not selected.empty
    assert {"name", "score", "target_weight"}.issubset(selected.columns)

    result = run_backtest(daily, selected, fee_rate=0.0003)
    assert not result.equity_curve.empty
    assert "total_return" in result.metrics
    assert result.positions["weight"].gt(0).all()


def test_backtest_trades_at_next_open_and_uses_open_to_open_returns() -> None:
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    daily = pd.DataFrame({
        "date": dates,
        "symbol": ["510300"] * 3,
        "name": ["ETF510300"] * 3,
        "open": [100.0, 110.0, 121.0],
        "high": [1000.0, 1000.0, 1000.0],
        "low": [1.0, 1.0, 1.0],
        "close": [1000.0, 100.0, 1000.0],
        "volume": [1000, 1000, 1000],
        "amount": [100000.0, 110000.0, 121000.0],
        "turnover": [1.0, 1.0, 1.0],
    })
    selected = pd.DataFrame({
        "date": [dates[0]],
        "symbol": ["510300"],
        "target_weight": [1.0],
    })

    result = run_backtest(daily, selected, fee_rate=0.0)

    returns = result.daily_returns.set_index("date")["gross_return"]
    assert returns.loc[dates[0]] == 0.0
    assert returns.loc[dates[1]] == pytest.approx(0.099)
    assert returns.loc[dates[2]] == 0.0
    positions = result.positions.set_index("date")
    assert positions.loc[dates[1], "shares"] == 9000
    assert positions["shares"].mod(100).eq(0).all()


def test_factor_ic_uses_tradeable_next_open_forward_return() -> None:
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    factors = pd.DataFrame({
        "date": [dates[0], dates[1], dates[2], dates[0], dates[1], dates[2]],
        "symbol": ["510300", "510300", "510300", "510500", "510500", "510500"],
        "score": [2.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        "open": [10.0, 10.0, 20.0, 10.0, 10.0, 5.0],
        "close": [10.0, 1.0, 1.0, 10.0, 100.0, 100.0],
    })

    result = factor_ic(factors)

    assert result["ic"] == pytest.approx(1.0)
    assert result["rank_ic"] == pytest.approx(1.0)
