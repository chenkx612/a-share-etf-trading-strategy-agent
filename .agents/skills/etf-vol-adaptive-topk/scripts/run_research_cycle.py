#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from quant_core.backtest.engine import compute_metrics, run_backtest  # noqa: E402
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


SKILL_ROOT = REPO_ROOT / ".agents" / "skills" / "etf-vol-adaptive-topk"
DEFAULT_OUTPUT_DIR = SKILL_ROOT / "outputs" / "research"
DEFAULT_PARAMS_FILE = SKILL_ROOT / "references" / "accepted_params.json"
DEFAULT_BENCHMARK = SKILL_ROOT / "references" / "csi300_benchmark.csv"
DEFAULT_UNIVERSE = (
    REPO_ROOT
    / ".agents"
    / "skills"
    / "etf-sharpe-topk"
    / "references"
    / "sector_rotation_universe.csv"
)
DEFAULTS = VolAdaptiveResidualSharpeParams()
DISPOSABLE_RESEARCH_FILES = {
    "best_params.json",
    "candidate_selected.csv",
    "candidate_selected.json",
    "candidate_shortlist.csv",
    "evaluations.json",
    "expanded_refresh_universe.csv",
    "selected_universe.csv",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a weekly pool/parameter challenge or an abnormal-market parameter-only challenge.",
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--mode", choices=["weekly", "abnormal"], default="weekly")
    parser.add_argument("--candidates", help="Exactly three reviewed ETF symbols for weekly mode.")
    parser.add_argument("--data-root", default=".")
    parser.add_argument("--root", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--params-file", default=str(DEFAULT_PARAMS_FILE))
    parser.add_argument("--benchmark-universe", default=str(DEFAULT_BENCHMARK))
    parser.add_argument("--lookback-months", type=int, default=12)
    parser.add_argument("--minimum-improvement", type=float, default=0.05)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(json_safe(payload), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def write_csv_atomic(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        frame.to_csv(handle, index=False)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def remove_disposable_research_files(output_dir: Path) -> None:
    for name in DISPOSABLE_RESEARCH_FILES:
        (output_dir / name).unlink(missing_ok=True)
    for path in output_dir.glob("equity_curve_*.png"):
        path.unlink()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_candidates(value: str | None, output_dir: Path) -> list[str]:
    if value:
        symbols = [part.strip() for part in value.split(",") if part.strip()]
    else:
        selected_path = output_dir / "candidate_selected.json"
        if not selected_path.is_file():
            raise FileNotFoundError(
                f"Missing reviewed candidates: pass --candidates or prepare {selected_path}"
            )
        payload = json.loads(selected_path.read_text(encoding="utf-8"))
        symbols = [str(symbol) for symbol in payload["candidate_symbols"]]
    if len(symbols) != 3 or len(set(symbols)) != 3:
        raise ValueError("Weekly mode requires exactly three distinct reviewed ETF symbols")
    return symbols


def reviewed_candidate_records(
    symbols: list[str],
    output_dir: Path,
    names: dict[str, str],
) -> list[dict[str, Any]]:
    selected_path = output_dir / "candidate_selected.json"
    if selected_path.is_file():
        payload = json.loads(selected_path.read_text(encoding="utf-8"))
        selected = payload.get("selected", [])
        by_symbol = {
            str(row["symbol"]): row
            for row in selected
            if isinstance(row, dict) and "symbol" in row
        }
        if all(symbol in by_symbol for symbol in symbols):
            return [
                {
                    **by_symbol[symbol],
                    "symbol": symbol,
                    "manual_override": bool(payload.get("manual_override", False)),
                }
                for symbol in symbols
            ]
    return [
        {
            "symbol": symbol,
            "name": names.get(symbol, symbol),
            "fund_size": None,
            "manual_override": True,
        }
        for symbol in symbols
    ]


def load_params_state(path: Path) -> tuple[VolAdaptiveResidualSharpeParams, dict[str, Any] | None]:
    if not path.is_file():
        return DEFAULTS, None
    payload = json.loads(path.read_text(encoding="utf-8"))
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError(f"Invalid accepted parameter state: {path}")
    return VolAdaptiveResidualSharpeParams(**parameters), payload


def metrics_valid(metrics: dict[str, float]) -> bool:
    return (
        bool(metrics)
        and abs(float(metrics.get("max_drawdown", -1.0)))
        < float(metrics.get("annual_return", 0.0))
    )


def metric_value(metrics: dict[str, float], name: str = "sortino") -> float:
    value = metrics.get(name)
    return float(value) if value is not None and pd.notna(value) else float("-inf")


def compact_metrics(metrics: dict[str, float]) -> dict[str, float]:
    return {
        key: float(metrics[key])
        for key in ["annual_return", "max_drawdown", "sortino"]
    }


def compact_evaluation(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "pool_label": evaluation["pool_label"],
        "pool_size": evaluation["pool_size"],
        "parameters": evaluation["parameters"],
        "valid": evaluation["valid"],
        "metrics": compact_metrics(evaluation["research_window"]),
    }


def log_step(message: str) -> None:
    print(f"[etf-vol-research] {message}", flush=True)


def run_params_backtest(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    symbols: set[str],
    params: VolAdaptiveResidualSharpeParams,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> Any:
    selected = select_vol_adaptive_residual_sharpe(
        factors[factors["symbol"].astype(str).isin(symbols)].copy(),
        params,
        start=start,
        end=end,
        universe_symbols=symbols,
    )
    window_daily = daily[
        (pd.to_datetime(daily["date"]) >= start)
        & (pd.to_datetime(daily["date"]) <= end)
        & daily["symbol"].astype(str).isin(symbols)
    ].copy()
    return run_backtest(window_daily, selected)


def evaluate_params(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    symbols: set[str],
    params: VolAdaptiveResidualSharpeParams,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, float]:
    metrics = run_params_backtest(
        daily,
        factors,
        symbols,
        params,
        start,
        end,
    ).metrics
    if not metrics:
        raise ValueError(
            f"No backtest metrics for {start.date().isoformat()} to {end.date().isoformat()}"
        )
    return metrics


def benchmark_equity_curve(
    daily: pd.DataFrame,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, float]]:
    frame = daily[
        (daily["symbol"].astype(str) == symbol)
        & (pd.to_datetime(daily["date"]) >= start)
        & (pd.to_datetime(daily["date"]) <= end)
    ][["date", "open"]].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").dropna(subset=["open"]).drop_duplicates("date")
    if frame.empty:
        raise ValueError(f"Missing benchmark data for {symbol} in the research window")
    frame["equity"] = frame["open"].astype(float) / float(frame["open"].iloc[0])
    returns = pd.DataFrame({
        "net_return": frame["equity"].pct_change().fillna(0.0),
        "turnover": 0.0,
    })
    return frame[["date", "equity"]].reset_index(drop=True), compute_metrics(returns)


def write_equity_curve_chart(
    strategy_curve: pd.DataFrame,
    benchmark_curve: pd.DataFrame,
    strategy_metrics: dict[str, float],
    benchmark_metrics: dict[str, float],
    benchmark_label: str,
    lookback_months: int,
    path: Path,
) -> None:
    cache_dir = Path(tempfile.gettempdir()) / "quant-matplotlib"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(11, 6))
    axis.plot(
        pd.to_datetime(strategy_curve["date"]),
        strategy_curve["equity"].astype(float),
        label="Strategy",
        linewidth=2.0,
    )
    axis.plot(
        pd.to_datetime(benchmark_curve["date"]),
        benchmark_curve["equity"].astype(float),
        label=benchmark_label,
        linewidth=1.6,
    )
    axis.axhline(1.0, color="#888888", linewidth=0.8, linestyle="--")
    axis.set_title(f"Recent {lookback_months}-Month Equity Curve")
    axis.set_xlabel("Date")
    axis.set_ylabel("Cumulative Net Value")
    axis.grid(alpha=0.25)
    axis.legend(loc="upper left")
    annotation = (
        "Strategy  "
        f"Annualized Return: {strategy_metrics['annual_return']:.2%}  "
        f"Max Drawdown: {strategy_metrics['max_drawdown']:.2%}\n"
        f"{benchmark_label}  "
        f"Annualized Return: {benchmark_metrics['annual_return']:.2%}  "
        f"Max Drawdown: {benchmark_metrics['max_drawdown']:.2%}"
    )
    axis.text(
        0.01,
        0.01,
        annotation,
        transform=axis.transAxes,
        fontsize=9,
        verticalalignment="bottom",
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.85},
    )
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".png",
        dir=path.parent,
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
    try:
        figure.savefig(temp_path, dpi=160, format="png")
        os.replace(temp_path, path)
    finally:
        plt.close(figure)
        temp_path.unlink(missing_ok=True)


def unique_params(
    candidates: Iterable[VolAdaptiveResidualSharpeParams],
) -> list[VolAdaptiveResidualSharpeParams]:
    seen: set[tuple[tuple[str, Any], ...]] = set()
    result = []
    for params in candidates:
        key = tuple(sorted(asdict(params).items()))
        if key not in seen:
            seen.add(key)
            result.append(params)
    return result


def ranking_grid() -> list[VolAdaptiveResidualSharpeParams]:
    return unique_params(
        VolAdaptiveResidualSharpeParams(
            top_n=top_n,
            sharpe_window=sharpe_window,
            factor_lower_bound=lower_bound,
            corr_window=DEFAULTS.corr_window,
            corr_threshold=DEFAULTS.corr_threshold,
            stop_loss_pct=DEFAULTS.stop_loss_pct,
            vol_short_window=DEFAULTS.vol_short_window,
            vol_long_window=DEFAULTS.vol_long_window,
            vol_ratio_threshold=DEFAULTS.vol_ratio_threshold,
            risk_off_top_n=min(DEFAULTS.risk_off_top_n, top_n),
            risk_off_gross=DEFAULTS.risk_off_gross,
            residual_sharpe_window=sharpe_window,
            residual_blend_alpha=alpha,
        )
        for top_n in [4, 5, 6]
        for sharpe_window in [20, 25, 30]
        for lower_bound in [0.0, 0.5]
        for alpha in [0.15, 0.27, 0.4]
    )


def risk_grid(seed: VolAdaptiveResidualSharpeParams) -> list[VolAdaptiveResidualSharpeParams]:
    base = asdict(seed)
    candidates = [seed]
    for ratio in [1.2, 1.3, 1.4]:
        for risk_top_n in [2, 3]:
            if risk_top_n > seed.top_n:
                continue
            for gross in [0.4, 0.6, 0.8]:
                candidates.append(VolAdaptiveResidualSharpeParams(
                    **{
                        **base,
                        "vol_ratio_threshold": ratio,
                        "risk_off_top_n": risk_top_n,
                        "risk_off_gross": gross,
                    }
                ))
    return unique_params(candidates)


def optimize_params(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    symbols: set[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[VolAdaptiveResidualSharpeParams, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []

    def run_grid(grid: list[VolAdaptiveResidualSharpeParams], stage: str) -> None:
        for params in grid:
            metrics = evaluate_params(daily, factors, symbols, params, start, end)
            rows.append({
                "stage": stage,
                **asdict(params),
                "valid": metrics_valid(metrics),
                **metrics,
            })

    run_grid(ranking_grid(), "ranking")
    ranking_rows = [row for row in rows if row["stage"] == "ranking"]
    ranking_rows.sort(
        key=lambda row: (bool(row["valid"]), metric_value(row)),
        reverse=True,
    )
    seed = VolAdaptiveResidualSharpeParams(
        **{key: ranking_rows[0][key] for key in asdict(DEFAULTS)}
    )
    run_grid(risk_grid(seed), "risk")
    rows.sort(
        key=lambda row: (bool(row["valid"]), metric_value(row)),
        reverse=True,
    )
    best = VolAdaptiveResidualSharpeParams(
        **{key: rows[0][key] for key in asdict(DEFAULTS)}
    )
    return best, rows


def candidate_pool(
    base: pd.DataFrame,
    candidate: str | None,
    names: dict[str, str],
    metadata: dict[str, dict[str, Any]] | None = None,
) -> pd.DataFrame:
    result = base.copy()
    result["symbol"] = result["symbol"].astype(str)
    if candidate is None or candidate in set(result["symbol"]):
        return result.reset_index(drop=True)
    return pd.concat([
        result,
        pd.DataFrame([{
            "symbol": candidate,
            "name": (metadata or {}).get(candidate, {}).get(
                "name", names.get(candidate, candidate)
            ),
            "fund_size": (metadata or {}).get(candidate, {}).get("fund_size", pd.NA),
        }]),
    ], ignore_index=True)


def latest_names(daily: pd.DataFrame) -> dict[str, str]:
    frame = daily.sort_values("date").dropna(subset=["name"]).drop_duplicates("symbol", keep="last")
    return frame.set_index(frame["symbol"].astype(str))["name"].astype(str).to_dict()


def evaluate_pool(
    label: str,
    pool: pd.DataFrame,
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = time.monotonic()
    log_step(f"START {label}")
    symbols = set(pool["symbol"].astype(str))
    params, grid_rows = optimize_params(daily, factors, symbols, start, end)
    metrics = evaluate_params(daily, factors, symbols, params, start, end)
    evaluation = {
        "pool_label": label,
        "pool_size": len(pool),
        "symbols": sorted(symbols),
        "parameters": asdict(params),
        "research_window": metrics,
        "valid": metrics_valid(metrics),
    }
    log_step(
        f"END {label} ({time.monotonic() - started:.1f}s, "
        f"sortino={metric_value(metrics):.4f})"
    )
    return evaluation, [{"pool_label": label, **row} for row in grid_rows]


def return_contributions(
    daily: pd.DataFrame,
    factors: pd.DataFrame,
    pool: pd.DataFrame,
    params: VolAdaptiveResidualSharpeParams,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    symbols = set(pool["symbol"].astype(str))
    selected = select_vol_adaptive_residual_sharpe(
        factors[factors["symbol"].astype(str).isin(symbols)].copy(),
        params,
        start=start,
        end=end,
        universe_symbols=symbols,
    )
    window_daily = daily[
        (pd.to_datetime(daily["date"]) >= start)
        & (pd.to_datetime(daily["date"]) <= end)
        & daily["symbol"].astype(str).isin(symbols)
    ].copy()
    result = run_backtest(window_daily, selected)
    if result.positions.empty:
        return pd.DataFrame({
            "symbol": sorted(symbols),
            "contribution": [0.0] * len(symbols),
        })
    opens = window_daily.pivot(index="date", columns="symbol", values="open").sort_index().ffill()
    forward = (opens.shift(-1) / opens - 1.0).stack().rename("forward_return").reset_index()
    positions = result.positions.copy()
    positions["symbol"] = positions["symbol"].astype(str)
    positions["date"] = pd.to_datetime(positions["date"])
    forward["date"] = pd.to_datetime(forward["date"])
    merged = positions.merge(forward, on=["date", "symbol"], how="left")
    merged["contribution"] = (
        merged["weight"].astype(float)
        * merged["forward_return"].fillna(0.0).astype(float)
    )
    contribution = merged.groupby("symbol", as_index=False)["contribution"].sum()
    return (
        pd.DataFrame({"symbol": sorted(symbols)})
        .merge(contribution, on="symbol", how="left")
        .fillna({"contribution": 0.0})
        .sort_values(["contribution", "symbol"])
        .reset_index(drop=True)
    )


def passes_challenge(
    candidate: dict[str, Any],
    baseline: dict[str, float],
    minimum_improvement: float,
) -> bool:
    return (
        candidate["valid"]
        and metric_value(candidate["research_window"])
        >= metric_value(baseline) + minimum_improvement
    )


def choose_best_proposal(
    proposals: list[tuple[dict[str, Any], pd.DataFrame, str]],
    baseline: dict[str, float],
    minimum_improvement: float,
) -> tuple[dict[str, Any], pd.DataFrame, str] | None:
    passing = [
        item
        for item in proposals
        if passes_challenge(
            item[0],
            baseline,
            minimum_improvement,
        )
    ]
    if not passing:
        return None
    return max(
        passing,
        key=lambda item: metric_value(item[0]["research_window"]),
    )


def promotion_status(changed: bool, apply: bool) -> str:
    if not changed:
        return "unchanged"
    return "applied" if apply else "proposed"


def main() -> None:
    args = parse_args()
    if args.lookback_months <= 0:
        raise ValueError("Lookback months must be positive")
    if args.minimum_improvement < 0:
        raise ValueError("Minimum improvement must be non-negative")

    output_dir = Path(args.root).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    data_root = Path(args.data_root).resolve()
    universe_path = Path(args.universe).resolve()
    params_path = Path(args.params_file).resolve()
    benchmark_path = Path(args.benchmark_universe).resolve()
    daily = read_daily(ProjectPaths(data_root))
    daily["date"] = pd.to_datetime(daily["date"])
    daily["symbol"] = daily["symbol"].astype(str)
    base = load_universe(universe_path)
    base["symbol"] = base["symbol"].astype(str)
    benchmark = load_universe(benchmark_path)
    if len(benchmark) != 1:
        raise ValueError("Benchmark universe must contain exactly one symbol")
    benchmark_symbol = str(benchmark.iloc[0]["symbol"])
    benchmark_name = str(benchmark.iloc[0]["name"])
    names = latest_names(daily)
    candidates = parse_candidates(args.candidates, output_dir) if args.mode == "weekly" else []
    reviewed_candidates = reviewed_candidate_records(candidates, output_dir, names)
    candidate_metadata = {
        str(record["symbol"]): record for record in reviewed_candidates
    }
    required_symbols = list(base["symbol"]) + candidates + [benchmark_symbol]
    required_universe = pd.DataFrame({"symbol": required_symbols})
    research_date = resolve_complete_universe_date(daily, required_universe, args.date)
    end = pd.Timestamp(research_date)
    start = end - pd.DateOffset(months=args.lookback_months) + pd.Timedelta(days=1)
    log_step(
        f"mode={args.mode} research={start.date()}..{end.date()} "
        f"lookback_months={args.lookback_months}"
    )

    accepted_params, accepted_state = load_params_state(params_path)
    sharpe_windows = sorted({20, 25, 30, accepted_params.sharpe_window})
    factors = compute_factors(daily, sharpe_windows=sharpe_windows)
    current_symbols = set(base["symbol"])
    current_metrics = evaluate_params(
        daily, factors, current_symbols, accepted_params, start, end
    )

    evaluations: list[dict[str, Any]] = []
    all_grid_rows: list[dict[str, Any]] = []
    base_evaluation, grid_rows = evaluate_pool(
        "base", base, daily, factors, start, end
    )
    evaluations.append(base_evaluation)
    all_grid_rows.extend(grid_rows)

    selected_pool = base
    selected_evaluation = base_evaluation
    accepted_reason = "kept current pool and parameters"
    proposals: list[tuple[dict[str, Any], pd.DataFrame, str]] = []
    if asdict(accepted_params) != base_evaluation["parameters"]:
        proposals.append((base_evaluation, base, "accepted refreshed parameters"))
    if args.mode == "weekly":
        for symbol in candidates:
            pool = candidate_pool(base, symbol, names, candidate_metadata)
            evaluation, grid_rows = evaluate_pool(
                f"add_{symbol}",
                pool,
                daily,
                factors,
                start,
                end,
            )
            evaluations.append(evaluation)
            proposals.append((
                evaluation,
                pool,
                f"accepted {evaluation['pool_label']}",
            ))
            all_grid_rows.extend(grid_rows)
    best_proposal = choose_best_proposal(
        proposals,
        current_metrics,
        args.minimum_improvement,
    )
    if best_proposal is not None:
        selected_evaluation, selected_pool, accepted_reason = best_proposal
    else:
        selected_evaluation = {
            "pool_label": "current",
            "pool_size": len(base),
            "symbols": sorted(current_symbols),
            "parameters": asdict(accepted_params),
            "research_window": current_metrics,
            "valid": metrics_valid(current_metrics),
        }
        selected_pool = base
        evaluations.append(selected_evaluation)

    pruning: dict[str, Any] = {"evaluated": False}
    if args.mode == "weekly" and len(selected_pool) > 1:
        selected_params = VolAdaptiveResidualSharpeParams(
            **selected_evaluation["parameters"]
        )
        contributions = return_contributions(
            daily,
            factors,
            selected_pool,
            selected_params,
            start,
            end,
        )
        worst = contributions.iloc[0]
        removed_symbol = str(worst["symbol"])
        pruned_pool = selected_pool[
            selected_pool["symbol"].astype(str) != removed_symbol
        ].reset_index(drop=True)
        pruned_evaluation, grid_rows = evaluate_pool(
            f"remove_{removed_symbol}",
            pruned_pool,
            daily,
            factors,
            start,
            end,
        )
        evaluations.append(pruned_evaluation)
        all_grid_rows.extend(grid_rows)
        prune_accepted = passes_challenge(
            pruned_evaluation,
            selected_evaluation["research_window"],
            args.minimum_improvement,
        )
        pruning = {
            "evaluated": True,
            "removed_symbol": removed_symbol,
            "removed_name": names.get(removed_symbol, removed_symbol),
            "removed_contribution": float(worst["contribution"]),
            "accepted": prune_accepted,
            "before": compact_metrics(selected_evaluation["research_window"]),
            "after": compact_metrics(pruned_evaluation["research_window"]),
        }
        if prune_accepted:
            selected_pool = pruned_pool
            selected_evaluation = pruned_evaluation
            accepted_reason += f"; pruned {removed_symbol}"

    best_params = selected_evaluation["parameters"]
    selected_params = VolAdaptiveResidualSharpeParams(**best_params)
    final_result = run_params_backtest(
        daily,
        factors,
        set(selected_pool["symbol"].astype(str)),
        selected_params,
        start,
        end,
    )
    if final_result.equity_curve.empty:
        raise ValueError("Selected strategy produced no equity curve")
    benchmark_curve, benchmark_metrics = benchmark_equity_curve(
        daily,
        benchmark_symbol,
        start,
        end,
    )
    equity_curve_path = output_dir.parent / f"equity_curve_{research_date}.png"
    benchmark_label = f"CSI 300 ETF ({benchmark_symbol})"
    write_equity_curve_chart(
        final_result.equity_curve,
        benchmark_curve,
        final_result.metrics,
        benchmark_metrics,
        benchmark_label,
        args.lookback_months,
        equity_curve_path,
    )
    equity_curve_summary = {
        "path": str(equity_curve_path),
        "benchmark_symbol": benchmark_symbol,
        "benchmark_name": benchmark_name,
        "strategy": {
            "annual_return": final_result.metrics["annual_return"],
            "max_drawdown": final_result.metrics["max_drawdown"],
        },
        "benchmark": {
            "annual_return": benchmark_metrics["annual_return"],
            "max_drawdown": benchmark_metrics["max_drawdown"],
        },
    }
    universe_changed = (
        selected_pool["symbol"].astype(str).tolist()
        != base["symbol"].astype(str).tolist()
    )
    parameters_changed = asdict(accepted_params) != best_params
    proposal_changed = universe_changed or parameters_changed
    status = promotion_status(proposal_changed, bool(args.apply))
    public_decision = (
        f"proposed: {accepted_reason}" if status == "proposed" else accepted_reason
    )
    grid_results_path = output_dir / "grid_results.csv"
    addition_challenges = [
        {
            "pool_label": evaluation["pool_label"],
            "added_symbol": str(evaluation["pool_label"]).removeprefix("add_"),
            "added_name": names.get(
                str(evaluation["pool_label"]).removeprefix("add_"),
                str(evaluation["pool_label"]).removeprefix("add_"),
            ),
            "passed_minimum_improvement": passes_challenge(
                evaluation,
                current_metrics,
                args.minimum_improvement,
            ),
            "included_in_final_pool": (
                str(evaluation["pool_label"]).removeprefix("add_")
                in set(selected_pool["symbol"].astype(str))
            ),
        }
        for evaluation in evaluations
        if str(evaluation["pool_label"]).startswith("add_")
    ]
    grid_best_by_pool = [
        compact_evaluation(evaluation)
        for evaluation in evaluations
        if evaluation["pool_label"] != "current"
    ]
    summary = {
        "strategy": STRATEGY_NAME,
        "mode": args.mode,
        "requested_date": args.date,
        "research_date": research_date,
        "windows": {
            "research_start": start.date().isoformat(),
            "research_end": end.date().isoformat(),
            "lookback_months": args.lookback_months,
        },
        "minimum_improvement": args.minimum_improvement,
        "proposal_changed": proposal_changed,
        "universe_changed": universe_changed,
        "parameters_changed": parameters_changed,
        "promotion_status": status,
        "decision": public_decision,
        "apply": bool(args.apply),
        "reviewed_candidates": reviewed_candidates,
        "addition_challenges": addition_challenges,
        "current_research_window": current_metrics,
        "selected": selected_evaluation,
        "pruning_challenge": pruning,
        "grid_search": {
            "path": str(grid_results_path),
            "objective": "sortino",
            "rows": len(all_grid_rows),
            "best_by_pool": grid_best_by_pool,
        },
        "equity_curve": equity_curve_summary,
    }
    write_csv_atomic(grid_results_path, pd.DataFrame(all_grid_rows))

    if args.apply and proposal_changed:
        previous_universe_hash = file_sha256(universe_path)
        if universe_changed:
            shutil.copy2(universe_path, output_dir / "universe_before.csv")
        if params_path.exists():
            shutil.copy2(params_path, output_dir / "params_before.json")
        if universe_changed:
            write_csv_atomic(universe_path, selected_pool)
        promoted_universe_hash = file_sha256(universe_path)
        write_json_atomic(params_path, {
            "strategy": STRATEGY_NAME,
            "accepted_date": research_date,
            "mode": args.mode,
            "parameters": best_params,
            "universe_sha256": promoted_universe_hash,
            "acceptance": {
                "decision": accepted_reason,
                "windows": summary["windows"],
                "minimum_improvement": args.minimum_improvement,
                "previous_universe_sha256": previous_universe_hash,
                "previous_parameters": asdict(accepted_params),
                "previous_state_accepted_date": (
                    accepted_state.get("accepted_date") if accepted_state else None
                ),
                "reviewed_candidates": reviewed_candidates,
                "selected": selected_evaluation,
                "pruning_challenge": pruning,
                "benchmark": equity_curve_summary["benchmark"],
            },
        })

    write_json_atomic(output_dir / "research_summary.json", summary)
    remove_disposable_research_files(output_dir)
    print(json.dumps(json_safe(summary), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
