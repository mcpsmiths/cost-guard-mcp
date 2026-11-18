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
                "Estimate is tagged UPPER_BOUND when BigQuery's own totalBytesProcessedAccuracy "
                "is UNKNOWN or UPPER_BOUND (e.g. federated or wildcard tables, or tables with a "
                "pending streaming buffer); a LOWER_BOUND accuracy is a floor, not a cap - the "
                "byte figure is tagged HEURISTIC and no dollar figure is derived from it, so a "
                "max_estimated_cost_usd cap refuses."
            ),
            (
                "Dry runs against tables protected by row-level security always report 0 bytes "
                "(by design, to prevent a side-channel): 0 bytes on a statement that references "
                "a table yields no dollar figure (HEURISTIC), so a max_estimated_cost_usd cap "
                "refuses - bound such runs with max_bytes_billed, which BigQuery enforces "
                "server-side as maximumBytesBilled. Genuinely free 0-byte dry runs share the "
                "shape and the outcome: an unfiltered COUNT(*) answered from table metadata, an "
                "empty table, a partition filter that prunes every partition."
            ),
            (
                "Projects on BigQuery Editions/capacity billing get a byte estimate only, no "
                "dollar figure — capacity billing has no fixed $/byte rate. The check looks up "
                "reservation assignments in the dry-run job's own location (US only when the "
                "job reports none)."
            ),
            (
                "Caps are enforced by BigQuery itself, the only engine where they are, and only "
                "on an on-demand project: run_query_bounded attaches the smaller of "
                "max_bytes_billed and a limit derived from a finite max_estimated_cost_usd to "
                "the job as maximumBytesBilled, and BigQuery rejects a query that would bill "
                "more before it runs. BigQuery bills in 1 MiB steps with a 10 MiB minimum per "
                "referenced table, so a tight cap on a many-table query can still be rejected "
                "after passing the estimate check. On a capacity-billed project there is no "
                "dollar figure, so a dollar cap refuses; a max_bytes_billed cap is still "
                "attached to the job, but whether BigQuery enforces it there is not verified."
            ),
            (
                "Prices are US list prices: the US multi-region on-demand rate ($6.25/TiB) is "
                "used for every dataset location, so a non-US dataset or a negotiated rate can "
                "be priced too low or too high, and a max_estimated_cost_usd cap compares "
                "against that figure."
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
                "UPPER_BOUND describes the byte figure (EXPLAIN's bytesAssigned) only, not the "
                "dollar figure. Unless an exact repeat of the same SQL text is found in your "
                "query history (the mean of its past runs is used then), the dollar figure is "
                "the warehouse rate times a fixed 30-second runtime placeholder scaled by the "
                "scanned bytes alone (x1 under 10 GiB, x4 under 100 GiB, x20 under 1 TiB, x100 "
                "from 1 TiB up: never below 30 s, never above 3000 s). It cannot see compute "
                "cost, so it can under-estimate - treat it as directional, not as an upper "
                "bound or a cap on spend."
            ),
            (
                "Caps are checked against the estimate only; Snowflake enforces none of them: "
                "max_bytes_billed and max_estimated_cost_usd are compared with the estimate "
                "before the query is submitted (a requested cap with no usable estimate "
                "refuses), no provider-side spend limit is set, and once a query is admitted "
                "the only hard stop the server sets is the 120-second limit on how long it runs "
                "(a watchdog cancel plus a server-side STATEMENT_TIMEOUT_IN_SECONDS). A query "
                "the estimate under-reads can therefore run for up to 120 seconds on the "
                "warehouse."
            ),
            (
                "Prices are US list prices: credit rates are the AWS US East / Azure East US / "
                "GCP us-central1 on-demand rates whatever region the account runs in, so a "
                "non-US account or a negotiated rate can be priced too low or too high, and a "
                "max_estimated_cost_usd cap compares against that figure."
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
                "estimate falls back to the caller-asserted size at Gen1 rates with a caveat; "
                "if SHOW WAREHOUSES lists rows but none named exactly like the pinned "
                "warehouse, no dollar figure is produced and a dollar cap refuses. The same "
                "row's state and auto_resume drive the warehouse-state guard (last gap)."
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
                "again. By default SELECT CURRENT_REGION(), the QUERY_HISTORY read and the "
                "session-context SELECT run only when the pinned warehouse's SHOW WAREHOUSES "
                "state is STARTED (skipped when no warehouse is pinned or the state cannot be "
                "read; the QUERY_HISTORY read is also skipped when estimate_query_cost gets no "
                "warehouse argument): the estimate then prices at Gen1 rates at the observed "
                "size with the byte-tier heuristic and a caveat, and check_credentials returns "
                "ok=true with a detail built from the SHOW WAREHOUSES row. USE WAREHOUSE, "
                "EXPLAIN USING JSON, SHOW WAREHOUSES and SELECT CURRENT_WAREHOUSE() always run. "
                "COST_GUARD_SNOWFLAKE_ALLOW_WAREHOUSE_RESUME=true (exactly that value) runs the "
                "gated statements regardless. run_query_bounded is not gated: its estimate and "
                "execution may resume the warehouse."
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
                "The dollar figure is the Serverless SQL warehouse rate times a fixed 30-second "
                "runtime placeholder scaled by the scanned bytes alone (x1 under 10 GiB, x4 "
                "under 100 GiB, x20 under 1 TiB, x100 from 1 TiB up: never below 30 s, never "
                "above 3000 s), with no query-history calibration. It cannot see compute cost, "
                "so it can under-estimate - treat it as directional, not as an upper bound or a "
                "cap on spend."
            ),
            (
                "Caps are checked against the estimate only; Databricks enforces none of them: "
                "max_bytes_billed and max_estimated_cost_usd are compared with the estimate "
                "before the query is submitted (a requested cap with no usable estimate "
                "refuses), no provider-side spend limit is set, and once a query is admitted "
                "the only hard stop the server sets is the 120-second limit on how long it runs "
                "(a watchdog cancel plus a server-side STATEMENT_TIMEOUT). A query the "
                "estimate under-reads can therefore run for up to 120 seconds on the "
                "warehouse."
            ),
            (
                "Prices are US list prices: the Serverless SQL rate is the US-region $0.70/DBU "
                "(EU regions list higher) whatever region the workspace runs in, so a non-US "
                "workspace or a negotiated rate can be priced too low or too high, and a "
                "max_estimated_cost_usd cap compares against that figure."
            ),
            (
                "The dollar figure is priced at the SQL warehouse DATABRICKS_HTTP_PATH binds "
                "to, as observed through the Warehouses API (cluster size, Serverless or not); "
                "warehouse_size can only raise the estimate, never lower it. If that lookup "
                "fails (e.g. the principal lacks CAN_VIEW/CAN_USE on the warehouse) the "
                "estimate falls back to the caller-asserted size with a caveat - for "
                "run_query_bounded, or for the read-only tools when "
                "COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true; by default "
                "check_credentials and estimate_query_cost refuse instead (next gap)."
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
                "STOPPED SQL warehouse and bill compute until it auto-stops again. By default "
                "they first read the warehouse's state through the Warehouses API and refuse "
                "unless it is RUNNING - also when the state cannot be read: check_credentials "
                "returns ok=false and estimate_query_cost no byte or dollar figure, with no "
                "session opened. COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true (exactly that "
                "value) lets them open the session regardless. run_query_bounded is not gated: "
                "its estimate and execution may start the warehouse."
            ),
            (
                "Databricks has no live test job and no live EXPLAIN COST capture: nothing added "
                "from 0.4.0 on has a recorded live run (it has only run against fakes), the "
                "connector was last exercised by hand on 2026-09-14 and, as a check_credentials "
                "call, on 2026-09-18, both on pre-0.4.0 code with no output recorded in the "
                "repo, and the size parser accepts both SI (GB) and IEC (GiB) unit labels "
                "because the real EXPLAIN COST text shape is unconfirmed; text it does not "
                "recognise leaves no size statistics, so both estimates are unavailable and "
                "every cap refuses."
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
