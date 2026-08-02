from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from quant_core.research.contracts import ResearchTask


SHANGHAI = ZoneInfo("Asia/Shanghai")
PRODUCTION_SCHEMA_VERSION = 1
EXECUTION_SEMANTICS = "close-signal/next-open-trade/open-to-open-return/v1"

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
