from __future__ import annotations

import pandas as pd
import pytest

from quant_core.config import BacktestConfig
from quant_core.research.evaluator import evaluate_candidate
from quant_core.strategy.sharpe_corr_threshold import SharpeCorrThresholdParams, select


def _daily() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=130, freq="B")
    rows = []
    for index, symbol in enumerate(["510300", "510500", "159915"]):
        for offset, day in enumerate(dates):
            price = 1.0 + index * 0.2 + offset * (0.002 + index * 0.0005)
            rows.append({
                "date": day,
                "symbol": symbol,
                "name": f"ETF{symbol}",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1_000,
                "amount": price * 1_000,
                "turnover": 1.0,
            })
    return pd.DataFrame(rows)


def test_research_baseline_uses_complete_strategy_defaults() -> None:
    params = SharpeCorrThresholdParams()

    assert params.top_n == 5
    assert params.sharpe_window == 25
    assert params.factor_lower_bound == pytest.approx(0.0)
    assert params.corr_window == 100
    assert params.corr_threshold == pytest.approx(0.9)
    assert params.stop_loss_pct == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top_n", 0),
        ("sharpe_window", 0),
        ("corr_window", 0),
        ("factor_lower_bound", float("nan")),
        ("corr_threshold", 1.1),
        ("stop_loss_pct", -0.1),
    ],
)
def test_research_baseline_rejects_invalid_strategy_params(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        SharpeCorrThresholdParams(**{field: value})


def test_research_uses_fixed_backtest_fee() -> None:
    config = BacktestConfig()

    assert config.fee_rate == pytest.approx(0.001)
    assert config.initial_capital == pytest.approx(100_000.0)


def test_research_evaluator_runs_sharpe_corr_threshold_baseline() -> None:
    daily = _daily()
    universe = pd.DataFrame({
        "symbol": ["510300", "510500", "159915"],
        "name": ["ETF510300", "ETF510500", "ETF159915"],
    })
    start = pd.Timestamp("2024-05-01")
    end = daily["date"].max()

    selected, result = evaluate_candidate(
        daily,
        universe,
        start,
        end,
        select,
    )

    assert not selected.empty
    assert {"date", "symbol", "target_weight"}.issubset(selected.columns)
    assert selected["date"].between(start, end).all()
    assert selected.groupby("date")["target_weight"].sum().le(1.0).all()
    assert result.metrics
