from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from quant_core.research.checkpoint import CheckpointReceiver, RUNTIME_DIR, submit


def _metadata(label: str) -> dict[str, str]:
    return {
        "previous_feedback": "",
        "hypothesis": f"Hypothesis {label}",
        "attempts": f"Attempts {label}",
        "development_effect": f"Development effect {label}",
        "candidate": f"Candidate {label}",
    }


def _submit(workspace: Path, label: str) -> dict[str, object]:
    metadata_path = workspace / RUNTIME_DIR / "metadata.json"
    metadata_path.write_text(json.dumps(_metadata(label)), encoding="utf-8")
    return dict(submit(metadata_path, workspace=workspace))


def test_checkpoint_receiver_falls_back_when_latest_frozen_copy_is_corrupt(
    tmp_path: Path,
) -> None:
    output = tmp_path / "round"
    output.mkdir()
    clock = [0.0]
    receiver = CheckpointReceiver(
        tmp_path,
        output,
        "strategy.py",
        "a" * 64,
        10.0,
        monotonic=lambda: clock[0],
    )
    receiver.start()
    try:
        for value in (1, 2):
            (tmp_path / "strategy.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
            assert _submit(tmp_path, str(value))["checkpoint_id"] == f"{value:03d}"
    finally:
        receiver.stop()

    (receiver.checkpoints / "002/files/strategy.py").write_text(
        "corrupted", encoding="utf-8",
    )
    checkpoint = receiver.latest_valid()

    assert checkpoint is not None
    assert checkpoint.checkpoint_id == "001"
    assert checkpoint.strategy_content == b"VALUE = 1\n"


def test_checkpoint_receiver_rejects_submission_at_or_after_deadline(
    tmp_path: Path,
) -> None:
    output = tmp_path / "round"
    output.mkdir()
    clock = [10.0]
    receiver = CheckpointReceiver(
        tmp_path,
        output,
        "strategy.py",
        None,
        10.0,
        monotonic=lambda: clock[0],
    )
    receiver.start()
    try:
        (tmp_path / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="after the Round deadline"):
            _submit(tmp_path, "late")
    finally:
        receiver.stop()

    assert receiver.latest_valid() is None


def test_checkpoint_receiver_uses_a_fresh_session_for_each_invocation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "round"
    output.mkdir()

    deadline = time.monotonic() + 10.0
    first = CheckpointReceiver(tmp_path, output, "strategy.py", None, deadline)
    first.start()
    try:
        (tmp_path / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
        _submit(tmp_path, "first")
    finally:
        first.stop()

    second = CheckpointReceiver(tmp_path, output, "strategy.py", None, deadline)
    second.start()
    try:
        assert second.latest_valid() is None
        (tmp_path / "strategy.py").write_text("VALUE = 2\n", encoding="utf-8")
        assert _submit(tmp_path, "second")["checkpoint_id"] == "001"
    finally:
        second.stop()

    assert first.checkpoints != second.checkpoints
    assert (first.checkpoints / "001").is_dir()
    assert (second.checkpoints / "001").is_dir()


def test_checkpoint_receiver_rejects_invalid_python(tmp_path: Path) -> None:
    output = tmp_path / "round"
    output.mkdir()
    receiver = CheckpointReceiver(
        tmp_path,
        output,
        "strategy.py",
        None,
        10.0,
        monotonic=lambda: 0.0,
    )
    receiver.start()
    try:
        (tmp_path / "strategy.py").write_text("def broken(:\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not valid Python"):
            _submit(tmp_path, "invalid")
    finally:
        receiver.stop()

    assert receiver.latest_valid() is None
