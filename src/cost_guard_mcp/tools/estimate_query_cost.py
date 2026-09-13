from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.types import CostEstimate, Engine


def estimate_query_cost(engine: Engine, sql: str, warehouse: str | None = None) -> CostEstimate:
    if engine == "bigquery":
        return bigquery_engine.dry_run(sql)
    if engine == "snowflake":
        return snowflake_engine.explain_estimate(sql, warehouse)
    raise UserVisibleError(
        f"estimate_query_cost: engine '{engine}' is not yet supported. "
        "Supported engines: ['bigquery', 'snowflake']."
    )
