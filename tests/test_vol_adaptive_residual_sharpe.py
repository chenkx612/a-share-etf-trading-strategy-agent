from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_core.strategy.vol_adaptive_residual_sharpe import (
    VolAdaptiveResidualSharpeParams,
    select_vol_adaptive_residual_sharpe,
)


def test_risk_off_cannot_increase_position_count() -> None:
    with pytest.raises(ValueError, match="must not exceed top_n"):
        VolAdaptiveResidualSharpeParams(top_n=2, risk_off_top_n=3)


def test_empty_eligible_set_still_records_risk_regime() -> None:
    dates = pd.bdate_range("2026-01-01", periods=20)
    factors = pd.DataFrame({
        "date": [signal_date for signal_date in dates for _ in range(2)],
        "symbol": ["A", "B"] * len(dates),
        "name": ["ETF A", "ETF B"] * len(dates),
        "close": np.tile([100.0, 101.0], len(dates)),
        "sharpe_5": [0.1, 0.2] * len(dates),
    })
    params = VolAdaptiveResidualSharpeParams(
        top_n=2,
        sharpe_window=5,
        factor_lower_bound=1.0,
        corr_window=5,
        vol_short_window=3,
        vol_long_window=10,
        risk_off_top_n=1,
        residual_sharpe_window=3,
    )

    selected = select_vol_adaptive_residual_sharpe(
        factors,
        params,
        start=dates[-1],
        end=dates[-1],
    )

    assert selected.empty
    assert selected.attrs["risk_regimes"][0]["date"] == dates[-1].date().isoformat()


def test_reported_score_is_the_residual_blended_rank_score() -> None:
    dates = pd.bdate_range("2026-01-01", periods=30)
    phase = np.arange(len(dates), dtype=float)
    closes = {
        "A": 100.0 * np.cumprod(1.01 + 0.004 * np.sin(phase)),
        "B": 100.0 * np.cumprod(0.998 + 0.003 * np.cos(phase)),
    }
    factors = pd.DataFrame([
        {
            "date": signal_date,
            "symbol": symbol,
            "name": f"ETF {symbol}",
            "close": close,
            "sharpe_5": raw_score,
        }
        for symbol, raw_score in (("A", 4.0), ("B", 1.0))
        for signal_date, close in zip(dates, closes[symbol])
    ])
    params = VolAdaptiveResidualSharpeParams(
        top_n=1,
        sharpe_window=5,
        corr_window=5,
        stop_loss_pct=1.0,
        vol_short_window=3,
        vol_long_window=10,
        risk_off_top_n=1,
        residual_sharpe_window=5,
        residual_blend_alpha=0.5,
    )

    selected = select_vol_adaptive_residual_sharpe(
        factors,
        params,
        start=dates[-1],
        end=dates[-1],
    )

    assert selected.iloc[0]["symbol"] == "A"
    assert selected.iloc[0]["score"] != pytest.approx(4.0)
    assert all(
        event["filter"] != "residual_blend"
        for event in selected.attrs["filter_events"]
    )
