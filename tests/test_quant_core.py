from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from quant_core.backtest.engine import compute_metrics, factor_ic, run_backtest
from quant_core.cli import (
    ensure_sharpe_factor_columns,
    metrics_satisfy_constraint,
    recommendation_output_frame,
    sort_optimization_results,
)
from quant_core.data.market_data import (
    AkshareMarketDataClient,
    normalize_tencent_daily,
    to_tencent_symbol,
)
from quant_core.factors import compute_factors
from quant_core.strategy.sharpe_corr_threshold import (
    SharpeCorrThresholdParams,
    select_sharpe_corr_threshold,
)


def sample_daily() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=45, freq="D")
    rows = []
    for i, symbol in enumerate(["510300", "510500", "159915"]):
        for j, day in enumerate(dates):
            price = 1.0 + i * 0.2 + j * (0.01 + i * 0.002)
            rows.append({
                "date": day,
                "symbol": symbol,
                "name": f"ETF{symbol}",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1000 + j,
                "amount": (1000 + j) * price,
                "turnover": 1.0 + i,
            })
    return pd.DataFrame(rows)


def test_sharpe_corr_threshold_selection_backtest_loop() -> None:
    daily = sample_daily()
    factors = compute_factors(daily)
    assert "sharpe_20" in factors.columns
    assert "sharpe_25" in factors.columns
    selected = select_sharpe_corr_threshold(
        factors,
        SharpeCorrThresholdParams(top_n=2, corr_window=10),
        start=pd.Timestamp("2024-01-25"),
        end=pd.Timestamp("2024-02-10"),
        universe_symbols={"510300", "510500", "159915"},
    )
    assert not selected.empty
    assert selected.groupby("date")["symbol"].count().max() <= 2
    assert selected.groupby("date")["target_weight"].sum().le(1.0).all()

    result = run_backtest(daily, selected)
    assert not result.daily_returns.empty
    assert "total_return" in result.metrics
    assert result.positions["weight"].gt(0).all()


def test_compute_factors_supports_parameterized_sharpe_windows() -> None:
    daily = sample_daily()
    factors = compute_factors(daily, sharpe_windows=[10, 30])

    assert "sharpe_10" in factors.columns
    assert "sharpe_30" in factors.columns
    assert "sharpe_25" not in factors.columns
    assert factors["sharpe_10"].notna().any()


def test_missing_sharpe_factor_columns_are_computed_from_daily() -> None:
    daily = sample_daily()
    factors = compute_factors(daily, sharpe_windows=[20]).drop(columns=["sharpe_20"])

    enriched = ensure_sharpe_factor_columns(factors, daily, [20, 30])

    assert "sharpe_20" in enriched.columns
    assert "sharpe_30" in enriched.columns
    assert enriched["sharpe_20"].notna().any()


def test_metrics_include_sortino() -> None:
    daily_returns = pd.DataFrame({
        "net_return": [0.02, -0.01, 0.03, -0.005],
        "turnover": [0.1, 0.2, 0.1, 0.2],
    })

    metrics = compute_metrics(daily_returns)

    assert "sortino" in metrics
    assert metrics["sortino"] > 0


def test_drawdown_lt_return_constraint_and_sorting() -> None:
    assert metrics_satisfy_constraint(
        {"annual_return": 0.2, "max_drawdown": -0.1},
        "drawdown-lt-return",
    )
    assert not metrics_satisfy_constraint(
        {"annual_return": 0.05, "max_drawdown": -0.1},
        "drawdown-lt-return",
    )
    results = pd.DataFrame([
        {"name": "invalid_high", "sortino": 5.0, "valid": False},
        {"name": "valid_low", "sortino": 1.0, "valid": True},
        {"name": "valid_high", "sortino": 2.0, "valid": True},
    ])

    sorted_results = sort_optimization_results(results, "sortino", "drawdown-lt-return")

    assert sorted_results.iloc[0]["name"] == "valid_high"


