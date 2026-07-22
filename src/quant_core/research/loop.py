from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from quant_core.research.contracts import ResearchTask
from quant_core.research.report import generate_loop_report
from quant_core.research.runner import (
    AgentContainerInfrastructureError,
    _metrics_key,
    preflight_agent_container,
    preflight_provider_authentication,
    run_managed_once,
    target_reached,
)
from quant_core.research.workspace import ResearchWorkspace, write_json_atomic


ManagedRunner = Callable[..., Path]
LoopReporter = Callable[[Path, ResearchWorkspace, dict[str, Any]], Path]
ContainerPreflight = Callable[[ResearchTask, Path], None]
ProviderPreflight = Callable[[ResearchTask, Path], None]
_ROUND_ID = re.compile(r"^(\d+)$")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _new_state(task: ResearchTask, fingerprint: str, run_number: int) -> dict[str, Any]:
    now = _timestamp()
    return {
        "schema_version": 2,
        "task_id": task.task_id,
        "run_number": run_number,
        "task_fingerprint": fingerprint,
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
    }


def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    if "round_ids" not in state:
        state["round_ids"] = state.pop("experiment_ids", [])
    if "current_round" not in state:
        state["current_round"] = state.pop("current_experiment_id", None)
    if "last_round" not in state:
        state["last_round"] = state.pop("last_experiment_id", None)
    state["schema_version"] = 2
    return state


def _next_round_id(experiments: Path, reserved: Sequence[object] = ()) -> str:
    highest = 0
    if experiments.exists():
        for path in experiments.iterdir():
            match = _ROUND_ID.fullmatch(path.name)
            if match:
                highest = max(highest, int(match.group(1)))
    for value in reserved:
        if isinstance(value, str) and _ROUND_ID.fullmatch(value):
            highest = max(highest, int(value))
    return f"{highest + 1:03d}"


def _record_decision(state: dict[str, Any], round_id: str, decision: str) -> None:
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


def _stop_reason(
    state: dict[str, Any],
    task: ResearchTask,
    champion_metrics: dict[str, Any] | None,
) -> str | None:
    budget = task.raw["budget"]
    if target_reached(task, champion_metrics):
        return "target_reached"
    if int(state["rounds_completed"]) >= int(budget["max_rounds"]):
        return "max_rounds"
    if float(state["elapsed_seconds"]) >= float(budget["max_hours"]) * 3600:
        return "max_hours"
    if int(state["consecutive_failures"]) >= int(budget["max_consecutive_failures"]):
        return "max_consecutive_failures"
    return None


def _save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _timestamp()
    write_json_atomic(path, state)


def _finish_with_report(
    task_file: Path,
    manager: ResearchWorkspace,
    loop_state_path: Path,
    state: dict[str, Any],
    reason: str,
    reporter: LoopReporter,
) -> Path:
    state["status"] = "stopped"
    state["stop_reason"] = reason
    state["report_status"] = "running"
    state["report_path"] = None
    state["report_error"] = None
    state["report_failure_kind"] = None
    state["report_failure_code"] = None
    _save(loop_state_path, state)
    manager.emit_event("run_stopping", message=f"stopping: {reason}")
    try:
        report_path = reporter(task_file, manager, state)
    except KeyboardInterrupt:
        state["report_status"] = "interrupted"
        state["report_error"] = "Report generation was interrupted"
        manager.cleanup_transient(remove_development_cache=True)
        _save(loop_state_path, state)
        raise
    except Exception as exc:
        state["report_status"] = "failed"
        state["report_error"] = str(exc)
        state["report_failure_kind"] = getattr(exc, "failure_kind", None)
        state["report_failure_code"] = getattr(exc, "failure_code", None)
    else:
        state["report_status"] = "completed"
        try:
            state["report_path"] = report_path.relative_to(manager.run_root).as_posix()
        except ValueError:
            state["report_path"] = str(report_path)
    manager.compact_artifacts()
    manager.cleanup_transient(remove_development_cache=True)
    _save(loop_state_path, state)
    return loop_state_path


