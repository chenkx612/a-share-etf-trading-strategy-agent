from __future__ import annotations

import pandas as pd

from quant_core.config import (
    SHARPE_CORR_THRESHOLD_CORR_THRESHOLD,
    SHARPE_CORR_THRESHOLD_CORR_WINDOW,
    SHARPE_CORR_THRESHOLD_LOWER_BOUND,
    SHARPE_CORR_THRESHOLD_STOP_LOSS_PCT,
    StrategyConfig,
)


def select_sharpe_corr_threshold(
    factors: pd.DataFrame,
    config: StrategyConfig,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    universe_symbols: set[str] | None = None,
    corr_window: int | None = None,
    corr_threshold: float | None = None,
    stop_loss_pct: float | None = None,
    factor_lower_bound: float | None = None,
) -> pd.DataFrame:
    if factors.empty:
        return pd.DataFrame(columns=["date", "symbol", "name", "score", "target_weight"])

    df = factors.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype(str)
    if universe_symbols is not None:
        df = df[df["symbol"].isin({str(symbol) for symbol in universe_symbols})]
    if df.empty:
        return pd.DataFrame(columns=["date", "symbol", "name", "score", "target_weight"])

    corr_window = SHARPE_CORR_THRESHOLD_CORR_WINDOW if corr_window is None else corr_window
    corr_threshold = SHARPE_CORR_THRESHOLD_CORR_THRESHOLD if corr_threshold is None else corr_threshold
    stop_loss_pct = SHARPE_CORR_THRESHOLD_STOP_LOSS_PCT if stop_loss_pct is None else stop_loss_pct
    factor_lower_bound = (
        SHARPE_CORR_THRESHOLD_LOWER_BOUND if factor_lower_bound is None else factor_lower_bound
    )

    score_col = config.factor_name
    prices = df.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
    daily_rets = prices.pct_change().fillna(0.0)
    rolling_corr = daily_rets.rolling(corr_window).corr()

    rows: list[dict[str, object]] = []
    signal_dates: list[pd.Timestamp] = []
    prev_selected: list[str] = []
    names = df.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()
    available_symbols = sorted(df["symbol"].unique().tolist())

    score_frame = df.pivot(index="date", columns="symbol", values=score_col).sort_index()
    for date in score_frame.index:
        if start is not None and date < start:
            continue
        if end is not None and date > end:
            continue
        signal_dates.append(date)

        stopped_assets = {
            asset
            for asset in prev_selected
            if asset in daily_rets.columns and daily_rets.loc[date, asset] < -stop_loss_pct
        }
        day_scores = score_frame.loc[date].dropna().drop(stopped_assets, errors="ignore")
        day_scores = day_scores[day_scores > factor_lower_bound]
        if day_scores.empty:
            prev_selected = []
            continue

        try:
            curr_corr = rolling_corr.loc[date]
        except KeyError:
            curr_corr = pd.DataFrame()

        selected: list[str] = []
        for asset in day_scores.sort_values(ascending=False).index.tolist():
            if len(selected) >= config.top_n:
                break
            if _too_correlated(asset, selected, curr_corr, corr_threshold):
                continue
            selected.append(str(asset))

        if selected:
            weight = 1.0 / config.top_n
            for asset in selected:
                rows.append({
                    "date": date,
                    "symbol": asset,
                    "name": names.get(asset, asset),
                    "score": float(day_scores.loc[asset]),
                    "target_weight": weight,
                })
        prev_selected = selected

    selected = pd.DataFrame(rows, columns=["date", "symbol", "name", "score", "target_weight"])
    selected.attrs["signal_dates"] = signal_dates
    selected.attrs["universe_symbols"] = available_symbols
    return selected


def _too_correlated(
    asset: str,
    selected: list[str],
    corr: pd.DataFrame,
    threshold: float,
) -> bool:
    if corr.empty:
        return False
    for selected_asset in selected:
        if asset in corr.index and selected_asset in corr.columns:
            value = corr.loc[asset, selected_asset]
            if value > threshold:
                return True
    return False
