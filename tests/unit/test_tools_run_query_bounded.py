import base64
import datetime
import decimal
import logging
import uuid
from unittest.mock import patch

import pytest

from cost_guard_mcp.engines.bigquery import ProviderByteCapExceeded, cost_cap_to_max_bytes_billed
from cost_guard_mcp.errors import UserVisibleError
from cost_guard_mcp.tools.run_query_bounded import _json_safe_rows, run_query_bounded
from cost_guard_mcp.types import AccuracyTier, CostEstimate, RefusalReason

_RUN_QUERY_BOUNDED_LOGGER_NAME = "cost_guard_mcp.tools.run_query_bounded"


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
