from __future__ import annotations

from dataclasses import dataclass


STRATEGY_NAME = "sharpe-corr-threshold"

SHARPE_CORR_THRESHOLD_TOP_N = 5
SHARPE_CORR_THRESHOLD_WINDOW = 25
SHARPE_CORR_THRESHOLD_LOWER_BOUND = 0.0
SHARPE_CORR_THRESHOLD_CORR_WINDOW = 100
SHARPE_CORR_THRESHOLD_CORR_THRESHOLD = 0.9
SHARPE_CORR_THRESHOLD_STOP_LOSS_PCT = 0.1
SHARPE_CORR_THRESHOLD_FEE_RATE = 0.0003


@dataclass(frozen=True)
class StrategyConfig:
    top_n: int = SHARPE_CORR_THRESHOLD_TOP_N
    fee_rate: float = SHARPE_CORR_THRESHOLD_FEE_RATE
    sharpe_window: int = SHARPE_CORR_THRESHOLD_WINDOW

    @property
    def factor_name(self) -> str:
        return f"sharpe_{self.sharpe_window}"
