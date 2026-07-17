from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestConfig:
    fee_rate: float = 0.001
    initial_capital: float = 100_000.0
    lot_size: int = 100
