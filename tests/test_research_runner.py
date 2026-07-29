from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import date
from pathlib import Path
from typing import Sequence

import pytest
import pandas as pd

import quant_core.research.runner as research_runner
from quant_core.research import ResearchTask, run_once
from quant_core.research.attempt import evaluate as evaluate_attempt
from quant_core.research.attempt import record_learning
from quant_core.research.checkpoint import RUNTIME_DIR, submit
from quant_core.research.runner import (
    AgentContainerInfrastructureError,
    CandidateBindPreflightError,
    _agent_read_only_paths,
    _candidate_evaluator_facade,
    _docker_opencode_command,
    _development_finalization_reserve,
    _metrics_key,
    _load_research_history,
    _RoundClock,
    _run_opencode_with_permissions,
    _stage_opencode_runtime,
    _workspace_env,
    preflight_agent_container,
    preflight_provider_authentication,
    probe_candidate_bind_source,
)
from quant_core.research.workspace import build_development_view


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

WALK_FORWARD_TASK_TOML = TASK_TOML.replace(
    'mode = "fixed"',
    'mode = "walk_forward"',
).replace(
    "[evaluation.fixed.development]",
    """[evaluation.walk_forward]
train_months = 36
max_parameter_sets = 64

[evaluation.walk_forward.schedule]
period = "calendar_month"
interval = 1
trigger = "start"

[evaluation.walk_forward.development]""",
).replace(
    "[evaluation.fixed.gate]",
    "[evaluation.walk_forward.gate]",
)


def test_research_history_normalizes_fixed_and_walk_forward_development_metrics(
    tmp_path: Path,
) -> None:
    fixed_round = tmp_path / "001"
    walk_forward_round = tmp_path / "002"
    fixed_round.mkdir()
    walk_forward_round.mkdir()
    common = {
        "status": "completed",
        "hypothesis": "hypothesis",
        "attempts": "attempts",
        "development_effect": "effect",
        "candidate": "candidate",
        "changes": {"files": ["strategy.py"]},
    }
    (fixed_round / "result.json").write_text(json.dumps({
        **common,
        "development_attempts": [{
            "attempt_id": "001",
            "candidate_sha256": "a" * 64,
            "hypothesis": "Test a risk filter",
            "development_metrics": {"sortino": 1.1},
            "outcome": "abandoned",
            "learning": "The filter reduced return without enough drawdown benefit.",
        }],
        "metrics": {
            "development": {"sortino": 1.1, "max_drawdown": -0.2},
            "gate": {"sortino": 9.9},
        },
    }), encoding="utf-8")
    (walk_forward_round / "result.json").write_text(json.dumps({
        **common,
        "metrics": {
            "development": {
                "aggregate": {
                    "sortino": 1.2,
                    "max_drawdown": -0.15,
                    "annual_return": 0.18,
                    "avg_turnover": 0.3,
                },
                "folds": [{"sortino": 99.0}],
                "no_feasible_parameter_folds": 0,
            },
            "gate": {"aggregate": {"sortino": 8.8}},
        },
    }), encoding="utf-8")
    for round_path in (fixed_round, walk_forward_round):
        (round_path / "decision.json").write_text(
            json.dumps({"decision": "rejected", "reasons": []}),
            encoding="utf-8",
        )

    history = _load_research_history(tmp_path)

    assert history[0]["development_metrics"] == {
        "sortino": 1.1,
        "max_drawdown": -0.2,
    }
    assert history[0]["development_attempts"] == [{
        "attempt_id": "001",
        "hypothesis": "Test a risk filter",
        "outcome": "abandoned",
        "learning": "The filter reduced return without enough drawdown benefit.",
    }]
    assert history[1]["development_metrics"] == {
        "sortino": 1.2,
        "max_drawdown": -0.15,
        "annual_return": 0.18,
        "avg_turnover": 0.3,
        "no_feasible_parameter_folds": 0,
    }
    serialized = json.dumps(history)
    assert "99.0" not in serialized
    assert "9.9" not in serialized
    assert "8.8" not in serialized


