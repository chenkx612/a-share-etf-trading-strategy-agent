from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import quant_core.data.market_data as market_data_module
from quant_core.data.market_data import (
    PartialMarketDataRefreshError,
    fetch_daily_if_stale,
    refresh_window_start,
    resolve_complete_universe_date,
)


TEST_DATE = date(2026, 6, 12)
REQUEST_END = date(2026, 6, 14)


def _daily(symbol: str, signal_date: date = TEST_DATE) -> dict[str, object]:
    return {
        "date": pd.Timestamp(signal_date),
        "symbol": symbol,
        "name": f"ETF {symbol}",
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 1.0,
        "amount": 1.0,
        "turnover": 1.0,
    }


def test_resolve_complete_universe_date_uses_latest_complete_session() -> None:
    universe = pd.DataFrame([
        {"symbol": "510300", "name": "a"},
        {"symbol": "510500", "name": "b"},
    ])
    daily = pd.DataFrame([
        {"date": "2026-06-11", "symbol": "510300"},
        {"date": "2026-06-11", "symbol": "510500"},
        {"date": TEST_DATE.isoformat(), "symbol": "510300"},
    ])

    assert resolve_complete_universe_date(
        daily,
        universe,
        TEST_DATE.isoformat(),
    ) == "2026-06-11"


def test_resolve_complete_universe_date_fails_closed() -> None:
    universe = pd.DataFrame([
        {"symbol": "510300", "name": "a"},
        {"symbol": "510500", "name": "b"},
    ])
    daily = pd.DataFrame([
        {"date": TEST_DATE.isoformat(), "symbol": "510300"},
    ])

    with pytest.raises(RuntimeError, match="No complete recommendation date"):
        resolve_complete_universe_date(daily, universe, TEST_DATE.isoformat())


def test_fetch_daily_if_stale_skips_complete_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data_module,
        "latest_trade_date_on_or_before",
        lambda target: TEST_DATE,
    )
    universe = pd.DataFrame([{"symbol": "510300", "name": "沪深300ETF"}])
    calls: list[tuple[date, date]] = []

    incoming, target = fetch_daily_if_stale(
        universe,
        TEST_DATE,
        REQUEST_END,
        existing=pd.DataFrame([_daily("510300")]),
        fetch_one=lambda _one, start, end: calls.append((start, end)) or pd.DataFrame(),
    )

    assert incoming.empty
    assert target == TEST_DATE
    assert calls == []


def test_fetch_daily_if_stale_refreshes_only_missing_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data_module,
        "latest_trade_date_on_or_before",
        lambda target: TEST_DATE,
    )
    universe = pd.DataFrame([
        {"symbol": "510300", "name": "沪深300ETF"},
        {"symbol": "510500", "name": "中证500ETF"},
    ])
    fetched: list[str] = []

    def fetch_one(single: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        assert start == date(2021, 5, 14)
        assert end == REQUEST_END
        fetched.extend(single["symbol"].astype(str))
        return pd.DataFrame([_daily("510300")])

    incoming, target = fetch_daily_if_stale(
        universe,
        date(2010, 1, 1),
        REQUEST_END,
        existing=pd.DataFrame([_daily("510500")]),
        fetch_one=fetch_one,
    )

    assert target == TEST_DATE
    assert fetched == ["510300"]
    assert incoming["symbol"].tolist() == ["510300"]


def test_refresh_window_start_includes_one_month_buffer() -> None:
    assert refresh_window_start(date(2026, 8, 27)) == date(2021, 7, 27)


def test_fetch_daily_if_stale_preserves_partial_refresh_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data_module,
        "latest_trade_date_on_or_before",
        lambda target: TEST_DATE,
    )
    universe = pd.DataFrame([
        {"symbol": "510300", "name": "沪深300ETF"},
        {"symbol": "510500", "name": "中证500ETF"},
    ])

    with pytest.raises(
        PartialMarketDataRefreshError,
        match="missing symbols=\\['510500'\\]",
    ) as raised:
        fetch_daily_if_stale(
            universe,
            date(2026, 1, 1),
            REQUEST_END,
            existing=None,
            fetch_one=lambda *_args: pd.DataFrame([_daily("510300")]),
        )

    assert raised.value.incoming["symbol"].tolist() == ["510300"]
