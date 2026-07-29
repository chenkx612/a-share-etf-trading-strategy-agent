from __future__ import annotations

import json
from datetime import date, datetime
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import quant_core.cli as cli
from quant_core.cli import build_parser
from quant_core.production import (
    ProductionContext,
    SearchResult,
    StrategyDataRequirements,
    _historical_boundaries,
    _load_requirements,
    closed_market_data_end,
    is_schedule_boundary,
    next_schedule_boundary,
    resolve_signal_date,
    run_recommendation,
    schedule_bucket,
    search_parameters,
)
from quant_core.schedule import schedule_boundaries


class FakeStrategy:
    @staticmethod
    def select_with_params(
        daily: pd.DataFrame,
        universe: pd.DataFrame,
        start: pd.Timestamp,
        end: pd.Timestamp,
        params: dict[str, object],
    ) -> pd.DataFrame:
        dates = sorted(
            pd.to_datetime(
                daily.loc[pd.to_datetime(daily["date"]).between(start, end), "date"]
            ).unique()
        )
        return pd.DataFrame(
            [
                {
                    "date": signal_date,
                    "symbol": str(params["symbol"]),
                    "name": str(params["symbol"]),
                    "score": 1.0,
                    "rank": 1,
                    "target_weight": 1.0,
                }
                for signal_date in dates
            ]
        )


def sample_daily() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", "2026-07-24")
    x = np.arange(len(dates), dtype=float)
    rows: list[dict[str, object]] = []
    for symbol, growth in (("A", 0.001), ("B", 0.0002)):
        close = 100.0 * np.exp(growth * x)
        for signal_date, price in zip(dates, close):
            rows.append(
                {
                    "date": signal_date,
                    "symbol": symbol,
                    "name": symbol,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1.0,
                    "amount": 1.0,
                    "turnover": 1.0,
                }
            )
    return pd.DataFrame(rows)


def context(root: Path) -> ProductionContext:
    production = {
        "schedule": {
            "period": "calendar_month",
            "interval": 1,
            "trigger": "start",
        },
        "train_months": 3,
        "objective": "sortino",
        "constraints": {
            "max_drawdown": {"operator": "abs<=", "threshold": 0.5},
        },
        "max_parameter_sets": 4,
        "curve_months": 3,
        "benchmark": "B",
    }
    task = SimpleNamespace(
        task_id="fake-task",
        production=production,
        raw={"data": {"universe": "universes/fake.csv"}},
    )
    task_path = root / "tasks" / "fake.toml"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("id='fake-task'\n", encoding="utf-8")
    return ProductionContext(
        root=root,
        task_path=task_path,
        task=task,
        strategy=FakeStrategy(),
        universe=pd.DataFrame(
            [{"symbol": "A", "name": "A"}, {"symbol": "B", "name": "B"}]
        ),
        grid=({"symbol": "A"}, {"symbol": "B"}),
        requirements=StrategyDataRequirements(
            (
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
            ),
            20,
        ),
        hashes={"strategy": "a", "champion": "a"},
        champion={"champion_number": 1, "champion_round_id": "001/001"},
    )


def test_load_requirements_accepts_explicit_task_contract() -> None:
    requirements = _load_requirements(
        SimpleNamespace(),
        {
            "required_columns": ["date", "symbol", "open", "close"],
            "min_history": 125,
        },
    )

    assert requirements == StrategyDataRequirements(
        ("date", "symbol", "open", "close"),
        125,
    )


def test_load_requirements_prefers_fixed_task_contract() -> None:
    strategy = SimpleNamespace(
        data_requirements=lambda: {
            "required_columns": ["date", "symbol", "open", "close"],
            "min_history": 1,
        }
    )

    requirements = _load_requirements(
        strategy,
        {
            "required_columns": [
                "date",
                "symbol",
                "name",
                "open",
                "close",
            ],
            "min_history": 125,
        },
    )

    assert requirements == StrategyDataRequirements(
        ("date", "symbol", "name", "open", "close"),
        125,
    )


def test_load_requirements_requires_strategy_or_task_declaration() -> None:
    with pytest.raises(ValueError, match="must be declared"):
        _load_requirements(SimpleNamespace())


def test_cli_recommend_is_task_action_and_old_today_options_are_rejected() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["recommend", "active_etf_rerank_topk", "--date", "2026-07-24", "--skip-refresh"]
    )
    assert args.task == "active_etf_rerank_topk"
    assert args.date == "2026-07-24"
    assert args.skip_refresh
    with pytest.raises(SystemExit):
        parser.parse_args(["recommend", "today", "--universe", "universe.csv"])


