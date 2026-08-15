"""Filesystem layout for task-, Run-, and Round-scoped research artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class WorkspaceLayout:
    source: Path
    research_root: Path
    task_id: str
    run_number: int | None = None
    evaluation_environment_sha256: str | None = None
    diagnostics_enabled: bool = False
    evaluation_runtime_override: Path | None = None

    def __post_init__(self) -> None:
        self.source = self.source.resolve()
        self.research_root = self.research_root.resolve()
        if not _SAFE_TASK_ID.fullmatch(self.task_id):
            raise ValueError("task.id may contain only letters, numbers, '.', '_' and '-'")
        if self.run_number is not None and self.run_number < 1:
            raise ValueError("run number must be positive")
        digest = self.evaluation_environment_sha256
        if digest is not None and (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("evaluation environment must be a SHA-256 digest")
        if self.research_root == self.source:
            raise ValueError("research root must not be the source workspace")
        if (
            self.source not in self.research_root.parents
            and self.research_root in self.source.parents
        ):
            raise ValueError("research root must not contain the source workspace")

    @property
    def root(self) -> Path:
        return self.research_root / self.task_id

    @property
    def state_path(self) -> Path:
        return self.root / "champion.json"

    @property
    def legacy_state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def champion_path(self) -> Path:
        return self.root / "champion.py"

    @property
    def champion_next_path(self) -> Path:
        return self.root / ".tmp" / "champion.next.py"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def run_id(self) -> str:
        if self.run_number is None:
            raise RuntimeError("research workspace is not bound to a run")
        return f"{self.run_number:03d}"

    @property
    def run_root(self) -> Path:
        return self.runs / self.run_id

    @property
    def uses_legacy_run_layout(self) -> bool:
        return not (self.run_root / "artifacts").exists() and (
            (self.run_root / "state.json").exists()
            or (self.run_root / "rounds").exists()
        )

    @property
    def run_artifacts_root(self) -> Path:
        return self.run_root if self.uses_legacy_run_layout else self.run_root / "artifacts"

    @property
    def run_contracts_root(self) -> Path:
        return self.run_root if self.uses_legacy_run_layout else self.run_artifacts_root / "contracts"

    @property
    def run_report_root(self) -> Path:
        return self.run_root if self.uses_legacy_run_layout else self.run_artifacts_root / "report"

    @property
    def loop_state_path(self) -> Path:
        return self.run_artifacts_root / "state.json"

    @property
    def report_path(self) -> Path:
        return self.run_root / "report.md"

    @property
    def report_facts_path(self) -> Path:
        return (
            self.run_root / "report-facts.json"
            if self.uses_legacy_run_layout
            else self.run_report_root / "facts.json"
        )

    @property
    def report_input_path(self) -> Path:
        return (
            self.run_root / "report-input.json"
            if self.uses_legacy_run_layout
            else self.run_report_root / "input.json"
        )

    @property
    def report_events_path(self) -> Path:
        return (
            self.run_root / "report-events.jsonl"
            if self.uses_legacy_run_layout
            else self.run_report_root / "events.jsonl"
        )

    @property
    def production_sync_path(self) -> Path:
        return self.run_artifacts_root / "production-sync.json"

    @property
    def production_sync_champion_path(self) -> Path:
        return self.run_artifacts_root / "production-sync-champion.py"

    @property
    def run_temp(self) -> Path:
        return self.root / ".tmp" / "runs" / self.run_id

    @property
    def terminal_champion_path(self) -> Path:
        return self.run_temp / "final-champion.py"

    @property
    def event_path(self) -> Path:
        return self.run_temp / "events.jsonl"

    @property
    def diagnostics_root(self) -> Path:
        if self.run_number is None:
            raise RuntimeError("research workspace is not bound to a run")
        return (
            self.root / ".cache" / "diagnostics" / self.run_id
            if self.uses_legacy_run_layout
            else self.run_artifacts_root / "diagnostics"
        )

    @property
    def diagnostics_event_path(self) -> Path:
        return self.diagnostics_root / "events.jsonl"

    @property
    def candidates(self) -> Path:
        return self.root / ".tmp" / "worktrees" / self.run_id / "candidates"

    @property
    def evaluators(self) -> Path:
        return self.root / ".tmp" / "worktrees" / self.run_id / "evaluators"

    @property
    def test_evaluators(self) -> Path:
        return self.root / ".tmp" / "worktrees" / "tests"

    @property
    def rounds(self) -> Path:
        return self.run_artifacts_root / "rounds"

    @property
    def legacy_experiments(self) -> Path:
        return self.root / "experiments"

    @property
    def runtime(self) -> Path:
        return self.root / ".cache" / "runtime"

    @property
    def development_views(self) -> Path:
        return self.runtime / "development-views"

    @property
    def evaluation_views(self) -> Path:
        return self.runtime / "evaluation-views"

    @property
    def development_inputs_path(self) -> Path:
        return self.run_contracts_root / "development-inputs.json"

    @property
    def evaluation_inputs_path(self) -> Path:
        return self.run_contracts_root / "evaluation-inputs.json"

    @property
    def resolved_periods_path(self) -> Path:
        return self.run_contracts_root / "resolved-periods.json"

    @property
    def preflight_failures_root(self) -> Path:
        return self.run_artifacts_root / "preflight-failures"

    @property
    def run_diagnostics_root(self) -> Path:
        return self.run_artifacts_root / "diagnostics"
