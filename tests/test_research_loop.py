from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import quant_core.research.loop as research_loop
import quant_core.research.champion_test as champion_test
from quant_core.research import run_loop
from quant_core.research.contracts import ResearchTask
from quant_core.research.environment import EvaluationEnvironment
from quant_core.research.runner import (
    AgentContainerInfrastructureError,
    CandidateBindPreflightError,
    ParentFixedTestsError,
    _metrics_key,
    run_parent_fixed_tests,
    run_managed_once,
)
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
round_minutes = 60

[opencode]
model = "xai/grok-4.5"
variant = "high"

[execution]
command_timeout_minutes = 60

[data]
universe = "universe.csv"

[scope]
editable = ["strategy.py"]

[commands]
tests = ["tests/test_strategy.py"]
backtest = ["backtest", "--start", "{{start}}", "--end", "{{end}}", "--run-id", "{{run_id}}"]

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

"""

TEST_PERIOD = """
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
    include_test: bool = False,
) -> Path:
    path = root / "task.toml"
    content = TASK.format(
        max_rounds=max_rounds,
        max_hours=max_hours,
        max_failures=max_failures,
    )
    if target is not None:
        content += f"\n[evaluation.target]\nobjective_at_least = {target}\n"
    if include_test:
        content += TEST_PERIOD
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


def _production_sync_fixture(
    tmp_path: Path,
) -> tuple[
    SimpleNamespace,
    ResearchWorkspace,
    ResearchWorkspace,
    dict[str, object],
]:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "loop-test")
    champion = base.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    manager = base.for_run(1)
    manager.run_artifacts_root.mkdir(parents=True)
    manager.terminal_champion_path.parent.mkdir(parents=True)
    manager.terminal_champion_path.write_text("2.0\n", encoding="utf-8")
    initial_sha256 = str(champion["champion_sha256"])
    final_sha256 = hashlib.sha256(b"2.0\n").hexdigest()
    base.champion_path.write_text("2.0\n", encoding="utf-8")
    champion_state = json.loads(base.state_path.read_text(encoding="utf-8"))
    champion_state["champion_sha256"] = final_sha256
    champion_state["champion_number"] = 1
    champion_state["champion_round_id"] = "001/001"
    base.state_path.write_text(json.dumps(champion_state), encoding="utf-8")
    state: dict[str, object] = {
        "schema_version": 6,
        "task_id": "loop-test",
        "run_number": 1,
        "status": "stopped",
        "stop_reason": "max_rounds",
        "production_sync_baseline_available": True,
        "initial_champion_sha256": initial_sha256,
        "initial_champion_number": 0,
        "initial_champion_round_id": None,
        "initial_production_strategy_sha256": initial_sha256,
        "final_champion_sha256": final_sha256,
        "final_champion_number": 1,
        "final_champion_round_id": "001/001",
        "production_sync_status": None,
        "production_sync_path": None,
        "production_sync_error": None,
    }
    research_loop._save(manager.loop_state_path, state)
    task = SimpleNamespace(
        production={"curve_months": 12, "benchmark": "510300"},
        task_id="loop-test",
        strategy_path="strategy.py",
    )
    return task, base, manager, state


def test_terminal_champion_is_atomically_synchronized_to_production(
    tmp_path: Path,
) -> None:
    task, _base, manager, state = _production_sync_fixture(tmp_path)

    research_loop._synchronize_production_strategy(task, manager, state)

    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "2.0\n"
    persisted = json.loads(manager.loop_state_path.read_text(encoding="utf-8"))
    audit = json.loads(manager.production_sync_path.read_text(encoding="utf-8"))
    assert persisted["production_sync_status"] == "completed"
    assert audit["status"] == "completed"
    assert audit["production_strategy_sha256"] == state["final_champion_sha256"]
    assert not manager.production_sync_champion_path.exists()


