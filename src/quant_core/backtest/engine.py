from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BacktestResult:
    daily_returns: pd.DataFrame
    equity_curve: pd.DataFrame
    positions: pd.DataFrame
    metrics: dict[str, float]


def selected_to_weight_matrix(selected: pd.DataFrame) -> pd.DataFrame:
    signal_dates = selected.attrs.get("signal_dates")
    universe_symbols = selected.attrs.get("universe_symbols")
    if selected.empty:
        if signal_dates is not None and universe_symbols is not None:
            return pd.DataFrame(
                0.0,
                index=pd.DatetimeIndex(signal_dates),
                columns=[str(symbol) for symbol in universe_symbols],
            ).sort_index()
        return pd.DataFrame()
    weights = selected.pivot(index="date", columns="symbol", values="target_weight").fillna(0.0).sort_index()
    if signal_dates is not None:
        weights = weights.reindex(pd.DatetimeIndex(signal_dates), fill_value=0.0)
    if universe_symbols is not None:
        weights = weights.reindex(columns=[str(symbol) for symbol in universe_symbols], fill_value=0.0)
    return weights.sort_index()


def run_backtest(
    daily: pd.DataFrame,
    selected: pd.DataFrame,
    fee_rate: float = 0.001,
    initial_capital: float = 1_000_000.0,
    lot_size: int = 100,
) -> BacktestResult:
    prices = daily.pivot(index="date", columns="symbol", values="open").sort_index().ffill()
    target_weights = selected_to_weight_matrix(selected)
    if prices.empty or target_weights.empty:
        empty = pd.DataFrame()
        return BacktestResult(empty, empty, empty, {})
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")

    target_weights = target_weights.reindex(prices.index).ffill().fillna(0.0)
    target_weights = target_weights.reindex(columns=prices.columns, fill_value=0.0)

    # Signals are generated after close, traded at the next open, then held to the following open.
    trade_targets = target_weights.shift(1).fillna(0.0)
    next_prices = prices.shift(-1).fillna(prices)

    cash = float(initial_capital)
    shares = pd.Series(0, index=prices.columns, dtype="int64")
    daily_rows = []
    position_rows = []
    equity_rows = []

    for date in prices.index:
        current_prices = prices.loc[date].astype(float)
        next_open_prices = next_prices.loc[date].astype(float)
        start_value = cash + float((shares * current_prices).sum())
        if start_value <= 0:
            raise ValueError("portfolio value must stay positive during backtest")

        target = trade_targets.loc[date].astype(float).clip(lower=0.0)
        target_sum = float(target.sum())
        if target_sum > 1.0:
            target = target / target_sum

        target_shares = _target_lot_shares(target, current_prices, start_value, fee_rate, lot_size)
        target_shares = _fit_lot_shares_to_cash(target_shares, shares, current_prices, cash, fee_rate, lot_size)
        delta_shares = target_shares - shares
        trade_values = (delta_shares.abs() * current_prices).fillna(0.0)
        traded_value = float(trade_values.sum())
        cost_value = traded_value * fee_rate

        cash -= float((delta_shares * current_prices).sum()) + cost_value
        shares = target_shares

        gross_end_value = cash + cost_value + float((shares * next_open_prices).sum())
        end_value = cash + float((shares * next_open_prices).sum())
        gross_return = gross_end_value / start_value - 1.0
        cost = cost_value / start_value
        net_return = end_value / start_value - 1.0
        turnover = traded_value / start_value

        daily_rows.append({
            "date": date,
            "gross_return": gross_return,
            "cost": cost,
            "net_return": net_return,
            "turnover": turnover,
        })
        equity_rows.append({"date": date, "equity": end_value / initial_capital})

        position_values = (shares * current_prices).fillna(0.0)
        for symbol, share_count in shares[shares != 0].items():
            position_rows.append({
                "date": date,
                "symbol": symbol,
                "shares": int(share_count),
                "weight": float(position_values.loc[symbol] / start_value),
            })

    daily_returns = pd.DataFrame(daily_rows)
    equity_curve = pd.DataFrame(equity_rows)
    positions = pd.DataFrame(position_rows, columns=["date", "symbol", "shares", "weight"])
    metrics = compute_metrics(daily_returns)
    return BacktestResult(daily_returns, equity_curve, positions, metrics)


def _target_lot_shares(
    target_weights: pd.Series,
    prices: pd.Series,
    portfolio_value: float,
    fee_rate: float,
    lot_size: int,
) -> pd.Series:
    valid_prices = prices.where(prices > 0)
    target_values = portfolio_value * target_weights
    raw_lots = target_values / (valid_prices * lot_size * (1.0 + fee_rate))
    lots = np.floor(raw_lots.replace([np.inf, -np.inf], np.nan).fillna(0.0))
    return (lots.astype("int64") * lot_size).reindex(prices.index, fill_value=0)


def _fit_lot_shares_to_cash(
    target_shares: pd.Series,
    current_shares: pd.Series,
    prices: pd.Series,
    cash: float,
    fee_rate: float,
    lot_size: int,
) -> pd.Series:
    adjusted = target_shares.copy()
    cash_after = _cash_after_trade(adjusted, current_shares, prices, cash, fee_rate)
    while cash_after < -1e-9:
        buy_deltas = adjusted - current_shares
        buy_deltas = buy_deltas[buy_deltas > 0]
        if buy_deltas.empty:
            break
        symbol = (buy_deltas * prices.loc[buy_deltas.index]).idxmax()
        adjusted.loc[symbol] = max(0, int(adjusted.loc[symbol]) - lot_size)
        cash_after = _cash_after_trade(adjusted, current_shares, prices, cash, fee_rate)
    return adjusted


def _cash_after_trade(
    target_shares: pd.Series,
    current_shares: pd.Series,
    prices: pd.Series,
    cash: float,
    fee_rate: float,
) -> float:
    delta_shares = target_shares - current_shares
    trade_value = float((delta_shares.abs() * prices).fillna(0.0).sum())
    signed_value = float((delta_shares * prices).fillna(0.0).sum())
    return cash - signed_value - trade_value * fee_rate


def compute_metrics(daily_returns: pd.DataFrame) -> dict[str, float]:
    if daily_returns.empty:
        return {}
    ret = daily_returns["net_return"].astype(float)
    equity = (1.0 + ret).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    annual_return = float(equity.iloc[-1] ** (252 / max(len(equity), 1)) - 1.0)
    annual_vol = float(ret.std(ddof=0) * math.sqrt(252))
    sharpe = float(annual_return / annual_vol) if annual_vol else 0.0
    downside = ret[ret < 0.0]
    downside_vol = float(downside.std(ddof=0) * math.sqrt(252)) if not downside.empty else 0.0
    sortino = float(annual_return / downside_vol) if downside_vol else 0.0
    drawdown = equity / equity.cummax() - 1.0
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": float(drawdown.min()),
        "avg_turnover": float(daily_returns["turnover"].mean()),
    }


def factor_ic(factors: pd.DataFrame, score_col: str = "score") -> dict[str, float]:
    if factors.empty or score_col not in factors.columns or "open" not in factors.columns:
        return {"ic": np.nan, "rank_ic": np.nan}
    df = factors.sort_values(["symbol", "date"]).copy()
    next_open = df.groupby("symbol")["open"].shift(-1)
    exit_open = df.groupby("symbol")["open"].shift(-2)
    df["forward_return"] = exit_open / next_open - 1.0
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
