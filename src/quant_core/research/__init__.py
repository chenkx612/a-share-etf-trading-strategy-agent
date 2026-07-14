"""Contracts for automated strategy research experiments."""

from quant_core.research.contracts import ExperimentResult, ResearchTask
from quant_core.research.loop import run_loop
from quant_core.research.runner import run_managed_once, run_once

__all__ = [
    "ExperimentResult",
    "ResearchTask",
    "run_loop",
    "run_managed_once",
    "run_once",
]
