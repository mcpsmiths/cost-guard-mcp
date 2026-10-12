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
            (
                "The dollar figure is priced at the warehouse the query is pinned to as observed "
                "through SHOW WAREHOUSES (size, type, Gen1/Gen2) - or the session's current "
                "warehouse when no warehouse argument is given; warehouse_size and edition "
                "(operator-configured via SNOWFLAKE_EDITION) can only raise the estimate, never "
                "lower it. If that lookup fails (e.g. the role cannot run SHOW WAREHOUSES) the "
                "estimate falls back to the caller-asserted size at Gen1 rates with a caveat."
            ),
            (
                "Only STANDARD warehouses are priced: a Snowpark-optimized (or any other "
                "non-STANDARD type, or unrecognised-size) warehouse gets a byte estimate but no "
                "dollar figure, so run_query_bounded refuses a max_estimated_cost_usd cap on it "
                "- omit the dollar cap to run there."
            ),
            (
                "check_credentials and estimate_query_cost are read-only with respect to data "
                "but not free of compute: they run lightweight statements (USE WAREHOUSE, a "
                "session-context SELECT, EXPLAIN, SHOW WAREHOUSES, CURRENT_REGION(), a "
                "QUERY_HISTORY read) on the named or default warehouse session, which can "
                "resume a suspended AUTO_RESUME warehouse and bill compute until it suspends "
                "again."
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
                "sources, non-Delta external tables) - without them both the byte and the "
                "dollar estimate are unavailable (None), not zero, and run_query_bounded "
                "refuses any max_bytes_billed or max_estimated_cost_usd cap for that query."
            ),
            (
                "The dollar figure assumes a fixed placeholder runtime on a Serverless SQL "
                "warehouse, not derived from this query's actual expected runtime."
            ),
            (
                "The dollar figure is priced at the SQL warehouse DATABRICKS_HTTP_PATH binds "
                "to, as observed through the Warehouses API (cluster size, Serverless or not); "
                "warehouse_size can only raise the estimate, never lower it. If that lookup "
                "fails (e.g. the principal lacks CAN_VIEW/CAN_USE on the warehouse) the "
                "estimate falls back to the caller-asserted size with a caveat."
            ),
            (
                "Only Serverless SQL warehouse pricing is modeled: a Classic/Pro warehouse "
                "(different DBU rates plus a separate cloud VM cost) gets a byte estimate but "
                "no dollar figure, so run_query_bounded refuses a max_estimated_cost_usd cap on "
                "it - omit the dollar cap to run there."
            ),
            (
                "There is no per-query USE WAREHOUSE equivalent - the SQL warehouse is "
                "fixed by DATABRICKS_HTTP_PATH at connect time, not overridable per call."
            ),
            (
                "check_credentials and estimate_query_cost are read-only with respect to data "
                "but not free of compute: they open a session on the DATABRICKS_HTTP_PATH "
                "warehouse and run SELECT current_user() / EXPLAIN COST, which can start a "
                "STOPPED SQL warehouse and bill compute until it auto-stops again."
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
