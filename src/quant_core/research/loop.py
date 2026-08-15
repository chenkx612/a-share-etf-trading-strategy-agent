from __future__ import annotations

import hashlib
import inspect
import json
import re
import shutil
import time
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from quant_core.research.contracts import ResearchTask
from quant_core.research.agent_errors import (
    AgentContainerInfrastructureError,
    CandidateBindPreflightError,
)
from quant_core.research.decision import metrics_key
from quant_core.research.loop_state import (
    new_loop_state as _new_state,
    normalize_loop_state as _normalize_state,
    record_decision as _record_decision,
    stop_reason as _stop_reason,
    timestamp as _timestamp,
)
from quant_core.research.periods import (
    bind_persisted_periods,
    resolve_relative_periods,
)
from quant_core.research.production_sync import (
    production_sync_record as _production_sync_record,
    recover_unresolved_production_sync as _recover_unresolved_production_sync,
    sha256_path as _sha256_path,
    synchronize_production_strategy as _synchronize_production_strategy,
    write_production_sync_record as _write_production_sync_record,
)
from quant_core.research.environment import (
    EvaluationEnvironment,
    capture_evaluation_environment,
    persist_evaluation_environment,
)
from quant_core.research.report import (
    generate_loop_report,
    write_minimal_loop_report,
)
from quant_core.research.runner import (
    preflight_agent_container,
    preflight_provider_authentication,
    probe_candidate_bind_source,
    run_parent_fixed_tests,
    run_managed_once,
)
from quant_core.research.workspace import (
    build_evaluation_view,
    DevelopmentInputsError,
    ResearchWorkspace,
)
from quant_core.research.storage import write_bytes_atomic, write_json_atomic


ManagedRunner = Callable[..., Path]
LoopReporter = Callable[[Path, ResearchWorkspace, dict[str, Any]], Path]
ContainerPreflight = Callable[..., None]
ProviderPreflight = Callable[[ResearchTask, Path], None]
EnvironmentProbe = Callable[[], EvaluationEnvironment]
ParentTestPreflight = Callable[..., dict[str, Any] | None]
_ROUND_ID = re.compile(r"^(\d+)$")


def _task_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()



def _applied_round_decision(
    manager: ResearchWorkspace,
    task_state: dict[str, Any],
    round_id: str,
) -> str | None:
    record_id = f"{manager.run_id}/{round_id}"
    experiment = manager.rounds / round_id
    decision_path = experiment / "decision.json"
    if not (experiment / "result.json").is_file() or not decision_path.is_file():
        return None
    if task_state.get("last_round_id", task_state.get("last_experiment_id")) not in {
        round_id,
        record_id,
    }:
        return None
    try:
        decision = json.loads(decision_path.read_text(encoding="utf-8")).get("decision")
    except (OSError, json.JSONDecodeError):
        return None
    if decision not in {"accepted", "rejected", "failed"}:
        return None
    if decision == "accepted" and task_state.get("champion_round_id") not in {
        round_id,
        record_id,
    }:
        return None
    return str(decision)


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


def _adopt_legacy_runner_round(
    manager: ResearchWorkspace,
    round_id: str,
) -> Path | None:
    """Move output from a pre-schema-5 injected runner into artifacts/."""
    legacy_round = manager.run_root / "rounds" / round_id
    canonical_round = manager.rounds / round_id
    if legacy_round.is_dir():
        canonical_round.parent.mkdir(parents=True, exist_ok=True)
        if canonical_round.exists():
            for child in legacy_round.iterdir():
                shutil.move(str(child), str(canonical_round / child.name))
            legacy_round.rmdir()
        else:
            shutil.move(str(legacy_round), str(canonical_round))
    try:
        legacy_round.parent.rmdir()
    except OSError:
        pass
    return canonical_round if canonical_round.is_dir() else None


def _sync_guard_query_count(
    state: dict[str, Any], manager: ResearchWorkspace
) -> None:
    if not manager.rounds.is_dir():
        state["guard_query_count"] = 0
        return
    state["guard_query_count"] = sum(
        (round_path / "guard.json").is_file()
        for round_path in manager.rounds.iterdir()
        if round_path.is_dir()
    )


def _save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _timestamp()
    write_json_atomic(path, state)


