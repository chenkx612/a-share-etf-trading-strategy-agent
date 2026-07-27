from __future__ import annotations

import json
import os
import shutil
import time
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
    _run_opencode_container,
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


@pytest.mark.skipif(
    os.environ.get("QUANT_TEST_AGENT_CONTAINER") != "1",
    reason="set QUANT_TEST_AGENT_CONTAINER=1 after building the research Agent image",
)
def test_candidate_container_official_attempt_uses_host_receiver(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    shutil.copytree(Path.cwd() / "src", workspace / "src")
    output = tmp_path / "round"
    output.mkdir()
    strategy = workspace / "strategy.py"
    strategy.write_text("VALUE = 1\n", encoding="utf-8")
    metrics_path = workspace / "outputs/development/metrics.json"
    checkpoint_receiver = CheckpointReceiver(
        workspace,
        output,
        "strategy.py",
        None,
        time.monotonic() + 60,
    )

    def host_evaluator(
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
        timeout: int,
    ) -> int:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps({
            "sortino": 1.25,
            "max_drawdown": -0.1,
        }), encoding="utf-8")
        return 0

    attempt_receiver = DevelopmentAttemptReceiver(
        workspace,
        output,
        ["host-development-evaluator"],
        metrics_path,
        checkpoint_receiver.latest_valid,
        host_evaluator,
        30,
        time.monotonic() + 60,
        _normalize_development_metrics,
    )
    checkpoint_receiver.start()
    attempt_receiver.start()
    try:
        metadata = workspace / RUNTIME_DIR / "metadata.json"
        metadata.write_text(json.dumps(_metadata("container")), encoding="utf-8")
        checkpoint = submit(metadata, workspace=workspace)
        exit_code = _run_opencode_container(
            ["python3", "-m", "quant_core.research.attempt", "evaluate"],
            "",
            workspace,
            output / "container-attempt.log",
            30,
        )
    finally:
        attempt_receiver.stop()
        checkpoint_receiver.stop()

    assert exit_code == 0
    response = json.loads(
        (output / "container-attempt.log").read_text(encoding="utf-8")
    )
    assert response["candidate_sha256"] == checkpoint["strategy_sha256"]
    assert response["development_metrics"]["sortino"] == 1.25
    attempt = json.loads(
        (output / "development-attempts/001/attempt.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt["candidate_sha256"] == checkpoint["strategy_sha256"]
    assert attempt["development_metrics"]["max_drawdown"] == -0.1


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
