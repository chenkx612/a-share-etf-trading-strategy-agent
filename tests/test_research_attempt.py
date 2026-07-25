from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from quant_core.research.attempt import (
    DevelopmentAttemptReceiver,
    evaluate,
    record_learning,
)
from quant_core.research.checkpoint import CheckpointReceiver, RUNTIME_DIR, submit
from quant_core.research.runner import (
    _development_attempts,
    _normalize_development_metrics,
)


def _metadata(label: str) -> dict[str, str]:
    return {
        "previous_feedback": "",
        "hypothesis": f"Hypothesis {label}",
        "attempts": f"Attempts {label}",
        "development_effect": f"Effect {label}",
        "candidate": f"Candidate {label}",
    }


def test_attempts_are_deduplicated_and_preserve_optional_learning(
    tmp_path: Path,
) -> None:
    output = tmp_path / "round"
    output.mkdir()
    strategy = tmp_path / "strategy.py"
    metrics_path = tmp_path / "outputs" / "metrics.json"
    checkpoint_receiver = CheckpointReceiver(
        tmp_path,
        output,
        "strategy.py",
        None,
        1000.0,
        monotonic=lambda: 0.0,
    )

    def command_runner(
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        value = int(strategy.read_text(encoding="utf-8").partition("=")[2])
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps({
            "sortino": float(value),
            "max_drawdown": -0.1 * value,
        }), encoding="utf-8")
        return 0

    attempt_receiver = DevelopmentAttemptReceiver(
        tmp_path,
        output,
        ["development"],
        metrics_path,
        checkpoint_receiver.latest_valid,
        command_runner,
        60,
        1000.0,
        _normalize_development_metrics,
        monotonic=lambda: 0.0,
    )
    checkpoint_receiver.start()
    attempt_receiver.start()
    try:
        strategy.write_text("VALUE=1", encoding="utf-8")
        metadata = tmp_path / RUNTIME_DIR / "metadata.json"
        metadata.write_text(json.dumps(_metadata("one")), encoding="utf-8")
        submit(metadata, workspace=tmp_path)
        first = evaluate(workspace=tmp_path)
        duplicate = evaluate(workspace=tmp_path)
        learning = tmp_path / RUNTIME_DIR / "learning.json"
        learning.write_text(json.dumps({
            "learning": "The first candidate was weaker than the intended target.",
        }), encoding="utf-8")
        record_learning(str(first["attempt_id"]), learning, workspace=tmp_path)

        strategy.write_text("VALUE=2", encoding="utf-8")
        metadata.write_text(json.dumps(_metadata("two")), encoding="utf-8")
        second_checkpoint = submit(metadata, workspace=tmp_path)
        second = evaluate(workspace=tmp_path)
    finally:
        attempt_receiver.stop()
        checkpoint_receiver.stop()

    assert first["attempt_id"] == "001"
    assert duplicate["attempt_id"] == "001"
    assert duplicate["deduplicated"] is True
    assert second["attempt_id"] == "002"
    result = {
        "status": "completed",
        "hypothesis": "Hypothesis two",
        "submission": {
            "strategy_sha256": second_checkpoint["strategy_sha256"],
        },
        "metrics": {"development": {"sortino": 2.0}},
    }
    attempts = _development_attempts(output, result)
    assert [attempt["outcome"] for attempt in attempts] == [
        "abandoned",
        "submitted",
    ]
    assert attempts[0]["learning"].startswith("The first candidate")
    assert attempts[1]["learning"] is None
    assert len(list((output / "development-attempts").iterdir())) == 2


def test_synthetic_attempt_id_follows_highest_recorded_id(tmp_path: Path) -> None:
    attempts_root = tmp_path / "development-attempts" / "002"
    attempts_root.mkdir(parents=True)
    (attempts_root / "attempt.json").write_text(json.dumps({
        "attempt_id": "002",
        "candidate_sha256": "a" * 64,
        "hypothesis": "Earlier successful evaluation",
        "development_metrics": {"sortino": 0.5},
    }), encoding="utf-8")

    attempts = _development_attempts(tmp_path, {
        "status": "completed",
        "hypothesis": "Final candidate",
        "submission": {"strategy_sha256": "b" * 64},
        "metrics": {"development": {"sortino": 1.0}},
    })

    assert [attempt["attempt_id"] for attempt in attempts] == ["002", "003"]


def test_attempt_rejects_strategy_changed_during_evaluation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "round"
    output.mkdir()
    strategy = tmp_path / "strategy.py"
    strategy.write_text("VALUE=1", encoding="utf-8")
    metrics_path = tmp_path / "outputs" / "metrics.json"
    checkpoint_receiver = CheckpointReceiver(
        tmp_path,
        output,
        "strategy.py",
        None,
        1000.0,
        monotonic=lambda: 0.0,
    )

    def mutating_runner(
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        strategy.write_text("VALUE=2", encoding="utf-8")
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(
            json.dumps({"sortino": 2.0}),
            encoding="utf-8",
        )
        return 0

    attempt_receiver = DevelopmentAttemptReceiver(
        tmp_path,
        output,
        ["development"],
        metrics_path,
        checkpoint_receiver.latest_valid,
        mutating_runner,
        60,
        1000.0,
        _normalize_development_metrics,
        monotonic=lambda: 0.0,
    )
    checkpoint_receiver.start()
    attempt_receiver.start()
    try:
        metadata = tmp_path / RUNTIME_DIR / "metadata.json"
        metadata.write_text(json.dumps(_metadata("one")), encoding="utf-8")
        submit(metadata, workspace=tmp_path)

        with pytest.raises(RuntimeError, match="strategy changed"):
            evaluate(workspace=tmp_path)
    finally:
        attempt_receiver.stop()
        checkpoint_receiver.stop()

    assert not (output / "development-attempts").exists()
