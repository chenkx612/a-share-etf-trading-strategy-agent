from __future__ import annotations

import pandas as pd

from quant_core.research.evaluator import evaluate_candidate
from quant_core.strategy.sharpe_corr_threshold import select


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
