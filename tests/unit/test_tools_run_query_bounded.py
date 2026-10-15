import asyncio
import base64
import datetime
import decimal
import json
import logging
import uuid
from unittest.mock import MagicMock, patch

import pytest
from pydantic_core import PydanticSerializationError

from cost_guard_mcp.engines.bigquery import ProviderByteCapExceeded, cost_cap_to_max_bytes_billed
from cost_guard_mcp.errors import UserVisibleError, as_tool_error
from cost_guard_mcp.result_bounds import (
    DEFAULT_MAX_ROWS,
    LARGE_MAX_ROWS_WARNING_THRESHOLD,
    MAX_RESULT_BYTES,
    trim_rows_to_byte_cap,
)
from cost_guard_mcp.sql_scope import ALLOW_MULTI_STATEMENT_SQL_ENV_VAR
from cost_guard_mcp.sql_scope_keywords import ALLOW_WRITE_STATEMENTS_ENV_VAR
from cost_guard_mcp.tools.result_rows import _json_safe_rows
from cost_guard_mcp.tools.run_query_bounded import _format_usd, run_query_bounded
from cost_guard_mcp.types import AccuracyTier, BoundedQueryResult, CostEstimate, RefusalReason

_RUN_QUERY_BOUNDED_LOGGER_NAME = "cost_guard_mcp.tools.run_query_bounded"
_ERRORS_LOGGER_NAME = "cost_guard_mcp.errors"


def _estimate(cost=None, bytes_=None):
    return CostEstimate(
        engine="bigquery",
        accuracy_tier=AccuracyTier.PRECISE,
        estimated_bytes=bytes_,
        estimated_cost_usd=cost,
    )


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_when_cost_cap_exceeded(mock_estimate, caplog):
    mock_estimate.return_value = _estimate(cost=10.00)
    with caplog.at_level(logging.INFO, logger=_RUN_QUERY_BOUNDED_LOGGER_NAME):
        result = run_query_bounded(
            "bigquery", "SELECT * FROM huge_table", max_estimated_cost_usd=1.00
        )
    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED
    assert result.estimate.estimated_cost_usd == 10.00
    assert "1.0" in result.hint or "1.00" in result.hint

    # "Refusal path" logging case: exactly one INFO record naming the engine and the
    # actual refusal reason, with no secret-looking content in it.
    records = [r for r in caplog.records if r.name == _RUN_QUERY_BOUNDED_LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelname == "INFO"
    assert "engine=bigquery" in records[0].message
    assert "reason=cost_cap_exceeded" in records[0].message


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_when_byte_cap_exceeded(mock_estimate):
    mock_estimate.return_value = _estimate(bytes_=10_000_000_000)
    result = run_query_bounded("bigquery", "SELECT * FROM t", max_bytes_billed=1_000_000)
    assert result.status == "refused"
    assert result.reason == RefusalReason.BYTE_CAP_EXCEEDED


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_fail_closed_when_cost_cap_requested_but_estimate_unavailable(mock_estimate):
    mock_estimate.return_value = _estimate(cost=None)
    result = run_query_bounded("bigquery", "SELECT * FROM t", max_estimated_cost_usd=1.00)
    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_no_dollar_estimate_hint_names_the_bigquery_shapes_only_for_bigquery(mock_estimate):
    # The engine-agnostic sentence must not tell a Snowflake/Databricks caller about BigQuery
    # dry runs; the BigQuery shapes and the max_bytes_billed remedy live in the BigQuery branch
    # (#96 fix-up review).
    mock_estimate.return_value = _estimate(cost=None)

    snowflake = run_query_bounded("snowflake", "SELECT * FROM t", max_estimated_cost_usd=1.00)
    bigquery = run_query_bounded("bigquery", "SELECT * FROM t", max_estimated_cost_usd=1.00)

    assert snowflake.status == "refused" and bigquery.status == "refused"
    assert snowflake.hint is not None and bigquery.hint is not None
    assert "caveats say why" in snowflake.hint
    assert "BigQuery" not in snowflake.hint and "LOWER_BOUND" not in snowflake.hint
    assert "row-level security" in bigquery.hint and "LOWER_BOUND" in bigquery.hint
    assert "max_bytes_billed" in bigquery.hint and "maximumBytesBilled" in bigquery.hint


@pytest.mark.parametrize("engine", ["bigquery", "snowflake"])
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_infinite_cost_cap_with_no_dollar_estimate_is_still_a_requested_cap_and_refuses(
    mock_estimate, mock_snowflake_execute, mock_bigquery_execute, engine
):
    # +inf compares as "no dollar cap" against any dollar figure and derives no server-side
    # byte cap (DECISIONS.md #16), but the parameter was given: with no dollar figure to compare
    # the fail-closed branch refuses as for any other dollar cap (#5). Base already did this for
    # a capacity-billed project; #96 widened the set of estimates without a dollar figure, and
    # the #96 fix-up review found the README/#16 sentinel wording overstated - pinned here and
    # documented in #22 (owner may still choose to make +inf skip this branch).
    mock_estimate.return_value = _estimate(cost=None, bytes_=0)

    result = run_query_bounded(engine, "SELECT * FROM t", max_estimated_cost_usd=float("inf"))

    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED
    assert result.executed is False
    mock_snowflake_execute.assert_not_called()
    mock_bigquery_execute.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_fail_closed_when_byte_cap_requested_but_estimate_unavailable(mock_estimate):
    mock_estimate.return_value = _estimate(bytes_=None)
    result = run_query_bounded("bigquery", "SELECT * FROM t", max_bytes_billed=1_000_000)
    assert result.status == "refused"
    assert result.reason == RefusalReason.BYTE_CAP_EXCEEDED


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_succeeds_when_under_all_caps(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded(
        "bigquery", "SELECT 1", max_estimated_cost_usd=1.00, max_bytes_billed=1_000_000, max_rows=10
    )

    assert result.status == "ok"
    assert result.row_count == 1
    assert result.rows == [{"a": 1}]


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_after_execution_when_row_cap_hit(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}, {"a": 2}], 2, True)  # row_cap_hit=True

    result = run_query_bounded("bigquery", "SELECT * FROM t", max_rows=2)

    assert result.status == "refused"
    assert result.reason == RefusalReason.ROW_CAP_EXCEEDED
    assert result.row_count == 2
    assert result.rows == [{"a": 1}, {"a": 2}]  # still returns the (capped) rows it did fetch


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_run_query_bounded_dispatches_to_databricks(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("databricks", "SELECT 1")

    mock_engine.execute_bounded.assert_called_once_with(
        "SELECT 1", warehouse=None, max_rows=DEFAULT_MAX_ROWS, wrap_in_limit=True
    )
    assert result.status == "ok"


@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_run_query_bounded_threads_warehouse_size_and_edition_to_cap_check(
    mock_estimate, mock_engine
):
    # Regression test: warehouse_size/edition must reach the cap-check estimate, or
    # max_estimated_cost_usd is always checked against the smallest warehouse's rate
    # regardless of which warehouse the query actually runs on - silently under-enforcing
    # the cap for a caller who specified a larger one.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([], 0, False)

    run_query_bounded(
        "snowflake",
        "SELECT 1",
        warehouse="WH",
        warehouse_size="X6LARGE",
        edition="enterprise",
    )

    mock_estimate.assert_called_once_with(
        "snowflake", "SELECT 1", "WH", "X6LARGE", "enterprise", allow_warehouse_wake=True
    )


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_run_query_bounded_estimate_is_allowed_to_wake_the_warehouse(mock_estimate, mock_engine):
    # DECISIONS.md #20: the read-only warehouse-state guards do not apply here - the execution
    # that follows wakes the warehouse anyway, so the cap gate is fed the full-lookup estimate.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([], 0, False)

    run_query_bounded("bigquery", "SELECT 1")

    assert mock_estimate.call_args.kwargs == {"allow_warehouse_wake": True}


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_run_query_bounded_rejects_unsupported_engine(mock_estimate):
    # estimate_query_cost has its OWN identical engine-dispatch else that raises for an
    # invalid engine BEFORE this function's own dispatch block is ever reached - mock it
    # to a non-refusing estimate so execution actually reaches this function's own else
    # branch instead of raising from the wrong function and passing for the wrong reason.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)

    with pytest.raises(UserVisibleError, match="is not yet supported"):
        run_query_bounded("redshift", "SELECT 1")


_MULTI_STATEMENT_SQL = "SELECT 1; DELETE FROM big_t WHERE TRUE"


@pytest.mark.parametrize("engine", ["bigquery", "snowflake", "databricks"])
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_multi_statement_text_before_any_estimate_or_execute(
    mock_estimate, mock_bigquery, mock_snowflake, mock_databricks, engine
):
    # The cost cap prices the submitted text as ONE unit; a second statement riding behind
    # a top-level ';' would execute under an estimate that never covered it. The pin must
    # fire before the estimate (which itself costs a dry run / EXPLAIN) and before dispatch.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)

    with pytest.raises(UserVisibleError, match="exactly one SQL statement"):
        run_query_bounded(engine, _MULTI_STATEMENT_SQL, max_estimated_cost_usd=1.0)

    mock_estimate.assert_not_called()
    for engine_module in (mock_bigquery, mock_snowflake, mock_databricks):
        engine_module.execute_bounded.assert_not_called()


@pytest.mark.parametrize("engine", ["bigquery", "snowflake", "databricks"])
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_a_write_statement_before_any_estimate_or_execute(
    mock_estimate, mock_bigquery, mock_snowflake, mock_databricks, engine
):
    # DECISIONS.md #13: the read gate runs right after the single-statement gate (the
    # multi-statement shape above is still refused by THAT contract, proving the order) and
    # before the estimate and the dispatch; the refusal names the operator opt-out.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)

    with pytest.raises(UserVisibleError, match="only read statements") as excinfo:
        run_query_bounded(engine, "DELETE FROM big_t WHERE TRUE", max_estimated_cost_usd=1.0)

    assert "COST_GUARD_ALLOW_WRITE_STATEMENTS=1" in str(excinfo.value)
    mock_estimate.assert_not_called()
    for engine_module in (mock_bigquery, mock_snowflake, mock_databricks):
        engine_module.execute_bounded.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_every_result_carries_the_constant_rows_provenance(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=10.0, bytes_=1000)
    refused = run_query_bounded("bigquery", "SELECT 1", max_estimated_cost_usd=1.0)
    assert refused.status == "refused"
    assert refused.rows_provenance == "warehouse_data_untrusted"

    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}], 1, False)
    ok = run_query_bounded("bigquery", "SELECT 1")
    assert ok.status == "ok"
    assert ok.rows_provenance == "warehouse_data_untrusted"


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_single_statement_with_trailing_semicolon_still_runs(mock_estimate, mock_engine):
    # Positive control for the pin: the existing LIMIT-wrapper contract strips one
    # trailing ';', so that shape must keep flowing through to the engine.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("bigquery", "SELECT 1;", max_rows=5)

    assert result.status == "ok"
    mock_engine.execute_bounded.assert_called_once()


