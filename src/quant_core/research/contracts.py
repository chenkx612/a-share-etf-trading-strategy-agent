from __future__ import annotations

import json
import math
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping


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


@dataclass(frozen=True)
class ResearchTask:
    raw: Mapping[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> ResearchTask:
        with Path(path).open("rb") as handle:
            return cls.from_mapping(tomllib.load(handle))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ResearchTask:
        _required(data, "id", str, "task")
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
        timeout_minutes = opencode.get("timeout_minutes")
        if not isinstance(timeout_minutes, int) or isinstance(timeout_minutes, bool) or timeout_minutes < 1:
            raise ValueError("task.opencode.timeout_minutes must be a positive integer")

        source = _required(data, "data", dict, "task")
        _required(source, "universe", str, "task.data")

        strategy = data.get("strategy")
        if strategy is not None:
            if not isinstance(strategy, dict) or set(strategy) != {"name", "module"}:
                raise ValueError("task.strategy must contain exactly name and module")
            _required(strategy, "name", str, "task.strategy")
            module = _required(strategy, "module", str, "task.strategy")
            if not all(part.isidentifier() for part in module.split(".")):
                raise ValueError("task.strategy.module must be a Python module path")

        scope = _required(data, "scope", dict, "task")
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
        cls._string_list(scope, "forbidden", required=False)

        baseline = data.get("baseline")
        if baseline is not None:
            if not isinstance(baseline, dict) or not set(baseline) <= {"mode", "exclude"}:
                raise ValueError("task.baseline may contain only mode and exclude")
            if baseline.get("mode") not in {"workspace", "none"}:
                raise ValueError("task.baseline.mode must be 'workspace' or 'none'")
            cls._string_list(baseline, "exclude", required=False)
            if baseline.get("exclude") and baseline.get("mode") != "none":
                raise ValueError("task.baseline.exclude requires mode = 'none'")

        commands = _required(data, "commands", dict, "task")
        cls._string_list(commands, "test", required=True)
        cls._string_list(commands, "backtest", required=True)
        metrics_path = _required(commands, "metrics_path", str, "task.commands")
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
        if "{run_id}" not in metrics_path:
            raise ValueError("task.commands.metrics_path must contain {run_id}")

        evaluation = _required(data, "evaluation", dict, "task")
        mode = _required(evaluation, "mode", str, "task.evaluation")
        if mode != "fixed":
            raise ValueError("task.evaluation.mode must be 'fixed'")
        _required(evaluation, "objective", str, "task.evaluation")
        constraints = _required(evaluation, "constraints", dict, "task.evaluation")
        if not constraints:
            raise ValueError("task.evaluation.constraints must not be empty")
        for name, constraint in constraints.items():
            if not isinstance(name, str) or not name:
                raise ValueError("task.evaluation.constraints must use non-empty metric names")
            if not isinstance(constraint, dict):
                raise ValueError(
                    f"task.evaluation.constraints.{name} must be an operator/threshold table"
                )
            if set(constraint) != {"operator", "threshold"}:
                raise ValueError(
                    f"task.evaluation.constraints.{name} must contain exactly operator and threshold"
                )
            operator = constraint.get("operator")
            threshold = constraint.get("threshold")
            if operator not in {">=", "<=", "abs<="}:
                raise ValueError(
                    f"task.evaluation.constraints.{name}.operator must be one of >=, <=, abs<="
                )
            if (
                not isinstance(threshold, (int, float))
                or isinstance(threshold, bool)
                or not math.isfinite(float(threshold))
            ):
                raise ValueError(
                    f"task.evaluation.constraints.{name}.threshold must be numeric and finite"
                )
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
        fixed = _required(evaluation, "fixed", dict, "task.evaluation")
        development = _period(
            _required(fixed, "development", dict, "task.evaluation.fixed"),
            "task.evaluation.fixed.development",
        )
        gate = _period(
            _required(fixed, "gate", dict, "task.evaluation.fixed"),
            "task.evaluation.fixed.gate",
        )
        if development[1] >= gate[0]:
            raise ValueError("fixed development and gate periods must not overlap")

        test = evaluation.get("test")
        if test is not None:
            if not isinstance(test, dict):
                raise ValueError("task.evaluation.test must be a table")
            test_period = _period(test, "task.evaluation.test")
            if gate[1] >= test_period[0]:
                raise ValueError("test period must start after the research period")

        return cls(raw=dict(data))

    @staticmethod
    def _string_list(data: Mapping[str, Any], key: str, *, required: bool) -> None:
        value = data.get(key)
        if value is None and not required:
            return
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"{key} must be a non-empty list of strings")

    @property
    def task_id(self) -> str:
        return str(self.raw["id"])

    @property
    def evaluation_mode(self) -> str:
        return str(self.raw["evaluation"]["mode"])

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
    def strategy_name(self) -> str | None:
        strategy = self.raw.get("strategy")
        return str(strategy["name"]) if isinstance(strategy, Mapping) else None

    @property
    def strategy_module(self) -> str | None:
        strategy = self.raw.get("strategy")
        return str(strategy["module"]) if isinstance(strategy, Mapping) else None


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
        feedback = data.get("feedback")
        if feedback is not None and (not isinstance(feedback, str) or not feedback.strip()):
            raise ValueError("result.feedback must be a non-empty str when present")
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
            if not all(isinstance(metrics[key], dict) for key in ("development", "gate")):
                raise ValueError("result development and gate metrics must be objects")
        else:
            _required(data, "error", str, "result")

        return cls(raw=dict(data))

    @property
    def experiment_id(self) -> str:
        return str(self.raw["experiment_id"])
