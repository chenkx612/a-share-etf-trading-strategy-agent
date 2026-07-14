from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

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
model = "deepseek/deepseek-chat"
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
max_drawdown = 0.20

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
    return path


def _runner(decisions: list[str]):
    remaining = iter(decisions)

    def run(task_path: Path, experiment_id: str, *, workspace: Path, research_root: Path) -> Path:
        decision = next(remaining)
        experiment = research_root / "loop-test" / "experiments" / experiment_id
        experiment.mkdir()
        result_path = experiment / "result.json"
        result_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        (experiment / "decision.json").write_text(json.dumps({
            "experiment_id": experiment_id,
            "decision": decision,
        }), encoding="utf-8")
        return result_path

    return run


def _running_state(task: Path, experiment_id: str = "loop-000001") -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": "loop-test",
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
        "current_experiment_id": experiment_id,
        "last_experiment_id": None,
        "stop_reason": None,
    }


def test_loop_stops_after_consecutive_failed_rounds(tmp_path: Path) -> None:
    task = _task(tmp_path, max_failures=2)

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["rejected", "failed", "failed"]),
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_consecutive_failures"
    assert state["rounds_completed"] == 3
    assert state["rejected"] == 1
    assert state["failed"] == 2


def test_rejected_round_breaks_a_failure_streak(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=3, max_failures=2)

    state_path = run_loop(
        task,
        workspace=tmp_path,
        managed_runner=_runner(["failed", "rejected", "failed"]),
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
        monotonic=lambda: next(times),
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_hours"
    assert state["rounds_completed"] == 1


def test_loop_stops_when_champion_reaches_target(tmp_path: Path) -> None:
    task = _task(tmp_path, target=1.5)

    def reach_target(task_path: Path, experiment_id: str, *, workspace: Path, research_root: Path) -> Path:
        manager = ResearchWorkspace(workspace, research_root, "loop-test")
        task_state = manager.load_state()
        metrics = {"gate": {"sortino": 1.5, "max_drawdown": -0.10}}
        manager.record_state(task_state, experiment_id, metrics)
        experiment = manager.experiments / experiment_id
        experiment.mkdir()
        result_path = experiment / "result.json"
        result_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        (experiment / "decision.json").write_text(json.dumps({
            "experiment_id": experiment_id,
            "decision": "accepted",
        }), encoding="utf-8")
        return result_path

    state_path = run_loop(task, workspace=tmp_path, managed_runner=reach_target)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "target_reached"
    assert state["rounds_completed"] == 1


def test_loop_recovers_a_completed_unrecorded_round(tmp_path: Path) -> None:
    task = _task(tmp_path, max_rounds=1)
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "loop-test")
    task_state = manager.initialize(date(2021, 12, 31))
    manager.record_state(task_state, "loop-000001")
    experiment = manager.experiments / "loop-000001"
    experiment.mkdir()
    (experiment / "decision.json").write_text(json.dumps({
        "experiment_id": "loop-000001",
        "decision": "accepted",
    }), encoding="utf-8")
    loop_state = manager.root / "loop-state.json"
    loop_state.write_text(json.dumps(_running_state(task)), encoding="utf-8")

    state_path = run_loop(task, workspace=tmp_path, managed_runner=_runner([]))

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_rounds"
    assert state["accepted"] == 1
    assert state["last_experiment_id"] == "loop-000001"


def test_loop_marks_an_incomplete_round_as_interrupted_failure(tmp_path: Path) -> None:
    task = _task(tmp_path, max_failures=1)
    experiment = tmp_path / ".research/loop-test/experiments/loop-000001"
    experiment.mkdir(parents=True)
    loop_state = tmp_path / ".research/loop-test/loop-state.json"
    loop_state.write_text(json.dumps(_running_state(task)), encoding="utf-8")

    state_path = run_loop(task, workspace=tmp_path, managed_runner=_runner([]))

    state = json.loads(state_path.read_text(encoding="utf-8"))
    decision = json.loads((experiment / "decision.json").read_text(encoding="utf-8"))
    result = json.loads((experiment / "result.json").read_text(encoding="utf-8"))
    assert state["stop_reason"] == "max_consecutive_failures"
    assert state["failed"] == 1
    assert decision["decision"] == "failed"
    assert result["status"] == "failed"


def test_loop_does_not_trust_a_decision_that_was_not_applied(tmp_path: Path) -> None:
    task = _task(tmp_path, max_failures=1)
    experiment = tmp_path / ".research/loop-test/experiments/loop-000001"
    experiment.mkdir(parents=True)
    (experiment / "decision.json").write_text(json.dumps({
        "experiment_id": "loop-000001",
        "decision": "accepted",
    }), encoding="utf-8")
    loop_state = tmp_path / ".research/loop-test/loop-state.json"
    loop_state.write_text(json.dumps(_running_state(task)), encoding="utf-8")

    state_path = run_loop(task, workspace=tmp_path, managed_runner=_runner([]))

    state = json.loads(state_path.read_text(encoding="utf-8"))
    decision = json.loads((experiment / "decision.json").read_text(encoding="utf-8"))
    assert state["failed"] == 1
    assert decision["decision"] == "failed"


def test_interrupted_loop_is_resumed_on_the_next_invocation(tmp_path: Path) -> None:
    task = _task(tmp_path, max_failures=1)

    def interrupt(task_path: Path, experiment_id: str, *, workspace: Path, research_root: Path) -> Path:
        experiment = research_root / "loop-test" / "experiments" / experiment_id
        experiment.mkdir()
        raise KeyboardInterrupt

    interrupted_path = run_loop(task, workspace=tmp_path, managed_runner=interrupt)
    interrupted = json.loads(interrupted_path.read_text(encoding="utf-8"))
    assert interrupted["status"] == "interrupted"

    resumed_path = run_loop(task, workspace=tmp_path, managed_runner=_runner([]))

    resumed = json.loads(resumed_path.read_text(encoding="utf-8"))
    assert resumed["stop_reason"] == "max_consecutive_failures"
    assert resumed["rounds_completed"] == 1