# --- BigQuery server-side byte cap derived from the dollar cap -------------------------------


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_bigquery_cost_cap_attaches_a_derived_server_side_byte_cap(mock_estimate, mock_execute):
    # A $0.00 PRECISE figure the gate admits (a statement that references no table, or any
    # figure below the cap): the provider must still enforce the dollar cap via a
    # maximumBytesBilled derived from it. (Since #96 the row-level-security shape no longer
    # reaches this point - its dry run carries no dollar figure and the gate refuses it; see
    # test_bigquery_dollar_cap_refuses_rls_and_lower_bound_dry_runs_before_dispatch.)
    mock_estimate.return_value = _estimate(cost=0.0, bytes_=0)
    mock_execute.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("bigquery", "SELECT * FROM rls_t", max_estimated_cost_usd=0.01)

    assert result.status == "ok"
    mock_execute.assert_called_once_with(
        "SELECT * FROM rls_t",
        max_bytes_billed=cost_cap_to_max_bytes_billed(0.01),
        max_rows=DEFAULT_MAX_ROWS,
        wrap_in_limit=True,
    )
    assert mock_execute.call_args.kwargs["max_bytes_billed"] is not None


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_bigquery_both_caps_use_the_smaller_byte_cap(mock_estimate, mock_execute):
    mock_estimate.return_value = _estimate(cost=0.00001, bytes_=1000)
    mock_execute.return_value = ([], 0, False)

    # Derived (cost cap 0.0001 -> ~17 MiB) is smaller than the caller's 1 GB: derived wins.
    run_query_bounded(
        "bigquery", "SELECT 1", max_bytes_billed=1_000_000_000, max_estimated_cost_usd=0.0001
    )
    assert mock_execute.call_args.kwargs["max_bytes_billed"] == cost_cap_to_max_bytes_billed(0.0001)

    # Derived (cost cap 1.0 -> ~164 GiB) is larger than the caller's 50 MB: the caller's
    # own byte cap is never raised.
    mock_execute.reset_mock()
    run_query_bounded(
        "bigquery", "SELECT 1", max_bytes_billed=50_000_000, max_estimated_cost_usd=1.0
    )
    assert mock_execute.call_args.kwargs["max_bytes_billed"] == 50_000_000


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_bigquery_without_a_cost_cap_passes_the_caller_byte_cap_unchanged(
    mock_estimate, mock_execute
):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([], 0, False)

    run_query_bounded("bigquery", "SELECT 1")
    assert mock_execute.call_args.kwargs["max_bytes_billed"] is None  # no cap invented

    mock_execute.reset_mock()
    run_query_bounded("bigquery", "SELECT 1", max_bytes_billed=123_456_789)
    assert mock_execute.call_args.kwargs["max_bytes_billed"] == 123_456_789


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_bigquery_infinite_cost_cap_means_no_cap_and_derives_no_byte_cap(
    mock_estimate, mock_execute
):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([], 0, False)

    result = run_query_bounded("bigquery", "SELECT 1", max_estimated_cost_usd=float("inf"))

    assert result.status == "ok"
    assert mock_execute.call_args.kwargs["max_bytes_billed"] is None


