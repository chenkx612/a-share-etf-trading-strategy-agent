"""Crash-safe Champion promotion transactions."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from quant_core.research.git_utils import git
from quant_core.research.storage import file_sha256, write_json_atomic


class PromotionWorkspace(Protocol):
    source: Path
    state_path: Path
    champion_path: Path
    champion_next_path: Path

    def metrics_applicability(
        self,
        state: Mapping[str, Any],
        metrics_key: str,
        evaluator_contract_paths: Sequence[str],
        *,
        champion_sha256: str | None = None,
        evaluator_contract_sha256: str | None = None,
    ) -> dict[str, str | None]: ...

    @staticmethod
    def metrics_record(
        metrics: Mapping[str, Any],
        applicability: Mapping[str, str | None],
        evaluated_in_round: str,
    ) -> dict[str, Any]: ...

    def record_state(
        self,
        state: dict[str, Any],
        round_id: str,
        champion_metrics_record: Mapping[str, Any] | None = None,
    ) -> None: ...

    def _remove_worktree(self, path: Path) -> None: ...


def recover_promotion(
    workspace: PromotionWorkspace,
    state: dict[str, Any],
) -> dict[str, Any]:
    pending = state.get("pending_promotion")
    if not isinstance(pending, dict):
        return state
    target_sha256 = str(pending["sha256"])
    if (
        workspace.champion_next_path.is_file()
        and file_sha256(workspace.champion_next_path) == target_sha256
    ):
        os.replace(workspace.champion_next_path, workspace.champion_path)
    if (
        workspace.champion_path.is_file()
        and file_sha256(workspace.champion_path) == target_sha256
    ):
        state["champion_sha256"] = target_sha256
        state["champion_number"] = int(pending["champion_number"])
        if state.get("schema_version") == 4:
            state["champion_metrics"] = pending["metrics"]
        else:
            metrics_record = pending.get("metrics_record")
            if not isinstance(metrics_record, dict) and isinstance(
                pending.get("metrics"), dict
            ):
                metrics_record = {
                    "metrics": dict(pending["metrics"]),
                    "status": "stale",
                    "stale_reasons": ["legacy_missing_applicability"],
                    "evaluated_in_round": str(pending["round_id"]),
                    "evaluated_at": None,
                    "applicability": None,
                }
            state["champion_metrics_record"] = metrics_record
        state["champion_round_id"] = str(pending["round_id"])
        state["last_round_id"] = str(pending["round_id"])
        state["project_revision"] = str(pending["project_revision"])
        state["champion_fixed_test_record"] = None
        state["champion_guard_evidence_sha256"] = pending.get(
            "guard_evidence_sha256"
        )
    workspace.champion_next_path.unlink(missing_ok=True)
    state["pending_promotion"] = None
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(workspace.state_path, state)
    return state


def promote_candidate(
    workspace: PromotionWorkspace,
    candidate: Path,
    state: dict[str, Any],
    round_id: str,
    metrics: Mapping[str, Any],
    editable: Sequence[str],
    metrics_key: str,
    evaluator_contract_paths: Sequence[str],
    evaluator_contract_sha256: str,
    *,
    guard_evidence_sha256: str | None = None,
) -> str:
    strategy_path = str(state["strategy_path"])
    if list(editable) != [strategy_path]:
        raise ValueError("editable strategy path does not match champion.json")
    candidate_strategy = candidate / strategy_path
    if not candidate_strategy.is_file():
        raise FileNotFoundError(
            f"Candidate strategy does not exist: {candidate_strategy}"
        )
    workspace.champion_next_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate_strategy, workspace.champion_next_path)
    sha256 = file_sha256(workspace.champion_next_path)
    number = int(state["champion_number"]) + 1
    applicability = workspace.metrics_applicability(
        state,
        metrics_key,
        evaluator_contract_paths,
        champion_sha256=sha256,
        evaluator_contract_sha256=evaluator_contract_sha256,
    )
    metrics_record = workspace.metrics_record(metrics, applicability, round_id)
    state["pending_promotion"] = {
        "round_id": round_id,
        "sha256": sha256,
        "champion_number": number,
        "metrics_record": metrics_record,
        "project_revision": git(workspace.source, "rev-parse", "HEAD"),
        "guard_evidence_sha256": guard_evidence_sha256,
    }
    write_json_atomic(workspace.state_path, state)
    os.replace(workspace.champion_next_path, workspace.champion_path)
    state["champion_sha256"] = sha256
    state["champion_number"] = number
    state["champion_round_id"] = round_id
    state["champion_fixed_test_record"] = None
    state["champion_guard_evidence_sha256"] = guard_evidence_sha256
    state["project_revision"] = state["pending_promotion"]["project_revision"]
    state["pending_promotion"] = None
    workspace.record_state(state, round_id, metrics_record)
    workspace._remove_worktree(candidate)
    return sha256
