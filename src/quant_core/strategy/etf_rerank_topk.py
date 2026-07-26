from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


STRATEGY_NAME = "active-etf-rerank-topk"


@dataclass(frozen=True)
class EtfRerankTopKParams:
    """Dual-sleeve cross-sectional reranker for liquid ETFs."""

    top_n: int = 1
    momentum_window: int = 120
    vol_window: int = 60
    min_history: int = 120
    mom_sleeve_weight: float = 0.30
    use_sharpe: bool = False
    rank_buffer: int = 1
    use_relative_mom: bool = True
    sleeve_dedup: bool = False
    use_efficiency_scale: bool = True
    mom_accel_lag: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.top_n, bool) or not isinstance(self.top_n, int) or self.top_n <= 0:
            raise ValueError("top_n must be a positive integer")
        if (
            isinstance(self.momentum_window, bool)
            or not isinstance(self.momentum_window, int)
            or self.momentum_window <= 1
        ):
            raise ValueError("momentum_window must be an integer > 1")
        if isinstance(self.vol_window, bool) or not isinstance(self.vol_window, int) or self.vol_window <= 1:
            raise ValueError("vol_window must be an integer > 1")
        if (
            isinstance(self.min_history, bool)
            or not isinstance(self.min_history, int)
            or self.min_history < max(self.momentum_window, self.vol_window)
        ):
            raise ValueError("min_history must be >= max(momentum_window, vol_window)")
        if not math.isfinite(self.mom_sleeve_weight) or not 0.0 <= self.mom_sleeve_weight <= 1.0:
            raise ValueError("mom_sleeve_weight must be in [0, 1]")
        if not isinstance(self.use_sharpe, bool):
            raise ValueError("use_sharpe must be a bool")
        if isinstance(self.rank_buffer, bool) or not isinstance(self.rank_buffer, int) or self.rank_buffer < 0:
            raise ValueError("rank_buffer must be a non-negative integer")
        if not isinstance(self.use_relative_mom, bool):
            raise ValueError("use_relative_mom must be a bool")
        if not isinstance(self.sleeve_dedup, bool):
            raise ValueError("sleeve_dedup must be a bool")
        if not isinstance(self.use_efficiency_scale, bool):
            raise ValueError("use_efficiency_scale must be a bool")
        if (
            isinstance(self.mom_accel_lag, bool)
            or not isinstance(self.mom_accel_lag, int)
            or self.mom_accel_lag < 0
        ):
            raise ValueError("mom_accel_lag must be a non-negative integer")
        if self.mom_accel_lag > 0 and self.mom_accel_lag < 2:
            raise ValueError("mom_accel_lag must be 0 (off) or >= 2")


