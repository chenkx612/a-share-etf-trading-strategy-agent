from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant_core.factors import compute_factors


STRATEGY_NAME = "vol-adaptive-residual-sharpe"


@dataclass(frozen=True)
class VolAdaptiveResidualSharpeParams:
    top_n: int = 5
    sharpe_window: int = 25
    factor_lower_bound: float = 0.0
    corr_window: int = 100
    corr_threshold: float = 0.9
    stop_loss_pct: float = 0.1
    # Vol-expansion risk-off: short/long EW realized-vol ratio
    vol_short_window: int = 10
    vol_long_window: int = 60
    vol_ratio_threshold: float = 1.3
    risk_off_top_n: int = 3
    risk_off_gross: float = 0.6
    # Name-level residual (idiosyncratic) Sharpe blend into cross-sectional rank
    residual_sharpe_window: int = 25
    residual_blend_alpha: float = 0.27

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
        if (
            isinstance(self.vol_short_window, bool)
            or not isinstance(self.vol_short_window, int)
            or self.vol_short_window <= 1
        ):
            raise ValueError("vol_short_window must be an integer > 1")
        if (
            isinstance(self.vol_long_window, bool)
            or not isinstance(self.vol_long_window, int)
            or self.vol_long_window <= self.vol_short_window
        ):
            raise ValueError("vol_long_window must be an integer greater than vol_short_window")
        if not math.isfinite(self.vol_ratio_threshold) or self.vol_ratio_threshold <= 0.0:
            raise ValueError("vol_ratio_threshold must be positive and finite")
        if (
            isinstance(self.risk_off_top_n, bool)
            or not isinstance(self.risk_off_top_n, int)
            or self.risk_off_top_n <= 0
        ):
            raise ValueError("risk_off_top_n must be a positive integer")
        if self.risk_off_top_n > self.top_n:
            raise ValueError("risk_off_top_n must not exceed top_n")
        if not math.isfinite(self.risk_off_gross) or not 0.0 < self.risk_off_gross <= 1.0:
            raise ValueError("risk_off_gross must be in (0, 1]")
        if (
            isinstance(self.residual_sharpe_window, bool)
            or not isinstance(self.residual_sharpe_window, int)
            or self.residual_sharpe_window <= 1
        ):
            raise ValueError("residual_sharpe_window must be an integer > 1")
        if not math.isfinite(self.residual_blend_alpha) or not 0.0 <= self.residual_blend_alpha <= 1.0:
            raise ValueError("residual_blend_alpha must be in [0, 1]")

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
    params = VolAdaptiveResidualSharpeParams()
    factors = compute_factors(daily, sharpe_windows=[params.sharpe_window])
    return select_vol_adaptive_residual_sharpe(
        factors,
        params,
        start=start,
        end=end,
        universe_symbols=set(universe["symbol"].astype(str)),
    )


def select_vol_adaptive_residual_sharpe(
    factors: pd.DataFrame,
    params: VolAdaptiveResidualSharpeParams,
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

    # Equal-weight universe return; vol-expansion ratio uses only past/current closes.
    ew_rets = daily_rets.mean(axis=1)
    vol_short = ew_rets.rolling(params.vol_short_window, min_periods=params.vol_short_window).std()
    vol_long = ew_rets.rolling(params.vol_long_window, min_periods=params.vol_long_window).std()
    vol_ratio = vol_short / vol_long.replace(0.0, pd.NA)

    # Residual (vs equal-weight universe) Sharpe: prefers names with idiosyncratic quality.
    residual_rets = daily_rets.sub(ew_rets, axis=0)
    resid_window = params.residual_sharpe_window
    residual_growth = np.expm1(
        np.log1p(residual_rets.clip(lower=-0.999999))
        .rolling(resid_window, min_periods=resid_window)
        .sum()
    )
    resid_std = residual_rets.rolling(resid_window, min_periods=resid_window).std()
    residual_sharpe = residual_growth / resid_std.replace(0.0, pd.NA)

    rows: list[dict[str, object]] = []
    filter_events: list[dict[str, object]] = []
    risk_regimes: list[dict[str, object]] = []
    signal_dates: list[pd.Timestamp] = []
    names = df.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()
    available_symbols = sorted(df["symbol"].unique().tolist())
    alpha = float(params.residual_blend_alpha)

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

        ratio = vol_ratio.loc[date] if date in vol_ratio.index else float("nan")
        risk_off = bool(pd.notna(ratio) and float(ratio) > params.vol_ratio_threshold)
        active_top_n = params.risk_off_top_n if risk_off else params.top_n
        gross = params.risk_off_gross if risk_off else 1.0
        risk_regimes.append({
            "date": date.date().isoformat(),
            "risk_off": risk_off,
            "vol_ratio": None if pd.isna(ratio) else float(ratio),
            "vol_ratio_threshold": float(params.vol_ratio_threshold),
            "active_top_n": int(active_top_n),
            "target_gross": float(gross),
            "target_cash": float(1.0 - gross),
        })

        if day_scores.empty:
            continue

        # Blend like-scaled raw and residual Sharpe for ranking only.
        rank_scores = day_scores.astype(float).copy()
        if alpha > 0.0 and date in residual_sharpe.index:
            day_resid = residual_sharpe.loc[date]
            for asset in list(rank_scores.index):
                if asset in day_resid.index and pd.notna(day_resid.loc[asset]):
                    raw = float(rank_scores.loc[asset])
                    resid = float(day_resid.loc[asset])
                    rank_scores.loc[asset] = (1.0 - alpha) * raw + alpha * resid

        try:
            curr_corr = rolling_corr.loc[date]
        except KeyError:
            curr_corr = pd.DataFrame()

        selected: list[str] = []
        for asset in rank_scores.sort_values(ascending=False).index.tolist():
            if len(selected) >= active_top_n:
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
                    "score": float(rank_scores.loc[asset]),
                })
                continue
            selected.append(str(asset))

        if selected:
            # Slot-equal weights: unused slots remain cash (gross/active_top_n per name).
            weight = gross / float(active_top_n)
            for asset in selected:
                rows.append({
                    "date": date,
                    "symbol": asset,
                    "name": names.get(asset, asset),
                    "score": float(rank_scores.loc[asset]),
                    "target_weight": weight,
                })
    selected = pd.DataFrame(rows, columns=["date", "symbol", "name", "score", "target_weight"])
    selected.attrs["signal_dates"] = signal_dates
    selected.attrs["universe_symbols"] = available_symbols
    selected.attrs["filter_events"] = filter_events
    selected.attrs["risk_regimes"] = risk_regimes
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