def test_sharpe_corr_threshold_leaves_cash_and_can_liquidate() -> None:
    dates = pd.date_range("2024-01-01", periods=4, freq="D")
    daily = pd.DataFrame([
        {
            "date": day,
            "symbol": symbol,
            "name": f"ETF{symbol}",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000,
            "amount": 100000.0,
            "turnover": 1.0,
        }
        for day in dates
        for symbol in ["510300", "510500"]
    ])
    factors = daily[daily["date"] < dates[-1]].copy()
    factors["sharpe_25"] = [
        1.0, 0.5,
        0.8, -0.2,
        -0.1, -0.3,
    ]
    params = SharpeCorrThresholdParams(top_n=2, corr_window=1)

    selected = select_sharpe_corr_threshold(
        factors,
        params,
        start=dates[0],
        end=dates[2],
        universe_symbols={"510300", "510500"},
    )

    weights_by_date = selected.groupby("date")["target_weight"].sum()
    assert weights_by_date.loc[dates[0]] == pytest.approx(1.0)
    assert weights_by_date.loc[dates[1]] == pytest.approx(0.5)
    assert dates[2] not in weights_by_date.index

    result = run_backtest(daily, selected, fee_rate=0.0)
    assert dates[3] not in set(result.positions["date"])


def test_sharpe_corr_threshold_stop_loss_filters_single_day_candidates() -> None:
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    closes = {
        "510300": [100.0, 100.0, 88.0],
        "510500": [100.0, 101.0, 102.0],
        "159915": [100.0, 99.0, 100.0],
    }
    rows = []
    for symbol, prices in closes.items():
        for day, close in zip(dates, prices, strict=True):
            rows.append({
                "date": day,
                "symbol": symbol,
                "name": f"ETF{symbol}",
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
                "amount": close * 1000,
                "turnover": 1.0,
                "sharpe_25": 0.1,
            })
    factors = pd.DataFrame(rows)
    factors.loc[
        factors["date"].eq(dates[-1]),
        "sharpe_25",
    ] = [10.0, 5.0, 4.0]

    selected = select_sharpe_corr_threshold(
        factors,
        SharpeCorrThresholdParams(
            top_n=2,
            corr_window=2,
            stop_loss_pct=0.1,
        ),
        start=dates[-1],
        end=dates[-1],
        universe_symbols={"510300", "510500", "159915"},
    )

    assert "510300" not in set(selected["symbol"])
    assert selected["symbol"].tolist() == ["510500", "159915"]
    assert selected.attrs["filter_events"] == [
        {
            "date": "2024-01-03",
            "symbol": "510300",
            "name": "ETF510300",
            "filter": "stop_loss",
            "condition": "daily_return < -stop_loss_pct",
            "daily_return": pytest.approx(-0.12),
            "stop_loss_pct": 0.1,
            "score": 10.0,
        }
    ]
    output = recommendation_output_frame(selected)
    assert output["record_type"].tolist() == ["recommendation", "recommendation", "filtered"]
    filtered_row = output[output["record_type"].eq("filtered")].iloc[0]
    assert filtered_row["symbol"] == "510300"
    assert filtered_row["filter"] == "stop_loss"
    assert filtered_row["target_weight"] == 0.0


def test_sharpe_corr_threshold_corr_filter_works_for_single_day_selection() -> None:
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    closes = {
        "510300": [100.0, 101.0, 102.0, 103.0, 104.0],
        "510500": [200.0, 202.0, 204.0, 206.0, 208.0],
        "159915": [100.0, 99.0, 101.0, 98.0, 100.0],
    }
    rows = []
    for symbol, prices in closes.items():
        for day, close in zip(dates, prices, strict=True):
            rows.append({
                "date": day,
                "symbol": symbol,
                "name": f"ETF{symbol}",
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
                "amount": close * 1000,
                "turnover": 1.0,
                "sharpe_25": 0.1,
            })
    factors = pd.DataFrame(rows)
    factors.loc[
        factors["date"].eq(dates[-1]),
        "sharpe_25",
    ] = [10.0, 9.0, 8.0]

    selected = select_sharpe_corr_threshold(
        factors,
        SharpeCorrThresholdParams(
            top_n=2,
            corr_window=3,
            corr_threshold=0.9,
        ),
        start=dates[-1],
        end=dates[-1],
        universe_symbols={"510300", "510500", "159915"},
    )

    assert selected["symbol"].tolist() == ["510300", "159915"]
    assert selected.attrs["filter_events"] == [
        {
            "date": "2024-01-05",
            "symbol": "510500",
            "name": "ETF510500",
            "filter": "correlation",
            "condition": "correlation > corr_threshold",
            "correlation": pytest.approx(1.0),
            "corr_threshold": 0.9,
            "selected_symbol": "510300",
            "selected_name": "ETF510300",
            "score": 9.0,
        }
    ]


