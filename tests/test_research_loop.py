from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from quant_core.research import run_loop
from quant_core.research.contracts import ResearchTask
from quant_core.research.environment import EvaluationEnvironment
from quant_core.research.runner import AgentContainerInfrastructureError, _metrics_key
from quant_core.research.workspace import ResearchWorkspace


ENVIRONMENT = EvaluationEnvironment.from_manifest({
    "schema_version": 1,
    "runner": "injected",
})


TASK = """
id = "loop-test"
goal = "Improve the strategy"

[budget]
max_rounds = {max_rounds}
max_hours = {max_hours}
max_consecutive_failures = {max_failures}

[opencode]
model = "xai/grok-4.5"
variant = "high"
timeout_minutes = 60

[data]
universe = "universe.csv"

[scope]
editable = ["strategy.py"]
forbidden = ["evaluator.py"]

[commands]
test = ["test-command"]
backtest = ["backtest", "--start", "{{start}}", "--end", "{{end}}", "--run-id", "{{run_id}}"]
metrics_path = "outputs/backtests/{{run_id}}/metrics.json"

[evaluation]
mode = "fixed"
objective = "sortino"

[evaluation.constraints]
max_drawdown = {{ operator = "abs<=", threshold = 0.20 }}

[evaluation.fixed.development]
start = "2018-01-01"
end = "2021-12-31"

[evaluation.fixed.gate]
start = "2022-01-01"
end = "2024-12-31"

[evaluation.test]
start = "2025-01-01"
end = "2025-12-31"
"""


def _init_repo(root: Path) -> None:
    if (root / ".git").exists():
        return
    (root / ".gitignore").write_text(".research/\ndata/\noutputs/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=Test", "-c",
        "user.email=test@example.invalid", "commit", "-q", "-m", "init",
    ], check=True)


