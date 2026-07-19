#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from quant_core.data.market_data import (  # noqa: E402
    ProjectPaths,
    load_universe,
    read_daily,
    resolve_complete_universe_date,
)
from quant_core.factors import compute_factors  # noqa: E402
from quant_core.strategy.vol_adaptive_residual_sharpe import (  # noqa: E402
    STRATEGY_NAME,
    VolAdaptiveResidualSharpeParams,
    select_vol_adaptive_residual_sharpe,
)


DEFAULTS = VolAdaptiveResidualSharpeParams()
DEFAULT_UNIVERSE = (
    REPO_ROOT
    / ".agents"
    / "skills"
    / "etf-sharpe-topk"
    / "references"
    / "sector_rotation_universe.csv"
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".agents" / "skills" / "etf-vol-adaptive-topk" / "outputs"
OUTPUT_COLUMNS = [
    "record_type",
    "signal_date",
    "holding_for",
    "symbol",
    "name",
    "score",
    "target_weight",
    "risk_off",
    "vol_ratio",
    "vol_ratio_threshold",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a complete ETF and cash target portfolio for the next trading day.",
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--data-root", default=".")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Use local qfq data without calling the market-data update command.",
    )
    parser.add_argument("--top-n", type=int, default=DEFAULTS.top_n)
    parser.add_argument("--sharpe-window", type=int, default=DEFAULTS.sharpe_window)
    parser.add_argument("--factor-lower-bound", type=float, default=DEFAULTS.factor_lower_bound)
    parser.add_argument("--corr-window", type=int, default=DEFAULTS.corr_window)
    parser.add_argument("--corr-threshold", type=float, default=DEFAULTS.corr_threshold)
    parser.add_argument("--stop-loss-pct", type=float, default=DEFAULTS.stop_loss_pct)
    parser.add_argument("--vol-short-window", type=int, default=DEFAULTS.vol_short_window)
    parser.add_argument("--vol-long-window", type=int, default=DEFAULTS.vol_long_window)
    parser.add_argument("--vol-ratio-threshold", type=float, default=DEFAULTS.vol_ratio_threshold)
    parser.add_argument("--risk-off-top-n", type=int, default=DEFAULTS.risk_off_top_n)
    parser.add_argument("--risk-off-gross", type=float, default=DEFAULTS.risk_off_gross)
    parser.add_argument(
        "--residual-sharpe-window",
        type=int,
        default=DEFAULTS.residual_sharpe_window,
    )
    parser.add_argument(
        "--residual-blend-alpha",
        type=float,
        default=DEFAULTS.residual_blend_alpha,
    )
    return parser.parse_args()


def five_year_start(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    try:
        return parsed.replace(year=parsed.year - 5).isoformat()
    except ValueError:
        return parsed.replace(year=parsed.year - 5, day=28).isoformat()


def refresh_data(
    args: argparse.Namespace,
    data_root: Path,
    universe_path: Path,
) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "quant_core.cli",
            "--root",
            str(data_root),
            "data",
            "update",
            "--start",
            five_year_start(args.date),
            "--end",
            args.date,
            "--universe",
            str(universe_path),
            "--universe-name",
            "sector-rotation",
            "--adjust",
            "qfq",
        ],
        cwd=REPO_ROOT,
        check=True,
    )


def build_params(args: argparse.Namespace) -> VolAdaptiveResidualSharpeParams:
    return VolAdaptiveResidualSharpeParams(
        top_n=args.top_n,
        sharpe_window=args.sharpe_window,
        factor_lower_bound=args.factor_lower_bound,
        corr_window=args.corr_window,
        corr_threshold=args.corr_threshold,
        stop_loss_pct=args.stop_loss_pct,
        vol_short_window=args.vol_short_window,
        vol_long_window=args.vol_long_window,
        vol_ratio_threshold=args.vol_ratio_threshold,
        risk_off_top_n=args.risk_off_top_n,
        risk_off_gross=args.risk_off_gross,
        residual_sharpe_window=args.residual_sharpe_window,
        residual_blend_alpha=args.residual_blend_alpha,
    )


def build_output(
    selected: pd.DataFrame,
    signal_date: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, Any]]:
    regimes = selected.attrs.get("risk_regimes", [])
    risk_regime = dict(regimes[-1]) if regimes else {
        "date": signal_date,
        "risk_off": False,
        "vol_ratio": None,
        "target_gross": 0.0,
        "target_cash": 1.0,
    }
    actual_gross = round(
        float(selected["target_weight"].sum()) if not selected.empty else 0.0,
        12,
    )
    risk_regime["actual_target_gross"] = actual_gross
    risk_regime["actual_target_cash"] = round(max(0.0, 1.0 - actual_gross), 12)

    common = {
        "signal_date": signal_date,
        "holding_for": "next_trading_day",
        "risk_off": bool(risk_regime["risk_off"]),
        "vol_ratio": risk_regime.get("vol_ratio"),
        "vol_ratio_threshold": risk_regime.get("vol_ratio_threshold"),
    }
    holding_rows = [
        {
            "record_type": "holding",
            **common,
            "symbol": str(row.symbol),
            "name": str(row.name),
            "score": float(row.score),
            "target_weight": round(float(row.target_weight), 12),
        }
        for row in selected.itertuples(index=False)
    ]
    holding_rows.append({
        "record_type": "cash",
        **common,
        "symbol": "CASH",
        "name": "现金",
        "score": None,
        "target_weight": risk_regime["actual_target_cash"],
    })

    filters = [
        dict(event)
        for event in selected.attrs.get("filter_events", [])
        if event.get("filter") in {"stop_loss", "correlation"}
    ]
    return pd.DataFrame(holding_rows, columns=OUTPUT_COLUMNS), filters, risk_regime


def records_without_nulls(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        records.append({
            key: value
            for key, value in row.items()
            if not (value is None or (not isinstance(value, (list, dict)) and pd.isna(value)))
        })
    return records


def main() -> None:
    args = parse_args()
    data_root = Path(args.data_root).resolve()
    universe_path = Path(args.universe).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not universe_path.is_file():
        raise FileNotFoundError(f"ETF universe does not exist: {universe_path}")
    if not args.skip_refresh:
        refresh_data(args, data_root, universe_path)

    daily = read_daily(ProjectPaths(data_root))
    universe = load_universe(universe_path)
    signal_date = resolve_complete_universe_date(daily, universe, args.date)
    symbols = set(universe["symbol"].astype(str))
    params = build_params(args)
    factors = compute_factors(daily, sharpe_windows=[params.sharpe_window])
    selected = select_vol_adaptive_residual_sharpe(
        factors[factors["symbol"].astype(str).isin(symbols)].copy(),
        params,
        start=pd.Timestamp(signal_date),
        end=pd.Timestamp(signal_date),
        universe_symbols=symbols,
    )
    output, filters, risk_regime = build_output(selected, signal_date)

    output_dir.mkdir(parents=True, exist_ok=True)
    recommendation_path = output_dir / f"recommendation_{signal_date}_sector-rotation.csv"
    output.to_csv(recommendation_path, index=False)
    summary = {
        "strategy": STRATEGY_NAME,
        "requested_date": args.date,
        "signal_date": signal_date,
        "holding_for": "next_trading_day",
        "universe": str(universe_path),
        "parameters": asdict(params),
        "risk_regime": risk_regime,
        "holdings": records_without_nulls(output),
        "filters": filters,
        "recommendation_path": str(recommendation_path),
    }
    summary_path = output_dir / "recommendation_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
