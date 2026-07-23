from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant_core.research import environment as environment_module
from quant_core.research.environment import (
    EvaluationEnvironment,
    capture_evaluation_environment,
    persist_evaluation_environment,
)


def _fake_conda_prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "quant"
    conda_meta = prefix / "conda-meta"
    conda_meta.mkdir(parents=True)
    (conda_meta / "python-3.13.1-build_0.json").write_text(json.dumps({
        "name": "python",
        "version": "3.13.1",
        "build": "build_0",
        "build_number": 0,
        "channel": "https://user:secret@example.invalid/pkgs/main/osx-arm64",
        "subdir": "osx-arm64",
    }), encoding="utf-8")
    return prefix


def test_capture_requires_exact_quant_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = _fake_conda_prefix(tmp_path)
    monkeypatch.setattr(environment_module.sys, "prefix", str(prefix))
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "other")
    monkeypatch.setenv("CONDA_PREFIX", str(prefix))

    with pytest.raises(RuntimeError, match="requires Conda environment 'quant'"):
        capture_evaluation_environment()


def test_environment_manifest_is_stable_and_excludes_sensitive_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = _fake_conda_prefix(tmp_path)
    monkeypatch.setattr(environment_module.sys, "prefix", str(prefix))
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "quant")
    monkeypatch.setenv("CONDA_PREFIX", str(prefix))
    monkeypatch.setattr(environment_module.metadata, "distributions", lambda: [])

    first = capture_evaluation_environment()
    second = capture_evaluation_environment()
    serialized = json.dumps(first.manifest, sort_keys=True)

    assert first.sha256 == second.sha256
    assert str(tmp_path) not in serialized
    assert "secret" not in serialized
    assert "https://" not in serialized
    assert first.manifest["conda"]["packages"][0]["channel"] == "example.invalid/main"


def test_environment_manifest_registry_is_content_addressed(tmp_path: Path) -> None:
    environment = EvaluationEnvironment.from_manifest({
        "schema_version": 1,
        "python": {"version": "3.13.1"},
    })

    first = persist_evaluation_environment(tmp_path, environment)
    second = persist_evaluation_environment(tmp_path, environment)

    assert first == second
    assert first.name == f"{environment.sha256}.json"
    assert EvaluationEnvironment.from_manifest(
        json.loads(first.read_text(encoding="utf-8"))
    ).sha256 == environment.sha256


def test_environment_manifest_registry_rejects_a_false_digest(tmp_path: Path) -> None:
    environment = EvaluationEnvironment(
        manifest={"schema_version": 1},
        sha256="0" * 64,
    )

    with pytest.raises(RuntimeError, match="does not match"):
        persist_evaluation_environment(tmp_path, environment)

    assert not (tmp_path / "environments").exists()