def test_walk_forward_development_config_excludes_gate_and_test_periods(
    tmp_path: Path,
) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(WALK_FORWARD_TASK_TOML, encoding="utf-8")
    observed_config: dict[str, object] = {}
    development_commands: list[Sequence[str]] = []

    def fake_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        config_path = cwd / ".quant-research-development.json"
        observed_config.update(json.loads(config_path.read_text(encoding="utf-8")))
        assert "quant_core.research.attempt evaluate" in prompt
        agent_config = json.loads(
            (cwd / ".quant-research-agent-development.json").read_text(
                encoding="utf-8"
            )
        )
        assert agent_config["execution"] == {
            "round_clock_path": ".quant-research-round.json",
            "checkpoint_status_path": (
                ".quant-research-checkpoint-trusted/status.json"
            ),
            "strategy_path": "strategy.py",
            "progress_path": (
                "outputs/backtests/experiment-walk-forward-development/progress.json"
            ),
            "finalization_reserve_seconds": 300,
            "safety_factor": 1.25,
        }
        (cwd / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
        metadata_path = cwd / RUNTIME_DIR / "metadata.json"
        metadata_path.write_text(json.dumps({
            "previous_feedback": "",
            "hypothesis": "Improve selection",
            "attempts": "Tested one candidate.",
            "development_effect": "Development improved.",
            "candidate": "Retain candidate.",
        }), encoding="utf-8")
        submit(metadata_path, workspace=cwd)
        attempt = evaluate_attempt(workspace=cwd)
        learning_path = cwd / RUNTIME_DIR / "learning.json"
        learning_path.write_text(json.dumps({
            "learning": "The candidate improved Development Sortino.",
        }), encoding="utf-8")
        record_learning(str(attempt["attempt_id"]), learning_path, workspace=cwd)
        log_path.write_text(json.dumps({
            "type": "text",
            "part": {"text": json.dumps({
                "status": "completed",
                "previous_feedback": "",
                "hypothesis": "Improve selection",
                "attempts": "Tested one candidate.",
                "development_effect": "Development improved.",
                "candidate": "Retain candidate.",
            })},
        }) + "\n", encoding="utf-8")
        return 0

    def fake_command(
        command: Sequence[str], cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        if "--run-id" not in command:
            return 0
        run_id = command[command.index("--run-id") + 1]
        metrics_path = cwd / "outputs/backtests" / run_id / "metrics.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps({
            "aggregate": {"sortino": 1.2, "max_drawdown": -0.1},
            "folds": [],
            "no_feasible_parameter_folds": 0,
        }), encoding="utf-8")
        if "--walk-forward-config" in command:
            development_commands.append(command)
        return 0

    result_path = run_once(
        task_path,
        "experiment-walk-forward",
        tmp_path / "experiment-walk-forward",
        workspace=tmp_path,
        command_runner=fake_command,
        opencode_runner=fake_opencode,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert len(result["evaluation_environment_sha256"]) == 64
    assert observed_config["period"] == {"start": "2018-01-01", "end": "2021-12-31"}
    assert observed_config["walk_forward"] == {
        "train_months": 36,
        "max_parameter_sets": 64,
        "schedule": {
            "period": "calendar_month",
            "interval": 1,
            "trigger": "start",
        },
    }
    config_text = json.dumps(observed_config)
    assert "2022-01-01" not in config_text
    assert "2024-12-31" not in config_text
    assert "2025-01-01" not in config_text
    assert '"gate"' not in config_text
    assert '"test"' not in config_text
    assert len(development_commands) == 2
    assert result["development_attempts"] == [{
        "attempt_id": "001",
        "candidate_sha256": research_runner.hashlib.sha256(
            b"VALUE = 1\n"
        ).hexdigest(),
        "hypothesis": "Improve selection",
        "development_metrics": {
            "sortino": 1.2,
            "max_drawdown": -0.1,
            "no_feasible_parameter_folds": 0,
        },
        "outcome": "submitted",
        "learning": "The candidate improved Development Sortino.",
    }]


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


def test_development_finalization_reserve_scales_for_short_rounds() -> None:
    assert _development_finalization_reserve(5 * 60) == 75
    assert _development_finalization_reserve(30 * 60) == 300
    assert _development_finalization_reserve(60 * 60) == 300


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
        1_800_000,
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
    assert (
        "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=1800000"
        in command
    )
    assert command[-2:] == ["--dir", "/workspace"]
    assert "test-agent:local" in command


def test_candidate_evaluator_facade_cannot_reveal_walk_forward_by_patching_guard() -> None:
    namespace = {"__name__": "candidate_evaluator_facade"}
    exec(_candidate_evaluator_facade(), namespace)
    namespace["CANDIDATE_CONTAINER_MARKER"] = Path("/nonexistent")
    namespace["_reject_candidate_container_evaluation"] = lambda: None

    with pytest.raises(
        namespace["HarnessExecutionRequired"],
        match=r"python3 -m quant_core\.research\.attempt evaluate",
    ):
        namespace["evaluate_walk_forward"](
            None, None, None, None, None, None, None, None,
            execution=object(),
        )

    assert callable(namespace["evaluate_candidate"])
    assert "_passes" not in namespace


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


def test_provider_preflight_uses_trusted_host_opencode_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "xai": {"type": "oauth", "access": "access-a", "refresh": "refresh-a"},
    }), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))
    task = ResearchTask.from_mapping(tomllib.loads(TASK_TOML))

    def trusted_refresh(command, prompt, cwd, log_path, timeout, permissions) -> int:
        assert command[:3] == ["opencode", "run", "--pure"]
        assert set(permissions.values()) == {"deny"}
        payload = json.loads(auth.read_text(encoding="utf-8"))
        payload["xai"] = {
            "type": "oauth",
            "access": "access-b",
            "refresh": "refresh-b",
        }
        auth.write_text(json.dumps(payload), encoding="utf-8")
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(
        research_runner,
        "_run_opencode_with_permissions",
        trusted_refresh,
    )

    preflight_provider_authentication(task, tmp_path / ".research")

    saved = json.loads(auth.read_text(encoding="utf-8"))
    assert saved["xai"]["refresh"] == "refresh-b"


def test_provider_preflight_allows_environment_authenticated_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    original = {"deepseek": {"type": "api", "key": "unchanged"}}
    auth.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))
    payload = tomllib.loads(TASK_TOML)
    payload["opencode"]["model"] = "environment/test"
    task = ResearchTask.from_mapping(payload)

    def succeed(command, prompt, cwd, log_path, timeout, permissions) -> int:
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(
        research_runner,
        "_run_opencode_with_permissions",
        succeed,
    )

    preflight_provider_authentication(task, tmp_path / ".research")

    assert json.loads(auth.read_text(encoding="utf-8")) == original


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


def test_report_container_runner_mounts_frozen_input_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report_input = tmp_path / "report-input.json"
    report_input.write_text('{"rounds": []}', encoding="utf-8")
    report_facts = tmp_path / "report-facts.json"
    report_facts.write_text('{"schema_version": 1}', encoding="utf-8")
    captured: dict[str, object] = {}

    def capture_container(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        **kwargs,
    ) -> int:
        captured["read_only_paths"] = kwargs["read_only_paths"]
        captured["permissions"] = kwargs["permissions"]
        return 0

    monkeypatch.setattr(research_runner, "_run_opencode_container", capture_container)

    exit_code = research_runner._run_opencode_report_read_only(
        ["opencode", "run", "--file", "/workspace/report-input.json"],
        "short instructions",
        tmp_path,
        tmp_path / "report.log",
        10,
    )

    assert exit_code == 0
    assert captured["read_only_paths"] == (report_input, report_facts)
    assert captured["permissions"] == research_runner._NO_TOOL_PERMISSIONS


