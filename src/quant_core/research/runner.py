from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from quant_core.research.contracts import ExperimentResult, ResearchTask


CommandRunner = Callable[[Sequence[str], Path, Path, int], int]

AGENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "hypothesis": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1},
    },
    "required": ["status", "hypothesis", "summary"],
    "additionalProperties": False,
}


def _run_command(command: Sequence[str], cwd: Path, log_path: Path, timeout: int) -> int:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        log_path.write_text(f"{output}\nCommand timed out", encoding="utf-8")
        return 124
    except OSError as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return 127
    log_path.write_text(completed.stdout, encoding="utf-8")
    return completed.returncode


def _run_codex(command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int) -> int:
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                process.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                return 124
            return process.returncode
    except OSError as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return 127


def _snapshot(root: Path, excluded: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    candidates = (
        [root / item.decode() for item in completed.stdout.split(b"\0") if item]
        if completed.returncode == 0
        else list(root.rglob("*"))
    )
    files: dict[str, str] = {}
    for path in candidates:
        if not path.is_file() or excluded == path or excluded in path.parents:
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def _is_within(path: str, prefixes: Sequence[str]) -> bool:
    normalized = path.rstrip("/")
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def _format_command(command: Sequence[str], values: dict[str, str]) -> list[str]:
    return [part.format_map(values) for part in command]


def _values(task: ResearchTask, period: dict[str, str], run_id: str, workspace: Path) -> dict[str, str]:
    return {
        "python": sys.executable,
        "universe": str(task.raw["data"]["universe"]),
        "workspace": str(workspace),
        "start": period["start"],
        "end": period["end"],
        "run_id": run_id,
    }


def _prompt(task: ResearchTask, development_command: Sequence[str], test_command: Sequence[str]) -> str:
    raw = task.raw
    return "\n".join([
        "Complete one quantitative strategy research round.",
        f"Goal: {raw['goal']}",
        "Propose one falsifiable hypothesis. Iterate internally until completed or blocked.",
        f"Editable paths: {', '.join(raw['scope']['editable'])}",
        f"Forbidden paths: {', '.join(raw['scope'].get('forbidden', [])) or '(none)'}",
        f"Test command: {' '.join(test_command)}",
        f"Development backtest command: {' '.join(development_command)}",
        f"Objective: {raw['evaluation']['objective']}",
        f"Constraints: {json.dumps(raw['evaluation']['constraints'], ensure_ascii=False)}",
        "Use only the development period. Do not inspect gate or test periods.",
        "Finish with the required JSON status, hypothesis, and summary. Do not report gate metrics or an acceptance decision.",
    ])


def _write_failed(output_dir: Path, experiment_id: str, error: str) -> Path:
    result_path = output_dir / "result.json"
    payload = {"experiment_id": experiment_id, "status": "failed", "error": error}
    ExperimentResult.from_mapping(payload)
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_path


def run_once(
    task_path: str | Path,
    experiment_id: str,
    output_dir: str | Path,
    *,
    workspace: str | Path = ".",
    command_runner: CommandRunner = _run_command,
    codex_runner: Callable[[Sequence[str], str, Path, Path, int], int] = _run_codex,
) -> Path:
    task = ResearchTask.load(task_path)
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Workspace does not exist: {root}")
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    schema_path = out / "agent-output-schema.json"
    agent_output_path = out / "agent-output.json"
    schema_path.write_text(json.dumps(AGENT_OUTPUT_SCHEMA, indent=2), encoding="utf-8")

    raw = task.raw
    fixed = raw["evaluation"]["fixed"]
    development_values = _values(task, fixed["development"], f"{experiment_id}-development", root)
    development_command = _format_command(raw["commands"]["backtest"], development_values)
    test_command = _format_command(raw["commands"]["test"], development_values)
    prompt = _prompt(task, development_command, test_command)
    codex = raw["codex"]
    timeout = int(codex["timeout_minutes"]) * 60
    codex_command = [
        "codex", "--ask-for-approval", codex["approval_policy"], "exec",
        "--ephemeral", "--skip-git-repo-check", "--sandbox", codex["sandbox"], "--json",
        "--output-schema", str(schema_path), "--output-last-message", str(agent_output_path),
        "--cd", str(root), "-",
    ]

    before = _snapshot(root, out)
    exit_code = codex_runner(
        codex_command,
        prompt,
        root,
        out / "codex-events.jsonl",
        timeout,
    )
    if exit_code != 0:
        reason = "Codex timed out" if exit_code == 124 else "Codex session failed"
        return _write_failed(out, experiment_id, reason)
    if not agent_output_path.exists():
        return _write_failed(out, experiment_id, "Codex did not produce agent-output.json")

    try:
        agent_output = json.loads(agent_output_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _write_failed(out, experiment_id, "Codex produced invalid agent output")
    if not isinstance(agent_output, dict) or agent_output.get("status") not in {"completed", "blocked"} or not all(
        isinstance(agent_output.get(key), str) and agent_output[key].strip()
        for key in ("hypothesis", "summary")
    ):
        return _write_failed(out, experiment_id, "Codex produced invalid agent output")
    if agent_output["status"] == "blocked":
        return _write_failed(out, experiment_id, f"Codex was blocked: {agent_output['summary']}")

    after = _snapshot(root, out)
    changed = _changed_files(before, after)
    generated_dir = Path(str(raw["commands"]["metrics_path"]).format_map(development_values)).parent.as_posix()
    changed = [path for path in changed if not _is_within(path, [generated_dir])]
    editable = raw["scope"]["editable"]
    forbidden = raw["scope"].get("forbidden", [])
    invalid = [path for path in changed if not _is_within(path, editable) or _is_within(path, forbidden)]
    if invalid:
        return _write_failed(out, experiment_id, f"Changes outside editable scope: {', '.join(invalid)}")
    if not changed:
        return _write_failed(out, experiment_id, "Codex completed without code changes")

    if command_runner(test_command, root, out / "tests.log", timeout) != 0:
        return _write_failed(out, experiment_id, "Tests failed")
    metrics: dict[str, Any] = {}
    for label in ("development", "gate"):
        values = _values(task, fixed[label], f"{experiment_id}-{label}", root)
        command = _format_command(raw["commands"]["backtest"], values)
        if command_runner(command, root, out / f"{label}.log", timeout) != 0:
            return _write_failed(out, experiment_id, f"{label} backtest failed")
        metrics_path = root / str(raw["commands"]["metrics_path"]).format_map(values)
        if not metrics_path.exists():
            return _write_failed(out, experiment_id, f"Missing {label} metrics")
        try:
            metrics[label] = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _write_failed(out, experiment_id, f"Invalid {label} metrics")

    payload = {
        "experiment_id": experiment_id,
        "status": "completed",
        "hypothesis": agent_output["hypothesis"],
        "changes": {"summary": agent_output["summary"], "files": changed},
        "metrics": metrics,
    }
    ExperimentResult.from_mapping(payload)
    result_path = out / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_path
