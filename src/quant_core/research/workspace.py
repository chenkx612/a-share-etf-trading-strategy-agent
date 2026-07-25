from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import pandas as pd


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Quant Research Harness",
    "GIT_AUTHOR_EMAIL": "quant-research@example.invalid",
    "GIT_COMMITTER_NAME": "Quant Research Harness",
    "GIT_COMMITTER_EMAIL": "quant-research@example.invalid",
}


def workspace_python_env(workspace: Path) -> dict[str, str]:
    """Prefer a disposable worktree's source tree over an editable install."""
    env = dict(os.environ)
    source_root = str((workspace / "src").resolve())
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not existing else os.pathsep.join((source_root, existing))
    return env


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


def _git_bytes(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        if not detail:
            detail = completed.stdout.decode(errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


@contextmanager
def _temporary_git_write_environment(
    root: Path,
    *,
    prefix: str,
) -> Iterator[dict[str, str]]:
    """Keep temporary index and object writes outside the source repository."""
    common_dir = Path(_git(root, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()
    source_objects = common_dir / "objects"
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        temporary_root = Path(temporary)
        object_directory = temporary_root / "objects"
        object_directory.mkdir()
        alternates = json.dumps(str(source_objects), ensure_ascii=False)
        inherited_alternates = os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        if inherited_alternates:
            alternates = os.pathsep.join((alternates, inherited_alternates))
        yield {
            "GIT_INDEX_FILE": str(temporary_root / "index"),
            "GIT_OBJECT_DIRECTORY": str(object_directory),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternates,
        }


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


def runtime_inputs_manifest(root: Path) -> dict[str, str]:
    """Return stable hashes for the data and factor files used by an evaluation."""
    manifest: dict[str, str] = {}
    for relative_root in (Path("data"), Path("outputs") / "factors"):
        directory = root / relative_root
        if not directory.is_dir():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            manifest[path.relative_to(root).as_posix()] = _file_sha256(path)
    return manifest


def runtime_inputs_sha256(root: Path) -> str:
    """Return one stable digest for all fixed runtime inputs."""
    encoded = json.dumps(
        runtime_inputs_manifest(root),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _evaluator_contract_digest(paths: Sequence[str], listing: str) -> str:
    payload = {
        "schema_version": 1,
        "paths": sorted(paths),
        "tree": sorted(line for line in listing.splitlines() if line),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def evaluator_contract_sha256_for_commit(
    root: Path,
    contract_paths: Sequence[str],
    strategy_path: str,
) -> str:
    """Hash declared evaluator content from a worktree commit."""
    lines: list[str] = []
    for path in contract_paths:
        listing = _git(root, "ls-tree", "-r", "--full-tree", "HEAD", "--", path)
        path_lines = [
            line
            for line in listing.splitlines()
            if "\t" in line and line.split("\t", 1)[1] != strategy_path
        ]
        if not path_lines:
            raise FileNotFoundError(
                f"evaluator contract path has no committed files: {path}"
            )
        lines.extend(path_lines)
    return _evaluator_contract_digest(contract_paths, "\n".join(lines))


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class ResearchWorkspace:
    source: Path
    research_root: Path
    task_id: str
    run_number: int | None = None
    evaluation_environment_sha256: str | None = None
    diagnostics_enabled: bool = False

    def __post_init__(self) -> None:
        self.source = self.source.resolve()
        self.research_root = self.research_root.resolve()
        if not _SAFE_TASK_ID.fullmatch(self.task_id):
            raise ValueError("task.id may contain only letters, numbers, '.', '_' and '-'")
        if self.run_number is not None and self.run_number < 1:
            raise ValueError("run number must be positive")
        if self.evaluation_environment_sha256 is not None and (
            len(self.evaluation_environment_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.evaluation_environment_sha256
            )
        ):
            raise ValueError("evaluation environment must be a SHA-256 digest")
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
    def loop_state_path(self) -> Path:
        return self.run_root / "state.json"

    @property
    def report_path(self) -> Path:
        return self.run_root / "report.md"

    @property
    def report_facts_path(self) -> Path:
        return self.run_root / "report-facts.json"

    @property
    def run_temp(self) -> Path:
        return self.root / ".tmp" / "runs" / self.run_id

    @property
    def event_path(self) -> Path:
        return self.run_temp / "events.jsonl"

    @property
    def diagnostics_root(self) -> Path:
        if self.run_number is None:
            raise RuntimeError("research workspace is not bound to a run")
        return self.root / ".cache" / "diagnostics" / self.run_id

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

    def for_run(self, run_number: int) -> ResearchWorkspace:
        return ResearchWorkspace(
            self.source,
            self.research_root,
            self.task_id,
            run_number=run_number,
            evaluation_environment_sha256=self.evaluation_environment_sha256,
            diagnostics_enabled=self.diagnostics_enabled,
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
        if self.state_path.exists():
            champion_state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(state.get("last_round"), str):
                champion_state["last_round_id"] = f"{bound.run_id}/{state['last_round']}"
            accepted_rounds: list[str] = []
            for legacy_id, round_id in mapping.items():
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
            write_json_atomic(self.state_path, champion_state)
        legacy_loop_state.unlink()
        try:
            legacy_experiments.rmdir()
        except OSError:
            pass
        return run_number

    def _snapshot_commit(
        self,
        excluded_paths: Sequence[str],
        *,
        strategy_path: str,
        champion_path: Path | None,
    ) -> str:
        excluded = [*excluded_paths, "data", "outputs"]
        if strategy_path not in excluded:
            excluded.append(strategy_path)
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
            if champion_path is not None:
                blob = _git(self.source, "hash-object", "-w", "--", str(champion_path))
                _git(
                    self.source,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{blob},{strategy_path}",
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
                f"Temporary research base for {self.task_id}",
                env=env,
            )

    def evaluator_contract_sha256(
        self,
        contract_paths: Sequence[str],
        *,
        strategy_path: str,
    ) -> str:
        """Hash the explicitly declared fixed evaluator inputs."""
        for path in contract_paths:
            if not os.path.lexists(self.source / path):
                raise FileNotFoundError(f"evaluator contract path does not exist: {path}")
        with _temporary_git_write_environment(
            self.source,
            prefix="quant-contract-",
        ) as temporary_env:
            env = {**temporary_env, **_GIT_IDENTITY}
            _git(self.source, "read-tree", "HEAD", env=env)
            _git(self.source, "add", "-A", "--", *contract_paths, env=env)
            tree = _git(self.source, "write-tree", env=env)
            lines: list[str] = []
            for path in contract_paths:
                listing = _git(
                    self.source,
                    "ls-tree",
                    "-r",
                    "--full-tree",
                    tree,
                    "--",
                    path,
                    env=env,
                )
                path_lines = [
                    line
                    for line in listing.splitlines()
                    if "\t" in line and line.split("\t", 1)[1] != strategy_path
                ]
                if not path_lines:
                    raise ValueError(
                        f"evaluator contract path has no hashable files: {path}"
                    )
                lines.extend(path_lines)
            return _evaluator_contract_digest(contract_paths, "\n".join(lines))

    def metrics_applicability(
        self,
        state: Mapping[str, Any],
        metrics_key: str,
        evaluator_contract_paths: Sequence[str],
        *,
        champion_sha256: str | None = None,
        evaluator_contract_sha256: str | None = None,
    ) -> dict[str, str | None]:
        if self.evaluation_environment_sha256 is None:
            raise RuntimeError("fixed evaluation environment was not configured")
        strategy_path = str(state["strategy_path"])
        contract = evaluator_contract_sha256 or self.evaluator_contract_sha256(
            evaluator_contract_paths,
            strategy_path=strategy_path,
        )
        champion = champion_sha256
        if champion is None and isinstance(state.get("champion_sha256"), str):
            champion = str(state["champion_sha256"])
        return {
            "champion_sha256": champion,
            "metrics_key": metrics_key,
            "development_inputs_sha256": runtime_inputs_sha256(self.development_runtime),
            "gate_inputs_sha256": runtime_inputs_sha256(self.evaluation_runtime),
            "evaluator_contract_sha256": contract,
            "evaluation_environment_sha256": self.evaluation_environment_sha256,
        }

    def refresh_champion_metrics_status(
        self,
        state: dict[str, Any],
        metrics_key: str,
        evaluator_contract_paths: Sequence[str],
        *,
        evaluator_contract_sha256: str | None = None,
    ) -> dict[str, str | None]:
        """Refresh validity without discarding the last durable metrics value."""
        expected = self.metrics_applicability(
            state,
            metrics_key,
            evaluator_contract_paths,
            evaluator_contract_sha256=evaluator_contract_sha256,
        )
        record = state.get("champion_metrics_record")
        if not isinstance(record, dict):
            return expected
        actual = record.get("applicability")
        reasons: list[str] = []
        if not isinstance(actual, dict):
            existing_reasons = record.get("stale_reasons")
            if (
                isinstance(existing_reasons, list)
                and "legacy_missing_applicability" in existing_reasons
            ):
                reasons.append("legacy_missing_applicability")
            else:
                reasons.append("missing_applicability")
        else:
            for key, value in expected.items():
                if actual.get(key) != value:
                    reasons.append(f"{key}_changed")
        if not isinstance(record.get("metrics"), dict):
            reasons.append("missing_metrics")
        status = "stale" if reasons else "valid"
        if record.get("status") != status or record.get("stale_reasons") != reasons:
            record["status"] = status
            record["stale_reasons"] = reasons
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            write_json_atomic(self.state_path, state)
        return expected

    @staticmethod
    def valid_champion_metrics(state: Mapping[str, Any]) -> dict[str, Any] | None:
        record = state.get("champion_metrics_record")
        if not isinstance(record, Mapping) or record.get("status") != "valid":
            return None
        metrics = record.get("metrics")
        return dict(metrics) if isinstance(metrics, Mapping) else None

    @staticmethod
    def metrics_record(
        metrics: Mapping[str, Any],
        applicability: Mapping[str, str | None],
        evaluated_in_round: str,
    ) -> dict[str, Any]:
        return {
            "metrics": dict(metrics),
            "status": "valid",
            "stale_reasons": [],
            "evaluated_in_round": evaluated_in_round,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "applicability": dict(applicability),
        }

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

    def _cleanup_worktrees(self, root: Path, *, remove_root: bool = True) -> None:
        if not root.exists():
            return
        for path in list(root.iterdir()):
            if path.is_dir():
                self._remove_worktree(path)
            else:
                path.unlink()
        if not remove_root:
            return
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

    def _strategy_path(
        self,
        state: Mapping[str, Any],
        supplied: str | None,
    ) -> str:
        configured = state.get("strategy_path")
        if configured is not None and not isinstance(configured, str):
            raise ValueError("research workspace has an invalid strategy path")
        if supplied is not None and configured is not None and supplied != configured:
            raise ValueError("task strategy path changed after research workspace initialization")
        strategy_path = supplied or configured
        if strategy_path is None:
            raise ValueError("strategy path is required to migrate the research workspace")
        relative = Path(strategy_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("strategy path must be repository-relative")
        return strategy_path

    def _extract_champion(self, commit: str, strategy_path: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(self.source), "show", f"{commit}:{strategy_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            return False
        self.champion_next_path.parent.mkdir(parents=True, exist_ok=True)
        self.champion_next_path.write_bytes(completed.stdout)
        os.replace(self.champion_next_path, self.champion_path)
        return True

    def _latest_accepted_round(self) -> str | None:
        accepted: str | None = None
        for run_number in self.run_numbers():
            run = self.for_run(run_number)
            if not run.rounds.exists():
                continue
            for round_path in sorted(run.rounds.iterdir()):
                try:
                    decision = json.loads(
                        (round_path / "decision.json").read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                if decision.get("decision") == "accepted":
                    accepted = f"{run.run_id}/{round_path.name}"
        return accepted

    def _migrate_champion_layout(self, strategy_path: str | None = None) -> None:
        source = self.state_path if self.state_path.exists() else self.legacy_state_path
        if not source.exists():
            return
        state = json.loads(source.read_text(encoding="utf-8"))
        schema_version = state.get("schema_version")
        if schema_version == 6 and source == self.state_path:
            if "last_experiment_id" in state and "last_round_id" not in state:
                state["last_round_id"] = state.pop("last_experiment_id")
                write_json_atomic(self.state_path, state)
            return
        if schema_version not in {2, 3, 4, 5}:
            raise ValueError("research workspace uses an incompatible champion schema")
        if schema_version in {4, 5} and isinstance(state.get("pending_promotion"), dict):
            state = self._recover_promotion(state)
        if schema_version == 5:
            metrics_record = state.get("champion_metrics_record")
            if isinstance(metrics_record, dict):
                reasons = metrics_record.get("stale_reasons")
                stale_reasons = list(reasons) if isinstance(reasons, list) else []
                if "legacy_missing_evaluation_environment" not in stale_reasons:
                    stale_reasons.append("legacy_missing_evaluation_environment")
                metrics_record["status"] = "stale"
                metrics_record["stale_reasons"] = stale_reasons
            state["schema_version"] = 6
            write_json_atomic(self.state_path, state)
            if source != self.state_path:
                source.unlink()
            return
        editable = self._strategy_path(state, strategy_path)
        champion_commit = state.get("champion_commit")
        champion_sha256 = (
            str(state["champion_sha256"])
            if isinstance(state.get("champion_sha256"), str)
            else None
        )
        if champion_sha256 is None and isinstance(champion_commit, str):
            if not self._extract_champion(champion_commit, editable):
                raise ValueError("legacy Champion strategy cannot be read from its Git commit")
            champion_sha256 = _file_sha256(self.champion_path)
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
            champion_round_id = self._latest_accepted_round()
        migrated = {
            "schema_version": 6,
            "task_id": self.task_id,
            "baseline_mode": state.get("baseline_mode", "workspace"),
            "baseline_exclude": list(state.get("baseline_exclude", [])),
            "strategy_path": editable,
            "project_revision": _git(self.source, "rev-parse", "HEAD"),
            "champion_number": int(state.get("champion_number", 0)),
            "champion_sha256": champion_sha256,
            "champion_round_id": champion_round_id,
            "champion_metrics_record": metrics_record,
            "last_round_id": state.get(
                "last_round_id",
                state.get("last_experiment_id"),
            ),
            "pending_promotion": None,
            "development_end": state.get("development_end"),
            "updated_at": state.get("updated_at"),
        }
        write_json_atomic(self.state_path, migrated)
        if source != self.state_path:
            source.unlink()

    def cleanup_transient(
        self,
        *,
        remove_development_cache: bool = False,
        preserve_worktree_parents: bool = False,
    ) -> None:
        """Remove disposable worktrees and optionally the derived development cache."""
        self._migrate_transient_layout()
        if self.run_number is None:
            worktrees = self.root / ".tmp" / "worktrees"
            self._cleanup_worktrees(self.test_evaluators)
            scopes = list(worktrees.iterdir()) if worktrees.exists() else []
            for scope in scopes:
                if scope.is_dir():
                    if scope.name == "tests":
                        continue
                    self._cleanup_worktrees(scope / "candidates")
                    self._cleanup_worktrees(scope / "evaluators")
                    try:
                        scope.rmdir()
                    except OSError:
                        pass
            shutil.rmtree(self.root / ".tmp" / "runs", ignore_errors=True)
        else:
            self._cleanup_worktrees(
                self.candidates,
                remove_root=not preserve_worktree_parents,
            )
            self._cleanup_worktrees(
                self.evaluators,
                remove_root=not preserve_worktree_parents,
            )
            shutil.rmtree(self.run_temp, ignore_errors=True)
        if remove_development_cache:
            shutil.rmtree(self.development_runtime, ignore_errors=True)
        removable = [
            self.root / ".tmp" / "runs",
            self.runtime,
            self.root / ".cache",
        ]
        if not preserve_worktree_parents:
            removable.extend([
                self.root / ".tmp" / "worktrees",
                self.root / ".tmp",
            ])
        for path in removable:
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
                    for attempt_log in experiment.glob(
                        "opencode-events.attempt-*.jsonl"
                    ):
                        remove(attempt_log)

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

    def clear_diagnostics(self) -> None:
        """Remove opt-in, disposable diagnostics for this task."""
        shutil.rmtree(self.root / ".cache" / "diagnostics", ignore_errors=True)

    def finalize_diagnostics(self, state: Mapping[str, Any]) -> Path | None:
        """Freeze a deterministic post-run diagnostic index."""
        if not self.diagnostics_enabled or self.run_number is None:
            return None
        self.diagnostics_root.mkdir(parents=True, exist_ok=True)
        events: list[dict[str, Any]] = []
        try:
            for line in self.diagnostics_event_path.read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
        except (OSError, json.JSONDecodeError):
            events = []

        findings: list[dict[str, Any]] = []
        round_records: list[dict[str, Any]] = []
        raw_round_ids = state.get("round_ids")
        round_ids = raw_round_ids if isinstance(raw_round_ids, list) else []
        diagnostic_round_ids = list(round_ids)
        current_round = state.get("current_round")
        if (
            isinstance(current_round, str)
            and current_round.isdigit()
            and int(current_round) > 0
            and current_round not in diagnostic_round_ids
        ):
            diagnostic_round_ids.append(current_round)
        for round_id in diagnostic_round_ids:
            if not isinstance(round_id, str):
                continue
            experiment = self.rounds / round_id
            result: dict[str, Any] = {}
            decision: dict[str, Any] = {}
            for name, destination in (("result.json", result), ("decision.json", decision)):
                try:
                    value = json.loads((experiment / name).read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        destination.update(value)
                    else:
                        findings.append({"code": "invalid_round_artifact", "round": round_id, "path": f"rounds/{round_id}/{name}"})
                except FileNotFoundError:
                    findings.append({"code": "missing_round_artifact", "round": round_id, "path": f"rounds/{round_id}/{name}"})
                except (OSError, json.JSONDecodeError):
                    findings.append({"code": "invalid_round_artifact", "round": round_id, "path": f"rounds/{round_id}/{name}"})
            timing = result.get("round_timing")
            duration = timing.get("duration_seconds") if isinstance(timing, dict) else None
            attempts = result.get("development_attempts")
            round_records.append({
                "round": round_id,
                "status": result.get("status"),
                "decision": decision.get("decision"),
                "duration_seconds": duration,
                "development_attempts": len(attempts) if isinstance(attempts, list) else 0,
                "failure_kind": result.get("failure_kind"),
                "failure_code": result.get("failure_code"),
            })
            if result.get("status") == "completed" and decision.get("decision") == "failed":
                findings.append({"code": "completed_result_failed_decision", "round": round_id})

        try:
            counted = sum(int(state.get(key, 0)) for key in ("accepted", "rejected", "failed"))
            completed = int(state.get("rounds_completed", 0))
        except (TypeError, ValueError):
            findings.append({"code": "invalid_decision_counters"})
        else:
            if counted != completed:
                findings.append({"code": "decision_counter_mismatch"})
            if len(round_ids) != completed:
                findings.append({"code": "round_id_count_mismatch"})

        gaps: list[dict[str, Any]] = []
        timestamped_events: list[tuple[datetime, dict[str, Any]]] = []
        for event in events:
            try:
                timestamp = datetime.fromisoformat(str(event.get("timestamp")).replace("Z", "+00:00"))
            except ValueError:
                findings.append({"code": "invalid_event_timestamp"})
                continue
            timestamped_events.append((timestamp, event))
        timestamped_events.sort(key=lambda item: item[0])
        for previous, current in zip(timestamped_events, timestamped_events[1:]):
            seconds = (current[0] - previous[0]).total_seconds()
            if seconds < 60:
                continue
            previous_round = previous[1].get("round")
            current_round = current[1].get("round")
            gaps.append({
                "round": current_round if current_round == previous_round else None,
                "seconds": seconds,
                "after": previous[1].get("event"),
                "before": current[1].get("event"),
            })
        gaps.sort(key=lambda item: float(item["seconds"]), reverse=True)
        findings.extend({"code": "long_event_gap", **gap} for gap in gaps[:10])

        artifacts: list[dict[str, Any]] = []
        for round_id in diagnostic_round_ids:
            if not isinstance(round_id, str):
                continue
            experiment = self.rounds / round_id
            for name in (
                "tests.log", "development.log", "gate.log",
                "champion-development.log", "champion-gate.log",
            ):
                source = experiment / name
                if not source.is_file():
                    continue
                content = source.read_bytes()
                tail = content[-65536:]
                target = self.diagnostics_root / "logs" / round_id / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tail)
                artifacts.append({
                    "path": target.relative_to(self.diagnostics_root).as_posix(),
                    "source": source.relative_to(self.run_root).as_posix(),
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "truncated": len(tail) != len(content),
                })

        path = self.diagnostics_root / "diagnostic-summary.json"
        write_json_atomic(path, {
            "schema_version": 1,
            "task_id": self.task_id,
            "run": self.run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stop_reason": state.get("stop_reason"),
            "status": state.get("status"),
            "report_status": state.get("report_status"),
            "event_count": len(events),
            "rounds": round_records,
            "findings": findings,
            "artifacts": artifacts,
        })
        return path

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
            handle.flush()
        if self.diagnostics_enabled:
            self.diagnostics_event_path.parent.mkdir(parents=True, exist_ok=True)
            with self.diagnostics_event_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
        round_id = details.get("round")
        scope = f"{self.run_id}/{round_id}" if isinstance(round_id, str) else self.run_id
        message = str(details.get("message") or event.replace("_", " "))
        print(f"[{scope}] {message}", flush=True)

    def _recover_promotion(self, state: dict[str, Any]) -> dict[str, Any]:
        pending = state.get("pending_promotion")
        if not isinstance(pending, dict):
            return state
        target_sha256 = str(pending["sha256"])
        if (
            self.champion_next_path.is_file()
            and _file_sha256(self.champion_next_path) == target_sha256
        ):
            os.replace(self.champion_next_path, self.champion_path)
        if self.champion_path.is_file() and _file_sha256(self.champion_path) == target_sha256:
            state["champion_sha256"] = target_sha256
            state["champion_number"] = int(pending["champion_number"])
            if state.get("schema_version") == 4:
                state["champion_metrics"] = pending["metrics"]
            else:
                metrics_record = pending.get("metrics_record")
                if not isinstance(metrics_record, dict) and isinstance(pending.get("metrics"), dict):
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
        self.champion_next_path.unlink(missing_ok=True)
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
            write_json_atomic(self.state_path, state)

    def initialize(
        self,
        development_end: date | None = None,
        baseline_mode: str = "workspace",
        baseline_exclude: Sequence[str] = (),
        strategy_path: str | None = None,
    ) -> dict[str, Any]:
        self._migrate_champion_layout(strategy_path)
        self._migrate_transient_layout()
        if self.state_path.exists():
            state = self.load_state(strategy_path)
            if state.get("baseline_mode", "workspace") != baseline_mode:
                raise ValueError("task baseline mode changed after research workspace initialization")
            if state.get("baseline_exclude", []) != list(baseline_exclude):
                raise ValueError("task baseline exclusions changed after research workspace initialization")
            if self.run_number is not None:
                self._cleanup_worktrees(self.candidates, remove_root=False)
                self._cleanup_worktrees(self.evaluators, remove_root=False)
            self._prepare_runtime(state, development_end)
            return state

        self.root.mkdir(parents=True, exist_ok=True)
        editable = self._strategy_path({}, strategy_path)
        champion_sha256: str | None = None
        if baseline_mode == "workspace":
            source_strategy = self.source / editable
            if not source_strategy.is_file():
                raise FileNotFoundError(f"Strategy script does not exist: {source_strategy}")
            self.champion_next_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_strategy, self.champion_next_path)
            os.replace(self.champion_next_path, self.champion_path)
            champion_sha256 = _file_sha256(self.champion_path)
        state: dict[str, Any] = {
            "schema_version": 6,
            "task_id": self.task_id,
            "baseline_mode": baseline_mode,
            "baseline_exclude": list(baseline_exclude),
            "strategy_path": editable,
            "project_revision": _git(self.source, "rev-parse", "HEAD"),
            "champion_number": 0,
            "champion_sha256": champion_sha256,
            "champion_round_id": None,
            "champion_metrics_record": None,
            "last_round_id": None,
            "pending_promotion": None,
        }
        write_json_atomic(self.state_path, state)
        self._prepare_runtime(state, development_end)
        return state

    def load_state(self, strategy_path: str | None = None) -> dict[str, Any]:
        self._migrate_champion_layout(strategy_path)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("task_id") != self.task_id:
            raise ValueError("research workspace task id does not match task.toml")
        if state.get("schema_version") != 6:
            raise ValueError("research workspace uses an incompatible Champion schema")
        self._strategy_path(state, strategy_path)
        if isinstance(state.get("pending_promotion"), dict):
            state = self._recover_promotion(state)
        else:
            self.champion_next_path.unlink(missing_ok=True)
        champion_sha256 = state.get("champion_sha256")
        if isinstance(champion_sha256, str):
            if not self.champion_path.is_file():
                raise FileNotFoundError(f"Champion strategy does not exist: {self.champion_path}")
            if _file_sha256(self.champion_path) != champion_sha256:
                raise ValueError("Champion strategy hash does not match champion.json")
        return state

    def prepare_candidate(
        self,
        round_id: str,
        development_end: date | None = None,
        baseline_mode: str = "workspace",
        baseline_exclude: Sequence[str] = (),
        strategy_path: str | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        if (
            not round_id.isdigit()
            or int(round_id) < 1
            or round_id != f"{int(round_id):03d}"
        ):
            raise ValueError("round id must be a zero-padded positive number")
        state = self.initialize(
            development_end,
            baseline_mode,
            baseline_exclude,
            strategy_path,
        )
        # Keep the per-Run parent stable between rounds.  Removing only child
        # worktrees also recovers a candidate left by an uncatchable interrupt
        # during the pre-allocation bind probe, before it has any Round state.
        self._cleanup_worktrees(self.candidates, remove_root=False)
        candidate = self.candidates / round_id
        experiment = self.rounds / round_id
        if candidate.exists() or experiment.exists():
            raise FileExistsError(f"Round already exists: {round_id}")
        champion = (
            self.champion_path
            if isinstance(state.get("champion_sha256"), str)
            else None
        )
        base_commit = self._snapshot_commit(
            state.get("baseline_exclude", []),
            strategy_path=str(state["strategy_path"]),
            champion_path=champion,
        )
        self._add_worktree(candidate, base_commit)
        copy_runtime_inputs(self.development_runtime, candidate)
        return candidate, state

    def activate_candidate(self, candidate: Path, round_id: str) -> Path:
        expected = self.candidates / round_id
        if candidate.resolve() != expected.resolve() or not candidate.is_dir():
            raise ValueError("prepared candidate does not match the requested Round")
        experiment = self.rounds / round_id
        experiment.mkdir(parents=True)
        return experiment

    def discard_prepared_candidate(self, candidate: Path) -> None:
        self._remove_worktree(candidate)

    def create_candidate(
        self,
        round_id: str,
        development_end: date | None = None,
        baseline_mode: str = "workspace",
        baseline_exclude: Sequence[str] = (),
        strategy_path: str | None = None,
    ) -> tuple[Path, Path, dict[str, Any]]:
        candidate, state = self.prepare_candidate(
            round_id,
            development_end,
            baseline_mode,
            baseline_exclude,
            strategy_path,
        )
        experiment = self.activate_candidate(candidate, round_id)
        return candidate, experiment, state

    def create_champion_evaluator(self, round_id: str, state: Mapping[str, Any]) -> Path:
        if not isinstance(state.get("champion_sha256"), str):
            raise RuntimeError("research task does not have a champion yet")
        self._cleanup_worktrees(self.evaluators)
        evaluator = self.evaluators / round_id
        base_commit = self._snapshot_commit(
            state.get("baseline_exclude", []),
            strategy_path=str(state["strategy_path"]),
            champion_path=self.champion_path,
        )
        self._add_worktree(evaluator, base_commit)
        return evaluator

    def create_champion_test_evaluator(self, test_id: str, state: Mapping[str, Any]) -> Path:
        """Create a task-level evaluator for an immutable Champion test."""
        if not isinstance(state.get("champion_sha256"), str):
            raise RuntimeError("research task does not have a champion yet")
        evaluator = self.test_evaluators / test_id
        if evaluator.exists():
            raise FileExistsError(f"Test evaluator already exists: {test_id}")
        base_commit = self._snapshot_commit(
            state.get("baseline_exclude", []),
            strategy_path=str(state["strategy_path"]),
            champion_path=self.champion_path,
        )
        self._add_worktree(evaluator, base_commit)
        return evaluator

    def remove_evaluator(self, evaluator: Path) -> None:
        self._remove_worktree(evaluator)

    def write_candidate_patch(
        self,
        candidate: Path,
        state: Mapping[str, Any],
        editable: Sequence[str],
        destination: Path,
        expected_strategy_sha256: str | None,
    ) -> str:
        strategy_path = str(state["strategy_path"])
        if list(editable) != [strategy_path]:
            raise ValueError("editable strategy path does not match champion.json")
        candidate_strategy = candidate / strategy_path
        candidate_content = (
            candidate_strategy.read_bytes()
            if candidate_strategy.is_file()
            else None
        )
        candidate_sha256 = (
            hashlib.sha256(candidate_content).hexdigest()
            if candidate_content is not None
            else None
        )
        if candidate_sha256 != expected_strategy_sha256:
            raise RuntimeError(
                "candidate strategy hash does not match submission.strategy_sha256"
            )

        with tempfile.TemporaryDirectory(prefix="quant-patch-") as temporary:
            temporary_root = Path(temporary)
            generation_index = temporary_root / "generation.index"
            generation_env = {"GIT_INDEX_FILE": str(generation_index)}
            _git(candidate, "read-tree", "HEAD", env=generation_env)
            tracked = _git(
                candidate,
                "ls-tree",
                "--name-only",
                "HEAD",
                "--",
                strategy_path,
            )
            if candidate_content is not None and not tracked:
                _git(
                    candidate,
                    "add",
                    "-N",
                    "--",
                    strategy_path,
                    env=generation_env,
                )
            patch_content = _git_bytes(
                candidate,
                "diff",
                "--binary",
                "HEAD",
                "--",
                strategy_path,
                env=generation_env,
            )
            _write_bytes_atomic(destination, patch_content)

            validation_index = temporary_root / "validation.index"
            validation_env = {"GIT_INDEX_FILE": str(validation_index)}
            _git(candidate, "read-tree", "HEAD", env=validation_env)
            if patch_content:
                patch_path = temporary_root / "candidate.patch"
                patch_path.write_bytes(patch_content)
                _git(
                    candidate,
                    "apply",
                    "--cached",
                    "--whitespace=nowarn",
                    str(patch_path),
                    env=validation_env,
                )
            staged = _git(
                candidate,
                "ls-files",
                "--stage",
                "--",
                strategy_path,
                env=validation_env,
            )
            reconstructed = (
                _git_bytes(
                    candidate,
                    "show",
                    f":{strategy_path}",
                    env=validation_env,
                )
                if staged
                else None
            )
            if reconstructed != candidate_content:
                raise RuntimeError(
                    "candidate patch does not reconstruct the submitted strategy"
                )
            reconstructed_sha256 = (
                hashlib.sha256(reconstructed).hexdigest()
                if reconstructed is not None
                else None
            )
            if reconstructed_sha256 != expected_strategy_sha256:
                raise RuntimeError(
                    "reconstructed strategy hash does not match submission.strategy_sha256"
                )
        return hashlib.sha256(patch_content).hexdigest()

    def write_candidate_source(
        self,
        candidate: Path,
        state: Mapping[str, Any],
        destination: Path,
        expected_strategy_sha256: str,
    ) -> None:
        strategy_path = str(state["strategy_path"])
        candidate_strategy = candidate / strategy_path
        if not candidate_strategy.is_file():
            raise FileNotFoundError(
                f"Candidate strategy does not exist: {candidate_strategy}"
            )
        content = candidate_strategy.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_strategy_sha256:
            raise RuntimeError(
                "candidate strategy hash does not match submission.strategy_sha256"
            )
        _write_bytes_atomic(destination, content)
        if _file_sha256(destination) != expected_strategy_sha256:
            raise RuntimeError(
                "persisted candidate source hash does not match submission.strategy_sha256"
            )

    def record_state(
        self,
        state: dict[str, Any],
        round_id: str,
        champion_metrics_record: Mapping[str, Any] | None = None,
    ) -> None:
        state["last_round_id"] = round_id
        if champion_metrics_record is not None:
            state["champion_metrics_record"] = dict(champion_metrics_record)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(self.state_path, state)

    def promote(
        self,
        candidate: Path,
        state: dict[str, Any],
        round_id: str,
        metrics: Mapping[str, Any],
        editable: Sequence[str],
        metrics_key: str,
        evaluator_contract_paths: Sequence[str],
        evaluator_contract_sha256: str,
    ) -> str:
        strategy_path = str(state["strategy_path"])
        if list(editable) != [strategy_path]:
            raise ValueError("editable strategy path does not match champion.json")
        candidate_strategy = candidate / strategy_path
        if not candidate_strategy.is_file():
            raise FileNotFoundError(f"Candidate strategy does not exist: {candidate_strategy}")
        self.champion_next_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate_strategy, self.champion_next_path)
        sha256 = _file_sha256(self.champion_next_path)
        number = int(state["champion_number"]) + 1
        applicability = self.metrics_applicability(
            state,
            metrics_key,
            evaluator_contract_paths,
            champion_sha256=sha256,
            evaluator_contract_sha256=evaluator_contract_sha256,
        )
        metrics_record = self.metrics_record(metrics, applicability, round_id)
        state["pending_promotion"] = {
            "round_id": round_id,
            "sha256": sha256,
            "champion_number": number,
            "metrics_record": metrics_record,
            "project_revision": _git(self.source, "rev-parse", "HEAD"),
        }
        write_json_atomic(self.state_path, state)
        os.replace(self.champion_next_path, self.champion_path)
        state["champion_sha256"] = sha256
        state["champion_number"] = number
        state["champion_round_id"] = round_id
        state["project_revision"] = state["pending_promotion"]["project_revision"]
        state["pending_promotion"] = None
        self.record_state(state, round_id, metrics_record)
        self._remove_worktree(candidate)
        return sha256

    def reject(self, candidate: Path, state: dict[str, Any], round_id: str) -> None:
        self.record_state(state, round_id)
        self._remove_worktree(candidate)