def test_cli_prints_holdings_tuning_dates_and_curve_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "outputs" / "fake-task"
    run_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "record_type": "etf",
                "symbol": "510300",
                "name": "沪深300ETF",
                "target_weight": 0.75,
            },
            {
                "record_type": "cash",
                "symbol": "CASH",
                "name": "现金",
                "target_weight": 0.25,
            },
        ]
    ).to_csv(run_dir / "recommendation.csv", index=False)
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "signal_date": "2026-07-24",
                "trade_date": "2026-07-27",
                "search_status": "reused",
                "parameter_train_months": 18,
                "parameter_schedule": {
                    "period": "calendar_month",
                    "interval": 1,
                    "trigger": "start",
                },
                "last_tuning_date": "2026-07-01",
                "next_tuning_date": "2026-08-03",
                "recommendation_path": str(
                    (run_dir / "recommendation.csv").relative_to(tmp_path)
                ),
                "curve_png_path": str(
                    (run_dir / "causal_curve.png").relative_to(tmp_path)
                ),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "resolve_research_task_reference",
        lambda task, root: tmp_path / "tasks" / "fake.toml",
    )
    monkeypatch.setattr(cli, "run_recommendation", lambda *args, **kwargs: summary_path)

    cli.command_recommend(
        SimpleNamespace(
            task="fake-task",
            root=str(tmp_path),
            date="2026-07-24",
            skip_refresh=True,
        )
    )

    output = capsys.readouterr().out
    assert "parameter policy: 18-month lookback; calendar_month/start" in output
    assert (
        "parameter search: actually searched on 2026-07-01; "
        "next scheduled boundary 2026-08-03"
    ) in output
    assert "a late first run therefore has a shorter reuse span" in output
    assert "next-day target holdings:" in output
    assert "510300" in output
    assert "75.00%" in output
    assert "recent causal return curve:" in output


def test_schedule_supports_month_start_week_end_and_stable_trading_day_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.DatetimeIndex(
        [
            "2026-06-29",
            "2026-06-30",
            "2026-07-01",
            "2026-07-02",
            "2026-07-03",
            "2026-07-06",
        ]
    )
    exchange_dates = tuple(
        pd.DatetimeIndex(
            ["2026-06-25", "2026-06-26", *dates.astype(str).tolist()]
        ).date
    )
    monkeypatch.setattr(
        "quant_core.schedule.exchange_trade_dates", lambda: exchange_dates
    )
    monthly = {"period": "calendar_month", "interval": 1, "trigger": "start"}
    weekly = {"period": "iso_week", "interval": 1, "trigger": "end"}
    every_two = {"period": "trading_day", "interval": 2, "trigger": "start"}
    assert is_schedule_boundary(pd.Timestamp("2026-07-01"), monthly, dates)
    assert is_schedule_boundary(pd.Timestamp("2026-07-03"), weekly, dates)
    assert is_schedule_boundary(pd.Timestamp("2026-07-01"), every_two, dates)
    assert schedule_bucket(pd.Timestamp("2026-07-01"), monthly, dates).startswith(
        "calendar_month:"
    )
    assert schedule_bucket(pd.Timestamp("2026-07-01"), every_two, dates) == schedule_bucket(
        pd.Timestamp("2026-07-01"), every_two, dates[2:]
    )


def test_calendar_end_fallback_requires_a_following_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.DatetimeIndex(
        ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-10"]
    )
    monkeypatch.setattr("quant_core.schedule.exchange_trade_dates", lambda: ())
    monthly_end = {
        "period": "calendar_month",
        "interval": 1,
        "trigger": "end",
    }

    assert is_schedule_boundary(pd.Timestamp("2026-06-30"), monthly_end, dates)
    assert not is_schedule_boundary(pd.Timestamp("2026-07-10"), monthly_end, dates)
    assert schedule_boundaries(dates, monthly_end) == [
        pd.Timestamp("2026-06-30")
    ]


def test_calendar_boundaries_use_only_normalized_local_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.to_datetime(
        [
            "2026-06-29 15:00",
            "2026-06-30",
            "2026-06-30 15:00",
            "2026-07-06",
            "2026-07-07",
        ],
        format="mixed",
    )
    monkeypatch.setattr(
        "quant_core.schedule.exchange_trade_dates",
        lambda: pytest.fail("calendar period boundaries must not query a provider"),
    )

    assert schedule_boundaries(
        dates,
        {"period": "calendar_month", "interval": 1, "trigger": "start"},
    ) == [pd.Timestamp("2026-06-29"), pd.Timestamp("2026-07-06")]
    assert schedule_boundaries(
        dates,
        {"period": "calendar_month", "interval": 1, "trigger": "end"},
    ) == [pd.Timestamp("2026-06-30")]
    assert schedule_boundaries(
        dates,
        {"period": "iso_week", "interval": 1, "trigger": "start"},
    ) == [pd.Timestamp("2026-06-29"), pd.Timestamp("2026-07-06")]
    assert schedule_boundaries(
        dates,
        {"period": "iso_week", "interval": 1, "trigger": "end"},
    ) == [pd.Timestamp("2026-06-30")]


