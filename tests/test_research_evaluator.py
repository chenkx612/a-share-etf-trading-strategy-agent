from __future__ import annotations

import pandas as pd
import pytest

from quant_core.research.evaluator import evaluate_candidate, validate_selection


def _daily() -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=4, freq="D")
    return pd.DataFrame([
        {"date": date, "symbol": symbol, "open": 10.0 + offset}
        for offset, date in enumerate(dates)
        for symbol in ("A", "B")
    ])


def test_evaluator_runs_candidate_through_fixed_backtest() -> None:
    def select(
        daily: pd.DataFrame,
        universe: pd.DataFrame,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        assert daily["date"].max() == end
        return pd.DataFrame({
            "date": [start],
            "symbol": [str(universe.iloc[0]["symbol"])],
            "target_weight": [1.0],
        })

    selected, result = evaluate_candidate(
        _daily(),
        pd.DataFrame({"symbol": ["A", "B"]}),
        pd.Timestamp("2025-01-01"),
        pd.Timestamp("2025-01-04"),
        select,
        fee_rate=0.001,
    )

    assert selected["symbol"].tolist() == ["A"]
    assert "sortino" in result.metrics


def test_evaluator_rejects_invalid_candidate_weights() -> None:
    selected = pd.DataFrame({
        "date": ["2025-01-01", "2025-01-01"],
        "symbol": ["A", "B"],
        "target_weight": [0.8, 0.8],
    })

    with pytest.raises(ValueError, match="sum to at most one"):
        validate_selection(
            selected,
            _daily(),
            {"A", "B"},
            pd.Timestamp("2025-01-01"),
            pd.Timestamp("2025-01-04"),
        )