def test_production_sync_conflict_preserves_user_change_and_audit_source(
    tmp_path: Path,
) -> None:
    task, _base, manager, state = _production_sync_fixture(tmp_path)
    (tmp_path / "strategy.py").write_text("user change\n", encoding="utf-8")

    research_loop._synchronize_production_strategy(task, manager, state)

    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "user change\n"
    audit = json.loads(manager.production_sync_path.read_text(encoding="utf-8"))
    assert audit["status"] == "conflict"
    assert "changed outside" in audit["error"]
    assert manager.production_sync_champion_path.read_text(encoding="utf-8") == "2.0\n"


def test_unresolved_production_sync_recovers_before_allocating_another_run(
    tmp_path: Path,
) -> None:
    task, base, manager, state = _production_sync_fixture(tmp_path)
    (tmp_path / "strategy.py").write_text("user change\n", encoding="utf-8")
    research_loop._synchronize_production_strategy(task, manager, state)
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")

    recovered = research_loop._recover_unresolved_production_sync(task, base)

    assert recovered == manager.loop_state_path
    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "2.0\n"
    persisted = json.loads(manager.loop_state_path.read_text(encoding="utf-8"))
    audit = json.loads(manager.production_sync_path.read_text(encoding="utf-8"))
    assert persisted["production_sync_status"] == "completed"
    assert audit["recovered"] is True
    assert base.run_numbers() == [1]


def test_production_sync_is_not_needed_without_a_new_champion(tmp_path: Path) -> None:
    task, _base, manager, state = _production_sync_fixture(tmp_path)
    state["final_champion_sha256"] = state["initial_champion_sha256"]
    state["final_champion_number"] = state["initial_champion_number"]
    state["final_champion_round_id"] = state["initial_champion_round_id"]

    research_loop._synchronize_production_strategy(task, manager, state)

    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "1.0\n"
    assert state["production_sync_status"] == "not_needed"


def test_production_change_is_reported_even_when_champion_did_not_change(
    tmp_path: Path,
) -> None:
    task, _base, manager, state = _production_sync_fixture(tmp_path)
    state["final_champion_sha256"] = state["initial_champion_sha256"]
    (tmp_path / "strategy.py").write_text("user change\n", encoding="utf-8")

    research_loop._synchronize_production_strategy(task, manager, state)

    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "user change\n"
    assert state["production_sync_status"] == "conflict"


