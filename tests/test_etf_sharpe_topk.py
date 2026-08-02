from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import quant_core.data.market_data as market_data_module
import quant_core.cli as cli_module
from quant_core.commands import analysis as analysis_commands
from quant_core.data.market_data import (
    AkshareMarketDataClient,
    fetch_daily_if_stale,
    resolve_complete_universe_date,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPT_DIR = REPO_ROOT / ".agents" / "skills" / "etf-sharpe-topk" / "scripts"
TEST_DATE = "2026-06-12"
TEST_DAY = date(2026, 6, 12)
TEST_END_DAY = date(2026, 6, 14)


def load_skill_script(script_name: str):
    spec = importlib.util.spec_from_file_location(script_name, SKILL_SCRIPT_DIR / f"{script_name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[script_name] = module
    spec.loader.exec_module(module)
    return module


def test_resolve_complete_universe_date_requires_complete_universe() -> None:
    universe = pd.DataFrame([
        {"symbol": "510300", "name": "a"},
        {"symbol": "510500", "name": "b"},
    ])
    daily = pd.DataFrame([
        {"date": "2026-06-11", "symbol": "510300"},
        {"date": "2026-06-11", "symbol": "510500"},
        {"date": TEST_DATE, "symbol": "510300"},
    ])

    recommendation_date = resolve_complete_universe_date(
        daily,
        universe,
        TEST_DATE,
    )

    assert recommendation_date == "2026-06-11"


def test_resolve_complete_universe_date_fails_when_no_complete_date() -> None:
    universe = pd.DataFrame([
        {"symbol": "510300", "name": "a"},
        {"symbol": "510500", "name": "b"},
    ])
    daily = pd.DataFrame([
        {"date": TEST_DATE, "symbol": "510300"},
    ])

    with pytest.raises(RuntimeError, match="No complete recommendation date"):
        resolve_complete_universe_date(
            daily,
            universe,
            TEST_DATE,
        )


def test_normalize_spot_frame_maps_market_columns() -> None:
    selector = load_skill_script("select_etf_candidates")
    spot = pd.DataFrame([
        {"代码": "510300", "名称": "沪深300ETF", "涨跌幅": "1.2", "总市值": 20_000_000_000, "最新价": "4.1"},
        {"代码": "512760", "名称": "芯片ETF", "涨跌幅": "3.5", "总市值": 30_000_000_000, "最新价": "1.0"},
    ])

    frame = selector.normalize_spot_frame(spot, trade_date=TEST_DATE)

    assert frame["symbol"].tolist() == ["510300", "512760"]
    assert frame["return_pct"].tolist() == [1.2, 3.5]
    assert frame["fund_size"].tolist() == [20_000_000_000, 30_000_000_000]
    assert frame["data_date"].tolist() == [TEST_DATE, TEST_DATE]


def test_etf_pool_skill_uses_shared_universe_default() -> None:
    runner = load_skill_script("utils")
    selector = load_skill_script("select_etf_candidates")

    assert runner.DEFAULT_ROOT == ".agents/skills/etf-sharpe-topk/outputs"
    assert selector.DEFAULT_BASE_POOL == REPO_ROOT / "universes" / "sector_rotation.csv"
    assert runner.DEFAULT_BASE_POOL == selector.DEFAULT_BASE_POOL
    assert runner.run_dir(Path(runner.DEFAULT_ROOT), "current") == Path(".agents/skills/etf-sharpe-topk/outputs")
    assert runner.run_dir(Path(runner.DEFAULT_ROOT), "reviewed") == Path(
        ".agents/skills/etf-sharpe-topk/outputs/reviewed"
    )


def test_selector_base_universe_can_override_shared_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = load_skill_script("select_etf_candidates")
    custom_universe = tmp_path / "custom.csv"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "select_etf_candidates.py",
            "--base-universe",
            str(custom_universe),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert selector.parse_args().base_universe == str(custom_universe)


def test_sharpe_apply_updates_destination_and_dry_run_does_not(
    tmp_path: Path,
) -> None:
    runner = load_skill_script("utils")
    destination = tmp_path / "sector_rotation.csv"
    run_dir = tmp_path / "outputs"
    run_dir.mkdir()
    before = pd.DataFrame([{"symbol": "A", "name": "ETF A", "fund_size": 1.0}])
    after = pd.DataFrame([{"symbol": "B", "name": "ETF B", "fund_size": 2.0}])
    before.to_csv(destination, index=False)

    assert runner.apply_selected_universe(
        apply=False,
        base_universe=before,
        selected_universe=after,
        run_dir_path=run_dir,
        destination=destination,
    ) is None
    pd.testing.assert_frame_equal(pd.read_csv(destination), before)
    assert not list(run_dir.glob("universe_before.*"))

    backup = runner.apply_selected_universe(
        apply=True,
        base_universe=before,
        selected_universe=after,
        run_dir_path=run_dir,
        destination=destination,
    )

    assert backup is not None
    pd.testing.assert_frame_equal(pd.read_csv(destination), after)
    backed_up = pd.read_parquet(backup) if backup.suffix == ".parquet" else pd.read_csv(backup)
    pd.testing.assert_frame_equal(backed_up, before)


def test_etf_pool_uses_five_year_data_window_and_three_year_backtest_window() -> None:
    runner = load_skill_script("utils")

    assert runner.default_data_start("2026-06-12") == "2021-06-12"
    assert runner.default_start("2026-06-12") == "2023-06-12"
    assert runner.default_data_start("2024-02-29") == "2019-02-28"
    assert runner.default_start("2024-02-29") == "2021-02-28"


def test_selector_loads_base_chinese_names_from_shared_universe_csv(tmp_path: Path) -> None:
    selector = load_skill_script("select_etf_candidates")
    base_path = tmp_path / "sector_rotation.csv"
    pd.DataFrame([
        {"symbol": "159915", "name": "创业板", "fund_size": pd.NA},
        {"symbol": "512800", "name": "银行ETF", "fund_size": pd.NA},
    ]).to_csv(base_path, index=False)

    base = selector.load_base_pool(base_path)

    assert base["symbol"].tolist() == ["159915", "512800"]
    assert base["name"].tolist() == ["创业板", "银行ETF"]
    assert "semantic_name" not in base.columns


def test_selector_allows_keyword_theme_overlap_for_ai_review() -> None:
    selector = load_skill_script("select_etf_candidates")
    rows = [
        selector.CandidateRow(
            symbol="512800",
            name="银行ETF",
            fund_size=30_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=5.0,
            theme="finance",
            in_base_pool=False,
            base_theme_overlap=True,
            base_theme_matches="512800:银行ETF",
        ),
        selector.CandidateRow(
            symbol="159530",
            name="机器人ETF",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=4.0,
            theme="robotics",
            in_base_pool=False,
        ),
        selector.CandidateRow(
            symbol="159766",
            name="旅游ETF",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=3.0,
            theme="consumer",
            in_base_pool=False,
        ),
    ]

    selected = selector.select_diversified(rows, count=2)

    assert [row.symbol for row in selected] == ["512800", "159530"]


def test_selector_normalizes_etf_exposure_name_before_base_deduplication() -> None:
    selector = load_skill_script("select_etf_candidates")

    assert selector.normalized_etf_exposure_name("通信ETF华夏") == "通信"
    assert selector.normalized_etf_exposure_name("纳指ETF广发") == "纳指"
    assert selector.normalized_etf_exposure_name(" 纳指ETF ") == "纳指"
    assert selector.normalized_etf_exposure_name("创业板") == "创业板"


def test_selector_deduplicates_base_and_candidate_exposure_names() -> None:
    selector = load_skill_script("select_etf_candidates")
    rows = [
        selector.CandidateRow(
            symbol="515880",
            name="通信ETF华夏",
            fund_size=30_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=7.0,
            theme="other",
            in_base_pool=False,
            base_name_duplicate=True,
        ),
        selector.CandidateRow(
            symbol="159501",
            name="纳指ETF广发",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=6.0,
            theme="overseas",
            in_base_pool=False,
            base_name_duplicate=True,
        ),
        selector.CandidateRow(
            symbol="512801",
            name="银行ETF华夏",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=5.0,
            theme="finance",
            in_base_pool=False,
        ),
        selector.CandidateRow(
            symbol="512802",
            name="银行ETF易方达",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=4.0,
            theme="finance",
            in_base_pool=False,
        ),
        selector.CandidateRow(
            symbol="159766",
            name="旅游ETF",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=3.0,
            theme="consumer",
            in_base_pool=False,
        ),
    ]

    deduplicated = selector.script_deduplicated_rows(rows)
    selected = selector.select_diversified(rows, count=2)

    assert [row.symbol for row in deduplicated] == ["512801", "159766"]
    assert [row.symbol for row in selected] == ["512801", "159766"]


def test_selector_default_selection_fills_after_script_deduplication() -> None:
    selector = load_skill_script("select_etf_candidates")
    rows = [
        selector.CandidateRow(
            symbol="512801",
            name="银行ETF华夏",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=5.0,
            theme="finance",
            in_base_pool=False,
        ),
        selector.CandidateRow(
            symbol="512802",
            name="银行ETF易方达",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=4.0,
            theme="finance",
            in_base_pool=False,
        ),
        selector.CandidateRow(
            symbol="159766",
            name="旅游ETF",
            fund_size=20_000_000_000,
            date=TEST_DATE,
            latest_price=1.0,
            return_pct=3.0,
            theme="consumer",
            in_base_pool=False,
        ),
    ]

    selected = selector.resolve_selected(argparse.Namespace(candidates=None, top_shortlist=2, count=2), rows)

    assert [row.symbol for row in selected] == ["512801", "159766"]


def test_runner_requires_reviewed_candidates_for_full_automation() -> None:
    runner = load_skill_script("utils")

    with pytest.raises(SystemExit, match="Full automation requires reviewed candidates"):
        runner.require_reviewed_candidates(argparse.Namespace(candidates=None))

    runner.require_reviewed_candidates(argparse.Namespace(candidates="510300,510500,512100"))


def test_runner_prepares_reviewed_candidates_from_candidate_shortlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = load_skill_script("utils")
    pd.DataFrame([
        {
            "symbol": "512760",
            "name": "芯片ETF",
            "fund_size": 20_000_000_000,
            "date": TEST_DATE,
            "latest_price": 1.0,
            "return_pct": 3.0,
            "theme": "semiconductor",
            "in_base_pool": False,
        },
        {
            "symbol": "515050",
            "name": "5GETF",
            "fund_size": 15_000_000_000,
            "date": TEST_DATE,
            "latest_price": 1.0,
            "return_pct": 2.0,
            "theme": "other",
            "in_base_pool": "False",
        },
        {
            "symbol": "512800",
            "name": "银行ETF",
            "fund_size": 30_000_000_000,
            "date": TEST_DATE,
            "latest_price": 1.0,
            "return_pct": 1.0,
            "theme": "finance",
            "in_base_pool": False,
        },
    ]).to_csv(tmp_path / "candidate_shortlist.csv", index=False)
    pd.DataFrame([
        {"symbol": "512760"},
        {"symbol": "512800"},
        {"symbol": "159915"},
    ]).to_csv(tmp_path / "candidate_selected.csv", index=False)
    monkeypatch.setattr(
        runner,
        "load_base_pool",
        lambda: pd.DataFrame([{"symbol": "159915", "name": "cyb", "fund_size": pd.NA}]),
    )

    state = runner.prepare_reviewed_candidates(
        argparse.Namespace(
            candidates="512760,515050,512800",
            date=TEST_DATE,
            data_root=".",
        ),
        tmp_path,
    )

    assert state["payload"]["candidate_arg"] == "512760,515050,512800"
    assert state["payload"]["manual_override"] is True
    assert state["selected"]["symbol"].tolist() == ["512760", "515050", "512800"]
    assert state["expanded"]["symbol"].tolist() == ["159915", "512760", "515050", "512800"]


def test_runner_allows_reviewed_candidates_with_base_theme_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = load_skill_script("utils")
    pd.DataFrame([
        {
            "symbol": "512760",
            "name": "芯片ETF",
            "fund_size": 20_000_000_000,
            "date": TEST_DATE,
            "latest_price": 1.0,
            "return_pct": 3.0,
            "theme": "semiconductor",
            "in_base_pool": False,
            "base_theme_overlap": True,
            "base_theme_matches": "588170:科创半导体ETF",
        },
        {
            "symbol": "515050",
            "name": "5GETF",
            "fund_size": 15_000_000_000,
            "date": TEST_DATE,
            "latest_price": 1.0,
            "return_pct": 2.0,
            "theme": "other",
            "in_base_pool": False,
            "base_theme_overlap": False,
            "base_theme_matches": "",
        },
        {
            "symbol": "159530",
            "name": "机器人ETF",
            "fund_size": 15_000_000_000,
            "date": TEST_DATE,
            "latest_price": 1.0,
            "return_pct": 1.0,
            "theme": "robotics",
            "in_base_pool": False,
            "base_theme_overlap": False,
            "base_theme_matches": "",
        },
    ]).to_csv(tmp_path / "candidate_shortlist.csv", index=False)
    monkeypatch.setattr(
        runner,
        "load_base_pool",
        lambda: pd.DataFrame([{"symbol": "159915", "name": "cyb", "fund_size": pd.NA}]),
    )

    state = runner.prepare_reviewed_candidates(
        argparse.Namespace(
            candidates="512760,515050,159530",
            date=TEST_DATE,
            data_root=".",
        ),
        tmp_path,
    )

    assert state["payload"]["candidate_arg"] == "512760,515050,159530"
    assert state["selected"]["symbol"].tolist() == ["512760", "515050", "159530"]
    assert bool(state["selected"].loc[0, "base_theme_overlap"]) is True
    assert state["selected"].loc[0, "base_theme_matches"] == "588170:科创半导体ETF"


def test_data_update_does_not_create_outputs_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe_dir = tmp_path / "universes"
    universe_dir.mkdir()
    universe_path = universe_dir / "sector_rotation.csv"
    pd.DataFrame([{"symbol": "510300", "name": "沪深300ETF"}]).to_csv(
        universe_path,
        index=False,
    )
    daily = pd.DataFrame([
        {
            "date": pd.Timestamp(TEST_DATE),
            "symbol": "510300",
            "name": "沪深300ETF",
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1,
            "amount": 1,
            "turnover": 1,
        }
    ])
    clients = []

    class DummyMarketDataClient:
        def __init__(self, adjust: str) -> None:
            self.adjust = adjust
            clients.append(self)

        def fetch_daily(self, *args: object, **kwargs: object) -> pd.DataFrame:
            raise AssertionError("fetch_daily should be supplied to the stale-fetch helper only")

    monkeypatch.setattr(
        "quant_core.commands.analysis.AkshareMarketDataClient",
        DummyMarketDataClient,
    )
    monkeypatch.setattr(
        analysis_commands,
        "fetch_daily_if_stale",
        lambda *args, **kwargs: (daily, TEST_DAY),
    )

    cli_module.command_data_update(
        argparse.Namespace(
            root=str(tmp_path),
            start=TEST_DATE,
            end=TEST_DATE,
            universe=str(universe_path),
            universe_name="sector-rotation",
            adjust=None,
        )
    )

    assert clients[0].adjust == "qfq"
    assert not (tmp_path / "outputs").exists()
    data_files = list((tmp_path / "data").iterdir())
    assert len(data_files) == 1
    assert data_files[0].stem == "etf_daily"


def test_data_update_replaces_refreshed_symbol_history_and_keeps_current_symbols(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame([
        {"symbol": "510300", "name": "沪深300ETF"},
        {"symbol": "510500", "name": "中证500ETF"},
    ]).to_csv(universe_path, index=False)
    existing = pd.DataFrame([
        {"date": "2020-01-02", "symbol": "510300", "name": "沪深300ETF", "close": 1.0},
        {"date": TEST_DATE, "symbol": "510500", "name": "中证500ETF", "close": 2.0},
    ])
    incoming = pd.DataFrame([
        {"date": TEST_DATE, "symbol": "510300", "name": "沪深300ETF", "close": 3.0},
    ])
    written: list[pd.DataFrame] = []

    monkeypatch.setattr(
        analysis_commands,
        "AkshareMarketDataClient",
        lambda adjust: argparse.Namespace(fetch_daily=lambda *args, **kwargs: pd.DataFrame()),
    )
    monkeypatch.setattr(
        "quant_core.commands.analysis.read_daily",
        lambda paths: existing.copy(),
    )
    monkeypatch.setattr(
        analysis_commands,
        "fetch_daily_if_stale",
        lambda *args, **kwargs: (incoming.copy(), TEST_DAY),
    )
    monkeypatch.setattr(
        "quant_core.commands.analysis.write_table",
        lambda frame, path: written.append(frame.copy()) or path,
    )

    cli_module.command_data_update(
        argparse.Namespace(
            root=str(tmp_path),
            start="2021-06-12",
            end=TEST_DATE,
            universe=str(universe_path),
            universe_name="sector-rotation",
            adjust="qfq",
        )
    )

    assert len(written) == 1
    refreshed = written[0][written[0]["symbol"].astype(str).eq("510300")]
    current = written[0][written[0]["symbol"].astype(str).eq("510500")]
    assert pd.to_datetime(refreshed["date"]).tolist() == [pd.Timestamp(TEST_DATE)]
    assert pd.to_datetime(current["date"]).tolist() == [pd.Timestamp(TEST_DATE)]


def test_data_update_does_not_rewrite_cache_when_all_symbols_are_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    universe_path = tmp_path / "universe.csv"
    pd.DataFrame([{"symbol": "510300", "name": "沪深300ETF"}]).to_csv(universe_path, index=False)
    existing = pd.DataFrame([{"date": TEST_DATE, "symbol": "510300", "close": 1.0}])

    monkeypatch.setattr(
        "quant_core.commands.analysis.read_daily",
        lambda paths: existing.copy(),
    )
    monkeypatch.setattr(
        analysis_commands,
        "fetch_daily_if_stale",
        lambda *args, **kwargs: (pd.DataFrame(), TEST_DAY),
    )
    monkeypatch.setattr(
        analysis_commands,
        "write_table",
        lambda *args, **kwargs: pytest.fail("current cache must not be rewritten"),
    )

    cli_module.command_data_update(
        argparse.Namespace(
            root=str(tmp_path),
            start="2021-06-12",
            end=TEST_DATE,
            universe=str(universe_path),
            universe_name="sector-rotation",
            adjust="qfq",
        )
    )


def test_recommendation_outputs_are_copied_directly_to_automation_dir(tmp_path: Path) -> None:
    runner = load_skill_script("utils")
    rec_root = tmp_path / "workspace"
    automation_dir = tmp_path / "outputs"
    recommendation_dir = rec_root / "outputs" / "recommendations"
    factors_dir = rec_root / "outputs" / "factors"
    recommendation_dir.mkdir(parents=True)
    factors_dir.mkdir(parents=True)
    automation_dir.mkdir(parents=True)
    pd.DataFrame([{"date": TEST_DATE, "symbol": "510300", "score": 1.0}]).to_csv(
        recommendation_dir / f"{TEST_DATE}_sector-rotation.csv",
        index=False,
    )
    pd.DataFrame([{"date": TEST_DATE, "symbol": "510300", "momentum_20": 0.1}]).to_csv(
        factors_dir / "factors.csv",
        index=False,
    )

    destination = runner.copy_recommend_outputs(rec_root, automation_dir, TEST_DATE)

    assert destination == automation_dir / f"recommendation_{TEST_DATE}_sector-rotation.csv"
    assert destination.exists()
    assert not (automation_dir / f"recommendation_{TEST_DATE}_sector-rotation_filters.json").exists()
    assert not (automation_dir / "recommendation_factors.csv").exists()
    assert not (automation_dir / "outputs").exists()
    assert not (automation_dir / "data").exists()


def test_apply_recommendations_use_temporary_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = load_skill_script("utils")
    automation_dir = tmp_path / "automation"
    data_root = tmp_path / "repo-root"
    rec_root = tmp_path / "recommend-workspace"
    automation_dir.mkdir()
    data_root.mkdir()
    (automation_dir / "best.json").write_text(
        """{
  "top_n": 4,
  "fee_rate": 0.0003,
  "sharpe_window": 20,
  "factor_lower_bound": 0.0,
  "corr_window": 100,
  "corr_threshold": 0.9,
  "stop_loss_pct": 0.1
}""",
        encoding="utf-8",
    )
    roots: list[str] = []

    def fake_prepare_recommend_workspace(actual_data_root: Path, actual_automation_dir: Path) -> Path:
        assert actual_data_root == data_root
        assert actual_automation_dir == automation_dir
        rec_root.mkdir()
        return rec_root

    def fake_factor_compute(args: argparse.Namespace) -> None:
        roots.append(args.root)

    def fake_recommend_today(args: argparse.Namespace) -> None:
        roots.append(args.root)
        assert args.universe == str(automation_dir / "selected_universe.csv")
        recommendation_dir = Path(args.root) / "outputs" / "recommendations"
        recommendation_dir.mkdir(parents=True)
        pd.DataFrame([{"date": args.date, "symbol": "510300", "score": 1.0}]).to_csv(
            recommendation_dir / f"{args.date}_sector-rotation.csv",
            index=False,
        )

    monkeypatch.setattr(runner, "prepare_recommend_workspace", fake_prepare_recommend_workspace)
    monkeypatch.setattr(runner, "command_factor_compute", fake_factor_compute)
    monkeypatch.setattr(runner, "command_recommend_today", fake_recommend_today)

    destination = runner.generate_recommendations(
        argparse.Namespace(
            data_root=str(data_root),
            apply=True,
            start="2026-01-01",
        ),
        automation_dir,
        TEST_DATE,
    )

    assert destination == automation_dir / f"recommendation_{TEST_DATE}_sector-rotation.csv"
    assert roots == [str(rec_root), str(rec_root)]
    assert not (data_root / "outputs").exists()


def test_symbol_return_contributions_uses_held_weight_times_next_open_return() -> None:
    runner = load_skill_script("utils")
    daily = pd.DataFrame([
        {"date": pd.Timestamp("2026-06-10"), "symbol": "510300", "open": 10.0},
        {"date": pd.Timestamp("2026-06-10"), "symbol": "510500", "open": 10.0},
        {"date": pd.Timestamp("2026-06-11"), "symbol": "510300", "open": 10.0},
        {"date": pd.Timestamp("2026-06-11"), "symbol": "510500", "open": 10.0},
        {"date": pd.Timestamp("2026-06-12"), "symbol": "510300", "open": 8.0},
        {"date": pd.Timestamp("2026-06-12"), "symbol": "510500", "open": 12.0},
    ])
    selected = pd.DataFrame([
        {"date": pd.Timestamp("2026-06-10"), "symbol": "510300", "target_weight": 0.5},
        {"date": pd.Timestamp("2026-06-10"), "symbol": "510500", "target_weight": 0.5},
    ])

    contributions = runner.symbol_return_contributions(
        daily,
        selected,
        {"510300", "510500"},
    )

    assert contributions["symbol"].tolist() == ["510300", "510500"]
    assert contributions["contribution"].tolist() == pytest.approx([-0.098, 0.098])


def test_pruned_pool_challenge_accepts_only_when_sortino_improves(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_skill_script("utils")
    selected_universe = pd.DataFrame([
        {"symbol": "510300", "name": "沪深300ETF"},
        {"symbol": "510500", "name": "中证500ETF"},
    ])
    monkeypatch.setattr(
        runner,
        "best_strategy_selection",
        lambda *args, **kwargs: pd.DataFrame([{"symbol": "510300"}]),
    )
    monkeypatch.setattr(
        runner,
        "symbol_return_contributions",
        lambda *args, **kwargs: pd.DataFrame([
            {"symbol": "510300", "contribution": -0.2},
            {"symbol": "510500", "contribution": 0.1},
        ]),
    )
    monkeypatch.setattr(
        runner,
        "optimize_pool",
        lambda *args, **kwargs: pd.DataFrame([
            {
                "top_n": 4,
                "fee_rate": 0.0003,
                "sharpe_window": 20,
                "factor_lower_bound": 0.0,
                "corr_window": 100,
                "corr_threshold": 0.9,
                "stop_loss_pct": 0.1,
                "valid": True,
                "sortino": 1.5,
            }
        ]),
    )

    challenge = runner.evaluate_pruned_pool_challenge(
        args=argparse.Namespace(),
        daily=pd.DataFrame(),
        factors=pd.DataFrame(),
        selected_universe=selected_universe,
        current_best={
            "pool_label": "base",
            "sortino": 1.0,
            "fee_rate": 0.0003,
            "top_n": 4,
            "sharpe_window": 20,
            "factor_lower_bound": 0.0,
            "corr_window": 100,
            "corr_threshold": 0.9,
            "stop_loss_pct": 0.1,
        },
        names={"510300": "沪深300ETF", "510500": "中证500ETF"},
        start_date=pd.Timestamp("2023-06-12"),
        end_date=pd.Timestamp(TEST_DATE),
        sharpe_windows=[20],
    )

    assert challenge["accepted"] is True
    assert challenge["removed_symbol"] == "510300"
    assert challenge["universe"]["symbol"].tolist() == ["510500"]
    assert challenge["best"]["pruning_accepted"] is True
    assert challenge["best"]["base_sortino"] == 1.0


def test_write_summary_includes_recommendation_filters(tmp_path: Path) -> None:
    runner = load_skill_script("utils")
    recommendation_path = tmp_path / f"recommendation_{TEST_DATE}_sector-rotation.csv"
    pd.DataFrame([
        {
            "record_type": "recommendation",
            "date": TEST_DATE,
            "symbol": "510300",
            "name": "沪深300ETF",
            "score": 1.0,
            "target_weight": 0.25,
        },
        {
            "record_type": "filtered",
            "date": TEST_DATE,
            "symbol": "510500",
            "name": "中证500ETF",
            "score": 0.8,
            "target_weight": 0.0,
            "filter": "stop_loss",
            "daily_return": -0.12,
            "stop_loss_pct": 0.1,
        },
    ]).to_csv(recommendation_path, index=False)
    (tmp_path / "candidate_selected.json").write_text(
        json.dumps({
            "selected": [{"symbol": "510300"}],
            "candidate_arg": "510300,510500,159915",
            "manual_override": False,
        }),
        encoding="utf-8",
    )
    (tmp_path / "best.json").write_text(json.dumps({"pool_label": "base"}), encoding="utf-8")
    pd.DataFrame([{"pool_label": "base", "top_n": 4}]).to_csv(tmp_path / "evaluations.csv", index=False)
    pd.DataFrame([{"pool_label": "base", "top_n": 4}]).to_csv(tmp_path / "all_results.csv", index=False)

    runner.write_summary(
        argparse.Namespace(
            date=TEST_DATE,
            objective="sortino",
            constraint="drawdown-lt-return",
            apply=False,
            data_root=".",
        ),
        tmp_path,
        recommendation_path,
        TEST_DATE,
    )

    summary = json.loads((tmp_path / "automation_summary.json").read_text(encoding="utf-8"))
    assert summary["recommendations"] == [
        {
            "date": "2026-06-12",
            "symbol": "510300",
            "name": "沪深300ETF",
            "score": 1.0,
            "target_weight": 0.25,
        }
    ]
    assert summary["recommendation_filters"] == [
        {
            "date": "2026-06-12",
            "symbol": "510500",
            "name": "中证500ETF",
            "score": 0.8,
            "target_weight": 0.0,
            "filter": "stop_loss",
            "daily_return": -0.12,
            "stop_loss_pct": 0.1,
        }
    ]


def test_cleanup_intermediate_outputs_keeps_only_summary_recommendation_and_apply_backup(tmp_path: Path) -> None:
    runner = load_skill_script("utils")
    for filename in [
        "automation_summary.json",
        f"recommendation_{TEST_DATE}_sector-rotation.csv",
        "universe_before.csv",
        "candidate_selected.json",
        "candidate_selected.csv",
        "candidate_shortlist.csv",
        "expanded_refresh_universe.csv",
        "all_results.csv",
        "evaluations.csv",
        "best.json",
        "selected_universe.csv",
        "base_universe.csv",
        "add_510300_universe.csv",
    ]:
        (tmp_path / filename).write_text("", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()

    runner.cleanup_intermediate_outputs(tmp_path)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "automation_summary.json",
        f"recommendation_{TEST_DATE}_sector-rotation.csv",
        "universe_before.csv",
    ]


def test_fetch_daily_if_stale_skips_when_latest_trade_date_is_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_data_module, "latest_trade_date_on_or_before", lambda target: TEST_DAY)
    universe = pd.DataFrame([{"symbol": "510300", "name": "沪深300ETF"}])
    existing = pd.DataFrame([
        {
            "date": pd.Timestamp(TEST_DATE),
            "symbol": "510300",
            "name": "沪深300ETF",
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1,
            "amount": 1,
            "turnover": 1,
        }
    ])
    calls = []

    def fetch_one(single: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        calls.append((single, start, end))
        return pd.DataFrame()

    incoming, target_trade_date = fetch_daily_if_stale(
        universe,
        TEST_DAY,
        TEST_END_DAY,
        existing=existing,
        fetch_one=fetch_one,
    )

    assert incoming.empty
    assert target_trade_date == TEST_DAY
    assert calls == []


def test_fetch_daily_if_stale_refreshes_full_capped_window_when_latest_trade_date_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(market_data_module, "latest_trade_date_on_or_before", lambda target: TEST_DAY)
    universe = pd.DataFrame([{"symbol": "510300", "name": "沪深300ETF"}])
    existing = pd.DataFrame([
        {
            "date": pd.Timestamp("2026-06-11"),
            "symbol": "510300",
            "name": "沪深300ETF",
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1,
            "amount": 1,
            "turnover": 1,
        }
    ])
    calls = []

    def fetch_one(single: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        calls.append((start, end))
        return pd.DataFrame([
            {
                "date": pd.Timestamp(TEST_DATE),
                "symbol": "510300",
                "name": "沪深300ETF",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
                "amount": 1,
                "turnover": 1,
            }
        ])

    incoming, target_trade_date = fetch_daily_if_stale(
        universe,
        date(2010, 1, 1),
        TEST_END_DAY,
        existing=existing,
        fetch_one=fetch_one,
    )

    assert not incoming.empty
    assert target_trade_date == TEST_DAY
    assert calls == [(date(2021, 5, 31), TEST_END_DAY)]


def test_fetch_daily_if_stale_refreshes_only_symbols_missing_latest_trade_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(market_data_module, "latest_trade_date_on_or_before", lambda target: TEST_DAY)
    universe = pd.DataFrame([
        {"symbol": "510300", "name": "沪深300ETF"},
        {"symbol": "510500", "name": "中证500ETF"},
    ])
    existing = pd.DataFrame([
        {
            "date": pd.Timestamp(TEST_DATE),
            "symbol": "510500",
            "name": "中证500ETF",
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1,
            "amount": 1,
            "turnover": 1,
        }
    ])
    fetched_symbols = []

    def fetch_one(single: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        fetched_symbols.extend(single["symbol"].astype(str).tolist())
        return pd.DataFrame([
            {
                "date": pd.Timestamp(TEST_DATE),
                "symbol": "510300",
                "name": "沪深300ETF",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
                "amount": 1,
                "turnover": 1,
            }
        ])

    incoming, target_trade_date = fetch_daily_if_stale(
        universe,
        date(2026, 1, 1),
        TEST_END_DAY,
        existing=existing,
        fetch_one=fetch_one,
    )

    assert target_trade_date == TEST_DAY
    assert fetched_symbols == ["510300"]
    assert incoming["symbol"].tolist() == ["510300"]


def test_fetch_daily_if_stale_refuses_partial_market_data_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_data_module, "latest_trade_date_on_or_before", lambda target: TEST_DAY)
    universe = pd.DataFrame([
        {"symbol": "510300", "name": "沪深300ETF"},
        {"symbol": "510500", "name": "中证500ETF"},
    ])

    def fetch_one(single: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame([
            {
                "date": pd.Timestamp(TEST_DATE),
                "symbol": "510300",
                "name": "沪深300ETF",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
                "amount": 1,
                "turnover": 1,
            }
        ])

    with pytest.raises(RuntimeError, match="missing symbols=\\['510500'\\]"):
        fetch_daily_if_stale(
            universe,
            date(2026, 1, 1),
            TEST_END_DAY,
            existing=None,
            fetch_one=fetch_one,
        )
