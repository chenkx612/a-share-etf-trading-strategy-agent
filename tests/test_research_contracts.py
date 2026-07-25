from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from quant_core.research import ExperimentResult, ResearchTask
from quant_core.research.runner import _decide, target_reached


REPOSITORY_ROOT = Path(__file__).parents[1]


def fixed_task() -> dict:
    return {
        "id": "etf-momentum",
        "goal": "Develop an ETF momentum strategy",
        "budget": {"max_rounds": 3, "max_hours": 4, "max_consecutive_failures": 2},
        "opencode": {"model": "xai/grok-4.5", "variant": "high", "timeout_minutes": 60},
        "data": {"universe": "universe.csv"},
        "scope": {
            "editable": ["src/quant_core/strategy/etf_momentum.py"],
            "forbidden": ["data/"],
        },
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
            "contract": {"paths": ["README.md"]},
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


def walk_forward_task() -> dict:
    payload = fixed_task()
    payload["strategy"] = {
        "name": "etf-momentum",
        "module": "quant_core.strategy.etf_momentum",
    }
    payload["commands"]["backtest"].append("{strategy_module}")
    fixed = payload["evaluation"].pop("fixed")
    payload["evaluation"]["mode"] = "walk_forward"
    payload["evaluation"]["walk_forward"] = {
        "train_months": 36,
        "validation_months": 12,
        "step_months": 12,
        "max_parameter_sets": 256,
        **fixed,
    }
    return payload


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


def test_task_requires_positive_round_minutes_when_present() -> None:
    payload = fixed_task()
    payload["budget"]["round_minutes"] = 0

    with pytest.raises(ValueError, match="round_minutes"):
        ResearchTask.from_mapping(payload)


def test_result_validates_round_timing_when_present() -> None:
    payload = completed_result()
    payload["round_timing"] = {
        "started_at": "2026-07-18T01:00:00+00:00",
        "deadline": "2026-07-18T01:30:00+00:00",
        "finished_at": "2026-07-18T01:20:00+00:00",
        "timeout_seconds": 1800,
        "duration_seconds": 1200.0,
    }

    ExperimentResult.from_mapping(payload)

    payload["round_timing"]["duration_seconds"] = float("nan")
    with pytest.raises(ValueError, match="duration_seconds"):
        ExperimentResult.from_mapping(payload)


def test_result_validates_structured_development_attempts() -> None:
    payload = completed_result()
    payload["development_attempts"] = [{
        "attempt_id": "001",
        "candidate_sha256": "a" * 64,
        "hypothesis": "Test a volatility filter",
        "development_metrics": {"sortino": 1.4},
        "outcome": "submitted",
        "learning": None,
    }]

    ExperimentResult.from_mapping(payload)

    payload["development_attempts"][0]["learning"] = ""
    with pytest.raises(ValueError, match="learning"):
        ExperimentResult.from_mapping(payload)


def test_result_validates_submission_provenance_when_present() -> None:
    payload = completed_result()
    payload["submission"] = {
        "mode": "checkpoint",
        "checkpoint_id": "002",
        "submitted_at": "2026-07-18T01:20:00+00:00",
        "submitted_by_timeout": True,
        "strategy_sha256": "a" * 64,
    }

    ExperimentResult.from_mapping(payload)

    payload["submission"]["submitted_by_timeout"] = False
    with pytest.raises(ValueError, match="timeout marker"):
        ExperimentResult.from_mapping(payload)


def test_task_requires_exactly_one_editable_strategy_script() -> None:
    payload = fixed_task()
    payload["scope"]["editable"] = ["strategy_a.py", "strategy_b.py"]

    with pytest.raises(ValueError, match="exactly one"):
        ResearchTask.from_mapping(payload)


def test_task_requires_explicit_evaluator_contract_paths() -> None:
    payload = fixed_task()
    del payload["evaluation"]["contract"]

    with pytest.raises(ValueError, match="evaluation.contract"):
        ResearchTask.from_mapping(payload)


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["/absolute/path"],
        ["../outside.py"],
        ["src\\evaluator.py"],
        ["src/evaluator/"],
        ["src//evaluator.py"],
        ["./src/evaluator.py"],
        [" src/evaluator.py"],
        ["."],
        ["outputs/metrics.json"],
        ["src/quant_core/strategy/etf_momentum.py"],
        ["src/quant_core", "src/quant_core/config.py"],
        ["README.md", "README.md"],
    ],
)
def test_task_rejects_unsafe_evaluator_contract_paths(paths: list[str]) -> None:
    payload = fixed_task()
    payload["evaluation"]["contract"]["paths"] = paths

    with pytest.raises(ValueError, match="contract.paths|paths must"):
        ResearchTask.from_mapping(payload)


