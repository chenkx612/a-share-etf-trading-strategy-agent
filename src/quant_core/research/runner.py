from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from quant_core.research.contracts import ExperimentResult, ResearchTask
from quant_core.research.workspace import (
    ResearchWorkspace,
    copy_runtime_inputs,
    remove_runtime_inputs,
    write_json_atomic,
    workspace_python_env,
)


CommandRunner = Callable[[Sequence[str], Path, Path, int], int]
AgentRunner = Callable[[Sequence[str], str, Path, Path, int], int]
EventSink = Callable[..., None]
_RESEARCH_HISTORY_LIMIT = 12
_ROUND_CLOCK_FILE = ".quant-research-round.json"
_AGENT_CONTAINER_IMAGE = "quant-agent-research:latest"
_CONTAINER_WORKSPACE = "/workspace"


class AgentContainerInfrastructureError(RuntimeError):
    """Raised when the isolated Agent container cannot be started safely."""


@dataclass
class _RoundClock:
    path: Path
    timeout_seconds: int
    event_sink: EventSink | None
    event_details: Mapping[str, Any]
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self.deadline = self.started_at + timedelta(seconds=self.timeout_seconds)
        self._started_monotonic = self.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warnings_emitted: set[int] = set()

    @property
    def deadline_text(self) -> str:
        return self.deadline.isoformat()

    def _remaining_seconds(self) -> int:
        elapsed = max(0.0, self.monotonic() - self._started_monotonic)
        return max(0, int(math.ceil(self.timeout_seconds - elapsed)))

    @staticmethod
    def _phase(remaining: int) -> str:
        if remaining <= 60:
            return "submit_now"
        if remaining <= 5 * 60:
            return "finalize"
        if remaining <= 15 * 60:
            return "converge"
        return "research"

    def _write_status(self) -> None:
        remaining = self._remaining_seconds()
        write_json_atomic(self.path, {
            "schema_version": 1,
            "started_at": self.started_at.isoformat(),
            "deadline": self.deadline_text,
            "timeout_seconds": self.timeout_seconds,
            "remaining_seconds": remaining,
            "phase": self._phase(remaining),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        for threshold in (15, 5, 1):
            if (
                threshold not in self._warnings_emitted
                and self.timeout_seconds > threshold * 60
                and remaining <= threshold * 60
            ):
                self._warnings_emitted.add(threshold)
                _emit(
                    self.event_sink,
                    "round_time_warning",
                    remaining_minutes=threshold,
                    message=f"{threshold} minutes remaining",
                    **self.event_details,
                )

    def _run(self) -> None:
        while not self._stop.is_set():
            self._write_status()
            remaining = self._remaining_seconds()
            if remaining == 0:
                return
            self._stop.wait(min(5.0, float(remaining)))

    def start(self) -> None:
        self._write_status()
        self._thread = threading.Thread(
            target=self._run,
            name="quant-research-round-clock",
            daemon=True,
        )
        self._thread.start()

    def stop(self, finished_monotonic: float | None = None) -> dict[str, Any]:
        finished = self.monotonic() if finished_monotonic is None else finished_monotonic
        duration = max(0.0, finished - self._started_monotonic)
        finished_at = datetime.now(timezone.utc).isoformat()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.path.unlink(missing_ok=True)
        return {
            "started_at": self.started_at.isoformat(),
            "deadline": self.deadline_text,
            "finished_at": finished_at,
            "timeout_seconds": self.timeout_seconds,
            "duration_seconds": duration,
        }


def _emit(event_sink: EventSink | None, event: str, **details: Any) -> None:
    if event_sink is not None:
        event_sink(event, **details)


def _workspace_env(cwd: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    return {**workspace_python_env(cwd), **(extra or {})}


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


def _run_prompt_process(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
    extra_env: Mapping[str, str] | None = None,
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
            env = _workspace_env(cwd, extra_env)
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


def _run_opencode_with_permissions(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
    permissions: Mapping[str, str],
) -> int:
    return _run_prompt_process(
        command,
        prompt,
        cwd,
        log_path,
        timeout,
        {"OPENCODE_PERMISSION": json.dumps(dict(permissions))},
    )


def _container_mount(source: Path, target: str, *, read_only: bool = False) -> list[str]:
    source_text = str(source.resolve())
    if "," in source_text:
        raise ValueError("Docker mount source paths must not contain commas")
    specification = f"type=bind,src={source_text},dst={target}"
    if read_only:
        specification += ",readonly"
    return ["--mount", specification]


def _container_path(path: Path, workspace: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError(f"Agent container path is outside the candidate workspace: {path}") from exc
    return f"{_CONTAINER_WORKSPACE}/{relative.as_posix()}"


def _opencode_runtime_sources() -> tuple[tuple[Path, Path], ...]:
    return (
        (
            Path(
                os.environ.get(
                    "QUANT_OPENCODE_AUTH_FILE",
                    Path.home() / ".local" / "share" / "opencode" / "auth.json",
                )
            ).expanduser(),
            Path(".local/share/opencode/auth.json"),
        ),
        (
            Path(
                os.environ.get(
                    "QUANT_OPENCODE_CONFIG_FILE",
                    Path.home() / ".config" / "opencode" / "opencode.jsonc",
                )
            ).expanduser(),
            Path(".config/opencode/opencode.jsonc"),
        ),
        (
            Path(
                os.environ.get(
                    "QUANT_OPENCODE_MODELS_FILE",
                    Path.home() / ".cache" / "opencode" / "models.json",
                )
            ).expanduser(),
            Path(".cache/opencode/models.json"),
        ),
    )


def _stage_opencode_runtime(runtime_home: Path) -> None:
    for source, relative in _opencode_runtime_sources():
        if not source.is_file():
            continue
        destination = runtime_home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)


def _docker_opencode_command(
    command: Sequence[str],
    cwd: Path,
    permissions: Mapping[str, str],
    read_only_paths: Sequence[Path],
    hidden_mounts: Sequence[tuple[Path, Path]] = (),
    runtime_home: Path | None = None,
    container_name: str | None = None,
) -> list[str]:
    workspace = cwd.resolve()
    translated = [
        _CONTAINER_WORKSPACE if part == str(workspace) else part
        for part in command
    ]
    docker = [
        "docker",
        "run",
        "--init",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--workdir",
        _CONTAINER_WORKSPACE,
        "--env",
        f"OPENCODE_PERMISSION={json.dumps(dict(permissions), separators=(',', ':'))}",
        "--env",
        "PYTHONPATH=/workspace/src",
        *_container_mount(workspace, _CONTAINER_WORKSPACE),
    ]
    if container_name is None:
        docker.append("--rm")
    else:
        docker.extend(["--name", container_name])
    if runtime_home is not None:
        docker.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        docker.extend(_container_mount(runtime_home, "/home/agent"))
    mounted: set[Path] = set()
    for path in read_only_paths:
        resolved = path.resolve()
        if resolved in mounted or not resolved.exists():
            continue
        target = _container_path(resolved, workspace)
        docker.extend(_container_mount(resolved, target, read_only=True))
        mounted.add(resolved)
    for mask, hidden in hidden_mounts:
        docker.extend(
            _container_mount(
                mask,
                _container_path(hidden, workspace),
                read_only=True,
            )
        )
    docker.append(os.environ.get("QUANT_RESEARCH_AGENT_IMAGE", _AGENT_CONTAINER_IMAGE))
    docker.extend(translated)
    return docker


def _remove_agent_container(name: str) -> bool:
    try:
        completed = subprocess.run(
            ["docker", "rm", "--force", name],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (
        completed.returncode == 0
        or "no such container" in completed.stdout.casefold()
    )


def _bind_sources_exist(command: Sequence[str]) -> bool:
    sources: list[Path] = []
    for part in command:
        if not part.startswith("type=bind,src="):
            continue
        source = part.split("src=", 1)[1].split(",dst=", 1)[0]
        sources.append(Path(source))
    return bool(sources) and all(source.exists() for source in sources)


def _is_retryable_bind_source_failure(
    exit_code: int,
    log_path: Path,
    command: Sequence[str],
) -> bool:
    if exit_code == 0 or not _bind_sources_exist(command):
        return False
    try:
        detail = log_path.read_text(encoding="utf-8").casefold()
    except OSError:
        return False
    return "bind source path does not exist" in detail


def _run_opencode_container(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
    *,
    read_only_paths: Sequence[Path] = (),
) -> int:
    permissions = {
        "external_directory": "deny",
        "question": "deny",
    }
    try:
        if not cwd.is_dir():
            log_path.write_text(
                f"Agent candidate workspace does not exist: {cwd}",
                encoding="utf-8",
            )
            return 127
        container_input = prompt
        container_command_parts = list(command)
        if container_command_parts[:2] == ["opencode", "run"]:
            # Current OpenCode requires the message as a positional argument.
            container_command_parts.append(prompt)
            container_input = ""
        hidden = cwd / ".research"
        with tempfile.TemporaryDirectory(
            prefix=".agent-hidden-",
            dir=log_path.parent,
        ) as temporary:
            temporary_root = Path(temporary)
            runtime_home = temporary_root / "home"
            (runtime_home / ".local" / "share" / "opencode").mkdir(parents=True)
            (runtime_home / ".config" / "opencode").mkdir(parents=True)
            _stage_opencode_runtime(runtime_home)
            mask = temporary_root / "mask"
            mask.mkdir()
            hidden_mounts = (
                [(mask, hidden)]
                if hidden.is_dir()
                else []
            )
            for attempt in range(2):
                container_name = f"quant-agent-{uuid.uuid4().hex}"
                container_command = _docker_opencode_command(
                    container_command_parts,
                    cwd,
                    permissions,
                    read_only_paths,
                    hidden_mounts,
                    runtime_home,
                    container_name,
                )
                try:
                    exit_code = _run_prompt_process(
                        container_command,
                        container_input,
                        cwd,
                        log_path,
                        timeout,
                    )
                finally:
                    removed = _remove_agent_container(container_name)
                if not removed:
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write("\nFailed to remove Agent container")
                    return 127
                if attempt == 0 and _is_retryable_bind_source_failure(
                    exit_code,
                    log_path,
                    container_command,
                ):
                    time.sleep(0.25)
                    continue
                return exit_code
            raise AssertionError("unreachable")
    except (OSError, ValueError) as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return 127


def _container_infrastructure_error(log_path: Path) -> str | None:
    try:
        detail = log_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    markers = (
        "docker: Error response from daemon",
        "Cannot connect to the Docker daemon",
        "failed to connect to the docker API",
        "invalid mount config for type",
        "OCI runtime create failed",
        "Unable to find image",
        "pull access denied",
        "No such image",
        "Failed to remove Agent container",
    )
    folded = detail.casefold()
    if not any(marker.casefold() in folded for marker in markers):
        return None
    compact = " ".join(detail.split())
    return compact[:2000]


def preflight_agent_container(
    task: ResearchTask,
    research_root: Path,
) -> None:
    """Verify the real Agent mount topology before allocating a Loop Run."""
    preflight_root = research_root / task.task_id / ".tmp" / "container-preflight"
    preflight_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe-", dir=preflight_root) as temporary:
        root = Path(temporary)
        candidate = root / "workspace"
        candidate.mkdir()
        development = candidate / "data"
        development.mkdir()
        input_path = development / "input.txt"
        input_path.write_text("development", encoding="utf-8")
        hidden = candidate / ".research"
        hidden.mkdir()
        (hidden / "sentinel.json").write_text("hidden", encoding="utf-8")
        writable = candidate / "candidate.txt"
        expected_runtime_files = [
            f"/home/agent/{relative.as_posix()}"
            for source, relative in _opencode_runtime_sources()
            if source.is_file()
        ]
        configured_model = str(task.raw["opencode"]["model"])
        provider = configured_model.partition("/")[0]
        script = "\n".join([
            "import subprocess",
            "from pathlib import Path",
            "workspace = Path('/workspace')",
            "(workspace / 'candidate.txt').write_text('ok', encoding='utf-8')",
            "data = workspace / 'data/input.txt'",
            "assert data.read_text(encoding='utf-8') == 'development'",
            "try:",
            "    data.write_text('changed', encoding='utf-8')",
            "except OSError:",
            "    pass",
            "else:",
            "    raise AssertionError('development input was writable')",
            "assert not (workspace / '.research/sentinel.json').exists()",
            *[
                f"assert Path({path!r}).is_file()"
                for path in expected_runtime_files
            ],
            (
                "models = subprocess.run("
                f"['opencode', 'models', {provider!r}], "
                "text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, "
                "check=False, timeout=30)"
            ),
            "assert models.returncode == 0, models.stdout",
            f"assert {configured_model!r} in models.stdout.splitlines(), models.stdout",
        ])
        log_path = root / "preflight.log"
        exit_code = _run_opencode_container(
            ["python3", "-c", script],
            "",
            candidate,
            log_path,
            60,
            read_only_paths=[development],
        )
        if exit_code != 0:
            detail = _container_infrastructure_error(log_path)
            if detail is None:
                try:
                    detail = " ".join(log_path.read_text(encoding="utf-8").split())[:2000]
                except OSError:
                    detail = f"container exited with code {exit_code}"
            raise AgentContainerInfrastructureError(
                f"Agent container preflight failed: {detail}"
            )
        if not writable.is_file():
            raise AgentContainerInfrastructureError(
                "Agent container preflight did not persist its workspace write"
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


def _containerize_prompt_command(command: Sequence[str], workspace: Path) -> list[str]:
    root = str(workspace.resolve())
    translated: list[str] = []
    for part in command:
        if part == sys.executable:
            translated.append("python3")
        elif part == root:
            translated.append(_CONTAINER_WORKSPACE)
        elif part.startswith(root + os.sep):
            relative = Path(part).resolve().relative_to(workspace.resolve())
            translated.append(f"{_CONTAINER_WORKSPACE}/{relative.as_posix()}")
        else:
            translated.append(part)
    return translated


def _agent_read_only_paths(
    workspace: Path,
    forbidden: Sequence[str],
    generated_dir: str,
) -> list[Path]:
    candidates = ["data", "outputs/factors", *forbidden]
    paths: list[Path] = []
    for candidate in candidates:
        normalized = candidate.rstrip("/")
        if (
            not normalized
            or normalized == ".research"
            or normalized.startswith(".research/")
            or _is_within(generated_dir, [normalized])
        ):
            continue
        path = workspace / normalized
        if path.exists():
            paths.append(path)
    return paths


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


def _evaluation_command(
    task: ResearchTask, task_path: str | Path, label: str, values: Mapping[str, str],
    walk_forward_config: Path | None = None,
) -> list[str]:
    if task.evaluation_mode == "fixed":
        return _format_command(task.raw["commands"]["backtest"], values)
    command = [
        sys.executable, "-m", "quant_core.research.evaluator",
        "--root", values["workspace"], "--universe", values["universe"],
        "--start", values["start"], "--end", values["end"], "--run-id", values["run_id"],
        "--candidate-module", str(task.strategy_module),
    ]
    metrics_path = str(task.raw["commands"]["metrics_path"]).format_map(values)
    command.extend(["--metrics-path", metrics_path])
    if walk_forward_config is not None:
        command.extend(["--walk-forward-config", str(walk_forward_config)])
    else:
        command.extend(["--task", str(Path(task_path).resolve()), "--stage", label])
    return command


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
    round_deadline: str,
    round_clock_path: str,
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
        "Start with one primary falsifiable hypothesis and keep the search focused. Implementation "
        "attempt and parameter counts are heuristic guidance, not hard quotas.",
        "Avoid broad parameter sweeps and record meaningful hypothesis revisions honestly instead "
        "of retroactively describing a new signal family as the original mechanism.",
        "Stop development search as soon as a candidate passes the stated constraints and reaches "
        "the configured objective improvement; additional optimization is out of scope.",
        f"Candidate research deadline (UTC): {round_deadline}. This is a Harness-enforced hard stop.",
        f"Live Round clock: {round_clock_path}. Read it before evaluations and during finalization; "
        "remaining_seconds and phase are refreshed by the Harness.",
        "When the clock phase becomes converge, stop expanding the search. In finalize, preserve "
        "the best candidate, run focused tests, and prepare the required JSON. In submit_now, "
        "return immediately. Harness-owned validation after submission is outside this deadline.",
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


def _normalize_development_metrics(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    aggregate = value.get("aggregate")
    if not isinstance(aggregate, Mapping):
        return _scalar_metrics(value)
    metrics = _scalar_metrics(aggregate)
    no_feasible_folds = value.get("no_feasible_parameter_folds")
    if isinstance(no_feasible_folds, int) and not isinstance(no_feasible_folds, bool):
        metrics["no_feasible_parameter_folds"] = no_feasible_folds
    return metrics


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
                "development_metrics": _normalize_development_metrics(
                    result.get("metrics", {}).get("development")
                ),
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
    round_timing: Mapping[str, Any] | None = None,
    failure_kind: str | None = None,
) -> Path:
    result_path = output_dir / "result.json"
    payload = {"experiment_id": experiment_id, "status": "failed", "error": error}
    if round_timing is not None:
        payload["round_timing"] = dict(round_timing)
    if failure_kind is not None:
        payload["failure_kind"] = failure_kind
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
    opencode_runner: AgentRunner | None = None,
    event_sink: EventSink | None = None,
    round_id: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Path:
    task = ResearchTask.load(task_path)
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Workspace does not exist: {root}")
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    raw = task.raw
    fixed = task.evaluation_periods
    development_values = _values(task, fixed["development"], f"{experiment_id}-development", root)
    development_config: Path | None = None
    if task.evaluation_mode == "walk_forward":
        development_config = root / ".quant-research-development.json"
        write_json_atomic(development_config, {
            "period": dict(task.development_period),
            "walk_forward": {
                key: task.evaluation_periods[key]
                for key in (
                    "train_months",
                    "validation_months",
                    "step_months",
                    "max_parameter_sets",
                )
            },
            "constraints": task.raw["evaluation"]["constraints"], "objective": task.raw["evaluation"]["objective"],
        })
    development_command = _evaluation_command(task, task_path, "development", development_values, development_config)
    development_metrics_path = str(raw["commands"]["metrics_path"]).format_map(development_values)
    test_command = _format_command(raw["commands"]["test"], development_values)
    opencode = raw["opencode"]
    command_timeout = int(opencode["timeout_minutes"]) * 60
    round_timeout = int(raw["budget"].get("round_minutes", opencode["timeout_minutes"])) * 60
    before = _snapshot(root, out)
    event_details = {"round": round_id} if round_id is not None else {}
    round_clock = _RoundClock(
        root / _ROUND_CLOCK_FILE,
        round_timeout,
        event_sink,
        event_details,
        monotonic,
    )
    agent_development_command = _containerize_prompt_command(development_command, root)
    agent_metrics_path = _containerize_prompt_command([development_metrics_path], root)[0]
    agent_test_command = _containerize_prompt_command(test_command, root)
    prompt = _prompt(
        task,
        agent_development_command,
        agent_metrics_path,
        agent_test_command,
        research_history,
        task.baseline_mode != "none" if has_champion is None else has_champion,
        round_clock.deadline_text,
        _ROUND_CLOCK_FILE,
    )
    opencode_command = [
        "opencode", "run", "--auto", "--format", "json",
        "--model", opencode["model"], "--dir", str(root),
    ]
    if variant := opencode.get("variant"):
        opencode_command.extend(["--variant", variant])

    events_path = out / "opencode-events.jsonl"
    _emit(
        event_sink,
        "agent_started",
        message="agent started",
        deadline=round_clock.deadline_text,
        timeout_seconds=round_timeout,
        **event_details,
    )
    round_clock.start()
    try:
        if opencode_runner is None:
            generated_dir = Path(development_metrics_path).parent.as_posix()
            exit_code = _run_opencode_container(
                opencode_command,
                prompt,
                root,
                events_path,
                round_timeout,
                read_only_paths=_agent_read_only_paths(
                    root,
                    raw["scope"].get("forbidden", []),
                    generated_dir,
                ),
            )
        else:
            exit_code = opencode_runner(
                opencode_command,
                prompt,
                root,
                events_path,
                round_timeout,
            )
    finally:
        finished_monotonic = monotonic()
        round_timing = round_clock.stop(finished_monotonic)
    deadline_exceeded = (
        exit_code == 124
        or float(round_timing["duration_seconds"]) >= round_timeout
    )
    if exit_code != 0 or deadline_exceeded:
        if deadline_exceeded:
            _emit(
                event_sink,
                "round_deadline_exceeded",
                message="candidate research deadline exceeded",
                **event_details,
            )
            reason = "Candidate research deadline exceeded"
            failure_kind = None
        else:
            _emit(event_sink, "agent_failed", message="agent failed", **event_details)
            infrastructure_error = (
                _container_infrastructure_error(events_path)
                if opencode_runner is None
                else None
            )
            if infrastructure_error is None:
                reason = "OpenCode session failed"
                failure_kind = None
            else:
                reason = f"Agent container infrastructure failure: {infrastructure_error}"
                failure_kind = "infrastructure"
        return _write_failed(
            out,
            experiment_id,
            reason,
            round_timing=round_timing,
            failure_kind=failure_kind,
        )
    agent_output = _parse_opencode_output(events_path)
    if agent_output is None:
        _emit(event_sink, "agent_failed", message="invalid agent output", **event_details)
        return _write_failed(
            out,
            experiment_id,
            "OpenCode produced invalid agent output",
            round_timing=round_timing,
        )
    events_path.unlink(missing_ok=True)
    _emit(event_sink, "agent_completed", message="agent completed", **event_details)
    if agent_output["status"] == "blocked":
        return _write_failed(
            out,
            experiment_id,
            f"OpenCode was blocked: {agent_output['error']}",
            round_timing=round_timing,
        )

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
            round_timing,
        )
    if not changed:
        return _write_failed(
            out,
            experiment_id,
            "OpenCode completed without code changes",
            agent_output,
            round_timing,
        )

    _emit(event_sink, "tests_started", message="tests started", **event_details)
    if _run_with_failure_log(
        command_runner,
        test_command,
        root,
        out / "tests.log",
        command_timeout,
    ) != 0:
        _emit(event_sink, "tests_failed", message="tests failed", **event_details)
        return _write_failed(
            out,
            experiment_id,
            "Tests failed",
            agent_output,
            round_timing,
        )
    _emit(event_sink, "tests_passed", message="tests passed", **event_details)
    metrics: dict[str, Any] = {}
    for label in ("development", "gate"):
        evaluation_root = root
        if label == "gate":
            if gate_runtime is not None:
                remove_runtime_inputs(root)
                copy_runtime_inputs(Path(gate_runtime), root)
        values = _values(task, fixed[label], f"{experiment_id}-{label}", evaluation_root)
        command = (
            development_command
            if label == "development"
            else _evaluation_command(task, task_path, label, values)
        )
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
            command_timeout,
        ) != 0:
            _emit(
                event_sink,
                f"{label}_failed",
                message=f"{label} backtest failed",
                **event_details,
            )
            return _write_failed(
                out,
                experiment_id,
                f"{label} backtest failed",
                agent_output,
                round_timing,
            )
        metrics_path = evaluation_root / str(raw["commands"]["metrics_path"]).format_map(values)
        if not metrics_path.exists():
            return _write_failed(
                out,
                experiment_id,
                f"Missing {label} metrics",
                agent_output,
                round_timing,
            )
        try:
            metrics[label] = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _write_failed(
                out,
                experiment_id,
                f"Invalid {label} metrics",
                agent_output,
                round_timing,
            )
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
        "round_timing": round_timing,
    }
    ExperimentResult.from_mapping(payload)
    result_path = out / "result.json"
    write_json_atomic(result_path, payload)
    return result_path


def _evaluate_existing(
    task: ResearchTask,
    task_path: str | Path,
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
            task.evaluation_periods[label],
            f"{experiment_id}-champion-{label}",
            workspace,
        )
        command = _evaluation_command(task, task_path, label, values)
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


def _walk_forward_gate_is_feasible(
    task: ResearchTask,
    metrics: Mapping[str, Any] | None,
) -> bool:
    if task.evaluation_mode != "walk_forward":
        return True
    if not isinstance(metrics, Mapping):
        return False
    gate = metrics.get("gate")
    if not isinstance(gate, Mapping):
        return False
    no_feasible_folds = gate.get("no_feasible_parameter_folds")
    return (
        isinstance(no_feasible_folds, int)
        and not isinstance(no_feasible_folds, bool)
        and no_feasible_folds == 0
    )


def target_reached(task: ResearchTask, metrics: Mapping[str, Any] | None) -> bool:
    target = task.raw["evaluation"].get("target")
    if not isinstance(target, dict) or not isinstance(metrics, Mapping):
        return False
    if not _walk_forward_gate_is_feasible(task, metrics):
        return False
    gate = metrics.get("gate")
    if task.evaluation_mode == "walk_forward" and isinstance(gate, Mapping):
        gate = gate.get("aggregate")
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
    def gate_metrics(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        gate = value.get("gate", {})
        if task.evaluation_mode == "walk_forward" and isinstance(gate, Mapping):
            gate = gate.get("aggregate", {})
        return gate if isinstance(gate, Mapping) else {}
    champion_gate, candidate_gate = gate_metrics(champion), gate_metrics(candidate)
    champion_gate_is_feasible = _walk_forward_gate_is_feasible(task, champion)
    candidate_gate_is_feasible = _walk_forward_gate_is_feasible(task, candidate)
    champion_value = champion_gate.get(objective) if champion is not None else None
    candidate_value = candidate_gate.get(objective)
    champion_constraints_passed = (
        champion is not None
        and champion_gate_is_feasible
        and all(
            _constraint_passes(champion_gate.get(name), constraint)
            for name, constraint in evaluation["constraints"].items()
        )
    )
    champion_objective_is_finite = _is_finite_number(champion_value)
    acceptance = evaluation.get("acceptance", {})
    minimum_improvement = float(acceptance.get("minimum_improvement", 0.0))
    constraints: dict[str, Any] = {}
    constraints_passed = True
    for name, constraint in evaluation["constraints"].items():
        actual = candidate_gate.get(name)
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
    accepted = candidate_gate_is_feasible and constraints_passed and objective_passed
    reasons: list[str] = []
    if not candidate_gate_is_feasible:
        reasons.append("gate has folds with no feasible parameters")
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
        "periods": task.evaluation_periods,
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
    opencode_runner: AgentRunner | None = None,
    event_sink: EventSink | None = None,
) -> Path:
    """Run one isolated candidate and promote it only when it beats the champion."""
    task_file = Path(task_path).resolve()
    task = ResearchTask.load(task_file)
    source = Path(workspace).resolve()
    managed_root = Path(research_root)
    if not managed_root.is_absolute():
        managed_root = source / managed_root
    development_end = task.development_period["end"]
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
        task.strategy_path,
    )
    has_champion = isinstance(state.get("champion_sha256"), str)
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
        if result.get("failure_kind") == "infrastructure":
            decision["failure_kind"] = "infrastructure"
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
                task_file,
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
