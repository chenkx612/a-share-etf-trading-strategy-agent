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
    # Soft diversification: score = z(rank_factor) - corr_lambda * mean corr to already selected.
    # When corr_lambda == 0, fall back to hard corr_threshold blocking.
    corr_lambda: float = 0.0
    # Vol-scaled ranking: rank_factor = rolling Sharpe / rolling_vol(vol_window)^vol_power.
    # vol_window <= 0 disables scaling (rank by raw Sharpe). Equal slot weights unchanged.
    # vol_power > 1 demotes high-vol names more strongly than linear Sharpe/vol.
    vol_window: int = 0
    vol_power: float = 1.0
    # After a stop_loss exit, block re-entry for cooloff_days signal days. 0 disables.
    cooloff_days: int = 0
    # Pairwise observation-aware rolling corr: require min_pair_observations overlapping
    # non-NaN returns inside corr_window. 0 disables (Parent fillna(0) rolling corr).
    min_pair_observations: int = 0
    # Soft-corr penalty robustify: blend mean_corr-to-selected toward the causal
    # cross-sectional median pairwise corr among eligible candidates:
    # pen = (1 - corr_pen_median_mix) * mean_corr + corr_pen_median_mix * median_pair_corr.
    # 0.0 disables (Parent raw mean_corr). Shrinks extreme pair-corr estimates without
    # changing corr_window, corr_lambda, rank factor, or hold inertia.
    corr_pen_median_mix: float = 0.0

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
        if isinstance(self.vol_window, bool) or not isinstance(self.vol_window, int) or self.vol_window < 0:
            raise ValueError("vol_window must be an integer >= 0")
        if not math.isfinite(self.vol_power) or self.vol_power <= 0.0:
            raise ValueError("vol_power must be a finite number > 0")
        if (
            isinstance(self.cooloff_days, bool)
            or not isinstance(self.cooloff_days, int)
            or self.cooloff_days < 0
        ):
            raise ValueError("cooloff_days must be an integer >= 0")
        if (
            isinstance(self.min_pair_observations, bool)
            or not isinstance(self.min_pair_observations, int)
            or self.min_pair_observations < 0
        ):
            raise ValueError("min_pair_observations must be an integer >= 0")
        if not math.isfinite(self.corr_pen_median_mix) or not 0.0 <= self.corr_pen_median_mix <= 1.0:
            raise ValueError("corr_pen_median_mix must be in [0, 1]")

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
    # Parent anchor: corr_pen_median_mix=0.0 with min_pair_observations=40.
    # Compact 4-set grid: top_n x corr_pen_median_mix; fix reb/cooloff/min_pair.
    return [
        {
            "top_n": top_n,
            "sharpe_window": 25,
            "factor_lower_bound": 0.25,
            "corr_window": 100,
            "corr_threshold": 1.0,
            "stop_loss_pct": 0.10,
            "rebalance_every": 5,
            "max_gross": 0.75,
            "corr_lambda": 4.0,
            "vol_window": 15,
            "vol_power": 1.5,
            "cooloff_days": 5,
            "min_pair_observations": 40,
            "corr_pen_median_mix": corr_pen_median_mix,
        }
        for top_n in (3, 4)
        for corr_pen_median_mix in (0.0, 0.25)
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
    daily_rets = prices.pct_change()
    min_pair = int(params.min_pair_observations)
    if min_pair > 0:
        # Observation-aware: pairwise corr uses only joint non-NaN returns; pairs with
        # fewer than min_pair overlapping obs are NaN (soft-corr treats missing as 0 pen).
        rolling_corr = daily_rets.rolling(
            params.corr_window, min_periods=min_pair
        ).corr()
    else:
        # Parent path: fill missing returns with 0 before rolling corr.
        rolling_corr = daily_rets.fillna(0.0).rolling(params.corr_window).corr()

    rows: list[dict[str, object]] = []
    filter_events: list[dict[str, object]] = []
    signal_dates: list[pd.Timestamp] = []
    names = df.drop_duplicates("symbol").set_index("symbol")["name"].to_dict()
    available_symbols = sorted(df["symbol"].unique().tolist())

    primary_frame = df.pivot(index="date", columns="symbol", values=score_col).sort_index()
    if int(params.vol_window) > 0:
        rolling_vol = daily_rets.rolling(params.vol_window, min_periods=params.vol_window).std()
        # Causal rank: Sharpe / vol^vol_power (vol_power=1 is linear Sharpe/vol).
        vol_base = rolling_vol.replace(0.0, float("nan"))
        power = float(params.vol_power)
        if power == 1.0:
            rank_frame = primary_frame / vol_base
        else:
            rank_frame = primary_frame / (vol_base ** power)
    else:
        rank_frame = primary_frame

    held: list[str] = []
    # signal-day index after stop when cooloff ends (exclusive of re-entry until date_idx >= end).
    cooloff_until: dict[str, int] = {}
    signal_idx = -1
    days_since_rebalance = params.rebalance_every  # force first-day rebalance
    slot_weight = float(params.max_gross) / float(params.top_n)
    use_soft = float(params.corr_lambda) > 0.0
    cooloff_n = int(params.cooloff_days)

    for date in primary_frame.index:
        if start is not None and date < start:
            continue
        if end is not None and date > end:
            continue
        signal_dates.append(date)
        signal_idx += 1

        day_primary = primary_frame.loc[date].dropna()
        day_rank = rank_frame.loc[date] if date in rank_frame.index else pd.Series(dtype=float)
        day_ret = daily_rets.loc[date] if date in daily_rets.index else pd.Series(dtype=float)

        # Single-day stop on held names: drop, free slot, start cooloff.
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
                        "score": float(day_primary.loc[asset]) if asset in day_primary.index else float("nan"),
                    })
                    if cooloff_n > 0:
                        cooloff_until[str(asset)] = signal_idx + cooloff_n
                else:
                    remaining.append(asset)
            held = remaining

        days_since_rebalance += 1
        need_rebalance = days_since_rebalance >= params.rebalance_every or not held

        if need_rebalance:
            # Eligibility stays on primary rolling Sharpe > bound (not vol-scaled).
            eligible = day_primary[day_primary > params.factor_lower_bound]
            stopped_assets = {
                asset
                for asset in eligible.index
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
                    "score": float(eligible.loc[asset]),
                })
                if cooloff_n > 0:
                    cooloff_until[str(asset)] = signal_idx + cooloff_n
            eligible = eligible.drop(stopped_assets, errors="ignore")

            if cooloff_n > 0:
                blocked = {
                    asset
                    for asset in eligible.index
                    if signal_idx < int(cooloff_until.get(str(asset), -1))
                }
                for asset in sorted(blocked, key=str):
                    filter_events.append({
                        "date": date.date().isoformat(),
                        "symbol": str(asset),
                        "name": names.get(str(asset), str(asset)),
                        "filter": "cooloff",
                        "condition": "within cooloff_days after stop_loss",
                        "cooloff_days": cooloff_n,
                        "score": float(eligible.loc[asset]),
                    })
                eligible = eligible.drop(list(blocked), errors="ignore")

            # Rank among eligible by vol-scaled score when enabled; drop missing ranks.
            if int(params.vol_window) > 0:
                rank_vals = []
                for asset in eligible.index.tolist():
                    rv = day_rank[asset] if asset in day_rank.index else float("nan")
                    if pd.notna(rv) and math.isfinite(float(rv)):
                        rank_vals.append((asset, float(rv)))
                    else:
                        filter_events.append({
                            "date": date.date().isoformat(),
                            "symbol": str(asset),
                            "name": names.get(str(asset), str(asset)),
                            "filter": "vol_scale_missing",
                            "condition": "missing rolling vol for sharpe/vol rank",
                            "vol_window": int(params.vol_window),
                            "score": float(eligible.loc[asset]),
                        })
                candidates = (
                    pd.Series({a: v for a, v in rank_vals}, dtype=float)
                    if rank_vals
                    else pd.Series(dtype=float)
                )
            else:
                candidates = eligible

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
                    corr_pen_median_mix=float(params.corr_pen_median_mix),
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
                float(day_primary.loc[asset])
                if asset in day_primary.index and pd.notna(day_primary.loc[asset])
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


