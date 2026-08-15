"""Transactional synchronization of a terminal Champion to production code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from quant_core.research.contracts import ResearchTask
from quant_core.research.loop_state import timestamp
from quant_core.research.storage import write_bytes_atomic, write_json_atomic
from quant_core.research.workspace import ResearchWorkspace


def sha256_path(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_production_sync_record(
    manager: ResearchWorkspace,
    state: dict[str, Any],
    record: dict[str, Any],
) -> None:
    manager.production_sync_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(manager.production_sync_path, record)
    state["production_sync_status"] = record["status"]
    state["production_sync_path"] = manager.production_sync_path.relative_to(
        manager.run_artifacts_root
    ).as_posix()
    state["production_sync_error"] = record.get("error")
    write_json_atomic(manager.loop_state_path, state)


def production_sync_record(
    task: ResearchTask,
    manager: ResearchWorkspace,
    state: dict[str, Any],
    *,
    status: str,
    observed_sha256: str | None,
    error: str | None = None,
    recovered: bool = False,
) -> dict[str, Any]:
    existing: dict[str, Any] = {}
    if manager.production_sync_path.is_file():
        try:
            loaded = json.loads(manager.production_sync_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            existing = loaded
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "run": manager.run_id,
        "strategy_path": task.strategy_path,
        "initial_champion_sha256": state.get("initial_champion_sha256"),
        "initial_champion_number": state.get("initial_champion_number"),
        "initial_champion_round_id": state.get("initial_champion_round_id"),
        "initial_production_strategy_sha256": state.get(
            "initial_production_strategy_sha256"
        ),
        "final_champion_sha256": state.get("final_champion_sha256"),
        "final_champion_number": state.get("final_champion_number"),
        "final_champion_round_id": state.get("final_champion_round_id"),
        "observed_production_strategy_sha256": observed_sha256,
        "production_strategy_sha256": (
            observed_sha256
            if status not in {"completed", "already_synchronized"}
            else state.get("final_champion_sha256")
        ),
        "status": status,
        "error": error,
        "started_at": existing.get("started_at", timestamp()),
        "updated_at": timestamp(),
        "completed_at": (
            timestamp()
            if status in {"completed", "already_synchronized", "not_needed", "not_configured"}
            else None
        ),
        "recovered": recovered or bool(existing.get("recovered", False)),
    }


def synchronize_production_strategy(
    task: ResearchTask,
    manager: ResearchWorkspace,
    state: dict[str, Any],
    *,
    recovered: bool = False,
) -> None:
    if task.production is None:
        state["production_sync_status"] = "not_configured"
        state["production_sync_path"] = None
        state["production_sync_error"] = None
        return

    target = manager.source / task.strategy_path
    observed = sha256_path(target)
    final_sha256 = state.get("final_champion_sha256")
    initial_sha256 = state.get("initial_champion_sha256")
    if not state.get("production_sync_baseline_available", False):
        error = "legacy Run does not contain a production synchronization baseline"
        record = production_sync_record(
            task, manager, state, status="legacy_unavailable", observed_sha256=observed,
            error=error, recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.emit_event("production_sync_failed", message=error)
        return
    if final_sha256 == initial_sha256:
        expected = state.get("initial_production_strategy_sha256")
        if isinstance(initial_sha256, str) and (
            expected != initial_sha256 or observed != expected
        ):
            error = (
                "production strategy changed outside the Run"
                if expected == initial_sha256
                else "production strategy was not synchronized with the initial Champion"
            )
            record = production_sync_record(
                task,
                manager,
                state,
                status="conflict",
                observed_sha256=observed,
                error=error,
                recovered=recovered,
            )
            write_production_sync_record(manager, state, record)
            manager.emit_event("production_sync_conflict", message=error)
            return
        record = production_sync_record(
            task, manager, state, status="not_needed", observed_sha256=observed,
            recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.emit_event("production_sync_not_needed", message="production sync not needed")
        return
    if not isinstance(final_sha256, str):
        error = "Run does not have a final Champion to synchronize"
        record = production_sync_record(
            task, manager, state, status="failed", observed_sha256=observed,
            error=error, recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.emit_event("production_sync_failed", message=error)
        return

    frozen = (
        manager.production_sync_champion_path
        if recovered
        else manager.terminal_champion_path
    )
    if not frozen.is_file() or sha256_path(frozen) != final_sha256:
        error = "frozen final Champion is unavailable or has an invalid hash"
        record = production_sync_record(
            task, manager, state, status="failed", observed_sha256=observed,
            error=error, recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.emit_event("production_sync_failed", message=error)
        return
    try:
        current_champion_sha256 = manager.load_state(task.strategy_path).get(
            "champion_sha256"
        )
    except (OSError, ValueError) as exc:
        error = f"task Champion could not be validated before production sync: {exc}"
        record = production_sync_record(
            task,
            manager,
            state,
            status="failed",
            observed_sha256=observed,
            error=error,
            recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.emit_event("production_sync_failed", message=error)
        return
    if current_champion_sha256 != final_sha256:
        error = "task Champion changed after the Run terminal snapshot was frozen"
        if frozen != manager.production_sync_champion_path:
            write_bytes_atomic(manager.production_sync_champion_path, frozen.read_bytes())
        record = production_sync_record(
            task,
            manager,
            state,
            status="conflict",
            observed_sha256=observed,
            error=error,
            recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.emit_event("production_sync_conflict", message=error)
        return
    if observed == final_sha256:
        record = production_sync_record(
            task, manager, state, status="already_synchronized", observed_sha256=observed,
            recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.production_sync_champion_path.unlink(missing_ok=True)
        manager.emit_event(
            "production_sync_completed", message="production strategy already synchronized"
        )
        return

    expected = state.get("initial_production_strategy_sha256")
    baseline_is_safe = initial_sha256 is None or expected == initial_sha256
    if not baseline_is_safe or observed != expected:
        error = (
            "production strategy changed outside the Run"
            if baseline_is_safe
            else "production strategy was not synchronized with the initial Champion"
        )
        if frozen != manager.production_sync_champion_path:
            write_bytes_atomic(manager.production_sync_champion_path, frozen.read_bytes())
        record = production_sync_record(
            task, manager, state, status="conflict", observed_sha256=observed,
            error=error, recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.emit_event("production_sync_conflict", message=error)
        return

    if frozen != manager.production_sync_champion_path:
        write_bytes_atomic(manager.production_sync_champion_path, frozen.read_bytes())
        frozen = manager.production_sync_champion_path
    pending = production_sync_record(
        task, manager, state, status="pending", observed_sha256=observed,
        recovered=recovered,
    )
    write_production_sync_record(manager, state, pending)
    manager.emit_event("production_sync_started", message="production sync started")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(target, frozen.read_bytes())
        synchronized = sha256_path(target)
        if synchronized != final_sha256:
            raise RuntimeError("production strategy hash does not match the final Champion")
    except Exception as exc:
        record = production_sync_record(
            task, manager, state, status="failed", observed_sha256=sha256_path(target),
            error=str(exc), recovered=recovered,
        )
        write_production_sync_record(manager, state, record)
        manager.emit_event("production_sync_failed", message="production sync failed")
        return
    record = production_sync_record(
        task, manager, state, status="completed", observed_sha256=observed,
        recovered=recovered,
    )
    write_production_sync_record(manager, state, record)
    manager.production_sync_champion_path.unlink(missing_ok=True)
    manager.emit_event("production_sync_completed", message="production sync completed")


def recover_unresolved_production_sync(
    task: ResearchTask,
    manager: ResearchWorkspace,
) -> Path | None:
    run_numbers = manager.run_numbers()
    if run_numbers:
        run = manager.for_run(run_numbers[-1])
        try:
            state = json.loads(run.loop_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if state.get("schema_version") not in {6, 7} or state.get("production_sync_status") not in {
            "pending",
            "conflict",
            "failed",
        }:
            return None
        if not isinstance(state.get("final_champion_sha256"), str):
            return None
        record: dict[str, Any] = {}
        if run.production_sync_path.is_file():
            try:
                loaded = json.loads(run.production_sync_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                record = loaded
        if (
            record.get("task_id") != task.task_id
            or record.get("strategy_path") != task.strategy_path
        ):
            state["production_sync_status"] = "failed"
            state["production_sync_error"] = (
                "task contract changed before production synchronization recovery"
            )
            write_json_atomic(run.loop_state_path, state)
            return run.loop_state_path
        synchronize_production_strategy(task, run, state, recovered=True)
        run.cleanup_transient(remove_development_cache=True)
        return run.loop_state_path
    return None