def test_task_exposes_evaluator_contract_paths() -> None:
    task = ResearchTask.from_mapping(fixed_task())

    assert task.evaluator_contract_paths == ["README.md"]


@pytest.mark.parametrize(
    "task_name",
    [
        "liquid_etf_rerank_topk.toml",
        "sharpe_corr_threshold_optimization.toml",
    ],
)
def test_repository_task_contracts_cover_local_python_imports(task_name: str) -> None:
    task_path = REPOSITORY_ROOT / "tasks" / task_name
    task = ResearchTask.from_mapping(tomllib.loads(task_path.read_text(encoding="utf-8")))
    declared = [REPOSITORY_ROOT / path for path in task.evaluator_contract_paths]

    def covered(path: Path) -> bool:
        return any(path == root or root in path.parents for root in declared)

    python_files: list[Path] = []
    for path in declared:
        if path.is_dir():
            python_files.extend(path.rglob("*.py"))
        elif path.suffix == ".py":
            python_files.append(path)
    strategy_file = REPOSITORY_ROOT / task.strategy_path
    if strategy_file.is_file():
        python_files.append(strategy_file)
    strategy_package = strategy_file.parent / "__init__.py"
    assert covered(strategy_package), (
        f"{task_name} evaluator contract misses strategy package initializer: "
        f"{strategy_package.relative_to(REPOSITORY_ROOT)}"
    )

    missing: set[str] = set()
    for path in python_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.level == 0
            and isinstance(node.module, str)
            and node.module.startswith("quant_core")
        ]
        modules.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.startswith("quant_core")
        )
        for module in modules:
            relative = Path("src").joinpath(*module.split("."))
            candidates = [
                REPOSITORY_ROOT / relative.with_suffix(".py"),
                REPOSITORY_ROOT / relative / "__init__.py",
            ]
            dependency = next((candidate for candidate in candidates if candidate.is_file()), None)
            if dependency is None:
                continue
            dependency_relative = dependency.relative_to(REPOSITORY_ROOT).as_posix()
            if dependency_relative == task.strategy_path or covered(dependency):
                continue
            missing.add(dependency_relative)

    assert not missing, f"{task_name} evaluator contract misses imports: {sorted(missing)}"


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


def test_task_supports_explicit_strategy_metadata() -> None:
    payload = fixed_task()
    payload["strategy"] = {
        "name": "etf-momentum",
        "module": "quant_core.strategy.etf_momentum",
    }
    payload["commands"]["backtest"].extend([
        "--candidate-module",
        "{strategy_module}",
    ])

    task = ResearchTask.from_mapping(payload)

    assert task.strategy_name == "etf-momentum"
    assert task.strategy_module == "quant_core.strategy.etf_momentum"


def test_task_rejects_unused_strategy_metadata() -> None:
    payload = fixed_task()
    payload["strategy"] = {
        "name": "etf-momentum",
        "module": "quant_core.strategy.etf_momentum",
    }

    with pytest.raises(ValueError, match="must reference"):
        ResearchTask.from_mapping(payload)


def test_task_rejects_invalid_strategy_module() -> None:
    payload = fixed_task()
    payload["strategy"] = {
        "name": "etf-momentum",
        "module": "quant_core.strategy.etf-momentum",
    }
    payload["commands"]["backtest"].append("{strategy_module}")

    with pytest.raises(ValueError, match="Python module path"):
        ResearchTask.from_mapping(payload)


def test_task_rejects_strategy_placeholder_without_metadata() -> None:
    payload = fixed_task()
    payload["commands"]["backtest"].append("{strategy_module}")

    with pytest.raises(ValueError, match="task.strategy is required"):
        ResearchTask.from_mapping(payload)


def test_fixed_task_allows_missing_baseline() -> None:
    task = ResearchTask.from_mapping(fixed_task())

    assert task.task_id == "etf-momentum"
    assert task.evaluation_mode == "fixed"
    assert task.baseline_mode == "workspace"