def test_container_runner_retries_daemon_missing_bind_source_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    invocations: list[Sequence[str]] = []
    removed: list[str] = []
    monkeypatch.setenv(
        "QUANT_OPENCODE_AUTH_FILE",
        str(tmp_path / "missing-auth.json"),
    )
    monkeypatch.setenv(
        "QUANT_OPENCODE_CONFIG_FILE",
        str(tmp_path / "missing-config.jsonc"),
    )

    def transient_failure(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        invocations.append(command)
        if len(invocations) == 1:
            log_path.write_text(
                "docker: Error response from daemon: invalid mount config for "
                'type "bind": bind source path does not exist: /host_mnt/workspace',
                encoding="utf-8",
            )
            return 125
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(research_runner, "_run_prompt_process", transient_failure)

    def remove_container(name: str) -> bool:
        removed.append(name)
        return True

    monkeypatch.setattr(research_runner, "_remove_agent_container", remove_container)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run"],
        "prompt",
        tmp_path,
        tmp_path / "agent.log",
        1,
    )

    assert exit_code == 0
    assert len(invocations) == 2
    assert len(removed) == 2
    assert (tmp_path / "agent.attempt-001.log").is_file()
    assert (tmp_path / "agent.log").is_file()
    first_name = invocations[0][invocations[0].index("--name") + 1]
    second_name = invocations[1][invocations[1].index("--name") + 1]
    assert first_name != second_name


def test_candidate_bind_probe_retries_with_independent_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    invocations: list[Sequence[str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def process(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        invocations.append(command)
        if len(invocations) == 1:
            log_path.write_text(
                "docker: Error response from daemon: "
                "bind source path does not exist",
                encoding="utf-8",
            )
            return 125
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)
    summary_path = probe_candidate_bind_source(
        candidate,
        tmp_path / "evidence",
        process_runner=process,
        sleeper=lambda seconds: None,
        event_sink=lambda event, **details: events.append((event, details)),
        round_id="002",
    )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "passed"
    assert len(summary["attempts"]) == 2
    assert (summary_path.parent / "attempt-001.log").is_file()
    assert not (summary_path.parent / "attempt-002.log").exists()
    assert [event for event, _details in events] == [
        "bind_probe_started",
        "bind_probe_attempt",
        "bind_probe_attempt",
        "bind_probe_succeeded",
    ]
    first_name = invocations[0][invocations[0].index("--name") + 1]
    second_name = invocations[1][invocations[1].index("--name") + 1]
    assert first_name != second_name


def test_candidate_bind_probe_fails_after_bounded_retries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    invocations = 0

    def unavailable(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        nonlocal invocations
        invocations += 1
        log_path.write_text(
            "docker: Error response from daemon: bind source path does not exist",
            encoding="utf-8",
        )
        return 125

    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)
    with pytest.raises(CandidateBindPreflightError) as raised:
        probe_candidate_bind_source(
            candidate,
            tmp_path / "evidence",
            process_runner=unavailable,
            sleeper=lambda seconds: None,
        )

    assert raised.value.code == "candidate_bind_unavailable"
    assert invocations == 5
    summary = json.loads(raised.value.evidence_path.read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert len(summary["attempts"]) == 5
    assert len(list(raised.value.evidence_path.parent.glob("attempt-*.log"))) == 5


def test_candidate_bind_probe_stops_when_host_source_disappears(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    invocations = 0

    def disappear(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        nonlocal invocations
        invocations += 1
        candidate.rmdir()
        log_path.write_text(
            "docker: Error response from daemon: bind source path does not exist",
            encoding="utf-8",
        )
        return 125

    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)
    with pytest.raises(CandidateBindPreflightError) as raised:
        probe_candidate_bind_source(
            candidate,
            tmp_path / "evidence",
            process_runner=disappear,
            sleeper=lambda seconds: None,
        )

    assert raised.value.code == "candidate_bind_source_missing"
    assert invocations == 1


@pytest.mark.skipif(
    os.environ.get("QUANT_TEST_AGENT_CONTAINER") != "1",
    reason="set QUANT_TEST_AGENT_CONTAINER=1 after building the research Agent image",
)
def test_candidate_bind_probe_uses_real_agent_container(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / ".git").write_text("gitdir: /tmp/not-used\n", encoding="utf-8")

    summary_path = probe_candidate_bind_source(
        candidate,
        tmp_path / "evidence",
    )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "passed"
    assert len(summary["attempts"]) == 1


def test_container_runner_does_not_retry_when_bind_source_is_locally_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    invocations = 0
    monkeypatch.setenv(
        "QUANT_OPENCODE_AUTH_FILE",
        str(tmp_path / "missing-auth.json"),
    )
    monkeypatch.setenv(
        "QUANT_OPENCODE_CONFIG_FILE",
        str(tmp_path / "missing-config.jsonc"),
    )

    def local_failure(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        nonlocal invocations
        invocations += 1
        candidate.rmdir()
        log_path.write_text(
            "docker: Error response from daemon: bind source path does not exist",
            encoding="utf-8",
        )
        return 125

    monkeypatch.setattr(research_runner, "_run_prompt_process", local_failure)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run"],
        "prompt",
        candidate,
        tmp_path / "agent.log",
        1,
    )

    assert exit_code == 125
    assert invocations == 1


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


def test_container_runner_does_not_recover_oauth_until_cleanup_is_confirmed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    original = {
        "xai": {
            "type": "oauth",
            "access": "access-a",
            "refresh": "refresh-a",
            "expires": 100,
        },
    }
    auth.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))
    validation_called = False

    def rotate(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> int:
        nonlocal validation_called
        if command[0] != "docker":
            validation_called = True
            return 0
        mount = next(
            part for part in command
            if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
        )
        runtime_auth = (
            Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            / ".local/share/opencode/auth.json"
        )
        payload = json.loads(runtime_auth.read_text(encoding="utf-8"))
        payload["xai"].update({
            "access": "access-b",
            "refresh": "refresh-b",
            "expires": 200,
        })
        runtime_auth.write_text(json.dumps(payload), encoding="utf-8")
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(research_runner, "_run_prompt_process", rotate)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: False)
    log_path = tmp_path / "agent.log"

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--model", "xai/test"],
        "prompt",
        tmp_path,
        log_path,
        1,
    )

    assert exit_code == 127
    assert not validation_called
    assert json.loads(auth.read_text(encoding="utf-8")) == original
    assert "Failed to remove Agent container" in log_path.read_text(encoding="utf-8")


def test_container_runner_deletes_staged_runtime_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evaluator = tmp_path / "src/quant_core/research/evaluator.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("# authoritative host evaluator\n", encoding="utf-8")
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
    candidate_markers: list[Path] = []
    evaluator_facades: list[Path] = []
    evaluator_cache_masks: list[Path] = []

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
        marker_mount = next(
            part
            for part in command
            if part.startswith("type=bind,src=")
            and ",dst=/run/quant-research/candidate-container,readonly" in part
        )
        candidate_marker = Path(
            marker_mount.split("src=", 1)[1].split(",dst=", 1)[0]
        )
        candidate_markers.append(candidate_marker)
        facade_mount = next(
            part
            for part in command
            if part.startswith("type=bind,src=")
            and ",dst=/workspace/src/quant_core/research/evaluator.py,readonly"
            in part
        )
        evaluator_facade = Path(
            facade_mount.split("src=", 1)[1].split(",dst=", 1)[0]
        )
        evaluator_facades.append(evaluator_facade)
        cache_mount = next(
            part
            for part in command
            if part.startswith("type=bind,src=")
            and ",dst=/workspace/src/quant_core/research/__pycache__,readonly"
            in part
        )
        evaluator_cache_mask = Path(
            cache_mount.split("src=", 1)[1].split(",dst=", 1)[0]
        )
        evaluator_cache_masks.append(evaluator_cache_mask)
        assert (runtime_home / ".local/share/opencode/auth.json").is_file()
        assert (runtime_home / ".config/opencode/opencode.jsonc").is_file()
        assert (runtime_home / ".cache/opencode/models.json").is_file()
        assert candidate_marker.read_text(encoding="utf-8").startswith(
            "Harness-managed"
        )
        assert candidate_marker.stat().st_mode & 0o777 == 0o444
        assert "evaluate_walk_forward = _harness_execution_required" in (
            evaluator_facade.read_text(encoding="utf-8")
        )
        assert evaluator_facade.stat().st_mode & 0o777 == 0o444
        assert evaluator_cache_mask.is_dir()
        assert list(evaluator_cache_mask.iterdir()) == []
        assert (evaluator.parent / "__pycache__").is_dir()
        assert any(
            part.startswith("type=bind,src=")
            and ",dst=/workspace/src/quant_core/research,readonly" in part
            for part in command
        )
        return 0

    monkeypatch.setattr(research_runner, "_run_prompt_process", succeed)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run"],
        "probe",
        tmp_path,
        tmp_path / "agent.log",
        1,
        read_only_paths=[evaluator.parent],
    )

    assert exit_code == 0
    assert len(runtime_homes) == 1
    assert not runtime_homes[0].exists()
    assert len(candidate_markers) == 1
    assert not candidate_markers[0].exists()
    assert len(evaluator_facades) == 1
    assert not evaluator_facades[0].exists()
    assert len(evaluator_cache_masks) == 1
    assert not evaluator_cache_masks[0].exists()


