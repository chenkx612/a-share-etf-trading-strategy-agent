from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Sequence

import pytest

import quant_core.research.runner as research_runner
from quant_core.research import ResearchTask, run_once
from quant_core.research.runner import (
    AgentContainerInfrastructureError,
    _agent_read_only_paths,
    _docker_opencode_command,
    _metrics_key,
    _RoundClock,
    _run_opencode_with_permissions,
    _stage_opencode_runtime,
    _workspace_env,
    preflight_agent_container,
)


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


def test_workspace_env_prefers_candidate_source_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/existing/python/path")

    env = _workspace_env(tmp_path, {"EXTRA": "value"})

    assert env["PYTHONPATH"].split(os.pathsep) == [
        str((tmp_path / "src").resolve()),
        "/existing/python/path",
    ]
    assert env["EXTRA"] == "value"


def test_round_clock_emits_convergence_warnings_and_updates_phase(tmp_path: Path) -> None:
    events: list[tuple[str, int]] = []
    current_time = [0.0]
    clock = _RoundClock(
        tmp_path / ".quant-research-round.json",
        901,
        lambda event, **details: events.append((event, details["remaining_minutes"])),
        {"round": "001"},
        lambda: current_time[0],
    )

    current_time[0] = 2.0
    clock._write_status()
    assert json.loads(clock.path.read_text(encoding="utf-8"))["phase"] == "converge"
    current_time[0] = 602.0
    clock._write_status()
    assert json.loads(clock.path.read_text(encoding="utf-8"))["phase"] == "finalize"
    current_time[0] = 842.0
    clock._write_status()
    assert json.loads(clock.path.read_text(encoding="utf-8"))["phase"] == "submit_now"

    assert events == [
        ("round_time_warning", 15),
        ("round_time_warning", 5),
        ("round_time_warning", 1),
    ]
    clock.stop()


def test_opencode_timeout_kills_ordinary_child_processes(tmp_path: Path) -> None:
    marker = tmp_path / "child-finished"
    script = tmp_path / "agent.py"
    child_code = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.5); Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )

    exit_code = _run_opencode_with_permissions(
        [sys.executable, str(script)],
        "",
        tmp_path,
        tmp_path / "agent.log",
        0.1,
        {},
    )

    assert exit_code == 124
    time.sleep(0.7)
    assert not marker.exists()


def test_docker_opencode_command_mounts_only_candidate_and_read_only_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "research-root" / "task" / "candidate"
    candidate.mkdir(parents=True)
    development = candidate / "data"
    development.mkdir()
    fixed_script = candidate / "src" / "fixed.py"
    fixed_script.parent.mkdir()
    fixed_script.write_text("FIXED = True\n", encoding="utf-8")
    hidden = candidate / ".research"
    hidden.mkdir()
    mask = tmp_path / "empty-mask"
    mask.mkdir()
    runtime_home = tmp_path / "agent-home"
    runtime_home.mkdir()
    monkeypatch.setenv("QUANT_RESEARCH_AGENT_IMAGE", "test-agent:local")
    monkeypatch.setenv(
        "QUANT_OPENCODE_AUTH_FILE",
        str(tmp_path / "missing-auth.json"),
    )
    monkeypatch.setenv(
        "QUANT_OPENCODE_CONFIG_FILE",
        str(tmp_path / "missing-config.jsonc"),
    )

    command = _docker_opencode_command(
        ["opencode", "run", "--dir", str(candidate)],
        candidate,
        {"external_directory": "deny"},
        [development, fixed_script],
        [(mask, hidden)],
        runtime_home,
        "quant-agent-test",
    )

    rendered = "\n".join(command)
    assert f"src={candidate.resolve()},dst=/workspace" in rendered
    assert f"src={development.resolve()},dst=/workspace/data,readonly" in rendered
    assert (
        f"src={fixed_script.resolve()},dst=/workspace/src/fixed.py,readonly"
        in rendered
    )
    assert f"src={mask.resolve()},dst=/workspace/.research,readonly" in rendered
    assert f"src={runtime_home.resolve()},dst=/home/agent" in rendered
    assert f"--user\n{os.getuid()}:{os.getgid()}" in rendered
    assert "--name\nquant-agent-test" in rendered
    assert str(candidate.parent.resolve()) not in [
        part.split("src=", 1)[1].split(",dst=", 1)[0]
        for part in command
        if "src=" in part
    ]
    assert "--dir\n/workspace" in rendered
    assert command[-2:] == ["--dir", "/workspace"]
    assert "test-agent:local" in command


