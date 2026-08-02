from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REPORT_FACTS_SCHEMA_VERSION = 1


def _sha256(content: bytes | None) -> str | None:
    if content is None:
        return None
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _apply_patch(
    source: bytes | None,
    patch_path: Path,
    strategy_path: str,
    *,
    reverse: bool,
) -> bytes | None:
    if patch_path.read_bytes() == b"":
        return source
    with tempfile.TemporaryDirectory(prefix="quant-report-facts-") as temporary:
        root = Path(temporary)
        strategy = root / strategy_path
        if source is not None:
            strategy.parent.mkdir(parents=True, exist_ok=True)
            strategy.write_bytes(source)
        command = [
            "git",
            "apply",
            "--binary",
            "--whitespace=nowarn",
        ]
        if reverse:
            command.append("--reverse")
        command.append(str(patch_path.resolve()))
        completed = subprocess.run(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                detail or f"git apply exited with code {completed.returncode}"
            )
        return strategy.read_bytes() if strategy.is_file() else None


def _unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return ast.dump(node, include_attributes=False)


def _python_structure(source: bytes | None) -> dict[str, Any] | None:
    if source is None:
        return {
            "parameters": {},
            "definitions": {},
            "branches": {},
            "grid_loops": {},
        }
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return None

    parameters: dict[str, str | None] = {}
    definitions: dict[str, str] = {}
    branches: dict[str, list[str]] = {}
    grid_loops: dict[str, str] = {}

    def visit_body(body: Sequence[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = f"{prefix}.{node.name}" if prefix else node.name
                definitions[qualified] = (
                    "class"
                    if isinstance(node, ast.ClassDef)
                    else hashlib.sha256(
                        ast.dump(node, include_attributes=False).encode("utf-8")
                    ).hexdigest()
                )
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    tests = sorted({
                        rendered
                        for child in ast.walk(node)
                        if isinstance(child, (ast.If, ast.IfExp))
                        for rendered in [_unparse(child.test)]
                        if rendered is not None
                    })
                    branches[qualified] = tests
                    if "grid" in node.name:
                        for child in ast.walk(node):
                            if isinstance(child, ast.For):
                                target = _unparse(child.target)
                                iterator = _unparse(child.iter)
                                if target is not None and iterator is not None:
                                    grid_loops[f"{qualified}:{target}"] = iterator
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if (
                            isinstance(child, ast.AnnAssign)
                            and isinstance(child.target, ast.Name)
                        ):
                            parameters[f"{qualified}.{child.target.id}"] = _unparse(
                                child.value
                            )
                visit_body(node.body, qualified)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                parameters[node.target.id] = _unparse(node.value)

    visit_body(tree.body)
    return {
        "parameters": parameters,
        "definitions": definitions,
        "branches": branches,
        "grid_loops": grid_loops,
    }


def _mapping_changes(
    parent: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    parent_keys = set(parent)
    candidate_keys = set(candidate)
    return {
        "added": {key: candidate[key] for key in sorted(candidate_keys - parent_keys)},
        "removed": {key: parent[key] for key in sorted(parent_keys - candidate_keys)},
        "changed": {
            key: {"parent": parent[key], "candidate": candidate[key]}
            for key in sorted(parent_keys & candidate_keys)
            if parent[key] != candidate[key]
        },
    }


def _structured_changes(
    parent_source: bytes | None,
    candidate_source: bytes | None,
) -> dict[str, Any]:
    parent = _python_structure(parent_source)
    candidate = _python_structure(candidate_source)
    if parent is None or candidate is None:
        return {
            "status": "unclassified",
            "attribution": "combined_change",
            "attribution_warning": (
                "Python structure could not be parsed; single-mechanism attribution is prohibited"
            ),
        }

    parameter_changes = _mapping_changes(
        parent["parameters"],
        candidate["parameters"],
    )
    definition_changes = _mapping_changes(
        parent["definitions"],
        candidate["definitions"],
    )
    branch_changes = _mapping_changes(parent["branches"], candidate["branches"])
    grid_changes = _mapping_changes(parent["grid_loops"], candidate["grid_loops"])
    categories = {
        "parameters": parameter_changes,
        "definitions": definition_changes,
        "branches": branch_changes,
        "grid": grid_changes,
    }
    changed_categories = [
        name
        for name, changes in categories.items()
        if changes["added"] or changes["removed"] or changes["changed"]
    ]
    parameter_change_count = sum(
        len(parameter_changes[key])
        for key in ("added", "removed", "changed")
    )
    single_explicit_parameter_change = (
        changed_categories == ["parameters"]
        and parameter_change_count == 1
    )
    attribution = (
        "no_behavior_change"
        if not changed_categories
        else "single_structural_change"
        if single_explicit_parameter_change
        else "combined_change"
    )
    warning = (
        "Candidate contains multiple or opaque structural changes; "
        "single-mechanism attribution is prohibited"
        if attribution == "combined_change"
        else None
    )
    return {
        "status": "classified",
        "attribution": attribution,
        "attribution_warning": warning,
        "changed_categories": changed_categories,
        **categories,
    }


def _submission_sha256(
    result: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> str | None:
    for record in (decision, result):
        submission = record.get("submission")
        if isinstance(submission, Mapping):
            value = submission.get("strategy_sha256")
            if isinstance(value, str):
                return value
    return None


def build_report_facts(
    rounds_root: Path,
    round_ids: Sequence[str],
    frozen_report_input: Mapping[str, Any],
) -> dict[str, Any]:
    champion = frozen_report_input.get("champion")
    champion = champion if isinstance(champion, Mapping) else {}
    strategy_path = champion.get("strategy_path")
    if not isinstance(strategy_path, str) or not strategy_path:
        strategy_path = ""
    final_source_value = champion.get("strategy_source")
    final_source = (
        final_source_value.encode("utf-8")
        if isinstance(final_source_value, str)
        else None
    )
    final_sha256 = champion.get("sha256")
    final_sha256 = final_sha256 if isinstance(final_sha256, str) else None
    warnings: list[str] = []
    if _sha256(final_source) != final_sha256:
        warnings.append(
            "frozen final Champion source hash does not match champion.sha256"
        )

    records: dict[str, dict[str, Any]] = {}
    champion_cursor = final_source
    cursor_known = _sha256(final_source) == final_sha256
    for round_id in reversed(round_ids):
        experiment = rounds_root / round_id
        result = _read_json(experiment / "result.json")
        decision = _read_json(experiment / "decision.json")
        decision_value = decision.get("decision")
        submitted_sha256 = _submission_sha256(result, decision)
        patch_path = experiment / "candidate.patch"
        patch_sha256 = (
            hashlib.sha256(patch_path.read_bytes()).hexdigest()
            if patch_path.is_file()
            else None
        )
        recorded_patch_sha256 = decision.get("candidate_patch_sha256")
        round_warnings: list[str] = []
        patch_authenticated = (
            isinstance(recorded_patch_sha256, str)
            and recorded_patch_sha256 == patch_sha256
        )
        if patch_path.is_file() and not isinstance(recorded_patch_sha256, str):
            round_warnings.append(
                "decision lacks candidate_patch_sha256; Candidate Patch is unauthenticated"
            )
        elif isinstance(recorded_patch_sha256, str) and not patch_authenticated:
            round_warnings.append(
                "candidate.patch hash does not match decision.candidate_patch_sha256"
            )

        champion_after_known = cursor_known
        champion_after = champion_cursor if champion_after_known else None
        parent_source: bytes | None = champion_after
        candidate_source: bytes | None = None
        parent_known = champion_after_known
        candidate_known = False
        replay_verified = False
        if decision_value == "accepted":
            candidate_source = champion_after
            candidate_matches = (
                champion_after_known
                and isinstance(submitted_sha256, str)
                and _sha256(candidate_source) == submitted_sha256
            )
            if not champion_after_known:
                round_warnings.append("accepted Candidate source is unavailable")
            elif not candidate_matches:
                round_warnings.append(
                    "accepted Champion hash does not match submitted Candidate hash"
                )
            candidate_known = candidate_matches
            if (
                patch_authenticated
                and candidate_matches
                and strategy_path
            ):
                try:
                    parent_source = _apply_patch(
                        candidate_source,
                        patch_path,
                        strategy_path,
                        reverse=True,
                    )
                except RuntimeError as exc:
                    round_warnings.append(f"candidate.patch reverse replay failed: {exc}")
                    parent_known = False
                else:
                    parent_known = True
                    replay_verified = True
            else:
                round_warnings.append("accepted Round lacks replayable Candidate evidence")
                parent_known = False
            champion_cursor = parent_source if parent_known else None
            cursor_known = parent_known
        elif decision_value == "rejected":
            if patch_authenticated and champion_after_known and strategy_path:
                try:
                    candidate_source = _apply_patch(
                        parent_source,
                        patch_path,
                        strategy_path,
                        reverse=False,
                    )
                except RuntimeError as exc:
                    round_warnings.append(f"candidate.patch replay failed: {exc}")
                else:
                    candidate_known = (
                        isinstance(submitted_sha256, str)
                        and _sha256(candidate_source) == submitted_sha256
                    )
                    if candidate_known:
                        replay_verified = True
            elif result.get("status") == "completed":
                round_warnings.append(
                    "completed rejected Round lacks replayable Candidate evidence"
                )
            if candidate_source is not None and not candidate_known:
                round_warnings.append(
                    "replayed Candidate hash does not match submission.strategy_sha256"
                )

        changes = (
            _structured_changes(parent_source, candidate_source)
            if parent_known and candidate_known
            else {
                "status": "unavailable",
                "attribution": "unknown",
                "attribution_warning": (
                    "Candidate change facts are unavailable; mechanism attribution is prohibited"
                ),
            }
        )
        if (
            decision_value == "accepted"
            and isinstance(changes.get("attribution_warning"), str)
        ):
            round_warnings.append(str(changes["attribution_warning"]))
        warnings.extend(f"round {round_id}: {warning}" for warning in round_warnings)
        records[round_id] = {
            "experiment_id": round_id,
            "decision": decision_value,
            "parent_champion_sha256": _sha256(parent_source) if parent_known else None,
            "candidate_sha256": _sha256(candidate_source) if candidate_known else None,
            "submitted_candidate_sha256": submitted_sha256,
            "candidate_patch_sha256": patch_sha256,
            "recorded_candidate_patch_sha256": recorded_patch_sha256,
            "champion_after_sha256": (
                _sha256(champion_after) if champion_after_known else None
            ),
            "patch_replay_verified": replay_verified,
            "changes": changes,
            "integrity_warnings": round_warnings,
        }

    ordered = [records[round_id] for round_id in round_ids if round_id in records]
    return {
        "schema_version": REPORT_FACTS_SCHEMA_VERSION,
        "strategy_path": strategy_path or None,
        "final_champion_sha256": final_sha256,
        "rounds": ordered,
        "integrity_warnings": warnings,
    }


def validate_report_facts(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != REPORT_FACTS_SCHEMA_VERSION:
        raise ValueError("Frozen report facts use an incompatible schema")
    if not isinstance(payload.get("rounds"), list):
        raise ValueError("Frozen report facts rounds must be a list")
    if not isinstance(payload.get("integrity_warnings"), list):
        raise ValueError("Frozen report facts integrity_warnings must be a list")