def test_candidate_session_persists_validated_rotated_oauth_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    original = {
        "deepseek": {"type": "api", "key": "unchanged"},
        "xai": {
            "type": "oauth",
            "access": "trusted-access",
            "refresh": "trusted-refresh",
            "expires": 100,
        },
    }
    auth.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))

    def rotate_and_validate(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> int:
        if command[0] == "docker":
            home_mount = next(
                part
                for part in command
                if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
            )
            runtime_home = Path(home_mount.split("src=", 1)[1].split(",dst=", 1)[0])
            runtime_auth = runtime_home / ".local/share/opencode/auth.json"
            payload = json.loads(runtime_auth.read_text(encoding="utf-8"))
            assert payload["xai"]["refresh"] == "trusted-refresh"
            payload["xai"].update({
                "access": "rotated-access",
                "refresh": "rotated-refresh",
                "expires": 200,
            })
            runtime_auth.write_text(json.dumps(payload), encoding="utf-8")
            log_path.write_text("trusted-refresh rotated-refresh", encoding="utf-8")
            return 0
        assert command[:3] == ["opencode", "run", "--pure"]
        assert env is not None
        validation_auth = Path(env["HOME"]) / ".local/share/opencode/auth.json"
        payload = json.loads(validation_auth.read_text(encoding="utf-8"))
        assert payload["xai"]["refresh"] == "rotated-refresh"
        payload["xai"].update({
            "access": "verified-access",
            "refresh": "verified-refresh",
            "expires": 300,
        })
        validation_auth.write_text(json.dumps(payload), encoding="utf-8")
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(research_runner, "_run_prompt_process", rotate_and_validate)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--model", "xai/test"],
        "candidate",
        tmp_path,
        tmp_path / "candidate.log",
        1,
    )

    assert exit_code == 0
    saved = json.loads(auth.read_text(encoding="utf-8"))
    assert saved["deepseek"] == original["deepseek"]
    assert saved["xai"] == {
        "type": "oauth",
        "access": "verified-access",
        "refresh": "verified-refresh",
        "expires": 300,
    }
    assert auth.stat().st_mode & 0o777 == 0o600
    assert "trusted-refresh" not in (tmp_path / "candidate.log").read_text(encoding="utf-8")
    assert "rotated-refresh" not in (tmp_path / "candidate.log").read_text(encoding="utf-8")


