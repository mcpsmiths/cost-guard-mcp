"""Row normalisation for ``run_query_bounded`` - the DECISIONS.md #17 choke point.

Every value an engine hands back is made JSON-serialisable, UTF-8-encodable and nested no
deeper than the MCP SDK's serialiser can take, AFTER the billed execution and before any
return path (``ok``, ``row_cap_exceeded``, ``result_size_exceeded``), so a billed run always
yields a structured result and never a post-billing serialisation error. The size cap (#94,
``result_bounds``) is measured on this module's output: what is measured is what is sent.
Split out of ``tools/run_query_bounded.py`` for the 500-line guideline; the tool imports only
what it uses, the tests import the normaliser from here.
"""

import base64
import datetime
import math
import uuid
from dataclasses import dataclass
from decimal import Decimal

from cost_guard_mcp.result_bounds import MAX_CELL_NESTING_DEPTH

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


# A container at this depth is replaced by the placeholder and counted into `hint` (#105; the
# rationale is on the constant in result_bounds, where the engines' size estimator must stop
# descending at the same depth). The alias keeps this module's name for its callers.
_MAX_CELL_NESTING_DEPTH = MAX_CELL_NESTING_DEPTH
_NESTING_DEPTH_PLACEHOLDER = f"<nested value omitted: deeper than {_MAX_CELL_NESTING_DEPTH} levels>"


@dataclass
class _NormalisationTally:
    """Per-cell accumulator of what the normaliser had to replace - created afresh for every
    cell by _normalise_cell, never shared, so counts never leak between cells or calls."""

    depth_capped_containers: int = 0


@dataclass(frozen=True)
class _NormalisedRows:
    rows: list[dict]
    depth_capped_cells: int


def _json_safe_value(value: object, depth: int, tally: _NormalisationTally) -> object:
    """Normalise one connector-native cell so the mcp SDK can always serialise it.

    Runs AFTER the billed query has executed, so it must never raise and must cover every
    engine's native row values: bytes-like cells (BigQuery BYTES, Snowflake/Databricks
    BINARY) become base64 text instead of tripping pydantic's default utf8 bytes mode;
    NaN/Inf become explicit strings instead of JSON null; str cells (and every text this
    function produces from a non-str value) pass through _json_safe_text so a lone surrogate
    cannot trip pydantic's strict UTF-8 str encoder; driver-specific types pydantic does not
    know (BigQuery INTERVAL -> dateutil relativedelta) are stringified instead of raising
    "Unable to serialize unknown type"; a container nested _MAX_CELL_NESTING_DEPTH levels
    below the cell (`depth` counts container levels from 0 at the cell itself) becomes the
    constant placeholder so pydantic_core's recursion guard is never reached. Returns new
    objects, never mutates its input.
    """
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return _json_safe_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict | list | tuple | set | frozenset):
        if depth >= _MAX_CELL_NESTING_DEPTH:
            tally.depth_capped_containers += 1
            return _json_safe_text(_NESTING_DEPTH_PLACEHOLDER)
        if isinstance(value, dict):
            return {
                _json_safe_text(str(_json_safe_value(key, depth, tally))): _json_safe_value(
                    item, depth + 1, tally
                )
                for key, item in value.items()
            }
        return [_json_safe_value(item, depth + 1, tally) for item in value]
    if isinstance(value, _PYDANTIC_NATIVE_CELL_TYPES):
        return value
    return _json_safe_text(str(value))


def _normalise_rows(rows: list[dict]) -> _NormalisedRows:
    """The one choke point for all three engines and every post-execution return path (ok,
    row_cap_exceeded and result_size_exceeded): every value placed in BoundedQueryResult.rows -
    column names included - is JSON-serialisable, UTF-8-encodable and nested no deeper than the
    SDK's serialiser can take, with a count of the cells that had to be capped. The size cap
    (#94) is measured on this function's output, so what is measured is what is sent. One pass
    with a running count: an intermediate list of (value, capped) pairs would be a second full
    copy of the result, held after the billed execution."""
    safe_rows: list[dict] = []
    depth_capped_cells = 0
    for row in rows:
        safe_row: dict = {}
        for key, item in row.items():
            safe, capped = _normalise_cell(item)
            safe_row[_json_safe_text(str(key))] = safe
            depth_capped_cells += capped
        safe_rows.append(safe_row)
    return _NormalisedRows(rows=safe_rows, depth_capped_cells=depth_capped_cells)


def _normalise_cell(value: object) -> tuple[object, bool]:
    """One cell normalised, and whether anything inside it was depth-capped. The count the
    caller reports is of cells, as hint, README and the log field say: a cell with two
    over-deep branches is one cell to re-project, not two."""
    tally = _NormalisationTally()
    return _json_safe_value(value, 0, tally), tally.depth_capped_containers > 0


def _json_safe_rows(rows: list[dict]) -> list[dict]:
    return _normalise_rows(rows).rows


def _depth_note(depth_capped_cells: int) -> str | None:
    """The row-level caveat for capped cells, carried in `hint` (None when nothing was
    capped, so every existing caller's output is byte-identical). Pure: the tool emits the
    matching INFO record (counts only) under its own logger."""
    if depth_capped_cells == 0:
        return None
    return (
        f"{depth_capped_cells} result cell(s) nested deeper than {_MAX_CELL_NESTING_DEPTH} "
        f"levels were replaced by the placeholder {_NESTING_DEPTH_PLACEHOLDER!r}; project a "
        "shallower path (a struct field, JSON_EXTRACT / GET_PATH) to read them."
    )
