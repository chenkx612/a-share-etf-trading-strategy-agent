from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from quant_core.backtest.engine import BacktestResult, run_backtest
from quant_core.data.market_data import ProjectPaths, load_universe, read_daily


Selector = Callable[[pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp], pd.DataFrame]
REQUIRED_SELECTION_COLUMNS = {"date", "symbol", "target_weight"}


def evaluate_candidate(
    daily: pd.DataFrame,
    universe: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    selector: Selector,
    *,
    fee_rate: float,
) -> tuple[pd.DataFrame, BacktestResult]:
    history = daily.copy()
    history["date"] = pd.to_datetime(history["date"])
    history["symbol"] = history["symbol"].astype(str)
    symbols = set(universe["symbol"].astype(str))
    history = history[(history["symbol"].isin(symbols)) & (history["date"] <= end)]
    selected = selector(history.copy(), universe.copy(), start, end)
    selected = validate_selection(selected, history, symbols, start, end)
    backtest_daily = history[history["date"].between(start, end)].copy()
    if selected.empty:
        selected.attrs["signal_dates"] = sorted(backtest_daily["date"].unique())
    selected.attrs["universe_symbols"] = sorted(symbols)
    return selected, run_backtest(backtest_daily, selected, fee_rate=fee_rate)


def validate_selection(
    selected: object,
    daily: pd.DataFrame,
    symbols: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    if not isinstance(selected, pd.DataFrame):
        raise ValueError("candidate select() must return a pandas DataFrame")
    missing = REQUIRED_SELECTION_COLUMNS - set(selected.columns)
    if missing:
        raise ValueError(f"candidate selection is missing columns: {', '.join(sorted(missing))}")
    result = selected.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    result["symbol"] = result["symbol"].astype(str)
    result["target_weight"] = pd.to_numeric(result["target_weight"], errors="raise")
    if result.duplicated(["date", "symbol"]).any():
        raise ValueError("candidate selection contains duplicate date/symbol rows")
    if not result["date"].between(start, end).all():
        raise ValueError("candidate selection contains dates outside the evaluation period")
    trading_dates = set(pd.to_datetime(daily["date"]))
    if not set(result["date"]).issubset(trading_dates):
        raise ValueError("candidate selection contains non-trading dates")
    if not set(result["symbol"]).issubset(symbols):
        raise ValueError("candidate selection contains symbols outside the universe")
    weights = result["target_weight"].astype(float)
    if not np.isfinite(weights).all() or (weights < 0.0).any():
        raise ValueError("candidate weights must be finite and non-negative")
    if (result.groupby("date")["target_weight"].sum() > 1.0 + 1e-9).any():
        raise ValueError("candidate weights must sum to at most one on each date")
    return result.sort_values(["date", "symbol"]).reset_index(drop=True)


def _selector(module_name: str) -> Selector:
    module = importlib.import_module(module_name)
    selector = getattr(module, "select", None)
    if not callable(selector):
        raise ValueError(f"{module_name} must define callable select(daily, universe, start, end)")
    return selector


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--universe", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-module", default="quant_core.strategy.research_candidate")
    parser.add_argument("--fee-rate", type=float, default=0.001)
    args = parser.parse_args()

    paths = ProjectPaths(Path(args.root))
    paths.ensure()
    selected, result = evaluate_candidate(
        read_daily(paths),
        load_universe(Path(args.universe)),
        pd.Timestamp(args.start),
        pd.Timestamp(args.end),
        _selector(args.candidate_module),
        fee_rate=args.fee_rate,
    )
    run_dir = paths.outputs / "backtests" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    selected.to_csv(run_dir / "orders.csv", index=False)
    result.positions.to_csv(run_dir / "positions.csv", index=False)
    result.daily_returns.to_csv(run_dir / "daily_returns.csv", index=False)
    result.equity_curve.to_csv(run_dir / "equity_curve.csv", index=False)
    (run_dir / "metrics.json").write_text(json.dumps(result.metrics, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
