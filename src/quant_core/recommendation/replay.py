from __future__ import annotations

import math
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from quant_core import schedule as schedule_policy
from quant_core.backtest.engine import compute_metrics, run_backtest
from quant_core.data.market_data import (
    AkshareMarketDataClient,
    PartialMarketDataRefreshError,
    ProjectPaths,
    fetch_daily_if_stale,
    read_daily,
    refresh_window_start,
    replace_symbol_history,
    validate_daily,
    write_table,
)
from quant_core.recommendation.context import _normalize_symbol
from quant_core.recommendation.models import SHANGHAI, ProductionContext
from quant_core.recommendation.search import search_parameters
from quant_core.research.evaluator import validate_selection
from quant_core.schedule import is_schedule_boundary

def _validate_daily_requirements(
    daily: pd.DataFrame,
    context: ProductionContext,
    signal_date: pd.Timestamp,
) -> pd.DataFrame:
    missing = set(context.requirements.required_columns) - set(daily.columns)
    if missing:
        raise ValueError(f"market data is missing strategy-required columns: {sorted(missing)}")
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["symbol"] = frame["symbol"].map(_normalize_symbol)
    frame = frame[frame["date"] <= signal_date].sort_values(["date", "symbol"])
    universe_symbols = set(context.universe["symbol"])
    signal_rows = frame[
        (frame["date"] == signal_date) & frame["symbol"].isin(universe_symbols)
    ]
    valid_signal_rows = signal_rows
    for column in ("open", "close"):
        values = pd.to_numeric(valid_signal_rows[column], errors="coerce")
        valid_signal_rows = valid_signal_rows[np.isfinite(values) & (values > 0.0)]
    if valid_signal_rows.empty:
        raise RuntimeError(
            f"production universe has no valid open/close data on signal date "
            f"{signal_date.date()}"
        )
    counts = (
        frame[frame["symbol"].isin(universe_symbols)]
        .groupby("symbol")["date"]
        .nunique()
    )
    if counts.empty or int(counts.max()) < context.requirements.min_history:
        raise RuntimeError(
            "market data does not meet the strategy's declared minimum history requirement"
        )
    return frame


def _refresh_data(
    context: ProductionContext,
    requested: date,
    *,
    skip_refresh: bool,
) -> pd.DataFrame:
    paths = ProjectPaths(context.root)
    try:
        existing = read_daily(paths)
    except FileNotFoundError:
        existing = pd.DataFrame()
    if skip_refresh:
        if existing.empty:
            raise FileNotFoundError("offline recommendation requires local market data")
        return existing

    production = context.task.production
    assert production is not None
    benchmark = _normalize_symbol(production["benchmark"])
    refresh_universe = context.universe.copy()
    if benchmark not in set(refresh_universe["symbol"]):
        refresh_universe = pd.concat(
            [
                refresh_universe,
                pd.DataFrame([{"symbol": benchmark, "name": benchmark}]),
            ],
            ignore_index=True,
        )
    start = refresh_window_start(requested)
    client = AkshareMarketDataClient()
    refresh_error: PartialMarketDataRefreshError | None = None
    try:
        incoming, _ = fetch_daily_if_stale(
            refresh_universe,
            start,
            requested,
            existing=existing if not existing.empty else None,
            fetch_one=client.fetch_daily,
            log=print,
        )
    except PartialMarketDataRefreshError as error:
        incoming = error.incoming
        refresh_error = error
    if incoming.empty:
        if refresh_error is not None:
            raise refresh_error
        return existing
    _validate_refresh_preserves_available_history(existing, incoming, start)
    merged = replace_symbol_history(existing if not existing.empty else None, incoming)
    problems = validate_daily(merged)
    if problems:
        raise RuntimeError(f"refreshed market data is invalid: {problems}")
    write_table(merged, paths.data_daily)
    if refresh_error is not None:
        raise refresh_error
    return merged


