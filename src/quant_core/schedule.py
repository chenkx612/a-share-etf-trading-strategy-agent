from __future__ import annotations

from bisect import bisect_left
from datetime import date
from functools import lru_cache
from typing import Mapping, Sequence

import numpy as np
import pandas as pd


SUPPORTED_SCHEDULE_PERIODS = {"trading_day", "iso_week", "calendar_month"}


def _normalized_trading_dates(
    trading_dates: Sequence[pd.Timestamp],
) -> pd.DatetimeIndex:
    return (
        pd.DatetimeIndex(pd.to_datetime(trading_dates))
        .normalize()
        .unique()
        .sort_values()
    )


def validate_schedule(
    schedule: Mapping[str, object],
    *,
    context: str,
    require_start: bool = False,
) -> dict[str, object]:
    if set(schedule) != {"period", "interval", "trigger"}:
        raise ValueError(
            f"{context} must contain exactly period, interval, and trigger"
        )
    period = schedule.get("period")
    if not isinstance(period, str) or period not in SUPPORTED_SCHEDULE_PERIODS:
        raise ValueError(
            f"{context}.period must be trading_day, iso_week, or calendar_month"
        )
    interval = schedule.get("interval")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
        raise ValueError(f"{context}.interval must be a positive integer")
    trigger = schedule.get("trigger")
    if trigger not in {"start", "end"}:
        raise ValueError(f"{context}.trigger must be start or end")
    if require_start and trigger != "start":
        raise ValueError(f"{context}.trigger must be start")
    return {
        "period": period,
        "interval": interval,
        "trigger": trigger,
    }


def _period_ordinal(value: pd.Timestamp, period: str) -> int:
    if period == "calendar_month":
        return value.year * 12 + value.month - 1
    if period == "iso_week":
        monday = value.normalize() - pd.Timedelta(days=value.weekday())
        return int((monday - pd.Timestamp("1970-01-05")).days // 7)
    raise ValueError(f"period {period!r} does not have a calendar ordinal")


@lru_cache(maxsize=1)
def exchange_trade_dates() -> tuple[date, ...]:
    try:
        import akshare as ak

        calendar = ak.tool_trade_date_hist_sina()
        column = "trade_date" if "trade_date" in calendar.columns else calendar.columns[0]
        return tuple(sorted(pd.to_datetime(calendar[column]).dt.date.unique()))
    except Exception:
        return ()


def _trading_day_ordinal(value: pd.Timestamp) -> int:
    day = pd.Timestamp(value).date()
    calendar = exchange_trade_dates()
    if calendar:
        index = bisect_left(calendar, day)
        if index < len(calendar) and calendar[index] == day:
            return index
    return int(np.busday_count("1970-01-01", np.datetime64(day)))


def schedule_bucket(
    value: pd.Timestamp,
    schedule: Mapping[str, object],
    trading_dates: Sequence[pd.Timestamp],
) -> str:
    dates = _normalized_trading_dates(trading_dates)
    timestamp = pd.Timestamp(value).normalize()
    period = str(schedule["period"])
    interval = int(schedule["interval"])
    if period == "trading_day":
        if timestamp not in dates:
            raise ValueError(f"{timestamp.date()} is not a trading date")
        return f"trading_day:{_trading_day_ordinal(timestamp) // interval}"
    ordinal = _period_ordinal(timestamp, period)
    return f"{period}:{ordinal // interval}"


def is_schedule_boundary(
    value: pd.Timestamp,
    schedule: Mapping[str, object],
    trading_dates: Sequence[pd.Timestamp],
) -> bool:
    dates = _normalized_trading_dates(trading_dates)
    timestamp = pd.Timestamp(value).normalize()
    matches = np.flatnonzero(dates == timestamp)
    if not len(matches):
        return False
    index = int(matches[0])
    trigger = str(schedule["trigger"])
    if str(schedule["period"]) == "trading_day":
        ordinal = _trading_day_ordinal(timestamp)
        interval = int(schedule["interval"])
        remainder = ordinal % interval
        return remainder == (0 if trigger == "start" else interval - 1)
    period = str(schedule["period"])
    interval = int(schedule["interval"])
    bucket = _period_ordinal(timestamp, period) // interval
    if trigger == "start":
        return (
            index == 0
            or _period_ordinal(pd.Timestamp(dates[index - 1]), period) // interval
            != bucket
        )
    return (
        index < len(dates) - 1
        and _period_ordinal(pd.Timestamp(dates[index + 1]), period) // interval
        != bucket
    )


def schedule_boundaries(
    trading_dates: Sequence[pd.Timestamp],
    schedule: Mapping[str, object],
) -> list[pd.Timestamp]:
    dates = _normalized_trading_dates(trading_dates)
    if dates.empty:
        return []
    period = str(schedule["period"])
    interval = int(schedule["interval"])
    trigger = str(schedule["trigger"])
    if period == "trading_day":
        target = 0 if trigger == "start" else interval - 1
        return [
            pd.Timestamp(value)
            for value in dates
            if _trading_day_ordinal(pd.Timestamp(value)) % interval == target
        ]

    buckets = [
        _period_ordinal(pd.Timestamp(value), period) // interval
        for value in dates
    ]
    boundaries: list[pd.Timestamp] = []
    for index, value in enumerate(dates):
        timestamp = pd.Timestamp(value)
        if trigger == "start":
            boundary = index == 0 or buckets[index - 1] != buckets[index]
        else:
            boundary = (
                index < len(dates) - 1
                and buckets[index + 1] != buckets[index]
            )
        if boundary:
            boundaries.append(timestamp)
    return boundaries


def latest_schedule_boundary(
    value: pd.Timestamp,
    schedule: Mapping[str, object],
    trading_dates: Sequence[pd.Timestamp],
) -> pd.Timestamp:
    timestamp = pd.Timestamp(value).normalize()
    boundaries = [
        boundary
        for boundary in schedule_boundaries(trading_dates, schedule)
        if boundary <= timestamp
    ]
    if not boundaries:
        raise ValueError(
            f"no schedule boundary is available on or before {timestamp.date()}"
        )
    return max(boundaries)
