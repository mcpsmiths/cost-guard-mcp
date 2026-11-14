"""Coarse runtime-scaling tiers behind the Snowflake and Databricks dollar figures.

This is NOT derived from real query-history telemetry - both Snowflake's
ACCOUNT_USAGE.QUERY_HISTORY and Databricks' system.query.history require elevated
privileges this project does not request (see AGENTS.md's no-default-role rule).
It exists only to stop the cost formula being byte-blind (a 1 KB query and a 4 TB
query previously got an identical dollar estimate).

What it is: a floored, monotone, capped four-step function of the scanned bytes and
nothing else. The caller passes a 30 s baseline (snowflake._ASSUMED_RUNTIME_HOURS,
databricks._ASSUMED_RUNTIME_HOURS - a placeholder, not a measurement), and the baseline
is multiplied by 1/4/20/100 for scans under 10 GiB, under 100 GiB, under 1 TiB and 1 TiB
or more. So the result is never below 30 s and never above 3000 s, however large the scan.

What it is not: it is not an upper bound. It cannot see compute cost, so a compute-heavy
query on a small scan is priced at the 30 s floor, and a query that really runs longer than
3000 s is priced at 3000 s. It can under-estimate real runtime, and so the dollar figure
built on it; treat that figure as directional, not as a cap on spend. (Snowflake replaces
it with the mean of past runs when an exact repeat of the same SQL text has run before.)
Revisit with real usage data once available (same caveat already carried by the pricing
tables in snowflake_pricing.py / databricks_pricing.py).
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
