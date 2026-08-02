"""Compatibility facade for the recommendation workflow.

New code should import from :mod:`quant_core.recommendation`. Existing callers
can continue using this module while the implementation lives in the dedicated
package.
"""

from quant_core.recommendation.context import (
    _load_requirements,
    load_production_context,
)
from quant_core.recommendation.models import (
    EXECUTION_SEMANTICS,
    PRODUCTION_SCHEMA_VERSION,
    ParameterSearchError,
    ProductionContext,
    SearchResult,
    StrategyDataRequirements,
)
from quant_core.recommendation.replay import (
    _historical_boundaries,
    _refresh_data,
    _validate_refresh_preserves_available_history,
    causal_replay,
    closed_market_data_end,
    next_schedule_boundary,
    resolve_signal_date,
)
from quant_core.recommendation.search import (
    search_parameters,
)
from quant_core.recommendation.service import run_recommendation
from quant_core.schedule import is_schedule_boundary, schedule_bucket

__all__ = [
    "EXECUTION_SEMANTICS",
    "PRODUCTION_SCHEMA_VERSION",
    "ParameterSearchError",
    "ProductionContext",
    "SearchResult",
    "StrategyDataRequirements",
    "causal_replay",
    "closed_market_data_end",
    "is_schedule_boundary",
    "load_production_context",
    "next_schedule_boundary",
    "resolve_signal_date",
    "run_recommendation",
    "schedule_bucket",
    "search_parameters",
]
