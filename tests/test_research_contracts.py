from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import pytest

from quant_core.research import ExperimentResult, ResearchTask
from quant_core.research import environment as environment_module
from quant_core.research.environment import (
    EvaluationEnvironment,
    capture_evaluation_environment,
    persist_evaluation_environment,
)
from quant_core.research.runner import _decide, target_reached


REPOSITORY_ROOT = Path(__file__).parents[1]


def _fake_conda_prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "quant"
    conda_meta = prefix / "conda-meta"
    conda_meta.mkdir(parents=True)
    (conda_meta / "python-3.13.1-build_0.json").write_text(
        json.dumps({
            "name": "python",
            "version": "3.13.1",
            "build": "build_0",
            "build_number": 0,
            "channel": "https://user:secret@example.invalid/pkgs/main/osx-arm64",
            "subdir": "osx-arm64",
        }),
        encoding="utf-8",
    )
    return prefix


def test_capture_requires_exact_quant_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = _fake_conda_prefix(tmp_path)
    monkeypatch.setattr(environment_module.sys, "prefix", str(prefix))
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "other")
    monkeypatch.setenv("CONDA_PREFIX", str(prefix))

    with pytest.raises(RuntimeError, match="requires Conda environment 'quant'"):
        capture_evaluation_environment()


def test_environment_manifest_is_stable_and_excludes_sensitive_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = _fake_conda_prefix(tmp_path)
    monkeypatch.setattr(environment_module.sys, "prefix", str(prefix))
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "quant")
    monkeypatch.setenv("CONDA_PREFIX", str(prefix))
    monkeypatch.setattr(environment_module.metadata, "distributions", lambda: [])

    first = capture_evaluation_environment()
    second = capture_evaluation_environment()
    serialized = json.dumps(first.manifest, sort_keys=True)

    assert first.sha256 == second.sha256
    assert str(tmp_path) not in serialized
    assert "secret" not in serialized
    assert "https://" not in serialized
    assert first.manifest["conda"]["packages"][0]["channel"] == "example.invalid/main"


def test_environment_manifest_registry_is_content_addressed(tmp_path: Path) -> None:
    environment = EvaluationEnvironment.from_manifest({
        "schema_version": 1,
        "python": {"version": "3.13.1"},
    })

    first = persist_evaluation_environment(tmp_path, environment)
    second = persist_evaluation_environment(tmp_path, environment)

    assert first == second
    assert first.name == f"{environment.sha256}.json"
    assert EvaluationEnvironment.from_manifest(
        json.loads(first.read_text(encoding="utf-8"))
    ).sha256 == environment.sha256


def test_environment_manifest_registry_rejects_a_false_digest(tmp_path: Path) -> None:
    environment = EvaluationEnvironment(
        manifest={"schema_version": 1},
        sha256="0" * 64,
    )

    with pytest.raises(RuntimeError, match="does not match"):
        persist_evaluation_environment(tmp_path, environment)

    assert not (tmp_path / "environments").exists()


def fixed_task() -> dict:
    return {
        "id": "etf-momentum",
        "goal": "Develop an ETF momentum strategy",
        "budget": {
            "max_rounds": 3,
            "max_hours": 4,
            "max_consecutive_failures": 2,
            "round_minutes": 60,
        },
        "opencode": {"model": "xai/grok-4.5", "variant": "high"},
        "execution": {"command_timeout_minutes": 60},
        "data": {"universe": "universe.csv"},
        "scope": {
            "editable": ["src/quant_core/strategy/etf_momentum.py"],
        },
        "commands": {
            "tests": ["tests/test_etf_momentum.py"],
            "backtest": [
                "python3", "-m", "quant_core.cli",
                "--start", "{start}", "--end", "{end}", "--run-id", "{run_id}",
            ],
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
                "guard": {"start": "2025-01-01", "end": "2025-12-31"},
            },
            "guard": {
                "benchmark": "universe_equal_weight",
                "max_excess_annual_return_degradation": 0.10,
            },
        },
    }


def walk_forward_task() -> dict:
    payload = fixed_task()
    payload["strategy"] = {
        "name": "etf-momentum",
    }
    payload["commands"].pop("backtest")
    fixed = payload["evaluation"].pop("fixed")
    objective = payload["evaluation"].pop("objective")
    constraints = payload["evaluation"].pop("constraints")
    payload["evaluation"]["mode"] = "walk_forward"
    payload["parameter_selection"] = {
        "train_months": 36,
        "objective": objective,
        "constraints": constraints,
        "max_parameter_sets": 256,
        "schedule": {
            "period": "calendar_month",
            "interval": 1,
            "trigger": "start",
        },
    }
    payload["evaluation"]["walk_forward"] = fixed
    return payload


