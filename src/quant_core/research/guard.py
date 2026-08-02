from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant_core.data.market_data import ProjectPaths, load_universe, read_daily


GUARD_REJECTION_REASON = "guard degradation exceeded"


def metric_annual_return(metrics: Mapping[str, Any]) -> float:
    """Read the annual return from either fixed or walk-forward metrics."""
    aggregate = metrics.get("aggregate")
    source = aggregate if isinstance(aggregate, Mapping) else metrics
    value = source.get("annual_return")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError("Guard strategy annual_return must be finite")
    return float(value)


def universe_equal_weight_annual_return(
    runtime: Path,
    universe_path: Path,
    period: Mapping[str, Any],
) -> float:
    """Compute no-fee, daily-rebalanced equal-weight open-to-open return.

    A symbol participates on a date only when it has finite positive opens on
    that date and the immediately following trading date. Missing prices are
    never filled. The final trading date contributes the same zero forward
    return used by the no-fee strategy backtest horizon.
    """
    universe = load_universe(universe_path)
    symbols = set(universe["symbol"].astype(str))
    daily = read_daily(ProjectPaths(runtime)).copy()
    required = {"date", "symbol", "open"}
    missing = required - set(daily.columns)
    if missing:
        raise ValueError(f"Guard benchmark market data is missing columns: {sorted(missing)}")
    daily["date"] = pd.to_datetime(daily["date"], errors="raise")
    daily["symbol"] = daily["symbol"].astype(str)
    daily["open"] = pd.to_numeric(daily["open"], errors="coerce")
    start = pd.Timestamp(str(period["start"]))
    end = pd.Timestamp(str(period["end"]))
    scoped = daily[
        daily["symbol"].isin(symbols) & daily["date"].between(start, end)
    ][["date", "symbol", "open"]]
    if scoped.duplicated(["date", "symbol"]).any():
        raise ValueError("Guard benchmark market data contains duplicate date/symbol rows")
    dates = pd.DatetimeIndex(scoped["date"].drop_duplicates()).sort_values()
    if len(dates) < 2:
        raise ValueError("Guard benchmark has no adjacent trading dates")
    prices = scoped.pivot(index="date", columns="symbol", values="open").reindex(dates)
    prices = prices.where(np.isfinite(prices) & prices.gt(0.0))
    forward = prices.shift(-1) / prices - 1.0
    daily_returns: list[float] = []
    for day in dates[:-1]:
        cross_section = forward.loc[day].dropna()
        cross_section = cross_section[np.isfinite(cross_section)]
        if cross_section.empty:
            raise ValueError(
                "Guard benchmark has an empty valid cross-section on "
                f"{day.date().isoformat()}"
            )
        daily_returns.append(float(cross_section.mean()))
    daily_returns.append(0.0)
    equity = float(np.prod(1.0 + np.asarray(daily_returns, dtype=float)))
    annual_return = equity ** (252.0 / len(daily_returns)) - 1.0
    if not math.isfinite(annual_return):
        raise ValueError("Guard benchmark annual_return is not finite")
    return float(annual_return)
