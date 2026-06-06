from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


SHARPE_SINGLE_TOP_N = 5
SHARPE_SINGLE_WINDOW = 20
SHARPE_SINGLE_FEE_RATE = 0.0003

SECTOR_SHARPE_TOP_N = 5
SECTOR_SHARPE_FACTOR_WINDOW = 25
SECTOR_SHARPE_CORR_WINDOW = 100
SECTOR_SHARPE_CORR_THRESHOLD = 0.9
SECTOR_SHARPE_STOP_LOSS_PCT = 0.1
SECTOR_SHARPE_FEE_RATE = 0.0003


@dataclass(frozen=True)
class UniverseConfig:
    name: str
    description: str
    source: str
    min_fund_size_cny: float | None = None
    default_adjust: str = ""
    assets: dict[str, str] = field(default_factory=dict)

    def to_frame(self) -> pd.DataFrame:
        if self.source != "static":
            raise ValueError(f"Universe {self.name!r} is not a static universe")
        return pd.DataFrame([
            {"symbol": symbol, "name": alias, "fund_size": pd.NA}
            for alias, symbol in self.assets.items()
        ])


LARGE_ETF_UNIVERSE_NAME = "large-etf"
SECTOR_ROTATION_UNIVERSE_NAME = "sector-rotation"

SECTOR_ROTATION_ASSETS = {
    "cyb": "159915",
    "hs_tech": "513130",
    "nasdaq": "513100",
    "nas_tech": "159509",
    "india": "164824",
    "fcash": "159201",
    "bank": "512800",
    "grid": "159326",
    "gold": "518880",
    "telecom": "515880",
    "ai": "159819",
    "satellite": "159206",
    "software": "159852",
    "hksec": "513090",
    "sp_oil_gas": "159518",
    "ener_chem": "159981",
    "sp_biotech": "159502",
    "growth": "159259",
    "cloud": "159273",
    "hk_tech30": "159636",
    "kc_semi": "588170",
}

UNIVERSE_CONFIGS = {
    LARGE_ETF_UNIVERSE_NAME: UniverseConfig(
        name=LARGE_ETF_UNIVERSE_NAME,
        description="A-share ETFs filtered by fund size.",
        source="akshare_etf",
        min_fund_size_cny=10_000_000_000,
    ),
    SECTOR_ROTATION_UNIVERSE_NAME: UniverseConfig(
        name=SECTOR_ROTATION_UNIVERSE_NAME,
        description="Static ETF pool for sector rotation research.",
        source="static",
        default_adjust="qfq",
        assets=SECTOR_ROTATION_ASSETS,
    ),
}


def available_universe_names() -> list[str]:
    return list(UNIVERSE_CONFIGS)


def get_universe_config(name: str) -> UniverseConfig:
    try:
        return UNIVERSE_CONFIGS[name]
    except KeyError as exc:
        choices = ", ".join(available_universe_names())
        raise ValueError(f"Unknown universe {name!r}. Available universes: {choices}") from exc


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
    ) -> "StrategyConfig":
        return cls(
            top_n=top_n,
            fee_rate=fee_rate,
            factor_weights={f"sharpe_{SHARPE_SINGLE_WINDOW}": 1.0},
        )

    @classmethod
    def sector_sharpe_rotation(
        cls,
        top_n: int = SECTOR_SHARPE_TOP_N,
        fee_rate: float = SECTOR_SHARPE_FEE_RATE,
    ) -> "StrategyConfig":
        return cls(
            top_n=top_n,
            fee_rate=fee_rate,
            factor_weights={f"sharpe_{SECTOR_SHARPE_FACTOR_WINDOW}": 1.0},
        )