@pytest.mark.parametrize("engine", ["snowflake", "databricks"])
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_derived_byte_cap_is_bigquery_only(mock_estimate, mock_snowflake, mock_databricks, engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_snowflake.execute_bounded.return_value = ([], 0, False)
    mock_databricks.execute_bounded.return_value = ([], 0, False)

    run_query_bounded(engine, "SELECT 1", max_estimated_cost_usd=1.0)

    engine_module = mock_snowflake if engine == "snowflake" else mock_databricks
    engine_module.execute_bounded.assert_called_once_with(
        "SELECT 1", warehouse=None, max_rows=DEFAULT_MAX_ROWS, wrap_in_limit=True
    )


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_provider_byte_cap_rejection_of_a_derived_cap_is_a_cost_cap_refusal(
    mock_estimate, mock_execute
):
    # BigQuery rejected the job against the maximumBytesBilled we derived from the dollar
    # cap: per DECISIONS.md #4 that is a normal refusal result, not a ToolError.
    mock_estimate.return_value = _estimate(cost=0.0, bytes_=0)
    derived = cost_cap_to_max_bytes_billed(0.01)
    mock_execute.side_effect = ProviderByteCapExceeded(derived)

    result = run_query_bounded("bigquery", "SELECT * FROM rls_t", max_estimated_cost_usd=0.01)

    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED
    assert result.estimate is not None
    assert result.rows is None
    assert "maximumBytesBilled" in result.hint
    assert str(derived) in result.hint


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_provider_byte_cap_rejection_of_the_caller_byte_cap_is_a_byte_cap_refusal(
    mock_estimate, mock_execute
):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.side_effect = ProviderByteCapExceeded(50_000_000)

    result = run_query_bounded("bigquery", "SELECT 1", max_bytes_billed=50_000_000)

    assert result.status == "refused"
    assert result.reason == RefusalReason.BYTE_CAP_EXCEEDED
    assert "maximumBytesBilled" in result.hint


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_provider_byte_cap_rejection_with_no_cap_attached_is_not_attributed_to_the_caller(
    mock_estimate, mock_execute
):
    # Neither a caller byte cap nor a finite dollar cap was given, so no maximumBytesBilled
    # of ours was attached: a bytesBilledLimitExceeded rejection here is a provider-side
    # event the caller did not ask for. It must surface as the SanitizedEngineError it is
    # (-> ToolError), not as a refusal blaming "your max_bytes_billed=None".
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.side_effect = ProviderByteCapExceeded(None)

    with pytest.raises(ProviderByteCapExceeded):
        run_query_bounded("bigquery", "SELECT 1")

    mock_execute.reset_mock()
    with pytest.raises(ProviderByteCapExceeded):
        run_query_bounded("bigquery", "SELECT 1", max_estimated_cost_usd=float("inf"))


# --- #96: dry runs without a usable byte figure carry no dollar figure, so a dollar cap refuses


def _dry_run_job(total_bytes_processed, accuracy="PRECISE", referenced_tables=1, location="US"):
    job = MagicMock()
    job.total_bytes_processed = total_bytes_processed
    job._properties = {"statistics": {"query": {"totalBytesProcessedAccuracy": accuracy}}}
    job.referenced_tables = [MagicMock() for _ in range(referenced_tables)]
    job.location = location
    return job


_RLS_SHAPED_JOB = _dry_run_job(total_bytes_processed=0, accuracy="PRECISE", referenced_tables=1)
_LOWER_BOUND_JOB = _dry_run_job(total_bytes_processed=500, accuracy="LOWER_BOUND")
_UNPRICEABLE_SHAPES = pytest.mark.parametrize(
    "dry_run_job", [_RLS_SHAPED_JOB, _LOWER_BOUND_JOB], ids=["rls_zero_bytes", "lower_bound"]
)


def _bigquery_client_returning(mock_bq_module, dry_run_job):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = dry_run_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()
    return mock_client


@_UNPRICEABLE_SHAPES
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_bigquery_dollar_cap_refuses_rls_and_lower_bound_dry_runs_before_dispatch(
    mock_bq_module, _mock_capacity, mock_execute, dry_run_job, caplog
):
    # Through the real dry_run: the RLS shape (0 bytes on a statement that references a table)
    # and a LOWER_BOUND accuracy produce no dollar figure, so the existing fail-closed dollar
    # gate (DECISIONS.md #5) refuses before anything is submitted - executed is False and the
    # engine's execute_bounded is never called (#96, DECISIONS.md #22).
    _bigquery_client_returning(mock_bq_module, dry_run_job)

    with caplog.at_level(logging.INFO, logger=_RUN_QUERY_BOUNDED_LOGGER_NAME):
        result = run_query_bounded("bigquery", "SELECT * FROM t", max_estimated_cost_usd=1.00)

    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED
    assert result.executed is False
    assert result.rows is None
    assert result.estimate is not None
    assert result.estimate.estimated_cost_usd is None
    assert result.estimate.accuracy_tier == AccuracyTier.HEURISTIC
    assert "max_bytes_billed" in result.hint and "maximumBytesBilled" in result.hint
    mock_execute.assert_not_called()
    [record] = [r for r in caplog.records if r.name == _RUN_QUERY_BOUNDED_LOGGER_NAME]
    assert "reason=cost_cap_exceeded" in record.message
    assert "detail=no_cost_estimate_available" in record.message


@_UNPRICEABLE_SHAPES
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_bigquery_byte_cap_alone_still_runs_rls_and_lower_bound_dry_runs(
    mock_bq_module, _mock_capacity, mock_execute, dry_run_job
):
    # The documented way to run such a query under a cap: max_bytes_billed. The reported byte
    # figure (0 / the floor) admits it at the gate and the cap travels to BigQuery as
    # maximumBytesBilled, which the service enforces before running the job (DECISIONS.md #16).
    _bigquery_client_returning(mock_bq_module, dry_run_job)
    mock_execute.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("bigquery", "SELECT * FROM t", max_bytes_billed=10_000_000)

    assert result.status == "ok"
    assert result.executed is True
    assert result.estimate is not None and result.estimate.estimated_cost_usd is None
    mock_execute.assert_called_once()
    assert mock_execute.call_args.kwargs["max_bytes_billed"] == 10_000_000


@pytest.mark.parametrize(
    "dry_run_job",
    [
        _dry_run_job(total_bytes_processed=500, accuracy="UNKNOWN"),
        _dry_run_job(total_bytes_processed=500, accuracy="PRECISE"),
    ],
    ids=["unknown_accuracy", "precise"],
)
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_bigquery_dollar_cap_still_admits_unknown_accuracy_and_precise_dry_runs(
    mock_bq_module, _mock_capacity, mock_execute, dry_run_job
):
    # Unchanged by #96: these shapes keep a dollar figure, the gate compares it and the derived
    # maximumBytesBilled rides along.
    _bigquery_client_returning(mock_bq_module, dry_run_job)
    mock_execute.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("bigquery", "SELECT * FROM t", max_estimated_cost_usd=1.00)

    assert result.status == "ok"
    assert result.estimate is not None and result.estimate.estimated_cost_usd is not None
    mock_execute.assert_called_once()
    assert mock_execute.call_args.kwargs["max_bytes_billed"] == cost_cap_to_max_bytes_billed(1.00)


# --- non-finite caps (plan-review amendment A2) -------------------------------------------


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_nan_cost_cap_is_rejected_before_any_estimate_or_execute(mock_estimate, mock_engine):
    # `estimate > nan` is False for every estimate, so a NaN cap would admit anything with
    # no cap at all; the mcp SDK's JSON parser accepts a bare NaN literal.
    mock_estimate.return_value = _estimate(cost=10.0, bytes_=1000)

    with pytest.raises(UserVisibleError, match="finite"):
        run_query_bounded("snowflake", "SELECT 1", max_estimated_cost_usd=float("nan"))

    mock_estimate.assert_not_called()
    mock_engine.execute_bounded.assert_not_called()


# --- JSON-safe result rows -------------------------------------------------------------------


def test_json_safe_rows_encodes_bytes_like_cells_as_base64_text():
    raw = b"\xff\x00cost"
    expected = base64.b64encode(raw).decode("ascii")
    rows = _json_safe_rows([{"b": raw, "ba": bytearray(raw), "mv": memoryview(raw)}])
    assert rows == [{"b": expected, "ba": expected, "mv": expected}]


def test_json_safe_rows_stringifies_non_finite_floats_and_keeps_finite_ones():
    rows = _json_safe_rows(
        [{"nan": float("nan"), "inf": float("inf"), "ninf": float("-inf"), "f": 1.5}]
    )
    assert rows == [{"nan": "nan", "inf": "inf", "ninf": "-inf", "f": 1.5}]


def test_json_safe_rows_leaves_json_primitives_and_pydantic_native_types_untouched():
    when = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    cell_values = {
        "none": None,
        "t": True,
        "i": 7,
        "s": "x",
        "dec": decimal.Decimal("1.50"),
        "dt": when,
        "d": when.date(),
        "tm": when.time(),
        "td": datetime.timedelta(hours=1),
        "u": uuid.UUID(int=1),
    }
    rows = _json_safe_rows([cell_values])
    assert rows == [cell_values]
    assert rows[0]["dec"] is cell_values["dec"]


def test_json_safe_rows_recurses_into_struct_array_and_map_cells():
    nested = {"struct": {"inner": b"\x01", "list": [b"\x02", {"deep": float("nan")}]}}
    rows = _json_safe_rows([nested])
    assert rows == [
        {
            "struct": {
                "inner": base64.b64encode(b"\x01").decode("ascii"),
                "list": [base64.b64encode(b"\x02").decode("ascii"), {"deep": "nan"}],
            }
        }
    ]


def test_json_safe_rows_turns_tuple_and_set_cells_into_lists_and_non_string_keys_into_strings():
    rows = _json_safe_rows([{"tup": (1, 2), "set": frozenset({3}), "m": {("a", "b"): 1, 2: 2}}])
    assert rows == [{"tup": [1, 2], "set": [3], "m": {"['a', 'b']": 1, "2": 2}}]


def test_json_safe_rows_stringifies_driver_native_types_pydantic_cannot_serialise():
    class IntervalLike:
        def __str__(self) -> str:
            return "relativedelta(months=+1)"

    rows = _json_safe_rows([{"interval": IntervalLike()}])
    assert rows == [{"interval": "relativedelta(months=+1)"}]


def test_json_safe_rows_returns_new_rows_without_mutating_the_input():
    original = [{"b": b"\x00"}]
    snapshot = [dict(row) for row in original]
    _json_safe_rows(original)
    assert original == snapshot


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_ok_result_rows_are_normalised_after_dispatch(mock_estimate, mock_execute):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([{"b": b"\xff"}], 1, False)

    result = run_query_bounded("bigquery", "SELECT 1")

    assert result.rows == [{"b": base64.b64encode(b"\xff").decode("ascii")}]


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_row_cap_refusal_rows_are_normalised_too(mock_estimate, mock_execute):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([{"b": b"\xff"}], 1, True)

    result = run_query_bounded("bigquery", "SELECT 1", max_rows=1)

    assert result.status == "refused"
    assert result.rows == [{"b": base64.b64encode(b"\xff").decode("ascii")}]


# Regression tests for the cooperative pre-dispatch cancellation hook (audit fingerprint
# client-side-only-query-deadline-orphaned-on-process-exit). server.py binds
# `before_execute` to anyio.from_thread.check_cancelled so an MCP cancel that arrived while
# the estimate was running never starts the billable execution; direct callers (and these
# tests, except where stated) leave it at its default None.


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_before_execute_called_exactly_once_after_caps_and_before_execute(
    mock_estimate, mock_engine
):
    manager = MagicMock()
    manager.estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    manager.execute.return_value = ([{"a": 1}], 1, False)
    mock_estimate.side_effect = manager.estimate
    mock_engine.execute_bounded.side_effect = manager.execute

    result = run_query_bounded(
        "bigquery",
        "SELECT 1",
        max_estimated_cost_usd=1.00,
        max_bytes_billed=1_000_000,
        before_execute=manager.hook,
    )

    assert result.status == "ok"
    assert manager.hook.call_count == 1
    assert [c[0] for c in manager.mock_calls] == ["estimate", "hook", "execute"]


@pytest.mark.parametrize(
    ("estimate", "caps"),
    [
        (_estimate(cost=10.00), {"max_estimated_cost_usd": 1.00}),
        (_estimate(bytes_=10_000_000_000), {"max_bytes_billed": 1_000_000}),
        (_estimate(cost=None), {"max_estimated_cost_usd": 1.00}),
        (_estimate(bytes_=None), {"max_bytes_billed": 1_000_000}),
    ],
    ids=["cost_cap_exceeded", "byte_cap_exceeded", "cost_fail_closed", "byte_fail_closed"],
)
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_before_execute_not_called_when_a_cap_refuses(mock_estimate, mock_engine, estimate, caps):
    mock_estimate.return_value = estimate
    hook = MagicMock()

    result = run_query_bounded("bigquery", "SELECT * FROM t", before_execute=hook, **caps)

    assert result.status == "refused"
    hook.assert_not_called()
    mock_engine.execute_bounded.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_before_execute_raising_prevents_dispatch_and_propagates(mock_estimate, mock_engine):
    # asyncio.CancelledError is what anyio.from_thread.check_cancelled raises; it is a
    # BaseException, so this also proves the hook is not wrapped in any except Exception.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    hook = MagicMock(side_effect=asyncio.CancelledError("cancelled via cancel scope"))

    with pytest.raises(asyncio.CancelledError):
        run_query_bounded("bigquery", "SELECT 1", before_execute=hook)

    mock_engine.execute_bounded.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_before_execute_defaults_to_none_and_is_keyword_only(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_engine.execute_bounded.return_value = ([], 0, False)

    assert run_query_bounded("bigquery", "SELECT 1").status == "ok"

    # Positional use must be rejected: the 8 positional tool parameters are the public
    # contract server.py forwards, and the hook must never be confusable with one of them.
    with pytest.raises(TypeError):
        run_query_bounded("bigquery", "SELECT 1", None, None, None, None, None, None, MagicMock())


# --- end to end: the Snowflake cost cap is enforced at the observed warehouse size ----------


def _observed_warehouse_cursor(snowflake_fakes, size):
    # The audit's verifier-05 fixture: only the connector entry point is faked; the SHOW
    # WAREHOUSES row describes a Gen2/AWS STANDARD warehouse of the given size.
    return snowflake_fakes.cursor(
        plan={
            "GlobalStats": {
                "partitionsTotal": 10,
                "partitionsAssigned": 4,
                "bytesAssigned": 2_500_000,
            }
        },
        show_rows=[snowflake_fakes.show_row("ANALYTICS_XL", size, generation="2")],
        region="AWS_US_EAST_1",
    )


def test_snowflake_cost_cap_is_enforced_at_the_observed_warehouse_size(
    snowflake_fakes, snowflake_config
):
    # warehouse_size omitted against a 4X-Large warehouse under a 0.10 USD cap: the old code
    # priced at XSMALL (0.0225 USD), returned status 'ok' labelled UPPER_BOUND and executed.
    cursor = _observed_warehouse_cursor(snowflake_fakes, "4X-Large")
    with (
        patch(
            "cost_guard_mcp.engines.snowflake._connect",
            return_value=snowflake_fakes.connection(cursor),
        ),
        patch("cost_guard_mcp.engines.snowflake.execute_bounded") as mock_execute,
    ):
        result = run_query_bounded(
            "snowflake", "SELECT * FROM t", warehouse="ANALYTICS_XL", max_estimated_cost_usd=0.10
        )

    assert result.status == "refused"
    assert result.reason == RefusalReason.COST_CAP_EXCEEDED
    assert result.estimate.estimated_cost_usd > 0.10
    mock_execute.assert_not_called()


def test_snowflake_cost_cap_still_admits_a_query_on_a_small_observed_warehouse(
    snowflake_fakes, snowflake_config
):
    # Positive control: the same call against an X-Small warehouse stays under the cap and
    # runs, so the refusal above is driven by the observed size, not by the new code path.
    cursor = _observed_warehouse_cursor(snowflake_fakes, "X-Small")
    with (
        patch(
            "cost_guard_mcp.engines.snowflake._connect",
            return_value=snowflake_fakes.connection(cursor),
        ),
        patch("cost_guard_mcp.engines.snowflake.execute_bounded") as mock_execute,
    ):
        mock_execute.return_value = ([], 0, False)
        result = run_query_bounded(
            "snowflake", "SELECT * FROM t", warehouse="ANALYTICS_XL", max_estimated_cost_usd=0.10
        )

    assert result.status == "ok"
    assert result.estimate.estimated_cost_usd < 0.10
    mock_execute.assert_called_once()


# --- runtime-verification follow-ups (PR #86 adversarial probes P6/P13) --------------------


@pytest.mark.parametrize("max_rows", [0, -1, -2])
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_rejects_max_rows_below_one_before_estimating_or_executing(mock_estimate, max_rows):
    # max_rows=0/-1 used to dispatch `LIMIT 0` / `LIMIT -1` to the provider and then report
    # a row-cap *refusal* after the billed execution, contradicting the tool description.
    with pytest.raises(UserVisibleError, match="max_rows"):
        run_query_bounded("bigquery", "SELECT 1", max_rows=max_rows)
    mock_estimate.assert_not_called()


@pytest.mark.parametrize("max_bytes_billed", [0, -1, 2**63, 2**70])
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_rejects_byte_caps_outside_the_int64_range_before_estimating(
    mock_estimate, max_bytes_billed
):
    with pytest.raises(UserVisibleError, match="max_bytes_billed"):
        run_query_bounded("bigquery", "SELECT 1", max_bytes_billed=max_bytes_billed)
    mock_estimate.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_cost_cap_refusal_hint_shows_a_tiny_cap_exactly(mock_estimate):
    # `.4f` rendered max_estimated_cost_usd=0.00001 as "$0.0000", hiding the real value.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    result = run_query_bounded("bigquery", "SELECT 1", max_estimated_cost_usd=0.00001)
    assert result.status == "refused"
    assert "1e-05" in result.hint
    assert "$0.0000" not in result.hint


# --- runtime-verification follow-up (PR #86 adversarial probe-4 p11): lone surrogates -------
# A str cell is not automatically UTF-8: Python's surrogateescape decoding (and BigQuery text
# built from non-UTF-8 source bytes) yields lone surrogates such as \udcff, and pydantic_core
# serialises str through a strict UTF-8 encoder. The normaliser must turn such text into its
# visible escape instead of letting the SDK raise after the billed execution.

_LONE_SURROGATE_TEXT = "ok\udcffend"


def test_lone_surrogate_text_is_not_utf8_encodable():
    # Control for the tests below: the fake BigQuery backend handed back exactly this cell,
    # and the SDK's result serialiser refused it after 'tool=run_query_bounded
    # outcome=success' had already been logged (evidence/probe-4/p11-rows-surrogate.*).
    with pytest.raises(UnicodeEncodeError):
        _LONE_SURROGATE_TEXT.encode("utf-8")


def test_json_safe_rows_escapes_lone_surrogates_in_str_cells_and_column_names():
    rows = _json_safe_rows([{"a\udc80": _LONE_SURROGATE_TEXT, "m": {"k\udc81": "v\udc82"}}])
    assert rows == [{"a\\udc80": "ok\\udcffend", "m": {"k\\udc81": "v\\udc82"}}]


def test_json_safe_rows_leaves_ordinary_text_untouched():
    rows = _json_safe_rows([{"s": "héllo ✓", "e": ""}])
    assert rows == [{"s": "héllo ✓", "e": ""}]


def test_bounded_query_result_with_normalised_surrogate_rows_serialises_through_pydantic():
    raw_rows = [{"a\udc80": _LONE_SURROGATE_TEXT}]
    # Negative control: the un-normalised row still trips the real pydantic path, so the
    # assertion below exercises the normaliser and not a serialiser that turned lenient.
    with pytest.raises(PydanticSerializationError, match="utf-8"):
        BoundedQueryResult(status="ok", executed=True, rows=raw_rows, row_count=1).model_dump_json()

    dumped = BoundedQueryResult(
        status="ok", executed=True, rows=_json_safe_rows(raw_rows), row_count=1
    ).model_dump_json()

    assert json.loads(dumped)["rows"] == [{"a\\udc80": "ok\\udcffend"}]


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_surrogate_row_text_reaches_the_caller_escaped_with_a_success_outcome(
    mock_estimate, mock_execute, caplog
):
    # probe-4 p11: stderr said outcome=success and the model then got a bare 'Error
    # executing tool run_query_bounded' because the SDK could not encode the rows. The
    # tool keeps its promise of never raising after the billed query: the rows come back
    # with the surrogate as six visible characters and the outcome record still reads
    # success - the same path, not a refusal or an error in its place.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([{"s": _LONE_SURROGATE_TEXT, "n": 1}], 1, False)
    tool = as_tool_error(run_query_bounded)

    with caplog.at_level(logging.INFO, logger=_ERRORS_LOGGER_NAME):
        result = tool("bigquery", "SELECT 1", max_rows=5)

    assert result.status == "ok"
    assert result.rows == [{"s": "ok\\udcffend", "n": 1}]
    result.model_dump_json()  # the SDK's serialisation step, which used to raise here
    records = [r for r in caplog.records if r.name == _ERRORS_LOGGER_NAME]
    assert [r.message.split(" elapsed_ms=")[0] for r in records] == [
        "tool=run_query_bounded outcome=success"
    ]


# --- runtime-verification follow-through (PR #86 adversarial probe-2 p01c/p06) ---------------
# The refusal-hint formatter and the int64 byte-cap validation landed together; the formatter
# had no direct test and the cap's accepted edge was asserted only by refusal of 2**63.


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (1.0, "1.0000"),
        (0, "0.0000"),
        (0.00001, "1e-05"),
        (-0.00001, "-1e-05"),
        (1e300, "1e+300"),
    ],
    ids=["ordinary", "zero", "rounds_to_zero", "negative_rounds_to_zero", "huge"],
)
def test_format_usd_keeps_fixed_point_unless_it_would_hide_the_value(amount, expected):
    # Fixed four decimals for the values they render faithfully (including a true zero);
    # the shortest significant form only when `.4f` would show 0.0000 for a non-zero amount
    # or a wall of digits for a huge one.
    assert _format_usd(amount) == expected


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_byte_cap_int64_maximum_is_accepted_and_one_above_is_refused(mock_estimate, mock_execute):
    # maximumBytesBilled is an int64 API field: its maximum must reach the engine unchanged,
    # and the first value past it must be refused before any estimate, naming the parameter.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([], 0, False)

    result = run_query_bounded("bigquery", "SELECT 1", max_bytes_billed=2**63 - 1)

    assert result.status == "ok"
    assert mock_execute.call_args.kwargs["max_bytes_billed"] == 2**63 - 1

    mock_estimate.reset_mock()
    with pytest.raises(UserVisibleError, match="max_bytes_billed"):
        run_query_bounded("bigquery", "SELECT 1", max_bytes_billed=2**63)
    mock_estimate.assert_not_called()


