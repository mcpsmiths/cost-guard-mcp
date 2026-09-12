from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.tools.estimate_query_cost import estimate_query_cost
from cost_guard_mcp.types import BoundedQueryResult, Engine, RefusalReason


def run_query_bounded(
    engine: Engine,
    sql: str,
    max_bytes_billed: int | None = None,
    max_rows: int | None = None,
    max_estimated_cost_usd: float | None = None,
    warehouse: str | None = None,
) -> BoundedQueryResult:
    estimate = estimate_query_cost(engine, sql, warehouse)

    if (
        max_estimated_cost_usd is not None
        and estimate.estimated_cost_usd is not None
        and estimate.estimated_cost_usd > max_estimated_cost_usd
    ):
        return BoundedQueryResult(
            status="refused",
            reason=RefusalReason.COST_CAP_EXCEEDED,
            estimate=estimate,
            hint=(
                f"Estimated cost ${estimate.estimated_cost_usd:.4f} exceeds "
                f"max_estimated_cost_usd=${max_estimated_cost_usd:.4f}. Narrow the query "
                "or raise the bound."
            ),
        )

    if (
        max_bytes_billed is not None
        and estimate.estimated_bytes is not None
        and estimate.estimated_bytes > max_bytes_billed
    ):
        return BoundedQueryResult(
            status="refused",
            reason=RefusalReason.BYTE_CAP_EXCEEDED,
            estimate=estimate,
            hint=(
                f"Estimated bytes {estimate.estimated_bytes} exceed "
                f"max_bytes_billed={max_bytes_billed}. Narrow the query or raise the bound."
            ),
        )

    if engine == "bigquery":
        rows, row_count, row_cap_hit = bigquery_engine.execute_bounded(
            sql, max_bytes_billed=max_bytes_billed, max_rows=max_rows
        )
    elif engine == "snowflake":
        from cost_guard_mcp.engines import (
            snowflake as snowflake_engine,
        )  # Task 4.5 adds this module

        rows, row_count, row_cap_hit = snowflake_engine.execute_bounded(
            sql, warehouse=warehouse, max_rows=max_rows
        )
    else:
        raise ValueError(f"run_query_bounded: engine '{engine}' is not yet supported.")

    if row_cap_hit:
        return BoundedQueryResult(
            status="refused",
            reason=RefusalReason.ROW_CAP_EXCEEDED,
            estimate=estimate,
            hint=(
                f"Query returned more than max_rows={max_rows} rows; showing the first "
                f"{row_count}. Add a LIMIT to your query or raise max_rows."
            ),
            rows=rows,
            row_count=row_count,
        )

    return BoundedQueryResult(status="ok", estimate=estimate, rows=rows, row_count=row_count)
