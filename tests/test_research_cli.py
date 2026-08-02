from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import quant_core.cli as cli


TASK = """
id = "test-snapshot"
goal = "Evaluate the champion"

[budget]
max_rounds = 1
max_hours = 1
max_consecutive_failures = 1
round_minutes = 1

[opencode]
model = "test/model"

[execution]
command_timeout_minutes = 1

[data]
universe = "universe.csv"

[scope]
editable = ["strategy.py"]

[commands]
tests = ["tests/test_strategy.py"]
backtest = ["fake-backtest", "{start}", "{end}", "{run_id}"]

[evaluation]
mode = "fixed"
objective = "sortino"

[evaluation.constraints]
max_drawdown = { operator = "abs<=", threshold = 0.20 }

[evaluation.fixed.development]
start = "2018-01-01"
end = "2021-12-31"

[evaluation.fixed.gate]
start = "2022-01-01"
end = "2024-12-31"

"""


def test_loop_shortcut_resolves_task_stem_and_enables_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    task_path = tasks / "sample_task.toml"
    task_path.write_text(
        'id = "sample-task"\n',
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


def test_research_loop_exits_nonzero_when_production_sync_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runs/001/artifacts/state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "schema_version": 6,
        "stop_reason": "max_rounds",
        "rounds_completed": 1,
        "accepted": 1,
        "rejected": 0,
        "failed": 0,
        "report_status": "completed",
        "report_path": "report.md",
        "guard_query_count": 0,
        "production_sync_status": "conflict",
        "production_sync_error": "production strategy changed outside the Run",
    }), encoding="utf-8")
    monkeypatch.setattr(cli, "run_loop", lambda *args, **kwargs: state_path)
    args = argparse.Namespace(
        task="task.toml",
        root=str(tmp_path),
        research_root=".research",
        retain_diagnostics=False,
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.command_research_loop(args)

    assert exc_info.value.code == 1


def test_loop_shortcut_accepts_task_id_and_explicit_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    task_path = tasks / "sample_task.toml"
    task_path.write_text(
        'id = "sample-task"\n',
        encoding="utf-8",
    )
    received: list[argparse.Namespace] = []
    monkeypatch.setattr(cli, "command_research_loop", received.append)

    for reference in ("sample-task", "sample_task.toml", str(task_path)):
        args = cli.build_parser().parse_args([
            "--root",
            str(tmp_path),
            "loop",
            reference,
        ])
        args.func(args)

    assert [args.task for args in received] == [str(task_path.resolve())] * 3
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