def production_task() -> dict:
    payload = walk_forward_task()
    payload["parameter_selection"]["train_months"] = 18
    payload["parameter_selection"]["max_parameter_sets"] = 128
    payload["production"] = {
        "curve_months": 12,
        "benchmark": "510300",
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


def test_task_rejects_aliases() -> None:
    payload = fixed_task()
    payload["aliases"] = ["short-name"]

    with pytest.raises(ValueError, match="task.aliases"):
        ResearchTask.from_mapping(payload)


def test_task_limits_id_to_three_words_without_documented_exception() -> None:
    payload = fixed_task()
    payload["id"] = "one-two-three-four"

    with pytest.raises(ValueError, match="at most three words"):
        ResearchTask.from_mapping(payload)

    payload["long_name_reason"] = "Compatibility with existing managed history."
    task = ResearchTask.from_mapping(payload)

    assert task.task_id == "one-two-three-four"


@pytest.mark.parametrize("reason", ["", "   ", 1])
def test_task_rejects_invalid_long_name_reason(reason: object) -> None:
    payload = fixed_task()
    payload["long_name_reason"] = reason

    with pytest.raises(ValueError, match="task.long_name_reason"):
        ResearchTask.from_mapping(payload)


def test_task_requires_positive_round_minutes_when_present() -> None:
    payload = fixed_task()
    payload["budget"]["round_minutes"] = 0

    with pytest.raises(ValueError, match="round_minutes"):
        ResearchTask.from_mapping(payload)


def test_task_accepts_strict_production_contract() -> None:
    task = ResearchTask.from_mapping(production_task())
    assert task.production is not None
    assert task.parameter_selection is not None
    assert task.parameter_selection["schedule"]["period"] == "calendar_month"


def test_task_accepts_explicit_production_data_requirements() -> None:
    payload = production_task()
    payload["production"]["data_requirements"] = {
        "required_columns": ["date", "symbol", "open", "close"],
        "min_history": 125,
    }

    task = ResearchTask.from_mapping(payload)

    assert task.production is not None
    assert task.production["data_requirements"]["min_history"] == 125


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        (
            {"required_columns": ["date", "symbol", "open"], "min_history": 125},
            "must include date, symbol, open, and close",
        ),
        (
            {
                "required_columns": ["date", "symbol", "open", "close", "close"],
                "min_history": 125,
            },
            "unique non-empty strings",
        ),
        (
            {
                "required_columns": ["date", "symbol", "open", "close"],
                "min_history": 0,
            },
            "positive integer",
        ),
    ],
)
def test_task_rejects_invalid_production_data_requirements(
    requirements: dict[str, object],
    message: str,
) -> None:
    payload = production_task()
    payload["production"]["data_requirements"] = requirements

    with pytest.raises(ValueError, match=message):
        ResearchTask.from_mapping(payload)


def test_walk_forward_rejects_legacy_start_anchored_frequency_fields() -> None:
    payload = walk_forward_task()
    payload["evaluation"]["walk_forward"]["validation_months"] = 1

    with pytest.raises(ValueError, match="must contain exactly"):
        ResearchTask.from_mapping(payload)


def test_walk_forward_requires_month_start_schedule() -> None:
    payload = walk_forward_task()
    payload["parameter_selection"]["schedule"]["trigger"] = "end"

    with pytest.raises(ValueError, match="trigger must be start"):
        ResearchTask.from_mapping(payload)


def test_production_requires_parameter_selection() -> None:
    payload = production_task()
    del payload["parameter_selection"]

    with pytest.raises(ValueError, match="parameter_selection is required"):
        ResearchTask.from_mapping(payload)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("schedule", "period"), "quarter", "schedule.period"),
        (("schedule", "interval"), 0, "schedule.interval"),
        (("schedule", "trigger"), "middle", "schedule.trigger"),
        (("train_months",), 0, "train_months"),
        (("objective",), "calmar", "objective"),
        (("max_parameter_sets",), 0, "max_parameter_sets"),
    ],
)
def test_task_rejects_invalid_parameter_selection_contract(
    path: tuple[str, ...], value: object, message: str
) -> None:
    payload = production_task()
    target = payload["parameter_selection"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=message):
        ResearchTask.from_mapping(payload)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("curve_months", 0, "curve_months"),
        ("benchmark", "", "benchmark"),
        ("objective", "sortino", "parameter search policy belongs"),
    ],
)
def test_task_rejects_invalid_or_legacy_production_contract(
    key: str, value: object, message: str
) -> None:
    payload = production_task()
    payload["production"][key] = value

    with pytest.raises(ValueError, match=message):
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


