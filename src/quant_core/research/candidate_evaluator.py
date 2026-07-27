from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from quant_core.backtest.engine import BacktestResult, run_backtest
from quant_core.config import BacktestConfig


Selector = Callable[
    [pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp],
    pd.DataFrame,
]
REQUIRED_SELECTION_COLUMNS = {"date", "symbol", "target_weight"}


def evaluate_candidate(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    selector: Selector,
    *,
    backtest_config: BacktestConfig = BacktestConfig(),
) -> tuple[pd.DataFrame, BacktestResult]:
    history = daily.copy()
    history["date"] = pd.to_datetime(history["date"])
    history["symbol"] = history["symbol"].astype(str)
    symbols = set(universe["symbol"].astype(str))
    history = history[(history["symbol"].isin(symbols)) & (history["date"] <= end)]
    selected = selector(history.copy(), universe.copy(), start, end)
    selected = validate_selection(selected, history, symbols, start, end)
    backtest_daily = history[history["date"].between(start, end)].copy()
    if selected.empty:
        selected.attrs["signal_dates"] = sorted(backtest_daily["date"].unique())
    selected.attrs["universe_symbols"] = sorted(symbols)
    return selected, run_backtest(
        backtest_daily,
        selected,
        fee_rate=backtest_config.fee_rate,
        initial_capital=backtest_config.initial_capital,
        lot_size=backtest_config.lot_size,
    )


def validate_selection(
    selected: object,
    daily: pd.DataFrame,
    symbols: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    if not isinstance(selected, pd.DataFrame):
        raise ValueError("candidate select() must return a pandas DataFrame")
    missing = REQUIRED_SELECTION_COLUMNS - set(selected.columns)
    if missing:
        raise ValueError(
            f"candidate selection is missing columns: {', '.join(sorted(missing))}"
        )
    result = selected.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    result["symbol"] = result["symbol"].astype(str)
    result["target_weight"] = pd.to_numeric(
        result["target_weight"],
        errors="raise",
    )
    if result.duplicated(["date", "symbol"]).any():
        raise ValueError("candidate selection contains duplicate date/symbol rows")
    if not result["date"].between(start, end).all():
        raise ValueError(
            "candidate selection contains dates outside the evaluation period"
        )
    trading_dates = set(pd.to_datetime(daily["date"]))
    if not set(result["date"]).issubset(trading_dates):
        raise ValueError("candidate selection contains non-trading dates")
    if not set(result["symbol"]).issubset(symbols):
        raise ValueError("candidate selection contains symbols outside the universe")
    weights = result["target_weight"].astype(float)
    if not np.isfinite(weights).all() or (weights < 0.0).any():
        raise ValueError("candidate weights must be finite and non-negative")
    if (result.groupby("date")["target_weight"].sum() > 1.0 + 1e-9).any():
        raise ValueError(
            "candidate weights must sum to at most one on each date"
        )
    return result.sort_values(["date", "symbol"]).reset_index(drop=True)