def test_invalid_rotated_oauth_state_does_not_overwrite_host(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    original = {
        "xai": {
            "type": "oauth",
            "access": "trusted-access",
            "refresh": "trusted-refresh",
            "expires": 100,
        },
    }
    auth.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))

    def corrupt(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> int:
        assert command[0] == "docker"
        home_mount = next(
            part
            for part in command
            if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
        )
        runtime_home = Path(home_mount.split("src=", 1)[1].split(",dst=", 1)[0])
        (runtime_home / ".local/share/opencode/auth.json").write_text(
            '{"xai":{"type":"oauth","access":"new","refresh":',
            encoding="utf-8",
        )
        log_path.write_text("failed with trusted-refresh", encoding="utf-8")
        return 1

    monkeypatch.setattr(research_runner, "_run_prompt_process", corrupt)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)
    log_path = tmp_path / "candidate.log"

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--model", "xai/test"],
        "candidate",
        tmp_path,
        log_path,
        1,
    )

    assert exit_code == 127
    assert json.loads(auth.read_text(encoding="utf-8")) == original
    assert "trusted-refresh" not in log_path.read_text(encoding="utf-8")
    failure = research_runner._infrastructure_failure(log_path)
    assert failure is not None
    assert failure.code == "provider_authentication_state"


def test_timeout_still_recovers_rotated_oauth_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "xai": {
            "type": "oauth",
            "access": "access-a",
            "refresh": "refresh-a",
            "expires": 100,
        },
    }), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))

    def time_out_after_rotation(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> int:
        if command[0] == "docker":
            mount = next(
                part for part in command
                if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
            )
            runtime_auth = (
                Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
                / ".local/share/opencode/auth.json"
            )
            payload = json.loads(runtime_auth.read_text(encoding="utf-8"))
            payload["xai"].update({
                "access": "access-b",
                "refresh": "refresh-b",
                "expires": 200,
            })
            runtime_auth.write_text(json.dumps(payload), encoding="utf-8")
            log_path.write_text("timed out", encoding="utf-8")
            return 124
        assert env is not None
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(research_runner, "_run_prompt_process", time_out_after_rotation)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--model", "xai/test"],
        "candidate",
        tmp_path,
        tmp_path / "candidate.log",
        1,
    )

    assert exit_code == 124
    saved = json.loads(auth.read_text(encoding="utf-8"))
    assert saved["xai"]["refresh"] == "refresh-b"


def test_concurrent_provider_change_prevents_oauth_overwrite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    original = {
        "xai": {
            "type": "oauth",
            "access": "access-a",
            "refresh": "refresh-a",
            "expires": 100,
        },
    }
    auth.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))

    def rotate_with_external_change(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> int:
        if command[0] == "docker":
            mount = next(
                part for part in command
                if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
            )
            runtime_auth = (
                Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
                / ".local/share/opencode/auth.json"
            )
            payload = json.loads(runtime_auth.read_text(encoding="utf-8"))
            payload["xai"].update({
                "access": "container-access",
                "refresh": "container-refresh",
                "expires": 200,
            })
            runtime_auth.write_text(json.dumps(payload), encoding="utf-8")
            external = json.loads(json.dumps(original))
            external["xai"].update({
                "access": "external-access",
                "refresh": "external-refresh",
                "expires": 300,
            })
            auth.write_text(json.dumps(external), encoding="utf-8")
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(research_runner, "_run_prompt_process", rotate_with_external_change)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--model", "xai/test"],
        "candidate",
        tmp_path,
        tmp_path / "candidate.log",
        1,
    )

    assert exit_code == 127
    saved = json.loads(auth.read_text(encoding="utf-8"))
    assert saved["xai"]["refresh"] == "external-refresh"
    failure = research_runner._infrastructure_failure(tmp_path / "candidate.log")
    assert failure is not None
    assert failure.code == "provider_authentication_state"


def test_interrupt_still_recovers_rotated_oauth_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "xai": {
            "type": "oauth",
            "access": "access-a",
            "refresh": "refresh-a",
            "expires": 100,
        },
    }), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))

    def interrupt_after_rotation(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> int:
        if command[0] == "docker":
            mount = next(
                part for part in command
                if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
            )
            runtime_auth = (
                Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
                / ".local/share/opencode/auth.json"
            )
            payload = json.loads(runtime_auth.read_text(encoding="utf-8"))
            payload["xai"].update({
                "access": "access-b",
                "refresh": "refresh-b",
                "expires": 200,
            })
            runtime_auth.write_text(json.dumps(payload), encoding="utf-8")
            log_path.write_text("interrupted", encoding="utf-8")
            raise KeyboardInterrupt
        log_path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(research_runner, "_run_prompt_process", interrupt_after_rotation)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)

    with pytest.raises(KeyboardInterrupt):
        research_runner._run_opencode_container(
            ["opencode", "run", "--model", "xai/test"],
            "candidate",
            tmp_path,
            tmp_path / "candidate.log",
            1,
        )

    saved = json.loads(auth.read_text(encoding="utf-8"))
    assert saved["xai"]["refresh"] == "refresh-b"


def test_failed_rotated_oauth_validation_is_an_authentication_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    original = {
        "xai": {
            "type": "oauth",
            "access": "access-a",
            "refresh": "refresh-a",
            "expires": 100,
        },
    }
    auth.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))

    def rotate_then_fail_validation(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> int:
        if command[0] == "docker":
            mount = next(
                part for part in command
                if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
            )
            runtime_auth = (
                Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
                / ".local/share/opencode/auth.json"
            )
            payload = json.loads(runtime_auth.read_text(encoding="utf-8"))
            payload["xai"].update({
                "access": "access-b",
                "refresh": "refresh-b",
                "expires": 200,
            })
            runtime_auth.write_text(json.dumps(payload), encoding="utf-8")
            log_path.write_text("", encoding="utf-8")
            return 0
        log_path.write_text("invalid_grant refresh-b", encoding="utf-8")
        return 1

    monkeypatch.setattr(research_runner, "_run_prompt_process", rotate_then_fail_validation)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)
    log_path = tmp_path / "candidate.log"

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--model", "xai/test"],
        "candidate",
        tmp_path,
        log_path,
        1,
    )

    assert exit_code == 127
    assert json.loads(auth.read_text(encoding="utf-8")) == original
    failure = research_runner._infrastructure_failure(log_path)
    assert failure is not None
    assert failure.code == "provider_authentication"


