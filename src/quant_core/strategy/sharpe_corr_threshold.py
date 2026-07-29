from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from quant_core.factors import compute_factors


STRATEGY_NAME = "sharpe-corr-threshold"


@dataclass(frozen=True)
class SharpeCorrThresholdParams:
    top_n: int = 3
    sharpe_window: int = 25
    factor_lower_bound: float = 0.0
    corr_window: int = 100
    corr_threshold: float = 0.95
    stop_loss_pct: float = 0.1
    # Sticky rebalance: re-rank only every N signal days (or after stop exits).
    rebalance_every: int = 5
    # Slot-equal weight: each selected name gets max_gross/top_n (unused slots = cash).
    max_gross: float = 0.75
    # Soft diversification: score = z(Sharpe) - corr_lambda * mean corr to already selected.
    # When corr_lambda == 0, fall back to hard corr_threshold blocking.
    corr_lambda: float = 0.0

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
            isinstance(self.rebalance_every, bool)
            or not isinstance(self.rebalance_every, int)
            or self.rebalance_every <= 0
        ):
            raise ValueError("rebalance_every must be a positive integer")
        if not math.isfinite(self.max_gross) or not 0.0 < self.max_gross <= 1.0:
            raise ValueError("max_gross must be in (0, 1]")
        if not math.isfinite(self.corr_lambda) or self.corr_lambda < 0.0:
            raise ValueError("corr_lambda must be a finite number >= 0")

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
        {
            "top_n": top_n,
            "sharpe_window": 25,
            "factor_lower_bound": 0.0,
            "corr_window": 100,
            "corr_threshold": 1.0,
            "stop_loss_pct": 0.10,
            "rebalance_every": rebalance_every,
            "max_gross": max_gross,
            "corr_lambda": corr_lambda,
        }
        for top_n in (3, 4)
        for rebalance_every in (5, 10)
        for max_gross in (0.75,)
        for corr_lambda in (4.0, 6.0)
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
    # No listing ffill: pre-list and missing closes stay NaN so returns are undefined.
    prices = df.pivot(index="date", columns="symbol", values="close").sort_index()
    daily_rets = prices.pct_change(fill_method=None)
    rolling_corr = daily_rets.fillna(0.0).rolling(params.corr_window).corr()

    rows: list[dict[str, object]] = []
    filter_events: list[dict[str, object]] = []
    signal_dates: list[pd.Timestamp] = []
    names = df.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()
    available_symbols = sorted(df["symbol"].unique().tolist())

    score_frame = df.pivot(index="date", columns="symbol", values=score_col).sort_index()
    held: list[str] = []
    days_since_rebalance = params.rebalance_every  # force first-day rebalance
    slot_weight = float(params.max_gross) / float(params.top_n)
    use_soft = float(params.corr_lambda) > 0.0

    for date in score_frame.index:
        if start is not None and date < start:
            continue
        if end is not None and date > end:
            continue
        signal_dates.append(date)

        day_scores = score_frame.loc[date].dropna()
        day_ret = daily_rets.loc[date] if date in daily_rets.index else pd.Series(dtype=float)

        # Intraday stop on held names: drop and free the slot until next rebalance.
        if held:
            remaining: list[str] = []
            for asset in held:
                ret_val = day_ret[asset] if asset in day_ret.index else float("nan")
                if pd.notna(ret_val) and float(ret_val) < -params.stop_loss_pct:
                    filter_events.append({
                        "date": date.date().isoformat(),
                        "symbol": str(asset),
                        "name": names.get(str(asset), str(asset)),
                        "filter": "stop_loss",
                        "condition": "daily_return < -stop_loss_pct",
                        "daily_return": float(ret_val),
                        "stop_loss_pct": float(params.stop_loss_pct),
                        "score": float(day_scores.loc[asset]) if asset in day_scores.index else float("nan"),
                    })
                else:
                    remaining.append(asset)
            held = remaining

        days_since_rebalance += 1
        need_rebalance = days_since_rebalance >= params.rebalance_every or not held

        if need_rebalance:
            candidates = day_scores[day_scores > params.factor_lower_bound]
            stopped_assets = {
                asset
                for asset in candidates.index
                if asset in day_ret.index
                and pd.notna(day_ret[asset])
                and float(day_ret[asset]) < -params.stop_loss_pct
            }
            for asset in sorted(stopped_assets):
                filter_events.append({
                    "date": date.date().isoformat(),
                    "symbol": str(asset),
                    "name": names.get(str(asset), str(asset)),
                    "filter": "stop_loss",
                    "condition": "daily_return < -stop_loss_pct",
                    "daily_return": float(day_ret[asset]),
                    "stop_loss_pct": float(params.stop_loss_pct),
                    "score": float(candidates.loc[asset]),
                })
            candidates = candidates.drop(stopped_assets, errors="ignore")

            try:
                curr_corr = rolling_corr.loc[date]
            except KeyError:
                curr_corr = pd.DataFrame()

            if use_soft:
                held = _select_soft_corr(
                    candidates,
                    curr_corr,
                    params.top_n,
                    float(params.corr_lambda),
                    filter_events,
                    date,
                    names,
                )
            else:
                held = _select_hard_corr(
                    candidates,
                    curr_corr,
                    params.top_n,
                    float(params.corr_threshold),
                    filter_events,
                    date,
                    names,
                )
            days_since_rebalance = 0

        if not held:
            continue

        for asset in held:
            score_val = (
                float(day_scores.loc[asset])
                if asset in day_scores.index and pd.notna(day_scores.loc[asset])
                else float("nan")
            )
            rows.append({
                "date": date,
                "symbol": asset,
                "name": names.get(asset, asset),
                "score": score_val if math.isfinite(score_val) else 0.0,
                "target_weight": slot_weight,
            })

    selected = pd.DataFrame(rows, columns=["date", "symbol", "name", "score", "target_weight"])
    selected.attrs["signal_dates"] = signal_dates
    selected.attrs["universe_symbols"] = available_symbols
    selected.attrs["filter_events"] = filter_events
    return selected


