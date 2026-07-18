from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Sequence

import pytest

from quant_core.research.report import generate_loop_report, regenerate_loop_report
from quant_core.research.workspace import ResearchWorkspace, write_json_atomic


TASK = """
id = "report-test"
goal = "Improve a strategy"

[budget]
max_rounds = 2
max_hours = 1
max_consecutive_failures = 2

[opencode]
model = "xai/grok-4.5"
variant = "high"
timeout_minutes = 30

[strategy]
name = "example"
module = "quant_core.strategy.example"

[data]
universe = "universe.csv"

[scope]
editable = ["src/quant_core/strategy/example.py"]

[commands]
test = ["test-command"]
backtest = [
  "backtest", "--candidate-module", "{strategy_module}",
  "--start", "{start}", "--end", "{end}", "--run-id", "{run_id}"
]
metrics_path = "outputs/backtests/{run_id}/metrics.json"

[evaluation]
mode = "fixed"
objective = "sortino"

[evaluation.constraints]
max_drawdown = { operator = "abs<=", threshold = 0.15 }

[evaluation.acceptance]
minimum_improvement = 0.03

[evaluation.fixed.development]
start = "2020-01-01"
end = "2021-12-31"

[evaluation.fixed.gate]
start = "2022-01-01"
end = "2023-12-31"
"""


def _repo(root: Path) -> None:
    strategy = root / "src/quant_core/strategy/example.py"
    strategy.parent.mkdir(parents=True)
    strategy.write_text("PARAMETER = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=Test", "-c",
        "user.email=test@example.invalid", "commit", "-q", "-m", "init",
    ], check=True)


