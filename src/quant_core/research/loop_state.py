"""Versioned state transitions for the outer research loop."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict

from quant_core.research.contracts import ResearchTask
from quant_core.research.decision import target_reached


LOOP_STATE_SCHEMA_VERSION = 7


class LoopState(TypedDict, total=False):
    schema_version: int
    task_id: str
    run_number: int
    task_fingerprint: str
    evaluation_environment_sha256: str
    status: str
    started_at: str
    updated_at: str
    elapsed_seconds: float
    rounds_completed: int
    accepted: int
    rejected: int
    failed: int
    consecutive_failures: int
    round_ids: list[str]
    current_round: str | None
    last_round: str | None
    stop_reason: str | None
    report_status: str | None
    report_path: str | None
    report_error: str | None
    report_failure_kind: str | None
    report_failure_code: str | None
    guard_query_count: int
    diagnostics_enabled: bool
    development_view_sha256: str
    development_end: str
    evaluation_inputs_sha256: str | None
    resolved_periods: dict[str, Any] | None
    production_sync_baseline_available: bool
    initial_champion_sha256: str | None
    initial_champion_number: int | None
    initial_champion_round_id: str | None
    initial_production_strategy_sha256: str | None
    production_sync_status: str | None
    production_sync_path: str | None
    production_sync_error: str | None
    # Legacy fields are accepted only while normalizing historical state.
    experiment_ids: list[str]
    current_experiment_id: str | None
    last_experiment_id: str | None
    test_status: str | None
    test_path: str | None
    test_error: str | None


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_loop_state(
    task: ResearchTask,
    fingerprint: str,
    run_number: int,
    evaluation_environment_sha256: str,
    diagnostics_enabled: bool,
    development_view_sha256: str,
    development_end: str,
    evaluation_inputs_sha256: str | None = None,
    resolved_periods: dict[str, Any] | None = None,
    initial_champion_sha256: str | None = None,
    initial_champion_number: int | None = None,
    initial_champion_round_id: str | None = None,
    initial_production_strategy_sha256: str | None = None,
) -> LoopState:
    now = timestamp()
    return {
        "schema_version": LOOP_STATE_SCHEMA_VERSION,
        "task_id": task.task_id,
        "run_number": run_number,
        "task_fingerprint": fingerprint,
        "evaluation_environment_sha256": evaluation_environment_sha256,
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "elapsed_seconds": 0.0,
        "rounds_completed": 0,
        "accepted": 0,
        "rejected": 0,
        "failed": 0,
        "consecutive_failures": 0,
        "round_ids": [],
        "current_round": None,
        "last_round": None,
        "stop_reason": None,
        "report_status": None,
        "report_path": None,
        "report_error": None,
        "report_failure_kind": None,
        "report_failure_code": None,
        "guard_query_count": 0,
        "diagnostics_enabled": diagnostics_enabled,
        "development_view_sha256": development_view_sha256,
        "development_end": development_end,
        "evaluation_inputs_sha256": evaluation_inputs_sha256,
        "resolved_periods": resolved_periods,
        "production_sync_baseline_available": True,
        "initial_champion_sha256": initial_champion_sha256,
        "initial_champion_number": initial_champion_number,
        "initial_champion_round_id": initial_champion_round_id,
        "initial_production_strategy_sha256": initial_production_strategy_sha256,
        "production_sync_status": None,
        "production_sync_path": None,
        "production_sync_error": None,
    }


def normalize_loop_state(state: LoopState) -> LoopState:
    if "round_ids" not in state:
        state["round_ids"] = state.pop("experiment_ids", [])
    if "current_round" not in state:
        state["current_round"] = state.pop("current_experiment_id", None)
    if "last_round" not in state:
        state["last_round"] = state.pop("last_experiment_id", None)
    schema_version = state.get("schema_version")
    if schema_version not in {2, 3, 4, 5, 6, LOOP_STATE_SCHEMA_VERSION}:
        raise ValueError("research loop uses an incompatible state schema")
    if schema_version != LOOP_STATE_SCHEMA_VERSION:
        state["schema_version"] = LOOP_STATE_SCHEMA_VERSION
        state.pop("test_status", None)
        state.pop("test_path", None)
        state.pop("test_error", None)
    state.setdefault("guard_query_count", 0)
    if schema_version not in {6, LOOP_STATE_SCHEMA_VERSION}:
        state.setdefault("production_sync_baseline_available", False)
    state.setdefault("production_sync_status", None)
    state.setdefault("production_sync_path", None)
    state.setdefault("production_sync_error", None)
    return state


def record_decision(state: LoopState, round_id: str, decision: str) -> None:
    round_ids = state.get("round_ids")
    if not isinstance(round_ids, list):
        round_ids = []
        state["round_ids"] = round_ids
    completed = int(state["rounds_completed"])
    counted = int(state["accepted"]) + int(state["rejected"]) + int(state["failed"])
    if completed != len(round_ids) or counted != completed:
        raise RuntimeError("research loop round counters are inconsistent")
    if round_id in round_ids:
        raise RuntimeError(f"round decision was already recorded: {round_id}")
    round_ids.append(round_id)
    state["rounds_completed"] = completed + 1
    state["last_round"] = round_id
    state["current_round"] = None
    if decision == "accepted":
        state["accepted"] = int(state["accepted"]) + 1
        state["consecutive_failures"] = 0
    elif decision == "rejected":
        state["rejected"] = int(state["rejected"]) + 1
        state["consecutive_failures"] = 0
    else:
        state["failed"] = int(state["failed"]) + 1
        state["consecutive_failures"] = int(state["consecutive_failures"]) + 1


def stop_reason(
    state: LoopState,
    task: ResearchTask,
    champion_metrics: dict[str, Any] | None,
) -> str | None:
    budget = task.budget
    if target_reached(task, champion_metrics):
        return "target_reached"
    if int(state["rounds_completed"]) >= int(budget["max_rounds"]):
        return "max_rounds"
    if float(state["elapsed_seconds"]) >= float(budget["max_hours"]) * 3600:
        return "max_hours"
    if int(state["consecutive_failures"]) >= int(
        budget["max_consecutive_failures"]
    ):
        return "max_consecutive_failures"
    return None
