from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.types import CostEstimate, Engine


def estimate_query_cost(
    engine: Engine,
    sql: str,
    warehouse: str | None = None,
    warehouse_size: str | None = None,
    edition: str | None = None,
) -> CostEstimate:
    if engine == "bigquery":
        return bigquery_engine.dry_run(sql)
    if engine == "snowflake":
        snowflake_kwargs: dict[str, str] = {}
        if warehouse_size is not None:
            snowflake_kwargs["warehouse_size"] = warehouse_size
        if edition is not None:
            snowflake_kwargs["edition"] = edition
        return snowflake_engine.explain_estimate(sql, warehouse, **snowflake_kwargs)
    if engine == "databricks":
        databricks_kwargs: dict[str, str] = {}
        if warehouse_size is not None:
            databricks_kwargs["warehouse_size"] = warehouse_size
        return databricks_engine.explain_estimate(sql, warehouse, **databricks_kwargs)
    raise UserVisibleError(
        f"estimate_query_cost: engine '{engine}' is not yet supported. "
        "Supported engines: ['bigquery', 'snowflake', 'databricks']."
    )
