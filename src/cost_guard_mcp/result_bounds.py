"""Result bounds for ``run_query_bounded`` (#94): the default row cap, the serialised-size cap
and the engine-side early stop that keeps a fetch from materialising far more than the cap.

Two measurements live here, with one relationship between them:

- ``row_json_bytes`` / ``trim_rows_to_byte_cap`` is the EXACT measurement, taken in the tool
  on the normalised rows (the DECISIONS.md #17 choke point - the one place every cell is
  already JSON-shaped): compact JSON (``separators=(",", ":")``, ``ensure_ascii=False``,
  ``default=str``) encoded as UTF-8. It is the definition of the cap and the only source of
  truth for the ``result_size_exceeded`` refusal. It cannot raise on a normalised row: keys are
  ``str``, floats are finite, lone surrogates were already replaced, the pydantic-native types
  (Decimal, datetime, date, time, timedelta, UUID) go through ``default=str``, containers are
  fresh acyclic objects, and the final encode uses ``backslashreplace`` as a last line of
  defence.
- ``approx_row_size`` is a STRICT LOWER BOUND on that exact measurement, computed by the
  engines on connector-native rows as they arrive (``collect_rows``). It is an under-estimate
  guard: it lets an engine stop fetching once the exact measurement is guaranteed to exceed the
  cap, never earlier. It counts the framing JSON always renders (braces, brackets, key and
  string quotes, colons, commas), so for rows of bare primitives it is exact and the stop lands
  within one row of the cap; where it under-estimates, an engine may fetch more than the cap -
  up to about six times it in the worst case (text made of control characters, which JSON
  renders as six bytes each; 4/3 for binary; the constant placeholder for over-deep nests) -
  never less than what the exact measurement will refuse. ``tests/unit/test_result_bounds.py``
  pins the bound over the normaliser's whole type table; a new branch in ``_json_safe_value``
  must extend that table. Serialised bytes are not process memory: the Python objects behind
  narrow rows weigh ~25x their JSON size (DECISIONS.md #21), which is why the tool logs a
  WARNING above ``LARGE_MAX_ROWS_WARNING_THRESHOLD``.

Neither the estimate nor ``collect_rows`` decides anything the caller sees. ``collect_rows``
reports a hit whenever it stopped before the result was exhausted - the ``max_rows + 1``
sentinel row or the estimate crossing the cap - so that if the bound were ever broken (an
over-estimate; the one known theoretical case is a cell whose dict keys collide after
stringification) the tool still refuses rather than returning a silently truncated ``ok``.

The wire response the MCP SDK builds from these rows is larger than the measurement: it
renders the rows twice - compact in ``structuredContent`` and ``indent=2`` in a text block that
is JSON-string-escaped again inside the JSON-RPC frame - so the frame is roughly 2x to 3.5x
``total_bytes`` (narrow rows are the worst case).
"""

