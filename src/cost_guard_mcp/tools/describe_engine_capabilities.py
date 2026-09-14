from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.types import AccuracyTier, Engine, EngineCapabilities

_CAPABILITIES: dict[str, EngineCapabilities] = {
    "bigquery": EngineCapabilities(
        engine="bigquery",
        supports_precise_bytes=True,
        supports_dollar_estimate=True,
        default_accuracy_tier=AccuracyTier.PRECISE,
        known_gaps=[
            (
                "Estimate downgrades to UPPER_BOUND when BigQuery's own "
                "totalBytesProcessedAccuracy is not PRECISE (e.g. federated or wildcard tables, "
                "or tables with a pending streaming buffer)."
            ),
            (
                "Projects on BigQuery Editions/capacity billing get a byte estimate only, no "
                "dollar figure — capacity billing has no fixed $/byte rate."
            ),
        ],
    ),
    "snowflake": EngineCapabilities(
        engine="snowflake",
        supports_precise_bytes=False,
        supports_dollar_estimate=True,
        default_accuracy_tier=AccuracyTier.UPPER_BOUND,
        known_gaps=[
            (
                "EXPLAIN's bytesAssigned/partitionsAssigned are themselves documented upper "
                "bounds — runtime optimizations can scan fewer bytes than estimated."
            ),
            "The estimate excludes Cortex AI Function ('AI Credits') cost entirely.",
            (
                "The dollar figure assumes a fixed placeholder runtime, not this query's actual "
                "expected runtime — treat it as directional, not a tight bound."
            ),
            (
                "EXPLAIN's plan can vary by which warehouse is active when it runs; this tool "
                "pins the warehouse explicitly to reduce that variance only when a "
                "warehouse argument is supplied - omitting it uses whatever warehouse is "
                "already active on the connection."
            ),
        ],
    ),
    "databricks": EngineCapabilities(
        engine="databricks",
        supports_precise_bytes=False,
        supports_dollar_estimate=True,
        default_accuracy_tier=AccuracyTier.HEURISTIC,
        known_gaps=[
            (
                "Databricks has no BigQuery-style dry-run; EXPLAIN COST's plan-node "
                "statistics are frequently absent (no ANALYZE TABLE run, streaming "
                "sources, non-Delta external tables) - byte estimates may be unavailable."
            ),
            (
                "The dollar figure assumes a fixed placeholder runtime on a Serverless SQL "
                "warehouse, not derived from this query's actual expected runtime."
            ),
            (
                "Only Serverless SQL warehouse pricing is modeled - Classic/Pro warehouses "
                "use different DBU rates plus a separate cloud VM cost not modeled here."
            ),
            (
                "There is no per-query USE WAREHOUSE equivalent - the SQL warehouse is "
                "fixed by DATABRICKS_HTTP_PATH at connect time, not overridable per call."
            ),
        ],
    ),
}


def describe_engine_capabilities(engine: Engine) -> EngineCapabilities:
    if engine not in _CAPABILITIES:
        raise UserVisibleError(
            f"describe_engine_capabilities: engine '{engine}' is not yet supported. "
            f"Supported engines: {sorted(_CAPABILITIES)}."
        )
    return _CAPABILITIES[engine]
