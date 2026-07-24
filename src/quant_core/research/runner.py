from __future__ import annotations

import fcntl
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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from quant_core.research.contracts import ExperimentResult, ResearchTask
from quant_core.research.environment import (
    EvaluationEnvironment,
    capture_evaluation_environment,
)
from quant_core.research.workspace import (
    ResearchWorkspace,
    copy_runtime_inputs,
    evaluator_contract_sha256_for_commit,
    remove_runtime_inputs,
    write_json_atomic,
    workspace_python_env,
)


CommandRunner = Callable[[Sequence[str], Path, Path, int], int]
AgentRunner = Callable[[Sequence[str], str, Path, Path, int], int]
EventSink = Callable[..., None]
_RESEARCH_HISTORY_LIMIT = 12
_ROUND_CLOCK_FILE = ".quant-research-round.json"
_DEVELOPMENT_FINALIZATION_RESERVE_SECONDS = 300
_DEVELOPMENT_ESTIMATE_SAFETY_FACTOR = 1.25
_AGENT_CONTAINER_IMAGE = "quant-agent-research:latest"
_CONTAINER_WORKSPACE = "/workspace"
_NO_TOOL_PERMISSIONS = {
    "external_directory": "deny",
    "question": "deny",
    "bash": "deny",
    "edit": "deny",
    "task": "deny",
    "skill": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "todowrite": "deny",
}


class AgentContainerInfrastructureError(RuntimeError):
    """Raised when the isolated Agent container cannot be started safely."""


class CandidateBindPreflightError(AgentContainerInfrastructureError):
    """Raised before a Round is allocated when Docker cannot see its worktree."""

    def __init__(self, message: str, code: str, evidence_path: Path) -> None:
        super().__init__(message)
        self.code = code
        self.evidence_path = evidence_path