def _validate_refresh_preserves_available_history(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    retention_start: date,
) -> None:
    """Reject a refresh that drops history already available in the five-year window."""
    if existing.empty or incoming.empty:
        return
    old = existing.copy()
    new = incoming.copy()
    old["date"] = pd.to_datetime(old["date"]).dt.normalize()
    new["date"] = pd.to_datetime(new["date"]).dt.normalize()
    old["symbol"] = old["symbol"].map(_normalize_symbol)
    new["symbol"] = new["symbol"].map(_normalize_symbol)
    window_start = pd.Timestamp(retention_start)

    for symbol in sorted(set(new["symbol"])):
        available = old[(old["symbol"] == symbol) & (old["date"] >= window_start)]
        if available.empty:
            continue
        refreshed = new[new["symbol"] == symbol]
        missing_dates = sorted(set(available["date"]) - set(refreshed["date"]))
        if missing_dates:
            preview = ", ".join(value.date().isoformat() for value in missing_dates[:3])
            raise RuntimeError(
                "refreshed market data would drop available history for "
                f"{symbol}: missing_dates={len(missing_dates)} ({preview})"
            )


def closed_market_data_end(
    requested: date,
    *,
    now: datetime | None = None,
) -> date:
    current = now or datetime.now(SHANGHAI)
    if requested >= current.date() and (current.hour, current.minute) < (15, 0):
        return current.date() - timedelta(days=1)
    return requested


def resolve_signal_date(
    daily: pd.DataFrame,
    requested: date,
    *,
    now: datetime | None = None,
) -> pd.Timestamp:
    dates = pd.DatetimeIndex(pd.to_datetime(daily["date"]).dt.normalize().unique()).sort_values()
    cutoff = pd.Timestamp(closed_market_data_end(requested, now=now))
    eligible = dates[dates <= cutoff]
    if eligible.empty:
        raise RuntimeError(f"no closed trading date is available on or before {requested}")
    return pd.Timestamp(eligible[-1])


def _next_trade_date(signal: pd.Timestamp, all_dates: Sequence[pd.Timestamp]) -> pd.Timestamp:
    dates = pd.DatetimeIndex(pd.to_datetime(all_dates)).sort_values().unique()
    later = dates[dates > signal]
    if len(later):
        return pd.Timestamp(later[0])
    try:
        import akshare as ak

        calendar = ak.tool_trade_date_hist_sina()
        column = "trade_date" if "trade_date" in calendar.columns else calendar.columns[0]
        official = pd.DatetimeIndex(pd.to_datetime(calendar[column])).sort_values()
        later = official[official > signal]
        if len(later):
            return pd.Timestamp(later[0])
    except Exception:
        pass
    candidate = signal + pd.Timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += pd.Timedelta(days=1)
    return candidate


def _target_holdings(
    context: ProductionContext,
    daily: pd.DataFrame,
    schedule_boundary: pd.Timestamp,
    signal_date: pd.Timestamp,
    trade_date: pd.Timestamp,
    parameters: Mapping[str, object],
) -> pd.DataFrame:
    raw = context.strategy.select_with_params(
        daily[daily["date"] <= signal_date].copy(),
        context.universe.copy(),
        schedule_boundary,
        signal_date,
        dict(parameters),
    )
    selected = validate_selection(
        raw,
        daily,
        set(context.universe["symbol"]),
        schedule_boundary,
        signal_date,
    )
    selected = selected[selected["date"] == signal_date].reset_index(drop=True)
    total = float(selected["target_weight"].sum()) if not selected.empty else 0.0
    if total > 1.0 + 1e-9:
        raise ValueError("target ETF weights exceed one")
    rows: list[dict[str, object]] = []
    for row in selected.itertuples(index=False):
        rows.append(
            {
                "record_type": "etf",
                "signal_date": signal_date.date().isoformat(),
                "trade_date": trade_date.date().isoformat(),
                "symbol": str(row.symbol),
                "name": str(getattr(row, "name", row.symbol)),
                "score": getattr(row, "score", None),
                "rank": getattr(row, "rank", None),
                "target_weight": float(row.target_weight),
            }
        )
    rows.append(
        {
            "record_type": "cash",
            "signal_date": signal_date.date().isoformat(),
            "trade_date": trade_date.date().isoformat(),
            "symbol": "CASH",
            "name": "现金",
            "score": None,
            "rank": None,
            "target_weight": max(0.0, 1.0 - total),
        }
    )
    output = pd.DataFrame(rows)
    if (
        not np.isfinite(output["target_weight"]).all()
        or (output["target_weight"] < 0).any()
        or not math.isclose(float(output["target_weight"].sum()), 1.0, abs_tol=1e-9)
    ):
        raise ValueError("ETF and cash target weights must be non-negative and sum to one")
    return output


