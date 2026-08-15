"""Agent runtime failure classification and credential-safe diagnostics."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quant_core.research.storage import write_bytes_atomic


class AgentContainerInfrastructureError(RuntimeError):
    """Raised when the isolated Agent container cannot be started safely."""

    def __init__(
        self,
        message: str,
        code: str = "agent_container_preflight_failed",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.failure_kind = "infrastructure"
        self.failure_code = code


class CandidateBindPreflightError(AgentContainerInfrastructureError):
    """Raised before a Round is allocated when Docker cannot see its worktree."""

    def __init__(self, message: str, code: str, evidence_path: Path) -> None:
        super().__init__(message, code)
        self.evidence_path = evidence_path


@dataclass(frozen=True)
class InfrastructureFailure:
    code: str
    message: str


def opencode_runtime_sources() -> tuple[tuple[Path, Path], ...]:
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


def infrastructure_failure(log_path: Path) -> InfrastructureFailure | None:
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


def container_infrastructure_error(log_path: Path) -> str | None:
    failure = infrastructure_failure(log_path)
    return failure.message if failure is not None else None


def redact_authentication_log(
    log_path: Path,
    additional_payloads: Sequence[Any] = (),
) -> None:
    auth_path = opencode_runtime_sources()[0][0]
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
        "access",
        "access_token",
        "apikey",
        "api_key",
        "key",
        "refresh",
        "refresh_token",
        "secret",
        "token",
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
        write_bytes_atomic(log_path, redacted.encode("utf-8"))