def test_opencode_runtime_is_staged_inside_single_home_mount(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "host-auth.json"
    config = tmp_path / "host-config.jsonc"
    models = tmp_path / "host-models.json"
    auth.write_text('{"token": "secret"}\n', encoding="utf-8")
    config.write_text('{"model": "test"}\n', encoding="utf-8")
    models.write_text('{"xai": {"models": {}}}\n', encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))
    monkeypatch.setenv("QUANT_OPENCODE_CONFIG_FILE", str(config))
    monkeypatch.setenv("QUANT_OPENCODE_MODELS_FILE", str(models))
    runtime_home = tmp_path / "runtime-home"

    _stage_opencode_runtime(runtime_home)
    command = _docker_opencode_command(
        ["true"],
        tmp_path,
        {},
        [],
        runtime_home=runtime_home,
    )

    assert (
        runtime_home / ".local/share/opencode/auth.json"
    ).read_text(encoding="utf-8") == auth.read_text(encoding="utf-8")
    assert (
        runtime_home / ".config/opencode/opencode.jsonc"
    ).read_text(encoding="utf-8") == config.read_text(encoding="utf-8")
    assert (
        runtime_home / ".cache/opencode/models.json"
    ).read_text(encoding="utf-8") == models.read_text(encoding="utf-8")
    assert (runtime_home / ".local/share/opencode/auth.json").stat().st_mode & 0o777 == 0o600
    rendered = "\n".join(command)
    assert f"src={runtime_home.resolve()},dst=/home/agent" in rendered
    assert str(auth.resolve()) not in rendered
    assert str(config.resolve()) not in rendered
    assert str(models.resolve()) not in rendered


def test_agent_container_preflight_reports_mount_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_container(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        *,
        read_only_paths: Sequence[Path] = (),
    ) -> int:
        log_path.write_text(
            "docker: Error response from daemon: invalid mount config for type bind",
            encoding="utf-8",
        )
        return 125

    monkeypatch.setattr(research_runner, "_run_opencode_container", fail_container)
    task = ResearchTask.from_mapping(tomllib.loads(TASK_TOML))

    with pytest.raises(
        AgentContainerInfrastructureError,
        match="invalid mount config",
    ):
        preflight_agent_container(task, tmp_path / ".research")

    preflight_root = tmp_path / ".research/runner-test/.tmp/container-preflight"
    assert list(preflight_root.iterdir()) == []


def test_agent_read_only_paths_preserve_generated_output_directory(tmp_path: Path) -> None:
    for relative in ("data", "outputs/factors", "outputs/backtests", "tests", ".research"):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)

    paths = _agent_read_only_paths(
        tmp_path,
        ["tests/", "outputs/", ".research/"],
        "outputs/backtests/run/metrics.json",
    )

    assert set(paths) == {
        tmp_path / "data",
        tmp_path / "outputs/factors",
        tmp_path / "tests",
    }


def test_container_runner_removes_container_after_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    removed: list[str] = []
    invocations: list[tuple[Sequence[str], str]] = []
    monkeypatch.setenv(
        "QUANT_OPENCODE_AUTH_FILE",
        str(tmp_path / "missing-auth.json"),
    )
    monkeypatch.setenv(
        "QUANT_OPENCODE_CONFIG_FILE",
        str(tmp_path / "missing-config.jsonc"),
    )
    def time_out(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        invocations.append((command, prompt))
        return 124

    monkeypatch.setattr(research_runner, "_run_prompt_process", time_out)

    def remove_container(name: str) -> bool:
        removed.append(name)
        return True

    monkeypatch.setattr(
        research_runner,
        "_remove_agent_container",
        remove_container,
    )

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--dir", str(tmp_path)],
        "prompt",
        tmp_path,
        tmp_path / "agent.log",
        1,
    )

    assert exit_code == 124
    assert len(removed) == 1
    assert removed[0].startswith("quant-agent-")
    assert invocations[0][0][-1] == "prompt"
    assert invocations[0][1] == ""


def test_container_cleanup_accepts_an_already_absent_container(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="Error response from daemon: No such container: quant-agent-test",
        ),
    )

    assert research_runner._remove_agent_container("quant-agent-test")


def test_container_runner_fails_when_container_cleanup_is_unconfirmed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "QUANT_OPENCODE_AUTH_FILE",
        str(tmp_path / "missing-auth.json"),
    )
    monkeypatch.setenv(
        "QUANT_OPENCODE_CONFIG_FILE",
        str(tmp_path / "missing-config.jsonc"),
    )
    monkeypatch.setattr(
        research_runner,
        "_run_prompt_process",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        research_runner,
        "_remove_agent_container",
        lambda name: False,
    )
    log_path = tmp_path / "agent.log"

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--dir", str(tmp_path)],
        "prompt",
        tmp_path,
        log_path,
        1,
    )

    assert exit_code == 127
    assert "Failed to remove Agent container" in log_path.read_text(encoding="utf-8")


