from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import pandas as pd

from quant_core import schedule as schedule_policy
from quant_core.backtest.engine import run_backtest
from quant_core.config import BacktestConfig
from quant_core.data.market_data import load_universe
from quant_core.recommendation.models import (
    EXECUTION_SEMANTICS,
    PRODUCTION_SCHEMA_VERSION,
    ProductionContext,
    StrategyDataRequirements,
)
from quant_core.research.contracts import ResearchTask


REQUIRED_STRATEGY_FUNCTIONS = ("parameter_grid", "select_with_params")

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
    parameter_selection = task.parameter_selection
    assert parameter_selection is not None
    if task.strategy_module is None:
        raise ValueError("production task must define task.strategy")

    strategy_path = root_path / task.strategy_path
    research_dir = root_path / ".research" / task.task_id
    champion_path = research_dir / "champion.py"
    metadata_path = research_dir / "champion.json"
    if not strategy_path.is_file() or not champion_path.is_file() or not metadata_path.is_file():
        raise RuntimeError(
            "Production requires a synchronized Champion; run the managed Loop and "
            "resolve any reported production synchronization conflict first"
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
            "resolve the managed Loop production synchronization conflict before recommending"
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
    maximum = int(parameter_selection["max_parameter_sets"])
    grid = _load_grid(module, maximum)
    configured_requirements = production.get("data_requirements")
    requirements = _load_requirements(
        module,
        configured_requirements
        if isinstance(configured_requirements, Mapping)
        else None,
    )

    universe_path = root_path / task.universe_path
    universe = load_universe(universe_path)
    if universe.empty or "symbol" not in universe or universe["symbol"].duplicated().any():
        raise ValueError("production universe must contain unique symbol rows")
    universe["symbol"] = universe["symbol"].map(_normalize_symbol)

    backtest_source = Path(inspect.getfile(run_backtest)).read_bytes()
    production_source = b"".join(
        path.read_bytes()
        for path in sorted(Path(__file__).parent.glob("*.py"))
    )
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
        "production_policy": _sha256_json({
            **dict(parameter_selection),
            **dict(production),
        }),
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
