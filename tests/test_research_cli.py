from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import quant_core.cli as cli
from quant_core.research.workspace import copy_runtime_inputs


TASK = """
id = "test-snapshot"
goal = "Evaluate the champion"

[budget]
max_rounds = 1
max_hours = 1
max_consecutive_failures = 1

[opencode]
model = "test/model"
timeout_minutes = 1

[data]
universe = "universe.csv"

[scope]
editable = ["strategy.py"]

[commands]
test = ["pytest"]
backtest = ["fake-backtest", "{start}", "{end}", "{run_id}"]
metrics_path = "outputs/backtests/{run_id}/metrics.json"

[evaluation]
mode = "fixed"
objective = "sortino"

[evaluation.contract]
paths = [".gitignore"]

[evaluation.constraints]
max_drawdown = { operator = "abs<=", threshold = 0.20 }

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


def test_loop_shortcut_resolves_task_stem_and_enables_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    task_path = tasks / "sample_task.toml"
    task_path.write_text(
        'id = "sample-task"\naliases = ["sample"]\n',
        encoding="utf-8",
    )
    received: list[argparse.Namespace] = []
    monkeypatch.setattr(cli, "command_research_loop", received.append)

    args = cli.build_parser().parse_args([
        "--root",
        str(tmp_path),
        "loop",
        "sample_task",
        "-d",
    ])
    args.func(args)

    assert len(received) == 1
    assert received[0].task == str(task_path.resolve())
    assert received[0].retain_diagnostics is True
    assert received[0].research_root == ".research"


def test_loop_shortcut_accepts_task_id_and_explicit_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    task_path = tasks / "sample_task.toml"
    task_path.write_text(
        'id = "sample-task"\naliases = ["sample"]\n',
        encoding="utf-8",
    )
    received: list[argparse.Namespace] = []
    monkeypatch.setattr(cli, "command_research_loop", received.append)

    for reference in ("sample-task", "sample", "sample_task.toml", str(task_path)):
        args = cli.build_parser().parse_args([
            "--root",
            str(tmp_path),
            "loop",
            reference,
        ])
        args.func(args)

    assert [args.task for args in received] == [str(task_path.resolve())] * 4
    assert all(args.retain_diagnostics is False for args in received)


def test_loop_shortcut_reports_unknown_and_ambiguous_tasks(
    tmp_path: Path,
) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "first.toml").write_text('id = "shared"\n', encoding="utf-8")
    (tasks / "second.toml").write_text('id = "shared"\n', encoding="utf-8")

    with pytest.raises(SystemExit, match="unknown task: missing; available tasks: first, second"):
        cli.command_loop(argparse.Namespace(
            task="missing",
            root=str(tmp_path),
            research_root=".research",
            retain_diagnostics=False,
        ))
    with pytest.raises(SystemExit, match="task reference is ambiguous: shared"):
        cli.command_loop(argparse.Namespace(
            task="shared",
            root=str(tmp_path),
            research_root=".research",
            retain_diagnostics=False,
        ))

    (tasks / "first.toml").write_text(
        'id = "first-task"\naliases = ["short"]\n',
        encoding="utf-8",
    )
    (tasks / "second.toml").write_text(
        'id = "second-task"\naliases = ["short"]\n',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="task reference is ambiguous: short"):
        cli.command_loop(argparse.Namespace(
            task="short",
            root=str(tmp_path),
            research_root=".research",
            retain_diagnostics=False,
        ))


def test_legacy_research_loop_command_remains_available() -> None:
    args = cli.build_parser().parse_args([
        "research",
        "loop",
        "--task",
        "tasks/sample.toml",
        "--retain-diagnostics",
    ])

    assert args.func is cli.command_research_loop
    assert args.task == "tasks/sample.toml"
    assert args.retain_diagnostics is True


def test_research_test_snapshots_current_runtime_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK, encoding="utf-8")
    (tmp_path / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    latest_data = b"date,value\n2025-12-31,2\n"
    (tmp_path / "data/etf_daily.csv").write_bytes(latest_data)
    stale_runtime = tmp_path / "stale-runtime"
    (stale_runtime / "data").mkdir(parents=True)
    (stale_runtime / "data/etf_daily.csv").write_text(
        "date,value\n2024-12-31,1\n",
        encoding="utf-8",
    )

    class FakeWorkspace:
        def __init__(
            self,
            source: Path,
            research_root: Path,
            task_id: str,
            evaluation_environment_sha256: str | None = None,
        ) -> None:
            self.source = source
            self.root = research_root / task_id
            self.evaluation_runtime = stale_runtime

        def initialize(self, *args, **kwargs):
            return {"champion_sha256": "champion-sha"}

        def create_champion_test_evaluator(self, test_id: str, state):
            evaluator = self.root / ".tmp" / test_id
            evaluator.mkdir(parents=True)
            return evaluator

        def remove_evaluator(self, evaluator: Path) -> None:
            pass

    def fake_run(command, *, cwd, **kwargs):
        copied = (cwd / "data/etf_daily.csv").read_bytes()
        run_id = command[-1]
        metrics_path = cwd / "outputs" / "backtests" / run_id / "metrics.json"
        metrics_path.parent.mkdir(parents=True)
        metrics_path.write_text(
            json.dumps({"sortino": 2.0 if copied == latest_data else -1.0}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    monkeypatch.setattr(cli, "ResearchWorkspace", FakeWorkspace)
    monkeypatch.setattr(cli, "copy_runtime_inputs", copy_runtime_inputs)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.command_research_test(argparse.Namespace(
        task=str(task_path),
        root=str(tmp_path),
        research_root=".research",
    ))

    result_path = next((tmp_path / ".research/test-snapshot/tests").glob("*/result.json"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["metrics"]["sortino"] == 2.0
    assert len(result["evaluation_environment_sha256"]) == 64
    assert (
        tmp_path
        / ".research/test-snapshot/environments"
        / f"{result['evaluation_environment_sha256']}.json"
    ).is_file()
    assert result["runtime_inputs"] == {
        "data/etf_daily.csv": hashlib.sha256(latest_data).hexdigest(),
    }
