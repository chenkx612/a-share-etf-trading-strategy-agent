from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quant_core.recommendation.context import (
    _atomic_csv,
    _normalize_symbol,
    load_production_context,
)
from quant_core.recommendation.models import (
    EXECUTION_SEMANTICS,
    PRODUCTION_SCHEMA_VERSION,
    SHANGHAI,
    ParameterSearchError,
    ProductionContext,
    SearchResult,
    StrategyDataRequirements,
)
from quant_core.recommendation.replay import (
    _next_trade_date,
    _refresh_data,
    _target_holdings,
    _validate_daily_requirements,
    _write_curve_png,
    causal_replay,
    closed_market_data_end,
    next_schedule_boundary,
    resolve_signal_date,
)
from quant_core.recommendation.search import (
    _freeze_path,
    _freeze_search,
    _legacy_parameter_store,
    _recommendation_output_dir,
    _valid_freeze,
    search_parameters,
)
from quant_core.research.workspace import write_json_atomic
from quant_core.schedule import latest_schedule_boundary

def run_recommendation(
    root: str | Path,
    task_path: str | Path,
    *,
    requested_date: date | None = None,
    skip_refresh: bool = False,
    now: datetime | None = None,
) -> Path:
    context: ProductionContext | None = None
    signal_date: pd.Timestamp | None = None
    try:
        context = load_production_context(root, task_path)
        current = now or datetime.now(SHANGHAI)
        requested = requested_date or current.date()
        refresh_end = closed_market_data_end(requested, now=current)
        daily_raw = _refresh_data(context, refresh_end, skip_refresh=skip_refresh)
        normalized_symbols = daily_raw["symbol"].map(_normalize_symbol)
        universe_raw = daily_raw[
            normalized_symbols.isin(set(context.universe["symbol"]))
        ].copy()
        signal_date = resolve_signal_date(universe_raw, requested, now=current)
        daily = _validate_daily_requirements(daily_raw, context, signal_date)
        universe_daily = daily[daily["symbol"].isin(set(context.universe["symbol"]))]
        all_dates = pd.DatetimeIndex(universe_raw["date"].unique()).sort_values()
        visible_dates = pd.DatetimeIndex(universe_daily["date"].unique()).sort_values()
        production = context.task.production
        assert production is not None
        parameter_selection = context.task.parameter_selection
        assert parameter_selection is not None

        schedule_boundary = latest_schedule_boundary(
            signal_date,
            parameter_selection["schedule"],
            visible_dates,
        )
        expected_path = _freeze_path(context, schedule_boundary, visible_dates)
        legacy_expected_path = (
            _legacy_parameter_store(context) / expected_path.name
        )
        expected_freeze = None
        for candidate_path in (expected_path, legacy_expected_path):
            candidate = _valid_freeze(
                candidate_path,
                context.hashes,
                on_or_before=signal_date,
            )
            if candidate is not None:
                expected_path = candidate_path
                expected_freeze = candidate
                break
        if expected_freeze is not None:
            freeze_path = expected_path
            freeze_payload = expected_freeze
            parameters = expected_freeze["parameters"]
            search_status = "reused"
        else:
            search = search_parameters(
                context,
                daily[daily["date"] <= schedule_boundary],
                schedule_boundary,
            )
            freeze_path = _freeze_search(context, search, visible_dates)
            freeze_payload = {
                "searched_on": search.signal_date.date().isoformat(),
                "parameters": search.parameters,
            }
            parameters = search.parameters
            search_status = "searched"

        trade_date = _next_trade_date(signal_date, all_dates)
        last_tuning_date = pd.Timestamp(freeze_payload["searched_on"]).normalize()
        next_tuning_date = next_schedule_boundary(
            signal_date,
            parameter_selection["schedule"],
            visible_dates,
        )
        holdings = _target_holdings(
            context,
            daily,
            schedule_boundary,
            signal_date,
            trade_date,
            parameters,
        )
        curve, curve_metrics, replay_audit = causal_replay(context, daily, signal_date)

        output_dir = _recommendation_output_dir(context)
        recommendation_path = output_dir / "recommendation.csv"
        curve_path = output_dir / "causal_curve.csv"
        png_path = output_dir / "causal_curve.png"
        search_path = output_dir / "parameter_search.json"
        _atomic_csv(holdings, recommendation_path)
        _atomic_csv(curve, curve_path)
        _write_curve_png(curve, curve_metrics, png_path, str(production["benchmark"]))
        write_json_atomic(
            search_path,
            {
                "schema_version": PRODUCTION_SCHEMA_VERSION,
                "status": search_status,
                "freeze_path": str(freeze_path.relative_to(context.root)),
                "parameters": parameters,
                "causal_replay_searches": replay_audit,
            },
        )
        summary_path = output_dir / "summary.json"
        write_json_atomic(
            summary_path,
            {
                "schema_version": PRODUCTION_SCHEMA_VERSION,
                "status": "completed",
                "task_id": context.task.task_id,
                "task_path": str(context.task_path.relative_to(context.root)),
                "signal_date": signal_date.date().isoformat(),
                "trade_date": trade_date.date().isoformat(),
                "search_status": search_status,
                "parameter_train_months": int(parameter_selection["train_months"]),
                "parameter_schedule": dict(parameter_selection["schedule"]),
                "last_tuning_date": last_tuning_date.date().isoformat(),
                "next_tuning_date": next_tuning_date.date().isoformat(),
                "parameters": parameters,
                "champion_number": context.champion.get("champion_number"),
                "champion_round_id": context.champion.get("champion_round_id"),
                "input_hashes": dict(context.hashes),
                "execution_semantics": EXECUTION_SEMANTICS,
                "curve_metrics": curve_metrics,
                "benchmark": str(production["benchmark"]),
                "survivorship_bias": (
                    "历史重放使用当前股票池快照，存在幸存者偏差；该曲线是严格因果研究重放，"
                    "不代表真实历史实盘净值。"
                ),
                "recommendation_path": str(recommendation_path.relative_to(context.root)),
                "curve_csv_path": str(curve_path.relative_to(context.root)),
                "curve_png_path": str(png_path.relative_to(context.root)),
                "parameter_search_path": str(search_path.relative_to(context.root)),
            },
        )
        return summary_path
    except Exception as exc:
        root_path = Path(root).resolve()
        task_id = context.task.task_id if context is not None else Path(task_path).stem
        failure_dir = root_path / ".cache" / "production" / task_id / "failures"
        failure_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
        write_json_atomic(
            failure_dir / f"{timestamp}.json",
            {
                "schema_version": PRODUCTION_SCHEMA_VERSION,
                "status": "failed",
                "task_id": task_id,
                "signal_date": (
                    signal_date.date().isoformat() if signal_date is not None else None
                ),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "input_hashes": dict(context.hashes) if context is not None else None,
                "search_rows": (
                    list(exc.rows) if isinstance(exc, ParameterSearchError) else None
                ),
            },
        )
        raise