def test_container_runner_deletes_staged_runtime_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    config = tmp_path / "opencode.jsonc"
    models = tmp_path / "models.json"
    auth.write_text('{"xai": "credential"}', encoding="utf-8")
    config.write_text("{}", encoding="utf-8")
    models.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))
    monkeypatch.setenv("QUANT_OPENCODE_CONFIG_FILE", str(config))
    monkeypatch.setenv("QUANT_OPENCODE_MODELS_FILE", str(models))
    runtime_homes: list[Path] = []

    def succeed(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        home_mount = next(
            part
            for part in command
            if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
        )
        runtime_home = Path(home_mount.split("src=", 1)[1].split(",dst=", 1)[0])
        runtime_homes.append(runtime_home)
        assert (runtime_home / ".local/share/opencode/auth.json").is_file()
        assert (runtime_home / ".config/opencode/opencode.jsonc").is_file()
        assert (runtime_home / ".cache/opencode/models.json").is_file()
        return 0

    monkeypatch.setattr(research_runner, "_run_prompt_process", succeed)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run"],
        "probe",
        tmp_path,
        tmp_path / "agent.log",
        1,
    )

    assert exit_code == 0
    assert len(runtime_homes) == 1
    assert not runtime_homes[0].exists()


@pytest.mark.skipif(
    os.environ.get("QUANT_TEST_AGENT_CONTAINER") != "1",
    reason="set QUANT_TEST_AGENT_CONTAINER=1 after building the research Agent image",
)
def test_agent_container_blocks_host_and_read_only_access(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    development = candidate / "data"
    development.mkdir()
    input_path = development / "input.txt"
    input_path.write_text("development", encoding="utf-8")
    fixed_script = candidate / "src" / "fixed.py"
    fixed_script.parent.mkdir()
    fixed_script.write_text("FIXED = True\n", encoding="utf-8")
    gate = tmp_path / "gate"
    gate.mkdir()
    secret = gate / "metrics.json"
    secret.write_text("gate-secret", encoding="utf-8")
    raw_history = candidate / ".research"
    raw_history.mkdir()
    (raw_history / "result.json").write_text("raw-gate-result", encoding="utf-8")
    mask = tmp_path / "empty-mask"
    mask.mkdir()
    runtime_home = tmp_path / "agent-home"
    runtime_home.mkdir()
    (candidate / "gate-link").symlink_to(secret)
    monkeypatch.setenv(
        "QUANT_OPENCODE_AUTH_FILE",
        str(tmp_path / "missing-auth.json"),
    )
    monkeypatch.setenv(
        "QUANT_OPENCODE_CONFIG_FILE",
        str(tmp_path / "missing-config.jsonc"),
    )
    script = (
        "import subprocess\n"
        "from pathlib import Path\n"
        "workspace = Path('/workspace')\n"
        "(workspace / 'candidate.txt').write_text('ok')\n"
        "data = workspace / 'data/input.txt'\n"
        "assert data.read_text() == 'development'\n"
        "try:\n"
        "    data.write_text('changed')\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('development data was writable')\n"
        "try:\n"
        "    (workspace / 'src/fixed.py').write_text('changed')\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('fixed script was writable')\n"
        f"assert not Path({str(secret)!r}).exists()\n"
        "assert not (workspace / '.research/result.json').exists()\n"
        f"assert subprocess.run(['bash', '-lc', 'cat {str(secret)}']).returncode != 0\n"
        "assert subprocess.run(['bash', '-lc', 'cat gate-link'], cwd=workspace).returncode != 0\n"
        "assert subprocess.run(['bash', '-lc', 'cat .research/result.json'], cwd=workspace).returncode != 0\n"
        "try:\n"
        "    (workspace / 'gate-link').read_text()\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('gate symlink escaped the container')\n"
    )
    command = _docker_opencode_command(
        ["python3", "-c", script],
        candidate,
        {},
        [development, fixed_script],
        [(mask, raw_history)],
        runtime_home,
    )

    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout
    assert (candidate / "candidate.txt").read_text(encoding="utf-8") == "ok"
    assert input_path.read_text(encoding="utf-8") == "development"
    assert fixed_script.read_text(encoding="utf-8") == "FIXED = True\n"
    assert secret.read_text(encoding="utf-8") == "gate-secret"


def test_run_once_uses_opencode_and_evaluates_gate(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")
    opencode_commands: list[Sequence[str]] = []
    events: list[str] = []

    def fake_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        opencode_commands.append(command)
        assert "2022-01-01" not in prompt
        assert "2025-01-01" not in prompt
        assert "Gate objective used to compare candidate with champion: sortino" in prompt
        assert "Minimum objective improvement required for acceptance: 0.0" in prompt
        assert "A feasible candidate replaces an infeasible champion" in prompt
        assert "heuristic guidance, not hard quotas" in prompt
        assert "do not perform local threshold mining" in prompt
        assert "Candidate research deadline (UTC):" in prompt
        assert "Live Round clock: .quant-research-round.json" in prompt
        assert timeout == 60 * 60
        clock = json.loads((cwd / ".quant-research-round.json").read_text(encoding="utf-8"))
        assert clock["timeout_seconds"] == 60 * 60
        assert clock["phase"] == "research"
        assert (
            "Development metrics path after that command: "
            "outputs/backtests/experiment-001-development/metrics.json"
        ) in prompt
        assert "development backtest is silent on success" in prompt
        assert "Do not load or run ETF discovery" in prompt
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
        event_sink=lambda event, **details: events.append(event),
        round_id="001",
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    command = list(opencode_commands[0])
    assert command[:2] == ["opencode", "run"]
    assert command[command.index("--model") + 1] == "xai/grok-4.5"
    assert command[command.index("--variant") + 1] == "high"
    assert command[command.index("--format") + 1] == "json"
    assert command[command.index("--dir") + 1] == str(tmp_path)
    assert "--auto" in command
    assert not (result_path.parent / "agent-output.json").exists()
    assert not (result_path.parent / "opencode-events.jsonl").exists()
    assert not (result_path.parent / "tests.log").exists()
    assert not (result_path.parent / "development.log").exists()
    assert not (result_path.parent / "gate.log").exists()
    assert not (tmp_path / ".quant-research-round.json").exists()
    assert result["status"] == "completed"
    assert result["previous_feedback"] == ""
    assert "feedback" not in result
    assert result["attempts"].startswith("Added and tested")
    assert result["candidate"] == "Add momentum strategy"
    assert result["metrics"]["gate"]["sortino"] == 1.2
    assert result["round_timing"]["timeout_seconds"] == 60 * 60
    assert events == [
        "agent_started",
        "agent_completed",
        "tests_started",
        "tests_passed",
        "development_started",
        "development_completed",
        "gate_started",
        "gate_completed",
    ]


def test_run_once_classifies_container_initialization_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")

    def fail_container(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        *,
        read_only_paths: Sequence[Path] = (),
    ) -> int:
        log_path.write_text(
            "docker: Error response from daemon: OCI runtime create failed",
            encoding="utf-8",
        )
        return 125

    monkeypatch.setattr(research_runner, "_run_opencode_container", fail_container)

    result_path = run_once(
        task_path,
        "experiment-infrastructure",
        tmp_path / "experiment-infrastructure",
        workspace=tmp_path,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["failure_kind"] == "infrastructure"
    assert "OCI runtime create failed" in result["error"]


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
    assert result["experiment_id"] == "experiment-blocked"
    assert result["status"] == "failed"
    assert result["error"] == "OpenCode was blocked: No viable development hypothesis"
    assert result["round_timing"]["timeout_seconds"] == 60 * 60


def test_run_once_enforces_round_deadline_and_records_timing(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(
        TASK_TOML.replace("max_hours = 4", "max_hours = 4\nround_minutes = 7"),
        encoding="utf-8",
    )
    events: list[str] = []

    def timed_out_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        assert timeout == 7 * 60
        assert '"timeout_seconds":420' in (
            cwd / ".quant-research-round.json"
        ).read_text(encoding="utf-8").replace(" ", "")
        return 124

    result_path = run_once(
        task_path,
        "experiment-timeout",
        tmp_path / "experiment-timeout",
        workspace=tmp_path,
        opencode_runner=timed_out_opencode,
        event_sink=lambda event, **details: events.append(event),
        round_id="001",
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["error"] == "Candidate research deadline exceeded"
    assert result["round_timing"]["timeout_seconds"] == 7 * 60
    assert events == ["agent_started", "round_deadline_exceeded"]
    assert not (tmp_path / ".quant-research-round.json").exists()


def test_run_once_rejects_late_success_before_tests_or_gate(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(
        TASK_TOML.replace("max_hours = 4", "max_hours = 4\nround_minutes = 7"),
        encoding="utf-8",
    )
    current_time = [0.0]
    commands: list[Sequence[str]] = []

    def late_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        current_time[0] = 421.0
        return 0

    def unexpected_command(
        command: Sequence[str], cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        commands.append(command)
        return 0

    result_path = run_once(
        task_path,
        "experiment-late-success",
        tmp_path / "experiment-late-success",
        workspace=tmp_path,
        opencode_runner=late_opencode,
        command_runner=unexpected_command,
        monotonic=lambda: current_time[0],
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["error"] == "Candidate research deadline exceeded"
    assert result["round_timing"]["duration_seconds"] == 421.0
    assert commands == []