@pytest.mark.parametrize("max_rows", [2**63 - 1, 2**63, 2**64, 10**30])
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_rejects_max_rows_above_int64_before_estimating(mock_estimate, max_rows):
    # The wrapper dispatches LIMIT max_rows + 1, so the largest admissible value is 2**63 - 2
    # (round-3 verification: 2**63 - 1 still produced LIMIT 9223372036854775808).
    # PR #86 delta verification: max_rows had no upper bound while max_bytes_billed was
    # bounded to int64, so 'LIMIT 9223372036854775808' was dispatched after the estimate ran.
    with pytest.raises(UserVisibleError, match="max_rows"):
        run_query_bounded("bigquery", "SELECT 1", max_rows=max_rows)
    mock_estimate.assert_not_called()


@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_accepts_the_largest_max_rows_whose_limit_literal_fits_int64(mock_estimate):
    mock_estimate.return_value = _estimate(cost=0.0001)
    with patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded") as execute:
        execute.return_value = ([{"a": 1}], 1, False)
        result = run_query_bounded("bigquery", "SELECT 1", max_rows=2**63 - 2)
    assert result.status == "ok"
    assert execute.call_args.kwargs["max_rows"] == 2**63 - 2


# --- #94: default row cap, LIMIT wrap rule, serialised-size cap ------------------------------


