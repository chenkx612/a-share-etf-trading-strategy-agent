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
        max_iterations = data.get("max_iterations")
        if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1:
            raise ValueError("task.max_iterations must be a positive integer")

        source = _required(data, "data", dict, "task")
        _required(source, "universe", str, "task.data")

        scope = _required(data, "scope", dict, "task")
        cls._string_list(scope, "editable", required=True)
        cls._string_list(scope, "forbidden", required=False)

        commands = _required(data, "commands", dict, "task")
        cls._string_list(commands, "test", required=True)
        cls._string_list(commands, "backtest", required=True)

        evaluation = _required(data, "evaluation", dict, "task")
        mode = _required(evaluation, "mode", str, "task.evaluation")
        _required(evaluation, "objective", str, "task.evaluation")
        constraints = _required(evaluation, "constraints", dict, "task.evaluation")
        if not constraints or not all(
            isinstance(key, str)
            and key
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            for key, value in constraints.items()
        ):
            raise ValueError("task.evaluation.constraints must contain numeric limits")
        if mode == "fixed":
            fixed = _required(evaluation, "fixed", dict, "task.evaluation")
            train = _period(_required(fixed, "train", dict, "task.evaluation.fixed"), "task.evaluation.fixed.train")
            validation = _period(
                _required(fixed, "validation", dict, "task.evaluation.fixed"),
                "task.evaluation.fixed.validation",
            )
            if train[1] >= validation[0]:
                raise ValueError("fixed train and validation periods must not overlap")
        elif mode == "walk_forward":
            walk = _required(evaluation, "walk_forward", dict, "task.evaluation")
            research_period = _period(walk, "task.evaluation.walk_forward")
            for key in ("train_months", "validation_months", "step_months"):
                value = walk.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ValueError(f"task.evaluation.walk_forward.{key} must be a positive integer")
        else:
            raise ValueError("task.evaluation.mode must be 'fixed' or 'walk_forward'")

        test = _required(evaluation, "test", dict, "task.evaluation")
        test_period = _period(test, "task.evaluation.test")
        research_end = validation[1] if mode == "fixed" else research_period[1]
        if research_end >= test_period[0]:
            raise ValueError("test period must start after the research period")

        baseline = data.get("baseline")
        if baseline is not None and not isinstance(baseline, dict):
            raise ValueError("task.baseline must be a table when provided")

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
        if status == "completed":
            _required(data, "hypothesis", str, "result")
            changes = _required(data, "changes", dict, "result")
            _required(changes, "summary", str, "result.changes")
            ResearchTask._string_list(changes, "files", required=True)

            verification = _required(data, "verification", dict, "result")
            for key in ("tests_passed", "backtest_completed"):
                if not isinstance(verification.get(key), bool):
                    raise ValueError(f"result.verification.{key} must be a boolean")

            metrics = _required(data, "metrics", dict, "result")
            has_fixed = "train" in metrics and "validation" in metrics
            has_walk_forward = "walk_forward" in metrics
            if has_fixed == has_walk_forward:
                raise ValueError("result.metrics must contain fixed or walk-forward metrics")
            if "test" in metrics:
                raise ValueError("result.metrics must not contain test metrics during the research loop")
        else:
            _required(data, "error", str, "result")

        return cls(raw=dict(data))

    @property
    def experiment_id(self) -> str:
        return str(self.raw["experiment_id"])