def _task(
    root: Path,
    *,
    max_rounds: int = 5,
    max_hours: int = 4,
    max_failures: int = 2,
    target: float | None = None,
) -> Path:
    path = root / "task.toml"
    content = TASK.format(
        max_rounds=max_rounds,
        max_hours=max_hours,
        max_failures=max_failures,
    )
    if target is not None:
        content += f"\n[evaluation.target]\nobjective_at_least = {target}\n"
    path.write_text(content, encoding="utf-8")
    (root / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(root)
    return path


def _runner(decisions: list[str]):
    remaining = iter(decisions)

    def run(
        task_path: Path,
        experiment_id: str,
        *,
        workspace: Path,
        research_root: Path,
        run_number: int,
        event_sink,
    ) -> Path:
        decision = next(remaining)
        experiment = (
            research_root / "loop-test" / "runs" / f"{run_number:03d}"
            / "rounds" / experiment_id
        )
        experiment.mkdir()
        result_path = experiment / "result.json"
        result_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        (experiment / "decision.json").write_text(json.dumps({
            "experiment_id": experiment_id,
            "decision": decision,
        }), encoding="utf-8")
        return result_path

    return run


def _reporter(task_path: Path, manager: ResearchWorkspace, state: dict[str, object]) -> Path:
    report = manager.report_path
    report.write_text("# Test report\n", encoding="utf-8")
    return report


def _running_state(task: Path, experiment_id: str = "001") -> dict[str, object]:
    return {
        "schema_version": 3,
        "task_id": "loop-test",
        "run_number": 1,
        "task_fingerprint": hashlib.sha256(task.read_bytes()).hexdigest(),
        "evaluation_environment_sha256": ENVIRONMENT.sha256,
        "status": "running",
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "elapsed_seconds": 0.0,
        "rounds_completed": 0,
        "accepted": 0,
        "rejected": 0,
        "failed": 0,
        "consecutive_failures": 0,
        "round_ids": [],
        "current_round": experiment_id,
        "last_round": None,
        "stop_reason": None,
    }


def test_loop_stops_after_consecutive_failed_rounds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task = _task(tmp_path, max_failures=2)

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected", "failed", "failed"]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_consecutive_failures"
    assert state["rounds_completed"] == 3
    assert state["rejected"] == 1
    assert state["failed"] == 2
    assert state["round_ids"] == [
        "001",
        "002",
        "003",
    ]
    assert state["report_status"] == "completed"
    assert state["report_path"] == "report.md"
    assert state_path == tmp_path / ".research/loop-test/runs/001/state.json"
    assert (state_path.parent / "report.md").exists()
    assert not (tmp_path / ".research/loop-test/.tmp/runs/001/events.jsonl").exists()
    output = capsys.readouterr().out
    assert "[001] started" in output
    assert "[001/001] candidate started" in output
    assert "[001/003] completed: failed" in output


def test_container_preflight_failure_does_not_allocate_a_run(tmp_path: Path) -> None:
    task = _task(tmp_path)

    def fail_preflight(task, research_root: Path) -> None:
        raise RuntimeError("container mount unavailable")

    with pytest.raises(RuntimeError, match="container mount unavailable"):
        run_loop(
            task,
            workspace=tmp_path,
            managed_runner=_runner(["rejected"]),
            reporter=_reporter,
            container_preflight=fail_preflight,
        )

    assert not (tmp_path / ".research/loop-test/runs").exists()


def test_environment_preflight_failure_does_not_allocate_a_run(tmp_path: Path) -> None:
    task = _task(tmp_path)

    def fail_environment() -> EvaluationEnvironment:
        raise RuntimeError("Research Harness requires Conda environment 'quant'")

    with pytest.raises(RuntimeError, match="requires Conda environment 'quant'"):
        run_loop(
            task,
            workspace=tmp_path,
            managed_runner=_runner(["rejected"]),
            reporter=_reporter,
            environment_probe=fail_environment,
        )

    assert not (tmp_path / ".research/loop-test").exists()


def test_provider_preflight_failure_does_not_allocate_a_run(tmp_path: Path) -> None:
    task = _task(tmp_path)

    def fail_authentication(task, research_root: Path) -> None:
        raise AgentContainerInfrastructureError("Provider authentication failed")

    with pytest.raises(AgentContainerInfrastructureError, match="authentication failed"):
        run_loop(
            task,
            workspace=tmp_path,
            managed_runner=_runner(["rejected"]),
            reporter=_reporter,
            provider_preflight=fail_authentication,
        )

    assert not (tmp_path / ".research/loop-test/runs").exists()


def test_provider_preflight_stops_before_allocating_next_round(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=3)
    preflight_calls = 0

    def authentication_preflight(task, research_root: Path) -> None:
        nonlocal preflight_calls
        preflight_calls += 1
        if preflight_calls == 2:
            raise AgentContainerInfrastructureError("Provider authentication failed")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected", "accepted"]),
        reporter=_reporter,
        provider_preflight=authentication_preflight,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "infrastructure_failure"
    assert state["rounds_completed"] == 1
    assert state["round_ids"] == ["001"]
    assert not (state_path.parent / "rounds/002").exists()


def test_infrastructure_failure_stops_after_one_round(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=5, max_failures=3)

    def fail_infrastructure(
        task_path: Path,
        experiment_id: str,
        *,
        workspace: Path,
        research_root: Path,
        run_number: int,
        event_sink,
    ) -> Path:
        experiment = (
            research_root / "loop-test" / "runs" / f"{run_number:03d}"
            / "rounds" / experiment_id
        )
        experiment.mkdir()
        result_path = experiment / "result.json"
        result_path.write_text(json.dumps({
            "experiment_id": f"{run_number:03d}/{experiment_id}",
            "status": "failed",
            "error": "Agent container infrastructure failure: bind source unavailable",
            "failure_kind": "infrastructure",
        }), encoding="utf-8")
        (experiment / "decision.json").write_text(json.dumps({
            "experiment_id": f"{run_number:03d}/{experiment_id}",
            "decision": "failed",
            "failure_kind": "infrastructure",
            "reasons": ["bind source unavailable"],
        }), encoding="utf-8")
        return result_path

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=fail_infrastructure,
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "infrastructure_failure"
    assert state["rounds_completed"] == 1
    assert state["failed"] == 1
    assert state["round_ids"] == ["001"]
    assert not (state_path.parent / "rounds/002").exists()


def test_rejected_round_breaks_a_failure_streak(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=3, max_failures=2)

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["failed", "rejected", "failed"]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_rounds"
    assert state["consecutive_failures"] == 1


def test_loop_does_not_start_another_round_after_time_budget(tmp_path: Path) -> None:
    task = _task(tmp_path, max_hours=1)
    times = iter([0.0, 3600.0])

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
        monotonic=lambda: next(times),
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_hours"
    assert state["rounds_completed"] == 1


def test_loop_stops_when_champion_reaches_target(tmp_path: Path) -> None:
    task = _task(tmp_path, target=1.5)

    def reach_target(
        task_path: Path,
        experiment_id: str,
        *,
        workspace: Path,
        research_root: Path,
        run_number: int,
        event_sink,
    ) -> Path:
        manager = ResearchWorkspace(
            workspace,
            research_root,
            "loop-test",
            run_number=run_number,
            evaluation_environment_sha256=ENVIRONMENT.sha256,
        )
        task_state = manager.load_state()
        metrics = {"gate": {"sortino": 1.5, "max_drawdown": -0.10}}
        loaded_task = ResearchTask.load(task_path)
        metrics_record = manager.metrics_record(
            metrics,
            manager.metrics_applicability(task_state, _metrics_key(loaded_task)),
            f"{run_number:03d}/{experiment_id}",
        )
        manager.record_state(task_state, experiment_id, metrics_record)
        experiment = manager.rounds / experiment_id
        experiment.mkdir()
        result_path = experiment / "result.json"
        result_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        (experiment / "decision.json").write_text(json.dumps({
            "experiment_id": experiment_id,
            "decision": "accepted",
        }), encoding="utf-8")
        return result_path

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=reach_target,
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "target_reached"
    assert state["rounds_completed"] == 1


def test_rebuilt_runtime_keeps_valid_metrics_for_target_stop_before_a_round(
    tmp_path: Path,
) -> None:
    task_path = _task(tmp_path, target=1.5)
    task = ResearchTask.load(task_path)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    state = manager.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    state["champion_metrics_record"] = manager.metrics_record(
        {"gate": {"sortino": 1.5, "max_drawdown": -0.10}},
        manager.metrics_applicability(state, _metrics_key(task)),
        "001/001",
    )
    manager.record_state(state, "001/001")
    manager.cleanup_transient(remove_development_cache=True)

    def unexpected_runner(*args, **kwargs):
        raise AssertionError("a target-reaching Champion must stop before a new round")

    state_path = run_loop(
        task_path,
        workspace=tmp_path,
        managed_runner=unexpected_runner,
        reporter=_reporter,
    )

    loop_state = json.loads(state_path.read_text(encoding="utf-8"))
    champion_state = json.loads(manager.state_path.read_text(encoding="utf-8"))
    assert loop_state["stop_reason"] == "target_reached"
    assert loop_state["rounds_completed"] == 0
    assert champion_state["champion_metrics_record"]["status"] == "valid"


def test_preflight_failure_does_not_delete_valid_champion_metrics(tmp_path: Path) -> None:
    task_path = _task(tmp_path)
    task = ResearchTask.load(task_path)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    state = manager.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    state["champion_metrics_record"] = manager.metrics_record(
        {"gate": {"sortino": 1.2, "max_drawdown": -0.10}},
        manager.metrics_applicability(state, _metrics_key(task)),
        "001/001",
    )
    manager.record_state(state, "001/001")
    manager.cleanup_transient(remove_development_cache=True)

    def fail_preflight(task: ResearchTask, research_root: Path) -> None:
        raise RuntimeError("container unavailable")

    with pytest.raises(RuntimeError, match="container unavailable"):
        run_loop(
            task_path,
            workspace=tmp_path,
            managed_runner=_runner([]),
            reporter=_reporter,
            container_preflight=fail_preflight,
        )

    champion_state = json.loads(manager.state_path.read_text(encoding="utf-8"))
    assert champion_state["champion_metrics_record"]["status"] == "valid"
    assert champion_state["champion_metrics_record"]["metrics"]["gate"]["sortino"] == 1.2


def test_loop_recovers_a_completed_unrecorded_round(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=1)
    base = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    task_state = base.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    manager = base.for_run(1)
    manager.rounds.mkdir(parents=True)
    task_state["champion_round_id"] = "001/001"
    manager.record_state(task_state, "001/001")
    experiment = manager.rounds / "001"
    experiment.mkdir()
    (experiment / "result.json").write_text(json.dumps({
        "experiment_id": "001/001",
        "status": "completed",
    }), encoding="utf-8")
    (experiment / "decision.json").write_text(json.dumps({
        "experiment_id": "001/001",
        "decision": "accepted",
    }), encoding="utf-8")
    loop_state = manager.loop_state_path
    loop_state.write_text(json.dumps(_running_state(task)), encoding="utf-8")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_rounds"
    assert state["accepted"] == 1
    assert state["last_round"] == "001"
    assert state["round_ids"] == ["001"]


def test_loop_marks_an_incomplete_round_as_interrupted_failure(tmp_path: Path) -> None:
    task = _task(tmp_path, max_failures=1)
    experiment = tmp_path / ".research/loop-test/runs/001/rounds/001"
    experiment.mkdir(parents=True)
    loop_state = tmp_path / ".research/loop-test/runs/001/state.json"
    loop_state.write_text(json.dumps(_running_state(task)), encoding="utf-8")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    decision = json.loads((experiment / "decision.json").read_text(encoding="utf-8"))
    result = json.loads((experiment / "result.json").read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_consecutive_failures"
    assert state["failed"] == 1
    assert decision["decision"] == "failed"
    assert result["status"] == "failed"


def test_loop_stops_instead_of_resuming_under_a_changed_environment(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, max_rounds=2, max_failures=2)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        run_number=1,
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    manager.rounds.mkdir(parents=True)
    manager.loop_state_path.write_text(
        json.dumps(_running_state(task)),
        encoding="utf-8",
    )
    changed = EvaluationEnvironment.from_manifest({
        "schema_version": 1,
        "runner": "changed",
    })

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["accepted"]),
        reporter=_reporter,
        environment_probe=lambda: changed,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    result = json.loads(
        (manager.rounds / "001/result.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "infrastructure_failure"
    assert state["rounds_completed"] == 1
    assert state["resume_environment_sha256"] == changed.sha256
    assert result["failure_code"] == "evaluation_environment_changed"
    assert not (manager.rounds / "002").exists()


def test_changed_environment_preserves_an_applied_round(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, max_rounds=2, max_failures=2)
    base = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    task_state = base.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    task_state["champion_round_id"] = "001/001"
    task_state["champion_number"] = 1
    manager = base.for_run(1)
    manager.rounds.mkdir(parents=True)
    manager.record_state(task_state, "001/001")
    experiment = manager.rounds / "001"
    experiment.mkdir()
    result_payload = {
        "experiment_id": "001/001",
        "status": "completed",
        "candidate": "preserve this evidence",
    }
    decision_payload = {
        "experiment_id": "001/001",
        "decision": "accepted",
        "reasons": ["promotion completed"],
    }
    (experiment / "result.json").write_text(
        json.dumps(result_payload),
        encoding="utf-8",
    )
    (experiment / "decision.json").write_text(
        json.dumps(decision_payload),
        encoding="utf-8",
    )
    manager.loop_state_path.write_text(
        json.dumps(_running_state(task)),
        encoding="utf-8",
    )
    changed = EvaluationEnvironment.from_manifest({
        "schema_version": 1,
        "runner": "changed",
    })

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
        environment_probe=lambda: changed,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    champion = json.loads(base.state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "infrastructure_failure"
    assert state["accepted"] == 1
    assert state["failed"] == 0
    assert json.loads(
        (experiment / "result.json").read_text(encoding="utf-8")
    ) == result_payload
    assert json.loads(
        (experiment / "decision.json").read_text(encoding="utf-8")
    ) == decision_payload
    assert champion["champion_round_id"] == "001/001"


def test_loop_materializes_missing_interrupted_round_before_allocating_next(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, max_rounds=2, max_failures=2)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        run_number=1,
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    manager.rounds.mkdir(parents=True)
    manager.loop_state_path.write_text(
        json.dumps(_running_state(task)),
        encoding="utf-8",
    )

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    interrupted = manager.rounds / "001"
    next_round = manager.rounds / "002"
    assert state["stop_reason"] == "max_rounds"
    assert state["rounds_completed"] == 2
    assert state["failed"] == 1
    assert state["rejected"] == 1
    assert state["round_ids"] == ["001", "002"]
    assert json.loads((interrupted / "result.json").read_text(encoding="utf-8"))[
        "status"
    ] == "failed"
    assert json.loads((interrupted / "decision.json").read_text(encoding="utf-8"))[
        "decision"
    ] == "failed"
    assert next_round.is_dir()


def test_resumed_loop_materializes_current_round_before_authentication_fuse(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, max_rounds=3, max_failures=2)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        run_number=1,
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    manager.rounds.mkdir(parents=True)
    manager.loop_state_path.write_text(
        json.dumps(_running_state(task)),
        encoding="utf-8",
    )

    def fail_authentication(task, research_root: Path) -> None:
        raise AgentContainerInfrastructureError("Provider authentication failed")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
        provider_preflight=fail_authentication,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    interrupted = manager.rounds / "001"
    assert state["stop_reason"] == "infrastructure_failure"
    assert state["rounds_completed"] == 1
    assert state["failed"] == 1
    assert state["round_ids"] == ["001"]
    assert (interrupted / "result.json").is_file()
    assert (interrupted / "decision.json").is_file()
    assert not (manager.rounds / "002").exists()


def test_round_allocator_does_not_reuse_recorded_id_without_directory(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, max_rounds=2)
    state = _running_state(task)
    state.update({
        "rounds_completed": 1,
        "rejected": 1,
        "round_ids": ["001"],
        "current_round": None,
        "last_round": "001",
    })
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        run_number=1,
    )
    manager.rounds.mkdir(parents=True)
    manager.loop_state_path.write_text(json.dumps(state), encoding="utf-8")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
    )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["round_ids"] == ["001", "002"]
    assert saved["rounds_completed"] == 2
    assert not (manager.rounds / "001").exists()
    assert (manager.rounds / "002/result.json").is_file()


def test_loop_does_not_trust_a_decision_that_was_not_applied(tmp_path: Path) -> None:
    task = _task(tmp_path, max_failures=1)
    experiment = tmp_path / ".research/loop-test/runs/001/rounds/001"
    experiment.mkdir(parents=True)
    (experiment / "decision.json").write_text(json.dumps({
        "experiment_id": "001/001",
        "decision": "accepted",
    }), encoding="utf-8")
    loop_state = tmp_path / ".research/loop-test/runs/001/state.json"
    loop_state.write_text(json.dumps(_running_state(task)), encoding="utf-8")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    decision = json.loads((experiment / "decision.json").read_text(encoding="utf-8"))
    assert state["failed"] == 1
    assert decision["decision"] == "failed"


def test_interrupted_loop_is_resumed_on_the_next_invocation(tmp_path: Path) -> None:
    task = _task(tmp_path, max_failures=1)

    def interrupt(
        task_path: Path,
        experiment_id: str,
        *,
        workspace: Path,
        research_root: Path,
        run_number: int,
        event_sink,
    ) -> Path:
        manager = ResearchWorkspace(
            workspace,
            research_root,
            "loop-test",
            run_number=run_number,
            evaluation_environment_sha256=ENVIRONMENT.sha256,
        )
        manager.create_candidate(
            experiment_id,
            date(2021, 12, 31),
            strategy_path="strategy.py",
        )
        raise KeyboardInterrupt

    interrupted_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=interrupt,
        reporter=_reporter,
    )
    interrupted = json.loads(interrupted_path.read_text(encoding="utf-8"))
    assert interrupted["status"] == "interrupted"
    assert not (
        tmp_path / ".research/loop-test/.tmp/worktrees/001/candidates/001"
    ).exists()

    resumed_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
    )

    resumed = json.loads(resumed_path.read_text(encoding="utf-8"))
    assert resumed["stop_reason"] == "max_consecutive_failures"
    assert resumed["rounds_completed"] == 1