def _wide_rows(count, cell_bytes):
    return [{"s": "x" * cell_bytes, "n": index} for index in range(count)]


@pytest.mark.parametrize("engine", ["bigquery", "snowflake", "databricks"])
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_omitted_max_rows_dispatches_the_default_row_cap_to_every_engine(
    mock_estimate, mock_bigquery, mock_snowflake, mock_databricks, engine
):
    # The audit's reproduction: no max_rows used to mean no row bound at all (100,000 rows
    # came back). The default is applied in the tool, so the inputSchema is unchanged.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    modules = {
        "bigquery": mock_bigquery,
        "snowflake": mock_snowflake,
        "databricks": mock_databricks,
    }
    for module in modules.values():
        module.execute_bounded.return_value = ([], 0, False)

    result = run_query_bounded(engine, "SELECT 1")

    kwargs = modules[engine].execute_bounded.call_args.kwargs
    assert kwargs["max_rows"] == DEFAULT_MAX_ROWS == 1000
    assert kwargs["wrap_in_limit"] is True
    assert result.status == "ok"


@pytest.mark.parametrize("max_rows", [1, 5000, 2**63 - 2])
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_explicit_max_rows_is_honoured_unchanged(mock_estimate, mock_execute, max_rows):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([], 0, False)

    run_query_bounded("bigquery", "SELECT 1", max_rows=max_rows)

    assert mock_execute.call_args.kwargs["max_rows"] == max_rows


