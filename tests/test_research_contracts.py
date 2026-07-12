from __future__ import annotations

import pytest

from quant_core.research import ExperimentResult, ResearchTask


def fixed_task() -> dict:
    return {
        "id": "etf-momentum",
        "goal": "Develop an ETF momentum strategy",
        "max_iterations": 3,
        "data": {"universe": "universe.csv"},
        "scope": {"editable": ["src/quant_core/strategy/"], "forbidden": ["data/"]},
        "commands": {"test": ["pytest"], "backtest": ["python3", "-m", "quant_core.cli"]},
        "evaluation": {
            "mode": "fixed",
            "objective": "sortino",
            "constraints": {"max_drawdown": 0.20},
            "fixed": {
                "train": {"start": "2018-01-01", "end": "2021-12-31"},
                "validation": {"start": "2022-01-01", "end": "2024-12-31"},
            },
            "test": {"start": "2025-01-01", "end": "2025-12-31"},
        },
    }


def completed_result() -> dict:
    return {
        "experiment_id": "experiment-001",
        "status": "completed",
        "hypothesis": "Medium-term momentum persists",
        "changes": {"summary": "Add a momentum strategy", "files": ["strategy.py"]},
        "verification": {"tests_passed": True, "backtest_completed": True},
        "metrics": {
            "train": {"sortino": 1.4, "max_drawdown": -0.12},
            "validation": {"sortino": 1.1, "max_drawdown": -0.16},
        },
    }


def test_fixed_task_allows_missing_baseline() -> None:
    task = ResearchTask.from_mapping(fixed_task())

    assert task.task_id == "etf-momentum"
    assert task.evaluation_mode == "fixed"


def test_walk_forward_task_is_supported() -> None:
    payload = fixed_task()
    payload["evaluation"] = {
        "mode": "walk_forward",
        "objective": "sortino",
        "constraints": {"max_drawdown": 0.20},
        "walk_forward": {
            "start": "2018-01-01",
            "end": "2024-12-31",
            "train_months": 36,
            "validation_months": 12,
            "step_months": 12,
        },
        "test": {"start": "2025-01-01", "end": "2025-12-31"},
    }

    assert ResearchTask.from_mapping(payload).evaluation_mode == "walk_forward"


def test_test_period_must_follow_research_period() -> None:
    payload = fixed_task()
    payload["evaluation"]["test"]["start"] = "2024-01-01"

    with pytest.raises(ValueError, match="must start after"):
        ResearchTask.from_mapping(payload)


def test_task_requires_numeric_constraints() -> None:
    payload = fixed_task()
    payload["evaluation"]["constraints"] = {}

    with pytest.raises(ValueError, match="numeric limits"):
        ResearchTask.from_mapping(payload)


def test_completed_result_keeps_test_metrics_hidden() -> None:
    result = ExperimentResult.from_mapping(completed_result())

    assert result.experiment_id == "experiment-001"


def test_completed_result_rejects_test_metrics() -> None:
    payload = completed_result()
    payload["metrics"]["test"] = {"sortino": 2.0}

    with pytest.raises(ValueError, match="must not contain"):
        ExperimentResult.from_mapping(payload)


def test_walk_forward_result_is_supported() -> None:
    payload = completed_result()
    payload["metrics"] = {
        "walk_forward": {
            "aggregate": {"sortino": 1.1, "max_drawdown": -0.17},
            "folds_path": "artifacts/folds.json",
        },
    }

    ExperimentResult.from_mapping(payload)


def test_failed_result_only_requires_error() -> None:
    result = ExperimentResult.from_mapping({
        "experiment_id": "experiment-001",
        "status": "failed",
        "error": "Codex session timed out",
    })

    assert result.experiment_id == "experiment-001"
