from __future__ import annotations

import pandas as pd

from quant_agent.backtest.engine import run_backtest
from quant_agent.config import StrategyConfig
from quant_agent.factors import compute_factors
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
