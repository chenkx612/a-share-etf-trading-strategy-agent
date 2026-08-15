"""Round deadline status, phase warnings, and safe liveness heartbeats."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from quant_core.research.storage import write_json_atomic


EventSink = Callable[..., None]
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60


@dataclass
class RoundClock:
    path: Path
    timeout_seconds: int
    event_sink: EventSink | None
    event_details: Mapping[str, Any]
    monotonic: Callable[[], float] = time.monotonic
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("Round timeout must be positive")
        if self.heartbeat_interval_seconds < 1:
            raise ValueError("heartbeat interval must be positive")
        self.started_at = datetime.now(timezone.utc)
        self.deadline = self.started_at + timedelta(seconds=self.timeout_seconds)
        self._started_monotonic = self.monotonic()
        self._last_heartbeat_monotonic = self._started_monotonic
        self._heartbeat_sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warnings_emitted: set[int] = set()

    @property
    def deadline_text(self) -> str:
        return self.deadline.isoformat()

    @property
    def deadline_monotonic(self) -> float:
        return self._started_monotonic + self.timeout_seconds

    def _remaining_seconds(self) -> int:
        elapsed = max(0.0, self.monotonic() - self._started_monotonic)
        return max(0, int(math.ceil(self.timeout_seconds - elapsed)))

    @staticmethod
    def _phase(remaining: int) -> str:
        if remaining <= 60:
            return "submit_now"
        if remaining <= 5 * 60:
            return "finalize"
        if remaining <= 15 * 60:
            return "converge"
        return "research"

    def _emit(self, event: str, **details: Any) -> None:
        if self.event_sink is not None:
            self.event_sink(event, **details)

    def _emit_heartbeat(self, now: float) -> None:
        if now - self._last_heartbeat_monotonic < self.heartbeat_interval_seconds:
            return
        self._last_heartbeat_monotonic = now
        self._heartbeat_sequence += 1
        self._emit(
            "agent_heartbeat",
            sequence=self._heartbeat_sequence,
            elapsed_seconds=max(0.0, now - self._started_monotonic),
            process_alive=True,
            message="agent process remains active",
            **self.event_details,
        )

    def _write_status(self) -> None:
        now = self.monotonic()
        remaining = self._remaining_seconds()
        write_json_atomic(self.path, {
            "schema_version": 1,
            "started_at": self.started_at.isoformat(),
            "deadline": self.deadline_text,
            "timeout_seconds": self.timeout_seconds,
            "remaining_seconds": remaining,
            "phase": self._phase(remaining),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self._emit_heartbeat(now)
        for threshold in (15, 5, 1):
            if (
                threshold not in self._warnings_emitted
                and self.timeout_seconds > threshold * 60
                and remaining <= threshold * 60
            ):
                self._warnings_emitted.add(threshold)
                self._emit(
                    "round_time_warning",
                    remaining_minutes=threshold,
                    message=f"{threshold} minutes remaining",
                    **self.event_details,
                )

    def _run(self) -> None:
        while not self._stop.is_set():
            self._write_status()
            remaining = self._remaining_seconds()
            if remaining == 0:
                return
            self._stop.wait(min(5.0, float(remaining)))

    def start(self) -> None:
        self._write_status()
        self._thread = threading.Thread(
            target=self._run,
            name="quant-research-round-clock",
            daemon=True,
        )
        self._thread.start()

    def stop(self, finished_monotonic: float | None = None) -> dict[str, Any]:
        finished = self.monotonic() if finished_monotonic is None else finished_monotonic
        duration = max(0.0, finished - self._started_monotonic)
        finished_at = datetime.now(timezone.utc).isoformat()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.path.unlink(missing_ok=True)
        return {
            "started_at": self.started_at.isoformat(),
            "deadline": self.deadline_text,
            "finished_at": finished_at,
            "timeout_seconds": self.timeout_seconds,
            "duration_seconds": duration,
        }