def test_schedule_boundaries_scans_normalized_dates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.bdate_range("2000-01-03", periods=5_000)
    monkeypatch.setattr("quant_core.schedule.exchange_trade_dates", lambda: ())
    monkeypatch.setattr(
        "quant_core.schedule.is_schedule_boundary",
        lambda *args, **kwargs: pytest.fail(
            "batch boundary discovery must not rescan through is_schedule_boundary"
        ),
    )

    boundaries = schedule_boundaries(
        dates,
        {"period": "calendar_month", "interval": 1, "trigger": "start"},
    )

    assert len(boundaries) > 100


def test_curve_uses_last_real_tuning_date_before_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.bdate_range("2026-04-01", "2026-07-31")
    monkeypatch.setattr(
        "quant_core.schedule.exchange_trade_dates",
        lambda: tuple(dates.date),
    )
    schedule = {"period": "calendar_month", "interval": 1, "trigger": "start"}
    boundaries = _historical_boundaries(
        dates,
        schedule,
        pd.Timestamp("2026-05-15"),
    )
    assert boundaries == [
        pd.Timestamp("2026-05-01"),
        pd.Timestamp("2026-06-01"),
        pd.Timestamp("2026-07-01"),
    ]


def test_next_tuning_date_is_next_schedule_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.bdate_range("2026-07-01", "2026-09-30")
    monkeypatch.setattr(
        "quant_core.schedule.exchange_trade_dates",
        lambda: tuple(dates.date),
    )
    schedule = {"period": "calendar_month", "interval": 1, "trigger": "start"}
    assert next_schedule_boundary(
        pd.Timestamp("2026-07-24"),
        schedule,
        dates[dates <= pd.Timestamp("2026-07-24")],
    ) == pd.Timestamp("2026-08-03")


def test_signal_date_never_uses_an_unclosed_current_session() -> None:
    daily = pd.DataFrame(
        {"date": pd.to_datetime(["2026-07-23", "2026-07-24"])}
    )
    assert resolve_signal_date(
        daily,
        pd.Timestamp("2026-07-24").date(),
        now=datetime(2026, 7, 24, 14, 59, tzinfo=pd.Timestamp.now(tz="Asia/Shanghai").tz),
    ) == pd.Timestamp("2026-07-23")


def test_market_data_refresh_uses_previous_date_before_current_session_closes() -> None:
    early_morning = datetime(
        2026,
        7,
        30,
        0,
        30,
        tzinfo=pd.Timestamp.now(tz="Asia/Shanghai").tz,
    )

    assert closed_market_data_end(
        pd.Timestamp("2026-07-30").date(),
        now=early_morning,
    ) == pd.Timestamp("2026-07-29").date()
    assert closed_market_data_end(
        pd.Timestamp("2026-07-29").date(),
        now=early_morning,
    ) == pd.Timestamp("2026-07-29").date()