def test_generate_loop_report_uses_current_loop_rounds_and_champion(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK, encoding="utf-8")
    _repo(tmp_path)
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "report-test")
    task_state = base.initialize(
        date(2021, 12, 31),
        strategy_path="src/quant_core/strategy/example.py",
    )
    manager = base.for_run(1)
    manager.rounds.mkdir(parents=True)
    task_state["champion_number"] = 1
    task_state["champion_round_id"] = "001/001"
    task_state["champion_metrics"] = {
        "development": {"sortino": 1.4, "max_drawdown": -0.10},
        "gate": {"sortino": 1.2, "max_drawdown": -0.12},
    }
    write_json_atomic(manager.state_path, task_state)
    stale = manager.rounds / "000"
    stale.mkdir()
    write_json_atomic(stale / "result.json", {
        "status": "completed",
        "hypothesis": "Historical hypothesis from an earlier loop",
    })
    write_json_atomic(stale / "decision.json", {
        "experiment_id": "001/000",
        "decision": "rejected",
    })
    experiment = manager.rounds / "001"
    experiment.mkdir()
    write_json_atomic(experiment / "result.json", {
        "status": "completed",
        "hypothesis": "Use a faster risk filter",
        "attempts": "Tested three bounded variants",
        "development_effect": "Sortino improved",
        "candidate": "Set PARAMETER to 1",
        "metrics": task_state["champion_metrics"],
    })
    write_json_atomic(experiment / "decision.json", {
        "experiment_id": "001/001",
        "decision": "accepted",
        "objective": {
            "champion_constraints_passed": False,
            "relative_improvement_required": False,
        },
        "constraints": {
            "max_drawdown": {"actual": -0.12, "passed": True},
        },
        "reasons": [],
    })
    captured: dict[str, object] = {}

    def fake_agent(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        captured.update(command=list(command), prompt=prompt, cwd=cwd, timeout=timeout)
        log_path.write_text(json.dumps({
            "type": "text",
            "part": {"text": "# Research Loop 总结\n\n## 最终 Champion\n\nPARAMETER = 1"},
        }) + "\n", encoding="utf-8")
        return 0

    report_path = generate_loop_report(
        task_path,
        manager,
        {
            "status": "stopped",
            "stop_reason": "max_rounds",
            "rounds_completed": 1,
            "accepted": 1,
            "rejected": 0,
            "failed": 0,
            "elapsed_seconds": 42.0,
            "round_ids": ["001"],
        },
        agent_runner=fake_agent,
    )

    prompt = str(captured["prompt"])
    command = list(captured["command"])
    assert "Use a faster risk filter" in prompt
    assert "Historical hypothesis from an earlier loop" not in prompt
    assert '"decision": "accepted"' in prompt
    assert '"relative_improvement_required": false' in prompt
    assert '"source_round": "001/001"' in prompt
    assert '"sortino": 1.2' in prompt
    assert '"integrity_warnings": []' in prompt
    assert "PARAMETER = 1" in prompt
    assert command[command.index("--model") + 1] == "xai/grok-4.5"
    assert command[command.index("--variant") + 1] == "high"
    assert captured["cwd"] == manager.run_root
    assert captured["timeout"] == 600
    assert report_path.read_text(encoding="utf-8").startswith("# Research Loop 总结")
    assert not (manager.run_root / "report-events.jsonl").exists()

    loop_state_path = manager.loop_state_path
    write_json_atomic(loop_state_path, {
        "status": "stopped",
        "stop_reason": "max_rounds",
        "rounds_completed": 1,
        "accepted": 1,
        "rejected": 0,
        "failed": 0,
        "elapsed_seconds": 42.0,
        "round_ids": ["001"],
    })
    shutil.rmtree(base.runtime)
    regenerated = regenerate_loop_report(
        task_path,
        workspace=tmp_path,
        agent_runner=fake_agent,
    )
    saved_state = json.loads(loop_state_path.read_text(encoding="utf-8"))
    assert regenerated == report_path
    assert saved_state["report_status"] == "completed"
    assert saved_state["report_path"] == "report.md"
    assert saved_state["report_error"] is None
    assert "updated_at" in saved_state
    assert not base.runtime.exists()


def test_generate_loop_report_rejects_incomplete_agent_text(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK, encoding="utf-8")
    _repo(tmp_path)
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "report-test")
    base.initialize(
        date(2021, 12, 31),
        strategy_path="src/quant_core/strategy/example.py",
    )
    manager = base.for_run(1)
    manager.rounds.mkdir(parents=True)

    def fake_agent(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        log_path.write_text(json.dumps({
            "type": "text",
            "part": {"text": "我先分析一下这些记录。"},
        }) + "\n", encoding="utf-8")
        return 0

    with pytest.raises(RuntimeError, match="no valid Markdown report"):
        generate_loop_report(
            task_path,
            manager,
            {"rounds_completed": 0, "round_ids": []},
            agent_runner=fake_agent,
        )


def test_generate_loop_report_exposes_state_integrity_warnings(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK, encoding="utf-8")
    _repo(tmp_path)
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "report-test")
    base.initialize(
        date(2021, 12, 31),
        strategy_path="src/quant_core/strategy/example.py",
    )
    manager = base.for_run(1)
    manager.rounds.mkdir(parents=True)
    captured: dict[str, str] = {}

    def fake_agent(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        captured["prompt"] = prompt
        log_path.write_text(json.dumps({
            "type": "text",
            "part": {
                "text": "# Research Loop 总结\n\n## 总览\n\nHarness 状态一致性异常。"
            },
        }) + "\n", encoding="utf-8")
        return 0

    generate_loop_report(
        task_path,
        manager,
        {
            "rounds_completed": 1,
            "accepted": 0,
            "rejected": 0,
            "failed": 1,
            "round_ids": [],
        },
        agent_runner=fake_agent,
    )

    prompt = captured["prompt"]
    assert "integrity_warnings" in prompt
    assert "rounds_completed=1 but round_ids contains 0 entries" in prompt
    assert "不得为缺失的轮次或工件虚构研究内容" in prompt


def test_legacy_loop_state_scopes_report_to_latest_rounds(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK, encoding="utf-8")
    _repo(tmp_path)
    base = ResearchWorkspace(tmp_path, tmp_path / ".research", "report-test")
    base.initialize(
        date(2021, 12, 31),
        strategy_path="src/quant_core/strategy/example.py",
    )
    manager = base.for_run(1)
    manager.rounds.mkdir(parents=True)
    for experiment_id, hypothesis in (
        ("001", "Earlier loop"),
        ("002", "Current loop"),
    ):
        experiment = manager.rounds / experiment_id
        experiment.mkdir()
        write_json_atomic(experiment / "result.json", {
            "status": "completed",
            "hypothesis": hypothesis,
        })
        write_json_atomic(experiment / "decision.json", {
            "experiment_id": experiment_id,
            "decision": "rejected",
        })

    captured: dict[str, str] = {}

    def fake_agent(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        captured["prompt"] = prompt
        log_path.write_text(json.dumps({
            "type": "text",
            "part": {"text": "# Research Loop 总结\n\n## 总览\n\n完成。"},
        }) + "\n", encoding="utf-8")
        return 0

    generate_loop_report(
        task_path,
        manager,
        {"rounds_completed": 1},
        agent_runner=fake_agent,
    )

    assert "Current loop" in captured["prompt"]
    assert "Earlier loop" not in captured["prompt"]
    assert '"integrity_warnings": []' in captured["prompt"]
