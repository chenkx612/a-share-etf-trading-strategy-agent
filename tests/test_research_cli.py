from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

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
        def __init__(self, source: Path, research_root: Path, task_id: str) -> None:
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
    assert result["runtime_inputs"] == {
        "data/etf_daily.csv": hashlib.sha256(latest_data).hexdigest(),
    }
