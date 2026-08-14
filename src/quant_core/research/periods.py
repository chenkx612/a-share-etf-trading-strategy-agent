from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from quant_core.data.market_data import ProjectPaths, load_universe, read_daily
from quant_core.research.contracts import ResearchTask
from quant_core.schedule import latest_schedule_boundary


def _first_weekday_on_or_after(value: pd.Timestamp) -> pd.Timestamp:
    """Return the first weekday on or after a calendar period boundary."""
    value = pd.Timestamp(value).normalize()
    while value.weekday() >= 5:
        value += pd.Timedelta(days=1)
    return value


def _has_required_history(
    actual_start: pd.Timestamp,
    required_start: pd.Timestamp,
) -> bool:
    """Accept a start on Monday when a month offset lands on a weekend.

    Relative windows are calendar-month based, while market data only contains
    trading sessions.  A weekday requirement remains exact so missing market
    data is not treated as sufficient history.
    """
    return actual_start <= _first_weekday_on_or_after(required_start)


def _period(
    dates: pd.DatetimeIndex,
    start: pd.Timestamp,
    end: pd.Timestamp,
    label: str,
) -> dict[str, str]:
    selected = dates[(dates >= start) & (dates < end)]
    if selected.empty:
        raise ValueError(
            f"relative {label} period contains no trading dates in "
            f"{start.date()}..{(end - pd.Timedelta(days=1)).date()}"
        )
    return {
        "start": selected[0].date().isoformat(),
        "end": selected[-1].date().isoformat(),
    }


def resolve_relative_periods(
    task: ResearchTask,
    *,
    source: Path,
    runtime: Path,
) -> ResearchTask:
    config = task.relative_period_config
    if config is None:
        return task

    universe_path = Path(str(task.raw["data"]["universe"]))
    if not universe_path.is_absolute():
        universe_path = source / universe_path
    universe = load_universe(universe_path)
    symbols = set(universe["symbol"].astype(str))
    daily = read_daily(ProjectPaths(runtime)).copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="raise")
    daily["symbol"] = daily["symbol"].astype(str)
    scoped = daily[daily["symbol"].isin(symbols)]
    available = set(scoped["symbol"])
    missing_all = sorted(symbols - available)
    if missing_all:
        raise ValueError(
            "relative period resolution found universe symbols with no market data: "
            f"{missing_all}"
        )
    if scoped.empty:
        raise ValueError("relative period resolution found no universe market data")

    anchor = pd.Timestamp(scoped["date"].max())
    present = set(scoped.loc[scoped["date"].eq(anchor), "symbol"])
    missing_latest = sorted(symbols - present)
    if missing_latest:
        raise ValueError(
            "latest universe market-data date is incomplete: "
            f"date={anchor.date().isoformat()}, missing={missing_latest}"
        )

    dates = pd.DatetimeIndex(scoped["date"].drop_duplicates()).sort_values()
    exclusive_end = anchor + pd.Timedelta(days=1)
    guard_months = int(config.get("guard_months", 0))
    guard_start = exclusive_end - pd.DateOffset(months=guard_months)
    gate_start = guard_start - pd.DateOffset(months=int(config["gate_months"]))
    development_start = gate_start - pd.DateOffset(
        months=int(config["development_months"])
    )
    assert task.parameter_selection is not None
    train_months = int(task.parameter_selection["train_months"])
    actual_start = pd.Timestamp(dates[0])
    minimum_required_start = development_start - pd.DateOffset(
        months=train_months
    )
    if not _has_required_history(actual_start, minimum_required_start):
        raise ValueError(
            "insufficient market-data history for relative walk-forward periods: "
            f"required_start<={minimum_required_start.date().isoformat()}, "
            f"actual_start={actual_start.date().isoformat()}"
        )

    periods: dict[str, dict[str, str]] = {
        "development": _period(dates, development_start, gate_start, "development"),
        "gate": _period(dates, gate_start, guard_start, "gate"),
    }
    if guard_months:
        periods["guard"] = _period(dates, guard_start, exclusive_end, "guard")

    schedule = task.parameter_selection["schedule"]
    development_first = pd.Timestamp(periods["development"]["start"])
    try:
        replay_start = latest_schedule_boundary(
            development_first, schedule, dates
        )
    except ValueError as exc:
        raise ValueError(
            "insufficient market-data history for the first Development "
            "walk-forward boundary"
        ) from exc
    required_start = replay_start - pd.DateOffset(
        months=train_months
    )
    if not _has_required_history(actual_start, required_start):
        raise ValueError(
            "insufficient market-data history for relative walk-forward periods: "
            f"required_start<={required_start.date().isoformat()}, "
            f"actual_start={actual_start.date().isoformat()}"
        )

    resolution: dict[str, Any] = {
        "schema_version": 1,
        "anchor": anchor.date().isoformat(),
        "anchor_policy": str(config["anchor"]),
        "actual_data_start": actual_start.date().isoformat(),
        "actual_data_end": anchor.date().isoformat(),
        "required_training_start": required_start.date().isoformat(),
        "configured_months": {
            "development": int(config["development_months"]),
            "gate": int(config["gate_months"]),
            "guard": guard_months or None,
        },
    }
    return task.with_resolved_periods(periods, resolution)


def bind_persisted_periods(
    task: ResearchTask,
    payload: Mapping[str, Any],
) -> ResearchTask:
    periods = payload.get("periods")
    resolution = payload.get("resolution")
    if not isinstance(periods, Mapping) or not isinstance(resolution, Mapping):
        raise ValueError("resolved period manifest is invalid")
    return task.with_resolved_periods(periods, resolution)
