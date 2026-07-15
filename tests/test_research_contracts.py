from __future__ import annotations

import pytest

from quant_core.research import ExperimentResult, ResearchTask
from quant_core.research.runner import _decide, target_reached


def fixed_task() -> dict:
    return {
        "id": "etf-momentum",
        "goal": "Develop an ETF momentum strategy",
        "budget": {"max_rounds": 3, "max_hours": 4, "max_consecutive_failures": 2},
        "opencode": {"model": "xai/grok-4.5", "variant": "high", "timeout_minutes": 60},
        "data": {"universe": "universe.csv"},
        "scope": {"editable": ["src/quant_core/strategy/"], "forbidden": ["data/"]},
        "commands": {
            "test": ["pytest"],
            "backtest": [
                "python3", "-m", "quant_core.cli",
                "--start", "{start}", "--end", "{end}", "--run-id", "{run_id}",
            ],
            "metrics_path": "outputs/backtests/{run_id}/metrics.json",
        },
        "evaluation": {
            "mode": "fixed",
            "objective": "sortino",
            "constraints": {
                "max_drawdown": {"operator": "abs<=", "threshold": 0.20},
            },
            "fixed": {
                "development": {"start": "2018-01-01", "end": "2021-12-31"},
                "gate": {"start": "2022-01-01", "end": "2024-12-31"},
            },
            "test": {"start": "2025-01-01", "end": "2025-12-31"},
        },
    }


def completed_result() -> dict:
    return {
        "experiment_id": "experiment-001",
        "status": "completed",
        "feedback": "No prior experiments were available.",
        "hypothesis": "Medium-term momentum persists",
        "attempts": "Tested medium-term momentum with a volatility filter.",
        "development_effect": "Development Sortino improved with acceptable drawdown.",
        "candidate": "Retained the volatility-filtered momentum strategy.",
        "changes": {"files": ["strategy.py"]},
        "metrics": {
            "development": {"sortino": 1.4, "max_drawdown": -0.12},
            "gate": {"sortino": 1.1, "max_drawdown": -0.16},
        },
    }


def test_task_requires_positive_budget() -> None:
    payload = fixed_task()
    payload["budget"]["max_rounds"] = 0

    with pytest.raises(ValueError, match="max_rounds"):
        ResearchTask.from_mapping(payload)


def test_task_requires_opencode_model() -> None:
    payload = fixed_task()
    payload["opencode"]["model"] = ""

    with pytest.raises(ValueError, match="opencode.model"):
        ResearchTask.from_mapping(payload)


def test_task_requires_opencode_provider_model_format() -> None:
    payload = fixed_task()
    payload["opencode"]["model"] = "deepseek-chat"

    with pytest.raises(ValueError, match="provider/model"):
        ResearchTask.from_mapping(payload)


def test_task_requires_positive_opencode_timeout() -> None:
    payload = fixed_task()
    payload["opencode"]["timeout_minutes"] = 0

    with pytest.raises(ValueError, match="opencode.timeout_minutes"):
        ResearchTask.from_mapping(payload)


def test_task_requires_non_empty_opencode_variant_when_provided() -> None:
    payload = fixed_task()
    payload["opencode"]["variant"] = ""

    with pytest.raises(ValueError, match="opencode.variant"):
        ResearchTask.from_mapping(payload)


def test_task_allows_model_without_variant() -> None:
    payload = fixed_task()
    payload["opencode"] = {"model": "opencode/hy3-free", "timeout_minutes": 60}

    ResearchTask.from_mapping(payload)


def test_fixed_task_allows_missing_baseline() -> None:
    task = ResearchTask.from_mapping(fixed_task())

    assert task.task_id == "etf-momentum"
    assert task.evaluation_mode == "fixed"


def test_task_rejects_unused_baseline_configuration() -> None:
    payload = fixed_task()
    payload["baseline"] = {"strategy": "legacy"}

    with pytest.raises(ValueError, match="not supported"):
        ResearchTask.from_mapping(payload)


def test_walk_forward_task_is_rejected_until_implemented() -> None:
    payload = fixed_task()
    payload["evaluation"] = {
        "mode": "walk_forward",
        "objective": "sortino",
        "constraints": {
            "max_drawdown": {"operator": "abs<=", "threshold": 0.20},
        },
        "walk_forward": {
            "start": "2018-01-01",
            "end": "2024-12-31",
            "train_months": 36,
            "validation_months": 12,
            "step_months": 12,
        },
        "test": {"start": "2025-01-01", "end": "2025-12-31"},
    }

    with pytest.raises(ValueError, match="must be 'fixed'"):
        ResearchTask.from_mapping(payload)


def test_test_period_must_follow_research_period() -> None:
    payload = fixed_task()
    payload["evaluation"]["test"]["start"] = "2024-01-01"

    with pytest.raises(ValueError, match="must start after"):
        ResearchTask.from_mapping(payload)