def test_task_rejects_explicit_evaluator_contract_paths() -> None:
    payload = fixed_task()
    payload["evaluation"]["contract"] = {"paths": ["README.md"]}

    with pytest.raises(ValueError, match="Harness-owned"):
        ResearchTask.from_mapping(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("strategy", {"name": "etf-momentum", "module": "legacy.module"}, "module is derived"),
        (
            "scope",
            {
                "editable": ["src/quant_core/strategy/etf_momentum.py"],
                "forbidden": ["data/"],
            },
            "Harness-owned",
        ),
        (
            "commands",
            {
                "test": ["pytest"],
                "backtest": ["backtest", "{start}", "{end}", "{run_id}"],
            },
            "obsolete",
        ),
        (
            "commands",
            {
                "tests": ["tests/test_etf_momentum.py"],
                "backtest": ["backtest", "{start}", "{end}", "{run_id}"],
                "metrics_path": "outputs/backtests/{run_id}/metrics.json",
            },
            "Harness-owned",
        ),
    ],
)
def test_task_rejects_removed_task_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = fixed_task()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        ResearchTask.from_mapping(payload)


@pytest.mark.parametrize(
    "test_path",
    ["--collect-only", "../test_strategy.py", "/tmp/test_strategy.py", "README.md"],
)
def test_task_rejects_non_test_command_targets(test_path: str) -> None:
    payload = fixed_task()
    payload["commands"]["tests"] = [test_path]

    with pytest.raises(ValueError, match="pytest files"):
        ResearchTask.from_mapping(payload)


def test_task_exposes_evaluator_contract_paths() -> None:
    payload = walk_forward_task()
    payload["commands"]["tests"] = [
        "tests/test_etf_momentum.py::test_selection",
    ]
    task = ResearchTask.from_mapping(payload)

    assert "src/quant_core" in task.evaluator_contract_paths
    assert "tests" in task.evaluator_contract_paths
    assert "universe.csv" in task.evaluator_contract_paths
    assert task.strategy_path not in task.evaluator_contract_paths


def test_strategy_module_preserves_root_level_src_filename() -> None:
    payload = fixed_task()
    payload["strategy"] = {"name": "root-strategy"}
    payload["scope"]["editable"] = ["src.py"]
    payload["commands"]["backtest"].append("{strategy_module}")

    task = ResearchTask.from_mapping(payload)

    assert task.strategy_module == "src"


def test_fixed_task_fingerprints_the_repository_around_the_editable_strategy() -> None:
    task = ResearchTask.from_mapping(fixed_task())

    assert task.evaluator_contract_paths == ["."]


@pytest.mark.parametrize(
    ("task_name", "expected_constraints"),
    [
        (
            "active_etf_rerank_topk.toml",
            {
                "annual_return": {"operator": ">=", "threshold": 0.03},
                "max_drawdown": {"operator": "abs<=", "threshold": 0.20},
                "avg_turnover": {"operator": "<=", "threshold": 0.35},
            },
        ),
        (
            "active_etf_sharpe.toml",
            {
                "annual_return": {"operator": ">=", "threshold": 0.05},
                "max_drawdown": {"operator": "abs<=", "threshold": 0.15},
                "avg_turnover": {"operator": "<=", "threshold": 0.30},
            },
        ),
        (
            "liquid_etf_rerank_topk.toml",
            {
                "annual_return": {"operator": ">=", "threshold": 0.03},
                "max_drawdown": {"operator": "abs<=", "threshold": 0.20},
                "avg_turnover": {"operator": "<=", "threshold": 0.35},
            },
        ),
        (
            "sharpe_corr_threshold_optimization.toml",
            {
                "annual_return": {"operator": ">=", "threshold": 0.05},
                "max_drawdown": {"operator": "abs<=", "threshold": 0.15},
                "avg_turnover": {"operator": "<=", "threshold": 0.30},
            },
        ),
    ],
)
def test_repository_walk_forward_tasks_use_shared_parameter_selection(
    task_name: str,
    expected_constraints: dict[str, dict[str, float | str]],
) -> None:
    task = ResearchTask.load(REPOSITORY_ROOT / "tasks" / task_name)

    assert task.evaluation_mode == "walk_forward"
    assert task.parameter_selection is not None
    assert task.parameter_selection["objective"] == "sharpe"
    assert task.parameter_selection["constraints"] == expected_constraints
    assert task.raw["evaluation"]["acceptance"]["minimum_improvement"] == 0.03
    assert "objective" not in task.raw["evaluation"]
    assert "constraints" not in task.raw["evaluation"]
    assert task.relative_period_config == {
        "anchor": "latest_complete_universe_date",
        "development_months": 30,
        "gate_months": 12,
        "guard_months": 6,
    }
    if task.production is not None:
        assert not {
            "schedule",
            "train_months",
            "objective",
            "constraints",
            "max_parameter_sets",
        } & set(task.production)


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