@pytest.mark.parametrize(
    ("max_rows", "expect_warning"),
    [
        (None, False),
        (LARGE_MAX_ROWS_WARNING_THRESHOLD, False),
        (LARGE_MAX_ROWS_WARNING_THRESHOLD + 1, True),
    ],
    ids=["default", "at_threshold", "above_threshold"],
)
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_explicit_max_rows_above_the_threshold_logs_one_warning(
    mock_estimate, mock_execute, max_rows, expect_warning, caplog
):
    # The byte cap is still exact and honoured, but for narrow rows the Python objects behind
    # an explicit max_rows in the millions cost far more memory and post-billing CPU than the
    # serialised bytes suggest (DECISIONS.md #21). The operator gets one WARNING, counts only.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([], 0, False)

    with caplog.at_level(logging.WARNING, logger=_RUN_QUERY_BOUNDED_LOGGER_NAME):
        result = run_query_bounded("bigquery", "SELECT 1", max_rows=max_rows)

    assert result.status == "ok"
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    if expect_warning:
        assert warnings == [
            (
                f"engine=bigquery run_query_bounded explicit max_rows={max_rows} exceeds "
                f"{LARGE_MAX_ROWS_WARNING_THRESHOLD}: narrow rows at this count can hold hundreds "
                "of MB and cost seconds of CPU after the billed execution"
            )
        ]
    else:
        assert warnings == []


