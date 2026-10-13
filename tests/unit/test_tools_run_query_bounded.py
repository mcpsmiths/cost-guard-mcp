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
from cost_guard_mcp.tools.run_query_bounded import _format_usd, _json_safe_rows, run_query_bounded
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

    mock_engine.execute_bounded.assert_called_once_with("SELECT 1", warehouse=None, max_rows=None)
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

    mock_estimate.assert_called_once_with("snowflake", "SELECT 1", "WH", "X6LARGE", "enterprise")


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
    # The RLS shape: BigQuery's dry run reports 0 bytes / $0.00 PRECISE for a row-level-
    # security-protected table by design. The gate admits it; the provider must still
    # enforce the dollar cap via maximumBytesBilled derived from it.
    mock_estimate.return_value = _estimate(cost=0.0, bytes_=0)
    mock_execute.return_value = ([{"a": 1}], 1, False)

    result = run_query_bounded("bigquery", "SELECT * FROM rls_t", max_estimated_cost_usd=0.01)

    assert result.status == "ok"
    mock_execute.assert_called_once_with(
        "SELECT * FROM rls_t",
        max_bytes_billed=cost_cap_to_max_bytes_billed(0.01),
        max_rows=None,
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
    engine_module.execute_bounded.assert_called_once_with("SELECT 1", warehouse=None, max_rows=None)


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
        BoundedQueryResult(status="ok", rows=raw_rows, row_count=1).model_dump_json()

    dumped = BoundedQueryResult(
        status="ok", rows=_json_safe_rows(raw_rows), row_count=1
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