def run_loop(
    task_path: str | Path,
    *,
    workspace: str | Path = ".",
    research_root: str | Path = ".research",
    managed_runner: ManagedRunner = run_managed_once,
    reporter: LoopReporter = generate_loop_report,
    monotonic: Callable[[], float] = time.monotonic,
    container_preflight: ContainerPreflight | None = None,
    provider_preflight: ProviderPreflight | None = None,
) -> Path:
    """Run managed research rounds until one of the configured budgets is exhausted."""
    task_file = Path(task_path).resolve()
    task = ResearchTask.load(task_file)
    source = Path(workspace).resolve()
    managed_root = Path(research_root)
    if not managed_root.is_absolute():
        managed_root = source / managed_root
    base_manager = ResearchWorkspace(source, managed_root, task.task_id)
    development_end = task.development_period["end"]
    base_manager.initialize(
        date.fromisoformat(development_end),
        task.baseline_mode,
        task.baseline_exclude,
        task.strategy_path,
    )
    metrics_key = _metrics_key(task)
    task_state = base_manager.load_state(task.strategy_path)
    base_manager.refresh_champion_metrics_status(task_state, metrics_key)
    base_manager.migrate_legacy_loop()
    fingerprint = _task_fingerprint(task_file)
    active_runs: list[tuple[int, dict[str, Any]]] = []
    for run_number in base_manager.run_numbers():
        candidate = base_manager.for_run(run_number)
        try:
            candidate_state = json.loads(candidate.loop_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if candidate_state.get("status") in {"running", "interrupted"}:
            active_runs.append((run_number, candidate_state))
    if len(active_runs) > 1:
        raise RuntimeError("multiple active research runs exist for the same task")
    if active_runs:
        run_number, state = active_runs[0]
        state = _normalize_state(state)
        if state.get("task_fingerprint") != fingerprint:
            raise ValueError("task.toml changed while a research loop was running")
        manager = base_manager.for_run(run_number)
    else:
        run_number = base_manager.next_run_number()
        manager = base_manager.for_run(run_number)

    # Injected managed runners own their execution environment. The production
    # runner must prove its Docker boundary before a Run is allocated or resumed.
    preflight = container_preflight
    if preflight is None and managed_runner is run_managed_once:
        preflight = preflight_agent_container
    if preflight is not None:
        preflight(task, managed_root)

    authentication_preflight = provider_preflight
    if authentication_preflight is None and managed_runner is run_managed_once:
        authentication_preflight = preflight_provider_authentication
    if authentication_preflight is not None and not active_runs:
        authentication_preflight(task, managed_root)

    if active_runs:
        state["status"] = "running"
        state["stop_reason"] = None
        lifecycle_event = ("run_resumed", "resumed")
    else:
        manager.rounds.mkdir(parents=True, exist_ok=False)
        state = _new_state(task, fingerprint, run_number)
        lifecycle_event = ("run_started", "started")
    loop_state_path = manager.loop_state_path
    _save(loop_state_path, state)
    manager.emit_event(lifecycle_event[0], message=lifecycle_event[1])

    current = state.get("current_round", state.get("current_experiment_id"))
    if isinstance(current, str):
        decision_path = manager.rounds / current / "decision.json"
        task_state = manager.load_state(task.strategy_path)
        record_id = f"{manager.run_id}/{current}"
        if decision_path.exists() and task_state.get(
            "last_round_id",
            task_state.get("last_experiment_id"),
        ) in {current, record_id}:
            decision = json.loads(decision_path.read_text(encoding="utf-8")).get("decision", "failed")
        else:
            decision = "failed"
            decision_path.parent.mkdir(parents=True, exist_ok=True)
            result_path = decision_path.parent / "result.json"
            if not result_path.exists():
                write_json_atomic(result_path, {
                    "experiment_id": record_id,
                    "status": "failed",
                    "error": "Loop was interrupted before the round was finalized",
                })
            write_json_atomic(decision_path, {
                "experiment_id": record_id,
                "decision": "failed",
                "reasons": ["loop was interrupted before the round was finalized"],
            })
        _record_decision(state, current, str(decision))
        _save(loop_state_path, state)

    while True:
        task_state = manager.load_state(task.strategy_path)
        manager.refresh_champion_metrics_status(task_state, metrics_key)
        champion_metrics = manager.valid_champion_metrics(task_state)
        reason = _stop_reason(
            state,
            task,
            champion_metrics,
        )
        if reason is not None:
            return _finish_with_report(
                task_file,
                manager,
                loop_state_path,
                state,
                reason,
                reporter,
            )

        if authentication_preflight is not None and (
            bool(active_runs) or int(state["rounds_completed"]) > 0
        ):
            try:
                authentication_preflight(task, managed_root)
            except AgentContainerInfrastructureError:
                return _finish_with_report(
                    task_file,
                    manager,
                    loop_state_path,
                    state,
                    "infrastructure_failure",
                    reporter,
                )

        round_ids = state.get("round_ids")
        reserved = round_ids if isinstance(round_ids, list) else []
        round_id = _next_round_id(manager.rounds, reserved)
        state["current_round"] = round_id
        _save(loop_state_path, state)
        manager.emit_event("round_started", round=round_id, message="candidate started")
        started = monotonic()
        try:
            result_path = managed_runner(
                task_file,
                round_id,
                workspace=source,
                research_root=managed_root,
                run_number=run_number,
                event_sink=manager.emit_event,
            )
        except KeyboardInterrupt:
            state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
            state["status"] = "interrupted"
            state["stop_reason"] = "interrupted"
            manager.cleanup_transient()
            manager.emit_event("run_interrupted", round=round_id, message="interrupted")
            _save(loop_state_path, state)
            return loop_state_path
        except Exception:
            state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
            state["status"] = "interrupted"
            state["stop_reason"] = "runner_error"
            manager.cleanup_transient()
            manager.emit_event("runner_error", round=round_id, message="runner error")
            _save(loop_state_path, state)
            raise

        state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        decision_path = result_path.parent / "decision.json"
        decision = json.loads(decision_path.read_text(encoding="utf-8")).get("decision", "failed")
        _record_decision(state, round_id, str(decision))
        manager.emit_event(
            "round_completed",
            round=round_id,
            decision=str(decision),
            message=f"completed: {decision}",
        )
        _save(loop_state_path, state)
        if result.get("failure_kind") == "infrastructure":
            return _finish_with_report(
                task_file,
                manager,
                loop_state_path,
                state,
                "infrastructure_failure",
                reporter,
            )
