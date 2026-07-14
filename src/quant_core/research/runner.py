from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from quant_core.research.contracts import ExperimentResult, ResearchTask
from quant_core.research.workspace import (
    ResearchWorkspace,
    copy_runtime_inputs,
    remove_runtime_inputs,
    write_json_atomic,
    write_patch,
)


CommandRunner = Callable[[Sequence[str], Path, Path, int], int]
AgentRunner = Callable[[Sequence[str], str, Path, Path, int], int]


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


def _run_opencode(command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int) -> int:
    def terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            process.wait()

    try:
        with log_path.open("w", encoding="utf-8") as log:
            permissions = json.dumps({
                "external_directory": "deny",
                "question": "deny",
            })
            env = {**os.environ, "OPENCODE_PERMISSION": permissions}
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=env,
            )
            try:
                process.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                terminate(process)
                return 124
            except KeyboardInterrupt:
                terminate(process)
                raise
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
        "Your final response must be exactly one JSON object with string fields status, hypothesis, and summary.",
        'Set status to either "completed" or "blocked". Do not wrap the JSON in Markdown.',
        "Do not report gate metrics or an acceptance decision.",
    ])


def _is_agent_output(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"status", "hypothesis", "summary"}
        and value.get("status") in {"completed", "blocked"}
        and all(
            isinstance(value.get(key), str) and bool(value[key].strip())
            for key in ("hypothesis", "summary")
        )
    )


def _parse_opencode_output(log_path: Path) -> dict[str, Any] | None:
    """Extract the last valid agent result from OpenCode's final text event."""
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    text: str | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text = part["text"]
    if text is None:
        return None
    decoder = json.JSONDecoder()
    result: dict[str, Any] | None = None
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            output, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if _is_agent_output(output):
            result = output
    return result


def _write_failed(output_dir: Path, experiment_id: str, error: str) -> Path:
    result_path = output_dir / "result.json"
    payload = {"experiment_id": experiment_id, "status": "failed", "error": error}
    ExperimentResult.from_mapping(payload)
    write_json_atomic(result_path, payload)
    return result_path