def test_recommendation_applies_preclose_cutoff_before_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = context(tmp_path)
    daily = sample_daily()
    refresh_dates: list[date] = []
    early_morning = datetime(
        2026,
        7,
        30,
        0,
        30,
        tzinfo=pd.Timestamp.now(tz="Asia/Shanghai").tz,
    )
    monkeypatch.setattr(
        "quant_core.production.load_production_context",
        lambda root, task_path: ctx,
    )

    def fake_refresh(
        production_context: ProductionContext,
        requested: date,
        *,
        skip_refresh: bool,
    ) -> pd.DataFrame:
        refresh_dates.append(requested)
        return daily

    monkeypatch.setattr("quant_core.production._refresh_data", fake_refresh)

    summary_path = run_recommendation(
        tmp_path,
        ctx.task_path,
        now=early_morning,
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert refresh_dates == [pd.Timestamp("2026-07-29").date()]
    assert summary["signal_date"] == "2026-07-24"


def test_search_is_deterministic_and_uses_only_data_through_signal(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    daily = sample_daily()
    signal = pd.Timestamp("2026-04-30")
    before = search_parameters(ctx, daily, signal)
    mutated = daily.copy()
    mutated.loc[mutated["date"] > signal, "open"] *= 100.0
    mutated.loc[mutated["date"] > signal, "close"] *= 100.0
    after = search_parameters(ctx, mutated, signal)
    assert before.parameters == after.parameters == {"symbol": "A"}
    assert before.metrics == after.metrics


def test_success_writes_holdings_summary_curve_and_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = context(tmp_path)
    daily = sample_daily()
    monkeypatch.setattr(
        "quant_core.production.load_production_context",
        lambda root, task_path: ctx,
    )
    monkeypatch.setattr(
        "quant_core.production._refresh_data",
        lambda context, requested, skip_refresh: daily,
    )
    summary_path = run_recommendation(
        tmp_path,
        ctx.task_path,
        requested_date=pd.Timestamp("2026-07-01").date(),
        skip_refresh=True,
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    recommendation = pd.read_csv(tmp_path / summary["recommendation_path"])
    curve = pd.read_csv(tmp_path / summary["curve_csv_path"])
    assert summary_path == tmp_path / "outputs" / "fake-task" / "summary.json"
    assert summary["recommendation_path"] == "outputs/fake-task/recommendation.csv"
    assert summary["status"] == "completed"
    assert summary["search_status"] == "searched"
    assert summary["parameter_train_months"] == 3
    assert summary["parameter_schedule"] == ctx.task.production["schedule"]
    assert summary["last_tuning_date"] == "2026-07-01"
    assert summary["next_tuning_date"] == "2026-08-03"
    assert recommendation["target_weight"].sum() == pytest.approx(1.0)
    assert set(recommendation["record_type"]) == {"etf", "cash"}
    assert not curve.empty
    png = tmp_path / summary["curve_png_path"]
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(
        "quant_core.production.causal_replay",
        lambda context, daily, signal_date: (
            curve.assign(date=pd.to_datetime(curve["date"])),
            summary["curve_metrics"],
            [],
        ),
    )
    reused_path = run_recommendation(
        tmp_path,
        ctx.task_path,
        requested_date=pd.Timestamp("2026-07-02").date(),
        skip_refresh=True,
    )
    reused = json.loads(reused_path.read_text(encoding="utf-8"))
    assert reused_path == summary_path
    assert reused["search_status"] == "reused"
    assert reused["signal_date"] == "2026-07-02"
    assert not (tmp_path / "outputs" / "fake-task" / "2026-07-01").exists()


def test_midmonth_first_run_backfills_the_month_start_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = context(tmp_path)
    daily = sample_daily()
    exchange_dates = tuple(pd.bdate_range("2024-01-02", "2026-07-31").date)
    monkeypatch.setattr(
        "quant_core.schedule.exchange_trade_dates", lambda: exchange_dates
    )
    calls: list[pd.Timestamp] = []

    def fake_search(
        context: ProductionContext, daily: pd.DataFrame, signal_date: pd.Timestamp
    ) -> SearchResult:
        signal = pd.Timestamp(signal_date)
        calls.append(signal)
        return SearchResult(
            signal,
            signal - pd.DateOffset(months=3),
            signal,
            {"symbol": "A"},
            {"sortino": 1.0},
            (),
        )

    curve = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-29", "2026-06-30"]),
            "strategy_return": [0.0, 0.0],
            "benchmark_return": [0.0, 0.0],
            "strategy_equity": [1.0, 1.0],
            "benchmark_equity": [1.0, 1.0],
        }
    )
    metrics = {
        "strategy_annual_return": 0.0,
        "strategy_max_drawdown": 0.0,
        "benchmark_annual_return": 0.0,
        "benchmark_max_drawdown": 0.0,
    }
    monkeypatch.setattr(
        "quant_core.production.load_production_context", lambda root, task_path: ctx
    )
    monkeypatch.setattr(
        "quant_core.production._refresh_data",
        lambda context, requested, skip_refresh: daily,
    )
    monkeypatch.setattr("quant_core.production.search_parameters", fake_search)
    monkeypatch.setattr(
        "quant_core.production.causal_replay",
        lambda context, daily, signal_date: (curve, metrics, []),
    )
    summary_path = run_recommendation(
        tmp_path,
        ctx.task_path,
        requested_date=pd.Timestamp("2026-07-24").date(),
        skip_refresh=True,
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert calls == [pd.Timestamp("2026-07-01")]
    assert summary["search_status"] == "searched"
    assert summary["last_tuning_date"] == "2026-07-01"


def test_recommendation_reuses_legacy_parameter_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = context(tmp_path)
    daily = sample_daily()
    legacy_store = (
        tmp_path / "outputs" / "production" / "fake-task" / "parameters"
    )
    legacy_store.mkdir(parents=True)
    legacy_freeze = legacy_store / "calendar_month-24318.json"
    legacy_freeze.write_text(
        json.dumps(
            {
                "searched_on": "2026-07-01",
                "parameters": {"symbol": "A"},
                "input_hashes": dict(ctx.hashes),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "quant_core.production.load_production_context", lambda root, task_path: ctx
    )
    monkeypatch.setattr(
        "quant_core.production._refresh_data",
        lambda context, requested, skip_refresh: daily,
    )
    monkeypatch.setattr(
        "quant_core.production.search_parameters",
        lambda *args, **kwargs: pytest.fail("legacy freeze should be reused"),
    )
    curve = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-22", "2026-07-23"]),
            "strategy_return": [0.0, 0.0],
            "benchmark_return": [0.0, 0.0],
            "strategy_equity": [1.0, 1.0],
            "benchmark_equity": [1.0, 1.0],
        }
    )
    metrics = {
        "strategy_annual_return": 0.0,
        "strategy_max_drawdown": 0.0,
        "benchmark_annual_return": 0.0,
        "benchmark_max_drawdown": 0.0,
    }
    monkeypatch.setattr(
        "quant_core.production.causal_replay",
        lambda context, daily, signal_date: (curve, metrics, []),
    )

    summary_path = run_recommendation(
        tmp_path,
        ctx.task_path,
        requested_date=pd.Timestamp("2026-07-24").date(),
        skip_refresh=True,
    )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    search = json.loads(
        (tmp_path / summary["parameter_search_path"]).read_text(encoding="utf-8")
    )
    assert summary["search_status"] == "reused"
    assert search["freeze_path"] == str(legacy_freeze.relative_to(tmp_path))


def test_signal_date_is_resolved_only_from_strategy_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = context(tmp_path)
    ctx = replace(
        base,
        universe=pd.DataFrame([{"symbol": "A", "name": "A"}]),
    )
    daily = sample_daily()
    daily = daily[
        ~((daily["symbol"] == "A") & (daily["date"] == pd.Timestamp("2026-07-24")))
    ]
    monkeypatch.setattr(
        "quant_core.production.load_production_context", lambda root, task_path: ctx
    )
    monkeypatch.setattr(
        "quant_core.production._refresh_data",
        lambda context, requested, skip_refresh: daily,
    )
    monkeypatch.setattr(
        "quant_core.production.search_parameters",
        lambda context, daily, signal_date: SearchResult(
            signal_date,
            signal_date - pd.DateOffset(months=3),
            signal_date,
            {"symbol": "A"},
            {"sortino": 1.0},
            (),
        ),
    )
    curve = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-07-22", "2026-07-23"]),
            "strategy_return": [0.0, 0.0],
            "benchmark_return": [0.0, 0.0],
            "strategy_equity": [1.0, 1.0],
            "benchmark_equity": [1.0, 1.0],
        }
    )
    metrics = {
        "strategy_annual_return": 0.0,
        "strategy_max_drawdown": 0.0,
        "benchmark_annual_return": 0.0,
        "benchmark_max_drawdown": 0.0,
    }
    monkeypatch.setattr(
        "quant_core.production.causal_replay",
        lambda context, daily, signal_date: (curve, metrics, []),
    )
    summary_path = run_recommendation(
        tmp_path,
        ctx.task_path,
        requested_date=pd.Timestamp("2026-07-24").date(),
        skip_refresh=True,
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["signal_date"] == "2026-07-23"


def test_failed_search_writes_audit_without_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = context(tmp_path)
    ctx.task.production["constraints"]["annual_return"] = {
        "operator": ">=",
        "threshold": 100.0,
    }
    daily = sample_daily()
    monkeypatch.setattr(
        "quant_core.production.load_production_context",
        lambda root, task_path: ctx,
    )
    monkeypatch.setattr(
        "quant_core.production._refresh_data",
        lambda context, requested, skip_refresh: daily,
    )
    with pytest.raises(RuntimeError, match="no set satisfying"):
        run_recommendation(
            tmp_path,
            ctx.task_path,
            requested_date=pd.Timestamp("2026-07-24").date(),
            skip_refresh=True,
        )
    assert list(
        (tmp_path / ".cache" / "production" / "fake-task" / "failures").glob("*.json")
    )
    assert not (tmp_path / "outputs" / "fake-task" / "recommendation.csv").exists()
