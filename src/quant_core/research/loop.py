from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from quant_core.research.contracts import ResearchTask
from quant_core.research.report import generate_loop_report
from quant_core.research.runner import run_managed_once, target_reached
from quant_core.research.workspace import ResearchWorkspace, write_json_atomic


ManagedRunner = Callable[..., Path]
LoopReporter = Callable[[Path, ResearchWorkspace, dict[str, Any]], Path]
_EXPERIMENT_ID = re.compile(r"^loop-(\d+)$")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _new_state(task: ResearchTask, fingerprint: str) -> dict[str, Any]:
    now = _timestamp()
    return {
        "schema_version": 1,
        "task_id": task.task_id,
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
        "experiment_ids": [],
        "current_experiment_id": None,
        "last_experiment_id": None,
        "stop_reason": None,
        "report_status": None,
        "report_path": None,
        "report_error": None,
    }


def _next_experiment_id(experiments: Path) -> str:
    highest = 0
    if experiments.exists():
        for path in experiments.iterdir():
            match = _EXPERIMENT_ID.fullmatch(path.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"loop-{highest + 1:06d}"


def _record_decision(state: dict[str, Any], experiment_id: str, decision: str) -> None:
    experiment_ids = state.get("experiment_ids")
    if not isinstance(experiment_ids, list):
        experiment_ids = []
        state["experiment_ids"] = experiment_ids
    if experiment_id not in experiment_ids:
        experiment_ids.append(experiment_id)
    state["rounds_completed"] = int(state["rounds_completed"]) + 1
    state["last_experiment_id"] = experiment_id
    state["current_experiment_id"] = None
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
    _save(loop_state_path, state)
    try:
        report_path = reporter(task_file, manager, state)
    except KeyboardInterrupt:
        state["report_status"] = "interrupted"
        state["report_error"] = "Report generation was interrupted"
        _save(loop_state_path, state)
        raise
    except Exception as exc:
        state["report_status"] = "failed"
        state["report_error"] = str(exc)
    else:
        state["report_status"] = "completed"
        try:
            state["report_path"] = report_path.relative_to(manager.root).as_posix()
        except ValueError:
            state["report_path"] = str(report_path)
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
) -> Path:
    """Run managed research rounds until one of the configured budgets is exhausted."""
    task_file = Path(task_path).resolve()
    task = ResearchTask.load(task_file)
    source = Path(workspace).resolve()
    managed_root = Path(research_root)
    if not managed_root.is_absolute():
        managed_root = source / managed_root
    manager = ResearchWorkspace(source, managed_root, task.task_id)
    development_end = task.raw["evaluation"]["fixed"]["development"]["end"]
    manager.initialize(
        date.fromisoformat(development_end),
        task.baseline_mode,
        task.baseline_exclude,
    )
    loop_state_path = manager.root / "loop-state.json"
    fingerprint = _task_fingerprint(task_file)

    if loop_state_path.exists():
        state = json.loads(loop_state_path.read_text(encoding="utf-8"))
        if state.get("status") in {"running", "interrupted"}:
            if state.get("task_fingerprint") != fingerprint:
                raise ValueError("task.toml changed while a research loop was running")
            state["status"] = "running"
            state["stop_reason"] = None
        else:
            state = _new_state(task, fingerprint)
    else:
        state = _new_state(task, fingerprint)

    current = state.get("current_experiment_id")
    if isinstance(current, str):
        decision_path = manager.experiments / current / "decision.json"
        task_state = manager.load_state()
        if decision_path.exists() and task_state.get("last_experiment_id") == current:
            decision = json.loads(decision_path.read_text(encoding="utf-8")).get("decision", "failed")
        else:
            decision = "failed"
            if decision_path.parent.exists():
                result_path = decision_path.parent / "result.json"
                if not result_path.exists():
                    write_json_atomic(result_path, {
                        "experiment_id": current,
                        "status": "failed",
                        "error": "Loop was interrupted before the round was committed",
                    })
                write_json_atomic(decision_path, {
                    "experiment_id": current,
                    "decision": "failed",
                    "reasons": ["loop was interrupted before the round was committed"],
                })
        _record_decision(state, current, str(decision))
        _save(loop_state_path, state)

    while True:
        task_state = manager.load_state()
        champion_metrics = task_state.get("champion_metrics")
        reason = _stop_reason(
            state,
            task,
            champion_metrics if isinstance(champion_metrics, dict) else None,
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

        experiment_id = _next_experiment_id(manager.experiments)
        state["current_experiment_id"] = experiment_id
        _save(loop_state_path, state)
        started = monotonic()
        try:
            result_path = managed_runner(
                task_file,
                experiment_id,
                workspace=source,
                research_root=managed_root,
            )
        except KeyboardInterrupt:
            state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
            state["status"] = "interrupted"
            state["stop_reason"] = "interrupted"
            _save(loop_state_path, state)
            return loop_state_path
        except Exception:
            state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
            state["status"] = "interrupted"
            state["stop_reason"] = "runner_error"
            _save(loop_state_path, state)
            raise

        state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
        decision_path = result_path.parent / "decision.json"
        decision = json.loads(decision_path.read_text(encoding="utf-8")).get("decision", "failed")
        _record_decision(state, experiment_id, str(decision))
        _save(loop_state_path, state)
