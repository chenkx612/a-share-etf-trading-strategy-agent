from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant_agent.strategy.selection import selected_to_weight_matrix


@dataclass(frozen=True)
class BacktestResult:
    daily_returns: pd.DataFrame
    equity_curve: pd.DataFrame
    positions: pd.DataFrame
    metrics: dict[str, float]


def run_backtest(
    daily: pd.DataFrame,
    selected: pd.DataFrame,
    fee_rate: float = 0.001,
) -> BacktestResult:
    prices = daily.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
    target_weights = selected_to_weight_matrix(selected)
    if prices.empty or target_weights.empty:
        empty = pd.DataFrame()
        return BacktestResult(empty, empty, empty, {})

    returns = prices.pct_change().fillna(0.0)
    target_weights = target_weights.reindex(prices.index).ffill().fillna(0.0)
    target_weights = target_weights.reindex(columns=prices.columns, fill_value=0.0)

    # Signals are generated after close and applied on the next trading day.
    trade_weights = target_weights.shift(1).fillna(0.0)
    turnover = trade_weights.diff().abs().sum(axis=1).fillna(trade_weights.abs().sum(axis=1))
    gross_returns = (trade_weights * returns).sum(axis=1)
    costs = turnover * fee_rate
    net_returns = gross_returns - costs
    equity = (1.0 + net_returns).cumprod()

    daily_returns = pd.DataFrame({
        "date": net_returns.index,
        "gross_return": gross_returns.values,
        "cost": costs.values,
        "net_return": net_returns.values,
        "turnover": turnover.values,
    })
    equity_curve = pd.DataFrame({"date": equity.index, "equity": equity.values})
    positions = trade_weights.reset_index().melt("date", var_name="symbol", value_name="weight")
    positions = positions[positions["weight"] != 0].reset_index(drop=True)
    metrics = compute_metrics(daily_returns)
    return BacktestResult(daily_returns, equity_curve, positions, metrics)


def compute_metrics(daily_returns: pd.DataFrame) -> dict[str, float]:
    if daily_returns.empty:
        return {}
    ret = daily_returns["net_return"].astype(float)
    equity = (1.0 + ret).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    annual_return = float(equity.iloc[-1] ** (252 / max(len(equity), 1)) - 1.0)
    annual_vol = float(ret.std(ddof=0) * math.sqrt(252))
    sharpe = float(annual_return / annual_vol) if annual_vol else 0.0
    drawdown = equity / equity.cummax() - 1.0
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "avg_turnover": float(daily_returns["turnover"].mean()),
    }


def factor_ic(factors: pd.DataFrame, score_col: str = "score") -> dict[str, float]:
    if factors.empty or score_col not in factors.columns:
        return {"ic": np.nan, "rank_ic": np.nan}
    df = factors.sort_values(["symbol", "date"]).copy()
    next_close = df.groupby("symbol")["close"].shift(-1)
    df["forward_return"] = next_close / df["close"] - 1.0
    pairs = df.dropna(subset=[score_col, "forward_return"])
    if pairs.empty:
        return {"ic": np.nan, "rank_ic": np.nan}

    def corr_or_nan(frame: pd.DataFrame, rank: bool = False) -> float:
        left = frame[score_col].rank() if rank else frame[score_col]
        right = frame["forward_return"].rank() if rank else frame["forward_return"]
        if left.nunique(dropna=True) < 2 or right.nunique(dropna=True) < 2:
            return np.nan
        return float(left.corr(right))

    ic_by_date = pairs.groupby("date").apply(lambda x: corr_or_nan(x), include_groups=False)
    rank_ic_by_date = pairs.groupby("date").apply(lambda x: corr_or_nan(x, rank=True), include_groups=False)
    return {
        "ic": float(ic_by_date.mean()),
        "rank_ic": float(rank_ic_by_date.mean()),
    }