@pytest.mark.parametrize(
    ("engine", "sql", "expected"),
    [
        ("databricks", "SHOW TABLES", False),
        ("databricks", "DESCRIBE t", False),
        ("databricks", "DESC t", False),
        ("databricks", "EXPLAIN SELECT 1", False),
        ("bigquery", "SELECT 1", True),
        ("bigquery", "(SELECT 1)", True),
        ("bigquery", "WITH t AS (SELECT 1) SELECT * FROM t", True),
        ("snowflake", "VALUES (1)", True),
        ("bigquery", "SELECT 1;", True),
        ("bigquery", "-- lead\nSELECT 1 -- trail", True),
    ],
)
@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.snowflake_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_limit_wrap_applies_only_to_query_shaped_statements(
    mock_estimate, mock_bigquery, mock_snowflake, mock_databricks, engine, sql, expected
):
    # SHOW/DESCRIBE/EXPLAIN are not valid as a derived table: with the default row cap they
    # would otherwise be unusable (there is no "no max_rows" any more). Their row cap is the
    # paged fetch's stop instead.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    modules = {
        "bigquery": mock_bigquery,
        "snowflake": mock_snowflake,
        "databricks": mock_databricks,
    }
    for module in modules.values():
        module.execute_bounded.return_value = ([], 0, False)

    run_query_bounded(engine, sql)

    kwargs = modules[engine].execute_bounded.call_args.kwargs
    assert kwargs["wrap_in_limit"] is expected
    assert kwargs["max_rows"] == DEFAULT_MAX_ROWS


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_limit_wrap_is_skipped_for_a_script_under_the_multi_statement_opt_out(
    mock_estimate, mock_execute, monkeypatch
):
    # The documented opt-out configuration (README: BigQuery scripts) keeps working with the
    # default row cap: the script runs unwrapped and the fetch stops at max_rows + 1.
    monkeypatch.setenv(ALLOW_MULTI_STATEMENT_SQL_ENV_VAR, "1")
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([], 0, False)

    run_query_bounded("bigquery", "SELECT 1; SELECT 2")

    assert mock_execute.call_args.kwargs["wrap_in_limit"] is False
    assert mock_execute.call_args.kwargs["max_rows"] == DEFAULT_MAX_ROWS


