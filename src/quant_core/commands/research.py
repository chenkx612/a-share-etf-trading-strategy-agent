from __future__ import annotations

import argparse
import json
import signal
import tomllib
from pathlib import Path
from types import FrameType

from quant_core.research import ResearchTask, regenerate_loop_report, run_loop, run_once
from quant_core.research.environment import (
    capture_evaluation_environment,
    persist_evaluation_environment,
)
from quant_core.research.workspace import ResearchWorkspace


def command_research_run_once(args: argparse.Namespace) -> None:
    environment = capture_evaluation_environment()
    output = Path(args.output).resolve()
    persist_evaluation_environment(output, environment)
    result_path = run_once(
        args.task,
        args.experiment_id,
        output,
        workspace=args.root,
        evaluation_environment=environment,
    )
    print(f"wrote experiment result to {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "completed":
        raise SystemExit(1)


def command_research_loop(args: argparse.Namespace) -> None:
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        state_path = run_loop(
            args.task,
            workspace=args.root,
            research_root=args.research_root,
            retain_diagnostics=args.retain_diagnostics,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print(f"wrote loop state to {state_path}")
    print(f"stop reason: {state['stop_reason']}")
    print(
        f"rounds: {state['rounds_completed']} "
        f"(accepted={state['accepted']}, rejected={state['rejected']}, failed={state['failed']})"
    )
    run_root = (
        state_path.parent.parent
        if state_path.parent.name == "artifacts"
        else state_path.parent
    )
    if state.get("report_status") == "completed":
        print(f"report: {run_root / str(state['report_path'])}")
    elif state.get("report_status") == "failed" and state.get("report_path"):
        print(f"report fallback: {run_root / str(state['report_path'])}")
        print(f"report failed: {state.get('report_error')}")
    elif state.get("report_status") == "failed":
        print(f"report failed: {state.get('report_error')}")
    sync_status = state.get("production_sync_status")
    if sync_status is not None:
        print(f"production sync: {sync_status}")
    if state.get("production_sync_error"):
        print(f"production sync error: {state['production_sync_error']}")
    if state["stop_reason"] == "interrupted":
        raise SystemExit(130)
    if sync_status in {"pending", "conflict", "failed", "legacy_unavailable"}:
        raise SystemExit(1)


def resolve_research_task_reference(reference: str, workspace: str | Path = ".") -> Path:
    """Resolve a task path, canonical tasks/<task-id>.toml, or task id."""
    explicit = Path(reference).expanduser()
    workspace_path = Path(workspace).resolve()
    tasks_dir = workspace_path / "tasks"
    if explicit.is_absolute() or len(explicit.parts) > 1:
        path = explicit if explicit.is_absolute() else Path.cwd() / explicit
        if not path.is_file():
            raise ValueError(f"task file does not exist: {reference}")
        return path.resolve()
    if explicit.suffix == ".toml":
        for path in (Path.cwd() / explicit, tasks_dir / explicit):
            if path.is_file():
                return path.resolve()
        raise ValueError(f"task file does not exist: {reference}")

    candidates: list[tuple[Path, str]] = []
    if tasks_dir.is_dir():
        for path in sorted(tasks_dir.glob("*.toml")):
            try:
                with path.open("rb") as handle:
                    payload = tomllib.load(handle)
                    task_id = payload.get("id")
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if isinstance(task_id, str) and task_id:
                candidates.append((path.resolve(), task_id))

    matches = {
        path
        for path, task_id in candidates
        if reference == path.stem or reference == task_id
    }
    if len(matches) == 1:
        return matches.pop()
    if len(matches) > 1:
        matched = ", ".join(
            str(path.relative_to(workspace_path)) for path in sorted(matches)
        )
        raise ValueError(f"task reference is ambiguous: {reference} ({matched})")

    available = ", ".join(path.stem for path, _task_id in candidates) or "none"
    raise ValueError(f"unknown task: {reference}; available tasks: {available}")


def command_loop(args: argparse.Namespace) -> None:
    try:
        args.task = str(resolve_research_task_reference(args.task, args.root))
    except ValueError as exc:
        raise SystemExit(f"quant-agent loop: error: {exc}") from exc
    command_research_loop(args)


def command_research_report(args: argparse.Namespace) -> None:
    report_path = regenerate_loop_report(
        args.task,
        workspace=args.root,
        research_root=args.research_root,
        run_number=args.run,
    )
    print(f"wrote loop report to {report_path}")


def command_research_clean(args: argparse.Namespace) -> None:
    task = ResearchTask.load(Path(args.task).resolve()) if args.task is not None else None
    task_id = task.task_id if task is not None else str(args.task_id)
    source = Path(args.root).resolve()
    research_root = Path(args.research_root)
    if not research_root.is_absolute():
        research_root = source / research_root
    manager = ResearchWorkspace(source, research_root, task_id)
    loop_state_paths = [
        manager.root / "loop-state.json",
        *(manager.for_run(run_number).loop_state_path for run_number in manager.run_numbers()),
    ]
    for loop_state_path in loop_state_paths:
        if not loop_state_path.exists():
            continue
        loop_state = json.loads(loop_state_path.read_text(encoding="utf-8"))
        if loop_state.get("status") == "running":
            raise RuntimeError("cannot clean artifacts while a research loop is running")
    if manager.state_path.exists() or manager.legacy_state_path.exists():
        manager.load_state(task.strategy_path if task is not None else None)
    manager.migrate_legacy_loop()
    manager.cleanup_transient(remove_development_cache=True)
    manager.clear_diagnostics()
    summary = manager.compact_artifacts()
    print(
        f"removed {summary['removed_files']} redundant files "
        f"({summary['removed_bytes']} bytes) from {manager.root}"
    )
