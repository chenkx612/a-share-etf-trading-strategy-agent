from __future__ import annotations

import pandas as pd
import pytest

from quant_core.research.evaluator import evaluate_candidate, evaluate_walk_forward, validate_selection


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
    )

    assert selected["symbol"].tolist() == ["A"]
    assert "sortino" in result.metrics
    assert result.positions["shares"].max() == 9_000


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


def test_walk_forward_selects_parameters_and_records_oos_folds() -> None:
    dates = pd.date_range("2024-01-01", "2024-05-31", freq="D")
    daily = pd.DataFrame([
        {"date": day, "symbol": symbol, "open": 10.0 + index + (0 if symbol == "A" else 1)}
        for index, day in enumerate(dates)
        for symbol in ("A", "B")
    ])

    def selector(daily, universe, start, end, params):
        symbol = "A" if params["symbol"] == "A" else "B"
        return pd.DataFrame({"date": pd.date_range(start, end, freq="D"), "symbol": symbol, "target_weight": 1.0})

    selected, result, folds = evaluate_walk_forward(
        daily, pd.DataFrame({"symbol": ["A", "B"]}),
        {"start": "2024-04-01", "end": "2024-05-31"},
        {"train_months": 3, "validation_months": 1, "max_parameter_sets": 2},
        {"max_drawdown": {"operator": "abs<=", "threshold": 1.0}}, "sortino",
        [{"symbol": "A"}, {"symbol": "B"}], selector,
    )

    assert len(folds) == 2
    assert all(fold["status"] == "selected" for fold in folds)
    assert not selected.empty
    assert "sortino" in result.metrics


def test_walk_forward_accepts_training_data_starting_on_first_trading_day() -> None:
    dates = pd.date_range("2024-06-03", "2024-09-30", freq="B")
    daily = pd.DataFrame([
        {"date": day, "symbol": "A", "open": 10.0 + index}
        for index, day in enumerate(dates)
    ])

    def selector(daily, universe, start, end, params):
        signal_dates = daily.loc[daily["date"].between(start, end), "date"]
        return pd.DataFrame({
            "date": signal_dates,
            "symbol": "A",
            "target_weight": 1.0,
        })

    selected, _, folds = evaluate_walk_forward(
        daily,
        pd.DataFrame({"symbol": ["A"]}),
        {"start": "2024-09-01", "end": "2024-09-30"},
        {"train_months": 3, "validation_months": 1, "max_parameter_sets": 1},
        {"max_drawdown": {"operator": "abs<=", "threshold": 1.0}},
        "sortino",
        [{}],
        selector,
    )

    assert folds[0]["train_start"] == "2024-06-01"
    assert folds[0]["status"] == "selected"
    assert not selected.empty


def test_walk_forward_rejects_an_empty_training_window() -> None:
    daily = pd.DataFrame([
        {"date": day, "symbol": "A", "open": 10.0 + index}
        for index, day in enumerate(pd.date_range("2024-01-01", "2024-05-31", freq="B"))
    ])

    with pytest.raises(ValueError, match="insufficient data"):
        evaluate_walk_forward(
            daily,
            pd.DataFrame({"symbol": ["A"]}),
            {"start": "2024-09-01", "end": "2024-09-30"},
            {"train_months": 3, "validation_months": 1, "max_parameter_sets": 1},
            {"max_drawdown": {"operator": "abs<=", "threshold": 1.0}},
            "sortino",
            [{}],
            lambda daily, universe, start, end, params: pd.DataFrame(),
        )