@patch("cost_guard_mcp.tools.run_query_bounded.databricks_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_limit_wrap_is_skipped_for_a_with_insert_under_the_write_opt_out(
    mock_estimate, mock_execute, monkeypatch
):
    # Spark allows WITH ... INSERT; the read gate is skipped under the opt-out, and wrapping it
    # would be invalid SQL - the first keyword alone (WITH) is not the wrap criterion.
    monkeypatch.setenv(ALLOW_WRITE_STATEMENTS_ENV_VAR, "1")
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([], 0, False)

    run_query_bounded("databricks", "WITH t AS (SELECT 1) INSERT INTO x SELECT * FROM t")

    assert mock_execute.call_args.kwargs["wrap_in_limit"] is False


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_refuses_with_result_size_exceeded_and_the_longest_fitting_prefix(
    mock_estimate, mock_execute, caplog
):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    rows = _wide_rows(10, 1024 * 1024)  # ~1 MiB each: seven fit under 8 MiB, the eighth does not
    mock_execute.return_value = (rows, 10, True)  # the engine's size estimate stopped the fetch

    with caplog.at_level(logging.INFO, logger=_RUN_QUERY_BOUNDED_LOGGER_NAME):
        result = run_query_bounded("bigquery", "SELECT * FROM wide")

    assert result.status == "refused"
    assert result.reason == RefusalReason.RESULT_SIZE_EXCEEDED
    expected = trim_rows_to_byte_cap(_json_safe_rows(rows))
    assert result.rows == expected.rows
    assert result.row_count == len(result.rows) == 7
    assert f"{MAX_RESULT_BYTES}-byte (8 MiB)" in result.hint
    assert "first 7 of the 10 rows fetched" in result.hint
    assert "LIMIT" in result.hint
    records = [r for r in caplog.records if r.name == _RUN_QUERY_BOUNDED_LOGGER_NAME]
    assert len(records) == 1
    message = records[0].message
    assert "engine=bigquery" in message
    assert "reason=result_size_exceeded" in message
    assert f"result_bytes={expected.total_bytes}" in message
    assert f"max_result_bytes={MAX_RESULT_BYTES}" in message
    assert "rows_returned=7 rows_fetched=10" in message


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_first_row_alone_over_the_cap_returns_zero_rows(mock_estimate, mock_execute):
    # The audit's second reproduction: one 8.4 MB cell, an 8.4 MB response. Never a partial
    # row - the honest answer is zero rows and a hint that says what to narrow.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([{"blob": "x" * 8_400_000}], 1, True)

    result = run_query_bounded("bigquery", "SELECT blob FROM t")

    assert result.status == "refused"
    assert result.reason == RefusalReason.RESULT_SIZE_EXCEEDED
    assert result.rows == []
    assert result.row_count == 0
    assert "first 0 of the 1 rows fetched" in result.hint
    assert "narrower columns" in result.hint
    assert len(result.model_dump_json()) < 2_000


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_size_cap_precedes_the_row_cap_and_the_hint_names_both(mock_estimate, mock_execute):
    # Both tripped: the row-cap hint ("showing the first 1000") would be false once rows were
    # dropped for size, so the size reason wins and its hint names the row cap too.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    rows = _wide_rows(1000, 9_000)  # 1000 rows x ~9 KB = ~9 MB
    mock_execute.return_value = (rows, 1000, True)

    result = run_query_bounded("bigquery", "SELECT * FROM t", max_rows=1000)

    assert result.reason == RefusalReason.RESULT_SIZE_EXCEEDED
    assert result.row_count < 1000
    assert "the row cap max_rows=1000 was reached too" in result.hint
    assert "rows beyond the 1000 fetched, if any, were not retrieved" in result.hint


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_result_exactly_at_the_size_cap_is_ok(mock_estimate, mock_execute):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    row = {"s": "x" * (MAX_RESULT_BYTES - 10)}  # '[{"s":"..."}]' is exactly the cap
    mock_execute.return_value = ([row], 1, False)

    result = run_query_bounded("bigquery", "SELECT 1")

    assert result.status == "ok"
    assert result.row_count == 1
    assert result.hint is None


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_size_cap_is_measured_after_normalisation(mock_estimate, mock_execute):
    # 6.5 MB of raw bytes is under the cap; its base64 form (what the caller receives) is
    # ~8.7 MB and is what gets measured - the cap is on the normalised rows (DECISIONS.md #17).
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([{"blob": b"\xff" * 6_500_000}], 1, False)

    result = run_query_bounded("bigquery", "SELECT blob FROM t")

    assert result.status == "refused"
    assert result.reason == RefusalReason.RESULT_SIZE_EXCEEDED
    assert result.rows == []


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_depth_note_is_appended_on_the_size_path(mock_estimate, mock_execute):
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    deep = json.loads('{"a":' * 300 + "1" + "}" * 300)
    mock_execute.return_value = ([{"j": deep}, {"s": "x" * MAX_RESULT_BYTES}], 2, True)

    result = run_query_bounded("bigquery", "SELECT 1")

    assert result.reason == RefusalReason.RESULT_SIZE_EXCEEDED
    assert result.row_count == 1
    assert "deeper than 64" in result.hint
    assert result.hint.index("8 MiB") < result.hint.index("deeper than 64")


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_row_cap_refusal_with_fewer_rows_than_max_rows_says_the_fetch_stopped_early(
    mock_estimate, mock_execute, caplog
):
    # Fail-closed fallback: the engine stopped on its size estimate (row_cap_hit=True) but the
    # exact measurement came in under the cap. Only a broken lower bound gets here; the result
    # is still a refusal, with an honest hint and one WARNING, never a silently truncated ok.
    mock_estimate.return_value = _estimate(cost=0.01, bytes_=1000)
    mock_execute.return_value = ([{"a": 1}] * 7, 7, True)

    with caplog.at_level(logging.INFO, logger=_RUN_QUERY_BOUNDED_LOGGER_NAME):
        result = run_query_bounded("bigquery", "SELECT 1", max_rows=1000)

    assert result.status == "refused"
    assert result.reason == RefusalReason.ROW_CAP_EXCEEDED
    assert result.row_count == 7
    assert "stopped after 7 rows" in result.hint
    assert "may be missing" in result.hint
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "rows_fetched=7 max_rows=1000" in warnings[0].message


# --- #101: `executed` on every return path ---------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "estimate", "engine_result", "expected_reason", "expected_executed"),
    [
        (
            {"max_estimated_cost_usd": 1.0},
            _estimate(cost=10.0),
            None,
            RefusalReason.COST_CAP_EXCEEDED,
            False,
        ),
        (
            {"max_estimated_cost_usd": 1.0},
            _estimate(cost=None),
            None,
            RefusalReason.COST_CAP_EXCEEDED,
            False,
        ),
        (
            {"max_bytes_billed": 10},
            _estimate(bytes_=100),
            None,
            RefusalReason.BYTE_CAP_EXCEEDED,
            False,
        ),
        (
            {"max_bytes_billed": 10},
            _estimate(bytes_=None),
            None,
            RefusalReason.BYTE_CAP_EXCEEDED,
            False,
        ),
        (
            {"max_bytes_billed": 10},
            _estimate(bytes_=5),
            ProviderByteCapExceeded(10),
            RefusalReason.BYTE_CAP_EXCEEDED,
            False,
        ),
        (
            {"max_rows": 1},
            _estimate(cost=0.01),
            ([{"a": 1}], 1, True),
            RefusalReason.ROW_CAP_EXCEEDED,
            True,
        ),
        (
            {},
            _estimate(cost=0.01),
            ([{"s": "x" * 9_000_000}], 1, True),
            RefusalReason.RESULT_SIZE_EXCEEDED,
            True,
        ),
        ({}, _estimate(cost=0.01), ([{"a": 1}], 1, False), None, True),
    ],
    ids=[
        "cost_cap",
        "cost_cap_no_estimate",
        "byte_cap",
        "byte_cap_no_estimate",
        "provider_byte_cap",
        "row_cap",
        "result_size",
        "ok",
    ],
)
@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine.execute_bounded")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_executed_states_whether_the_warehouse_ran_the_statement_on_every_path(
    mock_estimate, mock_execute, kwargs, estimate, engine_result, expected_reason, expected_executed
):
    # #101: "refused" used to be described as "did NOT run" while row_cap_exceeded (and now
    # result_size_exceeded) follow a billed execution. The provider byte-cap refusal is False:
    # BigQuery evaluates maximumBytesBilled before running the job (DECISIONS.md #16).
    mock_estimate.return_value = estimate
    if isinstance(engine_result, BaseException):
        mock_execute.side_effect = engine_result
    elif engine_result is not None:
        mock_execute.return_value = engine_result

    result = run_query_bounded("bigquery", "SELECT 1", **kwargs)

    assert result.reason == expected_reason
    assert result.status == ("ok" if expected_reason is None else "refused")
    assert result.executed is expected_executed
    assert result.model_dump(mode="json")["executed"] is expected_executed
    if not expected_executed:
        if engine_result is None:
            mock_execute.assert_not_called()
    else:
        mock_execute.assert_called_once()
