"""OpenCode runtime staging and concurrency-safe OAuth rotation."""

from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from quant_core.research.agent_errors import (
    InfrastructureFailure,
    opencode_runtime_sources,
)


PromptRunner = Callable[
    [Sequence[str], str, Path, Path, int, Mapping[str, str]],
    int,
]
OAUTH_ACCESS_FIELDS = ("access", "access_token")
OAUTH_REFRESH_FIELDS = ("refresh", "refresh_token")
OAUTH_EXPIRY_FIELDS = ("expires", "expiry", "expires_at")
OAUTH_MUTABLE_FIELDS = frozenset(
    (*OAUTH_ACCESS_FIELDS, *OAUTH_REFRESH_FIELDS, *OAUTH_EXPIRY_FIELDS)
)
AUTH_RECOVERY_TIMEOUT = 60


def stage_opencode_runtime(runtime_home: Path) -> None:
    for source, relative in opencode_runtime_sources():
        if not source.is_file():
            continue
        destination = runtime_home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)


def read_auth_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("OpenCode authentication file must contain a JSON object")
    return payload


def oauth_provider_auth(
    payload: Mapping[str, Any],
    provider: str | None,
) -> dict[str, Any] | None:
    if provider is None:
        return None
    value = payload.get(provider)
    if not isinstance(value, dict) or value.get("type") != "oauth":
        return None
    if not any(field in value for field in OAUTH_REFRESH_FIELDS):
        return None
    return dict(value)


def validated_rotated_provider_auth(
    original: Mapping[str, Any],
    candidate: Any,
) -> dict[str, Any]:
    if not isinstance(candidate, dict) or candidate.get("type") != original.get("type"):
        raise ValueError("OpenCode OAuth credential type changed")
    unexpected = set(candidate) - set(original) - OAUTH_MUTABLE_FIELDS
    if unexpected:
        raise ValueError("OpenCode OAuth credential contains unexpected fields")
    for key, value in original.items():
        if key not in OAUTH_MUTABLE_FIELDS and candidate.get(key) != value:
            raise ValueError("OpenCode OAuth credential identity fields changed")
    for fields, label in (
        (OAUTH_ACCESS_FIELDS, "access token"),
        (OAUTH_REFRESH_FIELDS, "refresh token"),
    ):
        present = [field for field in fields if field in original or field in candidate]
        if not present or not any(
            isinstance(candidate.get(field), str) and bool(candidate[field])
            for field in present
        ):
            raise ValueError(f"OpenCode OAuth credential is missing its {label}")
    for field in OAUTH_EXPIRY_FIELDS:
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


def write_auth_payload_atomic(path: Path, payload: Mapping[str, Any]) -> None:
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


def authentication_probe_command(
    command: Sequence[str],
    workspace: Path,
) -> list[str]:
    try:
        model = command[command.index("--model") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("OpenCode OAuth session is missing its configured model") from exc
    probe = [
        "opencode",
        "run",
        "--pure",
        "--format",
        "json",
        "--model",
        model,
        "--dir",
        str(workspace),
    ]
    try:
        variant = command[command.index("--variant") + 1]
    except (ValueError, IndexError):
        pass
    else:
        probe.extend(["--variant", variant])
    return probe


def recover_rotated_oauth(
    host_auth: Path | None,
    original_auth: bytes | None,
    runtime_home: Path,
    provider: str | None,
    command: Sequence[str],
    temporary_root: Path,
    *,
    prompt_runner: PromptRunner,
    no_tool_permissions: Mapping[str, str],
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
    original_provider = oauth_provider_auth(original_payload, provider)
    if original_provider is None:
        return None, snapshots
    runtime_auth = runtime_home / ".local/share/opencode/auth.json"
    try:
        runtime_payload = read_auth_payload(runtime_auth)
        snapshots.append(runtime_payload)
        rotated_provider = validated_rotated_provider_auth(
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
    stage_opencode_runtime(validation_home)
    validation_auth = validation_home / ".local/share/opencode/auth.json"
    try:
        validation_payload = read_auth_payload(validation_auth)
    except (OSError, ValueError, json.JSONDecodeError):
        validation_payload = dict(original_payload)
    validation_payload[provider] = rotated_provider
    write_auth_payload_atomic(validation_auth, validation_payload)
    validation_log = temporary_root / "credential-validation.jsonl"
    probe_command = authentication_probe_command(command, validation_workspace)
    probe_exit = prompt_runner(
        probe_command,
        "Reply exactly OK. Do not use tools.",
        validation_workspace,
        validation_log,
        AUTH_RECOVERY_TIMEOUT,
        {
            "HOME": str(validation_home),
            "OPENCODE_PERMISSION": json.dumps(dict(no_tool_permissions)),
        },
    )
    try:
        verified_payload = read_auth_payload(validation_auth)
        snapshots.append(verified_payload)
        verified_provider = validated_rotated_provider_auth(
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
        current_payload = read_auth_payload(host_auth)
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
        write_auth_payload_atomic(host_auth, current_payload)
    except OSError:
        return InfrastructureFailure(
            "provider_authentication_state",
            "Provider authentication state could not be persisted safely",
        ), snapshots
    snapshots.append(current_payload)
    return probe_failure, snapshots


def configured_provider(command: Sequence[str]) -> str | None:
    try:
        model = command[command.index("--model") + 1]
    except (ValueError, IndexError):
        return None
    provider, separator, _ = model.partition("/")
    return provider if separator and provider else None


@contextmanager
def opencode_auth_lock(
    provider: str | None,
    timeout: float,
) -> Iterator[Path | None]:
    auth_path = opencode_runtime_sources()[0][0]
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
