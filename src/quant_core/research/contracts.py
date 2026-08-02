from __future__ import annotations

import json
import math
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from quant_core.schedule import validate_schedule


SUPPORTED_BACKTEST_METRICS = {
    "total_return",
    "annual_return",
    "annual_volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "avg_turnover",
}

METRICS_PATH_TEMPLATE = "outputs/backtests/{run_id}/metrics.json"

EVALUATOR_CONTRACT_BASE_PATHS = (
    "pyproject.toml",
    "src/quant_core",
    "tests",
)


def _required(data: Mapping[str, Any], key: str, expected: type, context: str) -> Any:
    value = data.get(key)
    if not isinstance(value, expected) or (expected is str and not value.strip()):
        raise ValueError(f"{context}.{key} must be a non-empty {expected.__name__}")
    return value


def _date(value: Any, context: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO date string") from exc


def _period(data: Mapping[str, Any], context: str) -> tuple[date, date]:
    start = _date(data.get("start"), f"{context}.start")
    end = _date(data.get("end"), f"{context}.end")
    if start > end:
        raise ValueError(f"{context}.start must not be after end")
    return start, end


def _validate_objective_and_constraints(
    policy: Mapping[str, Any],
    context: str,
    *,
    restrict_metrics: bool = False,
) -> tuple[str, Mapping[str, Any]]:
    objective = _required(policy, "objective", str, context)
    if restrict_metrics and objective not in SUPPORTED_BACKTEST_METRICS:
        raise ValueError(f"{context}.objective is not a supported backtest metric")
    constraints = _required(policy, "constraints", dict, context)
    if not constraints:
        raise ValueError(f"{context}.constraints must not be empty")
    for name, constraint in constraints.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{context}.constraints must use non-empty metric names")
        if restrict_metrics and name not in SUPPORTED_BACKTEST_METRICS:
            raise ValueError(
                f"{context}.constraints.{name} is not a supported backtest metric"
            )
        if not isinstance(constraint, dict):
            raise ValueError(
                f"{context}.constraints.{name} must be an operator/threshold table"
            )
        if set(constraint) != {"operator", "threshold"}:
            raise ValueError(
                f"{context}.constraints.{name} must contain exactly operator and threshold"
            )
        operator = constraint.get("operator")
        if operator not in {">=", "<=", "abs<="}:
            raise ValueError(
                f"{context}.constraints.{name}.operator must be one of >=, <=, abs<="
            )
        threshold = constraint.get("threshold")
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(float(threshold))
        ):
            raise ValueError(
                f"{context}.constraints.{name}.threshold must be numeric and finite"
            )
    return objective, constraints


