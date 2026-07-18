from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
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
)


CommandRunner = Callable[[Sequence[str], Path, Path, int], int]
AgentRunner = Callable[[Sequence[str], str, Path, Path, int], int]
EventSink = Callable[..., None]
_RESEARCH_HISTORY_LIMIT = 12


def _emit(event_sink: EventSink | None, event: str, **details: Any) -> None:
    if event_sink is not None:
        event_sink(event, **details)


def _workspace_env(cwd: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, **(extra or {})}
    source_root = str((cwd / "src").resolve())
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not existing else os.pathsep.join((source_root, existing))
    return env


def _run_command(command: Sequence[str], cwd: Path, log_path: Path, timeout: int) -> int:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=_workspace_env(cwd),
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


def _run_with_failure_log(
    runner: CommandRunner,
    command: Sequence[str],
    cwd: Path,
    log_path: Path,
    timeout: int,
) -> int:
    exit_code = runner(command, cwd, log_path, timeout)
    if exit_code == 0:
        log_path.unlink(missing_ok=True)
    return exit_code


def _run_opencode_with_permissions(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
    permissions: Mapping[str, str],
) -> int:
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
            env = _workspace_env(
                cwd,
                {"OPENCODE_PERMISSION": json.dumps(dict(permissions))},
            )
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


def _run_opencode(command: Sequence[str], prompt: str, cwd: Path, log_path: Path, timeout: int) -> int:
    return _run_opencode_with_permissions(
        command,
        prompt,
        cwd,
        log_path,
        timeout,
        {
            "external_directory": "deny",
            "question": "deny",
        },
    )


def _run_opencode_read_only(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
) -> int:
    return _run_opencode_with_permissions(
        command,
        prompt,
        cwd,
        log_path,
        timeout,
        {
            "external_directory": "deny",
            "question": "deny",
            "bash": "deny",
            "edit": "deny",
            "task": "deny",
            "skill": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "todowrite": "deny",
        },
    )


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
    values = {
        "python": sys.executable,
        "universe": str(task.raw["data"]["universe"]),
        "workspace": str(workspace),
        "start": period["start"],
        "end": period["end"],
        "run_id": run_id,
    }
    if task.strategy_name is not None:
        values["strategy_name"] = task.strategy_name
    if task.strategy_module is not None:
        values["strategy_module"] = task.strategy_module
    return values


def _constraint_rule(constraint: Mapping[str, Any]) -> tuple[str, float]:
    return str(constraint["operator"]), float(constraint["threshold"])


def _constraint_descriptions(constraints: Mapping[str, Any]) -> list[dict[str, Any]]:
    descriptions: list[dict[str, Any]] = []
    for name, constraint in constraints.items():
        operator, threshold = _constraint_rule(constraint)
        descriptions.append({"metric": name, "operator": operator, "threshold": threshold})
    return descriptions


