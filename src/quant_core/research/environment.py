from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


REQUIRED_CONDA_ENV = "quant"
_NORMALIZE_NAME = re.compile(r"[-_.]+")


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalized_name(value: str) -> str:
    return _NORMALIZE_NAME.sub("-", value).casefold()


def _channel_name(value: object, subdir: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme:
        return value.rsplit("@", 1)[-1]
    path = parsed.path.rstrip("/")
    if isinstance(subdir, str) and path.endswith(f"/{subdir}"):
        path = path[: -(len(subdir) + 1)]
    channel = path.rsplit("/", 1)[-1] if path else ""
    return "/".join(part for part in (parsed.hostname, channel) if part)


def _conda_packages(prefix: Path) -> list[dict[str, object]]:
    conda_meta = prefix / "conda-meta"
    if not conda_meta.is_dir():
        raise RuntimeError(
            f"Research Harness requires Conda environment '{REQUIRED_CONDA_ENV}'; "
            "the active interpreter has no conda-meta directory"
        )
    packages: list[dict[str, object]] = []
    for path in sorted(conda_meta.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid Conda package metadata: {path.name}") from exc
        name = payload.get("name")
        version = payload.get("version")
        build = payload.get("build")
        if not all(isinstance(value, str) and value for value in (name, version, build)):
            raise RuntimeError(f"Incomplete Conda package metadata: {path.name}")
        packages.append({
            "name": _normalized_name(name),
            "version": version,
            "build": build,
            "build_number": payload.get("build_number"),
            "channel": _channel_name(payload.get("channel"), payload.get("subdir")),
            "subdir": payload.get("subdir"),
        })
    return sorted(
        packages,
        key=lambda item: (
            str(item["name"]),
            str(item["version"]),
            str(item["build"]),
        ),
    )


def _python_distributions() -> list[dict[str, str | None]]:
    distributions: list[dict[str, str | None]] = []
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not isinstance(name, str) or not name.strip():
            continue
        record = distribution.read_text("RECORD")
        distributions.append({
            "name": _normalized_name(name),
            "version": distribution.version,
            "record_sha256": _sha256(record.encode()) if record is not None else None,
        })
    return sorted(
        distributions,
        key=lambda item: (str(item["name"]), str(item["version"])),
    )


@dataclass(frozen=True)
class EvaluationEnvironment:
    manifest: Mapping[str, Any]
    sha256: str

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> EvaluationEnvironment:
        frozen = json.loads(_canonical_json(manifest))
        return cls(manifest=frozen, sha256=_sha256(_canonical_json(frozen)))


def capture_evaluation_environment(
    expected_conda_env: str = REQUIRED_CONDA_ENV,
) -> EvaluationEnvironment:
    active_name = os.environ.get("CONDA_DEFAULT_ENV")
    active_prefix = os.environ.get("CONDA_PREFIX")
    prefix = Path(sys.prefix).resolve()
    if (
        active_name != expected_conda_env
        or not active_prefix
        or Path(active_prefix).resolve() != prefix
        or prefix.name != expected_conda_env
    ):
        raise RuntimeError(
            f"Research Harness requires Conda environment '{expected_conda_env}'. "
            "Run it with: conda run --no-capture-output "
            f"-n {expected_conda_env} python -m quant_core.cli ..."
        )
    manifest = {
        "schema_version": 1,
        "conda": {
            "environment": expected_conda_env,
            "packages": _conda_packages(prefix),
        },
        "python": {
            "implementation": sys.implementation.name,
            "version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag,
            "soabi": sysconfig.get_config_var("SOABI"),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "distributions": _python_distributions(),
    }
    return EvaluationEnvironment.from_manifest(manifest)


def persist_evaluation_environment(
    task_root: Path,
    environment: EvaluationEnvironment,
) -> Path:
    actual_sha256 = EvaluationEnvironment.from_manifest(environment.manifest).sha256
    if actual_sha256 != environment.sha256:
        raise RuntimeError("Evaluation environment manifest does not match its SHA-256")
    directory = task_root / "environments"
    destination = directory / f"{environment.sha256}.json"
    encoded = json.dumps(
        environment.manifest,
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    ).encode() + b"\n"
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Evaluation environment manifest is invalid: {destination}"
            ) from exc
        if EvaluationEnvironment.from_manifest(existing).sha256 != environment.sha256:
            raise RuntimeError(
                f"Evaluation environment manifest hash mismatch: {destination}"
            )
        return destination
    directory.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix=f".{environment.sha256}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(encoded)
            temporary = Path(handle.name)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination
