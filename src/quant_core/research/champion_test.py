from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from quant_core.research.contracts import ResearchTask
from quant_core.research.workspace import (
    ResearchWorkspace,
    copy_runtime_inputs,
    runtime_inputs_manifest,
    workspace_python_env,
    write_json_atomic,
)


def evaluate_run_champion_test(
    task_file: Path,
    task: ResearchTask,
    manager: ResearchWorkspace,
    loop_state: Mapping[str, Any],
) -> Path:
    """Evaluate the final Champion once, outside promotion, for one Run."""
    test = task.test_period
    if not isinstance(test, Mapping):
        raise ValueError("task.evaluation.test is required")

    champion_state = manager.load_state(task.strategy_path)
    champion_sha256 = loop_state.get("final_champion_sha256")
    if not isinstance(champion_sha256, str):
        raise RuntimeError("research Run does not have a Champion to test")
    frozen_champion = manager.terminal_champion_path
    if (
        not frozen_champion.is_file()
        or hashlib.sha256(frozen_champion.read_bytes()).hexdigest()
        != champion_sha256
    ):
        raise RuntimeError("research Run final Champion snapshot is unavailable")
    champion_state = {
        **champion_state,
        "champion_sha256": champion_sha256,
        "champion_round_id": loop_state.get("final_champion_round_id"),
        "champion_number": loop_state.get("final_champion_number"),
    }

    output = manager.run_test_root
    output.mkdir(parents=True, exist_ok=True)
    evaluator_id = f"run-{manager.run_id}"
    evaluator = manager.create_champion_test_evaluator(
        evaluator_id,
        champion_state,
        champion_path=frozen_champion,
    )
    try:
        runtime = (
            manager.evaluation_runtime
            if task.relative_period_config is not None
            else manager.source
        )
        copy_runtime_inputs(runtime, evaluator)
        runtime_inputs = runtime_inputs_manifest(evaluator)
        run_id = f"test-run-{manager.run_id}"
        values = {
            "python": sys.executable,
            "universe": str(task.raw["data"]["universe"]),
            "workspace": str(evaluator),
            "start": str(test["start"]),
            "end": str(test["end"]),
            "run_id": run_id,
            "strategy_name": task.strategy_name or "",
            "strategy_module": task.strategy_module or "",
        }
        metrics_relative = task.metrics_path_template.format_map(values)
        if task.evaluation_mode == "walk_forward":
            command = [
                sys.executable,
                "-m",
                "quant_core.research.evaluator",
                "--root",
                str(evaluator),
                "--universe",
                str(task.raw["data"]["universe"]),
                "--start",
                str(test["start"]),
                "--end",
                str(test["end"]),
                "--run-id",
                run_id,
                "--candidate-module",
                str(task.strategy_module),
                "--task",
                str(task_file),
                "--stage",
                "test",
                "--metrics-path",
                metrics_relative,
            ]
            if task.relative_period_config is not None:
                command.extend(
                    ["--resolved-periods", str(manager.resolved_periods_path)]
                )
        else:
            command = [
                part.format_map(values)
                for part in task.raw["commands"]["backtest"]
            ]

        timeout_seconds = task.round_timeout_minutes * 60
        try:
            completed = subprocess.run(
                command,
                cwd=evaluator,
                env=workspace_python_env(evaluator),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            captured = exc.stdout or ""
            if isinstance(captured, bytes):
                captured = captured.decode("utf-8", errors="replace")
            detail = f"\nChampion Test timed out after {timeout_seconds} seconds\n"
            (output / "test.log").write_text(
                str(captured) + detail,
                encoding="utf-8",
            )
            raise RuntimeError(
                f"Champion Test evaluation timed out after {timeout_seconds} seconds"
            ) from exc
        (output / "test.log").write_text(completed.stdout, encoding="utf-8")
        metrics_path = Path(metrics_relative)
        if not metrics_path.is_absolute():
            metrics_path = evaluator / metrics_path
        if completed.returncode != 0:
            raise RuntimeError(
                f"Champion Test evaluation exited with {completed.returncode}"
            )
        if not metrics_path.is_file():
            raise RuntimeError("Champion Test evaluation did not write metrics")
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        result_path = manager.run_test_result_path
        write_json_atomic(
            result_path,
            {
                "schema_version": 1,
                "run": manager.run_id,
                "task_fingerprint": loop_state.get("task_fingerprint"),
                "strategy_sha256": champion_sha256,
                "champion_round_id": loop_state.get("final_champion_round_id"),
                "evaluation_environment_sha256": loop_state.get(
                    "evaluation_environment_sha256"
                ),
                "runtime_inputs": runtime_inputs,
                "evaluation_inputs_sha256": loop_state.get(
                    "evaluation_inputs_sha256"
                ),
                "test_period": dict(test),
                "period_resolution": (
                    dict(task.period_resolution)
                    if task.period_resolution is not None
                    else None
                ),
                "metrics": metrics,
            },
        )
        return result_path
    finally:
        manager.remove_evaluator(evaluator)