def _prompt(
    task: ResearchTask,
    development_command: Sequence[str],
    development_metrics_path: str,
    test_command: Sequence[str],
    research_history: Sequence[Mapping[str, Any]],
    has_champion: bool,
) -> str:
    raw = task.raw
    evaluation = raw["evaluation"]
    minimum_improvement = evaluation.get("acceptance", {}).get("minimum_improvement", 0.0)
    target = evaluation.get("target", {}).get("objective_at_least")
    history = (
        json.dumps(list(research_history), ensure_ascii=False, separators=(",", ":"))
        if research_history
        else "(no prior experiments)"
    )
    comparison_guidance = (
        f"Gate objective used to compare candidate with champion: {evaluation['objective']}\n"
        f"Minimum objective improvement required for acceptance: {minimum_improvement}\n"
        "A candidate must pass every hard gate constraint. A feasible candidate replaces an "
        "infeasible champion without needing relative objective improvement. Once the champion is "
        "feasible, a candidate must also improve the gate objective by the required amount."
        if has_champion
        else
        f"There is no champion yet. The first candidate with a numeric gate {evaluation['objective']} "
        "that passes every hard gate constraint becomes the initial champion. The configured minimum "
        "improvement does not apply until a champion exists."
    )
    lines = [
        "Complete one quantitative strategy research round.",
        f"Goal: {raw['goal']}",
        "Use the full prior research history internally when choosing this round's hypothesis.",
        "At the start, write previous_feedback only for the most recent prior round whose gate decision is now available.",
        "Keep previous_feedback concise; do not write a new comprehensive feedback summary for the full history.",
        "If there is no prior round, set previous_feedback to an empty string.",
        "Treat accepted/rejected as evidence about a specific implementation, not proof that an entire idea is true or false.",
        "A failed round is inconclusive. Do not repeat a rejected implementation unchanged.",
        f"Prior research history (sanitized; exact gate metrics are intentionally omitted): {history}",
        "Propose one falsifiable hypothesis and keep every development variant within that mechanism.",
        "Use at most 3 implementation attempts in this round and test at most 6 parameter "
        "configurations per attempt (18 candidate evaluations total, excluding the baseline).",
        "Do not start a new signal family or broad parameter sweep after seeing results. If the "
        "hypothesis is exhausted, submit the best defensible variant or return blocked.",
        "Stop development search as soon as a candidate passes the stated constraints and reaches "
        "the configured objective improvement; additional optimization is out of scope.",
        "After a rejected round, prefer a materially different risk mechanism. Reuse the prior "
        "mechanism only when the history supports one specific, pre-declared corrective change; "
        "do not perform local threshold mining around the rejected candidate.",
        f"Editable paths: {', '.join(raw['scope']['editable'])}",
        f"Forbidden paths: {', '.join(raw['scope'].get('forbidden', [])) or '(none)'}",
        "The universe and cached market data are fixed for this task. Do not load or run ETF "
        "discovery, pool-selection, data-refresh, or recommendation skills/workflows.",
        f"Test command: {' '.join(test_command)}",
        f"Development backtest command: {' '.join(development_command)}",
        f"Development metrics path after that command: {development_metrics_path}",
        "The development backtest is silent on success; read the metrics file directly instead "
        "of searching the workspace or treating empty stdout as a failure.",
        comparison_guidance,
        f"Hard gate constraints: {json.dumps(_constraint_descriptions(evaluation['constraints']), ensure_ascii=False)}",
        f"Optional absolute target for stopping the loop: {target if target is not None else '(none)'}",
        "Use only the development period. Do not inspect gate or test periods.",
        "If completed, your final response must be exactly one JSON object with string fields status, previous_feedback, hypothesis, attempts, development_effect, and candidate.",
        "If blocked, return exactly string fields status, previous_feedback, and error instead.",
        "In previous_feedback, distinguish the previous round's observed outcome from possible causes.",
        "In attempts, summarize approaches and variants tried on the development set during this round, including variants not retained in the final candidate.",
        "Attempts does not refer to a gate rejection or to whether the candidate becomes champion.",
        "In development_effect, summarize this round's development-set evidence only; do not mention gate results.",
        "In candidate, unambiguously describe the exact final candidate submitted to the Harness for gate evaluation.",
        "Clearly distinguish that submitted candidate from development variants listed in attempts but not retained.",
        'Set status to either "completed" or "blocked". Do not wrap the JSON in Markdown.',
        "Do not report gate metrics or an acceptance decision.",
    ]
    if task.strategy_name is not None:
        lines.insert(2, f"Configured strategy: {task.strategy_name} ({task.strategy_module})")
    return "\n".join(lines)


def _is_agent_output(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("previous_feedback"), str):
        return False
    if value.get("status") == "blocked":
        return (
            set(value) == {"status", "previous_feedback", "error"}
            and isinstance(value.get("error"), str)
            and bool(value["error"].strip())
        )
    return (
        value.get("status") == "completed"
        and set(value) == {
            "status", "previous_feedback", "hypothesis", "attempts", "development_effect", "candidate",
        }
        and all(
            isinstance(value.get(key), str) and bool(value[key].strip())
            for key in ("hypothesis", "attempts", "development_effect", "candidate")
        )
    )


def _scalar_metrics(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in list(value.items())[:20]
        if isinstance(item, (str, int, float, bool)) or item is None
    }