def _development_finalization_reserve(round_timeout: int) -> int:
    if round_timeout < 1:
        raise ValueError("Round timeout must be positive")
    return min(
        _DEVELOPMENT_FINALIZATION_RESERVE_SECONDS,
        max(1, round_timeout // 4),
    )


@dataclass(frozen=True)
class InfrastructureFailure:
    code: str
    message: str


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

    @property
    def deadline_monotonic(self) -> float:
        return self._started_monotonic + self.timeout_seconds

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


_OAUTH_ACCESS_FIELDS = ("access", "access_token")
_OAUTH_REFRESH_FIELDS = ("refresh", "refresh_token")
_OAUTH_EXPIRY_FIELDS = ("expires", "expiry", "expires_at")
_OAUTH_MUTABLE_FIELDS = frozenset(
    (*_OAUTH_ACCESS_FIELDS, *_OAUTH_REFRESH_FIELDS, *_OAUTH_EXPIRY_FIELDS)
)
_AUTH_RECOVERY_TIMEOUT = 60


def _read_auth_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("OpenCode authentication file must contain a JSON object")
    return payload


def _oauth_provider_auth(
    payload: Mapping[str, Any],
    provider: str | None,
) -> dict[str, Any] | None:
    if provider is None:
        return None
    value = payload.get(provider)
    if not isinstance(value, dict) or value.get("type") != "oauth":
        return None
    if not any(field in value for field in _OAUTH_REFRESH_FIELDS):
        return None
    return dict(value)


def _validated_rotated_provider_auth(
    original: Mapping[str, Any],
    candidate: Any,
) -> dict[str, Any]:
    if not isinstance(candidate, dict) or candidate.get("type") != original.get("type"):
        raise ValueError("OpenCode OAuth credential type changed")
    unexpected = set(candidate) - set(original) - _OAUTH_MUTABLE_FIELDS
    if unexpected:
        raise ValueError("OpenCode OAuth credential contains unexpected fields")
    for key, value in original.items():
        if key not in _OAUTH_MUTABLE_FIELDS and candidate.get(key) != value:
            raise ValueError("OpenCode OAuth credential identity fields changed")
    for fields, label in (
        (_OAUTH_ACCESS_FIELDS, "access token"),
        (_OAUTH_REFRESH_FIELDS, "refresh token"),
    ):
        present = [field for field in fields if field in original or field in candidate]
        if not present or not any(
            isinstance(candidate.get(field), str) and bool(candidate[field])
            for field in present
        ):
            raise ValueError(f"OpenCode OAuth credential is missing its {label}")
    for field in _OAUTH_EXPIRY_FIELDS:
        if field not in candidate:
            continue
        value = candidate[field]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ValueError("OpenCode OAuth credential has an invalid expiry")
    return dict(candidate)


def _write_auth_payload_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _authentication_probe_command(
    command: Sequence[str],
    workspace: Path,
) -> list[str]:
    try:
        model = command[command.index("--model") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("OpenCode OAuth session is missing its configured model") from exc
    probe = [
        "opencode", "run", "--pure", "--format", "json",
        "--model", model, "--dir", str(workspace),
    ]
    try:
        variant = command[command.index("--variant") + 1]
    except (ValueError, IndexError):
        pass
    else:
        probe.extend(["--variant", variant])
    return probe


def _recover_rotated_oauth(
    host_auth: Path | None,
    original_auth: bytes | None,
    runtime_home: Path,
    provider: str | None,
    command: Sequence[str],
    temporary_root: Path,
) -> tuple[InfrastructureFailure | None, list[Any]]:
    snapshots: list[Any] = []
    if host_auth is None or original_auth is None or provider is None:
        return None, snapshots
    try:
        original_payload = json.loads(original_auth)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, snapshots
    if not isinstance(original_payload, dict):
        return None, snapshots
    snapshots.append(original_payload)
    original_provider = _oauth_provider_auth(original_payload, provider)
    if original_provider is None:
        return None, snapshots
    runtime_auth = runtime_home / ".local/share/opencode/auth.json"
    try:
        runtime_payload = _read_auth_payload(runtime_auth)
        snapshots.append(runtime_payload)
        rotated_provider = _validated_rotated_provider_auth(
            original_provider,
            runtime_payload.get(provider),
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return InfrastructureFailure(
            "provider_authentication_state",
            "Provider authentication state produced by OpenCode is invalid; re-authenticate before retrying",
        ), snapshots
    if rotated_provider == original_provider:
        return None, snapshots

    validation_home = temporary_root / "validation-home"
    validation_workspace = temporary_root / "validation-workspace"
    validation_home.mkdir(mode=0o700)
    validation_workspace.mkdir()
    _stage_opencode_runtime(validation_home)
    validation_auth = validation_home / ".local/share/opencode/auth.json"
    try:
        validation_payload = _read_auth_payload(validation_auth)
    except (OSError, ValueError, json.JSONDecodeError):
        validation_payload = dict(original_payload)
    validation_payload[provider] = rotated_provider
    _write_auth_payload_atomic(validation_auth, validation_payload)
    validation_log = temporary_root / "credential-validation.jsonl"
    probe_command = _authentication_probe_command(command, validation_workspace)
    probe_exit = _run_prompt_process(
        probe_command,
        "Reply exactly OK. Do not use tools.",
        validation_workspace,
        validation_log,
        _AUTH_RECOVERY_TIMEOUT,
        {
            "HOME": str(validation_home),
            "OPENCODE_PERMISSION": json.dumps(dict(_NO_TOOL_PERMISSIONS)),
        },
    )
    try:
        verified_payload = _read_auth_payload(validation_auth)
        snapshots.append(verified_payload)
        verified_provider = _validated_rotated_provider_auth(
            original_provider,
            verified_payload.get(provider),
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return InfrastructureFailure(
            "provider_authentication_state",
            "Provider authentication validation produced invalid state; re-authenticate before retrying",
        ), snapshots
    probe_failure = (
        InfrastructureFailure(
            "provider_authentication",
            "Provider authentication failed during rotated credential validation; re-authenticate before retrying",
        )
        if probe_exit != 0
        else None
    )
    if probe_failure is not None and verified_provider == rotated_provider:
        return probe_failure, snapshots

    try:
        current_payload = _read_auth_payload(host_auth)
    except (OSError, ValueError, json.JSONDecodeError):
        return InfrastructureFailure(
            "provider_authentication_state",
            "Provider authentication file changed or became invalid during the session",
        ), snapshots
    if current_payload.get(provider) != original_payload.get(provider):
        return InfrastructureFailure(
            "provider_authentication_state",
            "Provider authentication state changed concurrently; refusing to overwrite it",
        ), snapshots
    current_payload[provider] = verified_provider
    try:
        _write_auth_payload_atomic(host_auth, current_payload)
    except OSError:
        return InfrastructureFailure(
            "provider_authentication_state",
            "Provider authentication state could not be persisted safely",
        ), snapshots
    snapshots.append(current_payload)
    return probe_failure, snapshots


def _configured_provider(command: Sequence[str]) -> str | None:
    try:
        model = command[command.index("--model") + 1]
    except (ValueError, IndexError):
        return None
    provider, separator, _ = model.partition("/")
    return provider if separator and provider else None


@contextmanager
def _opencode_auth_lock(provider: str | None, timeout: float):
    auth_path = _opencode_runtime_sources()[0][0]
    if provider is None or not auth_path.is_file():
        yield None
        return
    lock_path = auth_path.with_name(f"{auth_path.name}.quant-research.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_path.chmod(0o600)
        started = time.monotonic()
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started >= timeout:
                    raise ValueError("OpenCode credential lock timed out")
                time.sleep(0.1)
        try:
            yield auth_path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _docker_opencode_command(
    command: Sequence[str],
    cwd: Path,
    permissions: Mapping[str, str],
    read_only_paths: Sequence[Path],
    hidden_mounts: Sequence[tuple[Path, Path]] = (),
    runtime_home: Path | None = None,
    container_name: str | None = None,
    bash_timeout_ms: int | None = None,
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
    ]
    if bash_timeout_ms is not None:
        if bash_timeout_ms < 1:
            raise ValueError("OpenCode bash timeout must be positive")
        docker.extend([
            "--env",
            f"OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS={bash_timeout_ms}",
        ])
    docker.extend(_container_mount(workspace, _CONTAINER_WORKSPACE))
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


def _bind_source_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        return {
            "path": str(resolved),
            "exists": False,
            "error": str(exc),
        }
    return {
        "path": str(resolved),
        "exists": True,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "mode": stat.st_mode,
    }


def probe_candidate_bind_source(
    candidate: Path,
    evidence_path: Path,
    *,
    event_sink: EventSink | None = None,
    round_id: str | None = None,
    timeout: float = 5.0,
    process_runner: AgentRunner | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Path:
    """Prove Docker can read the exact candidate path without starting the Agent."""
    if timeout <= 0:
        raise ValueError("candidate bind probe timeout must be positive")
    candidate = candidate.resolve()
    evidence_path.mkdir(parents=True, exist_ok=False)
    runner = process_runner or _run_prompt_process
    started = monotonic()
    delays = (0.0, 0.25, 0.5, 1.0, 2.0)
    attempts: list[dict[str, Any]] = []
    event_details = {"round": round_id} if round_id is not None else {}
    _emit(
        event_sink,
        "bind_probe_started",
        message="checking candidate Docker bind",
        timeout_seconds=timeout,
        **event_details,
    )
    initial_identity = _bind_source_identity(candidate)
    failure_code = "candidate_bind_unavailable"
    failure_message = "Docker could not bind the candidate workspace"

    for attempt_number, delay in enumerate(delays, start=1):
        elapsed = max(0.0, monotonic() - started)
        remaining = timeout - elapsed
        if remaining <= 0:
            break
        if delay:
            wait = min(delay, remaining)
            sleeper(wait)
            remaining = timeout - max(0.0, monotonic() - started)
            if remaining <= 0:
                break

        before = _bind_source_identity(candidate)
        if not before.get("exists"):
            failure_code = "candidate_bind_source_missing"
            failure_message = "Candidate bind source disappeared on the host"
            attempts.append({
                "attempt": attempt_number,
                "delay_seconds": delay,
                "host_before": before,
                "exit_code": None,
                "retryable": False,
            })
            break
        if before != initial_identity:
            failure_code = "candidate_bind_source_changed"
            failure_message = "Candidate bind source identity changed before Docker could mount it"
            attempts.append({
                "attempt": attempt_number,
                "delay_seconds": delay,
                "host_before": before,
                "exit_code": None,
                "retryable": False,
            })
            break

        container_name = f"quant-bind-probe-{uuid.uuid4().hex}"
        log_path = evidence_path / f"attempt-{attempt_number:03d}.log"
        command = [
            "docker", "run", "--rm", "--name", container_name,
            "--mount", (
                f"type=bind,src={candidate},dst={_CONTAINER_WORKSPACE},readonly"
            ),
            os.environ.get("QUANT_RESEARCH_AGENT_IMAGE", _AGENT_CONTAINER_IMAGE),
            "python3", "-c",
            (
                "from pathlib import Path; "
                "assert Path('/workspace/.git').exists(), "
                "'candidate worktree marker is not visible'"
            ),
        ]
        exit_code = runner(
            command,
            "",
            candidate,
            log_path,
            max(0.1, remaining),
        )
        removed = _remove_agent_container(container_name)
        after = _bind_source_identity(candidate)
        try:
            detail = log_path.read_text(encoding="utf-8")
        except OSError:
            detail = ""
        retryable = (
            exit_code != 0
            and removed
            and after == initial_identity
            and "bind source path does not exist" in detail.casefold()
        )
        attempt = {
            "attempt": attempt_number,
            "delay_seconds": delay,
            "container_name": container_name,
            "exit_code": exit_code,
            "container_removed": removed,
            "host_before": before,
            "host_after": after,
            "retryable": retryable,
            "log": log_path.name if detail else None,
        }
        attempts.append(attempt)
        _emit(
            event_sink,
            "bind_probe_attempt",
            message=f"candidate bind probe attempt {attempt_number}",
            attempt=attempt_number,
            exit_code=exit_code,
            retryable=retryable,
            **event_details,
        )
        if exit_code == 0 and removed and after == initial_identity:
            if not detail:
                log_path.unlink(missing_ok=True)
            summary = evidence_path / "summary.json"
            write_json_atomic(summary, {
                "status": "passed",
                "timeout_seconds": timeout,
                "candidate": str(candidate),
                "initial_host_identity": initial_identity,
                "attempts": attempts,
            })
            _emit(
                event_sink,
                "bind_probe_succeeded",
                message="candidate Docker bind is visible",
                attempts=len(attempts),
                **event_details,
            )
            return summary
        if not removed:
            failure_code = "candidate_bind_probe_cleanup_failed"
            failure_message = "Candidate bind probe container could not be removed safely"
            break
        if after.get("exists") is not True:
            failure_code = "candidate_bind_source_missing"
            failure_message = "Candidate bind source disappeared on the host"
            break
        if after != initial_identity:
            failure_code = "candidate_bind_source_changed"
            failure_message = "Candidate bind source identity changed during Docker probing"
            break
        if not retryable:
            failure_code = "candidate_bind_probe_failed"
            compact = " ".join(detail.split())
            failure_message = compact[:2000] or f"Docker bind probe exited with code {exit_code}"
            break

    summary = evidence_path / "summary.json"
    write_json_atomic(summary, {
        "status": "failed",
        "failure_code": failure_code,
        "message": failure_message,
        "timeout_seconds": timeout,
        "candidate": str(candidate),
        "initial_host_identity": initial_identity,
        "attempts": attempts,
    })
    _emit(
        event_sink,
        "bind_probe_failed",
        message="candidate Docker bind is unavailable",
        attempts=len(attempts),
        failure_code=failure_code,
        **event_details,
    )
    raise CandidateBindPreflightError(
        failure_message,
        failure_code,
        summary,
    )


def _run_opencode_container(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
    *,
    read_only_paths: Sequence[Path] = (),
    provider: str | None = None,
    permissions: Mapping[str, str] | None = None,
) -> int:
    effective_permissions = (
        permissions
        if permissions is not None
        else {"external_directory": "deny", "question": "deny"}
    )
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
        credential_provider = provider or _configured_provider(container_command_parts)
        hidden = cwd / ".research"
        session_started = time.monotonic()
        with _opencode_auth_lock(credential_provider, float(timeout)) as host_auth:
            original_auth = host_auth.read_bytes() if host_auth is not None else None
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
                interrupted = False
                container_stopped = True
                exit_code = 127
                try:
                    for attempt in range(2):
                        container_name = f"quant-agent-{uuid.uuid4().hex}"
                        attempt_log = log_path.with_name(
                            f"{log_path.stem}.attempt-{attempt + 1:03d}{log_path.suffix}"
                        )
                        container_command = _docker_opencode_command(
                            container_command_parts,
                            cwd,
                            effective_permissions,
                            read_only_paths,
                            hidden_mounts,
                            runtime_home,
                            container_name,
                            max(1, int(float(timeout) * 1000)),
                        )
                        container_stopped = False
                        try:
                            exit_code = _run_prompt_process(
                                container_command,
                                container_input,
                                cwd,
                                attempt_log,
                                max(
                                    0.1,
                                    float(timeout) - (time.monotonic() - session_started),
                                ),
                            )
                        finally:
                            removed = _remove_agent_container(container_name)
                            container_stopped = removed
                        if not removed:
                            with attempt_log.open("a", encoding="utf-8") as log:
                                log.write("\nFailed to remove Agent container")
                            exit_code = 127
                            attempt_log.replace(log_path)
                            break
                        if attempt == 0 and _is_retryable_bind_source_failure(
                            exit_code,
                            attempt_log,
                            container_command,
                        ):
                            time.sleep(0.25)
                            continue
                        if attempt_log.exists():
                            attempt_log.replace(log_path)
                        else:
                            log_path.write_text("", encoding="utf-8")
                        break
                except KeyboardInterrupt:
                    interrupted = True
                if container_stopped:
                    recovery_failure, authentication_snapshots = _recover_rotated_oauth(
                        host_auth,
                        original_auth,
                        runtime_home,
                        credential_provider,
                        container_command_parts,
                        temporary_root,
                    )
                else:
                    recovery_failure = None
                    try:
                        authentication_snapshots = [json.loads(original_auth or b"null")]
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        authentication_snapshots = []
                _redact_authentication_log(log_path, authentication_snapshots)
                if recovery_failure is not None:
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write(f"\n{recovery_failure.message}")
                    exit_code = 127
                if interrupted:
                    raise KeyboardInterrupt
                return exit_code
    except (OSError, ValueError) as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return 127


def _infrastructure_failure(log_path: Path) -> InfrastructureFailure | None:
    try:
        detail = log_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    folded = detail.casefold()
    if "argument list too long" in folded or "e2big" in folded:
        return InfrastructureFailure(
            "invocation_argument_too_long",
            "Local process invocation exceeded the operating system argument limit",
        )
    authentication_markers = (
        "invalid_grant",
        "token refresh failed",
        "refresh token has been revoked",
        "refresh token is revoked",
        "refresh token has expired",
        "provider authentication failed",
    )
    if any(marker in folded for marker in authentication_markers):
        return InfrastructureFailure(
            "provider_authentication",
            "Provider authentication failed; re-authenticate before retrying",
        )
    if (
        "opencode credential lock timed out" in folded
        or "provider authentication state" in folded
    ):
        return InfrastructureFailure(
            "provider_authentication_state",
            "Provider authentication state could not be updated safely",
        )
    container_markers = (
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
    if not any(marker.casefold() in folded for marker in container_markers):
        return None
    compact = " ".join(detail.split())
    return InfrastructureFailure("container_runtime", compact[:2000])


def _container_infrastructure_error(log_path: Path) -> str | None:
    failure = _infrastructure_failure(log_path)
    return failure.message if failure is not None else None


def _redact_authentication_log(
    log_path: Path,
    additional_payloads: Sequence[Any] = (),
) -> None:
    auth_path = _opencode_runtime_sources()[0][0]
    try:
        detail = log_path.read_text(encoding="utf-8")
    except OSError:
        return
    payloads = list(additional_payloads)
    try:
        payloads.append(json.loads(auth_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    secrets: set[str] = set()
    secret_fields = {
        "access", "access_token", "apikey", "api_key", "key",
        "refresh", "refresh_token", "secret", "token",
    }

    def collect(value: Any, field: str | None = None) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                collect(child, str(key).casefold())
        elif isinstance(value, list):
            for child in value:
                collect(child, field)
        elif field in secret_fields and isinstance(value, str) and value:
            secrets.add(value)

    for payload in payloads:
        collect(payload)
    redacted = detail
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    if redacted != detail:
        temporary = log_path.with_suffix(log_path.suffix + ".tmp")
        temporary.write_text(redacted, encoding="utf-8")
        os.replace(temporary, log_path)


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


def preflight_provider_authentication(
    task: ResearchTask,
    research_root: Path,
) -> None:
    """Verify configured Provider authentication without allocating a research Round."""
    preflight_root = research_root / task.task_id / ".tmp" / "provider-preflight"
    preflight_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe-", dir=preflight_root) as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        events_path = root / "events.jsonl"
        opencode = task.raw["opencode"]
        command = [
            "opencode", "run", "--pure", "--format", "json",
            "--model", str(opencode["model"]), "--dir", str(workspace),
        ]
        if variant := opencode.get("variant"):
            command.extend(["--variant", str(variant)])
        provider = str(opencode["model"]).partition("/")[0]
        try:
            with _opencode_auth_lock(provider, 60):
                exit_code = _run_opencode_with_permissions(
                    command,
                    "Reply exactly OK. Do not use tools.",
                    workspace,
                    events_path,
                    60,
                    _NO_TOOL_PERMISSIONS,
                )
        except ValueError as exc:
            events_path.write_text(str(exc), encoding="utf-8")
            exit_code = 127
        if exit_code == 0:
            return
        failure = _infrastructure_failure(events_path)
        if failure is not None:
            raise AgentContainerInfrastructureError(failure.message)
        raise AgentContainerInfrastructureError(
            f"Provider authentication preflight exited with code {exit_code}"
        )


def _run_opencode_read_only(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
) -> int:
    return _run_opencode_container(
        command,
        prompt,
        cwd,
        log_path,
        timeout,
        permissions=_NO_TOOL_PERMISSIONS,
    )


def _run_opencode_report_read_only(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout: int,
) -> int:
    return _run_opencode_container(
        command,
        prompt,
        cwd,
        log_path,
        timeout,
        read_only_paths=(cwd / "report-input.json",),
        permissions=_NO_TOOL_PERMISSIONS,
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
    shell_timeout_seconds: int,
    finalization_reserve_seconds: int,
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
        f"Agent Shell default timeout: {shell_timeout_seconds} seconds. The Harness configures "
        "this explicitly; every command is still capped by the live Round remaining time. Do not "
        "set a shorter timeout when running the provided Development backtest command.",
        f"Live Round clock: {round_clock_path}. Read it before evaluations and during finalization; "
        "remaining_seconds and phase are refreshed by the Harness.",
        f"The Development evaluator reserves the final {finalization_reserve_seconds} seconds "
        "for focused tests and submission. It will reject a projected over-budget grid instead "
        "of silently truncating or automatically changing the parameter set.",
        "When the clock phase becomes converge, stop expanding the search. In finalize, preserve "
        "the best candidate, run focused tests, and prepare the required JSON. In submit_now, "
        "return immediately. Harness-owned validation after submission is outside this deadline.",
        "Before every Development backtest, ensure the current candidate is importable, run a "
        "focused test, then write "
        ".quant-research-checkpoint/metadata.json with string fields previous_feedback, hypothesis, attempts, "
        "development_effect, and candidate, then run `python3 -m "
        "quant_core.research.checkpoint submit .quant-research-checkpoint/metadata.json`. Repeat this whenever the best "
        "candidate improves. A checkpoint is confirmed only when the command returns an accepted "
        "checkpoint ID. In converge and finalize, checkpoint the best retained candidate early; "
        "in submit_now, prefer an immediate final response if ready, otherwise submit the best "
        "checkpoint immediately.",
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
    failure_code: str | None = None,
) -> Path:
    result_path = output_dir / "result.json"
    payload = {"experiment_id": experiment_id, "status": "failed", "error": error}
    if round_timing is not None:
        payload["round_timing"] = dict(round_timing)
    if failure_kind is not None:
        payload["failure_kind"] = failure_kind
    if failure_code is not None:
        payload["failure_code"] = failure_code
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
        submission = agent_output.get("submission")
        if isinstance(submission, Mapping):
            payload["submission"] = dict(submission)
    ExperimentResult.from_mapping(payload)
    write_json_atomic(result_path, payload)
    return result_path


def _run_once_impl(
    task_path: str | Path,
    experiment_id: str,
    output_dir: str | Path,
    *,
    workspace: str | Path = ".",
    gate_runtime: str | Path | None = None,
    research_history: Sequence[Mapping[str, Any]] = (),
    has_champion: bool | None = None,
    parent_champion_sha256: str | None = None,
    command_runner: CommandRunner = _run_command,
    opencode_runner: AgentRunner | None = None,
    event_sink: EventSink | None = None,
    round_id: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Path:
    from quant_core.research.checkpoint import (
        CheckpointReceiver,
        TRUSTED_RUNTIME_DIR,
        TRUSTED_STATUS_FILE,
    )

    task = ResearchTask.load(task_path)
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Workspace does not exist: {root}")
    out = Path(output_dir).resolve()
    if out == root:
        raise ValueError(
            "Research output directory must differ from the candidate workspace"
        )
    out.mkdir(parents=True, exist_ok=True)

    raw = task.raw
    fixed = task.evaluation_periods
    development_values = _values(task, fixed["development"], f"{experiment_id}-development", root)
    development_metrics_path = str(raw["commands"]["metrics_path"]).format_map(development_values)
    test_command = _format_command(raw["commands"]["test"], development_values)
    opencode = raw["opencode"]
    command_timeout = int(opencode["timeout_minutes"]) * 60
    round_timeout = int(raw["budget"].get("round_minutes", opencode["timeout_minutes"])) * 60
    finalization_reserve = _development_finalization_reserve(round_timeout)
    development_config: Path | None = None
    agent_development_config: Path | None = None
    if task.evaluation_mode == "walk_forward":
        base_development_config = {
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
            "constraints": task.raw["evaluation"]["constraints"],
            "objective": task.raw["evaluation"]["objective"],
        }
        development_config = root / ".quant-research-development.json"
        write_json_atomic(development_config, base_development_config)
        agent_development_config = root / ".quant-research-agent-development.json"
        write_json_atomic(agent_development_config, {
            **base_development_config,
            "execution": {
                "round_clock_path": _ROUND_CLOCK_FILE,
                "checkpoint_status_path": (
                    f"{TRUSTED_RUNTIME_DIR}/{TRUSTED_STATUS_FILE}"
                ),
                "strategy_path": task.strategy_path,
                "progress_path": str(
                    Path(development_metrics_path).parent / "progress.json"
                ),
                "finalization_reserve_seconds": finalization_reserve,
                "safety_factor": _DEVELOPMENT_ESTIMATE_SAFETY_FACTOR,
            },
        })
    development_command = _evaluation_command(task, task_path, "development", development_values, development_config)
    agent_development_command = _evaluation_command(
        task,
        task_path,
        "development",
        development_values,
        agent_development_config,
    )
    before = _snapshot(root, out)
    event_details = {"round": round_id} if round_id is not None else {}
    round_clock = _RoundClock(
        root / _ROUND_CLOCK_FILE,
        round_timeout,
        event_sink,
        event_details,
        monotonic,
    )
    checkpoint_receiver = CheckpointReceiver(
        root,
        out,
        task.strategy_path,
        parent_champion_sha256,
        round_clock.deadline_monotonic,
        monotonic=monotonic,
        event_sink=event_sink,
        event_details=event_details,
    )
    agent_development_command = _containerize_prompt_command(
        agent_development_command, root
    )
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
        round_timeout,
        finalization_reserve,
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
        checkpoint_receiver.start()
        if opencode_runner is None:
            generated_dir = Path(development_metrics_path).parent.as_posix()
            agent_read_only_paths = _agent_read_only_paths(
                root,
                raw["scope"].get("forbidden", []),
                generated_dir,
            )
            agent_read_only_paths.append(checkpoint_receiver.trusted_runtime)
            if out != root and root in out.parents:
                agent_read_only_paths.append(out)
            exit_code = _run_opencode_container(
                opencode_command,
                prompt,
                root,
                events_path,
                round_timeout,
                read_only_paths=agent_read_only_paths,
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
        checkpoint_receiver.stop()
    deadline_exceeded = (
        exit_code == 124
        or float(round_timing["duration_seconds"]) >= round_timeout
    )
    infrastructure_failure = (
        _infrastructure_failure(events_path)
        if opencode_runner is None and exit_code != 0
        else None
    )
    agent_output: dict[str, Any] | None = None
    if infrastructure_failure is not None:
        if infrastructure_failure.code == "provider_authentication":
            _redact_authentication_log(events_path)
        _emit(event_sink, "agent_failed", message="agent infrastructure failure", **event_details)
        reason = f"Agent infrastructure failure: {infrastructure_failure.message}"
        return _write_failed(
            out,
            experiment_id,
            reason,
            round_timing=round_timing,
            failure_kind="infrastructure",
            failure_code=infrastructure_failure.code,
        )
    if deadline_exceeded:
        development_progress: dict[str, Any] | None = None
        progress_path = root / Path(development_metrics_path).parent / "progress.json"
        try:
            progress_payload = json.loads(progress_path.read_text(encoding="utf-8"))
            if isinstance(progress_payload, dict):
                development_progress = progress_payload
        except (OSError, json.JSONDecodeError):
            pass
        _emit(
            event_sink,
            "round_deadline_exceeded",
            message="candidate research deadline exceeded",
            development_progress=development_progress,
            **event_details,
        )
        checkpoint = checkpoint_receiver.latest_valid()
        if checkpoint is None:
            return _write_failed(
                out,
                experiment_id,
                "Candidate research deadline exceeded",
                round_timing=round_timing,
            )
        strategy_path = root / checkpoint.strategy_path
        strategy_path.parent.mkdir(parents=True, exist_ok=True)
        strategy_path.write_bytes(checkpoint.strategy_content)
        agent_output = {
            "status": "completed",
            **dict(checkpoint.metadata),
            "submission": {
                "mode": "checkpoint",
                "checkpoint_id": checkpoint.checkpoint_id,
                "submitted_at": checkpoint.submitted_at,
                "submitted_by_timeout": True,
                "strategy_sha256": checkpoint.strategy_sha256,
            },
        }
        _emit(
            event_sink,
            "checkpoint_restored",
            checkpoint_id=checkpoint.checkpoint_id,
            strategy_sha256=checkpoint.strategy_sha256,
            message="restored candidate checkpoint after Round deadline",
            **event_details,
        )
        events_path.unlink(missing_ok=True)
    elif exit_code != 0:
        _emit(event_sink, "agent_failed", message="agent failed", **event_details)
        return _write_failed(
            out,
            experiment_id,
            "OpenCode session failed",
            round_timing=round_timing,
        )
    else:
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
        strategy_file = root / task.strategy_path
        strategy_sha256 = (
            hashlib.sha256(strategy_file.read_bytes()).hexdigest()
            if strategy_file.is_file()
            else hashlib.sha256(b"").hexdigest()
        )
        agent_output["submission"] = {
            "mode": "final",
            "submitted_at": str(round_timing["finished_at"]),
            "submitted_by_timeout": False,
            "strategy_sha256": strategy_sha256,
        }

    assert agent_output is not None

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
        "submission": agent_output["submission"],
    }
    ExperimentResult.from_mapping(payload)
    result_path = out / "result.json"
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
    parent_champion_sha256: str | None = None,
    command_runner: CommandRunner = _run_command,
    opencode_runner: AgentRunner | None = None,
    event_sink: EventSink | None = None,
    round_id: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    evaluation_environment: EvaluationEnvironment | None = None,
) -> Path:
    environment = evaluation_environment or capture_evaluation_environment()
    result_path = _run_once_impl(
        task_path,
        experiment_id,
        output_dir,
        workspace=workspace,
        gate_runtime=gate_runtime,
        research_history=research_history,
        has_champion=has_champion,
        parent_champion_sha256=parent_champion_sha256,
        command_runner=command_runner,
        opencode_runner=opencode_runner,
        event_sink=event_sink,
        round_id=round_id,
        monotonic=monotonic,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["evaluation_environment_sha256"] = environment.sha256
    ExperimentResult.from_mapping(result)
    write_json_atomic(result_path, result)
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
        "evaluation": {
            "mode": task.evaluation_mode,
            "objective": task.raw["evaluation"]["objective"],
            "constraints": task.raw["evaluation"]["constraints"],
            "periods": task.evaluation_periods,
        },
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fail_candidate_evidence(
    manager: ResearchWorkspace,
    candidate: Path,
    state: dict[str, Any],
    experiment: Path,
    decision_path: Path,
    record_id: str,
    result: Mapping[str, Any],
    error: Exception,
    *,
    candidate_patch_sha256: str | None = None,
) -> Path:
    message = f"Candidate evidence integrity failure: {error}"
    decision = {
        "experiment_id": record_id,
        "decision": "failed",
        "reasons": [message],
        "failure_kind": "infrastructure",
        "failure_code": "candidate_patch_integrity_failed",
    }
    submission = result.get("submission")
    if isinstance(submission, Mapping):
        decision["submission"] = dict(submission)
    if candidate_patch_sha256 is not None:
        decision["candidate_patch_sha256"] = candidate_patch_sha256
    write_json_atomic(decision_path, decision)
    manager.reject(candidate, state, record_id)
    return _write_failed(
        experiment,
        record_id,
        message,
        result,
        failure_kind="infrastructure",
        failure_code="candidate_patch_integrity_failed",
    )


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
    monotonic: Callable[[], float] = time.monotonic,
    evaluation_environment: EvaluationEnvironment | None = None,
    prepared_candidate: tuple[Path, Path, dict[str, Any]] | None = None,
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
    environment = evaluation_environment or capture_evaluation_environment()
    manager = ResearchWorkspace(
        source,
        managed_root,
        task.task_id,
        run_number=run_number,
        evaluation_environment_sha256=environment.sha256,
    )
    if prepared_candidate is None:
        manager.evaluator_contract_sha256(
            task.evaluator_contract_paths,
            strategy_path=task.strategy_path,
        )
        candidate, experiment, state = manager.create_candidate(
            round_id,
            date.fromisoformat(development_end),
            task.baseline_mode,
            task.baseline_exclude,
            task.strategy_path,
        )
    else:
        candidate, experiment, state = prepared_candidate
        if candidate.resolve() != (manager.candidates / round_id).resolve():
            raise ValueError("prepared candidate path does not match the requested Round")
        if experiment.resolve() != (manager.rounds / round_id).resolve():
            raise ValueError("prepared experiment path does not match the requested Round")
        if not candidate.is_dir() or not experiment.is_dir():
            raise FileNotFoundError("prepared candidate Round is incomplete")
    has_champion = isinstance(state.get("champion_sha256"), str)
    metrics_key = _metrics_key(task)
    evaluator_contract_sha256 = evaluator_contract_sha256_for_commit(
        candidate,
        task.evaluator_contract_paths,
        str(state["strategy_path"]),
    )
    applicability = manager.refresh_champion_metrics_status(
        state,
        metrics_key,
        task.evaluator_contract_paths,
        evaluator_contract_sha256=evaluator_contract_sha256,
    )
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
        parent_champion_sha256=(
            str(state["champion_sha256"])
            if isinstance(state.get("champion_sha256"), str)
            else None
        ),
        command_runner=command_runner,
        opencode_runner=opencode_runner,
        event_sink=event_sink,
        round_id=round_id,
        monotonic=monotonic,
        evaluation_environment=environment,
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
        if isinstance(result.get("submission"), dict):
            decision["submission"] = dict(result["submission"])
        if result.get("failure_kind") == "infrastructure":
            decision["failure_kind"] = "infrastructure"
            if isinstance(result.get("failure_code"), str):
                decision["failure_code"] = result["failure_code"]
        write_json_atomic(decision_path, decision)
        manager.reject(candidate, state, record_id)
        return result_path

    candidate_patch = experiment / "candidate.patch"
    try:
        candidate_patch_sha256 = manager.write_candidate_patch(
            candidate,
            state,
            task.raw["scope"]["editable"],
            candidate_patch,
            str(result["submission"]["strategy_sha256"]),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        patch_sha256 = (
            hashlib.sha256(candidate_patch.read_bytes()).hexdigest()
            if candidate_patch.is_file()
            else None
        )
        return _fail_candidate_evidence(
            manager,
            candidate,
            state,
            experiment,
            decision_path,
            record_id,
            result,
            exc,
            candidate_patch_sha256=patch_sha256,
        )
    champion_metrics = manager.valid_champion_metrics(state) if has_champion else None
    evaluated_champion_record: dict[str, Any] | None = None
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
            evaluated_champion_record = manager.metrics_record(
                champion_metrics,
                applicability,
                record_id,
            )
        except RuntimeError as exc:
            decision = {
                "experiment_id": result["experiment_id"],
                "decision": "failed",
                "reasons": [str(exc)],
                "submission": dict(result["submission"]),
                "candidate_patch_sha256": candidate_patch_sha256,
            }
            write_json_atomic(decision_path, decision)
            manager.reject(candidate, state, record_id)
            failed_path = _write_failed(experiment, record_id, str(exc), result)
            return failed_path
        finally:
            manager.remove_evaluator(evaluator)

    decision = {
        "experiment_id": result["experiment_id"],
        **_decide(task, champion_metrics, result["metrics"]),
        "submission": dict(result["submission"]),
        "candidate_patch_sha256": candidate_patch_sha256,
    }
    if decision["decision"] == "accepted" and not has_champion:
        try:
            manager.write_candidate_source(
                candidate,
                state,
                experiment / "candidate.py",
                str(result["submission"]["strategy_sha256"]),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return _fail_candidate_evidence(
                manager,
                candidate,
                state,
                experiment,
                decision_path,
                record_id,
                result,
                exc,
                candidate_patch_sha256=candidate_patch_sha256,
            )
    write_json_atomic(decision_path, decision)
    if decision["decision"] == "accepted":
        manager.promote(
            candidate,
            state,
            record_id,
            result["metrics"],
            task.raw["scope"]["editable"],
            metrics_key,
            task.evaluator_contract_paths,
            evaluator_contract_sha256,
        )
    else:
        if evaluated_champion_record is not None:
            state["champion_metrics_record"] = evaluated_champion_record
        manager.reject(candidate, state, record_id)
    return result_path
