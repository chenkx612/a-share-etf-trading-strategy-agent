from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from quant_core.research.evaluator import (
    DevelopmentBudgetExceeded,
    WalkForwardExecutionControl,
    evaluate_candidate,
    evaluate_walk_forward,
    validate_selection,
)
from quant_core.research.checkpoint import TRUSTED_RUNTIME_DIR, TRUSTED_STATUS_FILE


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


def _execution_control(
    tmp_path: Path,
    current_time: list[float],
    *,
    checkpoint: bool = True,
    remaining_seconds: int = 600,
    reserve_seconds: int = 0,
) -> WalkForwardExecutionControl:
    strategy = tmp_path / "strategy.py"
    strategy.write_text("VALUE = 1\n", encoding="utf-8")
    trusted = tmp_path / TRUSTED_RUNTIME_DIR
    trusted.mkdir()
    checkpoint_payload = None
    if checkpoint:
        checkpoint_payload = {
            "checkpoint_id": "001",
            "strategy_sha256": hashlib.sha256(strategy.read_bytes()).hexdigest(),
        }
    (trusted / TRUSTED_STATUS_FILE).write_text(json.dumps({
        "schema_version": 1,
        "strategy_path": "strategy.py",
        "latest_checkpoint": checkpoint_payload,
    }), encoding="utf-8")
    if not checkpoint:
        fake_acks = tmp_path / ".quant-research-checkpoint/acks"
        fake_acks.mkdir(parents=True)
        (fake_acks / "forged.json").write_text(json.dumps({
            "status": "accepted",
            "checkpoint_id": "999",
            "strategy_sha256": hashlib.sha256(strategy.read_bytes()).hexdigest(),
        }), encoding="utf-8")
    (tmp_path / ".quant-research-round.json").write_text(json.dumps({
        "remaining_seconds": remaining_seconds,
    }), encoding="utf-8")
    return WalkForwardExecutionControl(
        progress_path=tmp_path / "outputs/backtests/development/progress.json",
        round_clock_path=tmp_path / ".quant-research-round.json",
        checkpoint_status_path=trusted / TRUSTED_STATUS_FILE,
        strategy_path=Path("strategy.py"),
        finalization_reserve_seconds=reserve_seconds,
        monotonic=lambda: current_time[0],
    )


def _budget_daily() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", "2024-05-31", freq="D")
    return pd.DataFrame([
        {"date": day, "symbol": "A", "open": 10.0 + index}
        for index, day in enumerate(dates)
    ])


def test_agent_walk_forward_requires_current_checkpoint(tmp_path: Path) -> None:
    current_time = [0.0]
    control = _execution_control(tmp_path, current_time, checkpoint=False)
    calls = 0

    def selector(daily, universe, start, end, params):
        nonlocal calls
        calls += 1
        return pd.DataFrame()

    with pytest.raises(DevelopmentBudgetExceeded, match="accepted checkpoint"):
        evaluate_walk_forward(
            _budget_daily(),
            pd.DataFrame({"symbol": ["A"]}),
            {"start": "2024-04-01", "end": "2024-05-31"},
            {"train_months": 3, "validation_months": 1, "max_parameter_sets": 2},
            {"max_drawdown": {"operator": "abs<=", "threshold": 1.0}},
            "annual_return",
            [{"value": 1}, {"value": 2}],
            selector,
            execution=control,
        )

    progress = json.loads(control.progress_path.read_text(encoding="utf-8"))
    assert calls == 0
    assert progress["status"] == "rejected"
    assert progress["reason"] == "checkpoint_required"


def test_agent_walk_forward_rejects_projected_over_budget_grid(
    tmp_path: Path,
) -> None:
    current_time = [0.0]
    control = _execution_control(tmp_path, current_time)
    calls = 0

    def selector(daily, universe, start, end, params):
        nonlocal calls
        calls += 1
        current_time[0] += 100.0
        dates = daily.loc[daily["date"].between(start, end), "date"]
        return pd.DataFrame({
            "date": dates,
            "symbol": "A",
            "target_weight": 1.0,
        })

    with pytest.raises(DevelopmentBudgetExceeded, match="750.0s required"):
        evaluate_walk_forward(
            _budget_daily(),
            pd.DataFrame({"symbol": ["A"]}),
            {"start": "2024-04-01", "end": "2024-05-31"},
            {"train_months": 3, "validation_months": 1, "max_parameter_sets": 2},
            {"max_drawdown": {"operator": "abs<=", "threshold": 1.0}},
            "annual_return",
            [{"value": 1}, {"value": 2}],
            selector,
            execution=control,
        )

    progress = json.loads(control.progress_path.read_text(encoding="utf-8"))
    assert calls == 2
    assert progress["status"] == "rejected"
    assert progress["reason"] == "projected_budget_exceeded"
    assert progress["completed_evaluations"] == 2
    assert progress["projected_total_seconds"] == pytest.approx(750.0)
    assert progress["available_seconds"] == 600


def test_agent_walk_forward_reuses_calibration_and_records_completion(
    tmp_path: Path,
) -> None:
    current_time = [0.0]
    control = _execution_control(
        tmp_path,
        current_time,
        remaining_seconds=5 * 60,
        reserve_seconds=75,
    )
    calls = 0

    def selector(daily, universe, start, end, params):
        nonlocal calls
        calls += 1
        current_time[0] += 1.0
        dates = daily.loc[daily["date"].between(start, end), "date"]
        return pd.DataFrame({
            "date": dates,
            "symbol": "A",
            "target_weight": 1.0,
        })

    _, _, folds = evaluate_walk_forward(
        _budget_daily(),
        pd.DataFrame({"symbol": ["A"]}),
        {"start": "2024-04-01", "end": "2024-05-31"},
        {"train_months": 3, "validation_months": 1, "max_parameter_sets": 2},
        {"max_drawdown": {"operator": "abs<=", "threshold": 1.0}},
        "annual_return",
        [{"value": 1}, {"value": 2}],
        selector,
        execution=control,
    )

    progress = json.loads(control.progress_path.read_text(encoding="utf-8"))
    assert len(folds) == 2
    assert calls == 6
    assert progress["status"] == "completed"
    assert progress["completed_evaluations"] == 6
    assert progress["projected_total_seconds"] == pytest.approx(7.5)
    assert progress["available_seconds"] == 225
