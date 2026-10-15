"""#105: result-cell nesting depth is capped before the mcp SDK serialises the result.

pydantic_core's serialiser has a fixed recursion guard (~250 levels, counting the
CallToolResult / structuredContent / rows / row envelope) and raised "Circular reference
detected (depth exceeded)" AFTER the billed execution, so the host received a protocol error
or "Error executing tool run_query_bounded" and the result was lost.
"""

import json
import logging
from unittest.mock import patch

import pydantic_core
from mcp.types import CallToolResult, TextContent

from cost_guard_mcp.tools.run_query_bounded import (
    _MAX_CELL_NESTING_DEPTH,
    _NESTING_DEPTH_PLACEHOLDER,
    _json_safe_rows,
    _normalise_rows,
    run_query_bounded,
)
from cost_guard_mcp.types import AccuracyTier, BoundedQueryResult, CostEstimate

_LOGGER_NAME = "cost_guard_mcp.tools.run_query_bounded"


def _nested(depth: int) -> dict:
    """The shape BigQuery's json_to_py produces for a JSON cell nested `depth` levels deep."""
    return json.loads('{"a":' * depth + "1" + "}" * depth)


def _descend(value, levels: int):
    for _ in range(levels):
        value = value["a"]
    return value


def _estimate():
    return CostEstimate(
        engine="bigquery",
        accuracy_tier=AccuracyTier.PRECISE,
        estimated_bytes=1,
        estimated_cost_usd=0.0,
    )


def test_normalise_rows_caps_a_300_level_cell_and_counts_it():
    normalised = _normalise_rows([{"j": _nested(300)}])

    cell = normalised.rows[0]["j"]
    assert _descend(cell, _MAX_CELL_NESTING_DEPTH) == _NESTING_DEPTH_PLACEHOLDER
    assert normalised.depth_capped_cells == 1
    # Serialisable through BOTH SDK steps (to_json for the text block, model_dump(mode="json")
    # for structuredContent) and through the runner's envelope dump, which at main failed at
    # depth 252 with "Circular reference detected".
    result = BoundedQueryResult(status="ok", rows=normalised.rows, row_count=1)
    dumped = result.model_dump(mode="json")
    assert dumped["rows"] == normalised.rows
    pydantic_core.to_json(result)
    envelope = CallToolResult(
        content=[TextContent(type="text", text="x")], structuredContent=dumped
    )
    envelope.model_dump(mode="json")


def test_depth_at_the_cap_is_intact_and_one_past_it_is_replaced():
    intact = _normalise_rows([{"j": _nested(_MAX_CELL_NESTING_DEPTH)}])
    assert _descend(intact.rows[0]["j"], _MAX_CELL_NESTING_DEPTH) == 1
    assert intact.depth_capped_cells == 0

    capped = _normalise_rows([{"j": _nested(_MAX_CELL_NESTING_DEPTH + 1)}])
    assert _descend(capped.rows[0]["j"], _MAX_CELL_NESTING_DEPTH) == _NESTING_DEPTH_PLACEHOLDER
    assert capped.depth_capped_cells == 1


def test_nested_lists_and_mixed_containers_are_capped_too():
    lists = json.loads("[" * 300 + "1" + "]" * 300)
    normalised = _normalise_rows([{"l": lists}])
    value = normalised.rows[0]["l"]
    for _ in range(_MAX_CELL_NESTING_DEPTH):
        value = value[0]
    assert value == _NESTING_DEPTH_PLACEHOLDER
    assert normalised.depth_capped_cells == 1

    mixed = json.loads('{"a":[' * 150 + "1" + "]}" * 150)
    assert _normalise_rows([{"m": mixed}]).depth_capped_cells == 1


def test_capped_cells_are_counted_across_rows_and_per_call():
    rows = [{"a": _nested(300), "b": _nested(300)}, {"a": _nested(300), "b": 1}]
    assert _normalise_rows(rows).depth_capped_cells == 3
    # The tally is local to the call: a second call never accumulates the first one's count.
    assert _normalise_rows([{"a": 1}]).depth_capped_cells == 0


def test_a_cell_with_several_capped_branches_counts_once():
    # hint, README and the log field promise a CELL count ("project a shallower path" is
    # per cell); a cell whose nested value has two over-deep branches is one cell, not two.
    deep = _nested(_MAX_CELL_NESTING_DEPTH + 1)
    assert _normalise_rows([{"j": {"x": deep, "y": deep}}]).depth_capped_cells == 1
    assert _normalise_rows([{"l": [deep, deep, deep]}]).depth_capped_cells == 1
    # Distinct cells still count separately.
    assert _normalise_rows([{"j": {"x": deep}, "k": [deep, deep]}]).depth_capped_cells == 2


def test_scalars_and_shallow_cells_are_untouched():
    normalised = _normalise_rows([{"n": 1, "s": "x", "j": _nested(3), "l": [1, [2, [3]]]}])
    assert normalised.rows == [{"n": 1, "s": "x", "j": _nested(3), "l": [1, [2, [3]]]}]
    assert normalised.depth_capped_cells == 0


def test_json_safe_rows_still_returns_plain_rows():
    rows = _json_safe_rows([{"j": _nested(300)}])
    assert _descend(rows[0]["j"], _MAX_CELL_NESTING_DEPTH) == _NESTING_DEPTH_PLACEHOLDER


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_ok_result_carries_the_depth_note_in_hint(mock_estimate, mock_engine, caplog):
    mock_estimate.return_value = _estimate()
    mock_engine.execute_bounded.return_value = ([{"j": _nested(300)}], 1, False)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = run_query_bounded("bigquery", "SELECT 1")

    assert result.status == "ok"
    assert result.hint is not None
    assert f"deeper than {_MAX_CELL_NESTING_DEPTH}" in result.hint
    assert _NESTING_DEPTH_PLACEHOLDER in result.hint
    assert "1 result cell" in result.hint
    assert _descend(result.rows[0]["j"], _MAX_CELL_NESTING_DEPTH) == _NESTING_DEPTH_PLACEHOLDER
    # One INFO record per call, counts only - never row content.
    records = [r.message for r in caplog.records if "depth_capped_cells" in r.message]
    assert records == [
        f"engine=bigquery run_query_bounded depth_capped_cells=1 max_depth={_MAX_CELL_NESTING_DEPTH}"
    ]


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_shallow_rows_leave_hint_none(mock_estimate, mock_engine):
    # Byte-identical output for every existing caller: hint stays null when nothing was capped.
    mock_estimate.return_value = _estimate()
    mock_engine.execute_bounded.return_value = ([{"j": _nested(3)}], 1, False)

    result = run_query_bounded("bigquery", "SELECT 1")

    assert result.status == "ok"
    assert result.hint is None


@patch("cost_guard_mcp.tools.run_query_bounded.bigquery_engine")
@patch("cost_guard_mcp.tools.run_query_bounded.estimate_query_cost")
def test_row_cap_refusal_keeps_its_hint_and_appends_the_depth_note(mock_estimate, mock_engine):
    mock_estimate.return_value = _estimate()
    mock_engine.execute_bounded.return_value = ([{"j": _nested(300)}], 1, True)

    result = run_query_bounded("bigquery", "SELECT 1", max_rows=1)

    assert result.status == "refused"
    assert result.hint is not None
    assert result.hint.startswith("Query returned more than max_rows=1")
    assert f"deeper than {_MAX_CELL_NESTING_DEPTH}" in result.hint