def test_loop_synchronizes_its_frozen_final_champion_after_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_path = _task(tmp_path, max_rounds=1)
    task = ResearchTask.load(task_path)
    task.raw["production"] = {"curve_months": 12, "benchmark": "510300"}
    monkeypatch.setattr(ResearchTask, "load", lambda _path: task)

    def promoting_runner(
        task_file: Path,
        round_id: str,
        *,
        workspace: Path,
        research_root: Path,
        run_number: int,
        event_sink,
    ) -> Path:
        del task_file, workspace, event_sink
        base = ResearchWorkspace(tmp_path, research_root, task.task_id)
        promoted = b"2.0\n"
        base.champion_path.write_bytes(promoted)
        champion_state = json.loads(base.state_path.read_text(encoding="utf-8"))
        champion_state["champion_sha256"] = hashlib.sha256(promoted).hexdigest()
        champion_state["champion_number"] = 1
        champion_state["champion_round_id"] = f"{run_number:03d}/{round_id}"
        champion_state["last_round_id"] = f"{run_number:03d}/{round_id}"
        base.state_path.write_text(json.dumps(champion_state), encoding="utf-8")
        experiment = base.for_run(run_number).rounds / round_id
        experiment.mkdir()
        result_path = experiment / "result.json"
        result_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        (experiment / "decision.json").write_text(json.dumps({
            "experiment_id": round_id,
            "decision": "accepted",
        }), encoding="utf-8")
        return result_path

    state_path = run_loop(
        task_path,
        workspace=tmp_path,
        managed_runner=promoting_runner,
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    audit = json.loads((state_path.parent / "production-sync.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == 6
    assert state["production_sync_status"] == "completed"
    assert audit["final_champion_round_id"] == "001/001"
    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "2.0\n"


def test_loop_stops_before_first_round_when_production_is_initially_unsynchronized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_path = _task(tmp_path)
    task = ResearchTask.load(task_path)
    task.raw["production"] = {"curve_months": 12, "benchmark": "510300"}
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", task.task_id)
    base.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    (tmp_path / "strategy.py").write_text("manual change\n", encoding="utf-8")
    monkeypatch.setattr(ResearchTask, "load", lambda _path: task)

    state_path = run_loop(
        task_path,
        workspace=tmp_path,
        managed_runner=lambda *args, **kwargs: pytest.fail("must not start a Round"),
        reporter=lambda *args: pytest.fail("must not generate a terminal report"),
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    audit = json.loads((state_path.parent / "production-sync.json").read_text(encoding="utf-8"))
    assert state["stop_reason"] == "production_sync_conflict"
    assert state["rounds_completed"] == 0
    assert state["production_sync_status"] == "conflict"
    assert audit["status"] == "conflict"
    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "manual change\n"


def test_legacy_interrupted_loop_is_migrated_before_active_run_scan(
    tmp_path: Path,
) -> None:
    task_path = _task(tmp_path)
    task = ResearchTask.load(task_path)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        task.task_id,
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    manager.initialize(
        date.fromisoformat(task.development_period["end"]),
        strategy_path=task.strategy_path,
    )
    fingerprint = hashlib.sha256(task_path.read_bytes()).hexdigest()
    manager.root.mkdir(parents=True, exist_ok=True)
    (manager.root / "loop-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": task.task_id,
                "status": "interrupted",
                "task_fingerprint": fingerprint,
                "rounds_completed": 0,
                "experiment_ids": [],
                "current_experiment_id": None,
                "last_experiment_id": None,
            }
        ),
        encoding="utf-8",
    )

    report = tmp_path / "report.md"
    result = run_loop(
        task_path,
        workspace=tmp_path,
        research_root=tmp_path / ".research",
        managed_runner=lambda *args, **kwargs: pytest.fail(
            "an incompatible migrated Run must not allocate a new Round"
        ),
        environment_probe=lambda: ENVIRONMENT,
        reporter=lambda *args: report,
    )

    assert result == manager.for_run(1).loop_state_path
    assert manager.run_numbers() == [1]
    assert not (manager.root / "runs/002").exists()


def _reporter(task_path: Path, manager: ResearchWorkspace, state: dict[str, object]) -> Path:
    report = manager.report_path
    report.write_text("# Test report\n", encoding="utf-8")
    return report


def _running_state(task: Path, experiment_id: str = "001") -> dict[str, object]:
    base = ResearchWorkspace(
        task.parent,
        task.parent / ".research",
        "loop-test",
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    champion = base.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    run = base.for_run(1)
    run.run_root.mkdir(parents=True, exist_ok=True)
    run.freeze_run_development_inputs()
    return {
        "schema_version": 4,
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
        "development_view_sha256": champion["development_view_sha256"],
        "development_end": champion["development_end"],
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
    assert state["schema_version"] == 6
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
    assert state["test_status"] == "not_configured"
    assert state_path == tmp_path / ".research/loop-test/runs/001/artifacts/state.json"
    assert (state_path.parent.parent / "report.md").exists()
    assert not (tmp_path / ".research/loop-test/.tmp/runs/001/events.jsonl").exists()
    output = capsys.readouterr().out
    assert "[001] started" in output
    assert "[001/001] candidate started" in output
    assert "[001/003] completed: failed" in output


def test_loop_automatically_tests_the_run_champion_after_report(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, max_rounds=1, include_test=True)
    stages: list[str] = []

    def reporter(
        task_path: Path,
        manager: ResearchWorkspace,
        state: dict[str, object],
    ) -> Path:
        stages.append("report")
        return _reporter(task_path, manager, state)

    def champion_tester(
        task_path: Path,
        research_task: ResearchTask,
        manager: ResearchWorkspace,
        state: dict[str, object],
    ) -> Path:
        stages.append("test")
        assert research_task.test_period == {
            "start": "2025-01-01",
            "end": "2025-12-31",
        }
        manager.run_test_root.mkdir(parents=True)
        manager.run_test_result_path.write_text(
            json.dumps({"metrics": {"sortino": 1.25}}),
            encoding="utf-8",
        )
        return manager.run_test_result_path

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=reporter,
        champion_tester=champion_tester,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert stages == ["report", "test"]
    assert state["stop_reason"] == "max_rounds"
    assert state["test_status"] == "completed"
    assert state["test_path"] == "test/result.json"
    assert json.loads(
        (state_path.parent / state["test_path"]).read_text(encoding="utf-8")
    )["metrics"]["sortino"] == 1.25
    run_root = state_path.parent.parent
    assert sorted(path.name for path in run_root.iterdir()) == [
        "artifacts",
        "report.md",
    ]
    report = (run_root / "report.md").read_text(encoding="utf-8")
    assert report.count("<!-- harness:test-observation:start -->") == 1
    assert "| sortino | 1.25 |" in report


def test_automatic_champion_test_failure_does_not_change_loop_result(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, max_rounds=1, include_test=True)

    def failing_tester(task_path, research_task, manager, state):
        manager.run_test_root.mkdir(parents=True)
        (manager.run_test_root / "test.log").write_text(
            "test evaluator detail", encoding="utf-8"
        )
        raise RuntimeError("test evaluator unavailable")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
        champion_tester=failing_tester,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "max_rounds"
    assert state["report_status"] == "completed"
    assert state["test_status"] == "failed"
    assert state["test_path"] is None
    assert state["test_error"] == "test evaluator unavailable"
    assert (state_path.parent / "test/test.log").read_text(
        encoding="utf-8"
    ) == "test evaluator detail"


def test_default_automatic_test_writes_run_scoped_auditable_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(tmp_path, max_rounds=1, include_test=True)
    (tmp_path / "data").mkdir()
    market_data = b"date,value\n2025-12-31,7\n"
    (tmp_path / "data/market.csv").write_bytes(market_data)
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] != "backtest":
            return real_run(command, **kwargs)
        cwd = kwargs["cwd"]
        assert command[0] == "backtest"
        assert (cwd / "data/market.csv").read_bytes() == market_data
        run_id = command[-1]
        metrics_path = cwd / "outputs/backtests" / run_id / "metrics.json"
        metrics_path.parent.mkdir(parents=True)
        metrics_path.write_text(
            json.dumps({"sortino": 1.75, "max_drawdown": -0.08}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="test complete")

    monkeypatch.setattr(champion_test.subprocess, "run", fake_run)

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    result = json.loads(
        (state_path.parent / "test/result.json").read_text(encoding="utf-8")
    )
    assert state["test_status"] == "completed"
    assert result["run"] == "001"
    assert result["strategy_sha256"]
    assert result["test_period"] == {
        "start": "2025-01-01",
        "end": "2025-12-31",
    }
    assert result["metrics"]["sortino"] == 1.75
    assert result["runtime_inputs"] == {
        "data/market.csv": hashlib.sha256(market_data).hexdigest(),
    }
    assert not (state_path.parent / "test/test.log").exists()


def test_automatic_test_uses_champion_frozen_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(tmp_path, max_rounds=1, include_test=True)
    original_sha256 = hashlib.sha256(b"1.0\n").hexdigest()
    real_run = subprocess.run

    def mutate_task_champion(
        task_path: Path,
        manager: ResearchWorkspace,
        state: dict[str, object],
    ) -> Path:
        changed = b"2.0\n"
        manager.champion_path.write_bytes(changed)
        champion_state = json.loads(manager.state_path.read_text(encoding="utf-8"))
        champion_state["champion_sha256"] = hashlib.sha256(changed).hexdigest()
        champion_state["champion_round_id"] = "002/001"
        manager.state_path.write_text(json.dumps(champion_state), encoding="utf-8")
        return _reporter(task_path, manager, state)

    def fake_run(command, **kwargs):
        if command[0] != "backtest":
            return real_run(command, **kwargs)
        evaluator = kwargs["cwd"]
        assert (evaluator / "strategy.py").read_bytes() == b"1.0\n"
        metrics_path = (
            evaluator / "outputs/backtests/test-run-001/metrics.json"
        )
        metrics_path.parent.mkdir(parents=True)
        metrics_path.write_text(json.dumps({"sortino": 1.0}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="tested frozen Champion")

    monkeypatch.setattr(champion_test.subprocess, "run", fake_run)
    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=mutate_task_champion,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    result = json.loads(
        (state_path.parent / "test/result.json").read_text(encoding="utf-8")
    )
    assert state["final_champion_sha256"] == original_sha256
    assert result["strategy_sha256"] == original_sha256
    assert result["champion_round_id"] is None


def test_automatic_test_timeout_is_bounded_and_keeps_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(tmp_path, max_rounds=1, include_test=True)
    task.write_text(
        task.read_text(encoding="utf-8").replace(
            "round_minutes = 60",
            "round_minutes = 1",
        ),
        encoding="utf-8",
    )
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] != "backtest":
            return real_run(command, **kwargs)
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(command, 60, output="partial test output")

    monkeypatch.setattr(champion_test.subprocess, "run", fake_run)
    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_rounds"
    assert state["test_status"] == "failed"
    assert state["test_error"] == "Champion Test evaluation timed out after 60 seconds"
    log = (state_path.parent / "test/test.log").read_text(encoding="utf-8")
    assert "partial test output" in log
    assert "timed out after 60 seconds" in log


def test_parent_preflight_failure_does_not_allocate_run_or_call_agent(
    tmp_path: Path,
) -> None:
    task_path = _task(tmp_path)
    agent_called = False

    def agent(*args, **kwargs):
        nonlocal agent_called
        agent_called = True
        raise AssertionError("agent must not run")

    def failing_preflight(task, manager, environment, **kwargs):
        def fail(command, cwd, log_path, timeout):
            log_path.write_text("parent baseline failed", encoding="utf-8")
            return 1

        return run_parent_fixed_tests(
            task,
            manager,
            environment,
            command_runner=fail,
            evidence_root=kwargs["evidence_root"],
        )

    with pytest.raises(ParentFixedTestsError) as raised:
        run_loop(
            task_path,
            workspace=tmp_path,
            managed_runner=agent,
            reporter=_reporter,
            parent_test_preflight=failing_preflight,
            environment_probe=lambda: ENVIRONMENT,
        )

    assert raised.value.failure_code == "parent_fixed_tests_failed"
    assert not agent_called
    assert not (tmp_path / ".research/loop-test/runs").exists()
    evidence = json.loads(
        (raised.value.evidence_path / "failure.json").read_text(encoding="utf-8")
    )
    assert evidence["failure_kind"] == "infrastructure"
    assert (raised.value.evidence_path / "parent-tests.log").read_text(
        encoding="utf-8"
    ) == "parent baseline failed"


def test_parent_preflight_success_is_reused_before_run_allocation(
    tmp_path: Path,
) -> None:
    task_path = _task(tmp_path, max_rounds=1)
    test_calls = 0

    def cached_preflight(task, manager, environment, **kwargs):
        def pass_test(command, cwd, log_path, timeout):
            nonlocal test_calls
            test_calls += 1
            log_path.write_text("", encoding="utf-8")
            return 0

        return run_parent_fixed_tests(
            task,
            manager,
            environment,
            command_runner=pass_test,
            evidence_root=kwargs["evidence_root"],
        )

    state_path = run_loop(
        task_path,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
        parent_test_preflight=cached_preflight,
        environment_probe=lambda: ENVIRONMENT,
    )

    assert json.loads(state_path.read_text(encoding="utf-8"))[
        "rounds_completed"
    ] == 1
    assert test_calls == 1
    champion = json.loads(
        (tmp_path / ".research/loop-test/champion.json").read_text(encoding="utf-8")
    )
    assert champion["champion_fixed_test_record"]["status"] == "passed"


def test_parent_preflight_failure_before_next_round_does_not_change_counters(
    tmp_path: Path,
) -> None:
    task_path = _task(tmp_path, max_rounds=3)
    checks = 0

    def becomes_stale(task, manager, environment, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 1:
            return None
        evidence = kwargs["evidence_root"] / "stale-parent"
        evidence.mkdir(parents=True)
        (evidence / "parent-tests.log").write_text(
            "stale parent failed",
            encoding="utf-8",
        )
        raise ParentFixedTestsError(
            "Parent fixed tests failed",
            "parent_fixed_tests_failed",
            evidence,
        )

    state_path = run_loop(
        task_path,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
        parent_test_preflight=becomes_stale,
        environment_probe=lambda: ENVIRONMENT,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["stop_reason"] == "infrastructure_failure"
    assert state["failure_code"] == "parent_fixed_tests_failed"
    assert state["rounds_completed"] == 1
    assert state["rejected"] == 1
    assert state["failed"] == 0
    assert state["consecutive_failures"] == 0
    assert state["round_ids"] == ["001"]
    assert not (state_path.parent / "rounds/002").exists()


def test_loop_retain_diagnostics_preserves_event_timeline_and_summary(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=1)

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected"]),
        reporter=_reporter,
        retain_diagnostics=True,
    )

    diagnostics = tmp_path / ".research/loop-test/runs/001/artifacts/diagnostics"
    events = [json.loads(line) for line in (diagnostics / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = json.loads((diagnostics / "diagnostic-summary.json").read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["diagnostics_enabled"] is True
    assert not (tmp_path / ".research/loop-test/.tmp/runs/001/events.jsonl").exists()
    assert [event["event"] for event in events][-3:] == [
        "report_completed",
        "test_skipped",
        "run_completed",
    ]
    assert summary["event_count"] == len(events)
    assert summary["rounds"] == [{
        "round": "001",
        "status": "completed",
        "decision": "rejected",
        "duration_seconds": None,
        "development_attempts": 0,
        "failure_kind": None,
        "failure_code": None,
    }]


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


def test_candidate_bind_preflight_failure_does_not_allocate_a_round(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = _task(tmp_path)

    def fail_bind(candidate: Path, evidence_path: Path, **kwargs) -> Path:
        evidence_path.mkdir(parents=True)
        summary = evidence_path / "summary.json"
        summary.write_text(
            json.dumps({
                "status": "failed",
                "failure_code": "candidate_bind_unavailable",
            }),
            encoding="utf-8",
        )
        raise CandidateBindPreflightError(
            "Docker could not bind the candidate workspace",
            "candidate_bind_unavailable",
            summary,
        )

    monkeypatch.setattr(research_loop, "probe_candidate_bind_source", fail_bind)
    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=run_managed_once,
        reporter=_reporter,
        container_preflight=lambda task, root: None,
        provider_preflight=lambda task, root: None,
        parent_test_preflight=lambda task, manager, environment: None,
        environment_probe=lambda: ENVIRONMENT,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "infrastructure_failure"
    assert state["rounds_completed"] == 0
    assert state["round_ids"] == []
    assert state["current_round"] is None
    assert state["preflight_failure"]["failure_code"] == "candidate_bind_unavailable"
    diagnostic = state_path.parent / state["preflight_failure"]["evidence_path"]
    assert (diagnostic / "summary.json").is_file()
    assert not (state_path.parent / "rounds/001").exists()


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
            manager.metrics_applicability(
                task_state,
                _metrics_key(loaded_task),
                loaded_task.evaluator_contract_paths,
            ),
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
        manager.metrics_applicability(
            state,
            _metrics_key(task),
            task.evaluator_contract_paths,
        ),
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
        manager.metrics_applicability(
            state,
            _metrics_key(task),
            task.evaluator_contract_paths,
        ),
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


def test_legacy_active_run_without_development_contract_fails_closed(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, max_failures=1)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        run_number=1,
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    manager.rounds.mkdir(parents=True)
    legacy = {
        **_running_state(task),
        "schema_version": 3,
    }
    legacy.pop("development_view_sha256")
    legacy.pop("development_end")
    manager.development_inputs_path.unlink()
    manager.loop_state_path.write_text(json.dumps(legacy), encoding="utf-8")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "infrastructure_failure"
    assert state["failure_code"] == "development_inputs_incompatible"
    assert state["rounds_completed"] == 0


@pytest.mark.parametrize(
    "frozen_content",
    [
        "{",
        json.dumps({"schema_version": 1, "development_end": "2021-12-31"}),
    ],
)
def test_invalid_frozen_development_manifest_stops_active_run(
    tmp_path: Path,
    frozen_content: str,
) -> None:
    task = _task(tmp_path, max_failures=1)
    state = _running_state(task)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "loop-test",
        run_number=1,
        evaluation_environment_sha256=ENVIRONMENT.sha256,
    )
    manager.rounds.mkdir(parents=True, exist_ok=True)
    manager.loop_state_path.write_text(json.dumps(state), encoding="utf-8")
    manager.development_inputs_path.write_text(frozen_content, encoding="utf-8")

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
    )

    stopped = json.loads(state_path.read_text(encoding="utf-8"))
    assert stopped["status"] == "stopped"
    assert stopped["stop_reason"] == "infrastructure_failure"
    assert stopped["failure_code"] == "development_inputs_incompatible"
    assert stopped["report_status"] == "completed"
    assert stopped["rounds_completed"] == 0


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
    candidates = (
        tmp_path / ".research/loop-test/.tmp/worktrees/001/candidates"
    )
    assert not (candidates / "001").exists()
    assert candidates.is_dir()
    parent_inode = candidates.stat().st_ino

    resumed_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner([]),
        reporter=_reporter,
    )

    resumed = json.loads(resumed_path.read_text(encoding="utf-8"))
    assert resumed["stop_reason"] == "max_consecutive_failures"
    assert resumed["rounds_completed"] == 1
    assert parent_inode > 0


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
        experiment = (
            research_root / "loop-test" / "runs" / f"{run_number:03d}"
            / "rounds" / experiment_id
        )
        experiment.mkdir(parents=True)
        (experiment / "tests.log").write_text("runner detail\n", encoding="utf-8")
        raise RuntimeError("runner bug")

    with pytest.raises(RuntimeError, match="runner bug"):
        run_loop(
            task,
            workspace=tmp_path,
            managed_runner=fail,
            reporter=_reporter,
            monotonic=lambda: next(times),
            retain_diagnostics=True,
        )

    state = json.loads(
        (tmp_path / ".research/loop-test/runs/001/artifacts/state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "interrupted"
    assert state["stop_reason"] == "runner_error"
    assert state["elapsed_seconds"] == 120.0
    diagnostics = tmp_path / ".research/loop-test/runs/001/artifacts/diagnostics"
    summary = json.loads(
        (diagnostics / "diagnostic-summary.json").read_text(encoding="utf-8")
    )
    assert summary["rounds"] == [{
        "round": "001",
        "status": None,
        "decision": None,
        "duration_seconds": None,
        "development_attempts": 0,
        "failure_kind": None,
        "failure_code": None,
    }]
    assert summary["artifacts"][0]["source"] == "rounds/001/tests.log"


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
    assert state["report_path"] == "report.md"
    minimal = (state_path.parent.parent / "report.md").read_text(encoding="utf-8")
    assert "报告 Agent 未能生成完整复盘" in minimal
    assert "Test 状态：not_configured" in minimal


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
    assert state_path == tmp_path / ".research/loop-test/runs/002/artifacts/state.json"
    assert (tmp_path / ".research/loop-test/runs/001/report.md").exists()
    assert (tmp_path / ".research/loop-test/runs/002/report.md").exists()