def test_task_requires_non_empty_constraints() -> None:
    payload = fixed_task()
    payload["evaluation"]["constraints"] = {}

    with pytest.raises(ValueError, match="must not be empty"):
        ResearchTask.from_mapping(payload)


def test_task_rejects_implicit_numeric_constraints() -> None:
    payload = fixed_task()
    payload["evaluation"]["constraints"] = {"max_drawdown": 0.20}

    with pytest.raises(ValueError, match="operator/threshold table"):
        ResearchTask.from_mapping(payload)


def test_task_accepts_explicit_lower_and_absolute_constraints() -> None:
    payload = fixed_task()
    payload["evaluation"]["constraints"] = {
        "annual_return": {"operator": ">=", "threshold": 0.10},
        "max_drawdown": {"operator": "abs<=", "threshold": 0.20},
    }

    ResearchTask.from_mapping(payload)


def test_task_rejects_unknown_constraint_operator() -> None:
    payload = fixed_task()
    payload["evaluation"]["constraints"] = {
        "annual_return": {"operator": ">", "threshold": 0.10},
    }

    with pytest.raises(ValueError, match="operator must be one of"):
        ResearchTask.from_mapping(payload)


def test_task_rejects_extra_constraint_fields() -> None:
    payload = fixed_task()
    payload["evaluation"]["constraints"] = {
        "annual_return": {"operator": ">=", "threshold": 0.10, "value": 0.10},
    }

    with pytest.raises(ValueError, match="exactly operator and threshold"):
        ResearchTask.from_mapping(payload)


def test_task_requires_numeric_target() -> None:
    payload = fixed_task()
    payload["evaluation"]["target"] = {"objective_at_least": "high"}

    with pytest.raises(ValueError, match="objective_at_least"):
        ResearchTask.from_mapping(payload)


def test_target_requires_gate_constraints_to_pass() -> None:
    payload = fixed_task()
    payload["evaluation"]["target"] = {"objective_at_least": 1.5}
    task = ResearchTask.from_mapping(payload)

    assert not target_reached(task, {
        "gate": {"sortino": 1.6, "max_drawdown": -0.30},
    })


def test_target_supports_lower_bound_constraints() -> None:
    payload = fixed_task()
    payload["evaluation"]["target"] = {"objective_at_least": 1.5}
    payload["evaluation"]["constraints"] = {
        "annual_return": {"operator": ">=", "threshold": 0.10},
        "max_drawdown": {"operator": "abs<=", "threshold": 0.20},
    }
    task = ResearchTask.from_mapping(payload)

    assert target_reached(task, {
        "gate": {"sortino": 1.6, "annual_return": 0.12, "max_drawdown": -0.15},
    })
    assert not target_reached(task, {
        "gate": {"sortino": 1.6, "annual_return": 0.08, "max_drawdown": -0.15},
    })


def test_decision_enforces_lower_bound_constraints() -> None:
    payload = fixed_task()
    payload["evaluation"]["constraints"] = {
        "annual_return": {"operator": ">=", "threshold": 0.10},
    }
    task = ResearchTask.from_mapping(payload)
    champion = {"gate": {"sortino": 1.0, "annual_return": 0.12}}

    accepted = _decide(task, champion, {"gate": {"sortino": 1.2, "annual_return": 0.11}})
    rejected = _decide(task, champion, {"gate": {"sortino": 1.2, "annual_return": 0.08}})

    assert accepted["decision"] == "accepted"
    assert rejected["decision"] == "rejected"
    assert rejected["constraints"]["annual_return"]["operator"] == ">="


def test_completed_result_keeps_test_metrics_hidden() -> None:
    result = ExperimentResult.from_mapping(completed_result())

    assert result.experiment_id == "experiment-001"


def test_completed_result_rejects_test_metrics() -> None:
    payload = completed_result()
    payload["metrics"]["test"] = {"sortino": 2.0}

    with pytest.raises(ValueError, match="must not contain"):
        ExperimentResult.from_mapping(payload)


def test_completed_result_rejects_redundant_change_summary() -> None:
    payload = completed_result()
    payload["changes"]["summary"] = "Duplicate candidate description"

    with pytest.raises(ValueError, match="exactly files"):
        ExperimentResult.from_mapping(payload)


def test_walk_forward_result_is_rejected_until_implemented() -> None:
    payload = completed_result()
    payload["metrics"] = {
        "walk_forward": {
            "aggregate": {"sortino": 1.1, "max_drawdown": -0.17},
            "folds_path": "artifacts/folds.json",
        },
    }

    with pytest.raises(ValueError, match="development and gate"):
        ExperimentResult.from_mapping(payload)


def test_failed_result_only_requires_error() -> None:
    result = ExperimentResult.from_mapping({
        "experiment_id": "experiment-001",
        "status": "failed",
        "error": "OpenCode session timed out",
    })

    assert result.experiment_id == "experiment-001"
