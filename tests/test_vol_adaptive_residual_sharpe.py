from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_core.strategy.vol_adaptive_residual_sharpe import (
    VolAdaptiveResidualSharpeParams,
    select_vol_adaptive_residual_sharpe,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_skill_script(filename: str):
    path = (
        Path(__file__).resolve().parents[1]
        / ".agents"
        / "skills"
        / "etf-vol-adaptive-topk"
        / "scripts"
        / filename
    )
    module_name = filename.removesuffix(".py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vol_adaptive_scripts_share_repository_universe_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    research = load_skill_script("run_research_cycle.py")
    recommendation = load_skill_script("recommend_next_holdings.py")

    expected = REPO_ROOT / "universes" / "sector_rotation.csv"
    assert research.DEFAULT_UNIVERSE == expected
    assert recommendation.DEFAULT_UNIVERSE == expected

    custom_universe = tmp_path / "custom.csv"
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_research_cycle.py", "--universe", str(custom_universe)],
    )
    assert research.parse_args().universe == str(custom_universe)
    monkeypatch.setattr(
        sys,
        "argv",
        ["recommend_next_holdings.py", "--universe", str(custom_universe)],
    )
    assert recommendation.parse_args().universe == str(custom_universe)


def test_vol_adaptive_apply_updates_universe_and_dry_run_does_not(
    tmp_path: Path,
) -> None:
    research = load_skill_script("run_research_cycle.py")
    universe_path = tmp_path / "sector_rotation.csv"
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    before = pd.DataFrame([{"symbol": "A", "name": "ETF A", "fund_size": 1.0}])
    after = pd.DataFrame([{"symbol": "B", "name": "ETF B", "fund_size": 2.0}])
    before.to_csv(universe_path, index=False)

    assert research.apply_universe_update(
        apply=False,
        universe_changed=True,
        universe_path=universe_path,
        selected_pool=after,
        output_dir=output_dir,
    ) is None
    pd.testing.assert_frame_equal(pd.read_csv(universe_path), before)
    assert not (output_dir / "universe_before.csv").exists()

    backup = research.apply_universe_update(
        apply=True,
        universe_changed=True,
        universe_path=universe_path,
        selected_pool=after,
        output_dir=output_dir,
    )

    assert backup == output_dir / "universe_before.csv"
    pd.testing.assert_frame_equal(pd.read_csv(universe_path), after)
    pd.testing.assert_frame_equal(pd.read_csv(backup), before)


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
    script = load_skill_script("recommend_next_holdings.py")
    selected = pd.DataFrame([
        {
            "date": pd.Timestamp("2026-07-17"),
            "symbol": symbol,
            "name": name,
            "score": score,
            "target_weight": 0.4 / 3.0,
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
        "target_gross": 0.4,
        "target_cash": 0.6,
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
    assert output.loc[output["record_type"] == "holding", "target_weight"].tolist() == [
        0.133333333333,
        0.133333333333,
        0.133333333334,
    ]
    assert output.loc[output["record_type"] == "cash", "target_weight"].item() == 0.6
    assert output["target_weight"].sum() == 1.0
    assert filters[0]["filter"] == "correlation"
    assert risk_regime["actual_target_gross"] == 0.4


def test_weekly_research_uses_bounded_two_stage_grids() -> None:
    script = load_skill_script("run_research_cycle.py")

    ranking = script.ranking_grid()
    assert len(ranking) == 54
    assert all(params.residual_sharpe_window == params.sharpe_window for params in ranking)

    risk = script.risk_grid(ranking[0])
    assert len(risk) <= 19
    assert all(params.risk_off_top_n <= params.top_n for params in risk)


def test_research_challenge_uses_recent_window_improvement() -> None:
    script = load_skill_script("run_research_cycle.py")
    candidate = {
        "valid": True,
        "research_window": {"sortino": 1.2},
    }

    assert script.passes_challenge(
        candidate,
        baseline={"sortino": 1.0},
        minimum_improvement=0.05,
    )
    assert not script.passes_challenge(
        candidate,
        baseline={"sortino": 1.18},
        minimum_improvement=0.05,
    )


def test_research_challenge_rejects_invalid_result() -> None:
    script = load_skill_script("run_research_cycle.py")
    candidate = {
        "valid": False,
        "research_window": {"sortino": 2.0},
    }

    assert not script.passes_challenge(
        candidate,
        baseline={"sortino": 1.0},
        minimum_improvement=0.05,
    )


def test_daily_recommendation_rejects_mismatched_promoted_universe(tmp_path: Path) -> None:
    script = load_skill_script("recommend_next_holdings.py")
    universe = tmp_path / "universe.csv"
    universe.write_text("symbol,name\nA,ETF A\n", encoding="utf-8")
    state = {"universe_sha256": script.file_sha256(universe)}

    assert script.verify_universe_state(state, universe)

    universe.write_text("symbol,name\nB,ETF B\n", encoding="utf-8")
    with pytest.raises(ValueError, match="do not match"):
        script.verify_universe_state(state, universe)


def test_research_dry_run_is_a_proposal_not_an_applied_change() -> None:
    script = load_skill_script("run_research_cycle.py")

    assert script.promotion_status(changed=True, apply=False) == "proposed"
    assert script.promotion_status(changed=True, apply=True) == "applied"
    assert script.promotion_status(changed=False, apply=True) == "unchanged"


def test_candidate_pool_preserves_reviewed_candidate_metadata() -> None:
    script = load_skill_script("run_research_cycle.py")
    base = pd.DataFrame([{"symbol": "A", "name": "ETF A", "fund_size": 1.0}])

    result = script.candidate_pool(
        base,
        "B",
        names={"B": "Fallback"},
        metadata={"B": {"name": "Reviewed ETF", "fund_size": 12.5}},
    )

    candidate = result.loc[result["symbol"] == "B"].iloc[0]
    assert candidate["name"] == "Reviewed ETF"
    assert candidate["fund_size"] == 12.5


def test_proposal_selection_considers_every_candidate_that_passes_threshold() -> None:
    script = load_skill_script("run_research_cycle.py")
    pool = pd.DataFrame([{"symbol": "A"}])
    failed_candidate = {
        "valid": True,
        "research_window": {"sortino": 0.9},
    }
    passed_candidate = {
        "valid": True,
        "research_window": {"sortino": 1.2},
    }

    selected = script.choose_best_proposal(
        [
            (failed_candidate, pool, "failed"),
            (passed_candidate, pool, "passed"),
        ],
        baseline={"sortino": 1.0},
        minimum_improvement=0.05,
    )

    assert selected is not None
    assert selected[0] is passed_candidate


def test_best_proposal_compares_parameter_refresh_with_addition() -> None:
    script = load_skill_script("run_research_cycle.py")
    base_pool = pd.DataFrame([{"symbol": "A"}])
    added_pool = pd.DataFrame([{"symbol": "A"}, {"symbol": "B"}])
    parameter_refresh = {"valid": True, "research_window": {"sortino": 1.3}}
    addition = {"valid": True, "research_window": {"sortino": 1.2}}

    selected = script.choose_best_proposal(
        [
            (addition, added_pool, "addition"),
            (parameter_refresh, base_pool, "parameter refresh"),
        ],
        baseline={"sortino": 1.0},
        minimum_improvement=0.05,
    )

    assert selected is not None
    assert selected[0] is parameter_refresh
    assert selected[2] == "parameter refresh"


def test_research_json_writer_replaces_nan_with_null(tmp_path: Path) -> None:
    script = load_skill_script("run_research_cycle.py")
    path = tmp_path / "result.json"

    script.write_json_atomic(path, {"missing": float("nan")})
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: pytest.fail(f"invalid JSON constant: {value}"),
    )

    assert payload == {"missing": None}


def test_benchmark_curve_uses_open_prices_and_reports_drawdown() -> None:
    script = load_skill_script("run_research_cycle.py")
    dates = pd.bdate_range("2026-01-02", periods=3)
    daily = pd.DataFrame({
        "date": dates,
        "symbol": ["510300"] * 3,
        "open": [100.0, 110.0, 99.0],
    })

    curve, metrics = script.benchmark_equity_curve(
        daily,
        "510300",
        dates[0],
        dates[-1],
    )

    assert curve["equity"].tolist() == [1.0, 1.1, 0.99]
    assert metrics["max_drawdown"] == pytest.approx(-0.1)


def test_equity_curve_chart_is_written_as_png(tmp_path: Path) -> None:
    script = load_skill_script("run_research_cycle.py")
    dates = pd.bdate_range("2026-01-02", periods=3)
    strategy_curve = pd.DataFrame({
        "date": dates,
        "equity": [1.0, 1.1, 1.05],
    })
    benchmark_curve = pd.DataFrame({
        "date": dates,
        "equity": [1.0, 1.02, 1.01],
    })
    path = tmp_path / "equity_curve.png"

    script.write_equity_curve_chart(
        strategy_curve,
        benchmark_curve,
        {"annual_return": 0.2, "max_drawdown": -0.05},
        {"annual_return": 0.08, "max_drawdown": -0.02},
        "CSI 300 ETF (510300)",
        12,
        path,
    )

    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_research_cleanup_keeps_only_auditable_outputs(tmp_path: Path) -> None:
    script = load_skill_script("run_research_cycle.py")
    keep = [
        "research_summary.json",
        "grid_results.csv",
        "universe_before.csv",
        "params_before.json",
    ]
    remove = list(script.DISPOSABLE_RESEARCH_FILES) + ["equity_curve_2026-07-17.png"]
    for name in keep + remove:
        (tmp_path / name).write_text("test", encoding="utf-8")

    script.remove_disposable_research_files(tmp_path)

    assert all((tmp_path / name).exists() for name in keep)
    assert all(not (tmp_path / name).exists() for name in remove)


def test_compact_grid_evaluation_does_not_repeat_pool_symbols() -> None:
    script = load_skill_script("run_research_cycle.py")
    evaluation = {
        "pool_label": "add_B",
        "pool_size": 2,
        "symbols": ["A", "B"],
        "parameters": {"top_n": 1},
        "valid": True,
        "research_window": {
            "annual_return": 0.2,
            "max_drawdown": -0.1,
            "sortino": 1.5,
        },
    }

    compact = script.compact_evaluation(evaluation)

    assert "symbols" not in compact
    assert compact["metrics"]["annual_return"] == 0.2
