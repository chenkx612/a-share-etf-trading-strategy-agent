from __future__ import annotations

import numpy as np
import pandas as pd


DEFAULT_SHARPE_WINDOWS = (20, 25)

BASE_FACTOR_COLUMNS = [
    "momentum_20",
    "reversal_5",
    "volatility_20",
    "amount_mean_20",
    "turnover_mean_20",
]

FACTOR_COLUMNS = [
    "momentum_20",
    *(f"sharpe_{window}" for window in DEFAULT_SHARPE_WINDOWS),
    "reversal_5",
    "volatility_20",
    "amount_mean_20",
    "turnover_mean_20",
]


def normalize_sharpe_windows(sharpe_windows: list[int] | tuple[int, ...] | None = None) -> tuple[int, ...]:
    windows = DEFAULT_SHARPE_WINDOWS if sharpe_windows is None else tuple(sharpe_windows)
    normalized = tuple(sorted(set(int(window) for window in windows)))
    if not normalized:
        raise ValueError("At least one Sharpe window is required")
    invalid = [window for window in normalized if window <= 0]
    if invalid:
        raise ValueError(f"Sharpe windows must be positive integers: {invalid}")
    return normalized


def compute_factors(
    daily: pd.DataFrame,
    sharpe_windows: list[int] | tuple[int, ...] | None = None,
) -> pd.DataFrame:
    if daily.empty:
        return daily.copy()
    sharpe_windows = normalize_sharpe_windows(sharpe_windows)
    df = daily.sort_values(["symbol", "date"]).copy()
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    grouped = df.groupby("symbol", group_keys=False)
    df["ret_1"] = grouped["close"].pct_change()
    df["momentum_20"] = grouped["close"].pct_change(20)
    sharpe_columns = []
    for window in sharpe_windows:
        rolling_return = grouped["close"].pct_change(window)
        rolling_vol = grouped["ret_1"].transform(lambda x, window=window: x.rolling(window, min_periods=window).std())
        col = f"sharpe_{window}"
        df[col] = rolling_return / rolling_vol.replace(0, np.nan)
        sharpe_columns.append(col)
    df["reversal_5"] = -grouped["close"].pct_change(5)
    df["volatility_20"] = grouped["ret_1"].transform(lambda x: x.rolling(20, min_periods=10).std())
    df["amount_mean_20"] = grouped["amount"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["turnover_mean_20"] = grouped["turnover"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    factor_columns = [*BASE_FACTOR_COLUMNS, *sharpe_columns]
    df[factor_columns] = df[factor_columns].replace([np.inf, -np.inf], np.nan)
    return df


def cross_sectional_zscore(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for col in columns:
        mean = out.groupby("date")[col].transform("mean")
        std = out.groupby("date")[col].transform("std").replace(0, np.nan)
        out[f"{col}_z"] = (out[col] - mean) / std
    return out
