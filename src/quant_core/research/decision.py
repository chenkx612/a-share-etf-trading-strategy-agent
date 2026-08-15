"""Pure promotion and target decisions for research metrics.

This module deliberately has no workspace, process, or persistence dependencies.
The deterministic Harness owns these functions; candidate code only supplies the
metrics that they inspect.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Mapping
from typing import Any

from quant_core.research.contracts import ResearchTask


def constraint_rule(constraint: Mapping[str, Any]) -> tuple[str, float]:
    return str(constraint["operator"]), float(constraint["threshold"])


def constraint_passes(value: Any, constraint: Mapping[str, Any]) -> bool:
    if not is_finite_number(value):
        return False
    operator, threshold = constraint_rule(constraint)
    if operator == ">=":
        return float(value) >= threshold
    if operator == "abs<=":
        return abs(float(value)) <= threshold
    return float(value) <= threshold


def is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def walk_forward_gate_is_feasible(
    task: ResearchTask,
    metrics: Mapping[str, Any] | None,
) -> bool:
    if task.evaluation_mode != "walk_forward":
        return True
    if not isinstance(metrics, Mapping):
        return False
    gate = metrics.get("gate")
    if not isinstance(gate, Mapping):
        return False
    no_feasible_folds = gate.get("no_feasible_parameter_folds")
    return (
        isinstance(no_feasible_folds, int)
        and not isinstance(no_feasible_folds, bool)
        and no_feasible_folds == 0
    )


def _gate_metrics(
    task: ResearchTask,
    value: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    gate = value.get("gate", {})
    if task.evaluation_mode == "walk_forward" and isinstance(gate, Mapping):
        gate = gate.get("aggregate", {})
    return gate if isinstance(gate, Mapping) else {}


def target_reached(task: ResearchTask, metrics: Mapping[str, Any] | None) -> bool:
    target = task.target
    if target is None or not isinstance(metrics, Mapping):
        return False
    if not walk_forward_gate_is_feasible(task, metrics):
        return False
    gate = _gate_metrics(task, metrics)
    objective = gate.get(task.objective)
    threshold = target["objective_at_least"]
    if not is_finite_number(objective):
        return False
    return float(objective) >= float(threshold) and all(
        constraint_passes(gate.get(name), constraint)
        for name, constraint in task.constraints.items()
    )


def decide(
    task: ResearchTask,
    champion: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the Harness-owned accept/reject decision for a candidate."""
    objective = task.objective
    champion_gate = _gate_metrics(task, champion)
    candidate_gate = _gate_metrics(task, candidate)
    champion_gate_is_feasible = walk_forward_gate_is_feasible(task, champion)
    candidate_gate_is_feasible = walk_forward_gate_is_feasible(task, candidate)
    champion_value = champion_gate.get(objective) if champion is not None else None
    candidate_value = candidate_gate.get(objective)
    champion_constraints_passed = (
        champion is not None
        and champion_gate_is_feasible
        and all(
            constraint_passes(champion_gate.get(name), constraint)
            for name, constraint in task.constraints.items()
        )
    )
    champion_objective_is_finite = is_finite_number(champion_value)
    minimum_improvement = float(task.acceptance.get("minimum_improvement", 0.0))
    constraints: dict[str, Any] = {}
    constraints_passed = True
    for name, constraint in task.constraints.items():
        actual = candidate_gate.get(name)
        operator, threshold = constraint_rule(constraint)
        passed = constraint_passes(actual, constraint)
        constraints[name] = {
            "operator": operator,
            "threshold": threshold,
            "actual": actual,
            "passed": passed,
        }
        constraints_passed = constraints_passed and passed
    candidate_objective_is_finite = is_finite_number(candidate_value)
    relative_improvement_required = (
        champion is not None
        and champion_constraints_passed
        and champion_objective_is_finite
    )
    objective_passed = (
        candidate_objective_is_finite
        if not relative_improvement_required
        else (
            candidate_objective_is_finite
            and float(candidate_value)
            >= float(champion_value) + minimum_improvement
            and (
                minimum_improvement > 0
                or float(candidate_value) > float(champion_value)
            )
        )
    )
    accepted = candidate_gate_is_feasible and constraints_passed and objective_passed
    reasons: list[str] = []
    if not candidate_gate_is_feasible:
        reasons.append("gate has folds with no feasible parameters")
    if not constraints_passed:
        reasons.append("gate constraints failed")
    if not objective_passed and not relative_improvement_required:
        reasons.append("gate objective is not finite")
    elif not objective_passed:
        reasons.append("gate objective did not improve over champion")
    return {
        "decision": "accepted" if accepted else "rejected",
        "objective": {
            "name": objective,
            "champion": champion_value,
            "candidate": candidate_value,
            "minimum_improvement": minimum_improvement,
            "champion_constraints_passed": (
                champion_constraints_passed if champion is not None else None
            ),
            "relative_improvement_required": relative_improvement_required,
        },
        "constraints": constraints,
        "reasons": reasons,
    }


def metrics_key(task: ResearchTask) -> str:
    """Fingerprint the fixed evaluation inputs that define comparable metrics."""
    periods = dict(task.evaluation_periods)
    if task.evaluation_mode == "walk_forward":
        assert task.parameter_selection is not None
        periods = {
            key: task.parameter_selection[key]
            for key in ("train_months", "max_parameter_sets", "schedule")
        } | periods
    relevant = {
        "strategy": {
            "name": task.strategy_name,
            "module": task.strategy_module,
        },
        "data": task.data,
        "commands": {
            **task.commands,
            "test_prefix": [sys.executable, "-m", "pytest", "-q"],
            "metrics_path": task.metrics_path_template,
        },
        "evaluation": {
            "mode": task.evaluation_mode,
            "objective": task.objective,
            "constraints": task.constraints,
            "periods": periods,
        },
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
