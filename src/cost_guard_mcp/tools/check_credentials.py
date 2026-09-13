from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.types import CredentialCheckResult, Engine


def check_credentials(engine: Engine, warehouse: str | None = None) -> CredentialCheckResult:
    if engine == "bigquery":
        return bigquery_engine.check_credentials()
    if engine == "snowflake":
        return snowflake_engine.check_credentials(warehouse)
    if engine == "databricks":
        return databricks_engine.check_credentials(warehouse)
    raise UserVisibleError(
        f"check_credentials: engine '{engine}' is not yet supported. "
        "Supported engines: ['bigquery', 'snowflake', 'databricks']."
    )
