from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Quant Research Harness",
    "GIT_AUTHOR_EMAIL": "quant-research@example.invalid",
    "GIT_COMMITTER_NAME": "Quant Research Harness",
    "GIT_COMMITTER_EMAIL": "quant-research@example.invalid",
}


def _git(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _filter_tables(root: Path, end: date) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".csv", ".parquet"}:
            continue
        try:
            frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
        except (OSError, ValueError):
            continue
        if "date" not in frame.columns:
            continue
        dates = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame[dates.dt.date <= end]
        if path.suffix == ".parquet":
            frame.to_parquet(path, index=False)
        else:
            frame.to_csv(path, index=False)


def remove_runtime_inputs(workspace: Path) -> None:
    shutil.rmtree(workspace / "data", ignore_errors=True)
    shutil.rmtree(workspace / "outputs" / "factors", ignore_errors=True)


def copy_runtime_inputs(source: Path, destination: Path, *, end: date | None = None) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    data = source / "data"
    if data.is_dir():
        shutil.copytree(data, destination / "data", symlinks=True)
    factors = source / "outputs" / "factors"
    if factors.is_dir():
        (destination / "outputs").mkdir(exist_ok=True)
        shutil.copytree(factors, destination / "outputs" / "factors", symlinks=True)
    if end is not None:
        _filter_tables(destination / "data", end)
        _filter_tables(destination / "outputs" / "factors", end)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class ResearchWorkspace:
    source: Path
    research_root: Path
    task_id: str

    def __post_init__(self) -> None:
        self.source = self.source.resolve()
        self.research_root = self.research_root.resolve()
        if not _SAFE_TASK_ID.fullmatch(self.task_id):
            raise ValueError("task.id may contain only letters, numbers, '.', '_' and '-'")
        repository = Path(_git(self.source, "rev-parse", "--show-toplevel")).resolve()
        if repository != self.source:
            raise ValueError("research workspace must be the Git repository root")
        if self.research_root == self.source:
            raise ValueError("research root must not be the source workspace")
        if self.source not in self.research_root.parents and self.research_root in self.source.parents:
            raise ValueError("research root must not contain the source workspace")

    @property
    def root(self) -> Path:
        return self.research_root / self.task_id

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def candidates(self) -> Path:
        return self.root / "worktrees" / "candidates"

    @property
    def evaluators(self) -> Path:
        return self.root / "worktrees" / "evaluators"

    @property
    def experiments(self) -> Path:
        return self.root / "experiments"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def development_runtime(self) -> Path:
        return self.runtime / "development"

    @property
    def evaluation_runtime(self) -> Path:
        return self.runtime / "evaluation"

    @property
    def champion_ref(self) -> str:
        digest = hashlib.sha256(self.task_id.encode()).hexdigest()[:12]
        return f"refs/quant-research/{digest}/champion"

    @property
    def seed_ref(self) -> str:
        digest = hashlib.sha256(self.task_id.encode()).hexdigest()[:12]
        return f"refs/quant-research/{digest}/seed"

    def _snapshot_commit(self, excluded_paths: Sequence[str]) -> str:
        excluded = [*excluded_paths, "data", "outputs"]
        if self.source in self.research_root.parents:
            excluded.append(self.research_root.relative_to(self.source).as_posix())
        with tempfile.TemporaryDirectory(prefix="quant-index-") as temporary:
            index = Path(temporary) / "index"
            env = {"GIT_INDEX_FILE": str(index), **_GIT_IDENTITY}
            _git(self.source, "read-tree", "HEAD", env=env)
            _git(self.source, "add", "-A", env=env)
            for path in excluded:
                _git(
                    self.source,
                    "rm",
                    "-r",
                    "--cached",
                    "--ignore-unmatch",
                    "--",
                    path.rstrip("/"),
                    env=env,
                )
            tree = _git(self.source, "write-tree", env=env)
            parent = _git(self.source, "rev-parse", "HEAD")
            return _git(
                self.source,
                "commit-tree",
                tree,
                "-p",
                parent,
                "-m",
                f"Research seed for {self.task_id}",
                env=env,
            )

    def _add_worktree(self, path: Path, commit: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(self.source, "worktree", "add", "--detach", str(path), commit)

    def _remove_worktree(self, path: Path) -> None:
        if path.exists():
            if (path / ".git").exists():
                _git(self.source, "worktree", "remove", "--force", str(path))
            else:
                shutil.rmtree(path)
        _git(self.source, "worktree", "prune")

    def _cleanup_worktrees(self, root: Path) -> None:
        if not root.exists():
            return
        for path in list(root.iterdir()):
            if path.is_dir():
                self._remove_worktree(path)
            else:
                path.unlink()

    def _recover_promotion(self, state: dict[str, Any]) -> dict[str, Any]:
        pending = state.get("pending_promotion")
        if not isinstance(pending, dict):
            return state
        commit = str(pending["commit"])
        try:
            _git(self.source, "cat-file", "-e", f"{commit}^{{commit}}")
        except RuntimeError:
            pass
        else:
            _git(self.source, "update-ref", self.champion_ref, commit)
            state["champion_commit"] = commit
            state["champion_number"] = int(pending["champion_number"])
            state["champion_metrics"] = pending["metrics"]
            state["last_experiment_id"] = str(pending["experiment_id"])
        state["pending_promotion"] = None
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(self.state_path, state)
        return state

    def _prepare_runtime(self, state: dict[str, Any], development_end: date | None) -> None:
        if not self.evaluation_runtime.exists():
            copy_runtime_inputs(self.source, self.evaluation_runtime)
        expected = development_end.isoformat() if development_end else None
        if not self.development_runtime.exists() or state.get("development_end") != expected:
            shutil.rmtree(self.development_runtime, ignore_errors=True)
            copy_runtime_inputs(self.evaluation_runtime, self.development_runtime, end=development_end)
            state["development_end"] = expected
            state["champion_metrics"] = None
            state["champion_metrics_key"] = None
            write_json_atomic(self.state_path, state)

    def initialize(
        self,
        development_end: date | None = None,
        baseline_mode: str = "workspace",
        baseline_exclude: Sequence[str] = (),
    ) -> dict[str, Any]:
        if self.state_path.exists():
            state = self._recover_promotion(self.load_state())
            if state.get("baseline_mode", "workspace") != baseline_mode:
                raise ValueError("task baseline mode changed after research workspace initialization")
            if state.get("baseline_exclude", []) != list(baseline_exclude):
                raise ValueError("task baseline exclusions changed after research workspace initialization")
            _git(self.source, "update-ref", self.seed_ref, str(state["seed_commit"]))
            self._prepare_runtime(state, development_end)
            return state

        self.candidates.mkdir(parents=True, exist_ok=True)
        self.evaluators.mkdir(parents=True, exist_ok=True)
        self.experiments.mkdir(parents=True, exist_ok=True)
        seed_commit = self._snapshot_commit(baseline_exclude)
        state: dict[str, Any] = {
            "schema_version": 2,
            "task_id": self.task_id,
            "baseline_mode": baseline_mode,
            "baseline_exclude": list(baseline_exclude),
            "seed_commit": seed_commit,
            "seed_ref": self.seed_ref,
            "champion_commit": seed_commit if baseline_mode == "workspace" else None,
            "champion_ref": self.champion_ref,
            "champion_number": 0,
            "champion_metrics": None,
            "champion_metrics_key": None,
            "last_experiment_id": None,
            "pending_promotion": None,
        }
        if baseline_mode == "workspace":
            _git(self.source, "update-ref", self.champion_ref, seed_commit)
        _git(self.source, "update-ref", self.seed_ref, seed_commit)
        write_json_atomic(self.state_path, state)
        self._prepare_runtime(state, development_end)
        return state

    def load_state(self) -> dict[str, Any]:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("task_id") != self.task_id:
            raise ValueError("research workspace task id does not match task.toml")
        if state.get("schema_version") != 2:
            raise ValueError("research workspace uses an incompatible pre-worktree schema")
        return state

    def candidate_base_commit(self, state: Mapping[str, Any] | None = None) -> str:
        current = state or self.load_state()
        champion = current.get("champion_commit")
        return str(champion if isinstance(champion, str) else current["seed_commit"])

    def create_candidate(
        self,
        experiment_id: str,
        development_end: date | None = None,
        baseline_mode: str = "workspace",
        baseline_exclude: Sequence[str] = (),
    ) -> tuple[Path, Path, dict[str, Any]]:
        if not _SAFE_TASK_ID.fullmatch(experiment_id):
            raise ValueError("experiment id may contain only letters, numbers, '.', '_' and '-'")
        state = self.initialize(development_end, baseline_mode, baseline_exclude)
        self._cleanup_worktrees(self.candidates)
        candidate = self.candidates / experiment_id
        experiment = self.experiments / experiment_id
        if candidate.exists() or experiment.exists():
            raise FileExistsError(f"Experiment already exists: {experiment_id}")
        self._add_worktree(candidate, self.candidate_base_commit(state))
        copy_runtime_inputs(self.development_runtime, candidate)
        experiment.mkdir(parents=True)
        return candidate, experiment, state

    def create_champion_evaluator(self, experiment_id: str, state: Mapping[str, Any]) -> Path:
        commit = state.get("champion_commit")
        if not isinstance(commit, str):
            raise RuntimeError("research task does not have a champion yet")
        self._cleanup_worktrees(self.evaluators)
        evaluator = self.evaluators / experiment_id
        self._add_worktree(evaluator, commit)
        return evaluator

    def remove_evaluator(self, evaluator: Path) -> None:
        self._remove_worktree(evaluator)

    def write_candidate_patch(
        self,
        candidate: Path,
        state: Mapping[str, Any],
        editable: Sequence[str],
        destination: Path,
    ) -> None:
        patch = _git(
            candidate,
            "diff",
            "--binary",
            self.candidate_base_commit(state),
            "--",
            *editable,
        )
        destination.write_text(patch + ("\n" if patch else ""), encoding="utf-8")

    def record_state(
        self,
        state: dict[str, Any],
        experiment_id: str,
        champion_metrics: Mapping[str, Any] | None = None,
    ) -> None:
        state["last_experiment_id"] = experiment_id
        if champion_metrics is not None:
            state["champion_metrics"] = dict(champion_metrics)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(self.state_path, state)

    def promote(
        self,
        candidate: Path,
        state: dict[str, Any],
        experiment_id: str,
        metrics: Mapping[str, Any],
        editable: Sequence[str],
    ) -> str:
        _git(candidate, "add", "-A", "--", *editable)
        _git(
            candidate,
            "-c",
            f"user.name={_GIT_IDENTITY['GIT_AUTHOR_NAME']}",
            "-c",
            f"user.email={_GIT_IDENTITY['GIT_AUTHOR_EMAIL']}",
            "commit",
            "-m",
            f"Research {self.task_id}: {experiment_id}",
            env=_GIT_IDENTITY,
        )
        commit = _git(candidate, "rev-parse", "HEAD")
        number = int(state["champion_number"]) + 1
        state["pending_promotion"] = {
            "experiment_id": experiment_id,
            "commit": commit,
            "champion_number": number,
            "metrics": dict(metrics),
        }
        write_json_atomic(self.state_path, state)
        _git(self.source, "update-ref", self.champion_ref, commit)
        state["champion_commit"] = commit
        state["champion_number"] = number
        state["pending_promotion"] = None
        self.record_state(state, experiment_id, metrics)
        self._remove_worktree(candidate)
        return commit

    def reject(self, candidate: Path, state: dict[str, Any], experiment_id: str) -> None:
        self.record_state(state, experiment_id)
        self._remove_worktree(candidate)