def _historical_boundaries(
    dates: pd.DatetimeIndex,
    schedule: Mapping[str, object],
    curve_start: pd.Timestamp,
) -> list[pd.Timestamp]:
    if dates.empty:
        return []
    boundaries = [
        pd.Timestamp(value)
        for value in dates
        if is_schedule_boundary(pd.Timestamp(value), schedule, dates)
    ]
    anchors = [value for value in boundaries if value <= curve_start]
    if not anchors:
        raise RuntimeError(
            "insufficient market data to find the last parameter-search date "
            "before the causal curve window"
        )
    anchor = max(anchors)
    return [anchor, *(value for value in boundaries if value > curve_start)]


def next_schedule_boundary(
    signal_date: pd.Timestamp,
    schedule: Mapping[str, object],
    trading_dates: Sequence[pd.Timestamp],
) -> pd.Timestamp:
    signal = pd.Timestamp(signal_date).normalize()
    known = pd.DatetimeIndex(pd.to_datetime(trading_dates)).normalize()
    calendar = schedule_policy.exchange_trade_dates()
    if calendar:
        official = pd.DatetimeIndex(calendar)
        tail_start = max(signal, pd.Timestamp(official[-1])) + pd.Timedelta(days=1)
        future = official.union(pd.bdate_range(tail_start, periods=800))
    else:
        future = pd.bdate_range(signal + pd.Timedelta(days=1), periods=800)
    dates = known.union(future).sort_values().unique()
    for candidate in dates[dates > signal]:
        value = pd.Timestamp(candidate)
        if is_schedule_boundary(value, schedule, dates):
            return value
    raise RuntimeError("unable to resolve the next parameter-search date")


