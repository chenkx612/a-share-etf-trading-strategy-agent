from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Sequence

from quant_core.research import ResearchTask, run_once
from quant_core.research.runner import _metrics_key


TASK_TOML = """
id = "runner-test"
goal = "Develop one strategy candidate"

[budget]
max_rounds = 3
max_hours = 4
max_consecutive_failures = 2

[opencode]
model = "xai/grok-4.5"
variant = "high"
timeout_minutes = 60

[strategy]
name = "runner-strategy"
module = "strategy"

[data]
universe = "universe.csv"

[scope]
editable = ["strategy.py"]
forbidden = ["evaluator.py"]

[commands]
test = ["{python}", "-m", "pytest", "-q"]
backtest = [
  "backtest", "--candidate-module", "{strategy_module}",
  "--start", "{start}", "--end", "{end}", "--run-id", "{run_id}"
]
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
        assert "Gate objective used to compare candidate with champion: sortino" in prompt
        assert "Minimum objective improvement required for acceptance: 0.0" in prompt
        assert "A feasible candidate replaces an infeasible champion" in prompt
        assert "Configured strategy: runner-strategy (strategy)" in prompt
        assert (
            'Hard gate constraints: [{"metric": "max_drawdown", "operator": "abs<=", '
            '"threshold": 0.2}]'
        ) in prompt
        agent_output = {
            "status": "completed",
            "previous_feedback": "",
            "hypothesis": "Momentum persists",
            "attempts": "Added and tested one medium-term momentum signal.",
            "development_effect": "Development Sortino improved while drawdown stayed within the limit.",
            "candidate": "Add momentum strategy",
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
            assert command[command.index("--candidate-module") + 1] == "strategy"
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
    assert command[command.index("--model") + 1] == "xai/grok-4.5"
    assert command[command.index("--variant") + 1] == "high"
    assert command[command.index("--format") + 1] == "json"
    assert command[command.index("--dir") + 1] == str(tmp_path)
    assert "--auto" in command
    assert json.loads((result_path.parent / "agent-output.json").read_text(encoding="utf-8"))["status"] == "completed"
    assert result["status"] == "completed"
    assert "feedback" not in result
    assert result["attempts"].startswith("Added and tested")
    assert result["candidate"] == "Add momentum strategy"
    assert result["metrics"]["gate"]["sortino"] == 1.2


def test_metrics_cache_key_changes_with_strategy_module() -> None:
    first_payload = tomllib.loads(TASK_TOML)
    second_payload = tomllib.loads(TASK_TOML)
    second_payload["strategy"]["module"] = "other_strategy"

    first = ResearchTask.from_mapping(first_payload)
    second = ResearchTask.from_mapping(second_payload)

    assert _metrics_key(first) != _metrics_key(second)

def test_run_once_accepts_compact_blocked_output(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")

    def blocked_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        log_path.write_text(json.dumps({
            "type": "text",
            "part": {"text": json.dumps({
                "status": "blocked",
                "previous_feedback": "",
                "error": "No viable development hypothesis",
            })},
        }) + "\n", encoding="utf-8")
        return 0

    result_path = run_once(
        task_path,
        "experiment-blocked",
        tmp_path / "experiment-blocked",
        workspace=tmp_path,
        opencode_runner=blocked_opencode,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result == {
        "experiment_id": "experiment-blocked",
        "status": "failed",
        "error": "OpenCode was blocked: No viable development hypothesis",
    }