def test_task_supports_no_baseline() -> None:
    payload = fixed_task()
    payload["baseline"] = {"mode": "none", "exclude": ["src/quant_core/strategy/"]}

    task = ResearchTask.from_mapping(payload)

    assert task.baseline_mode == "none"
    assert task.baseline_exclude == ["src/quant_core/strategy/"]


def test_task_rejects_unknown_baseline_mode() -> None:
    payload = fixed_task()
    payload["baseline"] = {"mode": "legacy"}

    with pytest.raises(ValueError, match="baseline.mode"):
        ResearchTask.from_mapping(payload)


def test_task_rejects_exclusions_for_workspace_baseline() -> None:
    payload = fixed_task()
    payload["baseline"] = {"mode": "workspace", "exclude": ["strategy.py"]}

    with pytest.raises(ValueError, match="requires mode"):
        ResearchTask.from_mapping(payload)


def test_walk_forward_task_is_accepted() -> None:
    payload = walk_forward_task()

    task = ResearchTask.from_mapping(payload)
    assert task.evaluation_mode == "walk_forward"


def test_walk_forward_task_does_not_require_unused_backtest_template() -> None:
    payload = walk_forward_task()
    del payload["commands"]["backtest"]

    task = ResearchTask.from_mapping(payload)

    assert task.evaluation_mode == "walk_forward"


def test_test_period_must_follow_research_period() -> None:
    payload = fixed_task()
    payload["evaluation"]["test"]["start"] = "2024-01-01"

    with pytest.raises(ValueError, match="must start after"):
        ResearchTask.from_mapping(payload)


def test_task_allows_no_test_period() -> None:
    payload = fixed_task()
    del payload["evaluation"]["test"]

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


def test_task_rejects_non_finite_thresholds() -> None:
    payload = fixed_task()
    payload["evaluation"]["constraints"]["max_drawdown"]["threshold"] = float("nan")

    with pytest.raises(ValueError, match="numeric and finite"):
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


def test_task_requires_finite_minimum_improvement() -> None:
    payload = fixed_task()
    payload["evaluation"]["acceptance"] = {"minimum_improvement": float("inf")}

    with pytest.raises(ValueError, match="finite and non-negative"):
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


def test_walk_forward_target_requires_all_gate_folds_to_be_feasible() -> None:
    payload = walk_forward_task()
    payload["evaluation"]["target"] = {"objective_at_least": 1.5}
    task = ResearchTask.from_mapping(payload)
    gate = {
        "aggregate": {"sortino": 1.6, "max_drawdown": 0.0},
        "no_feasible_parameter_folds": 1,
    }

    assert not target_reached(task, {"gate": gate})
    gate["no_feasible_parameter_folds"] = 0
    assert target_reached(task, {"gate": gate})


@pytest.mark.parametrize("no_feasible_folds", [1, 3])
def test_walk_forward_candidate_with_infeasible_gate_folds_is_rejected(
    no_feasible_folds: int,
) -> None:
    task = ResearchTask.from_mapping(walk_forward_task())
    candidate = {
        "gate": {
            "aggregate": {"sortino": 2.0, "max_drawdown": 0.0},
            "no_feasible_parameter_folds": no_feasible_folds,
        },
    }

    decision = _decide(task, None, candidate)

    assert decision["decision"] == "rejected"
    assert decision["reasons"] == ["gate has folds with no feasible parameters"]


@pytest.mark.parametrize("no_feasible_folds", [None, "0", 0.0, False])
def test_walk_forward_gate_feasibility_requires_an_integer_zero(
    no_feasible_folds: object,
) -> None:
    task = ResearchTask.from_mapping(walk_forward_task())
    candidate = {
        "gate": {
            "aggregate": {"sortino": 2.0, "max_drawdown": 0.0},
            "no_feasible_parameter_folds": no_feasible_folds,
        },
    }

    assert _decide(task, None, candidate)["decision"] == "rejected"


def test_walk_forward_feasible_candidate_compares_normally() -> None:
    task = ResearchTask.from_mapping(walk_forward_task())
    champion = {
        "gate": {
            "aggregate": {"sortino": 1.0, "max_drawdown": -0.10},
            "no_feasible_parameter_folds": 0,
        },
    }
    candidate = {
        "gate": {
            "aggregate": {"sortino": 1.2, "max_drawdown": -0.10},
            "no_feasible_parameter_folds": 0,
        },
    }

    assert _decide(task, champion, candidate)["decision"] == "accepted"