def _eligible_median_pairwise_corr(
    candidate_assets: list[str],
    corr: pd.DataFrame,
) -> float:
    """Causal median of pairwise corr among eligible names (upper triangle)."""
    if corr.empty or len(candidate_assets) < 2:
        return 0.0
    values: list[float] = []
    for i, a in enumerate(candidate_assets):
        if a not in corr.index:
            continue
        row = corr.loc[a]
        for b in candidate_assets[i + 1 :]:
            if b in corr.columns:
                value = row[b] if b in row.index else float("nan")
                if pd.notna(value):
                    values.append(float(value))
    if not values:
        return 0.0
    values.sort()
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return float(values[mid])
    return float((values[mid - 1] + values[mid]) / 2.0)


def _select_soft_corr(
    candidates: pd.Series,
    curr_corr: pd.DataFrame,
    top_n: int,
    corr_lambda: float,
    filter_events: list[dict[str, object]],
    date: pd.Timestamp,
    names: dict[str, str],
    corr_pen_median_mix: float = 0.0,
) -> list[str]:
    if candidates.empty:
        return []
    raw = candidates.astype(float)
    std = float(raw.std(ddof=0))
    if math.isfinite(std) and std > 0.0:
        z_scores = (raw - float(raw.mean())) / std
    else:
        z_scores = raw * 0.0

    mix = float(corr_pen_median_mix)
    median_pair = 0.0
    if mix > 0.0:
        median_pair = _eligible_median_pairwise_corr(
            [str(a) for a in raw.index.tolist()],
            curr_corr,
        )

    selected: list[str] = []
    remaining = [str(a) for a in raw.sort_values(ascending=False).index.tolist()]
    while len(selected) < top_n and remaining:
        best_asset: str | None = None
        best_val = float("-inf")
        best_pen = 0.0
        best_raw_pen = 0.0
        for asset in remaining:
            raw_pen = _mean_corr_to_selected(asset, selected, curr_corr)
            if mix > 0.0:
                pen = (1.0 - mix) * raw_pen + mix * median_pair
            else:
                pen = raw_pen
            z_val = float(z_scores.loc[asset]) if asset in z_scores.index else 0.0
            val = z_val - corr_lambda * pen
            if val > best_val:
                best_val = val
                best_asset = asset
                best_pen = pen
                best_raw_pen = raw_pen
        if best_asset is None:
            break
        for asset in remaining:
            if asset == best_asset:
                continue
            if float(raw.loc[asset]) <= float(raw.loc[best_asset]):
                continue
            asset_raw_pen = _mean_corr_to_selected(asset, selected, curr_corr)
            asset_pen = (
                (1.0 - mix) * asset_raw_pen + mix * median_pair
                if mix > 0.0
                else asset_raw_pen
            )
            if asset_raw_pen <= 0.0 and asset_pen <= 0.0:
                continue
            filter_events.append({
                "date": date.date().isoformat(),
                "symbol": str(asset),
                "name": names.get(str(asset), str(asset)),
                "filter": "soft_correlation",
                "condition": "z_sharpe - corr_lambda * corr_pen < selected",
                "corr_lambda": float(corr_lambda),
                "mean_corr_to_selected": float(asset_raw_pen),
                "corr_pen_used": float(asset_pen),
                "corr_pen_median_mix": float(mix),
                "median_pair_corr": float(median_pair),
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