def causal_replay(
    context: ProductionContext,
    daily: pd.DataFrame,
    signal_date: pd.Timestamp,
) -> tuple[pd.DataFrame, Mapping[str, float], list[Mapping[str, object]]]:
    production = context.task.production
    assert production is not None
    parameter_selection = context.task.parameter_selection
    assert parameter_selection is not None
    curve_start = signal_date - pd.DateOffset(months=int(production["curve_months"]))
    symbols = set(context.universe["symbol"])
    strategy_daily = daily[daily["symbol"].isin(symbols)].copy()
    curve_dates = pd.DatetimeIndex(
        strategy_daily.loc[
            strategy_daily["date"].between(curve_start, signal_date), "date"
        ].unique()
    ).sort_values()
    if len(curve_dates) < 2:
        raise RuntimeError("insufficient market data for the configured causal curve window")
    all_strategy_dates = pd.DatetimeIndex(strategy_daily["date"].unique()).sort_values()
    boundaries = _historical_boundaries(
        all_strategy_dates[all_strategy_dates <= signal_date],
        parameter_selection["schedule"],
        pd.Timestamp(curve_dates[0]),
    )
    selections: list[pd.DataFrame] = []
    audits: list[Mapping[str, object]] = []
    for index, boundary in enumerate(boundaries):
        result = search_parameters(context, daily[daily["date"] <= boundary], boundary)
        segment_start = boundary
        segment_end = (
            boundaries[index + 1] - pd.Timedelta(days=1)
            if index + 1 < len(boundaries)
            else signal_date
        )
        selected = context.strategy.select_with_params(
            daily[daily["date"] <= segment_end].copy(),
            context.universe.copy(),
            segment_start,
            segment_end,
            dict(result.parameters),
        )
        selected = validate_selection(
            selected,
            daily,
            symbols,
            segment_start,
            segment_end,
        )
        selections.append(selected)
        audits.append(
            {
                "boundary": boundary.date().isoformat(),
                "train_start": result.train_start.date().isoformat(),
                "train_end": result.train_end.date().isoformat(),
                "parameters": result.parameters,
                "metrics": result.metrics,
            }
        )
    combined = pd.concat(selections, ignore_index=True) if selections else pd.DataFrame()
    replay_start = boundaries[0]
    replay_dates = pd.DatetimeIndex(
        strategy_daily.loc[
            strategy_daily["date"].between(replay_start, signal_date), "date"
        ].unique()
    ).sort_values()
    combined.attrs["signal_dates"] = list(replay_dates)
    combined.attrs["universe_symbols"] = sorted(symbols)
    backtest_daily = strategy_daily[
        strategy_daily["date"].between(replay_start, signal_date)
    ].copy()
    result = run_backtest(backtest_daily, combined)
    if result.daily_returns.empty:
        raise RuntimeError("causal strategy replay produced no daily returns")
    strategy_returns = (
        result.daily_returns[
            result.daily_returns["date"].between(curve_dates[0], signal_date)
        ]
        .set_index("date")["net_return"]
        .astype(float)
    )

    benchmark = _normalize_symbol(production["benchmark"])
    benchmark_frame = (
        daily[
            (daily["symbol"] == benchmark)
            & daily["date"].between(curve_dates[0], signal_date)
        ][["date", "open"]]
        .drop_duplicates("date", keep="last")
        .sort_values("date")
        .set_index("date")
    )
    benchmark_open = pd.to_numeric(benchmark_frame["open"], errors="coerce")
    benchmark_returns = benchmark_open.shift(-1) / benchmark_open - 1.0
    aligned = pd.DataFrame(
        {
            "strategy_return": strategy_returns,
            "benchmark_return": benchmark_returns,
        }
    ).dropna()
    if aligned.empty or not aligned.index.equals(strategy_returns.index[:-1]):
        raise RuntimeError("benchmark history is incomplete or not aligned with the strategy curve")
    aligned["strategy_equity"] = (1.0 + aligned["strategy_return"]).cumprod()
    aligned["benchmark_equity"] = (1.0 + aligned["benchmark_return"]).cumprod()
    aligned = aligned.reset_index()
    strategy_metrics = compute_metrics(
        pd.DataFrame(
            {
                "net_return": aligned["strategy_return"],
                "turnover": result.daily_returns.set_index("date")
                .reindex(pd.DatetimeIndex(aligned["date"]))["turnover"]
                .to_numpy(),
            }
        )
    )
    benchmark_metrics = compute_metrics(
        pd.DataFrame(
            {
                "net_return": aligned["benchmark_return"],
                "turnover": 0.0,
            }
        )
    )
    metrics = {
        "strategy_annual_return": strategy_metrics["annual_return"],
        "strategy_max_drawdown": strategy_metrics["max_drawdown"],
        "benchmark_annual_return": benchmark_metrics["annual_return"],
        "benchmark_max_drawdown": benchmark_metrics["max_drawdown"],
    }
    return aligned, metrics, audits


def _write_curve_png(
    curve: pd.DataFrame, metrics: Mapping[str, float], path: Path, benchmark: str
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(
        curve["date"],
        curve["strategy_equity"],
        label=(
            f"Strategy ann. {metrics['strategy_annual_return']:.1%}, "
            f"MDD {metrics['strategy_max_drawdown']:.1%}"
        ),
    )
    axis.plot(
        curve["date"],
        curve["benchmark_equity"],
        label=(
            f"{benchmark} ann. {metrics['benchmark_annual_return']:.1%}, "
            f"MDD {metrics['benchmark_max_drawdown']:.1%}"
        ),
    )
    axis.set_title("Strictly causal production replay")
    axis.set_ylabel("Cumulative net value")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        figure.savefig(temporary_path, format="png", dpi=150)
        temporary_path.replace(path)
    finally:
        plt.close(figure)
        temporary_path.unlink(missing_ok=True)
