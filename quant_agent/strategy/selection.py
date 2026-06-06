from __future__ import annotations

import pandas as pd

from quant_agent.config import StrategyConfig
from quant_agent.factors.core import cross_sectional_zscore


def score_factors(factors: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    columns = list(config.factor_weights)
    scored = cross_sectional_zscore(factors, columns)
    score = pd.Series(0.0, index=scored.index)
    for factor, weight in config.factor_weights.items():
        score = score.add(scored[f"{factor}_z"].fillna(0.0) * weight, fill_value=0.0)
    scored["score"] = score
    return scored


def score_and_select(
    factors: pd.DataFrame,
    config: StrategyConfig,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    universe_symbols: set[str] | None = None,
) -> pd.DataFrame:
    if factors.empty:
        return pd.DataFrame()
    df = factors.copy()
    df["symbol"] = df["symbol"].astype(str)
    if universe_symbols is not None:
        df = df[df["symbol"].isin({str(symbol) for symbol in universe_symbols})]
    df["date"] = pd.to_datetime(df["date"])
    if start is not None:
        df = df[df["date"] >= start]
    if end is not None:
        df = df[df["date"] <= end]
    scored = score_factors(df, config)
    selected = (
        scored.dropna(subset=["score"])
        .sort_values(["date", "score"], ascending=[True, False])
        .groupby("date", group_keys=False)
        .head(config.top_n)
        .copy()
    )
    selected["target_weight"] = selected.groupby("date")["symbol"].transform(lambda x: 1.0 / len(x))
    return selected.sort_values(["date", "score"], ascending=[True, False]).reset_index(drop=True)


def selected_to_weight_matrix(selected: pd.DataFrame) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    return selected.pivot(index="date", columns="symbol", values="target_weight").fillna(0.0).sort_index()