def select(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    return select_with_params(daily, universe, start, end, _params_to_dict(EtfRerankTopKParams()))


def data_requirements() -> dict[str, object]:
    """Declare production inputs without exposing parameter-name conventions."""
    return {
        "required_columns": [
            "date",
            "symbol",
            "name",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "turnover",
        ],
        "min_history": 140,
    }


def parameter_grid() -> list[dict[str, object]]:
    grid: list[dict[str, object]] = []
    # Champion baseline signals unchanged. Grid simplification (priority 3):
    # remove mom_accel_lag from search (fix 0=level-only); keep top_n and momentum_window.
    # Rationale: under 12m train, non-zero accel selection is mixed and may add fold jitter
    # without consistent validation-direction edge versus level-only ER-scaled mom.
    for top_n in (1, 2):
        for momentum_window in (90, 120):
            vol_window = 60
            min_history = max(momentum_window, vol_window)
            grid.append(
                {
                    "top_n": top_n,
                    "momentum_window": momentum_window,
                    "vol_window": vol_window,
                    "min_history": min_history,
                    "mom_sleeve_weight": 0.30,
                    "use_sharpe": False,
                    "rank_buffer": 1,
                    "use_relative_mom": True,
                    "sleeve_dedup": False,
                    "use_efficiency_scale": True,
                    "mom_accel_lag": 0,
                }
            )
    return grid


def select_with_params(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    params: dict[str, object],
) -> pd.DataFrame:
    strategy_params = EtfRerankTopKParams(**params)
    return select_etf_rerank_topk(
        daily,
        strategy_params,
        start=start,
        end=end,
        universe_symbols=set(universe["symbol"].astype(str)),
    )


def _cross_sectional_percentile_returns(ret: np.ndarray) -> np.ndarray:
    """Per-day cross-sectional average-rank percentiles in (0, 1] on finite names."""
    out = np.full_like(ret, np.nan)
    n_rows, _ = ret.shape
    for i in range(n_rows):
        row = ret[i]
        finite = np.isfinite(row)
        count = int(np.count_nonzero(finite))
        if count == 0:
            continue
        vals = row[finite]
        order = np.argsort(vals, kind="mergesort")
        ranks = np.empty(count, dtype=float)
        sorted_vals = vals[order]
        start = 0
        while start < count:
            end = start + 1
            while end < count and sorted_vals[end] == sorted_vals[start]:
                end += 1
            avg_rank = 0.5 * (start + 1 + end)
            ranks[order[start:end]] = avg_rank
            start = end
        percentiles = ranks / float(count)
        out[i, finite] = percentiles
    return out


def _rolling_sum_full_window(values: np.ndarray, window: int) -> np.ndarray:
    """Causal rolling sum requiring a full window of finite observations."""
    n_rows, _ = values.shape
    out = np.full_like(values, np.nan)
    if window <= 0 or window > n_rows:
        return out
    finite = np.isfinite(values)
    filled = np.where(finite, values, 0.0)
    cumulative_sum = np.cumsum(filled, axis=0)
    cumulative_count = np.cumsum(finite.astype(np.int32), axis=0)
    out[window - 1] = np.where(
        cumulative_count[window - 1] == window,
        cumulative_sum[window - 1],
        np.nan,
    )
    if window < n_rows:
        sums = cumulative_sum[window:] - cumulative_sum[:-window]
        counts = cumulative_count[window:] - cumulative_count[:-window]
        out[window:] = np.where(counts == window, sums, np.nan)
    return out


def _rolling_efficiency_ratio(values: np.ndarray, window: int) -> np.ndarray:
    """Causal Kaufman efficiency: sum(r)/sum(|r|) over a full finite window, clipped to [0, 1]."""
    n_rows, _ = values.shape
    out = np.full_like(values, np.nan)
    if window <= 0 or window > n_rows:
        return out
    finite = np.isfinite(values)
    filled = np.where(finite, values, 0.0)
    abs_filled = np.where(finite, np.abs(values), 0.0)
    cum_sum = np.cumsum(filled, axis=0)
    cum_abs = np.cumsum(abs_filled, axis=0)
    cum_count = np.cumsum(finite.astype(np.int32), axis=0)

    def _er(sum_r: np.ndarray, sum_abs: np.ndarray, count: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            raw = np.where(sum_abs > 0.0, sum_r / sum_abs, np.nan)
        return np.where(count == window, np.clip(raw, 0.0, 1.0), np.nan)

    out[window - 1] = _er(cum_sum[window - 1], cum_abs[window - 1], cum_count[window - 1])
    if window < n_rows:
        sums = cum_sum[window:] - cum_sum[:-window]
        abs_sums = cum_abs[window:] - cum_abs[:-window]
        counts = cum_count[window:] - cum_count[:-window]
        out[window:] = _er(sums, abs_sums, counts)
    return out


def select_etf_rerank_topk(
    daily: pd.DataFrame,
    params: EtfRerankTopKParams,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    universe_symbols: set[str] | None = None,
) -> pd.DataFrame:
    columns = ["date", "symbol", "name", "score", "rank", "target_weight"]
    empty = pd.DataFrame(columns=columns)
    if daily is None or daily.empty:
        return empty

    df = daily.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype(str)
    if "name" not in df.columns:
        df["name"] = df["symbol"]
    if universe_symbols is not None:
        df = df[df["symbol"].isin({str(symbol) for symbol in universe_symbols})]
    df = df.drop_duplicates(["date", "symbol"], keep="last")
    df = df.sort_values(["symbol", "date"])
    if df.empty:
        return empty

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    close_is_valid = np.isfinite(df["close"].to_numpy(dtype=float, copy=False)) & (df["close"] > 0.0)
    df = df[close_is_valid]
    if df.empty:
        return empty

    names = df.drop_duplicates("symbol").set_index("symbol")["name"].astype(str).to_dict()
    available_symbols = sorted(df["symbol"].unique().tolist())
    close = df.pivot(index="date", columns="symbol", values="close").sort_index()
    symbols = close.columns.to_numpy()
    if len(symbols) == 0:
        return empty

    close_values = close.to_numpy(dtype=float, copy=False)
    returns = np.full_like(close_values, np.nan)
    returns[1:] = close_values[1:] / close_values[:-1] - 1.0
    both_finite = (
        np.isfinite(close_values[1:])
        & np.isfinite(close_values[:-1])
        & (close_values[:-1] > 0.0)
    )
    returns[1:] = np.where(both_finite, returns[1:], np.nan)

    momentum_window = int(params.momentum_window)
    vol_window = int(params.vol_window)
    raw_momentum = np.full_like(close_values, np.nan)
    if momentum_window < close_values.shape[0]:
        previous = close_values[:-momentum_window]
        current = close_values[momentum_window:]
        valid = np.isfinite(previous) & np.isfinite(current) & (previous > 0.0)
        raw_momentum[momentum_window:] = np.where(
            valid,
            current / previous - 1.0,
            np.nan,
        )

    if params.use_relative_mom:
        percentile_returns = _cross_sectional_percentile_returns(returns)
        momentum = _rolling_sum_full_window(percentile_returns, momentum_window)
    else:
        momentum = raw_momentum

    if params.use_efficiency_scale:
        efficiency = _rolling_efficiency_ratio(returns, momentum_window)
        momentum = np.where(
            np.isfinite(momentum) & np.isfinite(efficiency),
            momentum * efficiency,
            np.nan,
        )

    accel_lag = int(params.mom_accel_lag)
    if accel_lag >= 2:
        # Causal acceleration of the already ER-scaled momentum level:
        # score_t = level_t + (level_t - level_{t-lag}) when both finite; else keep level.
        accelerated = momentum.copy()
        level = momentum[accel_lag:]
        lagged = momentum[:-accel_lag]
        both = np.isfinite(level) & np.isfinite(lagged)
        accelerated[accel_lag:] = np.where(both, level + (level - lagged), level)
        momentum = accelerated

    return_frame = pd.DataFrame(returns, index=close.index, columns=close.columns)
    volatility = return_frame.rolling(
        vol_window,
        min_periods=vol_window,
    ).std().to_numpy(dtype=float, copy=False)
    with np.errstate(divide="ignore", invalid="ignore"):
        inverse_volatility = np.where(volatility > 0.0, 1.0 / volatility, np.nan)
        momentum_signal = (
            np.where(volatility > 0.0, momentum / volatility, np.nan)
            if params.use_sharpe
            else momentum
        )

    history = np.cumsum(np.isfinite(close_values).astype(np.int32), axis=0)
    top_n = int(params.top_n)
    momentum_weight = float(params.mom_sleeve_weight)
    defensive_weight = 1.0 - momentum_weight
    keep_rank = top_n + int(params.rank_buffer)
    minimum_history = float(params.min_history)

    rows: list[dict[str, object]] = []
    signal_dates: list[pd.Timestamp] = []
    previous_momentum: set[int] = set()

    for i, date in enumerate(close.index.to_numpy()):
        timestamp = pd.Timestamp(date)
        if start is not None and timestamp < start:
            continue
        if end is not None and timestamp > end:
            continue
        signal_dates.append(timestamp)

        eligible = (
            np.isfinite(close_values[i])
            & np.isfinite(momentum_signal[i])
            & np.isfinite(inverse_volatility[i])
            & (history[i] >= minimum_history)
        )
        eligible_indexes = np.flatnonzero(eligible)
        if eligible_indexes.size == 0:
            previous_momentum = set()
            continue

        momentum_values = momentum_signal[i, eligible_indexes]
        defensive_values = inverse_volatility[i, eligible_indexes]
        candidate_symbols = symbols[eligible_indexes]

        momentum_secondary = np.argsort(candidate_symbols, kind="mergesort")
        momentum_primary = np.argsort(
            -momentum_values[momentum_secondary],
            kind="mergesort",
        )
        momentum_order = eligible_indexes[momentum_secondary[momentum_primary]]

        defensive_secondary = np.argsort(candidate_symbols, kind="mergesort")
        defensive_primary = np.argsort(
            -defensive_values[defensive_secondary],
            kind="mergesort",
        )
        defensive_order = eligible_indexes[defensive_secondary[defensive_primary]]

        momentum_selected: list[int] = []
        if previous_momentum and keep_rank > 0:
            rank_map = {int(index): rank + 1 for rank, index in enumerate(momentum_order)}
            sticky = [
                index
                for index in momentum_order
                if int(index) in previous_momentum and rank_map[int(index)] <= keep_rank
            ]
            momentum_selected.extend(int(index) for index in sticky[:top_n])
        if len(momentum_selected) < top_n:
            selected_set = set(momentum_selected)
            for index in momentum_order:
                integer_index = int(index)
                if integer_index in selected_set:
                    continue
                momentum_selected.append(integer_index)
                selected_set.add(integer_index)
                if len(momentum_selected) >= top_n:
                    break

        momentum_set = set(momentum_selected)
        if params.sleeve_dedup:
            defensive_selected = [
                int(index)
                for index in defensive_order
                if int(index) not in momentum_set
            ][:top_n]
        else:
            defensive_selected = [int(index) for index in defensive_order[:top_n]]
        weights: dict[int, float] = {}
        if momentum_selected and momentum_weight > 0.0:
            sleeve_weight = momentum_weight / float(len(momentum_selected))
            for index in momentum_selected:
                weights[index] = weights.get(index, 0.0) + sleeve_weight
        if defensive_selected and defensive_weight > 0.0:
            sleeve_weight = defensive_weight / float(len(defensive_selected))
            for index in defensive_selected:
                weights[index] = weights.get(index, 0.0) + sleeve_weight
        if not weights:
            previous_momentum = set()
            continue

        total_weight = float(sum(weights.values()))
        if total_weight > 1.0 + 1e-12:
            weights = {index: weight / total_weight for index, weight in weights.items()}

        momentum_rank = {int(index): rank + 1 for rank, index in enumerate(momentum_order)}
        defensive_rank = {int(index): rank + 1 for rank, index in enumerate(defensive_order)}
        eligible_count = float(eligible_indexes.size)
        ordered_weights = sorted(
            weights.items(),
            key=lambda item: (-item[1], str(symbols[item[0]])),
        )
        for rank, (index, target_weight) in enumerate(ordered_weights, start=1):
            symbol = str(symbols[index])
            momentum_percentile = 1.0 - (
                momentum_rank.get(index, eligible_count) - 1.0
            ) / eligible_count
            defensive_percentile = 1.0 - (
                defensive_rank.get(index, eligible_count) - 1.0
            ) / eligible_count
            score = (
                momentum_weight * momentum_percentile
                + defensive_weight * defensive_percentile
            )
            rows.append(
                {
                    "date": timestamp,
                    "symbol": symbol,
                    "name": names.get(symbol, symbol),
                    "score": float(score),
                    "rank": int(rank),
                    "target_weight": float(target_weight),
                }
            )
        previous_momentum = set(momentum_selected)

    selected = pd.DataFrame(rows, columns=columns)
    selected.attrs["signal_dates"] = signal_dates
    selected.attrs["universe_symbols"] = available_symbols
    return selected


def _params_to_dict(params: EtfRerankTopKParams) -> dict[str, object]:
    return {
        "top_n": params.top_n,
        "momentum_window": params.momentum_window,
        "vol_window": params.vol_window,
        "min_history": params.min_history,
        "mom_sleeve_weight": params.mom_sleeve_weight,
        "use_sharpe": params.use_sharpe,
        "rank_buffer": params.rank_buffer,
        "use_relative_mom": params.use_relative_mom,
        "sleeve_dedup": params.sleeve_dedup,
        "use_efficiency_scale": params.use_efficiency_scale,
        "mom_accel_lag": params.mom_accel_lag,
    }
