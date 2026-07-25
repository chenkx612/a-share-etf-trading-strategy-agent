from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quant_core.research.checkpoint import FrozenCheckpoint
from quant_core.research.workspace import write_json_atomic


CONTROL_FILE = ".quant-research-attempt.json"
RUNTIME_DIR = ".quant-research-attempt"

CommandRunner = Callable[[Sequence[str], Path, Path, int], int]
CheckpointLoader = Callable[[], FrozenCheckpoint | None]
MetricsNormalizer = Callable[[object], dict[str, Any]]


class DevelopmentAttemptReceiver:
    def __init__(
        self,
        workspace: Path,
        output_dir: Path,
        command: Sequence[str],
        metrics_path: Path,
        checkpoint_loader: CheckpointLoader,
        command_runner: CommandRunner,
        command_timeout: int,
        deadline_monotonic: float,
        metrics_normalizer: MetricsNormalizer,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        event_sink: Callable[..., None] | None = None,
        event_details: Mapping[str, Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.output_dir = output_dir
        self.command = list(command)
        self.metrics_path = metrics_path
        self.checkpoint_loader = checkpoint_loader
        self.command_runner = command_runner
        self.command_timeout = command_timeout
        self.deadline_monotonic = deadline_monotonic
        self.metrics_normalizer = metrics_normalizer
        self.monotonic = monotonic
        self.event_sink = event_sink
        self.event_details = dict(event_details or {})
        self.runtime = workspace / RUNTIME_DIR
        self.requests = self.runtime / "requests"
        self.acks = self.runtime / "acks"
        self.attempts = output_dir / "development-attempts"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._processed: set[str] = set()
        self._next_id = 1

    def _emit(self, event: str, **details: Any) -> None:
        if self.event_sink is not None:
            self.event_sink(event, **details, **self.event_details)

    def start(self) -> None:
        self.requests.mkdir(parents=True, exist_ok=True)
        self.acks.mkdir(parents=True, exist_ok=True)
        self.attempts.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.workspace / CONTROL_FILE, {
            "schema_version": 1,
            "runtime_dir": RUNTIME_DIR,
            "request_timeout_seconds": self.command_timeout + 30,
        })
        self._thread = threading.Thread(
            target=self._run,
            name="quant-research-attempt-receiver",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1, self.command_timeout + 1))
        self._process_requests()
        (self.workspace / CONTROL_FILE).unlink(missing_ok=True)
        shutil.rmtree(self.runtime, ignore_errors=True)
        try:
            self.attempts.rmdir()
        except OSError:
            pass

    def _ack(self, request_id: str, payload: Mapping[str, Any]) -> None:
        write_json_atomic(self.acks / f"{request_id}.json", dict(payload))

    def _reject(self, request_id: str, error: str) -> None:
        self._ack(request_id, {"status": "rejected", "error": error})
        self._emit("development_attempt_rejected", message=error)

    def _current_checkpoint(self) -> FrozenCheckpoint:
        checkpoint = self.checkpoint_loader()
        if checkpoint is None:
            raise ValueError("submit an accepted checkpoint before Development evaluation")
        strategy = self.workspace / checkpoint.strategy_path
        if (
            not strategy.is_file()
            or hashlib.sha256(strategy.read_bytes()).hexdigest()
            != checkpoint.strategy_sha256
        ):
            raise ValueError("current strategy does not match the latest accepted checkpoint")
        return checkpoint

    def _existing(self, strategy_sha256: str) -> tuple[Path, dict[str, Any]] | None:
        if not self.attempts.exists():
            return None
        for directory in sorted(self.attempts.iterdir()):
            manifest_path = directory / "attempt.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(manifest, dict)
                and manifest.get("candidate_sha256") == strategy_sha256
            ):
                return directory, manifest
        return None

    def _evaluate(self, request_id: str) -> None:
        try:
            checkpoint = self._current_checkpoint()
        except (OSError, ValueError) as exc:
            self._reject(request_id, str(exc))
            return
        existing = self._existing(checkpoint.strategy_sha256)
        if existing is not None:
            _, manifest = existing
            self._ack(request_id, {
                "status": "accepted",
                "attempt_id": manifest["attempt_id"],
                "candidate_sha256": checkpoint.strategy_sha256,
                "deduplicated": True,
                "development_metrics": manifest["development_metrics"],
            })
            return
        remaining = int(self.deadline_monotonic - self.monotonic())
        if remaining < 1:
            self._reject(request_id, "Round deadline reached before Development evaluation")
            return
        attempt_id = f"{self._next_id:03d}"
        self._next_id += 1
        self._emit(
            "development_attempt_started",
            attempt_id=attempt_id,
            strategy_sha256=checkpoint.strategy_sha256,
            message="Development attempt started",
        )
        log_path = self.output_dir / f"development-attempt-{attempt_id}.log"
        exit_code = self.command_runner(
            self.command,
            self.workspace,
            log_path,
            min(self.command_timeout, remaining),
        )
        if exit_code != 0:
            self._reject(request_id, f"Development evaluation failed with exit code {exit_code}")
            return
        log_path.unlink(missing_ok=True)
        strategy = self.workspace / checkpoint.strategy_path
        if (
            not strategy.is_file()
            or hashlib.sha256(strategy.read_bytes()).hexdigest()
            != checkpoint.strategy_sha256
        ):
            self._reject(
                request_id,
                "strategy changed during Development evaluation",
            )
            return
        try:
            raw_metrics = json.loads(self.metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._reject(request_id, "Development evaluation produced invalid metrics")
            return
        if not isinstance(raw_metrics, dict):
            self._reject(request_id, "Development evaluation metrics must be an object")
            return
        normalized = self.metrics_normalizer(raw_metrics)
        directory = self.attempts / attempt_id
        directory.mkdir()
        completed_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "candidate_sha256": checkpoint.strategy_sha256,
            "hypothesis": checkpoint.metadata["hypothesis"],
            "completed_at": completed_at,
            "development_metrics": normalized,
        }
        write_json_atomic(directory / "attempt.json", manifest)
        self._ack(request_id, {
            "status": "accepted",
            "attempt_id": attempt_id,
            "candidate_sha256": checkpoint.strategy_sha256,
            "deduplicated": False,
            "development_metrics": normalized,
        })
        self._emit(
            "development_attempt_completed",
            attempt_id=attempt_id,
            strategy_sha256=checkpoint.strategy_sha256,
            message="Development attempt completed",
        )

    def _learn(self, request_id: str, payload: Mapping[str, Any]) -> None:
        if set(payload) != {"schema_version", "request_id", "action", "attempt_id", "learning"}:
            self._reject(request_id, "learning request has invalid fields")
            return
        attempt_id = payload.get("attempt_id")
        learning = payload.get("learning")
        if (
            not isinstance(attempt_id, str)
            or not attempt_id.isdigit()
            or not isinstance(learning, str)
            or not learning.strip()
        ):
            self._reject(request_id, "learning request is invalid")
            return
        directory = self.attempts / attempt_id
        if not (directory / "attempt.json").is_file():
            self._reject(request_id, "Development attempt does not exist")
            return
        learning_path = directory / "learning.json"
        if learning_path.exists():
            self._reject(request_id, "Development attempt learning is already recorded")
            return
        write_json_atomic(learning_path, {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "learning": learning.strip(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })
        self._ack(request_id, {"status": "accepted", "attempt_id": attempt_id})
        self._emit(
            "development_attempt_learning_recorded",
            attempt_id=attempt_id,
            message="Development attempt learning recorded",
        )

    def _process_requests(self) -> None:
        if not self.requests.exists():
            return
        for request in sorted(self.requests.glob("*.json")):
            request_id = request.stem
            if request_id in self._processed:
                continue
            self._processed.add(request_id)
            try:
                payload = json.loads(request.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._reject(request_id, "attempt request is not valid JSON")
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or payload.get("request_id") != request_id
            ):
                self._reject(request_id, "attempt request has invalid identity")
                continue
            action = payload.get("action")
            if action == "evaluate" and set(payload) == {
                "schema_version", "request_id", "action",
            }:
                self._evaluate(request_id)
            elif action == "learn":
                self._learn(request_id, payload)
            else:
                self._reject(request_id, "attempt request has invalid action")

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            self._process_requests()


def _request(payload: dict[str, Any], *, workspace: Path) -> Mapping[str, Any]:
    root = workspace.resolve()
    control = json.loads((root / CONTROL_FILE).read_text(encoding="utf-8"))
    if (
        not isinstance(control, dict)
        or set(control) != {
            "schema_version", "runtime_dir", "request_timeout_seconds",
        }
        or control.get("schema_version") != 1
    ):
        raise ValueError("attempt control file is invalid")
    request_id = uuid.uuid4().hex
    payload.update({"schema_version": 1, "request_id": request_id})
    runtime = root / str(control["runtime_dir"])
    ack_path = runtime / "acks" / f"{request_id}.json"
    write_json_atomic(runtime / "requests" / f"{request_id}.json", payload)
    timeout = control["request_timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise ValueError("attempt request timeout is invalid")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ack_path.exists():
            result = json.loads(ack_path.read_text(encoding="utf-8"))
            if result.get("status") != "accepted":
                raise RuntimeError(str(result.get("error", "attempt request rejected")))
            return result
        time.sleep(0.05)
    raise TimeoutError("Harness did not acknowledge the attempt request")


def evaluate(*, workspace: Path = Path(".")) -> Mapping[str, Any]:
    return _request({"action": "evaluate"}, workspace=workspace)


def record_learning(
    attempt_id: str,
    learning_path: Path,
    *,
    workspace: Path = Path("."),
) -> Mapping[str, Any]:
    value = json.loads(learning_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"learning"}:
        raise ValueError("learning file must contain exactly one learning string")
    return _request({
        "action": "learn",
        "attempt_id": attempt_id,
        "learning": value["learning"],
    }, workspace=workspace)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python3 -m quant_core.research.attempt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("evaluate")
    learn_parser = subparsers.add_parser("learn")
    learn_parser.add_argument("attempt_id")
    learn_parser.add_argument("learning", type=Path)
    args = parser.parse_args()
    result = (
        evaluate()
        if args.command == "evaluate"
        else record_learning(args.attempt_id, args.learning)
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
