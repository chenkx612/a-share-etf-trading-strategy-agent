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
    run_number: int | None = None

    def __post_init__(self) -> None:
        self.source = self.source.resolve()
        self.research_root = self.research_root.resolve()
        if not _SAFE_TASK_ID.fullmatch(self.task_id):
            raise ValueError("task.id may contain only letters, numbers, '.', '_' and '-'")
        if self.run_number is not None and self.run_number < 1:
            raise ValueError("run number must be positive")
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
        return self.root / "champion.json"

    @property
    def legacy_state_path(self) -> Path:
        return self.root / "state.json"

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
    def loop_state_path(self) -> Path:
        return self.run_root / "state.json"

    @property
    def report_path(self) -> Path:
        return self.run_root / "report.md"

    @property
    def run_temp(self) -> Path:
        return self.root / ".tmp" / "runs" / self.run_id

    @property
    def event_path(self) -> Path:
        return self.run_temp / "events.jsonl"

    @property
    def candidates(self) -> Path:
        return self.root / ".tmp" / "worktrees" / self.run_id / "candidates"

    @property
    def evaluators(self) -> Path:
        return self.root / ".tmp" / "worktrees" / self.run_id / "evaluators"

    @property
    def rounds(self) -> Path:
        return self.run_root / "rounds"

    @property
    def legacy_experiments(self) -> Path:
        return self.root / "experiments"

    @property
    def runtime(self) -> Path:
        return self.root / ".cache" / "runtime"

    @property
    def development_runtime(self) -> Path:
        return self.runtime / "development"

    @property
    def evaluation_runtime(self) -> Path:
        return self.runtime / "evaluation"

    @property
    def champion_ref(self) -> str:
        state = self._existing_champion_state()
        if state is not None and isinstance(state.get("champion_ref"), str):
            return str(state["champion_ref"])
        return f"{self._new_ref_namespace()}/champion"

    @property
    def seed_ref(self) -> str:
        state = self._existing_champion_state()
        if state is not None and isinstance(state.get("seed_ref"), str):
            return str(state["seed_ref"])
        return f"{self._new_ref_namespace()}/seed"

    def _existing_champion_state(self) -> dict[str, Any] | None:
        for path in (self.state_path, self.legacy_state_path):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
        return None

    def _new_ref_namespace(self) -> str:
        task_digest = hashlib.sha256(self.task_id.encode()).hexdigest()[:12]
        identity = f"{self.source}\0{self.root}".encode()
        workspace_id = hashlib.sha256(identity).hexdigest()[:12]
        return f"refs/quant-research/{task_digest}/{workspace_id}"

    def for_run(self, run_number: int) -> ResearchWorkspace:
        return ResearchWorkspace(
            self.source,
            self.research_root,
            self.task_id,
            run_number=run_number,
        )

    def next_run_number(self) -> int:
        highest = 0
        if self.runs.exists():
            for path in self.runs.iterdir():
                if path.is_dir() and path.name.isdigit():
                    highest = max(highest, int(path.name))
        return highest + 1

    def run_numbers(self) -> list[int]:
        if not self.runs.exists():
            return []
        return sorted(
            int(path.name)
            for path in self.runs.iterdir()
            if path.is_dir() and path.name.isdigit() and int(path.name) > 0
        )

    def migrate_legacy_loop(self) -> int | None:
        legacy_loop_state = self.root / "loop-state.json"
        if not legacy_loop_state.exists():
            return None
        state = json.loads(legacy_loop_state.read_text(encoding="utf-8"))
        legacy_experiments = self.legacy_experiments
        available = (
            sorted(path.name for path in legacy_experiments.iterdir() if path.is_dir())
            if legacy_experiments.exists()
            else []
        )
        configured = state.get("round_ids", state.get("experiment_ids"))
        if isinstance(configured, list):
            selected = [item for item in configured if isinstance(item, str) and item in available]
        else:
            rounds = max(0, int(state.get("rounds_completed", 0)))
            selected = available[-rounds:] if rounds else []
        current = state.get("current_round", state.get("current_experiment_id"))
        if isinstance(current, str) and current in available and current not in selected:
            selected.append(current)

        run_number = self.next_run_number()
        bound = self.for_run(run_number)
        bound.rounds.mkdir(parents=True, exist_ok=False)
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
        report_path = self.root / "loop-report.md"
        if report_path.exists():
            shutil.move(str(report_path), str(bound.report_path))
            state["report_path"] = "report.md"
        write_json_atomic(bound.loop_state_path, state)
        legacy_loop_state.unlink()
        try:
            legacy_experiments.rmdir()
        except OSError:
            pass
        return run_number

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
        try:
            root.rmdir()
        except OSError:
            pass

    def _migrate_transient_layout(self) -> None:
        legacy_worktrees = self.root / "worktrees"
        for name in ("candidates", "evaluators"):
            legacy = legacy_worktrees / name
            if not legacy.exists():
                continue
            self._cleanup_worktrees(legacy)
        try:
            legacy_worktrees.rmdir()
        except OSError:
            pass

        legacy_runtime = self.root / "runtime"
        if legacy_runtime.exists() and not self.runtime.exists():
            self.runtime.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_runtime), str(self.runtime))

    def _migrate_champion_layout(self) -> None:
        source = self.state_path if self.state_path.exists() else self.legacy_state_path
        if not source.exists():
            return
        state = json.loads(source.read_text(encoding="utf-8"))
        if state.get("schema_version") == 3 and source == self.state_path:
            if "last_experiment_id" in state and "last_round_id" not in state:
                state["last_round_id"] = state.pop("last_experiment_id")
                write_json_atomic(self.state_path, state)
            return
        if state.get("schema_version") != 2:
            raise ValueError("research workspace uses an incompatible champion schema")
        namespace = self._new_ref_namespace()
        state["schema_version"] = 3
        state["workspace_id"] = namespace.rsplit("/", 1)[-1]
        state["seed_ref"] = f"{namespace}/seed"
        state["champion_ref"] = f"{namespace}/champion"
        state["last_round_id"] = state.pop("last_experiment_id", None)
        _git(self.source, "update-ref", str(state["seed_ref"]), str(state["seed_commit"]))
        champion = state.get("champion_commit")
        if isinstance(champion, str):
            _git(self.source, "update-ref", str(state["champion_ref"]), champion)
        write_json_atomic(self.state_path, state)
        if source != self.state_path:
            source.unlink()

    def cleanup_transient(self, *, remove_development_cache: bool = False) -> None:
        """Remove disposable worktrees and optionally the derived development cache."""
        self._migrate_transient_layout()
        if self.run_number is None:
            worktrees = self.root / ".tmp" / "worktrees"
            scopes = list(worktrees.iterdir()) if worktrees.exists() else []
            for scope in scopes:
                if scope.is_dir():
                    self._cleanup_worktrees(scope / "candidates")
                    self._cleanup_worktrees(scope / "evaluators")
                    try:
                        scope.rmdir()
                    except OSError:
                        pass
            shutil.rmtree(self.root / ".tmp" / "runs", ignore_errors=True)
        else:
            self._cleanup_worktrees(self.candidates)
            self._cleanup_worktrees(self.evaluators)
            shutil.rmtree(self.run_temp, ignore_errors=True)
        if remove_development_cache:
            shutil.rmtree(self.development_runtime, ignore_errors=True)
        for path in (
            self.root / ".tmp" / "worktrees",
            self.root / ".tmp" / "runs",
            self.root / ".tmp",
            self.runtime,
            self.root / ".cache",
        ):
            try:
                path.rmdir()
            except OSError:
                pass

    def compact_artifacts(self) -> dict[str, int]:
        """Remove redundant successful-run diagnostics while preserving failures."""
        removed_files = 0
        removed_bytes = 0

        def remove(path: Path) -> None:
            nonlocal removed_files, removed_bytes
            if not path.is_file():
                return
            removed_bytes += path.stat().st_size
            path.unlink()
            removed_files += 1

        experiment_roots = (
            [self.rounds]
            if self.run_number is not None
            else [
                self.legacy_experiments,
                *(self.for_run(number).rounds for number in self.run_numbers()),
            ]
        )
        for experiment_root in experiment_roots:
            if not experiment_root.exists():
                continue
            for experiment in experiment_root.iterdir():
                if not experiment.is_dir():
                    continue
                result_path = experiment / "result.json"
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    result = {}
                agent_output_path = experiment / "agent-output.json"
                had_agent_output = agent_output_path.is_file()
                if had_agent_output and result.get("status") == "failed":
                    try:
                        agent_output = json.loads(agent_output_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        agent_output = {}
                    changed = False
                    for key in ("hypothesis", "attempts", "development_effect", "candidate"):
                        value = agent_output.get(key)
                        if key not in result and isinstance(value, str):
                            result[key] = value
                            changed = True
                    if changed:
                        write_json_atomic(result_path, result)
                remove(agent_output_path)

                if result.get("status") == "completed":
                    for name in (
                        "tests.log",
                        "development.log",
                        "gate.log",
                        "champion-development.log",
                        "champion-gate.log",
                    ):
                        remove(experiment / name)
                if result.get("status") == "completed" or had_agent_output:
                    remove(experiment / "opencode-events.jsonl")

        report_roots = (
            [self]
            if self.run_number is not None
            else [self.for_run(number) for number in self.run_numbers()]
        )
        for run in report_roots:
            try:
                report_state = json.loads(run.loop_state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report_state = {}
            if report_state.get("report_status") == "completed" and run.report_path.is_file():
                remove(run.run_root / "report-events.jsonl")

        return {"removed_files": removed_files, "removed_bytes": removed_bytes}

    def emit_event(self, event: str, **details: Any) -> None:
        if self.run_number is None:
            return
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": self.run_id,
            "event": event,
            **details,
        }
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        round_id = details.get("round")
        scope = f"{self.run_id}/{round_id}" if isinstance(round_id, str) else self.run_id
        message = str(details.get("message") or event.replace("_", " "))
        print(f"[{scope}] {message}", flush=True)

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
            state["last_round_id"] = str(
                pending.get("round_id", pending.get("experiment_id"))
            )
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
        self._migrate_champion_layout()
        self._migrate_transient_layout()
        if self.state_path.exists():
            state = self._recover_promotion(self.load_state())
            if state.get("baseline_mode", "workspace") != baseline_mode:
                raise ValueError("task baseline mode changed after research workspace initialization")
            if state.get("baseline_exclude", []) != list(baseline_exclude):
                raise ValueError("task baseline exclusions changed after research workspace initialization")
            _git(self.source, "update-ref", self.seed_ref, str(state["seed_commit"]))
            if self.run_number is not None:
                self._cleanup_worktrees(self.candidates)
                self._cleanup_worktrees(self.evaluators)
            self._prepare_runtime(state, development_end)
            return state

        self.root.mkdir(parents=True, exist_ok=True)
        seed_commit = self._snapshot_commit(baseline_exclude)
        namespace = self._new_ref_namespace()
        state: dict[str, Any] = {
            "schema_version": 3,
            "task_id": self.task_id,
            "workspace_id": namespace.rsplit("/", 1)[-1],
            "baseline_mode": baseline_mode,
            "baseline_exclude": list(baseline_exclude),
            "seed_commit": seed_commit,
            "seed_ref": f"{namespace}/seed",
            "champion_commit": seed_commit if baseline_mode == "workspace" else None,
            "champion_ref": f"{namespace}/champion",
            "champion_number": 0,
            "champion_metrics": None,
            "champion_metrics_key": None,
            "last_round_id": None,
            "pending_promotion": None,
        }
        if baseline_mode == "workspace":
            _git(self.source, "update-ref", str(state["champion_ref"]), seed_commit)
        _git(self.source, "update-ref", str(state["seed_ref"]), seed_commit)
        write_json_atomic(self.state_path, state)
        self._prepare_runtime(state, development_end)
        return state

    def load_state(self) -> dict[str, Any]:
        self._migrate_champion_layout()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("task_id") != self.task_id:
            raise ValueError("research workspace task id does not match task.toml")
        if state.get("schema_version") != 3:
            raise ValueError("research workspace uses an incompatible pre-worktree schema")
        return state

    def candidate_base_commit(self, state: Mapping[str, Any] | None = None) -> str:
        current = state or self.load_state()
        champion = current.get("champion_commit")
        return str(champion if isinstance(champion, str) else current["seed_commit"])

    def create_candidate(
        self,
        round_id: str,
        development_end: date | None = None,
        baseline_mode: str = "workspace",
        baseline_exclude: Sequence[str] = (),
    ) -> tuple[Path, Path, dict[str, Any]]:
        if (
            not round_id.isdigit()
            or int(round_id) < 1
            or round_id != f"{int(round_id):03d}"
        ):
            raise ValueError("round id must be a zero-padded positive number")
        state = self.initialize(development_end, baseline_mode, baseline_exclude)
        self._cleanup_worktrees(self.candidates)
        candidate = self.candidates / round_id
        experiment = self.rounds / round_id
        if candidate.exists() or experiment.exists():
            raise FileExistsError(f"Round already exists: {round_id}")
        self._add_worktree(candidate, self.candidate_base_commit(state))
        copy_runtime_inputs(self.development_runtime, candidate)
        experiment.mkdir(parents=True)
        return candidate, experiment, state

    def create_champion_evaluator(self, round_id: str, state: Mapping[str, Any]) -> Path:
        commit = state.get("champion_commit")
        if not isinstance(commit, str):
            raise RuntimeError("research task does not have a champion yet")
        self._cleanup_worktrees(self.evaluators)
        evaluator = self.evaluators / round_id
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
        round_id: str,
        champion_metrics: Mapping[str, Any] | None = None,
    ) -> None:
        state["last_round_id"] = round_id
        if champion_metrics is not None:
            state["champion_metrics"] = dict(champion_metrics)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(self.state_path, state)

    def promote(
        self,
        candidate: Path,
        state: dict[str, Any],
        round_id: str,
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
            f"Research {self.task_id}: {round_id}",
            env=_GIT_IDENTITY,
        )
        commit = _git(candidate, "rev-parse", "HEAD")
        number = int(state["champion_number"]) + 1
        state["pending_promotion"] = {
            "round_id": round_id,
            "commit": commit,
            "champion_number": number,
            "metrics": dict(metrics),
        }
        write_json_atomic(self.state_path, state)
        _git(self.source, "update-ref", self.champion_ref, commit)
        state["champion_commit"] = commit
        state["champion_number"] = number
        state["pending_promotion"] = None
        self.record_state(state, round_id, metrics)
        self._remove_worktree(candidate)
        return commit

    def reject(self, candidate: Path, state: dict[str, Any], round_id: str) -> None:
        self.record_state(state, round_id)
        self._remove_worktree(candidate)