def test_failed_probe_persists_credentials_rotated_by_trusted_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = tmp_path / "auth.json"
    original = {
        "xai": {
            "type": "oauth",
            "access": "access-a",
            "refresh": "refresh-a",
            "expires": 100,
        },
    }
    auth.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))

    def rotate_twice_then_fail(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
        log_path: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> int:
        if command[0] == "docker":
            mount = next(
                part for part in command
                if part.startswith("type=bind,src=") and ",dst=/home/agent" in part
            )
            runtime_auth = (
                Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
                / ".local/share/opencode/auth.json"
            )
            payload = json.loads(runtime_auth.read_text(encoding="utf-8"))
            payload["xai"].update({
                "access": "access-b",
                "refresh": "refresh-b",
                "expires": 200,
            })
            runtime_auth.write_text(json.dumps(payload), encoding="utf-8")
            log_path.write_text("", encoding="utf-8")
            return 0
        assert env is not None
        validation_auth = Path(env["HOME"]) / ".local/share/opencode/auth.json"
        payload = json.loads(validation_auth.read_text(encoding="utf-8"))
        payload["xai"].update({
            "access": "access-c",
            "refresh": "refresh-c",
            "expires": 300,
        })
        validation_auth.write_text(json.dumps(payload), encoding="utf-8")
        log_path.write_text("unrelated inference failure", encoding="utf-8")
        return 1

    monkeypatch.setattr(research_runner, "_run_prompt_process", rotate_twice_then_fail)
    monkeypatch.setattr(research_runner, "_remove_agent_container", lambda name: True)
    log_path = tmp_path / "candidate.log"

    exit_code = research_runner._run_opencode_container(
        ["opencode", "run", "--model", "xai/test"],
        "candidate",
        tmp_path,
        log_path,
        1,
    )

    assert exit_code == 127
    saved = json.loads(auth.read_text(encoding="utf-8"))
    assert saved["xai"] == {
        "type": "oauth",
        "access": "access-c",
        "refresh": "refresh-c",
        "expires": 300,
    }
    failure = research_runner._infrastructure_failure(log_path)
    assert failure is not None
    assert failure.code == "provider_authentication"


def test_provider_authentication_error_precedes_round_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "xai": {"type": "oauth", "refresh": "secret-refresh"},
    }), encoding="utf-8")
    monkeypatch.setenv("QUANT_OPENCODE_AUTH_FILE", str(auth))

    def revoked_token(*args, **kwargs) -> int:
        log_path = args[3]
        log_path.write_text(
            'xAI token refresh failed (400): {"error":"invalid_grant",'
            '"error_description":"Refresh token has been revoked: secret-refresh"}',
            encoding="utf-8",
        )
        return 124

    monkeypatch.setattr(research_runner, "_run_opencode_container", revoked_token)
    result_path = run_once(
        task_path,
        "experiment-authentication",
        tmp_path / "experiment-authentication",
        workspace=tmp_path,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["failure_kind"] == "infrastructure"
    assert result["failure_code"] == "provider_authentication"
    assert result["error"] == (
        "Agent infrastructure failure: Provider authentication failed; "
        "re-authenticate before retrying"
    )
    assert "invalid_grant" not in result["error"]
    events = result_path.parent / "opencode-events.jsonl"
    assert "secret-refresh" not in events.read_text(encoding="utf-8")
    assert "[REDACTED]" in events.read_text(encoding="utf-8")


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


@pytest.mark.skipif(
    os.environ.get("QUANT_TEST_AGENT_CONTAINER") != "1",
    reason="set QUANT_TEST_AGENT_CONTAINER=1 after building the research Agent image",
)
def test_real_agent_preflight_validates_the_frozen_development_view(
    tmp_path: Path,
) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")
    shutil.copytree(Path.cwd() / "src", tmp_path / "src")
    evaluation = tmp_path / "evaluation"
    (evaluation / "data").mkdir(parents=True)
    pd.DataFrame({
        "date": ["2018-01-01", "2021-12-31", "2022-01-01"],
        "value": [1, 2, 3],
    }).to_parquet(evaluation / "data/prices.parquet", index=False)
    view, manifest = build_development_view(
        evaluation,
        tmp_path / ".research/runner-test/.cache/runtime/development-views",
        date(2021, 12, 31),
    )

    preflight_agent_container(
        ResearchTask.load(task_path),
        tmp_path / ".research",
        view,
        manifest,
    )


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
        assert "Agent Shell default timeout: 3600 seconds" in prompt
        assert "reserves the final 300 seconds" in prompt
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
        (cwd / "strategy.py").write_text("SIGNAL = 'momentum'\n", encoding="utf-8")
        metadata_path = cwd / RUNTIME_DIR / "metadata.json"
        metadata_path.write_text(json.dumps({
            "previous_feedback": "",
            **{
                key: agent_output[key]
                for key in (
                    "hypothesis", "attempts", "development_effect", "candidate",
                )
            },
        }), encoding="utf-8")
        submit(metadata_path, workspace=cwd)
        attempt = evaluate_attempt(workspace=cwd)
        learning_path = cwd / RUNTIME_DIR / "learning.json"
        learning_path.write_text(json.dumps({
            "learning": "Momentum improved Development risk-adjusted return.",
        }), encoding="utf-8")
        record_learning(str(attempt["attempt_id"]), learning_path, workspace=cwd)
        log_path.write_text(
            json.dumps({
                "type": "text",
                "part": {
                    "text": f"Done.\n```json\n{json.dumps(agent_output)}\n```\nMetadata: {{\"ignored\": true}}",
                },
            }) + "\n",
            encoding="utf-8",
        )
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
    assert result["development_attempts"][0]["outcome"] == "submitted"
    assert result["development_attempts"][0]["learning"].startswith(
        "Momentum improved"
    )
    assert result["metrics"]["gate"]["sortino"] == 1.2
    assert result["round_timing"]["timeout_seconds"] == 60 * 60
    assert events == [
        "agent_started",
        "checkpoint_accepted",
        "development_attempt_started",
        "development_attempt_completed",
        "development_attempt_learning_recorded",
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
        trusted = [
            path
            for path in read_only_paths
            if path.name == ".quant-research-checkpoint-trusted"
        ]
        assert len(trusted) == 1
        assert (trusted[0] / "status.json").is_file()
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
    assert len(result["evaluation_environment_sha256"]) == 64
    assert "OCI runtime create failed" in result["error"]


def test_metrics_cache_key_changes_with_strategy_module() -> None:
    first_payload = tomllib.loads(TASK_TOML)
    second_payload = tomllib.loads(TASK_TOML)
    third_payload = tomllib.loads(TASK_TOML)
    second_payload["strategy"]["module"] = "other_strategy"
    third_payload["evaluation"]["fixed"]["gate"]["end"] = "2024-11-30"

    first = ResearchTask.from_mapping(first_payload)
    second = ResearchTask.from_mapping(second_payload)
    third = ResearchTask.from_mapping(third_payload)

    assert _metrics_key(first) != _metrics_key(second)
    assert _metrics_key(first) != _metrics_key(third)


def test_walk_forward_metrics_cache_key_changes_with_objective_and_constraints() -> None:
    first_payload = tomllib.loads(WALK_FORWARD_TASK_TOML)
    objective_payload = tomllib.loads(WALK_FORWARD_TASK_TOML)
    constraint_payload = tomllib.loads(WALK_FORWARD_TASK_TOML)
    objective_payload["evaluation"]["objective"] = "sharpe"
    constraint_payload["evaluation"]["constraints"]["max_drawdown"]["threshold"] = 0.15

    first = ResearchTask.from_mapping(first_payload)
    objective_changed = ResearchTask.from_mapping(objective_payload)
    constraint_changed = ResearchTask.from_mapping(constraint_payload)

    assert _metrics_key(first) != _metrics_key(objective_changed)
    assert _metrics_key(first) != _metrics_key(constraint_changed)


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
    events_path = result_path.parent / "opencode-events.jsonl"
    assert events_path.is_file()
    event = json.loads(events_path.read_text(encoding="utf-8"))
    assert json.loads(event["part"]["text"])["status"] == "blocked"


@pytest.mark.parametrize(
    ("failed_stage", "expected_error"),
    [
        ("tests", "Tests failed"),
        ("development", "development backtest failed"),
    ],
)
def test_run_once_keeps_agent_events_after_post_agent_failure(
    tmp_path: Path,
    failed_stage: str,
    expected_error: str,
) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")

    def completed_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        (cwd / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
        log_path.write_text(json.dumps({
            "type": "text",
            "part": {"text": json.dumps({
                "status": "completed",
                **_checkpoint_metadata("post-agent-failure"),
            })},
        }) + "\n", encoding="utf-8")
        return 0

    def failing_command(
        command: Sequence[str], cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        if failed_stage == "tests":
            return 1
        if "--run-id" in command:
            return 1
        return 0

    result_path = run_once(
        task_path,
        f"experiment-{failed_stage}-failure",
        tmp_path / f"experiment-{failed_stage}-failure",
        workspace=tmp_path,
        command_runner=failing_command,
        opencode_runner=completed_opencode,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["error"] == expected_error
    events_path = result_path.parent / "opencode-events.jsonl"
    assert events_path.is_file()
    assert "post-agent-failure" in events_path.read_text(encoding="utf-8")


def test_run_once_enforces_round_deadline_and_records_timing(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(
        TASK_TOML.replace("max_hours = 4", "max_hours = 4\nround_minutes = 7"),
        encoding="utf-8",
    )
    events: list[tuple[str, dict[str, object]]] = []

    def timed_out_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        assert timeout == 7 * 60
        assert '"timeout_seconds":420' in (
            cwd / ".quant-research-round.json"
        ).read_text(encoding="utf-8").replace(" ", "")
        progress = cwd / "outputs/backtests/experiment-timeout-development/progress.json"
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(json.dumps({
            "status": "running",
            "completed_evaluations": 4,
            "remaining_round_seconds": 2,
        }), encoding="utf-8")
        return 124

    result_path = run_once(
        task_path,
        "experiment-timeout",
        tmp_path / "experiment-timeout",
        workspace=tmp_path,
        opencode_runner=timed_out_opencode,
        event_sink=lambda event, **details: events.append((event, details)),
        round_id="001",
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["error"] == "Candidate research deadline exceeded"
    assert result["round_timing"]["timeout_seconds"] == 7 * 60
    assert [event for event, _ in events] == ["agent_started", "round_deadline_exceeded"]
    assert events[-1][1]["development_progress"] == {
        "status": "running",
        "completed_evaluations": 4,
        "remaining_round_seconds": 2,
    }
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


def test_run_once_rejects_output_equal_to_workspace(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")

    with pytest.raises(ValueError, match="must differ"):
        run_once(task_path, "experiment-output-root", tmp_path, workspace=tmp_path)


def _checkpoint_metadata(label: str) -> dict[str, str]:
    return {
        "previous_feedback": "",
        "hypothesis": f"Hypothesis {label}",
        "attempts": f"Attempts {label}",
        "development_effect": f"Development effect {label}",
        "candidate": f"Candidate {label}",
    }


def test_run_once_restores_latest_checkpoint_after_deadline(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(
        TASK_TOML.replace("max_hours = 4", "max_hours = 4\nround_minutes = 7"),
        encoding="utf-8",
    )
    (tmp_path / "strategy.py").write_text("VALUE = 0\n", encoding="utf-8")
    current_time = [0.0]
    events: list[tuple[str, dict[str, object]]] = []
    commands: list[Sequence[str]] = []

    def checkpointing_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        assert "quant_core.research.checkpoint submit" in prompt
        for value in (1, 2):
            (cwd / "strategy.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
            metadata_path = cwd / RUNTIME_DIR / "metadata.json"
            metadata_path.write_text(
                json.dumps(_checkpoint_metadata(str(value))), encoding="utf-8",
            )
            acknowledgement = submit(metadata_path, workspace=cwd)
            assert acknowledgement["checkpoint_id"] == f"{value:03d}"
        (cwd / "strategy.py").write_text("VALUE = 999\n", encoding="utf-8")
        log_path.write_text("checkpoint recovery event\n", encoding="utf-8")
        current_time[0] = 421.0
        return 124

    def successful_command(
        command: Sequence[str], cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        commands.append(command)
        if "--run-id" in command:
            run_id = command[command.index("--run-id") + 1]
            metrics = cwd / "outputs/backtests" / run_id / "metrics.json"
            metrics.parent.mkdir(parents=True, exist_ok=True)
            metrics.write_text(
                json.dumps({"sortino": 1.2, "max_drawdown": -0.1}),
                encoding="utf-8",
            )
        return 0

    result_path = run_once(
        task_path,
        "experiment-checkpoint-timeout",
        tmp_path / "experiment-checkpoint-timeout",
        workspace=tmp_path,
        command_runner=successful_command,
        opencode_runner=checkpointing_opencode,
        event_sink=lambda event, **details: events.append((event, details)),
        round_id="001",
        monotonic=lambda: current_time[0],
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert result["candidate"] == "Candidate 2"
    assert result["submission"]["mode"] == "checkpoint"
    assert result["submission"]["checkpoint_id"] == "002"
    assert result["submission"]["submitted_by_timeout"] is True
    assert result["development_attempts"][0]["outcome"] == "submitted"
    assert result["development_attempts"][0]["learning"] is None
    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert len(commands) == 3
    assert [event for event, _ in events].count("checkpoint_accepted") == 2
    assert "checkpoint_restored" in [event for event, _ in events]
    assert not (result_path.parent / "opencode-events.jsonl").exists()


def test_run_once_prefers_final_submission_over_checkpoint(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(TASK_TOML, encoding="utf-8")
    (tmp_path / "strategy.py").write_text("VALUE = 0\n", encoding="utf-8")

    def completed_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        (cwd / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
        metadata_path = cwd / RUNTIME_DIR / "metadata.json"
        metadata_path.write_text(json.dumps(_checkpoint_metadata("checkpoint")), encoding="utf-8")
        submit(metadata_path, workspace=cwd)
        (cwd / "strategy.py").write_text("VALUE = 2\n", encoding="utf-8")
        log_path.write_text(json.dumps({
            "type": "text",
            "part": {"text": json.dumps({
                "status": "completed",
                **_checkpoint_metadata("final"),
            })},
        }) + "\n", encoding="utf-8")
        return 0

    def successful_command(
        command: Sequence[str], cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        if "--run-id" in command:
            run_id = command[command.index("--run-id") + 1]
            metrics = cwd / "outputs/backtests" / run_id / "metrics.json"
            metrics.parent.mkdir(parents=True, exist_ok=True)
            metrics.write_text(json.dumps({"sortino": 1.0, "max_drawdown": -0.1}), encoding="utf-8")
        return 0

    result_path = run_once(
        task_path,
        "experiment-final",
        tmp_path / "experiment-final",
        workspace=tmp_path,
        command_runner=successful_command,
        opencode_runner=completed_opencode,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["submission"]["mode"] == "final"
    assert result["submission"]["submitted_by_timeout"] is False
    assert result["candidate"] == "Candidate final"
    assert (tmp_path / "strategy.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_timeout_checkpoint_test_failure_does_not_fall_back(tmp_path: Path) -> None:
    task_path = tmp_path / "task.toml"
    task_path.write_text(
        TASK_TOML.replace("max_hours = 4", "max_hours = 4\nround_minutes = 7"),
        encoding="utf-8",
    )
    (tmp_path / "strategy.py").write_text("VALUE = 0\n", encoding="utf-8")
    current_time = [0.0]
    commands: list[Sequence[str]] = []

    def checkpointing_opencode(
        command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        for value in (1, 2):
            (cwd / "strategy.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
            metadata = cwd / RUNTIME_DIR / "metadata.json"
            metadata.write_text(json.dumps(_checkpoint_metadata(str(value))), encoding="utf-8")
            submit(metadata, workspace=cwd)
        log_path.write_text("checkpoint failure event\n", encoding="utf-8")
        current_time[0] = 421.0
        return 124

    def failing_test(
        command: Sequence[str], cwd: Path, log_path: Path, timeout: int,
    ) -> int:
        commands.append(command)
        assert (cwd / "strategy.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        return 1

    result_path = run_once(
        task_path,
        "experiment-checkpoint-test-failure",
        tmp_path / "experiment-checkpoint-test-failure",
        workspace=tmp_path,
        command_runner=failing_test,
        opencode_runner=checkpointing_opencode,
        monotonic=lambda: current_time[0],
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["error"] == "Tests failed"
    assert result["submission"]["checkpoint_id"] == "002"
    assert len(commands) == 1
    assert (
        result_path.parent / "opencode-events.jsonl"
    ).read_text(encoding="utf-8") == "checkpoint failure event\n"
