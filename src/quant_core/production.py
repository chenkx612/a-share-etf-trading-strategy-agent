from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quant_core import schedule as schedule_policy
from quant_core.backtest.engine import compute_metrics, run_backtest
from quant_core.config import BacktestConfig
from quant_core.data.market_data import (
    AkshareMarketDataClient,
    ProjectPaths,
    fetch_daily_if_stale,
    load_universe,
    read_daily,
    replace_symbol_history,
    validate_daily,
    write_table,
)
from quant_core.research.contracts import ResearchTask
from quant_core.research.evaluator import evaluate_candidate, validate_selection
from quant_core.research.workspace import write_json_atomic
from quant_core.schedule import (
    is_schedule_boundary,
    latest_schedule_boundary,
    schedule_bucket,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
PRODUCTION_SCHEMA_VERSION = 1
EXECUTION_SEMANTICS = "close-signal/next-open-trade/open-to-open-return/v1"
REQUIRED_STRATEGY_FUNCTIONS = ("parameter_grid", "select_with_params")


class ParameterSearchError(RuntimeError):
    def __init__(self, message: str, rows: Sequence[Mapping[str, object]]) -> None:
        super().__init__(message)
        self.rows = tuple(rows)


@dataclass(frozen=True)
class StrategyDataRequirements:
    required_columns: tuple[str, ...]
    min_history: int


@dataclass(frozen=True)
class ProductionContext:
    root: Path
    task_path: Path
    task: ResearchTask
    strategy: ModuleType
    universe: pd.DataFrame
    grid: tuple[dict[str, object], ...]
    requirements: StrategyDataRequirements
    hashes: Mapping[str, str]
    champion: Mapping[str, Any]


@dataclass(frozen=True)
class SearchResult:
    signal_date: pd.Timestamp
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    parameters: Mapping[str, object]
    metrics: Mapping[str, float]
    rows: tuple[Mapping[str, object], ...]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _normalize_symbol(value: object) -> str:
    symbol = str(value)
    return symbol.zfill(6) if symbol.isdigit() else symbol


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        frame.to_csv(temporary_path, index=False)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_grid(module: ModuleType, maximum: int) -> tuple[dict[str, object], ...]:
    grid = module.parameter_grid()
    if not isinstance(grid, (list, tuple)) or not grid:
        raise ValueError("production strategy parameter_grid() must return a non-empty list")
    if len(grid) > maximum:
        raise ValueError(
            f"production strategy parameter_grid() has {len(grid)} entries; maximum is {maximum}"
        )
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in grid:
        if not isinstance(entry, dict):
            raise ValueError("production strategy parameter_grid() entries must be dictionaries")
        try:
            encoded = _canonical_json(entry)
        except (TypeError, ValueError) as exc:
            raise ValueError("production parameter values must be JSON serializable") from exc
        if encoded in seen:
            raise ValueError("production strategy parameter_grid() entries must be unique")
        seen.add(encoded)
        result.append(dict(entry))
    return tuple(result)


def _load_requirements(
    module: ModuleType,
    configured: Mapping[str, object] | None = None,
) -> StrategyDataRequirements:
    provider = getattr(module, "data_requirements", None)
    if configured is not None:
        value = dict(configured)
    elif callable(provider):
        value = provider()
    else:
        raise ValueError(
            "production data requirements must be declared by strategy "
            "data_requirements() or task.production.data_requirements"
        )
    if not isinstance(value, dict) or set(value) != {"required_columns", "min_history"}:
        raise ValueError(
            "data_requirements() must return exactly required_columns and min_history"
        )
    columns = value["required_columns"]
    if (
        not isinstance(columns, (list, tuple))
        or not columns
        or not all(isinstance(column, str) and column for column in columns)
        or len(set(columns)) != len(columns)
    ):
        raise ValueError("data_requirements().required_columns must be unique strings")
    min_history = value["min_history"]
    if not isinstance(min_history, int) or isinstance(min_history, bool) or min_history < 1:
        raise ValueError("data_requirements().min_history must be a positive integer")
    mandatory = {"date", "symbol", "open", "close"}
    if not mandatory.issubset(columns):
        raise ValueError(
            "data_requirements().required_columns must include date, symbol, open, and close"
        )
    return StrategyDataRequirements(tuple(columns), min_history)


def load_production_context(root: str | Path, task_path: str | Path) -> ProductionContext:
    root_path = Path(root).resolve()
    task_file = Path(task_path).resolve()
    task = ResearchTask.load(task_file)
    production = task.production
    if production is None:
        raise ValueError(f"task {task.task_id!r} does not define [production]")
    if task.strategy_module is None:
        raise ValueError("production task must define task.strategy.module")

    strategy_path = root_path / task.strategy_path
    research_dir = root_path / ".research" / task.task_id
    champion_path = research_dir / "champion.py"
    metadata_path = research_dir / "champion.json"
    if not strategy_path.is_file() or not champion_path.is_file() or not metadata_path.is_file():
        raise RuntimeError(
            "Production requires a synchronized Champion; run the managed Loop and "
            "synchronize the promoted strategy first"
        )
    champion = json.loads(metadata_path.read_text(encoding="utf-8"))
    strategy_hash = _sha256_file(strategy_path)
    champion_hash = _sha256_file(champion_path)
    if (
        champion.get("task_id") != task.task_id
        or champion.get("strategy_path") != task.strategy_path
        or champion.get("champion_sha256") != champion_hash
        or strategy_hash != champion_hash
    ):
        raise RuntimeError(
            "Production strategy, Champion code, and Champion metadata hashes do not match; "
            "synchronize Champion before recommending"
        )

    importlib.invalidate_caches()
    module = importlib.import_module(task.strategy_module)
    module_path = Path(inspect.getfile(module)).resolve()
    if module_path != strategy_path.resolve():
        raise RuntimeError(
            f"production module resolved to {module_path}, expected {strategy_path.resolve()}"
        )
    for name in REQUIRED_STRATEGY_FUNCTIONS:
        if not callable(getattr(module, name, None)):
            raise ValueError(f"production strategy must define callable {name}()")
    maximum = int(production["max_parameter_sets"])
    grid = _load_grid(module, maximum)
    configured_requirements = production.get("data_requirements")
    requirements = _load_requirements(
        module,
        configured_requirements
        if isinstance(configured_requirements, Mapping)
        else None,
    )

    universe_path = root_path / str(task.raw["data"]["universe"])
    universe = load_universe(universe_path)
    if universe.empty or "symbol" not in universe or universe["symbol"].duplicated().any():
        raise ValueError("production universe must contain unique symbol rows")
    universe["symbol"] = universe["symbol"].map(_normalize_symbol)

    backtest_source = Path(inspect.getfile(run_backtest)).read_bytes()
    production_source = Path(__file__).read_bytes()
    schedule_source = Path(inspect.getfile(schedule_policy)).read_bytes()
    backtest_contract = _sha256_bytes(
        backtest_source
        + _canonical_json(BacktestConfig().__dict__).encode("utf-8")
        + EXECUTION_SEMANTICS.encode("utf-8")
    )
    hashes = {
        "task": _sha256_file(task_file),
        "champion": champion_hash,
        "strategy": strategy_hash,
        "universe": _sha256_file(universe_path),
        "production_policy": _sha256_json(production),
        "parameter_grid": _sha256_json(grid),
        "backtest_contract": backtest_contract,
        "execution_contract": _sha256_bytes(
            production_source
            + schedule_source
            + backtest_contract.encode("ascii")
            + PRODUCTION_SCHEMA_VERSION.to_bytes(4, "big")
        ),
    }
    return ProductionContext(
        root=root_path,
        task_path=task_file,
        task=task,
        strategy=module,
        universe=universe,
        grid=grid,
        requirements=requirements,
        hashes=hashes,
        champion=champion,
    )


def _passes_constraints(
    metrics: Mapping[str, float], constraints: Mapping[str, Mapping[str, object]]
) -> bool:
    for name, rule in constraints.items():
        value = metrics.get(name)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return False
        threshold = float(rule["threshold"])
        operator = rule["operator"]
        if operator == ">=" and value < threshold:
            return False
        if operator == "<=" and value > threshold:
            return False
        if operator == "abs<=" and abs(value) > threshold:
            return False
    return True


def search_parameters(
    context: ProductionContext,
    daily: pd.DataFrame,
    signal_date: pd.Timestamp,
) -> SearchResult:
    production = context.task.production
    assert production is not None
    signal = pd.Timestamp(signal_date).normalize()
    train_start = signal - pd.DateOffset(months=int(production["train_months"]))
    rows: list[dict[str, object]] = []
    feasible: list[tuple[float, str, dict[str, object], dict[str, float]]] = []
    objective = str(production["objective"])
    constraints = production["constraints"]
    assert isinstance(constraints, Mapping)
    for params in context.grid:
        _, result = evaluate_candidate(
            daily[daily["date"] <= signal].copy(),
            context.universe.copy(),
            train_start,
            signal,
            lambda d, u, s, e, p=params: context.strategy.select_with_params(d, u, s, e, p),
        )
        metrics = {key: float(value) for key, value in result.metrics.items()}
        valid = _passes_constraints(metrics, constraints)
        encoded = _canonical_json(params)
        rows.append({"parameters": dict(params), "metrics": metrics, "valid": valid})
        value = metrics.get(objective)
        if valid and value is not None and math.isfinite(value):
            feasible.append((float(value), encoded, dict(params), metrics))
    if not feasible:
        raise ParameterSearchError(
            f"parameter search at {signal.date()} found no set satisfying production constraints",
            rows,
        )
    feasible.sort(key=lambda item: (-item[0], item[1]))
    _, _, parameters, metrics = feasible[0]
    return SearchResult(
        signal_date=signal,
        train_start=train_start,
        train_end=signal,
        parameters=parameters,
        metrics=metrics,
        rows=tuple(rows),
    )


def _input_hashes_match(payload: Mapping[str, object], hashes: Mapping[str, str]) -> bool:
    value = payload.get("input_hashes")
    return isinstance(value, dict) and value == dict(hashes)


def _parameter_store(context: ProductionContext) -> Path:
    return (
        context.root
        / ".cache"
        / "production"
        / context.task.task_id
        / "parameters"
    )


def _legacy_parameter_store(context: ProductionContext) -> Path:
    return (
        context.root
        / "outputs"
        / "production"
        / context.task.task_id
        / "parameters"
    )


def _recommendation_output_dir(context: ProductionContext) -> Path:
    return context.root / "outputs" / context.task.task_id


def _freeze_path(
    context: ProductionContext,
    signal_date: pd.Timestamp,
    trading_dates: Sequence[pd.Timestamp],
) -> Path:
    production = context.task.production
    assert production is not None
    bucket = schedule_bucket(signal_date, production["schedule"], trading_dates)
    safe_bucket = bucket.replace(":", "-")
    return _parameter_store(context) / f"{safe_bucket}.json"


def _freeze_search(
    context: ProductionContext,
    result: SearchResult,
    trading_dates: Sequence[pd.Timestamp],
) -> Path:
    path = _freeze_path(context, result.signal_date, trading_dates)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PRODUCTION_SCHEMA_VERSION,
        "task_id": context.task.task_id,
        "searched_on": result.signal_date.date().isoformat(),
        "train_start": result.train_start.date().isoformat(),
        "train_end": result.train_end.date().isoformat(),
        "parameters": result.parameters,
        "metrics": result.metrics,
        "objective": context.task.production["objective"] if context.task.production else None,
        "input_hashes": dict(context.hashes),
        "search_rows": result.rows,
    }
    write_json_atomic(path, payload)
    return path


