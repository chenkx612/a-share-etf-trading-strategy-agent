"""Compatibility migrations for historical Research Harness layouts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Protocol

from quant_core.research.git_utils import git
from quant_core.research.storage import file_sha256, write_json_atomic


class MigrationWorkspace(Protocol):
    root: Path
    state_path: Path
    legacy_state_path: Path
    legacy_experiments: Path
    champion_path: Path
    source: Path
    task_id: str

    @property
    def run_id(self) -> str: ...

    @property
    def run_root(self) -> Path: ...

    @property
    def rounds(self) -> Path: ...

    @property
    def report_path(self) -> Path: ...

    @property
    def loop_state_path(self) -> Path: ...

    def next_run_number(self) -> int: ...

    def for_run(self, run_number: int) -> MigrationWorkspace: ...

    def _recover_promotion(self, state: dict[str, Any]) -> dict[str, Any]: ...

    def _strategy_path(
        self,
        state: dict[str, Any],
        strategy_path: str | None,
    ) -> str: ...

    def _extract_champion(self, commit: str, strategy_path: str) -> bool: ...

    def _latest_accepted_round(self) -> str | None: ...


def migrate_legacy_loop(workspace: MigrationWorkspace) -> int | None:
    legacy_loop_state = workspace.root / "loop-state.json"
    if not legacy_loop_state.exists():
        return None
    state = json.loads(legacy_loop_state.read_text(encoding="utf-8"))
    legacy_experiments = workspace.legacy_experiments
    available = (
        sorted(path.name for path in legacy_experiments.iterdir() if path.is_dir())
        if legacy_experiments.exists()
        else []
    )
    configured = state.get("round_ids", state.get("experiment_ids"))
    if isinstance(configured, list):
        selected = [
            item
            for item in configured
            if isinstance(item, str) and item in available
        ]
    else:
        rounds = max(0, int(state.get("rounds_completed", 0)))
        selected = available[-rounds:] if rounds else []
    current = state.get("current_round", state.get("current_experiment_id"))
    if isinstance(current, str) and current in available and current not in selected:
        selected.append(current)

    run_number = workspace.next_run_number()
    bound = workspace.for_run(run_number)
    (bound.run_root / "rounds").mkdir(parents=True, exist_ok=False)
    mapping: dict[str, str] = {}
    for index, legacy_id in enumerate(selected, start=1):
        round_id = f"{index:03d}"
        mapping[legacy_id] = round_id
        destination = bound.rounds / round_id
        shutil.move(str(legacy_experiments / legacy_id), str(destination))
        for name in ("result.json", "decision.json"):
            path = destination / name
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            payload["experiment_id"] = f"{bound.run_id}/{round_id}"
            payload["run_number"] = run_number
            payload["round_number"] = index
            write_json_atomic(path, payload)

    state["schema_version"] = 2
    state["run_number"] = run_number
    state["round_ids"] = [mapping[item] for item in selected if item in mapping]
    current_value = state.get("current_round", state.get("current_experiment_id"))
    last_value = state.get("last_round", state.get("last_experiment_id"))
    state["current_round"] = (
        mapping.get(current_value) if isinstance(current_value, str) else None
    )
    state["last_round"] = (
        mapping.get(last_value) if isinstance(last_value, str) else None
    )
    for legacy_key in (
        "experiment_ids",
        "current_experiment_id",
        "last_experiment_id",
    ):
        state.pop(legacy_key, None)
    report_path = workspace.root / "loop-report.md"
    if report_path.exists():
        shutil.move(str(report_path), str(bound.report_path))
        state["report_path"] = "report.md"
    write_json_atomic(bound.loop_state_path, state)
    if workspace.state_path.exists():
        champion_state = json.loads(workspace.state_path.read_text(encoding="utf-8"))
        if isinstance(state.get("last_round"), str):
            champion_state["last_round_id"] = (
                f"{bound.run_id}/{state['last_round']}"
            )
        accepted_rounds: list[str] = []
        for round_id in mapping.values():
            decision_path = bound.rounds / round_id / "decision.json"
            try:
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if decision.get("decision") == "accepted":
                accepted_rounds.append(round_id)
        if accepted_rounds:
            champion_state["champion_round_id"] = (
                f"{bound.run_id}/{accepted_rounds[-1]}"
            )
        write_json_atomic(workspace.state_path, champion_state)
    legacy_loop_state.unlink()
    try:
        legacy_experiments.rmdir()
    except OSError:
        pass
    return run_number


def migrate_champion_layout(
    workspace: MigrationWorkspace,
    strategy_path: str | None = None,
) -> None:
    source = (
        workspace.state_path
        if workspace.state_path.exists()
        else workspace.legacy_state_path
    )
    if not source.exists():
        return
    state = json.loads(source.read_text(encoding="utf-8"))
    schema_version = state.get("schema_version")
    if schema_version in {7, 8, 9} and source == workspace.state_path:
        changed = False
        if "last_experiment_id" in state and "last_round_id" not in state:
            state["last_round_id"] = state.pop("last_experiment_id")
            changed = True
        if schema_version == 7:
            state["schema_version"] = 9
            state["champion_fixed_test_record"] = None
            state["champion_guard_evidence_sha256"] = None
            changed = True
        if schema_version == 8:
            state["schema_version"] = 9
            state["champion_guard_evidence_sha256"] = None
            changed = True
        if "champion_guard_evidence_sha256" not in state:
            state["champion_guard_evidence_sha256"] = None
            changed = True
        if changed:
            write_json_atomic(workspace.state_path, state)
        return
    if schema_version not in {2, 3, 4, 5, 6}:
        raise ValueError("research workspace uses an incompatible champion schema")
    if schema_version in {4, 5} and isinstance(state.get("pending_promotion"), dict):
        state = workspace._recover_promotion(state)
    if schema_version in {5, 6}:
        metrics_record = state.get("champion_metrics_record")
        if isinstance(metrics_record, dict):
            reasons = metrics_record.get("stale_reasons")
            stale_reasons = list(reasons) if isinstance(reasons, list) else []
            if (
                schema_version == 5
                and "legacy_missing_evaluation_environment" not in stale_reasons
            ):
                stale_reasons.append("legacy_missing_evaluation_environment")
            if (
                schema_version == 6
                and "legacy_missing_development_view" not in stale_reasons
            ):
                stale_reasons.append("legacy_missing_development_view")
            metrics_record["status"] = "stale"
            metrics_record["stale_reasons"] = stale_reasons
        state["schema_version"] = 9
        state["champion_fixed_test_record"] = None
        state["champion_guard_evidence_sha256"] = None
        state["development_view_sha256"] = None
        state["development_end"] = None
        write_json_atomic(workspace.state_path, state)
        if source != workspace.state_path:
            source.unlink()
        return
    editable = workspace._strategy_path(state, strategy_path)
    champion_commit = state.get("champion_commit")
    champion_sha256 = (
        str(state["champion_sha256"])
        if isinstance(state.get("champion_sha256"), str)
        else None
    )
    if champion_sha256 is None and isinstance(champion_commit, str):
        if not workspace._extract_champion(champion_commit, editable):
            raise ValueError("legacy Champion strategy cannot be read from its Git commit")
        champion_sha256 = file_sha256(workspace.champion_path)
    legacy_metrics = state.get("champion_metrics")
    metrics_record = None
    if isinstance(legacy_metrics, dict):
        metrics_record = {
            "metrics": legacy_metrics,
            "status": "stale",
            "stale_reasons": ["legacy_missing_applicability"],
            "evaluated_in_round": None,
            "evaluated_at": state.get("updated_at"),
            "applicability": None,
        }
    champion_round_id = state.get("champion_round_id")
    if champion_round_id is None:
        champion_round_id = workspace._latest_accepted_round()
    migrated = {
        "schema_version": 9,
        "task_id": workspace.task_id,
        "baseline_mode": state.get("baseline_mode", "workspace"),
        "baseline_exclude": list(state.get("baseline_exclude", [])),
        "strategy_path": editable,
        "project_revision": git(workspace.source, "rev-parse", "HEAD"),
        "champion_number": int(state.get("champion_number", 0)),
        "champion_sha256": champion_sha256,
        "champion_round_id": champion_round_id,
        "champion_metrics_record": metrics_record,
        "champion_fixed_test_record": None,
        "champion_guard_evidence_sha256": None,
        "last_round_id": state.get("last_round_id", state.get("last_experiment_id")),
        "pending_promotion": None,
        "development_view_sha256": None,
        "development_end": state.get("development_end"),
        "updated_at": state.get("updated_at"),
    }
    write_json_atomic(workspace.state_path, migrated)
    if source != workspace.state_path:
        source.unlink()
