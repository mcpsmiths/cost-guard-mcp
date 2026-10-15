import base64
import datetime
import logging
import math
import uuid
from collections.abc import Callable
from decimal import Decimal

from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.engines.bigquery import (
    _INT64_MAX,
    ProviderByteCapExceeded,
    cost_cap_to_max_bytes_billed,
)
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.sql_scope import assert_single_statement
from cost_guard_mcp.tools.estimate_query_cost import estimate_query_cost
from cost_guard_mcp.types import BoundedQueryResult, Engine, RefusalReason

logger = logging.getLogger(__name__)

# Above this, fixed four-decimal dollars become a wall of digits; fall back to %g.
_FIXED_POINT_USD_LIMIT = 1e12


def _format_usd(amount: float) -> str:
    """Render a dollar amount for a refusal hint without hiding its magnitude.

    Fixed four decimals read naturally for ordinary caps ($1.00, $10.00) but rendered a
    $0.00001 cap as "$0.0000", contradicting the refusal it explains, and would turn a
    huge cap into a 300-digit string. Use the shortest significant form only when
    fixed-point would lose the value.
    """
    fixed = f"{amount:.4f}"
    is_rounded_to_zero = amount != 0 and float(fixed) == 0
    if is_rounded_to_zero or abs(amount) >= _FIXED_POINT_USD_LIMIT:
        return f"{amount:.6g}"
    return fixed


# Cell types pydantic serialises natively in JSON mode (Decimal -> str, datetime/date/time ->
# ISO 8601, timedelta -> ISO 8601 duration, UUID -> str); passed through untouched.
_PYDANTIC_NATIVE_CELL_TYPES = (
    Decimal,
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    uuid.UUID,
)