def test_backtest_trades_at_next_open_and_uses_open_to_open_returns() -> None:
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    daily = pd.DataFrame({
        "date": dates,
        "symbol": ["510300"] * 3,
        "name": ["ETF510300"] * 3,
        "open": [100.0, 110.0, 121.0],
        "high": [1000.0, 1000.0, 1000.0],
        "low": [1.0, 1.0, 1.0],
        "close": [1000.0, 100.0, 1000.0],
        "volume": [1000, 1000, 1000],
        "amount": [100000.0, 110000.0, 121000.0],
        "turnover": [1.0, 1.0, 1.0],
    })
    selected = pd.DataFrame({
        "date": [dates[0]],
        "symbol": ["510300"],
        "target_weight": [1.0],
    })

    result = run_backtest(daily, selected, fee_rate=0.0)

    returns = result.daily_returns.set_index("date")["gross_return"]
    assert returns.loc[dates[0]] == 0.0
    assert returns.loc[dates[1]] == pytest.approx(0.099)
    assert returns.loc[dates[2]] == 0.0
    positions = result.positions.set_index("date")
    assert positions.loc[dates[1], "shares"] == 900
    assert positions["shares"].mod(100).eq(0).all()


def test_factor_ic_uses_tradeable_next_open_forward_return() -> None:
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    factors = pd.DataFrame({
        "date": [dates[0], dates[1], dates[2], dates[0], dates[1], dates[2]],
        "symbol": ["510300", "510300", "510300", "510500", "510500", "510500"],
        "score": [2.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        "open": [10.0, 10.0, 20.0, 10.0, 10.0, 5.0],
        "close": [10.0, 1.0, 1.0, 10.0, 100.0, 100.0],
    })

    result = factor_ic(factors)

    assert result["ic"] == pytest.approx(1.0)
    assert result["rank_ic"] == pytest.approx(1.0)


def test_market_data_client_falls_back_to_tencent_with_qfq(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fund_etf_hist_em(**kwargs):
        calls.append(("eastmoney", kwargs))
        raise ConnectionError("eastmoney unavailable")

    def stock_zh_a_hist_tx(**kwargs):
        calls.append(("tencent", kwargs))
        return pd.DataFrame({
            "date": ["2024-01-02"],
            "open": [1.0],
            "high": [1.1],
            "low": [0.9],
            "close": [1.05],
            "amount": [1000],
        })

    fake_akshare = SimpleNamespace(
        fund_etf_hist_em=fund_etf_hist_em,
        stock_zh_a_hist_tx=stock_zh_a_hist_tx,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    universe = pd.DataFrame([{"symbol": "159915", "name": "cyb"}])

    daily = AkshareMarketDataClient(adjust="qfq").fetch_daily(
        universe,
        date(2024, 1, 1),
        date(2024, 1, 31),
    )

    assert not daily.empty
    assert daily.loc[0, "symbol"] == "159915"
    assert daily.loc[0, "volume"] == 1000
    assert calls[0][0] == "eastmoney"
    assert calls[1] == (
        "tencent",
        {
            "symbol": "sz159915",
            "start_date": "20240101",
            "end_date": "20240131",
            "adjust": "qfq",
        },
    )


def test_normalize_tencent_daily_keeps_volume_when_amount_also_present() -> None:
    raw = pd.DataFrame({
        "date": ["2026-07-22"],
        "open": [3.65],
        "close": [3.586],
        "high": [3.716],
        "low": [3.575],
        "volume": [2.59e9],
        "turnover": [0.15],
        "amount": [9.48e9],
    })

    daily = normalize_tencent_daily(raw, "159915", "cyb")

    assert list(daily.columns) == [
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
    ]
    assert daily.loc[0, "volume"] == pytest.approx(2.59e9)
    assert daily.loc[0, "amount"] == pytest.approx(9.48e9)
    assert daily.loc[0, "symbol"] == "159915"


def test_tencent_symbol_uses_shanghai_prefix_for_5_6_9_codes() -> None:
    assert to_tencent_symbol("518880") == "sh518880"
    assert to_tencent_symbol("159915") == "sz159915"
