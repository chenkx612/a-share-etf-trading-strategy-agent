from __future__ import annotations

import json
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

        opencode = _required(data, "opencode", dict, "task")
        model = _required(opencode, "model", str, "task.opencode")
        provider, separator, model_name = model.partition("/")
        if not separator or not provider or not model_name:
            raise ValueError("task.opencode.model must use provider/model format")
        timeout_minutes = opencode.get("timeout_minutes")
        if not isinstance(timeout_minutes, int) or isinstance(timeout_minutes, bool) or timeout_minutes < 1:
            raise ValueError("task.opencode.timeout_minutes must be a positive integer")

        source = _required(data, "data", dict, "task")
        _required(source, "universe", str, "task.data")

        scope = _required(data, "scope", dict, "task")
        cls._string_list(scope, "editable", required=True)
        cls._string_list(scope, "forbidden", required=False)

        commands = _required(data, "commands", dict, "task")
        cls._string_list(commands, "test", required=True)
        cls._string_list(commands, "backtest", required=True)
        metrics_path = _required(commands, "metrics_path", str, "task.commands")
        backtest_template = " ".join(commands["backtest"])
        for placeholder in ("{start}", "{end}", "{run_id}"):
            if placeholder not in backtest_template:
                raise ValueError(f"task.commands.backtest must contain {placeholder}")
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
            if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
                raise ValueError(
                    f"task.evaluation.constraints.{name}.threshold must be numeric"
                )
        acceptance = evaluation.get("acceptance")
        if acceptance is not None:
            if not isinstance(acceptance, dict):
                raise ValueError("task.evaluation.acceptance must be a table")
            minimum = acceptance.get("minimum_improvement", 0.0)
            if not isinstance(minimum, (int, float)) or isinstance(minimum, bool) or minimum < 0:
                raise ValueError("task.evaluation.acceptance.minimum_improvement must be non-negative")
        target = evaluation.get("target")
        if target is not None:
            if not isinstance(target, dict):
                raise ValueError("task.evaluation.target must be a table")
            threshold = target.get("objective_at_least")
            if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
                raise ValueError("task.evaluation.target.objective_at_least must be numeric")
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

        test = _required(evaluation, "test", dict, "task.evaluation")
        test_period = _period(test, "task.evaluation.test")
        if gate[1] >= test_period[0]:
            raise ValueError("test period must start after the research period")

        if "baseline" in data:
            raise ValueError(
                "task.baseline is not supported; the initial baseline is the current workspace snapshot"
            )

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
        feedback = data.get("feedback")
        if feedback is not None and (not isinstance(feedback, str) or not feedback.strip()):
            raise ValueError("result.feedback must be a non-empty str when present")
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
