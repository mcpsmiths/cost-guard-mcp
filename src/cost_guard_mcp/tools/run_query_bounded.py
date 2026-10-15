import logging
import math
from collections.abc import Callable

from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines import databricks as databricks_engine
from cost_guard_mcp.engines import snowflake as snowflake_engine
from cost_guard_mcp.engines.bigquery import (
    _INT64_MAX,
    ProviderByteCapExceeded,
    cost_cap_to_max_bytes_billed,
)
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.result_bounds import (
    DEFAULT_MAX_ROWS,
    LARGE_MAX_ROWS_WARNING_THRESHOLD,
    MAX_RESULT_BYTES,
    row_cap_hint,
    size_cap_hint,
    trim_rows_to_byte_cap,
)
from cost_guard_mcp.sql_scope import assert_single_statement
from cost_guard_mcp.sql_scope_keywords import assert_read_statement, is_single_query_statement
from cost_guard_mcp.tools.estimate_query_cost import estimate_query_cost
from cost_guard_mcp.tools.result_rows import _MAX_CELL_NESTING_DEPTH, _depth_note, _normalise_rows
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
    # even when the estimate is replaced or skipped. And only a read statement: warehouse rows
    # re-enter the model context and can steer this tool - DECISIONS.md #13.
    assert_single_statement(sql, engine)
    assert_read_statement(sql, engine)

    # `estimate > nan` is False for every estimate, so a NaN cap would admit anything with
    # no cap at all - and the mcp SDK's JSON parser accepts a bare NaN literal. +inf is
    # left alone: it compares correctly ("no dollar cap" against any figure) and derives no
    # server-side byte cap below; it is still a requested dollar cap, so with no dollar figure
    # to compare the fail-closed branch below refuses it like any other (DECISIONS.md #22).
    if max_estimated_cost_usd is not None and math.isnan(max_estimated_cost_usd):
        raise UserVisibleError("max_estimated_cost_usd must be a finite number, not NaN.")

    # #94: omitting max_rows used to mean no row bound at all (the audit's reproduction fetched
    # 100,000 rows). The documented default is applied here, before the validation, so the
    # effective value is what gets validated and dispatched; an explicit value is honoured
    # unchanged, and a larger one (up to the bound below) is the opt-out. Not logged: it is
    # the documented behaviour, not an event. The inputSchema keeps `max_rows: int | None`.
    if max_rows is None:
        max_rows = DEFAULT_MAX_ROWS
    # Validate the remaining bounds before any estimate or execution: max_rows below 1
    # used to reach the provider as `LIMIT 0` / `LIMIT -1` and then be reported as a
    # row-cap refusal after the billed run; a byte cap outside int64 cannot be attached
    # to a BigQuery job and would surface as an opaque provider error instead.
    # The wrapper dispatches LIMIT max_rows + 1, so that literal must fit int64 too.
    if not 1 <= max_rows <= _INT64_MAX - 1:
        # The upper bound mirrors max_bytes_billed: a LIMIT literal above int64 is dispatched
        # after the estimate ran and only fails at the provider (delta verification).
        raise UserVisibleError(f"max_rows must be between 1 and {_INT64_MAX - 1} (got {max_rows}).")
    if max_bytes_billed is not None and not 1 <= max_bytes_billed <= _INT64_MAX:
        raise UserVisibleError(
            f"max_bytes_billed must be between 1 and {_INT64_MAX} bytes (got {max_bytes_billed})."
        )
    # Honoured, but said out loud: the 8 MiB cap bounds the serialised rows, not the Python
    # objects behind them (~25x the JSON size for narrow rows) nor the normalise + trim pass
    # over them, both of which happen after the billed execution and outside any watchdog
    # (DECISIONS.md #21). The default (1000) is far below this, so anything above is explicit.
    if max_rows > LARGE_MAX_ROWS_WARNING_THRESHOLD:
        logger.warning(
            "engine=%s run_query_bounded explicit max_rows=%s exceeds %s: narrow rows at this "
            "count can hold hundreds of MB and cost seconds of CPU after the billed execution",
            engine,
            max_rows,
            LARGE_MAX_ROWS_WARNING_THRESHOLD,
        )

    # Whether the engine may enclose the text in `SELECT * FROM (...) AS cost_guard_row_cap
    # LIMIT max_rows + 1`: only a single SELECT/WITH/VALUES statement is valid as a derived
    # table. SHOW/DESCRIBE/EXPLAIN, opt-out scripts and WITH ... INSERT run unwrapped and are
    # capped by the engine's paged fetch instead. Decided once here, with the gates' own lexer,
    # and passed down so the three engines cannot drift on the rule.
    wrap_in_limit = is_single_query_statement(sql, engine)

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
            hint = (
                "Could not produce a dollar cost estimate for this query - the estimate's "
                "caveats say why - refusing rather than running with no cost-cap enforcement."
            )
            if engine == "bigquery":
                # The BigQuery shapes that carry no dollar figure, and the documented way to
                # run them under a cap (#96, DECISIONS.md #22). Engine-specific text stays in
                # this branch so a Snowflake/Databricks refusal says nothing about dry runs.
                hint += (
                    " On BigQuery that is a capacity-billed project, a failed billing-model "
                    "check, or a dry run whose byte figure is not usable for pricing: 0 bytes "
                    "on a statement that references a table (the shape row-level security "
                    "produces, shared by aggregates answered from table metadata and by empty "
                    "data) or "
                    "LOWER_BOUND accuracy. Bound the run with max_bytes_billed instead; it is "
                    "enforced server-side as maximumBytesBilled."
                )
            return BoundedQueryResult(
                status="refused",
                executed=False,
                reason=RefusalReason.COST_CAP_EXCEEDED,
                estimate=estimate,
                hint=hint,
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
                executed=False,
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
                executed=False,
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
                executed=False,
                reason=RefusalReason.BYTE_CAP_EXCEEDED,
                estimate=estimate,
                hint=(
                    f"Estimated bytes {estimate.estimated_bytes} exceed "
                    f"max_bytes_billed={max_bytes_billed}. Narrow the query or raise the bound."
                ),
            )

    # BigQuery only: make the provider enforce the dollar cap too. The gate above trusts the
    # dry-run figure. Since #96 the shapes BigQuery itself flags as uninformative - row-level-
    # security-protected tables (always 0 bytes) and LOWER_BOUND accuracy - carry no dollar
    # figure and were refused above; the maximumBytesBilled derived here is the second layer
    # for the figures that do pass (UNKNOWN or unrecognised accuracy, absent statistics on a
    # statement that references no table, and the dry-run-versus-run gap in general). A
    # caller-supplied byte cap is only ever lowered, never raised; +inf means "no cap" and
    # derives nothing. (The dollar gate above has already refused a finite cap with no dollar
    # estimate, so a dollar estimate is guaranteed to exist whenever this derivation runs.)
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
                sql,
                max_bytes_billed=effective_max_bytes_billed,
                max_rows=max_rows,
                wrap_in_limit=wrap_in_limit,
            )
        elif engine == "snowflake":
            rows, row_count, row_cap_hit = snowflake_engine.execute_bounded(
                sql, warehouse=warehouse, max_rows=max_rows, wrap_in_limit=wrap_in_limit
            )
        elif engine == "databricks":
            rows, row_count, row_cap_hit = databricks_engine.execute_bounded(
                sql, warehouse=warehouse, max_rows=max_rows, wrap_in_limit=wrap_in_limit
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
        # executed=False: BigQuery evaluates maximumBytesBilled before running the job, so the
        # statement never ran even when a (failed) job id exists - DECISIONS.md #16.
        return BoundedQueryResult(
            status="refused",
            executed=False,
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
    normalised = _normalise_rows(rows)
    rows = normalised.rows
    # Carried in the existing nullable `hint` (None when nothing was capped): a new field
    # would widen the published outputSchema beyond the one property decision #13 adds.
    # `status` is and was the refusal discriminator, never `hint`.
    depth_note = _depth_note(normalised.depth_capped_cells)
    if depth_note is not None:
        logger.info(
            "engine=%s run_query_bounded depth_capped_cells=%s max_depth=%s",
            engine,
            normalised.depth_capped_cells,
            _MAX_CELL_NESTING_DEPTH,
        )

    # #94: the exact size measurement, on the normalised rows (what is measured is what is
    # sent). Checked before the row cap: the row-cap hint says "showing the first N", which
    # would be false once rows were dropped for size, so the size refusal wins and its hint
    # names the row cap when that was reached too. The longest prefix of whole rows that fits
    # is returned - zero rows when the first alone is over the cap.
    trimmed = trim_rows_to_byte_cap(rows)
    if trimmed.total_bytes > MAX_RESULT_BYTES:
        logger.info(
            "engine=%s run_query_bounded refused reason=%s result_bytes=%s max_result_bytes=%s "
            "rows_returned=%s rows_fetched=%s row_cap_hit=%s",
            engine,
            RefusalReason.RESULT_SIZE_EXCEEDED.value,
            trimmed.total_bytes,
            MAX_RESULT_BYTES,
            len(trimmed.rows),
            row_count,
            row_cap_hit,
        )
        return BoundedQueryResult(
            status="refused",
            executed=True,
            reason=RefusalReason.RESULT_SIZE_EXCEEDED,
            estimate=estimate,
            hint=_with_depth_note(
                size_cap_hint(
                    trimmed, fetched_rows=row_count, row_cap_hit=row_cap_hit, max_rows=max_rows
                ),
                depth_note,
            ),
            rows=trimmed.rows,
            row_count=len(trimmed.rows),
        )

    if row_cap_hit:
        if row_count < max_rows:
            # The engine stopped on its size estimate, yet the exact measurement is under the
            # cap: the estimator's lower bound was broken. Fail closed - a refusal with the
            # rows fetched and an honest hint - and make the inconsistency visible.
            logger.warning(
                "engine=%s run_query_bounded fetch stopped early: rows_fetched=%s max_rows=%s "
                "result_bytes=%s max_result_bytes=%s - the size estimate crossed the cap but "
                "the exact measurement did not; refusing with the rows fetched",
                engine,
                row_count,
                max_rows,
                trimmed.total_bytes,
                MAX_RESULT_BYTES,
            )
        logger.info(
            "engine=%s run_query_bounded refused reason=%s max_rows=%s row_count=%s",
            engine,
            RefusalReason.ROW_CAP_EXCEEDED.value,
            max_rows,
            row_count,
        )
        return BoundedQueryResult(
            status="refused",
            executed=True,
            reason=RefusalReason.ROW_CAP_EXCEEDED,
            estimate=estimate,
            hint=_with_depth_note(row_cap_hint(max_rows=max_rows, row_count=row_count), depth_note),
            rows=rows,
            row_count=row_count,
        )

    return BoundedQueryResult(
        status="ok",
        executed=True,
        estimate=estimate,
        rows=rows,
        row_count=row_count,
        hint=depth_note,
    )


def _with_depth_note(hint: str, depth_note: str | None) -> str:
    return hint if depth_note is None else f"{hint} {depth_note}"
