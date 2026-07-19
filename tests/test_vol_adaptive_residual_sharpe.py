from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_core.strategy.vol_adaptive_residual_sharpe import (
    VolAdaptiveResidualSharpeParams,
    select_vol_adaptive_residual_sharpe,
)


def load_recommendation_script():
    path = (
        Path(__file__).resolve().parents[1]
        / ".agents"
        / "skills"
        / "etf-vol-adaptive-topk"
        / "scripts"
        / "recommend_next_holdings.py"
    )
    spec = importlib.util.spec_from_file_location("recommend_next_holdings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_risk_off_cannot_increase_position_count() -> None:
    with pytest.raises(ValueError, match="must not exceed top_n"):
        VolAdaptiveResidualSharpeParams(top_n=2, risk_off_top_n=3)


def test_empty_eligible_set_still_records_risk_regime() -> None:
    dates = pd.bdate_range("2026-01-01", periods=20)
    factors = pd.DataFrame({
        "date": [date for date in dates for _ in range(2)],
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
    rows = []
    for symbol, raw_score in [("A", 4.0), ("B", 1.0)]:
        for date, close in zip(dates, closes[symbol]):
            rows.append({
                "date": date,
                "symbol": symbol,
                "name": f"ETF {symbol}",
                "close": close,
                "sharpe_5": raw_score,
            })
    factors = pd.DataFrame(rows)
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


def test_recommendation_output_separates_holdings_from_filter_audit() -> None:
    script = load_recommendation_script()
    selected = pd.DataFrame([
        {
            "date": pd.Timestamp("2026-07-17"),
            "symbol": symbol,
            "name": name,
            "score": score,
            "target_weight": 0.6 / 3.0,
        }
        for symbol, name, score in [
            ("159502", "标普生物科技ETF", 5.8),
            ("164824", "印度基金LOF", 3.5),
            ("513090", "香港证券ETF", 2.4),
        ]
    ])
    selected.attrs["risk_regimes"] = [{
        "date": "2026-07-17",
        "risk_off": True,
        "vol_ratio": 1.37,
        "vol_ratio_threshold": 1.3,
        "active_top_n": 3,
        "target_gross": 0.6,
        "target_cash": 0.4,
    }]
    selected.attrs["filter_events"] = [{
        "date": "2026-07-17",
        "symbol": "OTHER",
        "name": "Other ETF",
        "filter": "correlation",
        "correlation": 0.95,
        "corr_threshold": 0.9,
        "selected_symbol": "159502",
    }]

    output, filters, risk_regime = script.build_output(selected, "2026-07-17")

    assert output.columns.tolist() == script.OUTPUT_COLUMNS
    assert "filter" not in output.columns
    assert set(output["record_type"]) == {"holding", "cash"}
    assert output.loc[output["record_type"] == "holding", "target_weight"].tolist() == [0.2, 0.2, 0.2]
    assert output.loc[output["record_type"] == "cash", "target_weight"].item() == 0.4
    assert output["target_weight"].sum() == pytest.approx(1.0)
    assert filters[0]["filter"] == "correlation"
    assert risk_regime["actual_target_gross"] == 0.6
