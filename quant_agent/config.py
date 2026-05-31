from __future__ import annotations

from dataclasses import dataclass, field


SHARPE_SINGLE_TOP_N = 5
SHARPE_SINGLE_WINDOW = 20
SHARPE_SINGLE_FEE_RATE = 0.0003


@dataclass(frozen=True)
class StrategyConfig:
    top_n: int = 10
    min_fund_size_cny: float = 10_000_000_000
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
    ) -> "StrategyConfig":
        return cls(
            top_n=top_n,
            fee_rate=fee_rate,
            factor_weights={f"sharpe_{SHARPE_SINGLE_WINDOW}": 1.0},
        )
