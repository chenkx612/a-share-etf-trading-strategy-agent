from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from quant_core.factors import compute_factors


STRATEGY_NAME = "sharpe-corr-threshold"


@dataclass(frozen=True)
class SharpeCorrThresholdParams:
    top_n: int = 5
    sharpe_window: int = 25
    factor_lower_bound: float = 0.0
    corr_window: int = 100
    corr_threshold: float = 0.9
    stop_loss_pct: float = 0.1

    def __post_init__(self) -> None:
        if isinstance(self.top_n, bool) or not isinstance(self.top_n, int) or self.top_n <= 0:
            raise ValueError("top_n must be a positive integer")
        if (
            isinstance(self.sharpe_window, bool)
            or not isinstance(self.sharpe_window, int)
            or self.sharpe_window <= 0
        ):
            raise ValueError("sharpe_window must be a positive integer")
        if isinstance(self.corr_window, bool) or not isinstance(self.corr_window, int) or self.corr_window <= 0:
            raise ValueError("corr_window must be a positive integer")
        if not math.isfinite(self.factor_lower_bound):
            raise ValueError("factor_lower_bound must be finite")
        if not math.isfinite(self.corr_threshold) or not -1.0 <= self.corr_threshold <= 1.0:
            raise ValueError("corr_threshold must be between -1 and 1")
        if not math.isfinite(self.stop_loss_pct) or not 0.0 <= self.stop_loss_pct <= 1.0:
            raise ValueError("stop_loss_pct must be between 0 and 1")

    @property
    def factor_name(self) -> str:
        return f"sharpe_{self.sharpe_window}"


def select(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Candidate-compatible entry point for the research evaluator."""
    params = SharpeCorrThresholdParams()
    factors = compute_factors(daily, sharpe_windows=[params.sharpe_window])
    return select_sharpe_corr_threshold(
        factors,
        params,
        start=start,
        end=end,
        universe_symbols=set(universe["symbol"].astype(str)),
    )


def parameter_grid() -> list[dict[str, object]]:
    """Small, deterministic grid used by the research walk-forward harness."""
    return [
        {"top_n": top_n, "sharpe_window": window, "factor_lower_bound": bound,
         "corr_window": 100, "corr_threshold": threshold, "stop_loss_pct": stop}
        for top_n in (3, 5)
        for window in (20, 25, 60)
        for bound in (0.0,)
        for threshold in (0.8, 0.9)
        for stop in (0.08, 0.10)
    ]


def select_with_params(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    params: dict[str, object],
) -> pd.DataFrame:
    strategy_params = SharpeCorrThresholdParams(**params)
    factors = compute_factors(daily, sharpe_windows=[strategy_params.sharpe_window])
    return select_sharpe_corr_threshold(
        factors, strategy_params, start=start, end=end,
        universe_symbols=set(universe["symbol"].astype(str)),
    )


def select_sharpe_corr_threshold(
    factors: pd.DataFrame,
    params: SharpeCorrThresholdParams,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    universe_symbols: set[str] | None = None,
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

    score_col = params.factor_name
    prices = df.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
    daily_rets = prices.pct_change().fillna(0.0)
    rolling_corr = daily_rets.rolling(params.corr_window).corr()

    rows: list[dict[str, object]] = []
    filter_events: list[dict[str, object]] = []
    signal_dates: list[pd.Timestamp] = []
    names = df.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()
    available_symbols = sorted(df["symbol"].unique().tolist())

    score_frame = df.pivot(index="date", columns="symbol", values=score_col).sort_index()
    for date in score_frame.index:
        if start is not None and date < start:
            continue
        if end is not None and date > end:
            continue
        signal_dates.append(date)

        day_scores = score_frame.loc[date].dropna()
        stopped_assets = {
            asset
            for asset in day_scores.index
            if asset in daily_rets.columns and daily_rets.loc[date, asset] < -params.stop_loss_pct
        }
        for asset in sorted(stopped_assets):
            filter_events.append({
                "date": date.date().isoformat(),
                "symbol": str(asset),
                "name": names.get(str(asset), str(asset)),
                "filter": "stop_loss",
                "condition": "daily_return < -stop_loss_pct",
                "daily_return": float(daily_rets.loc[date, asset]),
                "stop_loss_pct": float(params.stop_loss_pct),
                "score": float(day_scores.loc[asset]),
            })
        day_scores = day_scores.drop(stopped_assets, errors="ignore")
        day_scores = day_scores[day_scores > params.factor_lower_bound]
        if day_scores.empty:
            continue

        try:
            curr_corr = rolling_corr.loc[date]
        except KeyError:
            curr_corr = pd.DataFrame()

        selected: list[str] = []
        for asset in day_scores.sort_values(ascending=False).index.tolist():
            if len(selected) >= params.top_n:
                break
            corr_block = _correlation_block(asset, selected, curr_corr, params.corr_threshold)
            if corr_block is not None:
                selected_asset, corr_value = corr_block
                filter_events.append({
                    "date": date.date().isoformat(),
                    "symbol": str(asset),
                    "name": names.get(str(asset), str(asset)),
                    "filter": "correlation",
                    "condition": "correlation > corr_threshold",
                    "correlation": float(corr_value),
                    "corr_threshold": float(params.corr_threshold),
                    "selected_symbol": str(selected_asset),
                    "selected_name": names.get(str(selected_asset), str(selected_asset)),
                    "score": float(day_scores.loc[asset]),
                })
                continue
            selected.append(str(asset))

        if selected:
            weight = 1.0 / params.top_n
            for asset in selected:
                rows.append({
                    "date": date,
                    "symbol": asset,
                    "name": names.get(asset, asset),
                    "score": float(day_scores.loc[asset]),
                    "target_weight": weight,
                })
    selected = pd.DataFrame(rows, columns=["date", "symbol", "name", "score", "target_weight"])
    selected.attrs["signal_dates"] = signal_dates
    selected.attrs["universe_symbols"] = available_symbols
    selected.attrs["filter_events"] = filter_events
    return selected


def _correlation_block(
    asset: str,
    selected: list[str],
    corr: pd.DataFrame,
    threshold: float,
) -> tuple[str, float] | None:
    if corr.empty:
        return None
    for selected_asset in selected:
        if asset in corr.index and selected_asset in corr.columns:
            value = corr.loc[asset, selected_asset]
            if value > threshold:
                return selected_asset, float(value)
    return None