def _json_safe_text(text: str) -> str:
    """Return `text` as UTF-8-encodable text, never raising.

    A Python str is not automatically UTF-8: surrogateescape decoding of non-UTF-8 source
    bytes (which BigQuery can hand back in a STRING cell) leaves lone surrogates such as
    \\udcff in it, and pydantic_core serialises str through a strict UTF-8 encoder, so one
    such cell raised PydanticSerializationError inside the SDK's result conversion - after
    the billed execution and after the success record. backslashreplace turns the surrogate
    into its six visible characters (\\udcff): honest about what came back, lossless for
    forensics, and never raising. The fast path is one encode of the all-valid common case.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text


def _json_safe_value(value: object) -> object:
    """Normalise one connector-native cell so the mcp SDK can always serialise it.

    Runs AFTER the billed query has executed, so it must never raise and must cover every
    engine's native row values: bytes-like cells (BigQuery BYTES, Snowflake/Databricks
    BINARY) become base64 text instead of tripping pydantic's default utf8 bytes mode;
    NaN/Inf become explicit strings instead of JSON null; str cells (and every text this
    function produces from a non-str value) pass through _json_safe_text so a lone surrogate
    cannot trip pydantic's strict UTF-8 str encoder; driver-specific types pydantic does not
    know (BigQuery INTERVAL -> dateutil relativedelta) are stringified instead of raising
    "Unable to serialize unknown type". Returns new objects, never mutates.
    """
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return _json_safe_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {
            _json_safe_text(str(_json_safe_value(key))): _json_safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, _PYDANTIC_NATIVE_CELL_TYPES):
        return value
    return _json_safe_text(str(value))


def _json_safe_rows(rows: list[dict]) -> list[dict]:
    """The one choke point for all three engines and both return paths (ok and
    row_cap_exceeded): every value placed in BoundedQueryResult.rows - column names
    included - is JSON-serialisable and UTF-8-encodable."""
    return [
        {_json_safe_text(str(key)): _json_safe_value(item) for key, item in row.items()}
        for row in rows
    ]


def run_query_bounded(
    engine: Engine,
    sql: str,
    max_bytes_billed: int | None = None,
    max_rows: int | None = None,
    max_estimated_cost_usd: float | None = None,
    warehouse: str | None = None,
    warehouse_size: str | None = None,
    edition: str | None = None,
    *,
    before_execute: Callable[[], None] | None = None,
) -> BoundedQueryResult:
    # The text priced below must be the text executed below, as exactly one statement:
    # a second statement behind a top-level ';' would run under a cap that was never
    # checked against it. Explicit here (estimate_query_cost checks too) so the pin holds
    # even when the estimate is replaced or skipped.
    assert_single_statement(sql, engine)

    # `estimate > nan` is False for every estimate, so a NaN cap would admit anything with
    # no cap at all - and the mcp SDK's JSON parser accepts a bare NaN literal. +inf is
    # left alone: it compares correctly and simply means "no dollar cap".
    if max_estimated_cost_usd is not None and math.isnan(max_estimated_cost_usd):
        raise UserVisibleError("max_estimated_cost_usd must be a finite number, not NaN.")

    # Validate the remaining bounds before any estimate or execution: max_rows below 1
    # used to reach the provider as `LIMIT 0` / `LIMIT -1` and then be reported as a
    # row-cap refusal after the billed run; a byte cap outside int64 cannot be attached
    # to a BigQuery job and would surface as an opaque provider error instead.
    # The wrapper dispatches LIMIT max_rows + 1, so that literal must fit int64 too.
    if max_rows is not None and not 1 <= max_rows <= _INT64_MAX - 1:
        # The upper bound mirrors max_bytes_billed: a LIMIT literal above int64 is dispatched
        # after the estimate ran and only fails at the provider (delta verification).
        raise UserVisibleError(f"max_rows must be between 1 and {_INT64_MAX - 1} (got {max_rows}).")
    if max_bytes_billed is not None and not 1 <= max_bytes_billed <= _INT64_MAX:
        raise UserVisibleError(
            f"max_bytes_billed must be between 1 and {_INT64_MAX} bytes (got {max_bytes_billed})."
        )

    # warehouse_size/edition must reach the cap-check estimate below, not just
    # estimate_query_cost's own public tool - otherwise the cost cap here would always be
    # checked against the smallest warehouse's rate regardless of which warehouse the
    # query actually runs on, silently under-enforcing the cap for a caller specifying a
    # larger one.
    # allow_warehouse_wake=True: the read-only warehouse-state guards (DECISIONS.md #20) do not
    # apply to this pre-execution estimate - the execution that follows wakes the warehouse
    # anyway, and starting it is the accepted cost of an executed query under caps. The cap
    # gate is therefore still fed the observed-warehouse, full-lookup estimate.
    estimate = estimate_query_cost(
        engine, sql, warehouse, warehouse_size, edition, allow_warehouse_wake=True
    )

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
                    f"Estimated cost ${_format_usd(estimate.estimated_cost_usd)} exceeds "
                    f"max_estimated_cost_usd=${_format_usd(max_estimated_cost_usd)}. "
                    "Narrow the query "
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

    # BigQuery only: make the provider enforce the dollar cap too. The gate above trusts
    # the dry-run figure, which BigQuery itself flags as uninformative for row-level-
    # security-protected tables (always 0 bytes), LOWER_BOUND estimates and absent
    # statistics - so attach a maximumBytesBilled derived from the cap. A caller-supplied
    # byte cap is only ever lowered, never raised; +inf means "no cap" and derives nothing.
    # (The dollar gate above has already refused a finite cap with no dollar estimate, so a
    # dollar estimate is guaranteed to exist whenever this derivation runs.)
    effective_max_bytes_billed = max_bytes_billed
    byte_cap_derived_from_cost_cap = False
    if (
        engine == "bigquery"
        and max_estimated_cost_usd is not None
        and math.isfinite(max_estimated_cost_usd)
    ):
        derived = cost_cap_to_max_bytes_billed(max_estimated_cost_usd)
        if max_bytes_billed is None or derived <= max_bytes_billed:
            effective_max_bytes_billed = derived
            byte_cap_derived_from_cost_cap = True

    # Last cooperative cancellation point. Everything above is read-only (the estimate and
    # the cap checks); everything below has a monetary side effect. server.py binds this to
    # anyio.from_thread.check_cancelled, so an MCP `notifications/cancelled` that arrived
    # while the estimate was running raises here and the billable query is never submitted
    # by the abandoned worker thread. Keyword-only and None by default: check_cancelled
    # raises NoEventLoopError outside an anyio worker thread, so the server injects it;
    # direct callers (tests, library use) must never pay for it or be able to pass it
    # positionally as a ninth tool parameter.
    if before_execute is not None:
        before_execute()

    try:
        if engine == "bigquery":
            rows, row_count, row_cap_hit = bigquery_engine.execute_bounded(
                sql, max_bytes_billed=effective_max_bytes_billed, max_rows=max_rows
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
    except ProviderByteCapExceeded as exc:
        if effective_max_bytes_billed is None:
            # No cap of ours was attached, so this is not a refusal the caller asked for:
            # let it surface as the SanitizedEngineError (-> ToolError) it is, like any
            # other provider error, rather than a refusal blaming "max_bytes_billed=None".
            raise
        # The provider enforced the byte cap we attached: a cap refusal is a normal result
        # (DECISIONS.md #4), attributed to whichever cap the effective limit came from.
        reason = (
            RefusalReason.COST_CAP_EXCEEDED
            if byte_cap_derived_from_cost_cap
            else RefusalReason.BYTE_CAP_EXCEEDED
        )
        logger.info(
            "engine=%s run_query_bounded refused reason=%s detail=provider_byte_cap_exceeded "
            "maximum_bytes_billed=%s",
            engine,
            reason.value,
            exc.maximum_bytes_billed,
        )
        cap_origin = (
            f"derived from max_estimated_cost_usd=${_format_usd(max_estimated_cost_usd)}"
            if byte_cap_derived_from_cost_cap and max_estimated_cost_usd is not None
            else f"your max_bytes_billed={max_bytes_billed}"
        )
        return BoundedQueryResult(
            status="refused",
            reason=reason,
            estimate=estimate,
            hint=(
                "BigQuery refused to run this query: it would bill more than the server-side "
                f"maximumBytesBilled of {exc.maximum_bytes_billed} bytes ({cap_origin}). "
                "Nothing was billed. BigQuery bills in 1 MiB steps with a 10 MiB minimum per "
                "referenced table, so a query estimated within a few MiB of the cap can trip "
                "it. Narrow the query or raise the bound."
            ),
        )

    # After dispatch and before either return path, so a billed execution always yields a
    # structured result (or an actionable refusal) and never a post-billing serialisation
    # error from a connector-native cell type.
    rows = _json_safe_rows(rows)

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
