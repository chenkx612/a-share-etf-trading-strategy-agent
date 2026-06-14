from __future__ import annotations

from dataclasses import dataclass, field


SHARPE_SINGLE_TOP_N = 5
SHARPE_SINGLE_WINDOW = 20
SHARPE_SINGLE_FEE_RATE = 0.0003

RANKED_CORR_TOP_N = 5
RANKED_CORR_FACTOR_WINDOW = 25
RANKED_CORR_CORR_WINDOW = 100
RANKED_CORR_CORR_THRESHOLD = 0.9
RANKED_CORR_STOP_LOSS_PCT = 0.1
RANKED_CORR_FEE_RATE = 0.0003

RANKED_THRESHOLD_TOP_N = 5
RANKED_THRESHOLD_WINDOW = 25
RANKED_THRESHOLD_LOWER_BOUND = 0.0
RANKED_THRESHOLD_CORR_WINDOW = 100
RANKED_THRESHOLD_CORR_THRESHOLD = 0.9
RANKED_THRESHOLD_STOP_LOSS_PCT = 0.1
RANKED_THRESHOLD_FEE_RATE = 0.0003


@dataclass(frozen=True)
class StrategyConfig:
    top_n: int = 10
    fee_rate: float = 0.001
    factor_weights: dict[str, float] = field(default_factory=lambda: {
        "momentum_20": 1.0,
        "reversal_5": 1.0,
        "volatility_20": -1.0,
        "amount_mean_20": 0.5,
        "turnover_mean_20": 0.5,
    })

    @classmethod
    def sharpe_single_factor(
        cls,
        top_n: int = SHARPE_SINGLE_TOP_N,
        fee_rate: float = SHARPE_SINGLE_FEE_RATE,
        sharpe_window: int = SHARPE_SINGLE_WINDOW,
    ) -> "StrategyConfig":
        return cls(
            top_n=top_n,
            fee_rate=fee_rate,
            factor_weights={f"sharpe_{sharpe_window}": 1.0},
        )

    @classmethod
    def ranked_correlation_filter(
        cls,
        top_n: int = RANKED_CORR_TOP_N,
        fee_rate: float = RANKED_CORR_FEE_RATE,
        sharpe_window: int = RANKED_CORR_FACTOR_WINDOW,
    ) -> "StrategyConfig":
        return cls(
            top_n=top_n,
            fee_rate=fee_rate,
            factor_weights={f"sharpe_{sharpe_window}": 1.0},
        )

    @classmethod
    def ranked_threshold_filter(
        cls,
        top_n: int = RANKED_THRESHOLD_TOP_N,
        fee_rate: float = RANKED_THRESHOLD_FEE_RATE,
        sharpe_window: int = RANKED_THRESHOLD_WINDOW,
    ) -> "StrategyConfig":
        return cls(
            top_n=top_n,
            fee_rate=fee_rate,
            factor_weights={f"sharpe_{sharpe_window}": 1.0},
        )
