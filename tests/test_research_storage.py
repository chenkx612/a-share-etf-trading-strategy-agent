from __future__ import annotations

import json
from pathlib import Path

import pytest

import quant_core.research.storage as storage
from quant_core.research.storage import file_sha256, write_bytes_atomic, write_json_atomic


def test_atomic_writers_replace_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")

    write_json_atomic(path, {"schema_version": 1, "status": "running"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "status": "running",
    }
    binary = tmp_path / "candidate.py"
    write_bytes_atomic(binary, b"VALUE = 1\n")
    assert binary.read_bytes() == b"VALUE = 1\n"
    assert len(file_sha256(binary)) == 64


def test_atomic_writer_cleans_unique_temporary_file_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        assert source.parent == tmp_path
        assert source != path.with_suffix(".json.tmp")
        assert destination == path
        raise OSError("replace failed")

    monkeypatch.setattr(storage.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_json_atomic(path, {"status": "new"})

    assert path.read_text(encoding="utf-8") == "old"
    assert sorted(tmp_path.iterdir()) == [path]