def test_walk_forward_feasible_candidate_replaces_unverifiable_champion() -> None:
    task = ResearchTask.from_mapping(walk_forward_task())
    champion = {"gate": {"aggregate": {"sortino": 5.0, "max_drawdown": -0.10}}}
    candidate = {
        "gate": {
            "aggregate": {"sortino": 1.0, "max_drawdown": -0.10},
            "no_feasible_parameter_folds": 0,
        },
    }

    decision = _decide(task, champion, candidate)

    assert decision["decision"] == "accepted"
    assert decision["objective"]["champion_constraints_passed"] is False
    assert decision["objective"]["relative_improvement_required"] is False


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


def test_feasible_candidate_replaces_infeasible_champion_without_objective_improvement() -> None:
    payload = fixed_task()
    payload["evaluation"]["acceptance"] = {"minimum_improvement": 0.50}
    task = ResearchTask.from_mapping(payload)
    champion = {"gate": {"sortino": 5.0, "max_drawdown": -0.30}}

    accepted = _decide(task, champion, {"gate": {"sortino": 1.0, "max_drawdown": -0.10}})
    rejected = _decide(task, champion, {"gate": {"sortino": 6.0, "max_drawdown": -0.30}})

    assert accepted["decision"] == "accepted"
    assert accepted["objective"]["champion_constraints_passed"] is False
    assert rejected["decision"] == "rejected"
    assert rejected["reasons"] == ["gate constraints failed"]


def test_feasible_champion_still_requires_objective_improvement() -> None:
    payload = fixed_task()
    payload["evaluation"]["acceptance"] = {"minimum_improvement": 0.50}
    task = ResearchTask.from_mapping(payload)
    champion = {"gate": {"sortino": 2.0, "max_drawdown": -0.10}}

    rejected = _decide(task, champion, {"gate": {"sortino": 2.4, "max_drawdown": -0.10}})

    assert rejected["decision"] == "rejected"
    assert rejected["objective"]["champion_constraints_passed"] is True
    assert rejected["reasons"] == ["gate objective did not improve over champion"]


def test_non_finite_objectives_never_block_or_create_a_champion() -> None:
    task = ResearchTask.from_mapping(fixed_task())
    invalid_champion = {"gate": {"sortino": float("nan"), "max_drawdown": -0.10}}

    accepted = _decide(
        task,
        invalid_champion,
        {"gate": {"sortino": 1.0, "max_drawdown": -0.10}},
    )
    rejected = _decide(
        task,
        None,
        {"gate": {"sortino": float("inf"), "max_drawdown": -0.10}},
    )

    assert accepted["decision"] == "accepted"
    assert accepted["objective"]["relative_improvement_required"] is False
    assert rejected["decision"] == "rejected"
    assert rejected["reasons"] == ["gate objective is not finite"]


def test_first_candidate_needs_constraints_but_not_relative_improvement() -> None:
    payload = fixed_task()
    payload["baseline"] = {"mode": "none"}
    payload["evaluation"]["acceptance"] = {"minimum_improvement": 0.50}
    task = ResearchTask.from_mapping(payload)

    accepted = _decide(task, None, {"gate": {"sortino": 0.2, "max_drawdown": -0.10}})
    rejected = _decide(task, None, {"gate": {"sortino": 2.0, "max_drawdown": -0.30}})

    assert accepted["decision"] == "accepted"
    assert accepted["objective"]["champion"] is None
    assert rejected["decision"] == "rejected"
    assert rejected["reasons"] == ["gate constraints failed"]


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


def test_failed_result_accepts_infrastructure_classification() -> None:
    result = ExperimentResult.from_mapping({
        "experiment_id": "experiment-001",
        "status": "failed",
        "error": "Docker bind source unavailable",
        "failure_kind": "infrastructure",
    })

    assert result.raw["failure_kind"] == "infrastructure"


def test_completed_result_rejects_failure_classification() -> None:
    payload = completed_result()
    payload["failure_kind"] = "infrastructure"

    with pytest.raises(ValueError, match="completed result"):
        ExperimentResult.from_mapping(payload)