@dataclass(frozen=True)
class ResearchTask:
    raw: Mapping[str, Any]
    resolved_periods: Mapping[str, Any] | None = None
    period_resolution: Mapping[str, Any] | None = None

    @classmethod
    def load(cls, path: str | Path) -> ResearchTask:
        with Path(path).open("rb") as handle:
            return cls.from_mapping(tomllib.load(handle))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ResearchTask:
        task_id = _required(data, "id", str, "task")
        name_words = [word for word in re.split(r"[-_.]+", task_id) if word]
        long_name_reason = data.get("long_name_reason")
        if len(name_words) > 3 and (
            not isinstance(long_name_reason, str) or not long_name_reason.strip()
        ):
            raise ValueError(
                "task.id must contain at most three words separated by '-', '_' or '.'; "
                "exceptional longer names require a non-empty task.long_name_reason"
            )
        if long_name_reason is not None and (
            not isinstance(long_name_reason, str) or not long_name_reason.strip()
        ):
            raise ValueError("task.long_name_reason must be a non-empty string when present")
        _required(data, "goal", str, "task")
        budget = _required(data, "budget", dict, "task")
        for key in ("max_rounds", "max_hours", "max_consecutive_failures"):
            value = budget.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"task.budget.{key} must be a positive integer")
        round_minutes = budget.get("round_minutes")
        if round_minutes is not None and (
            not isinstance(round_minutes, int)
            or isinstance(round_minutes, bool)
            or round_minutes < 1
        ):
            raise ValueError("task.budget.round_minutes must be a positive integer when present")

        opencode = _required(data, "opencode", dict, "task")
        model = _required(opencode, "model", str, "task.opencode")
        provider, separator, model_name = model.partition("/")
        if not separator or not provider or not model_name:
            raise ValueError("task.opencode.model must use provider/model format")
        variant = opencode.get("variant")
        if variant is not None and (not isinstance(variant, str) or not variant.strip()):
            raise ValueError("task.opencode.variant must be a non-empty string when provided")
        legacy_timeout_minutes = opencode.get("timeout_minutes")
        execution = data.get("execution")
        if execution is None:
            if (
                not isinstance(legacy_timeout_minutes, int)
                or isinstance(legacy_timeout_minutes, bool)
                or legacy_timeout_minutes < 1
            ):
                raise ValueError(
                    "task.execution.command_timeout_minutes must be a positive integer"
                )
        else:
            if not isinstance(execution, dict):
                raise ValueError("task.execution must be a table")
            command_timeout_minutes = execution.get("command_timeout_minutes")
            if (
                not isinstance(command_timeout_minutes, int)
                or isinstance(command_timeout_minutes, bool)
                or command_timeout_minutes < 1
            ):
                raise ValueError(
                    "task.execution.command_timeout_minutes must be a positive integer"
                )
            if legacy_timeout_minutes is not None:
                raise ValueError(
                    "task.opencode.timeout_minutes is legacy and cannot be combined with "
                    "task.execution.command_timeout_minutes"
                )
            if round_minutes is None:
                raise ValueError(
                    "task.budget.round_minutes is required with task.execution"
                )

        source = _required(data, "data", dict, "task")
        _required(source, "universe", str, "task.data")

        if "aliases" in data:
            raise ValueError("task.aliases is not supported; use task.id directly")

        strategy = data.get("strategy")
        if strategy is not None:
            if not isinstance(strategy, dict) or set(strategy) != {"name"}:
                raise ValueError(
                    "task.strategy must contain exactly name; module is derived "
                    "from task.scope.editable"
                )
            _required(strategy, "name", str, "task.strategy")

        parameter_selection = data.get("parameter_selection")
        if parameter_selection is not None:
            if not isinstance(parameter_selection, dict):
                raise ValueError("task.parameter_selection must be a table")
            expected_selection = {
                "schedule",
                "train_months",
                "objective",
                "constraints",
                "max_parameter_sets",
            }
            if set(parameter_selection) != expected_selection:
                raise ValueError(
                    "task.parameter_selection must contain exactly schedule, train_months, "
                    "objective, constraints, and max_parameter_sets"
                )
            for key in ("train_months", "max_parameter_sets"):
                value = parameter_selection.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ValueError(
                        f"task.parameter_selection.{key} must be a positive integer"
                    )
            validate_schedule(
                _required(
                    parameter_selection,
                    "schedule",
                    dict,
                    "task.parameter_selection",
                ),
                context="task.parameter_selection.schedule",
                require_start=True,
            )
            _validate_objective_and_constraints(
                parameter_selection,
                "task.parameter_selection",
                restrict_metrics=True,
            )

        production = data.get("production")
        if production is not None:
            if strategy is None:
                raise ValueError("task.strategy is required when production is configured")
            if not isinstance(production, dict):
                raise ValueError("task.production must be a table")
            expected = {"curve_months", "benchmark"}
            optional = {"data_requirements"}
            if not expected.issubset(production) or set(production) - expected - optional:
                raise ValueError(
                    "task.production must contain curve_months and benchmark, with optional "
                    "data_requirements; parameter search policy belongs in "
                    "task.parameter_selection"
                )
            if parameter_selection is None:
                raise ValueError(
                    "task.parameter_selection is required when production is configured"
                )
            curve_months = production.get("curve_months")
            if (
                not isinstance(curve_months, int)
                or isinstance(curve_months, bool)
                or curve_months < 1
            ):
                raise ValueError("task.production.curve_months must be a positive integer")
            benchmark = _required(production, "benchmark", str, "task.production")
            if not benchmark.isdigit():
                raise ValueError("task.production.benchmark must be a numeric security code")
            requirements = production.get("data_requirements")
            if requirements is not None:
                if not isinstance(requirements, dict) or set(requirements) != {
                    "required_columns",
                    "min_history",
                }:
                    raise ValueError(
                        "task.production.data_requirements must contain exactly "
                        "required_columns and min_history"
                    )
                columns = requirements.get("required_columns")
                if (
                    not isinstance(columns, list)
                    or not columns
                    or not all(isinstance(column, str) and column for column in columns)
                    or len(set(columns)) != len(columns)
                ):
                    raise ValueError(
                        "task.production.data_requirements.required_columns must be "
                        "unique non-empty strings"
                    )
                if not {"date", "symbol", "open", "close"}.issubset(columns):
                    raise ValueError(
                        "task.production.data_requirements.required_columns must include "
                        "date, symbol, open, and close"
                    )
                min_history = requirements.get("min_history")
                if (
                    not isinstance(min_history, int)
                    or isinstance(min_history, bool)
                    or min_history < 1
                ):
                    raise ValueError(
                        "task.production.data_requirements.min_history must be a "
                        "positive integer"
                    )

        scope = _required(data, "scope", dict, "task")
        if set(scope) != {"editable"}:
            if "forbidden" in scope:
                raise ValueError(
                    "task.scope.forbidden is Harness-owned; declare only editable"
                )
            raise ValueError("task.scope must contain exactly editable")
        cls._string_list(scope, "editable", required=True)
        editable = scope["editable"]
        if len(editable) != 1:
            raise ValueError("task.scope.editable must contain exactly one strategy script")
        strategy_path = Path(editable[0])
        if (
            strategy_path.is_absolute()
            or ".." in strategy_path.parts
            or editable[0].endswith("/")
        ):
            raise ValueError("task.scope.editable must be a repository-relative file path")
        if strategy is not None:
            cls._module_from_strategy_path(editable[0])

        baseline = data.get("baseline")
        if baseline is not None:
            if not isinstance(baseline, dict) or not set(baseline) <= {"mode", "exclude"}:
                raise ValueError("task.baseline may contain only mode and exclude")
            if baseline.get("mode") not in {"workspace", "none"}:
                raise ValueError("task.baseline.mode must be 'workspace' or 'none'")
            cls._string_list(baseline, "exclude", required=False)
            if baseline.get("exclude") and baseline.get("mode") != "none":
                raise ValueError("task.baseline.exclude requires mode = 'none'")

        evaluation = _required(data, "evaluation", dict, "task")
        mode = _required(evaluation, "mode", str, "task.evaluation")
        if mode not in {"fixed", "walk_forward"}:
            raise ValueError("task.evaluation.mode must be 'fixed' or 'walk_forward'")
        if mode == "walk_forward" and strategy is None:
            raise ValueError("task.strategy is required for walk_forward evaluation")
        if mode == "walk_forward" and parameter_selection is None:
            raise ValueError(
                "task.parameter_selection is required for walk_forward evaluation"
            )
        if mode == "walk_forward" and (
            "objective" in evaluation or "constraints" in evaluation
        ):
            raise ValueError(
                "walk_forward objective and constraints belong in "
                "task.parameter_selection"
            )
        if "contract" in evaluation:
            raise ValueError(
                "task.evaluation.contract is Harness-owned and must not be configured"
            )

        commands = _required(data, "commands", dict, "task")
        allowed_commands = {"tests", "backtest"} if mode == "fixed" else {"tests"}
        if set(commands) != allowed_commands:
            legacy = set(commands) & {"test", "metrics_path"}
            if legacy:
                fields = ", ".join(sorted(legacy))
                raise ValueError(
                    f"task.commands fields are Harness-owned or obsolete: {fields}"
                )
            if mode == "walk_forward" and "backtest" in commands:
                raise ValueError(
                    "task.commands.backtest is Harness-owned for walk_forward evaluation"
                )
            raise ValueError(
                f"task.commands must contain exactly {sorted(allowed_commands)}"
            )
        cls._string_list(commands, "tests", required=True)
        for test_path in commands["tests"]:
            path_part = test_path.split("::", 1)[0]
            normalized = PurePosixPath(path_part)
            if (
                "\\" in test_path
                or test_path != test_path.strip()
                or normalized.is_absolute()
                or ".." in normalized.parts
                or not path_part.startswith("tests/")
                or not path_part.endswith(".py")
            ):
                raise ValueError(
                    "task.commands.tests must contain repository-relative "
                    "pytest files or nodes below tests/"
                )
        cls._string_list(commands, "backtest", required=mode == "fixed")
        if mode == "fixed":
            backtest_template = " ".join(commands["backtest"])
            for placeholder in ("{start}", "{end}", "{run_id}"):
                if placeholder not in backtest_template:
                    raise ValueError(f"task.commands.backtest must contain {placeholder}")
            strategy_placeholders = ("{strategy_name}", "{strategy_module}")
            if strategy is None and any(
                placeholder in backtest_template for placeholder in strategy_placeholders
            ):
                raise ValueError(
                    "task.strategy is required when task.commands.backtest uses a strategy placeholder"
                )
            if strategy is not None and not any(
                placeholder in backtest_template for placeholder in strategy_placeholders
            ):
                raise ValueError(
                    "task.commands.backtest must reference {strategy_name} or {strategy_module} "
                    "when task.strategy is configured"
                )

        if mode == "fixed":
            _validate_objective_and_constraints(evaluation, "task.evaluation")
        acceptance = evaluation.get("acceptance")
        if acceptance is not None:
            if not isinstance(acceptance, dict):
                raise ValueError("task.evaluation.acceptance must be a table")
            minimum = acceptance.get("minimum_improvement", 0.0)
            if (
                not isinstance(minimum, (int, float))
                or isinstance(minimum, bool)
                or not math.isfinite(float(minimum))
                or minimum < 0
            ):
                raise ValueError(
                    "task.evaluation.acceptance.minimum_improvement must be finite and non-negative"
                )
        if "test" in evaluation:
            raise ValueError("task.evaluation.test is obsolete; configure task.evaluation.guard")
        guard_config = evaluation.get("guard")
        if guard_config is not None:
            if not isinstance(guard_config, dict) or set(guard_config) != {
                "benchmark",
                "max_excess_annual_return_degradation",
            }:
                raise ValueError(
                    "task.evaluation.guard must contain exactly benchmark and "
                    "max_excess_annual_return_degradation"
                )
            if guard_config.get("benchmark") != "universe_equal_weight":
                raise ValueError(
                    "task.evaluation.guard.benchmark must be universe_equal_weight"
                )
            degradation = guard_config.get(
                "max_excess_annual_return_degradation"
            )
            if (
                not isinstance(degradation, (int, float))
                or isinstance(degradation, bool)
                or not math.isfinite(float(degradation))
                or not 0.0 <= float(degradation) <= 1.0
            ):
                raise ValueError(
                    "task.evaluation.guard.max_excess_annual_return_degradation "
                    "must be finite and between 0 and 1"
                )
        target = evaluation.get("target")
        if target is not None:
            if not isinstance(target, dict):
                raise ValueError("task.evaluation.target must be a table")
            threshold = target.get("objective_at_least")
            if (
                not isinstance(threshold, (int, float))
                or isinstance(threshold, bool)
                or not math.isfinite(float(threshold))
            ):
                raise ValueError(
                    "task.evaluation.target.objective_at_least must be numeric and finite"
                )
        periods_key = "fixed" if mode == "fixed" else "walk_forward"
        periods = _required(evaluation, periods_key, dict, "task.evaluation")
        if mode == "walk_forward":
            absolute_keys = {"development", "gate"}
            if set(periods) == {"relative"}:
                relative = _required(
                    periods, "relative", dict, "task.evaluation.walk_forward"
                )
                allowed = {
                    "anchor",
                    "development_months",
                    "gate_months",
                    "guard_months",
                }
                if not {"anchor", "development_months", "gate_months"} <= set(relative):
                    raise ValueError(
                        "task.evaluation.walk_forward.relative must contain anchor, "
                        "development_months, and gate_months"
                    )
                if not set(relative) <= allowed:
                    raise ValueError(
                        "task.evaluation.walk_forward.relative contains unknown fields"
                    )
                if relative["anchor"] != "latest_complete_universe_date":
                    raise ValueError(
                        "task.evaluation.walk_forward.relative.anchor must be "
                        "latest_complete_universe_date"
                    )
                if "test_months" in relative:
                    raise ValueError(
                        "task.evaluation.walk_forward.relative.test_months is obsolete; "
                        "use guard_months"
                    )
                for key in ("development_months", "gate_months", "guard_months"):
                    if key not in relative:
                        continue
                    value = relative[key]
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value <= 0
                    ):
                        raise ValueError(
                            f"task.evaluation.walk_forward.relative.{key} "
                            "must be a positive integer"
                        )
                if ("guard_months" in relative) != (guard_config is not None):
                    raise ValueError(
                        "relative walk-forward guard requires both guard_months and "
                        "task.evaluation.guard"
                    )
                return cls(raw=dict(data))
            expected_absolute = absolute_keys | ({"guard"} if guard_config is not None else set())
            if set(periods) != expected_absolute:
                raise ValueError(
                    "task.evaluation.walk_forward must contain exactly development, "
                    "gate, and configured guard, or exactly relative; parameter "
                    "search policy belongs in task.parameter_selection"
                )
        elif mode == "fixed":
            expected_fixed = {"development", "gate"} | (
                {"guard"} if guard_config is not None else set()
            )
            if set(periods) != expected_fixed:
                raise ValueError(
                    "task.evaluation.fixed must contain exactly development, gate, "
                    "and configured guard"
                )
        development = _period(
            _required(periods, "development", dict, f"task.evaluation.{periods_key}"),
            f"task.evaluation.{periods_key}.development",
        )
        gate = _period(
            _required(periods, "gate", dict, f"task.evaluation.{periods_key}"),
            f"task.evaluation.{periods_key}.gate",
        )
        if development[1] >= gate[0]:
            raise ValueError("fixed development and gate periods must not overlap")

        guard = periods.get("guard")
        if guard_config is not None:
            guard_period = _period(guard, f"task.evaluation.{periods_key}.guard")
            if gate[1] >= guard_period[0]:
                raise ValueError("gate and guard periods must not overlap")

        return cls(raw=dict(data))

    @staticmethod
    def _string_list(data: Mapping[str, Any], key: str, *, required: bool) -> None:
        value = data.get(key)
        if value is None and not required:
            return
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"{key} must be a non-empty list of strings")

    @staticmethod
    def _module_from_strategy_path(path: str) -> str:
        strategy_path = PurePosixPath(path)
        if strategy_path.suffix != ".py":
            raise ValueError(
                "task.scope.editable must be a Python module file "
                "when task.strategy is configured"
            )
        module_parts = [*strategy_path.parts[:-1], strategy_path.stem]
        if len(module_parts) > 1 and module_parts[0] == "src":
            module_parts = module_parts[1:]
        if not all(part.isidentifier() for part in module_parts):
            raise ValueError(
                "task.scope.editable must map to a valid Python module path"
            )
        return ".".join(module_parts)

    @property
    def task_id(self) -> str:
        return str(self.raw["id"])

    @property
    def command_timeout_minutes(self) -> int:
        execution = self.raw.get("execution")
        if isinstance(execution, Mapping):
            return int(execution["command_timeout_minutes"])
        return int(self.raw["opencode"]["timeout_minutes"])

    @property
    def round_timeout_minutes(self) -> int:
        round_minutes = self.raw["budget"].get("round_minutes")
        if round_minutes is not None:
            return int(round_minutes)
        return int(self.raw["opencode"]["timeout_minutes"])

    @property
    def evaluation_mode(self) -> str:
        return str(self.raw["evaluation"]["mode"])

    @property
    def parameter_selection(self) -> Mapping[str, Any] | None:
        value = self.raw.get("parameter_selection")
        return value if isinstance(value, Mapping) else None

    @property
    def objective(self) -> str:
        policy = (
            self.parameter_selection
            if self.evaluation_mode == "walk_forward"
            else self.raw["evaluation"]
        )
        assert policy is not None
        return str(policy["objective"])

    @property
    def constraints(self) -> Mapping[str, Any]:
        policy = (
            self.parameter_selection
            if self.evaluation_mode == "walk_forward"
            else self.raw["evaluation"]
        )
        assert policy is not None
        value = policy["constraints"]
        assert isinstance(value, Mapping)
        return value

    @property
    def evaluation_periods(self) -> Mapping[str, Any]:
        if self.relative_period_config is not None:
            if self.resolved_periods is None:
                raise RuntimeError(
                    "relative evaluation periods must be resolved against a frozen "
                    "runtime input snapshot"
                )
            return {
                key: value
                for key, value in self.resolved_periods.items()
                if key in {"development", "gate", "guard"}
            }
        evaluation = self.raw["evaluation"]
        return evaluation["fixed" if self.evaluation_mode == "fixed" else "walk_forward"]

    @property
    def relative_period_config(self) -> Mapping[str, Any] | None:
        if self.evaluation_mode != "walk_forward":
            return None
        value = self.raw["evaluation"]["walk_forward"].get("relative")
        return value if isinstance(value, Mapping) else None

    def with_resolved_periods(
        self,
        periods: Mapping[str, Any],
        resolution: Mapping[str, Any],
    ) -> ResearchTask:
        if self.relative_period_config is None:
            raise ValueError("only relative tasks can bind resolved periods")
        expected = {"development", "gate"}
        if "guard_months" in self.relative_period_config:
            expected.add("guard")
        if set(periods) != expected:
            raise ValueError(
                f"resolved periods must contain exactly {sorted(expected)}"
            )
        development = _period(
            _required(periods, "development", dict, "resolved periods"),
            "resolved periods.development",
        )
        gate = _period(
            _required(periods, "gate", dict, "resolved periods"),
            "resolved periods.gate",
        )
        if development[1] >= gate[0]:
            raise ValueError("resolved development and gate periods must not overlap")
        if "guard" in expected:
            guard = _period(
                _required(periods, "guard", dict, "resolved periods"),
                "resolved periods.guard",
            )
            if gate[1] >= guard[0]:
                raise ValueError("resolved gate and guard periods must not overlap")
        return ResearchTask(
            raw=self.raw,
            resolved_periods=dict(periods),
            period_resolution=dict(resolution),
        )

    @property
    def development_period(self) -> Mapping[str, Any]:
        return self.evaluation_periods["development"]

    @property
    def gate_period(self) -> Mapping[str, Any]:
        return self.evaluation_periods["gate"]

    @property
    def guard_period(self) -> Mapping[str, Any] | None:
        if self.raw["evaluation"].get("guard") is None:
            return None
        value = self.evaluation_periods.get("guard")
        return value if isinstance(value, Mapping) else None

    @property
    def guard_config(self) -> Mapping[str, Any] | None:
        value = self.raw["evaluation"].get("guard")
        return value if isinstance(value, Mapping) else None

    @property
    def baseline_mode(self) -> str:
        return str(self.raw.get("baseline", {}).get("mode", "workspace"))

    @property
    def baseline_exclude(self) -> list[str]:
        return list(self.raw.get("baseline", {}).get("exclude", []))

    @property
    def strategy_path(self) -> str:
        return str(self.raw["scope"]["editable"][0])

    @property
    def evaluator_contract_paths(self) -> list[str]:
        if (
            self.evaluation_mode == "fixed"
            or not self.strategy_path.startswith("src/quant_core/strategy/")
        ):
            return ["."]
        paths = [
            *EVALUATOR_CONTRACT_BASE_PATHS,
            str(self.raw["data"]["universe"]),
        ]
        return list(dict.fromkeys(paths))

    @property
    def strategy_name(self) -> str | None:
        strategy = self.raw.get("strategy")
        return str(strategy["name"]) if isinstance(strategy, Mapping) else None

    @property
    def strategy_module(self) -> str | None:
        strategy = self.raw.get("strategy")
        return (
            self._module_from_strategy_path(self.strategy_path)
            if isinstance(strategy, Mapping)
            else None
        )

    @property
    def test_paths(self) -> list[str]:
        return list(self.raw["commands"]["tests"])

    @property
    def metrics_path_template(self) -> str:
        return METRICS_PATH_TEMPLATE

    @property
    def production(self) -> Mapping[str, Any] | None:
        value = self.raw.get("production")
        return value if isinstance(value, Mapping) else None


