from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Sequence

import pandas as pd
import pytest

from quant_core.research.checkpoint import RUNTIME_DIR, submit
from quant_core.research.runner import _run_opencode_container, run_managed_once
from quant_core.research.workspace import ResearchWorkspace


TASK = """
id = "{task_id}"
goal = "Improve the strategy"

[budget]
max_rounds = 3
max_hours = 4
max_consecutive_failures = 2

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

[evaluation.acceptance]
minimum_improvement = 0.05

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


def _git_text(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _task(root: Path, task_id: str = "managed-test") -> Path:
    path = root / f"{task_id}.toml"
    path.write_text(TASK.format(task_id=task_id), encoding="utf-8")
    _init_repo(root)
    return path


def _command(command: Sequence[str], cwd: Path, log_path: Path, timeout: int) -> int:
    if command[0] != "backtest":
        return 0
    signal = float((cwd / "strategy.py").read_text(encoding="utf-8").strip())
    if command[command.index("--start") + 1] == "2022-01-01" and (cwd / "data/etf_daily.csv").exists():
        daily = pd.read_csv(cwd / "data/etf_daily.csv")
        assert daily["date"].max() == "2024-01-02"
    run_id = command[command.index("--run-id") + 1]
    metrics_path = cwd / "outputs" / "backtests" / run_id / "metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps({"sortino": signal, "max_drawdown": -0.10}), encoding="utf-8")
    return 0


def _opencode_with_signal(
    signal: float,
    expected_history: str | None = None,
    previous_feedback: str = "",
):
    def run(command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int) -> int:
        assert not (cwd / "managed-test.toml").exists()
        if expected_history is not None:
            assert expected_history in prompt
        if (cwd / "data/etf_daily.csv").exists():
            daily = pd.read_csv(cwd / "data/etf_daily.csv")
            assert daily["date"].max() == "2021-12-31"
        log_path.write_text(json.dumps({"type": "text", "part": {"text": json.dumps({
            "status": "completed",
            "previous_feedback": previous_feedback,
            "hypothesis": "A higher signal improves Sortino",
            "attempts": f"Tested signal value {signal}.",
            "development_effect": f"Development Sortino was {signal}.",
            "candidate": f"Set signal to {signal}",
        })}}) + "\n", encoding="utf-8")
        (cwd / "strategy.py").write_text(f"{signal}\n", encoding="utf-8")
        return 0

    return run


def test_workspaces_are_isolated_by_task_id(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/etf_daily.csv").write_text(
        "date,value\n2021-12-31,1\n2024-01-02,2\n",
        encoding="utf-8",
    )
    pd.DataFrame({
        "date": pd.to_datetime(["2021-12-31", "2024-01-02"]),
        "value": [1, 2],
    }).to_parquet(tmp_path / "data/etf_daily.parquet", index=False)
    (tmp_path / "outputs/factors").mkdir(parents=True)
    (tmp_path / "outputs/factors/factors.csv").write_text("date,value\n", encoding="utf-8")
    (tmp_path / "outputs/backtests/old-run").mkdir(parents=True)
    (tmp_path / "outputs/backtests/old-run/metrics.json").write_text("{}", encoding="utf-8")
    _init_repo(tmp_path)
    first = ResearchWorkspace(tmp_path, tmp_path / ".research", "multi-factor")
    second = ResearchWorkspace(tmp_path, tmp_path / ".research", "macd-timing")

    first_state = first.initialize(date(2021, 12, 31), strategy_path="strategy.py")
    second_state = second.initialize(strategy_path="strategy.py")

    assert first.state_path != second.state_path
    assert isinstance(first_state["champion_sha256"], str)
    assert isinstance(second_state["champion_sha256"], str)
    assert first.champion_path.read_text(encoding="utf-8") == "1.0\n"
    assert second.champion_path.read_text(encoding="utf-8") == "1.0\n"
    assert (first.evaluation_runtime / "outputs/factors/factors.csv").exists()

    first_run = first.for_run(1)
    candidate, _experiment, _state = first_run.create_candidate(
        "001",
        date(2021, 12, 31),
        strategy_path="strategy.py",
    )
    assert (candidate / "outputs/factors/factors.csv").exists()
    assert not (candidate / "outputs/backtests").exists()
    assert pd.read_csv(candidate / "data/etf_daily.csv")["date"].tolist() == ["2021-12-31"]
    assert pd.read_parquet(candidate / "data/etf_daily.parquet")["date"].dt.date.tolist() == [date(2021, 12, 31)]
    assert pd.read_csv(first.evaluation_runtime / "data/etf_daily.csv")["date"].tolist() == [
        "2021-12-31",
        "2024-01-02",
    ]
    assert (candidate / ".git").is_file()
    first_run.reject(candidate, first_state, "001/001")


def test_same_task_in_different_research_roots_uses_distinct_champion_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)

    first = ResearchWorkspace(tmp_path, tmp_path / ".research", "same-task")
    second = ResearchWorkspace(tmp_path, tmp_path / ".research-alt", "same-task")
    first_state = first.initialize(strategy_path="strategy.py")
    second_state = second.initialize(strategy_path="strategy.py")

    assert first.champion_path != second.champion_path
    assert first_state["champion_sha256"] == second_state["champion_sha256"]
    assert first.state_path.name == "champion.json"
    assert second.state_path.name == "champion.json"


def test_legacy_task_and_loop_layout_migrates_to_numbered_run(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "legacy-task")
    head = _git_text(tmp_path, "rev-parse", "HEAD")
    legacy_state = {
        "schema_version": 3,
        "task_id": "legacy-task",
        "baseline_mode": "workspace",
        "baseline_exclude": [],
        "seed_commit": head,
        "champion_commit": head,
        "champion_number": 0,
        "champion_metrics": None,
        "champion_metrics_key": None,
        "pending_promotion": None,
    }
    manager.root.mkdir(parents=True)
    manager.legacy_state_path.write_text(json.dumps(legacy_state), encoding="utf-8")
    legacy_experiment = manager.root / "experiments/loop-000001"
    legacy_experiment.mkdir(parents=True)
    (legacy_experiment / "result.json").write_text(
        json.dumps({"experiment_id": "loop-000001", "status": "failed", "error": "old"}),
        encoding="utf-8",
    )
    (manager.root / "loop-state.json").write_text(
        json.dumps({
            "schema_version": 1,
            "task_id": "legacy-task",
            "status": "stopped",
            "rounds_completed": 1,
            "experiment_ids": ["loop-000001"],
            "current_experiment_id": None,
            "last_experiment_id": "loop-000001",
        }),
        encoding="utf-8",
    )

    migrated_state = manager.initialize(strategy_path="strategy.py")
    run_number = manager.migrate_legacy_loop()

    assert migrated_state["schema_version"] == 4
    assert manager.champion_path.read_text(encoding="utf-8") == "1.0\n"
    assert "champion_commit" not in migrated_state
    assert not manager.legacy_state_path.exists()
    assert run_number == 1
    assert (manager.root / "runs/001/rounds/001/result.json").exists()
    run_state = json.loads((manager.root / "runs/001/state.json").read_text(encoding="utf-8"))
    assert run_state["schema_version"] == 2
    assert run_state["round_ids"] == ["001"]


def test_temporary_worktree_captures_dirty_strategy_without_changing_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    (tmp_path / "strategy.py").write_text("1.5\n", encoding="utf-8")
    head_before = _git_text(tmp_path, "rev-parse", "HEAD")
    status_before = _git_text(tmp_path, "status", "--short")
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "dirty-seed")
    manager = base.for_run(1)

    candidate, _experiment, state = manager.create_candidate(
        "001",
        strategy_path="strategy.py",
    )

    assert (candidate / "strategy.py").read_text(encoding="utf-8") == "1.5\n"
    assert manager.champion_path.read_text(encoding="utf-8") == "1.5\n"
    assert state["champion_sha256"] == hashlib.sha256(b"1.5\n").hexdigest()
    assert _git_text(tmp_path, "rev-parse", "HEAD") == head_before
    assert _git_text(tmp_path, "status", "--short") == status_before
    manager.reject(candidate, state, "001/001")


def test_candidate_parent_remains_stable_between_rounds(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "stable-parent")
    manager = base.for_run(1)

    first, _experiment, state = manager.create_candidate(
        "001",
        strategy_path="strategy.py",
    )
    parent_inode = manager.candidates.stat().st_ino
    manager.reject(first, state, "001/001")

    assert manager.candidates.is_dir()
    second, _experiment, state = manager.create_candidate(
        "002",
        strategy_path="strategy.py",
    )

    assert manager.candidates.stat().st_ino == parent_inode
    assert second.is_dir()
    manager.reject(second, state, "001/002")


@pytest.mark.skipif(
    os.environ.get("QUANT_TEST_AGENT_CONTAINER") != "1",
    reason="set QUANT_TEST_AGENT_CONTAINER=1 after building the research Agent image",
)
def test_consecutive_candidate_worktrees_mount_in_real_agent_container(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "docker-stable-parent")
    manager = base.for_run(1)
    parent_inode: int | None = None

    for round_id in ("001", "002", "003", "004", "005"):
        candidate, _experiment, state = manager.create_candidate(
            round_id,
            strategy_path="strategy.py",
        )
        try:
            current_inode = manager.candidates.stat().st_ino
            if parent_inode is None:
                parent_inode = current_inode
            assert current_inode == parent_inode
            marker = candidate / f"mounted-{round_id}"
            exit_code = _run_opencode_container(
                [
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path('/workspace/{marker.name}').write_text('ok', encoding='utf-8')"
                    ),
                ],
                "",
                candidate,
                tmp_path / f"container-{round_id}.log",
                60,
            )
            assert exit_code == 0
            assert marker.read_text(encoding="utf-8") == "ok"
        except BaseException:
            manager.reject(candidate, state, f"001/{round_id}")
            raise
        if round_id == "001":
            manager.promote(
                candidate,
                state,
                "001/001",
                {"development": {"sortino": 1.0}, "gate": {"sortino": 1.0}},
                ["strategy.py"],
            )
        else:
            manager.reject(candidate, state, f"001/{round_id}")


def test_managed_run_promotes_only_an_improved_candidate(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/etf_daily.csv").write_text(
        "date,value\n2021-12-31,1\n2024-01-02,2\n",
        encoding="utf-8",
    )
    task = _task(tmp_path)

    first_result = run_managed_once(
        task,
        "001",
        run_number=1,
        workspace=tmp_path,
        command_runner=_command,
        opencode_runner=_opencode_with_signal(0.9),
    )
    first_decision = json.loads((first_result.parent / "decision.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / ".research/managed-test/champion.json").read_text(encoding="utf-8"))
    assert first_decision["decision"] == "rejected"
    assert first_decision["submission"]["mode"] == "final"
    assert first_decision["candidate_patch_sha256"] == hashlib.sha256(
        (first_result.parent / "candidate.patch").read_bytes()
    ).hexdigest()
    baseline_sha256 = hashlib.sha256(b"1.0\n").hexdigest()
    assert state["champion_sha256"] == baseline_sha256
    assert (tmp_path / ".research/managed-test/champion.py").read_text() == "1.0\n"
    assert not (
        tmp_path
        / ".research/managed-test/.tmp/worktrees/001/candidates/001"
    ).exists()

    second_result = run_managed_once(
        task,
        "001",
        run_number=2,
        workspace=tmp_path,
        command_runner=_command,
        opencode_runner=_opencode_with_signal(
            1.2,
            '"decision":"rejected"',
            "The lower signal implementation did not improve the objective.",
        ),
    )
    second_decision = json.loads((second_result.parent / "decision.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / ".research/managed-test/champion.json").read_text(encoding="utf-8"))
    assert second_decision["decision"] == "accepted"
    assert second_decision["submission"]["submitted_by_timeout"] is False
    assert state["champion_sha256"] == hashlib.sha256(b"1.2\n").hexdigest()
    assert state["champion_round_id"] == "002/001"
    assert (tmp_path / ".research/managed-test/champion.py").read_text() == "1.2\n"
    assert "-1.0" in (second_result.parent / "candidate.patch").read_text(encoding="utf-8")
    assert "+1.2" in (second_result.parent / "candidate.patch").read_text(encoding="utf-8")
    first_record = json.loads(first_result.read_text(encoding="utf-8"))
    second_record = json.loads(second_result.read_text(encoding="utf-8"))
    assert first_record["candidate"] == "Set signal to 0.9"
    assert first_record["attempts"] == "Tested signal value 0.9."
    assert first_record["feedback"].startswith("The lower signal")
    assert "feedback" not in second_record
    assert not (tmp_path / ".research/managed-test/research-memory.json").exists()
    assert _git_text(tmp_path, "for-each-ref", "refs/quant-research") == ""


def test_managed_run_can_promote_checkpoint_restored_after_timeout(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    task = _task(tmp_path)
    task.write_text(
        task.read_text(encoding="utf-8").replace(
            "max_hours = 4", "max_hours = 4\nround_minutes = 7",
        ),
        encoding="utf-8",
    )
    current_time = [0.0]
    events: list[str] = []

    def checkpointing_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        (cwd / "strategy.py").write_text("1.2\n", encoding="utf-8")
        metadata = cwd / RUNTIME_DIR / "metadata.json"
        metadata.write_text(json.dumps({
            "previous_feedback": "",
            "hypothesis": "A higher signal improves Sortino",
            "attempts": "Tested signal value 1.2.",
            "development_effect": "Development Sortino increased to 1.2.",
            "candidate": "Set signal to 1.2",
        }), encoding="utf-8")
        assert submit(metadata, workspace=cwd)["checkpoint_id"] == "001"
        (cwd / "strategy.py").write_text("9.9\n", encoding="utf-8")
        current_time[0] = 421.0
        return 124

    result_path = run_managed_once(
        task,
        "001",
        run_number=1,
        workspace=tmp_path,
        command_runner=_command,
        opencode_runner=checkpointing_opencode,
        event_sink=lambda event, **details: events.append(event),
        monotonic=lambda: current_time[0],
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    decision = json.loads((result_path.parent / "decision.json").read_text(encoding="utf-8"))
    patch = result_path.parent / "candidate.patch"
    assert result["submission"]["checkpoint_id"] == "001"
    assert decision["decision"] == "accepted"
    assert decision["submission"] == result["submission"]
    assert decision["candidate_patch_sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert "+1.2" in patch.read_text(encoding="utf-8")
    assert (tmp_path / ".research/managed-test/champion.py").read_text(encoding="utf-8") == "1.2\n"
    assert "checkpoint_restored" in events


def test_no_baseline_rejects_until_first_candidate_passes_constraints(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    task = _task(tmp_path)
    task.write_text(
        task.read_text(encoding="utf-8")
        .replace(
            '[data]\nuniverse = "universe.csv"',
            '[baseline]\nmode = "none"\nexclude = ["strategy.py"]\n\n'
            '[data]\nuniverse = "universe.csv"',
        )
        .replace('threshold = 0.20', 'threshold = 0.05'),
        encoding="utf-8",
    )

    def constraint_command(
        command: Sequence[str], cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        if command[0] != "backtest":
            return 0
        signal = float((cwd / "strategy.py").read_text(encoding="utf-8").strip())
        run_id = command[command.index("--run-id") + 1]
        metrics_path = cwd / "outputs/backtests" / run_id / "metrics.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps({
            "sortino": signal,
            "max_drawdown": -0.10 if signal < 1.0 else -0.04,
        }), encoding="utf-8")
        return 0

    rejected_result = run_managed_once(
        task,
        "001",
        run_number=1,
        workspace=tmp_path,
        command_runner=constraint_command,
        opencode_runner=_opencode_with_signal(0.9),
    )
    rejected_decision = json.loads(
        (rejected_result.parent / "decision.json").read_text(encoding="utf-8")
    )
    state = json.loads((tmp_path / ".research/managed-test/champion.json").read_text(encoding="utf-8"))
    assert rejected_decision["decision"] == "rejected"
    assert state["champion_sha256"] is None
    assert not (tmp_path / ".research/managed-test/champion.py").exists()
    assert not (rejected_result.parent / "champion-development.log").exists()

    accepted_result = run_managed_once(
        task,
        "001",
        run_number=2,
        workspace=tmp_path,
        command_runner=constraint_command,
        opencode_runner=_opencode_with_signal(1.2, '"decision":"rejected"'),
    )
    accepted_decision = json.loads(
        (accepted_result.parent / "decision.json").read_text(encoding="utf-8")
    )
    state = json.loads((tmp_path / ".research/managed-test/champion.json").read_text(encoding="utf-8"))
    assert accepted_decision["decision"] == "accepted"
    assert isinstance(state["champion_sha256"], str)
    assert (tmp_path / ".research/managed-test/champion.py").read_text() == "1.2\n"
    assert not (accepted_result.parent / "champion-development.log").exists()


def test_failed_candidate_does_not_change_champion(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    task = _task(tmp_path)

    def failed_opencode(command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int) -> int:
        (cwd / "strategy.py").write_text("99.0\n", encoding="utf-8")
        return 1

    result = run_managed_once(
        task,
        "001",
        run_number=1,
        workspace=tmp_path,
        command_runner=_command,
        opencode_runner=failed_opencode,
    )
    decision = json.loads((result.parent / "decision.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / ".research/managed-test/champion.json").read_text(encoding="utf-8"))
    assert decision["decision"] == "failed"
    assert state["champion_sha256"] == hashlib.sha256(b"1.0\n").hexdigest()
    assert (tmp_path / ".research/managed-test/champion.py").read_text() == "1.0\n"
    assert not (
        tmp_path
        / ".research/managed-test/.tmp/worktrees/001/candidates/001"
    ).exists()


def test_new_round_cleans_candidate_left_by_interrupted_run(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "recovery-test")
    base.initialize(strategy_path="strategy.py")
    manager = base.for_run(1)
    stale = manager.candidates / "interrupted"
    stale.mkdir(parents=True)
    (stale / "strategy.py").write_text("99.0\n", encoding="utf-8")

    candidate, _experiment, _state = manager.create_candidate(
        "001",
        strategy_path="strategy.py",
    )

    assert not stale.exists()
    assert (candidate / "strategy.py").read_text(encoding="utf-8") == "1.0\n"
    manager.reject(candidate, _state, "001/001")


def test_compact_artifacts_removes_success_diagnostics_but_keeps_failure_logs(
    tmp_path: Path,
) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "compact-test")
    base.initialize(strategy_path="strategy.py")
    manager = base.for_run(1)
    completed = manager.rounds / "001"
    completed.mkdir(parents=True)
    (completed / "result.json").write_text(
        json.dumps({"experiment_id": "001/001", "status": "completed"}),
        encoding="utf-8",
    )
    for name in ("agent-output.json", "opencode-events.jsonl", "tests.log", "gate.log"):
        (completed / name).write_text("diagnostic\n", encoding="utf-8")

    failed = manager.rounds / "002"
    failed.mkdir()
    (failed / "result.json").write_text(
        json.dumps({"experiment_id": "001/002", "status": "failed", "error": "boom"}),
        encoding="utf-8",
    )
    (failed / "opencode-events.jsonl").write_text("failure detail\n", encoding="utf-8")
    (failed / "tests.log").write_text("failure detail\n", encoding="utf-8")

    summary = manager.compact_artifacts()

    assert summary["removed_files"] == 4
    assert sorted(path.name for path in completed.iterdir()) == ["result.json"]
    assert (failed / "opencode-events.jsonl").exists()
    assert (failed / "tests.log").exists()


def test_round_id_cannot_escape_task_workspace(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "safe-task").for_run(1)

    with pytest.raises(ValueError, match="round id"):
        manager.create_candidate("../outside", strategy_path="strategy.py")


def test_recovery_finishes_a_file_promotion_after_state_write_was_interrupted(
    tmp_path: Path,
) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "recovery-commit",
    ).for_run(1)
    state = manager.initialize(strategy_path="strategy.py")
    candidate, _experiment, _state = manager.create_candidate(
        "001",
        strategy_path="strategy.py",
    )
    (candidate / "strategy.py").write_text("2.0\n", encoding="utf-8")
    manager.champion_next_path.parent.mkdir(parents=True, exist_ok=True)
    manager.champion_next_path.write_text("2.0\n", encoding="utf-8")
    sha256 = hashlib.sha256(b"2.0\n").hexdigest()
    state["pending_promotion"] = {
        "round_id": "001/001",
        "sha256": sha256,
        "champion_number": 1,
        "metrics": {"gate": {"sortino": 2.0}},
        "project_revision": _git_text(tmp_path, "rev-parse", "HEAD"),
    }
    manager.state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = manager.load_state(strategy_path="strategy.py")

    assert recovered["champion_sha256"] == sha256
    assert recovered["champion_round_id"] == "001/001"
    assert recovered["pending_promotion"] is None
    assert manager.champion_path.read_text(encoding="utf-8") == "2.0\n"
    manager._remove_worktree(candidate)


def test_recovery_rolls_back_a_promotion_without_a_completed_version(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "recovery-rollback")
    state = manager.initialize(strategy_path="strategy.py")
    original_sha256 = state["champion_sha256"]
    state["pending_promotion"] = {
        "round_id": "001/001",
        "sha256": "0" * 64,
        "champion_number": 1,
        "metrics": {"gate": {"sortino": 2.0}},
        "project_revision": _git_text(tmp_path, "rev-parse", "HEAD"),
    }
    manager.state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = manager.initialize(strategy_path="strategy.py")

    assert recovered["champion_sha256"] == original_sha256
    assert recovered["pending_promotion"] is None


def test_recovery_finalizes_state_when_champion_file_was_already_replaced(
    tmp_path: Path,
) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    manager = ResearchWorkspace(
        tmp_path,
        tmp_path / ".research",
        "recovery-after-replace",
    )
    state = manager.initialize(strategy_path="strategy.py")
    manager.champion_path.write_text("2.0\n", encoding="utf-8")
    sha256 = hashlib.sha256(b"2.0\n").hexdigest()
    state["pending_promotion"] = {
        "round_id": "001/001",
        "sha256": sha256,
        "champion_number": 1,
        "metrics": {"gate": {"sortino": 2.0}},
        "project_revision": _git_text(tmp_path, "rev-parse", "HEAD"),
    }
    manager.state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = manager.load_state(strategy_path="strategy.py")

    assert recovered["champion_sha256"] == sha256
    assert recovered["champion_round_id"] == "001/001"
    assert recovered["pending_promotion"] is None


def test_loading_state_removes_an_orphaned_promotion_file(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    _init_repo(tmp_path)
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "orphan-promotion")
    manager.initialize(strategy_path="strategy.py")
    manager.champion_next_path.parent.mkdir(parents=True, exist_ok=True)
    manager.champion_next_path.write_text("orphan\n", encoding="utf-8")

    manager.load_state(strategy_path="strategy.py")

    assert not manager.champion_next_path.exists()
    assert manager.champion_path.read_text(encoding="utf-8") == "1.0\n"
