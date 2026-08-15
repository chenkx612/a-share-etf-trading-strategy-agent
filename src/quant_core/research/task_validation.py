"""Section-oriented validation for the versioned ``task.toml`` contract."""

from __future__ import annotations

import math
import re
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


@dataclass(frozen=True)
class TaskPolicySections:
    strategy: Mapping[str, Any] | None
    parameter_selection: Mapping[str, Any] | None


def required(
    data: Mapping[str, Any],
    key: str,
    expected: type,
    context: str,
) -> Any:
    value = data.get(key)
    if not isinstance(value, expected) or (expected is str and not value.strip()):
        raise ValueError(f"{context}.{key} must be a non-empty {expected.__name__}")
    return value


def validate_objective_and_constraints(
    policy: Mapping[str, Any],
    context: str,
    *,
    restrict_metrics: bool = False,
) -> tuple[str, Mapping[str, Any]]:
    objective = required(policy, "objective", str, context)
    if restrict_metrics and objective not in SUPPORTED_BACKTEST_METRICS:
        raise ValueError(f"{context}.objective is not a supported backtest metric")
    constraints = required(policy, "constraints", dict, context)
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


def validate_task_policy_sections(data: Mapping[str, Any]) -> TaskPolicySections:
    task_id = required(data, "id", str, "task")
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
    required(data, "goal", str, "task")
    budget = required(data, "budget", dict, "task")
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
        raise ValueError(
            "task.budget.round_minutes must be a positive integer when present"
        )

    opencode = required(data, "opencode", dict, "task")
    model = required(opencode, "model", str, "task.opencode")
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
            raise ValueError("task.budget.round_minutes is required with task.execution")

    source = required(data, "data", dict, "task")
    required(source, "universe", str, "task.data")
    if "aliases" in data:
        raise ValueError("task.aliases is not supported; use task.id directly")

    strategy = data.get("strategy")
    if strategy is not None:
        if not isinstance(strategy, dict) or set(strategy) != {"name"}:
            raise ValueError(
                "task.strategy must contain exactly name; module is derived "
                "from task.scope.editable"
            )
        required(strategy, "name", str, "task.strategy")

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
            required(
                parameter_selection,
                "schedule",
                dict,
                "task.parameter_selection",
            ),
            context="task.parameter_selection.schedule",
            require_start=True,
        )
        validate_objective_and_constraints(
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
        benchmark = required(production, "benchmark", str, "task.production")
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

    return TaskPolicySections(strategy, parameter_selection)


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


def _string_list(data: Mapping[str, Any], key: str, *, required_value: bool) -> None:
    value = data.get(key)
    if value is None and not required_value:
        return
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(f"{key} must be a non-empty list of strings")


def module_from_strategy_path(path: str) -> str:
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
        raise ValueError("task.scope.editable must map to a valid Python module path")
    return ".".join(module_parts)


def validate_task_scope_and_evaluation(
    data: Mapping[str, Any],
    policy: TaskPolicySections,
) -> None:
    strategy = policy.strategy
    parameter_selection = policy.parameter_selection
    scope = required(data, "scope", dict, "task")
    if set(scope) != {"editable"}:
        if "forbidden" in scope:
            raise ValueError(
                "task.scope.forbidden is Harness-owned; declare only editable"
            )
        raise ValueError("task.scope must contain exactly editable")
    _string_list(scope, "editable", required_value=True)
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
        module_from_strategy_path(editable[0])

    baseline = data.get("baseline")
    if baseline is not None:
        if not isinstance(baseline, dict) or not set(baseline) <= {"mode", "exclude"}:
            raise ValueError("task.baseline may contain only mode and exclude")
        if baseline.get("mode") not in {"workspace", "none"}:
            raise ValueError("task.baseline.mode must be 'workspace' or 'none'")
        _string_list(baseline, "exclude", required_value=False)
        if baseline.get("exclude") and baseline.get("mode") != "none":
            raise ValueError("task.baseline.exclude requires mode = 'none'")

    evaluation = required(data, "evaluation", dict, "task")
    mode = required(evaluation, "mode", str, "task.evaluation")
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
            "walk_forward objective and constraints belong in task.parameter_selection"
        )
    if "contract" in evaluation:
        raise ValueError(
            "task.evaluation.contract is Harness-owned and must not be configured"
        )

    commands = required(data, "commands", dict, "task")
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
    _string_list(commands, "tests", required_value=True)
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
    _string_list(commands, "backtest", required_value=mode == "fixed")
    if mode == "fixed":
        backtest_template = " ".join(commands["backtest"])
        for placeholder in ("{start}", "{end}", "{run_id}"):
            if placeholder not in backtest_template:
                raise ValueError(
                    f"task.commands.backtest must contain {placeholder}"
                )
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
        validate_objective_and_constraints(evaluation, "task.evaluation")
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
        raise ValueError(
            "task.evaluation.test is obsolete; configure task.evaluation.guard"
        )
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
        degradation = guard_config.get("max_excess_annual_return_degradation")
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
    periods = required(evaluation, periods_key, dict, "task.evaluation")
    if mode == "walk_forward":
        absolute_keys = {"development", "gate"}
        if set(periods) == {"relative"}:
            relative = required(
                periods,
                "relative",
                dict,
                "task.evaluation.walk_forward",
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
            return
        expected_absolute = absolute_keys | (
            {"guard"} if guard_config is not None else set()
        )
        if set(periods) != expected_absolute:
            raise ValueError(
                "task.evaluation.walk_forward must contain exactly development, "
                "gate, and configured guard, or exactly relative; parameter "
                "search policy belongs in task.parameter_selection"
            )
    else:
        expected_fixed = {"development", "gate"} | (
            {"guard"} if guard_config is not None else set()
        )
        if set(periods) != expected_fixed:
            raise ValueError(
                "task.evaluation.fixed must contain exactly development, gate, "
                "and configured guard"
            )
    development = _period(
        required(periods, "development", dict, f"task.evaluation.{periods_key}"),
        f"task.evaluation.{periods_key}.development",
    )
    gate = _period(
        required(periods, "gate", dict, f"task.evaluation.{periods_key}"),
        f"task.evaluation.{periods_key}.gate",
    )
    if development[1] >= gate[0]:
        raise ValueError("fixed development and gate periods must not overlap")
    if guard_config is not None:
        guard_period = _period(
            periods.get("guard"),
            f"task.evaluation.{periods_key}.guard",
        )
        if gate[1] >= guard_period[0]:
            raise ValueError("gate and guard periods must not overlap")