def _load_research_history(
    experiments: Path,
    *,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build a compact history without exposing exact gate metrics to the research agent."""
    history: list[dict[str, Any]] = []
    if not experiments.exists():
        return history
    for experiment in sorted(path for path in experiments.iterdir() if path.is_dir()):
        result_path = experiment / "result.json"
        decision_path = experiment / "decision.json"
        if not result_path.exists() or not decision_path.exists():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entry: dict[str, Any] = {
            "experiment_id": experiment.name,
            "status": result.get("status"),
            "decision": decision.get("decision"),
            "feedback": result.get("feedback"),
        }
        if run_id is not None:
            entry["run_id"] = run_id
        reasons = decision.get("reasons")
        if isinstance(reasons, list):
            entry["decision_reasons"] = [str(reason) for reason in reasons[:5]]
        if result.get("status") == "completed":
            entry.update({
                "hypothesis": result.get("hypothesis"),
                "attempts": result.get("attempts"),
                "development_effect": result.get("development_effect"),
                "candidate": result.get("candidate"),
                "changed_files": result.get("changes", {}).get("files", []),
                "development_metrics": _scalar_metrics(result.get("metrics", {}).get("development")),
            })
        else:
            entry["error"] = result.get("error")
            for key in ("hypothesis", "attempts", "development_effect", "candidate"):
                if result.get(key) is not None:
                    entry[key] = result.get(key)
        history.append(entry)
    return history[-_RESEARCH_HISTORY_LIMIT:]


def _load_managed_history(manager: ResearchWorkspace) -> list[dict[str, Any]]:
    if manager.run_number is None:
        return _load_research_history(manager.rounds)
    history: list[dict[str, Any]] = []
    legacy = manager.legacy_experiments
    if legacy.exists():
        history.extend(_load_research_history(legacy))
    for run_number in manager.run_numbers():
        if run_number > manager.run_number:
            break
        run = manager.for_run(run_number)
        history.extend(_load_research_history(run.rounds, run_id=run.run_id))
    return history[-_RESEARCH_HISTORY_LIMIT:]


def _fill_previous_feedback(
    manager: ResearchWorkspace,
    research_history: Sequence[Mapping[str, Any]],
    feedback: str,
) -> None:
    if not research_history or not feedback.strip():
        return
    previous_id = research_history[-1].get("experiment_id")
    if not isinstance(previous_id, str):
        return
    previous_run = research_history[-1].get("run_id")
    if (
        manager.run_number is not None
        and isinstance(previous_run, str)
        and previous_run.isdigit()
    ):
        roots = [manager.for_run(int(previous_run)).rounds]
    else:
        roots = [manager.rounds]
    if manager.run_number is not None and not isinstance(previous_run, str):
        roots.extend(
            manager.for_run(number).rounds
            for number in reversed(manager.run_numbers())
            if number < manager.run_number
        )
        roots.append(manager.legacy_experiments)
    result_path: Path | None = None
    result: dict[str, Any] | None = None
    for root in roots:
        candidate = root / previous_id / "result.json"
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        result_path = candidate
        result = value
        break
    if result_path is None or result is None:
        return
    if isinstance(result.get("feedback"), str) and result["feedback"].strip():
        return
    result["feedback"] = feedback.strip()
    write_json_atomic(result_path, result)


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


def _write_failed(
    output_dir: Path,
    experiment_id: str,
    error: str,
    agent_output: Mapping[str, Any] | None = None,
) -> Path:
    result_path = output_dir / "result.json"
    payload = {"experiment_id": experiment_id, "status": "failed", "error": error}
    if agent_output is not None:
        for key in (
            "previous_feedback",
            "hypothesis",
            "attempts",
            "development_effect",
            "candidate",
        ):
            value = agent_output.get(key)
            if isinstance(value, str):
                payload[key] = value
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
    research_history: Sequence[Mapping[str, Any]] = (),
    has_champion: bool | None = None,
    command_runner: CommandRunner = _run_command,
    opencode_runner: AgentRunner = _run_opencode,
    event_sink: EventSink | None = None,
    round_id: str | None = None,
) -> Path:
    task = ResearchTask.load(task_path)
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Workspace does not exist: {root}")
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    raw = task.raw
    fixed = raw["evaluation"]["fixed"]
    development_values = _values(task, fixed["development"], f"{experiment_id}-development", root)
    development_command = _format_command(raw["commands"]["backtest"], development_values)
    development_metrics_path = str(raw["commands"]["metrics_path"]).format_map(development_values)
    test_command = _format_command(raw["commands"]["test"], development_values)
    prompt = _prompt(
        task,
        development_command,
        development_metrics_path,
        test_command,
        research_history,
        task.baseline_mode != "none" if has_champion is None else has_champion,
    )
    opencode = raw["opencode"]
    timeout = int(opencode["timeout_minutes"]) * 60
    opencode_command = [
        "opencode", "run", "--auto", "--format", "json",
        "--model", opencode["model"], "--dir", str(root),
    ]
    if variant := opencode.get("variant"):
        opencode_command.extend(["--variant", variant])

    before = _snapshot(root, out)
    events_path = out / "opencode-events.jsonl"
    event_details = {"round": round_id} if round_id is not None else {}
    _emit(event_sink, "agent_started", message="agent started", **event_details)
    exit_code = opencode_runner(
        opencode_command,
        prompt,
        root,
        events_path,
        timeout,
    )
    if exit_code != 0:
        _emit(event_sink, "agent_failed", message="agent failed", **event_details)
        reason = "OpenCode timed out" if exit_code == 124 else "OpenCode session failed"
        return _write_failed(out, experiment_id, reason)
    agent_output = _parse_opencode_output(events_path)
    if agent_output is None:
        _emit(event_sink, "agent_failed", message="invalid agent output", **event_details)
        return _write_failed(out, experiment_id, "OpenCode produced invalid agent output")
    events_path.unlink(missing_ok=True)
    _emit(event_sink, "agent_completed", message="agent completed", **event_details)
    if agent_output["status"] == "blocked":
        return _write_failed(out, experiment_id, f"OpenCode was blocked: {agent_output['error']}")

    after = _snapshot(root, out)
    changed = _changed_files(before, after)
    generated_dir = Path(str(raw["commands"]["metrics_path"]).format_map(development_values)).parent.as_posix()
    changed = [path for path in changed if not _is_within(path, [generated_dir])]
    editable = raw["scope"]["editable"]
    forbidden = raw["scope"].get("forbidden", [])
    invalid = [path for path in changed if not _is_within(path, editable) or _is_within(path, forbidden)]
    if invalid:
        return _write_failed(
            out,
            experiment_id,
            f"Changes outside editable scope: {', '.join(invalid)}",
            agent_output,
        )
    if not changed:
        return _write_failed(
            out,
            experiment_id,
            "OpenCode completed without code changes",
            agent_output,
        )

    _emit(event_sink, "tests_started", message="tests started", **event_details)
    if _run_with_failure_log(
        command_runner,
        test_command,
        root,
        out / "tests.log",
        timeout,
    ) != 0:
        _emit(event_sink, "tests_failed", message="tests failed", **event_details)
        return _write_failed(out, experiment_id, "Tests failed", agent_output)
    _emit(event_sink, "tests_passed", message="tests passed", **event_details)
    metrics: dict[str, Any] = {}
    for label in ("development", "gate"):
        evaluation_root = root
        if label == "gate":
            if gate_runtime is not None:
                remove_runtime_inputs(root)
                copy_runtime_inputs(Path(gate_runtime), root)
        values = _values(task, fixed[label], f"{experiment_id}-{label}", evaluation_root)
        command = _format_command(raw["commands"]["backtest"], values)
        _emit(
            event_sink,
            f"{label}_started",
            message=f"{label} backtest started",
            **event_details,
        )
        if _run_with_failure_log(
            command_runner,
            command,
            evaluation_root,
            out / f"{label}.log",
            timeout,
        ) != 0:
            _emit(
                event_sink,
                f"{label}_failed",
                message=f"{label} backtest failed",
                **event_details,
            )
            return _write_failed(out, experiment_id, f"{label} backtest failed", agent_output)
        metrics_path = evaluation_root / str(raw["commands"]["metrics_path"]).format_map(values)
        if not metrics_path.exists():
            return _write_failed(out, experiment_id, f"Missing {label} metrics", agent_output)
        try:
            metrics[label] = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _write_failed(out, experiment_id, f"Invalid {label} metrics", agent_output)
        _emit(
            event_sink,
            f"{label}_completed",
            message=f"{label} backtest completed",
            **event_details,
        )

    payload = {
        "experiment_id": experiment_id,
        "status": "completed",
        "previous_feedback": agent_output["previous_feedback"],
        "hypothesis": agent_output["hypothesis"],
        "attempts": agent_output["attempts"],
        "development_effect": agent_output["development_effect"],
        "candidate": agent_output["candidate"],
        "changes": {"files": changed},
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
    copy_runtime_inputs(runtime_source, workspace)
    for label in ("development", "gate"):
        values = _values(
            task,
            raw["evaluation"]["fixed"][label],
            f"{experiment_id}-champion-{label}",
            workspace,
        )
        command = _format_command(raw["commands"]["backtest"], values)
        if _run_with_failure_log(
            command_runner,
            command,
            workspace,
            output_dir / f"champion-{label}.log",
            timeout,
        ) != 0:
            raise RuntimeError(f"champion {label} backtest failed")
        metrics_path = workspace / str(raw["commands"]["metrics_path"]).format_map(values)
        try:
            metrics[label] = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"invalid champion {label} metrics") from exc
    return metrics


def _constraint_passes(value: Any, constraint: Mapping[str, Any]) -> bool:
    if not _is_finite_number(value):
        return False
    operator, threshold = _constraint_rule(constraint)
    if operator == ">=":
        return float(value) >= threshold
    if operator == "abs<=":
        return abs(float(value)) <= threshold
    return float(value) <= threshold


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


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
    if not _is_finite_number(objective):
        return False
    return float(objective) >= float(threshold) and all(
        _constraint_passes(gate.get(name), constraint)
        for name, constraint in evaluation["constraints"].items()
    )


def _decide(
    task: ResearchTask,
    champion: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    evaluation = task.raw["evaluation"]
    objective = str(evaluation["objective"])
    champion_value = champion.get("gate", {}).get(objective) if champion is not None else None
    candidate_value = candidate.get("gate", {}).get(objective)
    champion_constraints_passed = (
        champion is not None
        and all(
            _constraint_passes(champion.get("gate", {}).get(name), constraint)
            for name, constraint in evaluation["constraints"].items()
        )
    )
    champion_objective_is_finite = _is_finite_number(champion_value)
    acceptance = evaluation.get("acceptance", {})
    minimum_improvement = float(acceptance.get("minimum_improvement", 0.0))
    constraints: dict[str, Any] = {}
    constraints_passed = True
    for name, constraint in evaluation["constraints"].items():
        actual = candidate.get("gate", {}).get(name)
        operator, threshold = _constraint_rule(constraint)
        passed = _constraint_passes(actual, constraint)
        constraints[name] = {
            "operator": operator,
            "threshold": threshold,
            "actual": actual,
            "passed": passed,
        }
        constraints_passed = constraints_passed and passed
    candidate_objective_is_finite = _is_finite_number(candidate_value)
    relative_improvement_required = (
        champion is not None
        and champion_constraints_passed
        and champion_objective_is_finite
    )
    objective_passed = candidate_objective_is_finite if not relative_improvement_required else (
        candidate_objective_is_finite
        and float(candidate_value) >= float(champion_value) + minimum_improvement
        and (minimum_improvement > 0 or float(candidate_value) > float(champion_value))
    )
    accepted = constraints_passed and objective_passed
    reasons: list[str] = []
    if not constraints_passed:
        reasons.append("gate constraints failed")
    if not objective_passed and not relative_improvement_required:
        reasons.append("gate objective is not finite")
    elif not objective_passed:
        reasons.append("gate objective did not improve over champion")
    return {
        "decision": "accepted" if accepted else "rejected",
        "objective": {
            "name": objective,
            "champion": champion_value,
            "candidate": candidate_value,
            "minimum_improvement": minimum_improvement,
            "champion_constraints_passed": (
                champion_constraints_passed if champion is not None else None
            ),
            "relative_improvement_required": relative_improvement_required,
        },
        "constraints": constraints,
        "reasons": reasons,
    }


def _metrics_key(task: ResearchTask) -> str:
    relevant = {
        "strategy": task.raw.get("strategy"),
        "data": task.raw["data"],
        "commands": task.raw["commands"],
        "periods": task.raw["evaluation"]["fixed"],
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_managed_once(
    task_path: str | Path,
    round_id: str,
    *,
    run_number: int,
    workspace: str | Path = ".",
    research_root: str | Path = ".research",
    command_runner: CommandRunner = _run_command,
    opencode_runner: AgentRunner = _run_opencode,
    event_sink: EventSink | None = None,
) -> Path:
    """Run one isolated candidate and promote it only when it beats the champion."""
    task_file = Path(task_path).resolve()
    task = ResearchTask.load(task_file)
    source = Path(workspace).resolve()
    managed_root = Path(research_root)
    if not managed_root.is_absolute():
        managed_root = source / managed_root
    development_end = task.raw["evaluation"]["fixed"]["development"]["end"]
    if (
        not round_id.isdigit()
        or int(round_id) < 1
        or round_id != f"{int(round_id):03d}"
    ):
        raise ValueError("round id must be a zero-padded positive number")
    manager = ResearchWorkspace(
        source,
        managed_root,
        task.task_id,
        run_number=run_number,
    )
    candidate, experiment, state = manager.create_candidate(
        round_id,
        date.fromisoformat(development_end),
        task.baseline_mode,
        task.baseline_exclude,
    )
    has_champion = isinstance(state.get("champion_commit"), str)
    research_history = _load_managed_history(manager)
    if source in task_file.parents:
        candidate_task = candidate / task_file.relative_to(source)
        candidate_task.unlink(missing_ok=True)
    execution_id = f"{manager.run_id}-{round_id}"
    result_path = run_once(
        task_file,
        execution_id,
        experiment,
        workspace=candidate,
        gate_runtime=manager.evaluation_runtime,
        research_history=research_history,
        has_champion=has_champion,
        command_runner=command_runner,
        opencode_runner=opencode_runner,
        event_sink=event_sink,
        round_id=round_id,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["experiment_id"] = f"{manager.run_id}/{round_id}"
    result["run_number"] = run_number
    result["round_number"] = int(round_id)
    write_json_atomic(result_path, result)
    previous_feedback = result.pop("previous_feedback", None)
    if isinstance(previous_feedback, str):
        _fill_previous_feedback(manager, research_history, previous_feedback)
        write_json_atomic(result_path, result)
    decision_path = experiment / "decision.json"
    record_id = str(result["experiment_id"])
    if result.get("status") != "completed":
        decision = {
            "experiment_id": result["experiment_id"],
            "decision": "failed",
            "reasons": [result.get("error")],
        }
        write_json_atomic(decision_path, decision)
        manager.reject(candidate, state, record_id)
        return result_path

    manager.write_candidate_patch(
        candidate,
        state,
        task.raw["scope"]["editable"],
        experiment / "candidate.patch",
    )
    metrics_key = _metrics_key(task)
    champion_metrics = (
        state.get("champion_metrics")
        if has_champion and state.get("champion_metrics_key") == metrics_key
        else None
    )
    if has_champion and not isinstance(champion_metrics, dict):
        evaluator = manager.create_champion_evaluator(round_id, state)
        try:
            champion_metrics = _evaluate_existing(
                task,
                evaluator,
                manager.evaluation_runtime,
                round_id,
                experiment,
                command_runner,
            )
        except RuntimeError as exc:
            decision = {
                "experiment_id": result["experiment_id"],
                "decision": "failed",
                "reasons": [str(exc)],
            }
            write_json_atomic(decision_path, decision)
            manager.reject(candidate, state, record_id)
            failed_path = _write_failed(experiment, record_id, str(exc))
            return failed_path
        finally:
            manager.remove_evaluator(evaluator)

    decision = {"experiment_id": result["experiment_id"], **_decide(task, champion_metrics, result["metrics"])}
    write_json_atomic(decision_path, decision)
    if decision["decision"] == "accepted":
        state["champion_metrics_key"] = metrics_key
        manager.promote(
            candidate,
            state,
            record_id,
            result["metrics"],
            task.raw["scope"]["editable"],
        )
    else:
        if has_champion:
            state["champion_metrics"] = champion_metrics
            state["champion_metrics_key"] = metrics_key
        manager.reject(candidate, state, record_id)
    return result_path
