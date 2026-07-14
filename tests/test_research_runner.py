from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from quant_core.research import run_once


TASK_TOML = """
id = "runner-test"
goal = "Develop one strategy candidate"

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
test = ["{python}", "-m", "pytest", "-q"]
backtest = ["backtest", "--start", "{start}", "--end", "{end}", "--run-id", "{run_id}"]
metrics_path = "outputs/backtests/{run_id}/metrics.json"

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


def test_run_once_uses_opencode_and_evaluates_gate(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")
    opencode_commands: list[Sequence[str]] = []

    def fake_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        opencode_commands.append(command)
        assert "2022-01-01" not in prompt
        assert "2025-01-01" not in prompt
        agent_output = {
            "status": "completed",
            "hypothesis": "Momentum persists",
            "summary": "Add momentum strategy",
        }
        log_path.write_text(
            json.dumps({
                "type": "text",
                "part": {
                    "text": f"Done.\n```json\n{json.dumps(agent_output)}\n```\nMetadata: {{\"ignored\": true}}",
                },
            }) + "\n",
            encoding="utf-8",
        )
        (cwd / "strategy.py").write_text("SIGNAL = 'momentum'\n", encoding="utf-8")
        return 0

    def fake_command(command: Sequence[str], cwd: Path, log_path: Path, timeout: int) -> int:
        if command[0] == "backtest":
            run_id = command[command.index("--run-id") + 1]
            metrics_path = cwd / "outputs/backtests" / run_id / "metrics.json"
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            metrics_path.write_text(
                json.dumps({"sortino": 1.2, "max_drawdown": -0.1, "avg_turnover": 0.1}),
                encoding="utf-8",
            )
        return 0

    result_path = run_once(
        task_path,
        "experiment-001",
        tmp_path / "experiment-001",
        workspace=tmp_path,
        command_runner=fake_command,
        opencode_runner=fake_opencode,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    command = list(opencode_commands[0])
    assert command[:2] == ["opencode", "run"]
    assert command[command.index("--model") + 1] == "deepseek/deepseek-chat"
    assert command[command.index("--format") + 1] == "json"
    assert command[command.index("--dir") + 1] == str(tmp_path)
    assert "--auto" in command
    assert json.loads((result_path.parent / "agent-output.json").read_text(encoding="utf-8"))["status"] == "completed"
    assert result["status"] == "completed"
    assert result["metrics"]["gate"]["sortino"] == 1.2