def _valid_freeze(
    path: Path,
    hashes: Mapping[str, str],
    *,
    on_or_before: pd.Timestamp | None = None,
) -> Mapping[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    if not _input_hashes_match(payload, hashes):
        return None
    if on_or_before is not None:
        try:
            searched_on = pd.Timestamp(payload["searched_on"]).normalize()
        except (KeyError, TypeError, ValueError):
            return None
        if searched_on > pd.Timestamp(on_or_before).normalize():
            return None
    return payload


def _validate_daily_requirements(
    daily: pd.DataFrame,
    context: ProductionContext,
    signal_date: pd.Timestamp,
) -> pd.DataFrame:
    missing = set(context.requirements.required_columns) - set(daily.columns)
    if missing:
        raise ValueError(f"market data is missing strategy-required columns: {sorted(missing)}")
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["symbol"] = frame["symbol"].map(_normalize_symbol)
    frame = frame[frame["date"] <= signal_date].sort_values(["date", "symbol"])
    universe_symbols = set(context.universe["symbol"])
    signal_rows = frame[
        (frame["date"] == signal_date) & frame["symbol"].isin(universe_symbols)
    ]
    valid_signal_rows = signal_rows
    for column in ("open", "close"):
        values = pd.to_numeric(valid_signal_rows[column], errors="coerce")
        valid_signal_rows = valid_signal_rows[np.isfinite(values) & (values > 0.0)]
    if valid_signal_rows.empty:
        raise RuntimeError(
            f"production universe has no valid open/close data on signal date "
            f"{signal_date.date()}"
        )
    counts = (
        frame[frame["symbol"].isin(universe_symbols)]
        .groupby("symbol")["date"]
        .nunique()
    )
    if counts.empty or int(counts.max()) < context.requirements.min_history:
        raise RuntimeError(
            "market data does not meet the strategy's declared minimum history requirement"
        )
    return frame


def _refresh_data(
    context: ProductionContext,
    requested: date,
    *,
    skip_refresh: bool,
) -> pd.DataFrame:
    paths = ProjectPaths(context.root)
    try:
        existing = read_daily(paths)
    except FileNotFoundError:
        existing = pd.DataFrame()
    if skip_refresh:
        if existing.empty:
            raise FileNotFoundError("offline recommendation requires local market data")
        return existing

    production = context.task.production
    assert production is not None
    benchmark = _normalize_symbol(production["benchmark"])
    refresh_universe = context.universe.copy()
    if benchmark not in set(refresh_universe["symbol"]):
        refresh_universe = pd.concat(
            [
                refresh_universe,
                pd.DataFrame([{"symbol": benchmark, "name": benchmark}]),
            ],
            ignore_index=True,
        )
    history_years = math.ceil(
        (int(production["train_months"]) + int(production["curve_months"])) / 12
    ) + 1
    try:
        start = requested.replace(year=requested.year - history_years)
    except ValueError:
        start = requested.replace(year=requested.year - history_years, day=28)
    client = AkshareMarketDataClient()
    incoming, _ = fetch_daily_if_stale(
        refresh_universe,
        start,
        requested,
        existing=existing if not existing.empty else None,
        fetch_one=client.fetch_daily,
        log=print,
    )
    if incoming.empty:
        return existing
    merged = replace_symbol_history(existing if not existing.empty else None, incoming)
    problems = validate_daily(merged)
    if problems:
        raise RuntimeError(f"refreshed market data is invalid: {problems}")
    write_table(merged, paths.data_daily)
    return merged


def resolve_signal_date(
    daily: pd.DataFrame,
    requested: date,
    *,
    now: datetime | None = None,
) -> pd.Timestamp:
    dates = pd.DatetimeIndex(pd.to_datetime(daily["date"]).dt.normalize().unique()).sort_values()
    current = now or datetime.now(SHANGHAI)
    cutoff = pd.Timestamp(requested)
    if requested >= current.date() and (current.hour, current.minute) < (15, 0):
        cutoff = pd.Timestamp(current.date()) - pd.Timedelta(days=1)
    eligible = dates[dates <= cutoff]
    if eligible.empty:
        raise RuntimeError(f"no closed trading date is available on or before {requested}")
    return pd.Timestamp(eligible[-1])


def _next_trade_date(signal: pd.Timestamp, all_dates: Sequence[pd.Timestamp]) -> pd.Timestamp:
    dates = pd.DatetimeIndex(pd.to_datetime(all_dates)).sort_values().unique()
    later = dates[dates > signal]
    if len(later):
        return pd.Timestamp(later[0])
    try:
        import akshare as ak

        calendar = ak.tool_trade_date_hist_sina()
        column = "trade_date" if "trade_date" in calendar.columns else calendar.columns[0]
        official = pd.DatetimeIndex(pd.to_datetime(calendar[column])).sort_values()
        later = official[official > signal]
        if len(later):
            return pd.Timestamp(later[0])
    except Exception:
        pass
    candidate = signal + pd.Timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += pd.Timedelta(days=1)
    return candidate


def _target_holdings(
    context: ProductionContext,
    daily: pd.DataFrame,
    schedule_boundary: pd.Timestamp,
    signal_date: pd.Timestamp,
    trade_date: pd.Timestamp,
    parameters: Mapping[str, object],
) -> pd.DataFrame:
    raw = context.strategy.select_with_params(
        daily[daily["date"] <= signal_date].copy(),
        context.universe.copy(),
        schedule_boundary,
        signal_date,
        dict(parameters),
    )
    selected = validate_selection(
        raw,
        daily,
        set(context.universe["symbol"]),
        schedule_boundary,
        signal_date,
    )
    selected = selected[selected["date"] == signal_date].reset_index(drop=True)
    total = float(selected["target_weight"].sum()) if not selected.empty else 0.0
    if total > 1.0 + 1e-9:
        raise ValueError("target ETF weights exceed one")
    rows: list[dict[str, object]] = []
    for row in selected.itertuples(index=False):
        rows.append(
            {
                "record_type": "etf",
                "signal_date": signal_date.date().isoformat(),
                "trade_date": trade_date.date().isoformat(),
                "symbol": str(row.symbol),
                "name": str(getattr(row, "name", row.symbol)),
                "score": getattr(row, "score", None),
                "rank": getattr(row, "rank", None),
                "target_weight": float(row.target_weight),
            }
        )
    rows.append(
        {
            "record_type": "cash",
            "signal_date": signal_date.date().isoformat(),
            "trade_date": trade_date.date().isoformat(),
            "symbol": "CASH",
            "name": "现金",
            "score": None,
            "rank": None,
            "target_weight": max(0.0, 1.0 - total),
        }
    )
    output = pd.DataFrame(rows)
    if (
        not np.isfinite(output["target_weight"]).all()
        or (output["target_weight"] < 0).any()
        or not math.isclose(float(output["target_weight"].sum()), 1.0, abs_tol=1e-9)
    ):
        raise ValueError("ETF and cash target weights must be non-negative and sum to one")
    return output


def _historical_boundaries(
    dates: pd.DatetimeIndex,
    schedule: Mapping[str, object],
    curve_start: pd.Timestamp,
) -> list[pd.Timestamp]:
    if dates.empty:
        return []
    boundaries = [
        pd.Timestamp(value)
        for value in dates
        if is_schedule_boundary(pd.Timestamp(value), schedule, dates)
    ]
    anchors = [value for value in boundaries if value <= curve_start]
    if not anchors:
        raise RuntimeError(
            "insufficient market data to find the last parameter-search date "
            "before the causal curve window"
        )
    anchor = max(anchors)
    return [anchor, *(value for value in boundaries if value > curve_start)]


def next_schedule_boundary(
    signal_date: pd.Timestamp,
    schedule: Mapping[str, object],
    trading_dates: Sequence[pd.Timestamp],
) -> pd.Timestamp:
    signal = pd.Timestamp(signal_date).normalize()
    known = pd.DatetimeIndex(pd.to_datetime(trading_dates)).normalize()
    calendar = schedule_policy.exchange_trade_dates()
    if calendar:
        official = pd.DatetimeIndex(calendar)
        tail_start = max(signal, pd.Timestamp(official[-1])) + pd.Timedelta(days=1)
        future = official.union(pd.bdate_range(tail_start, periods=800))
    else:
        future = pd.bdate_range(signal + pd.Timedelta(days=1), periods=800)
    dates = known.union(future).sort_values().unique()
    for candidate in dates[dates > signal]:
        value = pd.Timestamp(candidate)
        if is_schedule_boundary(value, schedule, dates):
            return value
    raise RuntimeError("unable to resolve the next parameter-search date")


def causal_replay(
    context: ProductionContext,
    daily: pd.DataFrame,
    signal_date: pd.Timestamp,
) -> tuple[pd.DataFrame, Mapping[str, float], list[Mapping[str, object]]]:
    production = context.task.production
    assert production is not None
    curve_start = signal_date - pd.DateOffset(months=int(production["curve_months"]))
    symbols = set(context.universe["symbol"])
    strategy_daily = daily[daily["symbol"].isin(symbols)].copy()
    curve_dates = pd.DatetimeIndex(
        strategy_daily.loc[
            strategy_daily["date"].between(curve_start, signal_date), "date"
        ].unique()
    ).sort_values()
    if len(curve_dates) < 2:
        raise RuntimeError("insufficient market data for the configured causal curve window")
    all_strategy_dates = pd.DatetimeIndex(strategy_daily["date"].unique()).sort_values()
    boundaries = _historical_boundaries(
        all_strategy_dates[all_strategy_dates <= signal_date],
        production["schedule"],
        pd.Timestamp(curve_dates[0]),
    )
    selections: list[pd.DataFrame] = []
    audits: list[Mapping[str, object]] = []
    for index, boundary in enumerate(boundaries):
        result = search_parameters(context, daily[daily["date"] <= boundary], boundary)
        segment_start = boundary
        segment_end = (
            boundaries[index + 1] - pd.Timedelta(days=1)
            if index + 1 < len(boundaries)
            else signal_date
        )
        selected = context.strategy.select_with_params(
            daily[daily["date"] <= segment_end].copy(),
            context.universe.copy(),
            segment_start,
            segment_end,
            dict(result.parameters),
        )
        selected = validate_selection(
            selected,
            daily,
            symbols,
            segment_start,
            segment_end,
        )
        selections.append(selected)
        audits.append(
            {
                "boundary": boundary.date().isoformat(),
                "train_start": result.train_start.date().isoformat(),
                "train_end": result.train_end.date().isoformat(),
                "parameters": result.parameters,
                "metrics": result.metrics,
            }
        )
    combined = pd.concat(selections, ignore_index=True) if selections else pd.DataFrame()
    replay_start = boundaries[0]
    replay_dates = pd.DatetimeIndex(
        strategy_daily.loc[
            strategy_daily["date"].between(replay_start, signal_date), "date"
        ].unique()
    ).sort_values()
    combined.attrs["signal_dates"] = list(replay_dates)
    combined.attrs["universe_symbols"] = sorted(symbols)
    backtest_daily = strategy_daily[
        strategy_daily["date"].between(replay_start, signal_date)
    ].copy()
    result = run_backtest(backtest_daily, combined)
    if result.daily_returns.empty:
        raise RuntimeError("causal strategy replay produced no daily returns")
    strategy_returns = (
        result.daily_returns[
            result.daily_returns["date"].between(curve_dates[0], signal_date)
        ]
        .set_index("date")["net_return"]
        .astype(float)
    )

    benchmark = _normalize_symbol(production["benchmark"])
    benchmark_frame = (
        daily[
            (daily["symbol"] == benchmark)
            & daily["date"].between(curve_dates[0], signal_date)
        ][["date", "open"]]
        .drop_duplicates("date", keep="last")
        .sort_values("date")
        .set_index("date")
    )
    benchmark_open = pd.to_numeric(benchmark_frame["open"], errors="coerce")
    benchmark_returns = benchmark_open.shift(-1) / benchmark_open - 1.0
    aligned = pd.DataFrame(
        {
            "strategy_return": strategy_returns,
            "benchmark_return": benchmark_returns,
        }
    ).dropna()
    if aligned.empty or not aligned.index.equals(strategy_returns.index[:-1]):
        raise RuntimeError("benchmark history is incomplete or not aligned with the strategy curve")
    aligned["strategy_equity"] = (1.0 + aligned["strategy_return"]).cumprod()
    aligned["benchmark_equity"] = (1.0 + aligned["benchmark_return"]).cumprod()
    aligned = aligned.reset_index()
    strategy_metrics = compute_metrics(
        pd.DataFrame(
            {
                "net_return": aligned["strategy_return"],
                "turnover": result.daily_returns.set_index("date")
                .reindex(pd.DatetimeIndex(aligned["date"]))["turnover"]
                .to_numpy(),
            }
        )
    )
    benchmark_metrics = compute_metrics(
        pd.DataFrame(
            {
                "net_return": aligned["benchmark_return"],
                "turnover": 0.0,
            }
        )
    )
    metrics = {
        "strategy_annual_return": strategy_metrics["annual_return"],
        "strategy_max_drawdown": strategy_metrics["max_drawdown"],
        "benchmark_annual_return": benchmark_metrics["annual_return"],
        "benchmark_max_drawdown": benchmark_metrics["max_drawdown"],
    }
    return aligned, metrics, audits


def _write_curve_png(
    curve: pd.DataFrame, metrics: Mapping[str, float], path: Path, benchmark: str
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(
        curve["date"],
        curve["strategy_equity"],
        label=(
            f"Strategy ann. {metrics['strategy_annual_return']:.1%}, "
            f"MDD {metrics['strategy_max_drawdown']:.1%}"
        ),
    )
    axis.plot(
        curve["date"],
        curve["benchmark_equity"],
        label=(
            f"{benchmark} ann. {metrics['benchmark_annual_return']:.1%}, "
            f"MDD {metrics['benchmark_max_drawdown']:.1%}"
        ),
    )
    axis.set_title("Strictly causal production replay")
    axis.set_ylabel("Cumulative net value")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        figure.savefig(temporary_path, format="png", dpi=150)
        temporary_path.replace(path)
    finally:
        plt.close(figure)
        temporary_path.unlink(missing_ok=True)


def run_recommendation(
    root: str | Path,
    task_path: str | Path,
    *,
    requested_date: date | None = None,
    skip_refresh: bool = False,
) -> Path:
    context: ProductionContext | None = None
    signal_date: pd.Timestamp | None = None
    try:
        context = load_production_context(root, task_path)
        requested = requested_date or datetime.now(SHANGHAI).date()
        daily_raw = _refresh_data(context, requested, skip_refresh=skip_refresh)
        normalized_symbols = daily_raw["symbol"].map(_normalize_symbol)
        universe_raw = daily_raw[
            normalized_symbols.isin(set(context.universe["symbol"]))
        ].copy()
        signal_date = resolve_signal_date(universe_raw, requested)
        daily = _validate_daily_requirements(daily_raw, context, signal_date)
        universe_daily = daily[daily["symbol"].isin(set(context.universe["symbol"]))]
        all_dates = pd.DatetimeIndex(universe_raw["date"].unique()).sort_values()
        visible_dates = pd.DatetimeIndex(universe_daily["date"].unique()).sort_values()
        production = context.task.production
        assert production is not None

        schedule_boundary = latest_schedule_boundary(
            signal_date,
            production["schedule"],
            visible_dates,
        )
        expected_path = _freeze_path(context, schedule_boundary, visible_dates)
        legacy_expected_path = (
            _legacy_parameter_store(context) / expected_path.name
        )
        expected_freeze = None
        for candidate_path in (expected_path, legacy_expected_path):
            candidate = _valid_freeze(
                candidate_path,
                context.hashes,
                on_or_before=signal_date,
            )
            if candidate is not None:
                expected_path = candidate_path
                expected_freeze = candidate
                break
        if expected_freeze is not None:
            freeze_path = expected_path
            freeze_payload = expected_freeze
            parameters = expected_freeze["parameters"]
            search_status = "reused"
        else:
            search = search_parameters(
                context,
                daily[daily["date"] <= schedule_boundary],
                schedule_boundary,
            )
            freeze_path = _freeze_search(context, search, visible_dates)
            freeze_payload = {
                "searched_on": search.signal_date.date().isoformat(),
                "parameters": search.parameters,
            }
            parameters = search.parameters
            search_status = "searched"

        trade_date = _next_trade_date(signal_date, all_dates)
        last_tuning_date = pd.Timestamp(freeze_payload["searched_on"]).normalize()
        next_tuning_date = next_schedule_boundary(
            signal_date,
            production["schedule"],
            visible_dates,
        )
        holdings = _target_holdings(
            context,
            daily,
            schedule_boundary,
            signal_date,
            trade_date,
            parameters,
        )
        curve, curve_metrics, replay_audit = causal_replay(context, daily, signal_date)

        output_dir = _recommendation_output_dir(context)
        recommendation_path = output_dir / "recommendation.csv"
        curve_path = output_dir / "causal_curve.csv"
        png_path = output_dir / "causal_curve.png"
        search_path = output_dir / "parameter_search.json"
        _atomic_csv(holdings, recommendation_path)
        _atomic_csv(curve, curve_path)
        _write_curve_png(curve, curve_metrics, png_path, str(production["benchmark"]))
        write_json_atomic(
            search_path,
            {
                "schema_version": PRODUCTION_SCHEMA_VERSION,
                "status": search_status,
                "freeze_path": str(freeze_path.relative_to(context.root)),
                "parameters": parameters,
                "causal_replay_searches": replay_audit,
            },
        )
        summary_path = output_dir / "summary.json"
        write_json_atomic(
            summary_path,
            {
                "schema_version": PRODUCTION_SCHEMA_VERSION,
                "status": "completed",
                "task_id": context.task.task_id,
                "task_path": str(context.task_path.relative_to(context.root)),
                "signal_date": signal_date.date().isoformat(),
                "trade_date": trade_date.date().isoformat(),
                "search_status": search_status,
                "parameter_train_months": int(production["train_months"]),
                "parameter_schedule": dict(production["schedule"]),
                "last_tuning_date": last_tuning_date.date().isoformat(),
                "next_tuning_date": next_tuning_date.date().isoformat(),
                "parameters": parameters,
                "champion_number": context.champion.get("champion_number"),
                "champion_round_id": context.champion.get("champion_round_id"),
                "input_hashes": dict(context.hashes),
                "execution_semantics": EXECUTION_SEMANTICS,
                "curve_metrics": curve_metrics,
                "benchmark": str(production["benchmark"]),
                "survivorship_bias": (
                    "历史重放使用当前股票池快照，存在幸存者偏差；该曲线是严格因果研究重放，"
                    "不代表真实历史实盘净值。"
                ),
                "recommendation_path": str(recommendation_path.relative_to(context.root)),
                "curve_csv_path": str(curve_path.relative_to(context.root)),
                "curve_png_path": str(png_path.relative_to(context.root)),
                "parameter_search_path": str(search_path.relative_to(context.root)),
            },
        )
        return summary_path
    except Exception as exc:
        root_path = Path(root).resolve()
        task_id = context.task.task_id if context is not None else Path(task_path).stem
        failure_dir = root_path / ".cache" / "production" / task_id / "failures"
        failure_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
        write_json_atomic(
            failure_dir / f"{timestamp}.json",
            {
                "schema_version": PRODUCTION_SCHEMA_VERSION,
                "status": "failed",
                "task_id": task_id,
                "signal_date": (
                    signal_date.date().isoformat() if signal_date is not None else None
                ),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "input_hashes": dict(context.hashes) if context is not None else None,
                "search_rows": (
                    list(exc.rows) if isinstance(exc, ParameterSearchError) else None
                ),
            },
        )
        raise