@dataclass(frozen=True)
class ExperimentResult:
    raw: Mapping[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> ExperimentResult:
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ExperimentResult:
        _required(data, "experiment_id", str, "result")
        status = _required(data, "status", str, "result")
        if status not in {"completed", "failed"}:
            raise ValueError("result.status must be 'completed' or 'failed'")
        failure_kind = data.get("failure_kind")
        if failure_kind is not None and failure_kind != "infrastructure":
            raise ValueError("result.failure_kind must be 'infrastructure' when present")
        if status == "completed" and failure_kind is not None:
            raise ValueError("completed result must not declare failure_kind")
        failure_code = data.get("failure_code")
        if failure_code is not None and (
            failure_kind != "infrastructure"
            or not isinstance(failure_code, str)
            or not failure_code.strip()
        ):
            raise ValueError(
                "result.failure_code must be a non-empty string for infrastructure failures"
            )
        environment_sha256 = data.get("evaluation_environment_sha256")
        if environment_sha256 is not None and (
            not isinstance(environment_sha256, str)
            or len(environment_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in environment_sha256
            )
        ):
            raise ValueError(
                "result.evaluation_environment_sha256 must be a SHA-256 digest"
            )
        development_view_sha256 = data.get("development_view_sha256")
        development_end = data.get("development_end")
        if (development_view_sha256 is None) != (development_end is None):
            raise ValueError(
                "result Development view hash and end must be declared together"
            )
        if development_view_sha256 is not None:
            if (
                not isinstance(development_view_sha256, str)
                or len(development_view_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in development_view_sha256
                )
            ):
                raise ValueError(
                    "result.development_view_sha256 must be a SHA-256 digest"
                )
            if not isinstance(development_end, str):
                raise ValueError("result.development_end must be an ISO date")
            try:
                date.fromisoformat(development_end)
            except ValueError as exc:
                raise ValueError("result.development_end must be an ISO date") from exc
        feedback = data.get("feedback")
        if feedback is not None and (not isinstance(feedback, str) or not feedback.strip()):
            raise ValueError("result.feedback must be a non-empty str when present")
        submission = data.get("submission")
        if submission is not None:
            if not isinstance(submission, dict):
                raise ValueError("result.submission must be an object")
            mode = submission.get("mode")
            expected = {
                "mode", "submitted_at", "submitted_by_timeout", "strategy_sha256",
            }
            if mode == "checkpoint":
                expected.add("checkpoint_id")
            if mode not in {"final", "checkpoint"} or set(submission) != expected:
                raise ValueError("result.submission has invalid fields")
            if not isinstance(submission.get("submitted_at"), str) or not submission["submitted_at"].strip():
                raise ValueError("result.submission.submitted_at must be a non-empty string")
            submitted_by_timeout = submission.get("submitted_by_timeout")
            if not isinstance(submitted_by_timeout, bool) or submitted_by_timeout != (mode == "checkpoint"):
                raise ValueError("result.submission timeout marker does not match its mode")
            strategy_sha256 = submission.get("strategy_sha256")
            if (
                not isinstance(strategy_sha256, str)
                or len(strategy_sha256) != 64
                or any(character not in "0123456789abcdef" for character in strategy_sha256)
            ):
                raise ValueError("result.submission.strategy_sha256 must be a SHA-256 digest")
            if mode == "checkpoint" and (
                not isinstance(submission.get("checkpoint_id"), str)
                or not submission["checkpoint_id"].isdigit()
                or int(submission["checkpoint_id"]) < 1
            ):
                raise ValueError("result.submission.checkpoint_id must be a positive numeric ID")
        round_timing = data.get("round_timing")
        if round_timing is not None:
            if not isinstance(round_timing, dict) or set(round_timing) != {
                "started_at",
                "deadline",
                "finished_at",
                "timeout_seconds",
                "duration_seconds",
            }:
                raise ValueError("result.round_timing has invalid fields")
            for key in ("started_at", "deadline", "finished_at"):
                _required(round_timing, key, str, "result.round_timing")
            timeout_seconds = round_timing["timeout_seconds"]
            if (
                not isinstance(timeout_seconds, int)
                or isinstance(timeout_seconds, bool)
                or timeout_seconds < 1
            ):
                raise ValueError("result.round_timing.timeout_seconds must be a positive integer")
            duration_seconds = round_timing["duration_seconds"]
            if (
                not isinstance(duration_seconds, (int, float))
                or isinstance(duration_seconds, bool)
                or not math.isfinite(duration_seconds)
                or duration_seconds < 0
            ):
                raise ValueError(
                    "result.round_timing.duration_seconds must be finite and non-negative"
                )
        development_attempts = data.get("development_attempts")
        if development_attempts is not None:
            if not isinstance(development_attempts, list):
                raise ValueError("result.development_attempts must be a list")
            seen_ids: set[str] = set()
            seen_hashes: set[str] = set()
            submitted = 0
            for index, attempt in enumerate(development_attempts):
                context = f"result.development_attempts[{index}]"
                legacy_fields = {
                    "attempt_id",
                    "candidate_sha256",
                    "hypothesis",
                    "development_metrics",
                    "outcome",
                    "learning",
                }
                current_fields = legacy_fields | {
                    "development_view_sha256",
                    "development_end",
                }
                if (
                    not isinstance(attempt, dict)
                    or frozenset(attempt) not in {
                        frozenset(legacy_fields),
                        frozenset(current_fields),
                    }
                    or (
                        set(attempt) == legacy_fields
                        and isinstance(data.get("development_view_sha256"), str)
                    )
                ):
                    raise ValueError(f"{context} has invalid fields")
                attempt_id = _required(attempt, "attempt_id", str, context)
                if (
                    not attempt_id.isdigit()
                    or int(attempt_id) < 1
                    or attempt_id in seen_ids
                ):
                    raise ValueError(f"{context}.attempt_id must be a unique positive numeric ID")
                seen_ids.add(attempt_id)
                digest = _required(attempt, "candidate_sha256", str, context)
                if (
                    len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    or digest in seen_hashes
                ):
                    raise ValueError(f"{context}.candidate_sha256 must be a unique SHA-256 digest")
                seen_hashes.add(digest)
                if set(attempt) == legacy_fields:
                    view_digest = None
                    attempt_end = None
                else:
                    view_digest = _required(
                        attempt, "development_view_sha256", str, context
                    )
                    attempt_end = _required(attempt, "development_end", str, context)
                if view_digest is not None and (
                    len(view_digest) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in view_digest
                    )
                ):
                    raise ValueError(
                        f"{context}.development_view_sha256 must be a SHA-256 digest"
                    )
                if attempt_end is not None:
                    try:
                        date.fromisoformat(attempt_end)
                    except ValueError as exc:
                        raise ValueError(
                            f"{context}.development_end must be an ISO date"
                        ) from exc
                if view_digest is not None and data.get("development_view_sha256") != view_digest:
                    raise ValueError(
                        f"{context} does not match the frozen Development view"
                    )
                if attempt_end is not None and data.get("development_end") != attempt_end:
                    raise ValueError(
                        f"{context} does not match the frozen Development end"
                    )
                _required(attempt, "hypothesis", str, context)
                if not isinstance(attempt.get("development_metrics"), dict):
                    raise ValueError(f"{context}.development_metrics must be an object")
                outcome = attempt.get("outcome")
                if outcome not in {"abandoned", "submitted"}:
                    raise ValueError(f"{context}.outcome is invalid")
                submitted += outcome == "submitted"
                learning = attempt.get("learning")
                if learning is not None and (
                    not isinstance(learning, str) or not learning.strip()
                ):
                    raise ValueError(f"{context}.learning must be null or a non-empty string")
            if submitted > 1:
                raise ValueError("result.development_attempts may contain only one submitted attempt")
        if status == "completed":
            _required(data, "hypothesis", str, "result")
            _required(data, "attempts", str, "result")
            _required(data, "development_effect", str, "result")
            _required(data, "candidate", str, "result")
            changes = _required(data, "changes", dict, "result")
            if set(changes) != {"files"}:
                raise ValueError("result.changes must contain exactly files")
            ResearchTask._string_list(changes, "files", required=True)

            metrics = _required(data, "metrics", dict, "result")
            if "development" not in metrics or "gate" not in metrics:
                raise ValueError("result.metrics must contain development and gate metrics")
            if "test" in metrics:
                raise ValueError("result.metrics must not contain test metrics during the research loop")
            if "guard" in metrics:
                raise ValueError("result.metrics must not contain guard metrics during the research loop")
            if not all(isinstance(metrics[key], dict) for key in ("development", "gate")):
                raise ValueError("result development and gate metrics must be objects")
        else:
            _required(data, "error", str, "result")

        return cls(raw=dict(data))

    @property
    def experiment_id(self) -> str:
        return str(self.raw["experiment_id"])
