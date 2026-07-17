"""Contracts for automated strategy research experiments."""

from quant_core.research.contracts import ExperimentResult, ResearchTask
from quant_core.research.loop import run_loop
from quant_core.research.report import regenerate_loop_report
from quant_core.research.runner import run_managed_once, run_once

__all__ = [
    "ExperimentResult",
    "ResearchTask",
    "regenerate_loop_report",
    "run_loop",
    "run_managed_once",
    "run_once",
]
