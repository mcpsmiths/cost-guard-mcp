"""Coarse, deliberately conservative runtime-scaling tiers.

This is NOT derived from real query-history telemetry - both Snowflake's
ACCOUNT_USAGE.QUERY_HISTORY and Databricks' system.query.history require elevated
privileges this project does not request (see AGENTS.md's no-default-role rule).
This exists only to stop the cost formula being byte-blind (a 1 KB query and a 4 TB
query previously got an identical dollar estimate) - it biases toward OVER-estimating
a big query's runtime, never under, matching this tool's purpose of never being falsely
reassuring about cost. Revisit with real usage data once available (same caveat already
carried by the pricing tables in snowflake_pricing.py / databricks_pricing.py).
"""

_GB = 1024**3
_TB = 1024**4

_TIERS = (
    (10 * _GB, 1),
    (100 * _GB, 4),
    (1 * _TB, 20),
    (float("inf"), 100),
)


def scale_runtime_hours(estimated_bytes: int | None, *, baseline_hours: float) -> float:
    """Scale baseline_hours up by a coarse size-tier multiplier. Never returns less
    than baseline_hours - estimated_bytes=None returns the baseline unchanged."""
    if estimated_bytes is None:
        return baseline_hours
    for upper_bound, multiplier in _TIERS:
        if estimated_bytes < upper_bound:
            return baseline_hours * multiplier
    return baseline_hours * _TIERS[-1][1]