def run_once(
    task_path: str | Path,
    experiment_id: str,
    output_dir: str | Path,
    *,
    workspace: str | Path = ".",
    gate_runtime: str | Path | None = None,
    command_runner: CommandRunner = _run_command,
    opencode_runner: AgentRunner = _run_opencode,
) -> Path:
    task = ResearchTask.load(task_path)
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Workspace does not exist: {root}")
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    agent_output_path = out / "agent-output.json"

    raw = task.raw
    fixed = raw["evaluation"]["fixed"]
    development_values = _values(task, fixed["development"], f"{experiment_id}-development", root)
    development_command = _format_command(raw["commands"]["backtest"], development_values)
    test_command = _format_command(raw["commands"]["test"], development_values)
    prompt = _prompt(task, development_command, test_command)
    opencode = raw["opencode"]
    timeout = int(opencode["timeout_minutes"]) * 60
    opencode_command = [
        "opencode", "run", "--auto", "--format", "json",
        "--model", opencode["model"], "--dir", str(root),
    ]

    before = _snapshot(root, out)
    events_path = out / "opencode-events.jsonl"
    exit_code = opencode_runner(
        opencode_command,
        prompt,
        root,
        events_path,
        timeout,
    )
    if exit_code != 0:
        reason = "OpenCode timed out" if exit_code == 124 else "OpenCode session failed"
        return _write_failed(out, experiment_id, reason)
    agent_output = _parse_opencode_output(events_path)
    if agent_output is None:
        return _write_failed(out, experiment_id, "OpenCode produced invalid agent output")
    write_json_atomic(agent_output_path, agent_output)
    if agent_output["status"] == "blocked":
        return _write_failed(out, experiment_id, f"OpenCode was blocked: {agent_output['summary']}")

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
        return _write_failed(out, experiment_id, "OpenCode completed without code changes")

    if command_runner(test_command, root, out / "tests.log", timeout) != 0:
        return _write_failed(out, experiment_id, "Tests failed")
    metrics: dict[str, Any] = {}
    for label in ("development", "gate"):
        temporary: tempfile.TemporaryDirectory[str] | None = None
        evaluation_root = root
        if label == "gate":
            temporary = tempfile.TemporaryDirectory(prefix="quant-gate-")
            evaluation_root = Path(temporary.name) / "workspace"
            shutil.copytree(root, evaluation_root, symlinks=True)
            if gate_runtime is not None:
                remove_runtime_inputs(evaluation_root)
                copy_runtime_inputs(Path(gate_runtime), evaluation_root)
        try:
            values = _values(task, fixed[label], f"{experiment_id}-{label}", evaluation_root)
            command = _format_command(raw["commands"]["backtest"], values)
            if command_runner(command, evaluation_root, out / f"{label}.log", timeout) != 0:
                return _write_failed(out, experiment_id, f"{label} backtest failed")
            metrics_path = evaluation_root / str(raw["commands"]["metrics_path"]).format_map(values)
            if not metrics_path.exists():
                return _write_failed(out, experiment_id, f"Missing {label} metrics")
            try:
                metrics[label] = json.loads(metrics_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return _write_failed(out, experiment_id, f"Invalid {label} metrics")
        finally:
            if temporary is not None:
                temporary.cleanup()

    payload = {
        "experiment_id": experiment_id,
        "status": "completed",
        "hypothesis": agent_output["hypothesis"],
        "changes": {"summary": agent_output["summary"], "files": changed},
        "metrics": metrics,
    }
    ExperimentResult.from_mapping(payload)
    result_path = out / "result.json"
    write_json_atomic(result_path, payload)
    return result_path


def _evaluate_existing(
    task: ResearchTask,
    workspace: Path,
    runtime_source: Path,
    experiment_id: str,
    output_dir: Path,
    command_runner: CommandRunner,
) -> dict[str, Any]:
    raw = task.raw
    timeout = int(raw["opencode"]["timeout_minutes"]) * 60
    metrics: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="quant-champion-") as temporary:
        evaluation_root = Path(temporary) / "workspace"
        shutil.copytree(workspace, evaluation_root, symlinks=True)
        copy_runtime_inputs(runtime_source, evaluation_root)
        for label in ("development", "gate"):
            values = _values(
                task,
                raw["evaluation"]["fixed"][label],
                f"{experiment_id}-champion-{label}",
                evaluation_root,
            )
            command = _format_command(raw["commands"]["backtest"], values)
            if command_runner(command, evaluation_root, output_dir / f"champion-{label}.log", timeout) != 0:
                raise RuntimeError(f"champion {label} backtest failed")
            metrics_path = evaluation_root / str(raw["commands"]["metrics_path"]).format_map(values)
            try:
                metrics[label] = json.loads(metrics_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise RuntimeError(f"invalid champion {label} metrics") from exc
    return metrics


def _constraint_passes(name: str, value: Any, limit: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if name == "max_drawdown":
        return abs(float(value)) <= float(limit)
    return float(value) <= float(limit)


def target_reached(task: ResearchTask, metrics: Mapping[str, Any] | None) -> bool:
    target = task.raw["evaluation"].get("target")
    if not isinstance(target, dict) or not isinstance(metrics, Mapping):
        return False
    gate = metrics.get("gate")
    if not isinstance(gate, Mapping):
        return False
    evaluation = task.raw["evaluation"]
    objective = gate.get(str(evaluation["objective"]))
    threshold = target["objective_at_least"]
    if not isinstance(objective, (int, float)) or isinstance(objective, bool):
        return False
    return float(objective) >= float(threshold) and all(
        _constraint_passes(name, gate.get(name), limit)
        for name, limit in evaluation["constraints"].items()
    )


def _decide(task: ResearchTask, champion: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    evaluation = task.raw["evaluation"]
    objective = str(evaluation["objective"])
    champion_value = champion.get("gate", {}).get(objective)
    candidate_value = candidate.get("gate", {}).get(objective)
    acceptance = evaluation.get("acceptance", {})
    minimum_improvement = float(acceptance.get("minimum_improvement", 0.0))
    constraints: dict[str, Any] = {}
    constraints_passed = True
    for name, limit in evaluation["constraints"].items():
        actual = candidate.get("gate", {}).get(name)
        passed = _constraint_passes(name, actual, limit)
        constraints[name] = {"limit": limit, "actual": actual, "passed": passed}
        constraints_passed = constraints_passed and passed
    objective_passed = (
        isinstance(champion_value, (int, float))
        and not isinstance(champion_value, bool)
        and isinstance(candidate_value, (int, float))
        and not isinstance(candidate_value, bool)
        and float(candidate_value) >= float(champion_value) + minimum_improvement
        and (minimum_improvement > 0 or float(candidate_value) > float(champion_value))
    )
    accepted = constraints_passed and objective_passed
    reasons: list[str] = []
    if not constraints_passed:
        reasons.append("gate constraints failed")
    if not objective_passed:
        reasons.append("gate objective did not improve over champion")
    return {
        "decision": "accepted" if accepted else "rejected",
        "objective": {
            "name": objective,
            "champion": champion_value,
            "candidate": candidate_value,
            "minimum_improvement": minimum_improvement,
        },
        "constraints": constraints,
        "reasons": reasons,
    }


def _metrics_key(task: ResearchTask) -> str:
    relevant = {
        "data": task.raw["data"],
        "commands": task.raw["commands"],
        "periods": task.raw["evaluation"]["fixed"],
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_managed_once(
    task_path: str | Path,
    experiment_id: str,
    *,
    workspace: str | Path = ".",
    research_root: str | Path = ".research",
    command_runner: CommandRunner = _run_command,
    opencode_runner: AgentRunner = _run_opencode,
) -> Path:
    """Run one isolated candidate and promote it only when it beats the champion."""
    task_file = Path(task_path).resolve()
    task = ResearchTask.load(task_file)
    source = Path(workspace).resolve()
    managed_root = Path(research_root)
    if not managed_root.is_absolute():
        managed_root = source / managed_root
    manager = ResearchWorkspace(source, managed_root, task.task_id)
    development_end = task.raw["evaluation"]["fixed"]["development"]["end"]
    candidate, experiment, state = manager.create_candidate(experiment_id, date.fromisoformat(development_end))
    if source in task_file.parents:
        candidate_task = candidate / task_file.relative_to(source)
        candidate_task.unlink(missing_ok=True)
    result_path = run_once(
        task_file,
        experiment_id,
        experiment,
        workspace=candidate,
        gate_runtime=manager.evaluation_runtime,
        command_runner=command_runner,
        opencode_runner=opencode_runner,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    decision_path = experiment / "decision.json"
    if result.get("status") != "completed":
        decision = {"experiment_id": experiment_id, "decision": "failed", "reasons": [result.get("error")]}
        write_json_atomic(decision_path, decision)
        manager.reject(candidate, state, experiment_id)
        return result_path

    write_patch(manager.champion_path(state), candidate, task.raw["scope"]["editable"], experiment / "candidate.patch")
    metrics_key = _metrics_key(task)
    champion_metrics = state.get("champion_metrics") if state.get("champion_metrics_key") == metrics_key else None
    if not isinstance(champion_metrics, dict):
        try:
            champion_metrics = _evaluate_existing(
                task,
                manager.champion_path(state),
                manager.evaluation_runtime,
                experiment_id,
                experiment,
                command_runner,
            )
        except RuntimeError as exc:
            decision = {"experiment_id": experiment_id, "decision": "failed", "reasons": [str(exc)]}
            write_json_atomic(decision_path, decision)
            manager.reject(candidate, state, experiment_id)
            return _write_failed(experiment, experiment_id, str(exc))

    state["champion_metrics_key"] = metrics_key
    decision = {"experiment_id": experiment_id, **_decide(task, champion_metrics, result["metrics"])}
    write_json_atomic(decision_path, decision)
    if decision["decision"] == "accepted":
        manager.promote(candidate, state, experiment_id, result["metrics"])
    else:
        state["champion_metrics"] = champion_metrics
        manager.reject(candidate, state, experiment_id)
    return result_path