def _select_hard_corr(
    candidates: pd.Series,
    curr_corr: pd.DataFrame,
    top_n: int,
    corr_threshold: float,
    filter_events: list[dict[str, object]],
    date: pd.Timestamp,
    names: dict[str, str],
) -> list[str]:
    selected: list[str] = []
    for asset in candidates.sort_values(ascending=False).index.tolist():
        if len(selected) >= top_n:
            break
        corr_block = _correlation_block(str(asset), selected, curr_corr, corr_threshold)
        if corr_block is not None:
            selected_asset, corr_value = corr_block
            filter_events.append({
                "date": date.date().isoformat(),
                "symbol": str(asset),
                "name": names.get(str(asset), str(asset)),
                "filter": "correlation",
                "condition": "correlation > corr_threshold",
                "correlation": float(corr_value),
                "corr_threshold": float(corr_threshold),
                "selected_symbol": str(selected_asset),
                "selected_name": names.get(str(selected_asset), str(selected_asset)),
                "score": float(candidates.loc[asset]),
            })
            continue
        selected.append(str(asset))
    return selected


def _select_soft_corr(
    candidates: pd.Series,
    curr_corr: pd.DataFrame,
    top_n: int,
    corr_lambda: float,
    filter_events: list[dict[str, object]],
    date: pd.Timestamp,
    names: dict[str, str],
) -> list[str]:
    if candidates.empty:
        return []
    raw = candidates.astype(float)
    std = float(raw.std(ddof=0))
    if math.isfinite(std) and std > 0.0:
        z_scores = (raw - float(raw.mean())) / std
    else:
        z_scores = raw * 0.0

    selected: list[str] = []
    remaining = [str(a) for a in raw.sort_values(ascending=False).index.tolist()]
    while len(selected) < top_n and remaining:
        best_asset: str | None = None
        best_val = float("-inf")
        best_pen = 0.0
        for asset in remaining:
            pen = _mean_corr_to_selected(asset, selected, curr_corr)
            z_val = float(z_scores.loc[asset]) if asset in z_scores.index else 0.0
            val = z_val - corr_lambda * pen
            if val > best_val:
                best_val = val
                best_asset = asset
                best_pen = pen
        if best_asset is None:
            break
        # Record high-Sharpe names skipped when a lower-Sharpe diversifier wins the slot.
        for asset in remaining:
            if asset == best_asset:
                continue
            if float(raw.loc[asset]) <= float(raw.loc[best_asset]):
                continue
            skipped_pen = _mean_corr_to_selected(asset, selected, curr_corr)
            if skipped_pen <= 0.0:
                continue
            filter_events.append({
                "date": date.date().isoformat(),
                "symbol": str(asset),
                "name": names.get(str(asset), str(asset)),
                "filter": "soft_correlation",
                "condition": "z_sharpe - corr_lambda * mean_corr < selected",
                "corr_lambda": float(corr_lambda),
                "mean_corr_to_selected": float(skipped_pen),
                "selected_symbol": str(best_asset),
                "selected_name": names.get(str(best_asset), str(best_asset)),
                "score": float(raw.loc[asset]),
            })
            break
        selected.append(best_asset)
        remaining = [a for a in remaining if a != best_asset]
    return selected


def _mean_corr_to_selected(
    asset: str,
    selected: list[str],
    corr: pd.DataFrame,
) -> float:
    if not selected or corr.empty or asset not in corr.index:
        return 0.0
    values: list[float] = []
    for selected_asset in selected:
        if selected_asset in corr.columns:
            value = corr.loc[asset, selected_asset]
            if pd.notna(value):
                values.append(float(value))
    if not values:
        return 0.0
    return float(sum(values) / len(values))


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
            if pd.notna(value) and float(value) > threshold:
                return selected_asset, float(value)
    return None
