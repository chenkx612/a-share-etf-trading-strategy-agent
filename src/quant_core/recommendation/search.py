from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from quant_core.recommendation.context import _canonical_json
from quant_core.recommendation.models import (
    PRODUCTION_SCHEMA_VERSION,
    ParameterSearchError,
    ProductionContext,
    SearchResult,
)
from quant_core.research.evaluator import evaluate_candidate
from quant_core.research.storage import write_json_atomic
from quant_core.schedule import schedule_bucket

def _passes_constraints(
    metrics: Mapping[str, float], constraints: Mapping[str, Mapping[str, object]]
) -> bool:
    for name, rule in constraints.items():
        value = metrics.get(name)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return False
        threshold = float(rule["threshold"])
        operator = rule["operator"]
        if operator == ">=" and value < threshold:
            return False
        if operator == "<=" and value > threshold:
            return False
        if operator == "abs<=" and abs(value) > threshold:
            return False
    return True


def search_parameters(
    context: ProductionContext,
    daily: pd.DataFrame,
    signal_date: pd.Timestamp,
) -> SearchResult:
    parameter_selection = context.task.parameter_selection
    assert parameter_selection is not None
    signal = pd.Timestamp(signal_date).normalize()
    train_start = signal - pd.DateOffset(
        months=int(parameter_selection["train_months"])
    )
    rows: list[dict[str, object]] = []
    feasible: list[tuple[float, str, dict[str, object], dict[str, float]]] = []
    objective = str(parameter_selection["objective"])
    constraints = parameter_selection["constraints"]
    assert isinstance(constraints, Mapping)
    for params in context.grid:
        _, result = evaluate_candidate(
            daily[daily["date"] <= signal].copy(),
            context.universe.copy(),
            train_start,
            signal,
            lambda d, u, s, e, p=params: context.strategy.select_with_params(d, u, s, e, p),
        )
        metrics = {key: float(value) for key, value in result.metrics.items()}
        valid = _passes_constraints(metrics, constraints)
        encoded = _canonical_json(params)
        rows.append({"parameters": dict(params), "metrics": metrics, "valid": valid})
        value = metrics.get(objective)
        if valid and value is not None and math.isfinite(value):
            feasible.append((float(value), encoded, dict(params), metrics))
    if not feasible:
        raise ParameterSearchError(
            f"parameter search at {signal.date()} found no set satisfying production constraints",
            rows,
        )
    feasible.sort(key=lambda item: (-item[0], item[1]))
    _, _, parameters, metrics = feasible[0]
    return SearchResult(
        signal_date=signal,
        train_start=train_start,
        train_end=signal,
        parameters=parameters,
        metrics=metrics,
        rows=tuple(rows),
    )


def _input_hashes_match(payload: Mapping[str, object], hashes: Mapping[str, str]) -> bool:
    value = payload.get("input_hashes")
    return isinstance(value, dict) and value == dict(hashes)


def _parameter_store(context: ProductionContext) -> Path:
    return (
        context.root
        / ".cache"
        / "production"
        / context.task.task_id
        / "parameters"
    )


def _legacy_parameter_store(context: ProductionContext) -> Path:
    return (
        context.root
        / "outputs"
        / "production"
        / context.task.task_id
        / "parameters"
    )


def _recommendation_output_dir(context: ProductionContext) -> Path:
    return context.root / "outputs" / context.task.task_id


def _freeze_path(
    context: ProductionContext,
    signal_date: pd.Timestamp,
    trading_dates: Sequence[pd.Timestamp],
) -> Path:
    parameter_selection = context.task.parameter_selection
    assert parameter_selection is not None
    bucket = schedule_bucket(
        signal_date,
        parameter_selection["schedule"],
        trading_dates,
    )
    safe_bucket = bucket.replace(":", "-")
    return _parameter_store(context) / f"{safe_bucket}.json"


def _freeze_search(
    context: ProductionContext,
    result: SearchResult,
    trading_dates: Sequence[pd.Timestamp],
) -> Path:
    parameter_selection = context.task.parameter_selection
    assert parameter_selection is not None
    path = _freeze_path(context, result.signal_date, trading_dates)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PRODUCTION_SCHEMA_VERSION,
        "task_id": context.task.task_id,
        "searched_on": result.signal_date.date().isoformat(),
        "train_start": result.train_start.date().isoformat(),
        "train_end": result.train_end.date().isoformat(),
        "parameters": result.parameters,
        "metrics": result.metrics,
        "objective": str(parameter_selection["objective"]),
        "input_hashes": dict(context.hashes),
        "search_rows": result.rows,
    }
    write_json_atomic(path, payload)
    return path


def _valid_freeze(
    path: Path,
    hashes: Mapping[str, str],
    *,
    on_or_before: pd.Timestamp | None = None,
) -> Mapping[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    if not _input_hashes_match(payload, hashes):
        return None
    if on_or_before is not None:
        try:
            searched_on = pd.Timestamp(payload["searched_on"]).normalize()
        except (KeyError, TypeError, ValueError):
            return None
        if searched_on > pd.Timestamp(on_or_before).normalize():
            return None
    return payload