def _freeze_terminal_champion(
    task: ResearchTask,
    manager: ResearchWorkspace,
    state: dict[str, Any],
) -> None:
    """Freeze the Run's final Champion before the Run becomes resumable history."""
    champion_state = manager.load_state(task.strategy_path)
    champion_sha256 = champion_state.get("champion_sha256")
    state["final_champion_sha256"] = champion_sha256
    state["final_champion_round_id"] = champion_state.get("champion_round_id")
    state["final_champion_number"] = champion_state.get("champion_number")
    state["final_champion_metrics_record"] = champion_state.get(
        "champion_metrics_record"
    )
    if not isinstance(champion_sha256, str):
        manager.terminal_champion_path.unlink(missing_ok=True)
        return
    source = manager.champion_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != champion_sha256:
        raise RuntimeError("Champion changed while the Run terminal snapshot was captured")
    manager.terminal_champion_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manager.terminal_champion_path.with_suffix(".py.tmp")
    temporary.write_bytes(source)
    temporary.replace(manager.terminal_champion_path)


def _finish_with_report(
    task_file: Path,
    task: ResearchTask,
    manager: ResearchWorkspace,
    loop_state_path: Path,
    state: dict[str, Any],
    reason: str,
    reporter: LoopReporter,
) -> Path:
    _freeze_terminal_champion(task, manager, state)
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
        manager.emit_event("report_interrupted", message="report interrupted")
        manager.finalize_diagnostics(state)
        manager.cleanup_transient(remove_development_cache=True)
        _save(loop_state_path, state)
        raise
    except Exception as exc:
        state["report_status"] = "failed"
        state["report_error"] = str(exc)
        state["report_failure_kind"] = getattr(exc, "failure_kind", None)
        state["report_failure_code"] = getattr(exc, "failure_code", None)
        manager.emit_event("report_failed", message="report failed")
    else:
        state["report_status"] = "completed"
        try:
            state["report_path"] = report_path.relative_to(manager.run_root).as_posix()
        except ValueError:
            state["report_path"] = str(report_path)
        manager.emit_event("report_completed", message="report completed")

    if state.get("report_status") == "failed":
        state["report_path"] = "report.md"
        write_minimal_loop_report(task, manager, state)
    try:
        _synchronize_production_strategy(task, manager, state)
    except Exception as exc:
        state["production_sync_status"] = "failed"
        state["production_sync_error"] = str(exc)
        manager.emit_event("production_sync_failed", message="production sync failed")
        _save(loop_state_path, state)
    manager.emit_event("run_completed", message=f"stopped: {reason}")
    manager.finalize_diagnostics(state)
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
    environment_probe: EnvironmentProbe | None = None,
    parent_test_preflight: ParentTestPreflight | None = None,
    retain_diagnostics: bool = False,
) -> Path:
    """Run managed research rounds until one of the configured budgets is exhausted."""
    task_file = Path(task_path).resolve()
    task = ResearchTask.load(task_file)
    source = Path(workspace).resolve()
    managed_root = Path(research_root)
    if not managed_root.is_absolute():
        managed_root = source / managed_root
    if environment_probe is None:
        if managed_runner is run_managed_once:
            environment = capture_evaluation_environment()
        else:
            environment = EvaluationEnvironment.from_manifest({
                "schema_version": 1,
                "runner": "injected",
            })
    else:
        environment = environment_probe()
    base_manager = ResearchWorkspace(
        source,
        managed_root,
        task.task_id,
        evaluation_environment_sha256=environment.sha256,
    )
    base_manager.migrate_legacy_loop()
    recovered_sync = _recover_unresolved_production_sync(task, base_manager)
    if recovered_sync is not None:
        return recovered_sync
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

    evaluation_view: Path | None = None
    evaluation_manifest: dict[str, Any] | None = None
    resolved_payload: dict[str, Any] | None = None
    if task.relative_period_config is not None:
        if active_runs:
            active_manager = base_manager.for_run(active_runs[0][0])
            try:
                evaluation_manifest = json.loads(
                    active_manager.evaluation_inputs_path.read_text(encoding="utf-8")
                )
                resolved_payload = json.loads(
                    active_manager.resolved_periods_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise DevelopmentInputsError(
                    "Active Run relative input contracts are unavailable"
                ) from exc
            task = bind_persisted_periods(task, resolved_payload)
            evaluation_view = active_manager.evaluation_runtime
        else:
            evaluation_view, evaluation_manifest = build_evaluation_view(
                source, base_manager.evaluation_views
            )
            task = resolve_relative_periods(
                task, source=source, runtime=evaluation_view
            )
            resolved_payload = {
                "schema_version": 1,
                "periods": dict(task.resolved_periods or {}),
                "resolution": dict(task.period_resolution or {}),
            }
        base_manager.evaluation_runtime_override = evaluation_view
    evaluator_contract_sha256 = base_manager.evaluator_contract_sha256(
        task.evaluator_contract_paths,
        strategy_path=task.strategy_path,
    )
    persist_evaluation_environment(base_manager.root, environment)
    development_end = task.development_period["end"]
    base_manager.initialize(
        date.fromisoformat(development_end),
        task.baseline_mode,
        task.baseline_exclude,
        task.strategy_path,
        evaluation_runtime=evaluation_view,
    )
    development_manifest = base_manager.development_manifest()
    development_view_sha256 = str(
        development_manifest["development_view_sha256"]
    )
    metrics_key_value = metrics_key(task)
    task_state = base_manager.load_state(task.strategy_path)
    base_manager.refresh_champion_metrics_status(
        task_state,
        metrics_key_value,
        task.evaluator_contract_paths,
        evaluator_contract_sha256=evaluator_contract_sha256,
        gate_runtime=evaluation_view,
    )
    if active_runs:
        run_number, state = active_runs[0]
        state = _normalize_state(state)
        if state.get("task_fingerprint") != fingerprint:
            raise ValueError("task.toml changed while a research loop was running")
        manager = base_manager.for_run(run_number)
        diagnostics_enabled = bool(state.get("diagnostics_enabled", False) or retain_diagnostics)
        state["diagnostics_enabled"] = diagnostics_enabled
        manager.diagnostics_enabled = diagnostics_enabled
        incompatible_development_inputs = (
            state.get("schema_version") not in {4, 5, 6, 7}
            or state.get("development_view_sha256") != development_view_sha256
            or state.get("development_end") != development_end
            or not manager.development_inputs_path.is_file()
        )
        if not incompatible_development_inputs:
            try:
                manager.freeze_run_development_inputs()
            except DevelopmentInputsError:
                incompatible_development_inputs = True
        if incompatible_development_inputs:
            state["status"] = "stopped"
            state["stop_reason"] = "infrastructure_failure"
            state["failure_kind"] = "infrastructure"
            state["failure_code"] = "development_inputs_incompatible"
            state["failure_message"] = (
                "Active Run lacks the frozen Development input contract "
                "or its inputs changed"
            )
            _save(manager.loop_state_path, state)
            return _finish_with_report(
                task_file,
                task,
                manager,
                manager.loop_state_path,
                state,
                "infrastructure_failure",
                reporter,
            )
    else:
        run_number = base_manager.next_run_number()
        manager = base_manager.for_run(run_number)
        diagnostics_enabled = retain_diagnostics
        manager.diagnostics_enabled = diagnostics_enabled

    if active_runs and state.get("evaluation_environment_sha256") != environment.sha256:
        current = state.get("current_round")
        if isinstance(current, str):
            record_id = f"{manager.run_id}/{current}"
            experiment = manager.rounds / current
            experiment.mkdir(parents=True, exist_ok=True)
            applied = _applied_round_decision(
                manager,
                manager.load_state(task.strategy_path),
                current,
            )
            if applied is None:
                result_path = experiment / "result.json"
                if not result_path.exists():
                    write_json_atomic(result_path, {
                        "experiment_id": record_id,
                        "status": "failed",
                        "error": "Evaluation environment changed before the interrupted Round resumed",
                        "failure_kind": "infrastructure",
                        "failure_code": "evaluation_environment_changed",
                        "evaluation_environment_sha256": environment.sha256,
                    })
                write_json_atomic(experiment / "decision.json", {
                    "experiment_id": record_id,
                    "decision": "failed",
                    "reasons": [
                        "evaluation environment changed before the interrupted Round resumed"
                    ],
                    "failure_kind": "infrastructure",
                    "failure_code": "evaluation_environment_changed",
                })
                applied = "failed"
            _record_decision(state, current, applied)
        state["resume_environment_sha256"] = environment.sha256
        _save(manager.loop_state_path, state)
        return _finish_with_report(
            task_file,
            task,
            manager,
            manager.loop_state_path,
            state,
            "infrastructure_failure",
            reporter,
        )
    # Injected managed runners own their execution environment. The production
    # runner must prove its Docker boundary before a Run is allocated or resumed.
    preflight = container_preflight
    if preflight is None and managed_runner is run_managed_once:
        preflight = preflight_agent_container
    if preflight is not None:
        try:
            parameters = tuple(inspect.signature(preflight).parameters.values())
            parameter_count = len(inspect.signature(preflight).parameters)
            accepts_view = (
                any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters)
                or parameter_count >= 4
            )
            accepts_source = (
                any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters)
                or parameter_count >= 5
            )
        except (TypeError, ValueError):
            accepts_view = False
        if accepts_view:
            arguments = [
                task,
                managed_root,
                base_manager.development_runtime,
                development_manifest,
            ]
            if accepts_source:
                arguments.append(source)
            preflight(*arguments)
        else:
            preflight(task, managed_root)

    authentication_preflight = provider_preflight
    if authentication_preflight is None and managed_runner is run_managed_once:
        authentication_preflight = preflight_provider_authentication
    if authentication_preflight is not None and not active_runs:
        authentication_preflight(task, managed_root)

    fixed_test_preflight = parent_test_preflight
    if fixed_test_preflight is None and managed_runner is run_managed_once:
        fixed_test_preflight = run_parent_fixed_tests

    def check_parent_fixed_tests(evidence_root: Path) -> None:
        if fixed_test_preflight is None:
            return
        current_state = base_manager.load_state(task.strategy_path)
        if not isinstance(current_state.get("champion_sha256"), str):
            return
        try:
            signature = inspect.signature(fixed_test_preflight)
            accepts_evidence = (
                "evidence_root" in signature.parameters
                or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
        except (TypeError, ValueError):
            accepts_evidence = True
        if accepts_evidence:
            fixed_test_preflight(
                task,
                manager,
                environment,
                evidence_root=evidence_root,
            )
        else:
            fixed_test_preflight(task, manager, environment)

    parent_checked_before_lifecycle = False
    if not active_runs:
        check_parent_fixed_tests(base_manager.root / "preflight-failures")
        parent_checked_before_lifecycle = True

    if active_runs:
        state["status"] = "running"
        state["stop_reason"] = None
        lifecycle_event = ("run_resumed", "resumed")
    else:
        manager.run_root.mkdir(parents=True, exist_ok=False)
        manager.run_artifacts_root.mkdir(parents=True, exist_ok=False)
        if evaluation_manifest is not None and resolved_payload is not None:
            manager.freeze_run_evaluation_inputs(
                evaluation_manifest, resolved_payload
            )
        manager.freeze_run_development_inputs()
        manager.rounds.mkdir(parents=True, exist_ok=False)
        state = _new_state(
            task,
            fingerprint,
            run_number,
            environment.sha256,
            diagnostics_enabled,
            development_view_sha256,
            development_end,
            (
                str(evaluation_manifest["evaluation_inputs_sha256"])
                if evaluation_manifest is not None
                else None
            ),
            (
                dict(resolved_payload["periods"])
                if resolved_payload is not None
                else None
            ),
            (
                str(task_state["champion_sha256"])
                if isinstance(task_state.get("champion_sha256"), str)
                else None
            ),
            (
                int(task_state["champion_number"])
                if isinstance(task_state.get("champion_number"), int)
                else None
            ),
            (
                str(task_state["champion_round_id"])
                if isinstance(task_state.get("champion_round_id"), str)
                else None
            ),
            (
                _sha256_path(source / task.strategy_path)
                if task.production is not None
                else None
            ),
        )
        lifecycle_event = ("run_started", "started")
    loop_state_path = manager.loop_state_path
    _save(loop_state_path, state)
    manager.emit_event(lifecycle_event[0], message=lifecycle_event[1])

    if (
        not active_runs
        and task.production is not None
        and isinstance(state.get("initial_champion_sha256"), str)
        and state.get("initial_production_strategy_sha256")
        != state.get("initial_champion_sha256")
    ):
        error = "production strategy was not synchronized with the initial Champion"
        record = _production_sync_record(
            task,
            manager,
            state,
            status="conflict",
            observed_sha256=state.get("initial_production_strategy_sha256"),
            error=error,
        )
        _write_production_sync_record(manager, state, record)
        state["status"] = "stopped"
        state["stop_reason"] = "production_sync_conflict"
        _save(loop_state_path, state)
        manager.emit_event("production_sync_conflict", message=error)
        manager.emit_event("run_completed", message="stopped: production_sync_conflict")
        manager.cleanup_transient(remove_development_cache=True)
        return loop_state_path

    current = state.get("current_round", state.get("current_experiment_id"))
    if isinstance(current, str):
        decision_path = manager.rounds / current / "decision.json"
        task_state = manager.load_state(task.strategy_path)
        record_id = f"{manager.run_id}/{current}"
        decision = _applied_round_decision(manager, task_state, current)
        if decision is None:
            decision = "failed"
            decision_path.parent.mkdir(parents=True, exist_ok=True)
            result_path = decision_path.parent / "result.json"
            if not result_path.exists():
                write_json_atomic(result_path, {
                    "experiment_id": record_id,
                    "status": "failed",
                    "error": "Loop was interrupted before the round was finalized",
                    "evaluation_environment_sha256": environment.sha256,
                })
            write_json_atomic(decision_path, {
                "experiment_id": record_id,
                "decision": "failed",
                "reasons": ["loop was interrupted before the round was finalized"],
            })
        _record_decision(state, current, str(decision))
        _sync_guard_query_count(state, manager)
        _save(loop_state_path, state)

    while True:
        task_state = manager.load_state(task.strategy_path)
        manager.refresh_champion_metrics_status(
            task_state,
            metrics_key_value,
            task.evaluator_contract_paths,
        )
        champion_metrics = manager.valid_champion_metrics(task_state)
        reason = _stop_reason(
            state,
            task,
            champion_metrics,
        )
        if reason is not None:
            return _finish_with_report(
                task_file,
                task,
                manager,
                loop_state_path,
                state,
                reason,
                reporter,
            )

        if parent_checked_before_lifecycle:
            parent_checked_before_lifecycle = False
        else:
            try:
                check_parent_fixed_tests(manager.preflight_failures_root)
            except Exception as exc:
                if getattr(exc, "failure_kind", None) != "infrastructure":
                    raise
                state["status"] = "stopped"
                state["failure_kind"] = "infrastructure"
                state["failure_code"] = getattr(
                    exc,
                    "failure_code",
                    "parent_fixed_tests_unavailable",
                )
                state["failure_message"] = str(exc)
                evidence_path = getattr(exc, "evidence_path", None)
                preflight_failure = {
                    "failure_kind": "infrastructure",
                    "failure_code": state["failure_code"],
                    "message": str(exc),
                }
                if isinstance(evidence_path, Path):
                    relative_evidence = (
                        evidence_path.relative_to(manager.run_artifacts_root).as_posix()
                    )
                    state["failure_evidence"] = relative_evidence
                    preflight_failure["evidence_path"] = relative_evidence
                state["preflight_failure"] = preflight_failure
                _save(loop_state_path, state)
                return _finish_with_report(
                    task_file,
                    task,
                    manager,
                    loop_state_path,
                    state,
                    "infrastructure_failure",
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
                    task,
                    manager,
                    loop_state_path,
                    state,
                    "infrastructure_failure",
                    reporter,
                )

        round_ids = state.get("round_ids")
        reserved = round_ids if isinstance(round_ids, list) else []
        round_id = _next_round_id(manager.rounds, reserved)
        prepared_candidate: tuple[Path, Path, dict[str, Any]] | None = None
        if managed_runner is run_managed_once:
            candidate: Path | None = None
            probe_root = manager.run_temp / "bind-probes" / round_id
            try:
                candidate, candidate_state = manager.prepare_candidate(
                    round_id,
                    date.fromisoformat(development_end),
                    task.baseline_mode,
                    task.baseline_exclude,
                    task.strategy_path,
                )
                probe_candidate_bind_source(
                    candidate,
                    probe_root,
                    event_sink=manager.emit_event,
                    round_id=round_id,
                )
                experiment = manager.activate_candidate(candidate, round_id)
                probe_evidence = experiment / "bind-probe"
                probe_evidence.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(probe_root), str(probe_evidence))
                prepared_candidate = (candidate, experiment, candidate_state)
            except CandidateBindPreflightError as exc:
                if candidate is not None and candidate.exists():
                    manager.discard_prepared_candidate(candidate)
                diagnostic = manager.run_diagnostics_root / f"bind-probe-{round_id}"
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                source = exc.evidence_path.parent
                if source.exists():
                    shutil.move(str(source), str(diagnostic))
                state["preflight_failure"] = {
                    "failure_kind": "infrastructure",
                    "failure_code": exc.code,
                    "message": str(exc),
                    "evidence_path": diagnostic.relative_to(
                        manager.run_artifacts_root
                    ).as_posix(),
                }
                _save(loop_state_path, state)
                return _finish_with_report(
                    task_file,
                    task,
                    manager,
                    loop_state_path,
                    state,
                    "infrastructure_failure",
                    reporter,
                )
            except KeyboardInterrupt:
                if candidate is not None and candidate.exists():
                    manager.discard_prepared_candidate(candidate)
                state["status"] = "interrupted"
                state["stop_reason"] = "interrupted"
                manager.emit_event(
                    "run_interrupted",
                    message="interrupted before Round allocation",
                )
                manager.finalize_diagnostics(state)
                manager.cleanup_transient(preserve_worktree_parents=True)
                _save(loop_state_path, state)
                return loop_state_path
            except Exception:
                if candidate is not None and candidate.exists():
                    manager.discard_prepared_candidate(candidate)
                raise
        state["current_round"] = round_id
        _save(loop_state_path, state)
        manager.emit_event("round_started", round=round_id, message="candidate started")
        started = monotonic()
        try:
            runner_options = {
                "workspace": source,
                "research_root": managed_root,
                "run_number": run_number,
                "event_sink": manager.emit_event,
            }
            if managed_runner is run_managed_once:
                runner_options["evaluation_environment"] = environment
                runner_options["prepared_candidate"] = prepared_candidate
            else:
                # Older injected runners derive the former Round parent from
                # research_root. It is removed immediately after their output
                # is adopted into the schema-5 artifacts tree.
                (manager.run_root / "rounds").mkdir(exist_ok=True)
            result_path = managed_runner(task_file, round_id, **runner_options)
        except KeyboardInterrupt:
            _adopt_legacy_runner_round(manager, round_id)
            state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
            state["status"] = "interrupted"
            state["stop_reason"] = "interrupted"
            manager.emit_event("run_interrupted", round=round_id, message="interrupted")
            manager.finalize_diagnostics(state)
            manager.cleanup_transient(preserve_worktree_parents=True)
            _save(loop_state_path, state)
            return loop_state_path
        except Exception:
            _adopt_legacy_runner_round(manager, round_id)
            state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
            state["status"] = "interrupted"
            state["stop_reason"] = "runner_error"
            manager.emit_event("runner_error", round=round_id, message="runner error")
            manager.finalize_diagnostics(state)
            manager.cleanup_transient(preserve_worktree_parents=True)
            _save(loop_state_path, state)
            raise

        # Keep injected/legacy runner adapters usable while schema-5 storage is
        # authoritative. Production runners already return the canonical path.
        canonical_round = manager.rounds / round_id
        if result_path.parent != canonical_round:
            legacy_round = manager.run_root / "rounds" / round_id
            if result_path.parent == legacy_round and legacy_round.is_dir():
                _adopt_legacy_runner_round(manager, round_id)
                result_path = canonical_round / result_path.name

        state["elapsed_seconds"] = float(state["elapsed_seconds"]) + max(0.0, monotonic() - started)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        decision_path = result_path.parent / "decision.json"
        decision = json.loads(decision_path.read_text(encoding="utf-8")).get("decision", "failed")
        _record_decision(state, round_id, str(decision))
        _sync_guard_query_count(state, manager)
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
                task,
                manager,
                loop_state_path,
                state,
                "infrastructure_failure",
                reporter,
            )
