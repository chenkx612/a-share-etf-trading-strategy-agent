from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IGNORED_NAMES = {".git", ".research", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_RUNTIME_ROOTS = {"data", "outputs"}


def _copy_tree(source: Path, destination: Path, extra_ignored: set[str] | None = None) -> None:
    ignored = _IGNORED_NAMES | (extra_ignored or set())

    def ignore(directory: str, names: list[str]) -> set[str]:
        excluded = {name for name in names if name in ignored or name.endswith(".pyc")}
        if Path(directory).resolve() == source.resolve():
            excluded.update(name for name in names if name in _RUNTIME_ROOTS)
        return excluded

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)


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


def _files(root: Path, prefixes: Sequence[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for prefix in prefixes:
        target = root / prefix.rstrip("/")
        if target.is_file():
            found[target.relative_to(root).as_posix()] = target
        elif target.is_dir():
            for path in target.rglob("*"):
                if path.is_file():
                    found[path.relative_to(root).as_posix()] = path
    return found


def write_patch(base: Path, candidate: Path, editable: Sequence[str], destination: Path) -> None:
    before = _files(base, editable)
    after = _files(candidate, editable)
    lines: list[str] = []
    for relative in sorted(before.keys() | after.keys()):
        old = before.get(relative)
        new = after.get(relative)
        try:
            old_lines = old.read_text(encoding="utf-8").splitlines(keepends=True) if old else []
            new_lines = new.read_text(encoding="utf-8").splitlines(keepends=True) if new else []
        except UnicodeDecodeError:
            lines.append(f"Binary file changed: {relative}\n")
            continue
        lines.extend(difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{relative}" if old else "/dev/null",
            tofile=f"b/{relative}" if new else "/dev/null",
        ))
    destination.write_text("".join(lines), encoding="utf-8")


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
        if self.research_root == self.source:
            raise ValueError("research root must not be the source workspace")
        if self.source in self.research_root.parents:
            return
        if self.research_root in self.source.parents:
            raise ValueError("research root must not contain the source workspace")

    @property
    def root(self) -> Path:
        return self.research_root / self.task_id

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def candidates(self) -> Path:
        return self.root / "candidates"

    @property
    def experiments(self) -> Path:
        return self.root / "experiments"

    @property
    def versions(self) -> Path:
        return self.root / "versions"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def development_runtime(self) -> Path:
        return self.runtime / "development"

    @property
    def evaluation_runtime(self) -> Path:
        return self.runtime / "evaluation"

    def _recover_promotion(self, state: dict[str, Any]) -> dict[str, Any]:
        pending = state.get("pending_promotion")
        if not isinstance(pending, dict):
            return state
        destination = self.root / str(pending["champion"])
        temporary = self.root / str(pending["temporary"])
        if destination.exists():
            state["champion"] = str(pending["champion"])
            state["champion_number"] = int(pending["champion_number"])
            state["champion_metrics"] = pending["metrics"]
            state["last_experiment_id"] = str(pending["experiment_id"])
        else:
            shutil.rmtree(temporary, ignore_errors=True)
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

    def initialize(self, development_end: date | None = None) -> dict[str, Any]:
        if self.state_path.exists():
            state = self._recover_promotion(self.load_state())
            self._prepare_runtime(state, development_end)
            return state
        self.versions.mkdir(parents=True, exist_ok=True)
        self.candidates.mkdir(parents=True, exist_ok=True)
        self.experiments.mkdir(parents=True, exist_ok=True)
        baseline = self.versions / "baseline"
        extra_ignored: set[str] = set()
        if self.source in self.research_root.parents:
            extra_ignored.add(self.research_root.relative_to(self.source).parts[0])
        _copy_tree(self.source, baseline, extra_ignored)
        state: dict[str, Any] = {
            "schema_version": 1,
            "task_id": self.task_id,
            "baseline": "versions/baseline",
            "champion": "versions/baseline",
            "champion_number": 0,
            "champion_metrics": None,
            "champion_metrics_key": None,
            "last_experiment_id": None,
            "pending_promotion": None,
        }
        write_json_atomic(self.state_path, state)
        self._prepare_runtime(state, development_end)
        return state

    def load_state(self) -> dict[str, Any]:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("task_id") != self.task_id:
            raise ValueError("research workspace task id does not match task.toml")
        return state

    def champion_path(self, state: Mapping[str, Any] | None = None) -> Path:
        current = state or self.load_state()
        return self.root / str(current["champion"])

    def create_candidate(
        self,
        experiment_id: str,
        development_end: date | None = None,
    ) -> tuple[Path, Path, dict[str, Any]]:
        if not _SAFE_TASK_ID.fullmatch(experiment_id):
            raise ValueError("experiment id may contain only letters, numbers, '.', '_' and '-'")
        state = self.initialize(development_end)
        # Parallel workers are outside the MVP. Anything left here is from an interrupted run.
        for stale in self.candidates.iterdir():
            if stale.is_dir():
                shutil.rmtree(stale)
            else:
                stale.unlink()
        for stale in self.versions.glob(".champion-*.tmp"):
            shutil.rmtree(stale, ignore_errors=True)
        candidate = self.candidates / experiment_id
        experiment = self.experiments / experiment_id
        if candidate.exists() or experiment.exists():
            raise FileExistsError(f"Experiment already exists: {experiment_id}")
        _copy_tree(self.champion_path(state), candidate)
        copy_runtime_inputs(self.development_runtime, candidate)
        experiment.mkdir(parents=True)
        return candidate, experiment, state

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
    ) -> Path:
        number = int(state["champion_number"]) + 1
        destination = self.versions / f"champion-{number:03d}"
        temporary = self.versions / f".champion-{number:03d}.tmp"
        state["pending_promotion"] = {
            "experiment_id": experiment_id,
            "champion": destination.relative_to(self.root).as_posix(),
            "temporary": temporary.relative_to(self.root).as_posix(),
            "champion_number": number,
            "metrics": dict(metrics),
        }
        write_json_atomic(self.state_path, state)
        _copy_tree(candidate, temporary)
        os.replace(temporary, destination)
        state["champion"] = destination.relative_to(self.root).as_posix()
        state["champion_number"] = number
        state["pending_promotion"] = None
        self.record_state(state, experiment_id, metrics)
        shutil.rmtree(candidate)
        return destination

    def reject(self, candidate: Path, state: dict[str, Any], experiment_id: str) -> None:
        self.record_state(state, experiment_id)
        shutil.rmtree(candidate, ignore_errors=True)
