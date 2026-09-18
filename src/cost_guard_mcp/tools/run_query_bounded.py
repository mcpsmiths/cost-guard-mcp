import logging

from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.tools.estimate_query_cost import estimate_query_cost
from cost_guard_mcp.types import BoundedQueryResult, Engine, RefusalReason

logger = logging.getLogger(__name__)


def run_query_bounded(
    engine: Engine,
    sql: str,
    max_bytes_billed: int | None = None,
    max_rows: int | None = None,
    max_estimated_cost_usd: float | None = None,
    warehouse: str | None = None,
    warehouse_size: str | None = None,
    edition: str | None = None,
) -> BoundedQueryResult:
    # warehouse_size/edition must reach the cap-check estimate below, not just
    # estimate_query_cost's own public tool - otherwise the cost cap here would always be
    # checked against the smallest warehouse's rate regardless of which warehouse the
    # query actually runs on, silently under-enforcing the cap for a caller specifying a
    # larger one.
    estimate = estimate_query_cost(engine, sql, warehouse, warehouse_size, edition)

    if max_estimated_cost_usd is not None:
        if estimate.estimated_cost_usd is None:
            logger.info(
                "engine=%s run_query_bounded refused reason=%s detail=no_cost_estimate_available",
                engine,
                RefusalReason.COST_CAP_EXCEEDED.value,
            )
            return BoundedQueryResult(
                status="refused",
                reason=RefusalReason.COST_CAP_EXCEEDED,
                estimate=estimate,
                hint=(
                    "Could not produce a dollar cost estimate for this query (e.g. a "
                    "capacity-billed project) — refusing rather than running with no "
                    "cost-cap enforcement."
                ),
            )
        if estimate.estimated_cost_usd > max_estimated_cost_usd:
            logger.info(
                "engine=%s run_query_bounded refused reason=%s "
                "estimated_cost_usd=%s max_estimated_cost_usd=%s",
                engine,
                RefusalReason.COST_CAP_EXCEEDED.value,
                estimate.estimated_cost_usd,
                max_estimated_cost_usd,
            )
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

    if max_bytes_billed is not None:
        if estimate.estimated_bytes is None:
            logger.info(
                "engine=%s run_query_bounded refused reason=%s detail=no_byte_estimate_available",
                engine,
                RefusalReason.BYTE_CAP_EXCEEDED.value,
            )
            return BoundedQueryResult(
                status="refused",
                reason=RefusalReason.BYTE_CAP_EXCEEDED,
                estimate=estimate,
                hint=(
                    "Could not produce a byte estimate for this query — refusing "
                    "rather than running with no byte-cap enforcement."
                ),
            )
        if estimate.estimated_bytes > max_bytes_billed:
            logger.info(
                "engine=%s run_query_bounded refused reason=%s "
                "estimated_bytes=%s max_bytes_billed=%s",
                engine,
                RefusalReason.BYTE_CAP_EXCEEDED.value,
                estimate.estimated_bytes,
                max_bytes_billed,
            )
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
        rows, row_count, row_cap_hit = snowflake_engine.execute_bounded(
            sql, warehouse=warehouse, max_rows=max_rows
        )
    elif engine == "databricks":
        rows, row_count, row_cap_hit = databricks_engine.execute_bounded(
            sql, warehouse=warehouse, max_rows=max_rows
        )
    else:
        raise UserVisibleError(f"run_query_bounded: engine '{engine}' is not yet supported.")

    if row_cap_hit:
        logger.info(
            "engine=%s run_query_bounded refused reason=%s max_rows=%s row_count=%s",
            engine,
            RefusalReason.ROW_CAP_EXCEEDED.value,
            max_rows,
            row_count,
        )
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
