from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.sql_scope import assert_single_statement
from cost_guard_mcp.sql_scope_keywords import assert_read_statement
from cost_guard_mcp.types import CostEstimate, Engine


def estimate_query_cost(
    engine: Engine,
    sql: str,
    warehouse: str | None = None,
    warehouse_size: str | None = None,
    edition: str | None = None,
    *,
    allow_warehouse_wake: bool = False,
) -> CostEstimate:
    """`allow_warehouse_wake` is keyword-only and is NOT a tool argument (server.py never passes
    it, so the published inputSchema is unchanged): it is run_query_bounded's bypass of the
    read-only warehouse-state guards (DECISIONS.md #20) - its execution wakes the warehouse
    anyway. The read-only estimate_query_cost tool leaves it False. BigQuery ignores it."""
    # The estimate prices the text as one unit, so the text must BE one unit - see sql_scope.
    # And only a read statement is priced: warehouse rows re-enter the model context and can
    # steer the next call (DECISIONS.md #13) - the read gate sees text already proven single.
    assert_single_statement(sql, engine)
    assert_read_statement(sql, engine)
    if engine == "bigquery":
        return bigquery_engine.dry_run(sql)
    if engine == "snowflake":
        snowflake_kwargs: dict[str, str] = {}
        if warehouse_size is not None:
            snowflake_kwargs["warehouse_size"] = warehouse_size
        if edition is not None:
            snowflake_kwargs["edition"] = edition
        return snowflake_engine.explain_estimate(
            sql, warehouse, allow_warehouse_wake=allow_warehouse_wake, **snowflake_kwargs
        )
    if engine == "databricks":
        databricks_kwargs: dict[str, str] = {}
        if warehouse_size is not None:
            databricks_kwargs["warehouse_size"] = warehouse_size
        return databricks_engine.explain_estimate(
            sql, warehouse, allow_warehouse_wake=allow_warehouse_wake, **databricks_kwargs
        )
    raise UserVisibleError(
        f"estimate_query_cost: engine '{engine}' is not yet supported. "
        "Supported engines: ['bigquery', 'snowflake', 'databricks']."
    )
