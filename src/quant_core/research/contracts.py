from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from quant_core.research.result_validation import validate_experiment_result
from quant_core.research.task_validation import (
    module_from_strategy_path,
    required as _required,
    validate_task_policy_sections,
    validate_task_scope_and_evaluation,
)


METRICS_PATH_TEMPLATE = "outputs/backtests/{run_id}/metrics.json"

EVALUATOR_CONTRACT_BASE_PATHS = (
    "pyproject.toml",
    "src/quant_core",
    "tests",
)


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
    resolved_periods: Mapping[str, Any] | None = None
    period_resolution: Mapping[str, Any] | None = None

    @classmethod
    def load(cls, path: str | Path) -> ResearchTask:
        with Path(path).open("rb") as handle:
            return cls.from_mapping(tomllib.load(handle))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ResearchTask:
        policy = validate_task_policy_sections(data)
        validate_task_scope_and_evaluation(data, policy)

        return cls(raw=dict(data))

    @staticmethod
    def _module_from_strategy_path(path: str) -> str:
        return module_from_strategy_path(path)

    @property
    def task_id(self) -> str:
        return str(self.raw["id"])

    @property
    def goal(self) -> str:
        return str(self.raw["goal"])

    @property
    def budget(self) -> Mapping[str, Any]:
        value = self.raw["budget"]
        assert isinstance(value, Mapping)
        return value

    @property
    def data(self) -> Mapping[str, Any]:
        value = self.raw["data"]
        assert isinstance(value, Mapping)
        return value

    @property
    def universe_path(self) -> str:
        return str(self.data["universe"])

    @property
    def commands(self) -> Mapping[str, Any]:
        value = self.raw["commands"]
        assert isinstance(value, Mapping)
        return value

    @property
    def agent(self) -> Mapping[str, Any]:
        value = self.raw["opencode"]
        assert isinstance(value, Mapping)
        return value

    @property
    def evaluation(self) -> Mapping[str, Any]:
        value = self.raw["evaluation"]
        assert isinstance(value, Mapping)
        return value

    @property
    def acceptance(self) -> Mapping[str, Any]:
        value = self.evaluation.get("acceptance", {})
        assert isinstance(value, Mapping)
        return value

    @property
    def target(self) -> Mapping[str, Any] | None:
        value = self.evaluation.get("target")
        return value if isinstance(value, Mapping) else None

    @property
    def editable_paths(self) -> list[str]:
        return list(self.raw["scope"]["editable"])

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
        validate_experiment_result(data)
        return cls(raw=dict(data))

    @property
    def experiment_id(self) -> str:
        return str(self.raw["experiment_id"])
