"""Validation for the versioned experiment result contract."""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Mapping

from quant_core.research.task_validation import required


def _string_list(data: Mapping[str, Any], key: str) -> None:
    value = data.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(f"{key} must be a non-empty list of strings")


def validate_experiment_result(data: Mapping[str, Any]) -> None:
    required(data, "experiment_id", str, "result")
    status = required(data, "status", str, "result")
    if status not in {"completed", "failed"}:
        raise ValueError("result.status must be 'completed' or 'failed'")
    failure_kind = data.get("failure_kind")
    if failure_kind is not None and failure_kind != "infrastructure":
        raise ValueError("result.failure_kind must be 'infrastructure' when present")
    if status == "completed" and failure_kind is not None:
        raise ValueError("completed result must not declare failure_kind")
    failure_code = data.get("failure_code")
    if failure_code is not None and (
        failure_kind != "infrastructure"
        or not isinstance(failure_code, str)
        or not failure_code.strip()
    ):
        raise ValueError(
            "result.failure_code must be a non-empty string for infrastructure failures"
        )
    environment_sha256 = data.get("evaluation_environment_sha256")
    if environment_sha256 is not None and (
        not isinstance(environment_sha256, str)
        or len(environment_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in environment_sha256
        )
    ):
        raise ValueError(
            "result.evaluation_environment_sha256 must be a SHA-256 digest"
        )
    development_view_sha256 = data.get("development_view_sha256")
    development_end = data.get("development_end")
    if (development_view_sha256 is None) != (development_end is None):
        raise ValueError(
            "result Development view hash and end must be declared together"
        )
    if development_view_sha256 is not None:
        if (
            not isinstance(development_view_sha256, str)
            or len(development_view_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in development_view_sha256
            )
        ):
            raise ValueError(
                "result.development_view_sha256 must be a SHA-256 digest"
            )
        if not isinstance(development_end, str):
            raise ValueError("result.development_end must be an ISO date")
        try:
            date.fromisoformat(development_end)
        except ValueError as exc:
            raise ValueError("result.development_end must be an ISO date") from exc
    feedback = data.get("feedback")
    if feedback is not None and (not isinstance(feedback, str) or not feedback.strip()):
        raise ValueError("result.feedback must be a non-empty str when present")
    submission = data.get("submission")
    if submission is not None:
        if not isinstance(submission, dict):
            raise ValueError("result.submission must be an object")
        mode = submission.get("mode")
        expected = {
            "mode", "submitted_at", "submitted_by_timeout", "strategy_sha256",
        }
        if mode == "checkpoint":
            expected.add("checkpoint_id")
        if mode not in {"final", "checkpoint"} or set(submission) != expected:
            raise ValueError("result.submission has invalid fields")
        if not isinstance(submission.get("submitted_at"), str) or not submission["submitted_at"].strip():
            raise ValueError("result.submission.submitted_at must be a non-empty string")
        submitted_by_timeout = submission.get("submitted_by_timeout")
        if not isinstance(submitted_by_timeout, bool) or submitted_by_timeout != (mode == "checkpoint"):
            raise ValueError("result.submission timeout marker does not match its mode")
        strategy_sha256 = submission.get("strategy_sha256")
        if (
            not isinstance(strategy_sha256, str)
            or len(strategy_sha256) != 64
            or any(character not in "0123456789abcdef" for character in strategy_sha256)
        ):
            raise ValueError("result.submission.strategy_sha256 must be a SHA-256 digest")
        if mode == "checkpoint" and (
            not isinstance(submission.get("checkpoint_id"), str)
            or not submission["checkpoint_id"].isdigit()
            or int(submission["checkpoint_id"]) < 1
        ):
            raise ValueError("result.submission.checkpoint_id must be a positive numeric ID")
    round_timing = data.get("round_timing")
    if round_timing is not None:
        if not isinstance(round_timing, dict) or set(round_timing) != {
            "started_at",
            "deadline",
            "finished_at",
            "timeout_seconds",
            "duration_seconds",
        }:
            raise ValueError("result.round_timing has invalid fields")
        for key in ("started_at", "deadline", "finished_at"):
            required(round_timing, key, str, "result.round_timing")
        timeout_seconds = round_timing["timeout_seconds"]
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds < 1
        ):
            raise ValueError("result.round_timing.timeout_seconds must be a positive integer")
        duration_seconds = round_timing["duration_seconds"]
        if (
            not isinstance(duration_seconds, (int, float))
            or isinstance(duration_seconds, bool)
            or not math.isfinite(duration_seconds)
            or duration_seconds < 0
        ):
            raise ValueError(
                "result.round_timing.duration_seconds must be finite and non-negative"
            )
    development_attempts = data.get("development_attempts")
    if development_attempts is not None:
        if not isinstance(development_attempts, list):
            raise ValueError("result.development_attempts must be a list")
        seen_ids: set[str] = set()
        seen_hashes: set[str] = set()
        submitted = 0
        for index, attempt in enumerate(development_attempts):
            context = f"result.development_attempts[{index}]"
            legacy_fields = {
                "attempt_id",
                "candidate_sha256",
                "hypothesis",
                "development_metrics",
                "outcome",
                "learning",
            }
            current_fields = legacy_fields | {
                "development_view_sha256",
                "development_end",
            }
            if (
                not isinstance(attempt, dict)
                or frozenset(attempt) not in {
                    frozenset(legacy_fields),
                    frozenset(current_fields),
                }
                or (
                    set(attempt) == legacy_fields
                    and isinstance(data.get("development_view_sha256"), str)
                )
            ):
                raise ValueError(f"{context} has invalid fields")
            attempt_id = required(attempt, "attempt_id", str, context)
            if (
                not attempt_id.isdigit()
                or int(attempt_id) < 1
                or attempt_id in seen_ids
            ):
                raise ValueError(f"{context}.attempt_id must be a unique positive numeric ID")
            seen_ids.add(attempt_id)
            digest = required(attempt, "candidate_sha256", str, context)
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or digest in seen_hashes
            ):
                raise ValueError(f"{context}.candidate_sha256 must be a unique SHA-256 digest")
            seen_hashes.add(digest)
            if set(attempt) == legacy_fields:
                view_digest = None
                attempt_end = None
            else:
                view_digest = required(
                    attempt, "development_view_sha256", str, context
                )
                attempt_end = required(attempt, "development_end", str, context)
            if view_digest is not None and (
                len(view_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in view_digest
                )
            ):
                raise ValueError(
                    f"{context}.development_view_sha256 must be a SHA-256 digest"
                )
            if attempt_end is not None:
                try:
                    date.fromisoformat(attempt_end)
                except ValueError as exc:
                    raise ValueError(
                        f"{context}.development_end must be an ISO date"
                    ) from exc
            if view_digest is not None and data.get("development_view_sha256") != view_digest:
                raise ValueError(
                    f"{context} does not match the frozen Development view"
                )
            if attempt_end is not None and data.get("development_end") != attempt_end:
                raise ValueError(
                    f"{context} does not match the frozen Development end"
                )
            required(attempt, "hypothesis", str, context)
            if not isinstance(attempt.get("development_metrics"), dict):
                raise ValueError(f"{context}.development_metrics must be an object")
            outcome = attempt.get("outcome")
            if outcome not in {"abandoned", "submitted"}:
                raise ValueError(f"{context}.outcome is invalid")
            submitted += outcome == "submitted"
            learning = attempt.get("learning")
            if learning is not None and (
                not isinstance(learning, str) or not learning.strip()
            ):
                raise ValueError(f"{context}.learning must be null or a non-empty string")
        if submitted > 1:
            raise ValueError("result.development_attempts may contain only one submitted attempt")
    if status == "completed":
        required(data, "hypothesis", str, "result")
        required(data, "attempts", str, "result")
        required(data, "development_effect", str, "result")
        required(data, "candidate", str, "result")
        changes = required(data, "changes", dict, "result")
        if set(changes) != {"files"}:
            raise ValueError("result.changes must contain exactly files")
        _string_list(changes, "files")

        metrics = required(data, "metrics", dict, "result")
        if "development" not in metrics or "gate" not in metrics:
            raise ValueError("result.metrics must contain development and gate metrics")
        if "test" in metrics:
            raise ValueError("result.metrics must not contain test metrics during the research loop")
        if "guard" in metrics:
            raise ValueError("result.metrics must not contain guard metrics during the research loop")
        if not all(isinstance(metrics[key], dict) for key in ("development", "gate")):
            raise ValueError("result development and gate metrics must be objects")
    else:
        required(data, "error", str, "result")


