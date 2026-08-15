from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from quant_core.research.storage import write_json_atomic


CONTROL_FILE = ".quant-research-checkpoint.json"
RUNTIME_DIR = ".quant-research-checkpoint"
TRUSTED_RUNTIME_DIR = ".quant-research-checkpoint-trusted"
TRUSTED_STATUS_FILE = "status.json"
_METADATA_FIELDS = {
    "previous_feedback",
    "hypothesis",
    "attempts",
    "development_effect",
    "candidate",
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _valid_metadata(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _METADATA_FIELDS:
        return False
    return all(
        isinstance(value.get(key), str)
        and (key == "previous_feedback" or bool(value[key].strip()))
        for key in _METADATA_FIELDS
    )


@dataclass(frozen=True)
class FrozenCheckpoint:
    checkpoint_id: str
    submitted_at: str
    strategy_path: str
    strategy_sha256: str
    strategy_content: bytes
    metadata: Mapping[str, str]


class CheckpointReceiver:
    def __init__(
        self,
        workspace: Path,
        output_dir: Path,
        strategy_path: str,
        parent_champion_sha256: str | None,
        deadline_monotonic: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        event_sink: Callable[..., None] | None = None,
        event_details: Mapping[str, Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.output_dir = output_dir
        self.strategy_path = strategy_path
        self.parent_champion_sha256 = parent_champion_sha256
        self.deadline_monotonic = deadline_monotonic
        self.monotonic = monotonic
        self.event_sink = event_sink
        self.event_details = dict(event_details or {})
        self.runtime = workspace / RUNTIME_DIR
        self.requests = self.runtime / "requests"
        self.acks = self.runtime / "acks"
        self.trusted_runtime = workspace / TRUSTED_RUNTIME_DIR
        self.trusted_status = self.trusted_runtime / TRUSTED_STATUS_FILE
        self.checkpoint_root = output_dir / "checkpoints"
        self.checkpoints = self.checkpoint_root / uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._processed: set[str] = set()
        self._next_id = 1

    def _emit(self, event: str, **details: Any) -> None:
        if self.event_sink is not None:
            self.event_sink(event, **details, **self.event_details)

    def start(self) -> None:
        shutil.rmtree(self.trusted_runtime, ignore_errors=True)
        self.requests.mkdir(parents=True, exist_ok=True)
        self.acks.mkdir(parents=True, exist_ok=True)
        self.trusted_runtime.mkdir(parents=True)
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.trusted_status, {
            "schema_version": 1,
            "strategy_path": self.strategy_path,
            "latest_checkpoint": None,
        })
        write_json_atomic(self.workspace / CONTROL_FILE, {
            "schema_version": 1,
            "strategy_path": self.strategy_path,
            "runtime_dir": RUNTIME_DIR,
        })
        self._thread = threading.Thread(
            target=self._run,
            name="quant-research-checkpoint-receiver",
            daemon=True,
        )
        self._thread.start()

    def _ack(self, request_id: str, payload: Mapping[str, Any]) -> None:
        write_json_atomic(self.acks / f"{request_id}.json", dict(payload))

    def _reject(self, request_id: str, error: str) -> None:
        self._ack(request_id, {"status": "rejected", "error": error})
        self._emit("checkpoint_rejected", message=error)

    def _accept(self, request_id: str, payload: Mapping[str, Any]) -> None:
        if self.monotonic() >= self.deadline_monotonic:
            self._reject(request_id, "checkpoint submitted after the Round deadline")
            return
        if set(payload) != {"schema_version", "request_id", "metadata", "strategy"}:
            self._reject(request_id, "checkpoint request has invalid fields")
            return
        if payload.get("schema_version") != 1 or payload.get("request_id") != request_id:
            self._reject(request_id, "checkpoint request has invalid identity")
            return
        metadata = payload.get("metadata")
        strategy = payload.get("strategy")
        if not _valid_metadata(metadata):
            self._reject(request_id, "checkpoint metadata is invalid")
            return
        if not isinstance(strategy, dict) or set(strategy) != {
            "path", "sha256", "content_base64",
        }:
            self._reject(request_id, "checkpoint strategy payload is invalid")
            return
        if strategy.get("path") != self.strategy_path:
            self._reject(request_id, "checkpoint is outside scope.editable")
            return
        try:
            content = base64.b64decode(str(strategy["content_base64"]), validate=True)
        except (ValueError, TypeError):
            self._reject(request_id, "checkpoint strategy content is invalid")
            return
        digest = _sha256(content)
        if strategy.get("sha256") != digest:
            self._reject(request_id, "checkpoint strategy hash does not match")
            return
        try:
            source = content.decode("utf-8")
            compile(source, self.strategy_path, "exec")
        except (UnicodeDecodeError, SyntaxError):
            self._reject(request_id, "checkpoint strategy is not valid Python source")
            return

        checkpoint_id = f"{self._next_id:03d}"
        self._next_id += 1
        submitted_at = datetime.now(timezone.utc).isoformat()
        destination = self.checkpoints / checkpoint_id
        temporary = Path(tempfile.mkdtemp(prefix=f".{checkpoint_id}-", dir=self.checkpoints))
        try:
            frozen_strategy = temporary / "files" / self.strategy_path
            frozen_strategy.parent.mkdir(parents=True, exist_ok=True)
            frozen_strategy.write_bytes(content)
            write_json_atomic(temporary / "checkpoint.json", {
                "schema_version": 1,
                "checkpoint_id": checkpoint_id,
                "submitted_at": submitted_at,
                "strategy_path": self.strategy_path,
                "strategy_sha256": digest,
                "parent_champion_sha256": self.parent_champion_sha256,
                "metadata": dict(metadata),
            })
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        write_json_atomic(self.trusted_status, {
            "schema_version": 1,
            "strategy_path": self.strategy_path,
            "latest_checkpoint": {
                "checkpoint_id": checkpoint_id,
                "submitted_at": submitted_at,
                "strategy_sha256": digest,
            },
        })
        self._ack(request_id, {
            "status": "accepted",
            "checkpoint_id": checkpoint_id,
            "submitted_at": submitted_at,
            "strategy_sha256": digest,
        })
        self._emit(
            "checkpoint_accepted",
            checkpoint_id=checkpoint_id,
            strategy_sha256=digest,
            message="candidate checkpoint accepted",
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
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                self._reject(request_id, "checkpoint request is not valid JSON")
                continue
            if not isinstance(payload, dict):
                self._reject(request_id, "checkpoint request must be an object")
                continue
            self._accept(request_id, payload)

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            self._process_requests()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._process_requests()
        (self.workspace / CONTROL_FILE).unlink(missing_ok=True)
        shutil.rmtree(self.runtime, ignore_errors=True)
        shutil.rmtree(self.trusted_runtime, ignore_errors=True)
        try:
            self.checkpoints.rmdir()
        except OSError:
            pass
        try:
            self.checkpoint_root.rmdir()
        except OSError:
            pass

    def latest_valid(self) -> FrozenCheckpoint | None:
        if not self.checkpoints.exists():
            return None
        candidates = [
            path for path in self.checkpoints.iterdir()
            if path.is_dir() and path.name.isdigit()
        ]
        for directory in sorted(candidates, key=lambda path: int(path.name), reverse=True):
            try:
                manifest = json.loads(
                    (directory / "checkpoint.json").read_text(encoding="utf-8")
                )
                if not isinstance(manifest, dict):
                    continue
                content = (directory / "files" / self.strategy_path).read_bytes()
                metadata = manifest["metadata"]
                digest = _sha256(content)
                if (
                    set(manifest) != {
                        "schema_version",
                        "checkpoint_id",
                        "submitted_at",
                        "strategy_path",
                        "strategy_sha256",
                        "parent_champion_sha256",
                        "metadata",
                    }
                    or manifest.get("schema_version") != 1
                    or manifest.get("checkpoint_id") != directory.name
                    or manifest.get("strategy_path") != self.strategy_path
                    or manifest.get("strategy_sha256") != digest
                    or manifest.get("parent_champion_sha256") != self.parent_champion_sha256
                    or not isinstance(manifest.get("submitted_at"), str)
                    or not manifest["submitted_at"].strip()
                    or not _valid_metadata(metadata)
                ):
                    continue
                compile(content.decode("utf-8"), self.strategy_path, "exec")
            except (OSError, KeyError, UnicodeDecodeError, SyntaxError, json.JSONDecodeError):
                continue
            return FrozenCheckpoint(
                checkpoint_id=directory.name,
                submitted_at=str(manifest["submitted_at"]),
                strategy_path=self.strategy_path,
                strategy_sha256=digest,
                strategy_content=content,
                metadata=dict(metadata),
            )
        return None


def submit(metadata_path: Path, *, workspace: Path = Path(".")) -> Mapping[str, Any]:
    root = workspace.resolve()
    control = json.loads((root / CONTROL_FILE).read_text(encoding="utf-8"))
    if not isinstance(control, dict) or set(control) != {
        "schema_version", "strategy_path", "runtime_dir",
    } or control.get("schema_version") != 1:
        raise ValueError("checkpoint control file is invalid")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not _valid_metadata(metadata):
        raise ValueError("checkpoint metadata is invalid")
    strategy_path = str(control["strategy_path"])
    content = (root / strategy_path).read_bytes()
    request_id = uuid.uuid4().hex
    runtime = root / str(control["runtime_dir"])
    requests = runtime / "requests"
    ack_path = runtime / "acks" / f"{request_id}.json"
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "metadata": metadata,
        "strategy": {
            "path": strategy_path,
            "sha256": _sha256(content),
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    }
    write_json_atomic(requests / f"{request_id}.json", payload)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if ack_path.exists():
            result = json.loads(ack_path.read_text(encoding="utf-8"))
            if result.get("status") != "accepted":
                raise RuntimeError(str(result.get("error", "checkpoint rejected")))
            return result
        time.sleep(0.05)
    raise TimeoutError("Harness did not acknowledge the checkpoint")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python3 -m quant_core.research.checkpoint")
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("metadata", type=Path)
    args = parser.parse_args()
    result = submit(args.metadata)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