import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Applied by the tool when the caller omits max_rows (#94): an unbounded fetch is never the
# default. An explicit value up to the int64 bound in tools/run_query_bounded.py raises it.
DEFAULT_MAX_ROWS = 1000
# Above this explicit max_rows the tool logs one WARNING: the byte cap still holds, but the
# Python objects behind that many narrow rows (~25x their JSON size) and the normalise + trim
# pass over them cost memory and CPU after the billed execution, outside any watchdog.
LARGE_MAX_ROWS_WARNING_THRESHOLD = 100_000
# The exact cap on the compact-JSON size of the normalised rows list, in UTF-8 bytes. No
# opt-out: a model cannot usefully consume more per call, and splitting the query is the remedy.
MAX_RESULT_BYTES = 8 * 1024 * 1024
# Rows per fetchmany() / BigQuery page. Correctness never depends on it (both stops are checked
# row by row); it only sets how many round trips a result takes and how far past a stop the
# driver may already have buffered.
FETCH_PAGE_SIZE = 500
# pydantic_core's serialiser has a fixed recursion guard (~250 levels, counting the
# CallToolResult/structuredContent/rows/row envelope) and raises AFTER the billed execution
# (#105); 64 keeps the whole result an order of magnitude below it while far exceeding any real
# JSON/STRUCT depth (BigQuery STRUCTs stop at 15). The normaliser replaces a container at this
# depth with a constant placeholder; the estimator stops descending at the same depth, or a
# deep nest would be over-estimated against that placeholder.
MAX_CELL_NESTING_DEPTH = 64

# What compact JSON always renders around a value, counted so the lower bound is tight for
# narrow rows: the two braces of an object or brackets of an array, the two quotes of a string.
_CONTAINER_FRAMING_BYTES = 2
_QUOTE_BYTES = 2


def approx_row_size(row: Mapping[Any, object]) -> int:
    """A strict lower bound on ``row_json_bytes`` of the normalised form of ``row``.

    Counts the framing the compact form always renders - the braces, every key's quotes and
    colon, the comma between neighbours - plus, per value: ``None``/``bool``/``int``/``float``
    -> ``len(str(value))``, rendered bare (``True`` -> ``true``, ``None`` -> ``null``; a
    non-finite float becomes a quoted ``"nan"``, longer still); ``str`` -> its length plus the
    quotes (escapes and multibyte encoding only add bytes; the normaliser's backslashreplace
    only grows text); ``bytes``-like -> its length plus the quotes (base64 is 4/3 of it); a
    container -> its brackets plus the sum over its items and separators, keys estimated by
    the same per-type rule and always quoted (``str(b"\\x00")`` is ``b'\\x00'``, seven
    characters, where the normalised key is four characters of base64 - so never
    ``len(str(key))`` for bytes); a container ``MAX_CELL_NESTING_DEPTH`` levels below the cell
    -> the quotes of the placeholder string that replaces it; everything else ->
    ``len(str(value))`` plus the quotes, which is what the normaliser or ``default=str``
    renders. Exact for rows of bare primitives (``{"a": 1}`` -> 7); ~1/6 of the exact size for
    text made of control characters.
    """
    members = sum(_approx_key_size(key, 0) + _approx_size(cell, 0) for key, cell in row.items())
    return _CONTAINER_FRAMING_BYTES + members + _object_separator_bytes(len(row))


def _approx_size(value: object, depth: int) -> int:
    if value is None or isinstance(value, bool | int | float):
        return len(str(value))
    if isinstance(value, str | bytes | bytearray | memoryview):
        return len(value) + _QUOTE_BYTES
    if isinstance(value, dict | list | tuple | set | frozenset):
        if depth >= MAX_CELL_NESTING_DEPTH:
            return _QUOTE_BYTES
        if isinstance(value, dict):
            members = sum(
                _approx_key_size(key, depth) + _approx_size(item, depth + 1)
                for key, item in value.items()
            )
            return _CONTAINER_FRAMING_BYTES + members + _object_separator_bytes(len(value))
        items = sum(_approx_size(item, depth + 1) for item in value)
        return _CONTAINER_FRAMING_BYTES + items + max(0, len(value) - 1)
    return len(str(value)) + _QUOTE_BYTES


def _approx_key_size(key: object, depth: int) -> int:
    # Every key is rendered as a quoted string: the normaliser stringifies the ones that would
    # render bare as values, so those gain the quotes here; the rest are already counted quoted.
    if key is None or isinstance(key, bool | int | float):
        return len(str(key)) + _QUOTE_BYTES
    return _approx_size(key, depth)


def _object_separator_bytes(member_count: int) -> int:
    # One colon per member plus one comma between neighbours.
    return max(0, 2 * member_count - 1)


def collect_rows(
    rows: Iterable[Mapping[str, object]], max_rows: int | None
) -> tuple[list[dict[str, object]], bool]:
    """Consume ``rows`` one at a time into new dicts, stopping early on either cap.

    Returns ``(rows, stopped_early)``. The ``max_rows + 1``-th row (the sentinel the LIMIT
    wrap asks for) is detected before the size estimate and dropped: the caller gets exactly
    ``max_rows`` rows and ``True``. When the running estimate - each row's lower bound plus the
    one comma that separates it from its neighbour in the list form (the list's brackets stay
    uncounted, so the total is still a strict lower bound on what ``trim_rows_to_byte_cap``
    measures) - crosses ``MAX_RESULT_BYTES``, the rows so far (the last one pushed it over) and
    ``True`` come back - the tool's exact measurement will then exceed the cap too, so this stop
    never produces an ``ok``. ``False`` means the iterator was exhausted. The iterator's page
    size is irrelevant here: both checks run per row.
    """
    collected: list[dict[str, object]] = []
    estimate = 0
    for row in rows:
        collected.append(dict(row))
        if max_rows is not None and len(collected) > max_rows:
            return collected[:max_rows], True
        estimate += approx_row_size(collected[-1]) + 1
        if estimate > MAX_RESULT_BYTES:
            return collected, True
    return collected, False


def iter_fetchmany[T](
    fetchmany: Callable[[int], Sequence[T]],
    *,
    page_size: int = FETCH_PAGE_SIZE,
    between_pages: Callable[[int], None] | None = None,
) -> Iterator[T]:
    """Yield rows page by page from a DB-API ``fetchmany`` until it returns an empty page.

    A short page is NOT taken as the end - only an empty one is (no assumption about the
    driver; a fully fetched result costs one extra round trip). ``between_pages(fetched)`` is
    called before every page request after the first: the first page is never pre-empted
    (Snowflake's connector runs the RESULT_SCAN lazily inside it, and a finished, billed query
    always yields its first page); an exception from it propagates and no further page is
    requested. A consumer that stops iterating stops the paging too.
    """
    fetched = 0
    while True:
        if fetched and between_pages is not None:
            between_pages(fetched)
        page = fetchmany(page_size)
        if not page:
            return
        fetched += len(page)
        yield from page


def row_json_bytes(row: Mapping[str, object]) -> int:
    """UTF-8 bytes of ``row`` as compact JSON - the per-row unit of the exact measurement."""
    text = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(text.encode("utf-8", "backslashreplace"))


@dataclass(frozen=True)
class TrimmedRows:
    rows: list[dict[str, object]]
    total_bytes: int
    kept_bytes: int
    dropped_rows: int


def _framing_bytes(row_count: int) -> int:
    # "[" and "]" plus one comma between neighbours: the compact dump of a list of n rows is
    # exactly the sum of the per-row dumps plus this.
    return 2 + max(0, row_count - 1)


def trim_rows_to_byte_cap(
    rows: Sequence[dict[str, object]], cap: int = MAX_RESULT_BYTES
) -> TrimmedRows:
    """The longest prefix of ``rows`` whose compact-JSON list form fits in ``cap`` bytes.

    One ``json.dumps`` per row, once; never a partial row; zero rows when the first alone is
    over the cap. ``total_bytes`` is the size of the whole list (what the caller would have
    received), ``kept_bytes`` that of the prefix. Returns a new list in both cases.
    """
    sizes = [row_json_bytes(row) for row in rows]
    total_bytes = sum(sizes) + _framing_bytes(len(rows))
    if total_bytes <= cap:
        return TrimmedRows(
            rows=list(rows), total_bytes=total_bytes, kept_bytes=total_bytes, dropped_rows=0
        )
    kept = 0
    kept_payload = 0
    for size in sizes:
        if kept_payload + size + _framing_bytes(kept + 1) > cap:
            break
        kept_payload += size
        kept += 1
    return TrimmedRows(
        rows=list(rows[:kept]),
        total_bytes=total_bytes,
        kept_bytes=kept_payload + _framing_bytes(kept),
        dropped_rows=len(rows) - kept,
    )


def size_cap_hint(
    trimmed: TrimmedRows, *, fetched_rows: int, row_cap_hit: bool, max_rows: int
) -> str:
    """The ``result_size_exceeded`` hint - server-authored text, no remote data in it. Every
    clause is true whatever stopped the fetch: the row-cap clause appears only when the
    fetched count reached ``max_rows``, and nothing is claimed about rows nobody saw."""
    row_cap_clause = (
        f"; the row cap max_rows={max_rows} was reached too"
        if row_cap_hit and fetched_rows >= max_rows
        else ""
    )
    return (
        f"Query result is {trimmed.total_bytes} bytes serialised, over the {MAX_RESULT_BYTES}-byte "
        f"({MAX_RESULT_BYTES // (1024 * 1024)} MiB) result cap; showing the first "
        f"{len(trimmed.rows)} of the {fetched_rows} rows fetched{row_cap_clause}; rows beyond "
        f"the {fetched_rows} fetched, if any, were not retrieved. Add a LIMIT to your query, "
        "select fewer or narrower columns, or filter rows."
    )


def row_cap_hint(*, max_rows: int, row_count: int) -> str:
    """The ``row_cap_exceeded`` hint. ``row_count < max_rows`` is only reachable when the
    engine stopped on its size estimate and the exact measurement then came in under the cap
    (a broken lower bound): say so honestly instead of claiming max_rows was exceeded."""
    if row_count >= max_rows:
        return (
            f"Query returned more than max_rows={max_rows} rows; showing the first {row_count}. "
            "Add a LIMIT to your query or raise max_rows."
        )
    return (
        f"The fetch stopped after {row_count} rows, before max_rows={max_rows} was reached: the "
        f"engine's running size estimate crossed the {MAX_RESULT_BYTES}-byte result cap but the "
        "exact measurement did not, so rows may be missing. Add a LIMIT to your query or select "
        "fewer or narrower columns."
    )
