from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from quant_core.research import run_loop
from quant_core.research.workspace import ResearchWorkspace


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
        "schema_version": 2,
        "task_id": "loop-test",
        "run_number": 1,
        "task_fingerprint": hashlib.sha256(task.read_bytes()).hexdigest(),
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
        manager = ResearchWorkspace(workspace, research_root, "loop-test", run_number)
        task_state = manager.load_state()
        metrics = {"gate": {"sortino": 1.5, "max_drawdown": -0.10}}
        manager.record_state(task_state, experiment_id, metrics)
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


def test_loop_recovers_a_completed_unrecorded_round(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=1)
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "loop-test")
    task_state = base.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    manager = base.for_run(1)
    manager.rounds.mkdir(parents=True)
    manager.record_state(task_state, "001/001")
    experiment = manager.rounds / "001"
    experiment.mkdir()
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
        manager = ResearchWorkspace(workspace, research_root, "loop-test", run_number)
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
