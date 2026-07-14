from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Sequence

import pandas as pd
import pytest

from quant_core.research import run_managed_once
from quant_core.research.workspace import ResearchWorkspace


TASK = """
id = "{task_id}"
goal = "Improve the strategy"

[budget]
max_rounds = 3
max_hours = 4
max_consecutive_failures = 2

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


def _task(root: Path, task_id: str = "managed-test") -> Path:
    path = root / f"{task_id}.toml"
    path.write_text(TASK.format(task_id=task_id), encoding="utf-8")
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


def _opencode_with_signal(signal: float):
    def run(command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int) -> int:
        assert not (cwd / "managed-test.toml").exists()
        if (cwd / "data/etf_daily.csv").exists():
            daily = pd.read_csv(cwd / "data/etf_daily.csv")
            assert daily["date"].max() == "2021-12-31"
        log_path.write_text(json.dumps({"type": "text", "part": {"text": json.dumps({
            "status": "completed",
            "hypothesis": "A higher signal improves Sortino",
            "summary": f"Set signal to {signal}",
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
    first = ResearchWorkspace(tmp_path, tmp_path / ".research", "multi-factor")
    second = ResearchWorkspace(tmp_path, tmp_path / ".research", "macd-timing")

    first.initialize(date(2021, 12, 31))
    second.initialize()

    assert first.state_path != second.state_path
    assert first.champion_path().exists()
    assert second.champion_path().exists()
    assert not (first.champion_path() / "outputs").exists()
    assert (first.evaluation_runtime / "outputs/factors/factors.csv").exists()

    candidate, _experiment, _state = first.create_candidate("experiment-001", date(2021, 12, 31))
    assert (candidate / "outputs/factors/factors.csv").exists()
    assert not (candidate / "outputs/backtests").exists()
    assert pd.read_csv(candidate / "data/etf_daily.csv")["date"].tolist() == ["2021-12-31"]
    assert pd.read_parquet(candidate / "data/etf_daily.parquet")["date"].dt.date.tolist() == [date(2021, 12, 31)]
    assert pd.read_csv(first.evaluation_runtime / "data/etf_daily.csv")["date"].tolist() == [
        "2021-12-31",
        "2024-01-02",
    ]


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
        "experiment-001",
        workspace=tmp_path,
        command_runner=_command,
        opencode_runner=_opencode_with_signal(0.9),
    )
    first_decision = json.loads((first_result.parent / "decision.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / ".research/managed-test/state.json").read_text(encoding="utf-8"))
    assert first_decision["decision"] == "rejected"
    assert state["champion"] == "versions/baseline"
    assert not (tmp_path / ".research/managed-test/candidates/experiment-001").exists()

    second_result = run_managed_once(
        task,
        "experiment-002",
        workspace=tmp_path,
        command_runner=_command,
        opencode_runner=_opencode_with_signal(1.2),
    )
    second_decision = json.loads((second_result.parent / "decision.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / ".research/managed-test/state.json").read_text(encoding="utf-8"))
    champion = tmp_path / ".research/managed-test" / state["champion"]
    assert second_decision["decision"] == "accepted"
    assert state["champion"] == "versions/champion-001"
    assert (champion / "strategy.py").read_text(encoding="utf-8") == "1.2\n"
    assert not (champion / "outputs/backtests/experiment-002-development").exists()
    assert not (champion / "outputs/backtests/experiment-002-gate").exists()
    assert "-1.0" in (second_result.parent / "candidate.patch").read_text(encoding="utf-8")
    assert "+1.2" in (second_result.parent / "candidate.patch").read_text(encoding="utf-8")


def test_failed_candidate_does_not_change_champion(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    task = _task(tmp_path)

    def failed_opencode(command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int) -> int:
        (cwd / "strategy.py").write_text("99.0\n", encoding="utf-8")
        return 1

    result = run_managed_once(
        task,
        "experiment-failed",
        workspace=tmp_path,
        command_runner=_command,
        opencode_runner=failed_opencode,
    )
    decision = json.loads((result.parent / "decision.json").read_text(encoding="utf-8"))
    state = json.loads((tmp_path / ".research/managed-test/state.json").read_text(encoding="utf-8"))
    champion = tmp_path / ".research/managed-test" / state["champion"]
    assert decision["decision"] == "failed"
    assert (champion / "strategy.py").read_text(encoding="utf-8") == "1.0\n"
    assert not (tmp_path / ".research/managed-test/candidates/experiment-failed").exists()


def test_new_round_cleans_candidate_left_by_interrupted_run(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "recovery-test")
    manager.initialize()
    stale = manager.candidates / "interrupted"
    stale.mkdir()
    (stale / "strategy.py").write_text("99.0\n", encoding="utf-8")

    candidate, _experiment, _state = manager.create_candidate("next-round")

    assert not stale.exists()
    assert (candidate / "strategy.py").read_text(encoding="utf-8") == "1.0\n"


def test_experiment_id_cannot_escape_task_workspace(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "safe-task")

    with pytest.raises(ValueError, match="experiment id"):
        manager.create_candidate("../outside")


def test_recovery_commits_a_promoted_champion_after_state_write_was_interrupted(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "recovery-commit")
    state = manager.initialize()
    destination = manager.versions / "champion-001"
    shutil.copytree(manager.champion_path(state), destination)
    (destination / "strategy.py").write_text("2.0\n", encoding="utf-8")
    state["pending_promotion"] = {
        "experiment_id": "experiment-001",
        "champion": "versions/champion-001",
        "temporary": "versions/.champion-001.tmp",
        "champion_number": 1,
        "metrics": {"gate": {"sortino": 2.0}},
    }
    manager.state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = manager.initialize()

    assert recovered["champion"] == "versions/champion-001"
    assert recovered["pending_promotion"] is None
    assert (manager.champion_path(recovered) / "strategy.py").read_text(encoding="utf-8") == "2.0\n"


def test_recovery_rolls_back_a_promotion_without_a_completed_version(tmp_path: Path) -> None:
    (tmp_path / "strategy.py").write_text("1.0\n", encoding="utf-8")
    manager = ResearchWorkspace(tmp_path, tmp_path / ".research", "recovery-rollback")
    state = manager.initialize()
    temporary = manager.versions / ".champion-001.tmp"
    shutil.copytree(manager.champion_path(state), temporary)
    state["pending_promotion"] = {
        "experiment_id": "experiment-001",
        "champion": "versions/champion-001",
        "temporary": "versions/.champion-001.tmp",
        "champion_number": 1,
        "metrics": {"gate": {"sortino": 2.0}},
    }
    manager.state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = manager.initialize()

    assert recovered["champion"] == "versions/baseline"
    assert recovered["pending_promotion"] is None
    assert not temporary.exists()
