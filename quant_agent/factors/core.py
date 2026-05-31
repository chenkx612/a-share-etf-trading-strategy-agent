from __future__ import annotations

import numpy as np
import pandas as pd


FACTOR_COLUMNS = [
    "momentum_20",
    "sharpe_20",
    "reversal_5",
    "volatility_20",
    "amount_mean_20",
    "turnover_mean_20",
]


def compute_factors(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return daily.copy()
    df = daily.sort_values(["symbol", "date"]).copy()
    grouped = df.groupby("symbol", group_keys=False)
    df["ret_1"] = grouped["close"].pct_change()
    df["momentum_20"] = grouped["close"].pct_change(20)
    rolling_return_20 = grouped["close"].pct_change(20)
    rolling_vol_20 = grouped["ret_1"].transform(lambda x: x.rolling(20, min_periods=20).std())
    df["sharpe_20"] = rolling_return_20 / rolling_vol_20.replace(0, np.nan)
    df["reversal_5"] = -grouped["close"].pct_change(5)
    df["volatility_20"] = grouped["ret_1"].transform(lambda x: x.rolling(20, min_periods=10).std())
    df["amount_mean_20"] = grouped["amount"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["turnover_mean_20"] = grouped["turnover"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df[FACTOR_COLUMNS] = df[FACTOR_COLUMNS].replace([np.inf, -np.inf], np.nan)
    return df


def cross_sectional_zscore(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for col in columns:
        mean = out.groupby("date")[col].transform("mean")
        std = out.groupby("date")[col].transform("std").replace(0, np.nan)
        out[f"{col}_z"] = (out[col] - mean) / std
    return out