def test_loop_records_elapsed_time_before_reraising_runner_error(tmp_path: Path) -> None:
    task = _task(tmp_path)
    times = iter([10.0, 130.0])

    def fail(
        task_path: Path,
        experiment_id: str,
        *,
        workspace: Path,
        research_root: Path,
        run_number: int,
        event_sink,
    ) -> Path:
        raise RuntimeError("runner bug")

    with pytest.raises(RuntimeError, match="runner bug"):
        run_loop(
            task,
            workspace=tmp_path,
            managed_runner=fail,
            reporter=_reporter,
            monotonic=lambda: next(times),
        )

    state = json.loads(
        (tmp_path / ".research/loop-test/runs/001/state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "interrupted"
    assert state["stop_reason"] == "runner_error"
    assert state["elapsed_seconds"] == 120.0


def test_report_failure_does_not_change_loop_result(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=1)

    def fail_report(
        task_path: Path,
        manager: ResearchWorkspace,
        state: dict[str, object],
    ) -> Path:
        raise RuntimeError("report model unavailable")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=fail_report,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "max_rounds"
    assert state["rounds_completed"] == 1
    assert state["report_status"] == "failed"
    assert state["report_error"] == "report model unavailable"


def test_new_loop_on_same_root_tracks_only_its_own_experiments(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=1)
    reported_experiments: list[list[str]] = []

    def capture_report(
        task_path: Path,
        manager: ResearchWorkspace,
        state: dict[str, object],
    ) -> Path:
        round_ids = state["round_ids"]
        assert isinstance(round_ids, list)
        reported_experiments.append([str(round_id) for round_id in round_ids])
        return _reporter(task_path, manager, state)

    run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=capture_report,
    )
    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=capture_report,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert reported_experiments == [["001"], ["001"]]
    assert state["round_ids"] == ["001"]
    assert state_path == tmp_path / ".research/loop-test/runs/002/state.json"
    assert (tmp_path / ".research/loop-test/runs/001/report.md").exists()
    assert (tmp_path / ".research/loop-test/runs/002/report.md").exists()