def test_task_requires_positive_command_timeout() -> None:
    payload = fixed_task()
    payload["execution"]["command_timeout_minutes"] = 0

    with pytest.raises(ValueError, match="execution.command_timeout_minutes"):
        ResearchTask.from_mapping(payload)


def test_task_requires_non_empty_opencode_variant_when_provided() -> None:
    payload = fixed_task()
    payload["opencode"]["variant"] = ""

    with pytest.raises(ValueError, match="opencode.variant"):
        ResearchTask.from_mapping(payload)


def test_task_allows_model_without_variant() -> None:
    payload = fixed_task()
    payload["opencode"] = {"model": "opencode/hy3-free"}

    ResearchTask.from_mapping(payload)


def test_task_supports_legacy_opencode_timeout() -> None:
    payload = fixed_task()
    payload.pop("execution")
    payload["budget"].pop("round_minutes")
    payload["opencode"]["timeout_minutes"] = 60

    task = ResearchTask.from_mapping(payload)

    assert task.command_timeout_minutes == 60
    assert task.round_timeout_minutes == 60


def test_task_rejects_legacy_and_command_timeouts_together() -> None:
    payload = fixed_task()
    payload["opencode"]["timeout_minutes"] = 60

    with pytest.raises(ValueError, match="cannot be combined"):
        ResearchTask.from_mapping(payload)


def test_new_execution_contract_requires_explicit_round_timeout() -> None:
    payload = fixed_task()
    payload["budget"].pop("round_minutes")

    with pytest.raises(ValueError, match="budget.round_minutes is required"):
        ResearchTask.from_mapping(payload)


def test_active_etf_sharpe_has_distinct_round_and_command_timeouts() -> None:
    task = ResearchTask.load(REPOSITORY_ROOT / "tasks/active_etf_sharpe.toml")

    assert task.round_timeout_minutes == 30
    assert task.command_timeout_minutes == 10


def test_task_supports_explicit_strategy_metadata() -> None:
    payload = fixed_task()
    payload["strategy"] = {
        "name": "etf-momentum",
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
    }

    with pytest.raises(ValueError, match="must reference"):
        ResearchTask.from_mapping(payload)


def test_task_rejects_strategy_path_that_is_not_a_python_module() -> None:
    payload = fixed_task()
    payload["strategy"] = {"name": "etf-momentum"}
    payload["scope"]["editable"] = ["src/quant_core/strategy/etf-momentum.txt"]
    payload["commands"]["backtest"].append("{strategy_module}")

    with pytest.raises(ValueError, match="Python module"):
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

    task = ResearchTask.from_mapping(payload)

    assert task.evaluation_mode == "walk_forward"


def test_guard_period_must_follow_gate_period() -> None:
    payload = fixed_task()
    payload["evaluation"]["fixed"]["guard"]["start"] = "2024-01-01"

    with pytest.raises(ValueError, match="must not overlap"):
        ResearchTask.from_mapping(payload)


def test_task_allows_no_guard_period() -> None:
    payload = fixed_task()
    del payload["evaluation"]["guard"]
    del payload["evaluation"]["fixed"]["guard"]

    ResearchTask.from_mapping(payload)


def test_task_rejects_obsolete_test_contract_and_invalid_guard() -> None:
    payload = fixed_task()
    payload["evaluation"]["test"] = {"start": "2025-01-01", "end": "2025-12-31"}
    with pytest.raises(ValueError, match="evaluation.test is obsolete"):
        ResearchTask.from_mapping(payload)

    payload = fixed_task()
    payload["evaluation"]["guard"]["benchmark"] = "csi300"
    with pytest.raises(ValueError, match="benchmark"):
        ResearchTask.from_mapping(payload)

    payload = fixed_task()
    payload["evaluation"]["guard"]["max_excess_annual_return_degradation"] = -0.01
    with pytest.raises(ValueError, match="between 0 and 1"):
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


def test_fixed_task_accepts_custom_backtest_metrics() -> None:
    payload = fixed_task()
    payload["evaluation"]["objective"] = "custom_quality"
    payload["evaluation"]["constraints"] = {
        "custom_risk": {"operator": "<=", "threshold": 0.20},
    }

    task = ResearchTask.from_mapping(payload)

    assert task.objective == "custom_quality"
    assert "custom_risk" in task.constraints


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
