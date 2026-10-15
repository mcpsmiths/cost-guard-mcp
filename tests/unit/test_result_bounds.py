"""result_bounds (#94): the engines' size estimator is a strict lower bound on the tool's exact
measurement, the collector stops early on either cap, the paged fetch ends at the first empty
page, and the exact trim never splits a row."""

import datetime
import decimal
import itertools
import json
import uuid

import pytest
from dateutil.relativedelta import relativedelta

from cost_guard_mcp.result_bounds import (
    DEFAULT_MAX_ROWS,
    FETCH_PAGE_SIZE,
    MAX_CELL_NESTING_DEPTH,
    MAX_RESULT_BYTES,
    approx_row_size,
    collect_rows,
    iter_fetchmany,
    row_cap_hint,
    row_json_bytes,
    size_cap_hint,
    trim_rows_to_byte_cap,
)
from cost_guard_mcp.tools.run_query_bounded import _MAX_CELL_NESTING_DEPTH, _json_safe_rows


def _nested(depth):
    value = 1
    for _ in range(depth):
        value = {"a": value}
    return value


def _compact_bytes(rows):
    return len(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


# Every shape tools/run_query_bounded._json_safe_value (the DECISIONS.md #17 normaliser)
# distinguishes. A new branch there must add a row here: the engines' early stop is only safe
# while the lower bound holds for every cell type the normaliser can produce.
_TYPE_TABLE = [
    {"s": "ascii"},
    {"s": "héllo ✓ 日本"},
    {"s": "\x00\x01\x1f"},  # control characters: JSON escapes each one as six bytes
    {"s": "ok\udcffend"},  # lone surrogate: the normaliser backslashreplaces it
    {"s": ""},
    {"i": 0},
    {"i": -12345678901234567890},
    {"f": 1.5},
    {"f": -0.0},
    {"f": 1e300},
    {"f": float("nan")},
    {"f": float("inf")},
    {"f": float("-inf")},
    {"b": True},
    {"b": False},
    {"n": None},
    {"by": b"\xff\x00\xfe"},
    {"by": bytearray(b"abc")},
    {"by": memoryview(b"xyz")},
    {"by": b""},
    {"d": decimal.Decimal("1.500")},
    {"dt": datetime.datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=datetime.UTC)},
    {"date": datetime.date(2024, 1, 2)},
    {"t": datetime.time(3, 4, 5)},
    {"td": datetime.timedelta(days=1, seconds=5)},
    {"u": uuid.UUID("12345678-1234-5678-1234-567812345678")},
    {"rd": relativedelta(months=1)},
    {"dict": {"k": [1, 2, {"z": "y"}], 7: None, b"\x00": "bytes key"}},
    {"list": [1, "two", 3.0, None, [4, [5]]]},
    {"tuple": (1, 2)},
    {"set": {1}},
    {"frozenset": frozenset({"a"})},
    {"deep": _nested(300)},
    {"deep_exact": _nested(MAX_CELL_NESTING_DEPTH)},
    {},
    {"many": "x" * 10_000, "cols": 12, 3: "int key", None: "none key"},
]
_TYPE_TABLE_IDS = [str(index) for index in range(len(_TYPE_TABLE))]


def test_constants_are_the_documented_defaults():
    assert DEFAULT_MAX_ROWS == 1000
    assert MAX_RESULT_BYTES == 8 * 1024 * 1024 == 8388608
    assert FETCH_PAGE_SIZE == 500
    # The tool keeps its alias so existing imports (and the placeholder text) are unchanged.
    assert _MAX_CELL_NESTING_DEPTH == MAX_CELL_NESTING_DEPTH == 64


# --- approx_row_size: a strict lower bound on the exact normalised size ----------------------


@pytest.mark.parametrize("row", _TYPE_TABLE, ids=_TYPE_TABLE_IDS)
def test_approx_row_size_never_exceeds_the_exact_normalised_size(row):
    assert approx_row_size(row) <= row_json_bytes(_json_safe_rows([row])[0])


def test_approx_row_size_under_estimates_control_character_text_by_up_to_six_times():
    # The documented worst case: every control character is one estimated byte and six
    # serialised ones, so an engine may fetch up to ~6x the cap before the stop fires.
    row = {"s": "\x1f" * 1000}
    estimate = approx_row_size(row)
    exact = row_json_bytes(_json_safe_rows([row])[0])
    # 1000 estimated text bytes + the braces, the key's and the value's quotes and the colon.
    assert estimate == 1000 + 2 + 3 + 2 + 1
    assert exact >= 6 * 1000


def test_approx_row_size_is_exact_for_narrow_rows_of_json_primitives():
    # The reviewer's case: a {"a": 1} row was estimated at 3 bytes against 7 exact, so the
    # engine-side stop let ~2.8M narrow rows through under an explicit large max_rows. The
    # framing JSON always renders - braces, key quotes, colons, commas, value quotes - is
    # counted now, so the estimate is exact for rows of bare JSON primitives.
    for row in ({"a": 1}, {"a": 1, "b": True, "c": None, "d": -0.5}, {}, {"k": "v"}):
        assert approx_row_size(row) == row_json_bytes(row)
    assert approx_row_size({"a": 1}) == 7


def test_collect_rows_estimate_counts_one_inter_row_comma_and_stays_a_lower_bound(monkeypatch):
    # With the cap patched to 80 bytes: each {"a": 1} row is 7 bytes plus its inter-row comma,
    # so the running estimate crosses 80 on the 11th row (88); the exact list form of those 11
    # rows is 89 bytes, so the tool's exact measurement refuses too (lower bound intact), and
    # the stop lands within one row of the exact crossing instead of ~2.7x past it.
    from cost_guard_mcp import result_bounds

    monkeypatch.setattr(result_bounds, "MAX_RESULT_BYTES", 80)
    rows_iter, seen = _counting([{"a": index % 10} for index in range(1000)])
    rows, row_cap_hit = collect_rows(rows_iter, max_rows=None)
    assert len(rows) == len(seen) == 11
    assert row_cap_hit is True
    exact = trim_rows_to_byte_cap(rows, cap=80).total_bytes
    assert exact == 89
    assert exact > 80
    # One row fewer and the estimate (80) would not have stopped, while the exact size (81)
    # is already over: the estimate never exceeds the exact size, it may only trail it.
    assert trim_rows_to_byte_cap(rows[:10], cap=80).total_bytes == 81


def test_collect_rows_framing_estimate_is_a_lower_bound_over_the_type_table():
    # What collect_rows accumulates (per-row estimate + one comma per row) never exceeds the
    # exact compact list form the tool measures, for every cell type the normaliser produces.
    estimate = sum(approx_row_size(row) for row in _TYPE_TABLE) + len(_TYPE_TABLE)
    assert estimate <= trim_rows_to_byte_cap(_json_safe_rows(_TYPE_TABLE)).total_bytes


def test_approx_row_size_stops_descending_where_the_normaliser_truncates():
    # Without the depth bound a 300-level nest would be over-estimated (one byte per level)
    # against the constant placeholder the normaliser puts at level 64.
    # A container 64 levels below the cell is the first one replaced; from there on the
    # estimate no longer grows (the innermost scalar is gone too).
    assert (
        approx_row_size({"deep": _nested(300)})
        == approx_row_size({"deep": _nested(65)})
        == approx_row_size({"deep": _nested(1000)})
    )
    assert approx_row_size({"deep": _nested(300)}) < row_json_bytes(
        _json_safe_rows([{"deep": _nested(300)}])[0]
    )


# --- row_json_bytes / trim_rows_to_byte_cap: the exact measurement ----------------------------


def test_row_json_bytes_never_raises_on_anything_the_normaliser_produces():
    rows = _json_safe_rows(_TYPE_TABLE)
    trimmed = trim_rows_to_byte_cap(rows)
    assert trimmed.dropped_rows == 0
    assert trimmed.total_bytes == trimmed.kept_bytes == _compact_bytes(rows)


def test_row_json_bytes_counts_utf8_bytes_not_characters():
    assert row_json_bytes({"s": "日本"}) == row_json_bytes({"s": "xx"}) + 4


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"a": 1}],
        [{"a": 1}, {"b": "héllo"}],
        [{"n": index, "s": "x" * index} for index in range(50)],
        _json_safe_rows(_TYPE_TABLE),
    ],
    ids=["empty", "one", "two", "fifty", "type_table"],
)
def test_trim_framing_arithmetic_equals_the_compact_list_dump(rows):
    assert trim_rows_to_byte_cap(rows).total_bytes == _compact_bytes(rows)


def test_trim_keeps_everything_under_the_cap():
    rows = [{"a": index} for index in range(10)]
    trimmed = trim_rows_to_byte_cap(rows)
    assert trimmed.rows == rows
    assert trimmed.rows is not rows  # a new list, never the caller's
    assert trimmed.dropped_rows == 0
    assert trimmed.kept_bytes == trimmed.total_bytes


def test_trim_exactly_at_the_cap_keeps_everything():
    row = {"s": "x" * (MAX_RESULT_BYTES - 10)}  # '[{"s":"' + x*n + '"}]' is n + 10 bytes
    trimmed = trim_rows_to_byte_cap([row])
    assert trimmed.total_bytes == MAX_RESULT_BYTES
    assert trimmed.rows == [row]
    assert trimmed.dropped_rows == 0


def test_trim_drops_the_row_that_crosses_the_cap_and_never_splits_it():
    first = {"s": "x" * (MAX_RESULT_BYTES - 20)}
    second = {"a": 1234567890}
    trimmed = trim_rows_to_byte_cap([first, second])
    assert trimmed.total_bytes > MAX_RESULT_BYTES
    assert trimmed.rows == [first]
    assert trimmed.dropped_rows == 1
    assert trimmed.kept_bytes == _compact_bytes([first]) <= MAX_RESULT_BYTES


def test_trim_first_row_alone_over_the_cap_keeps_zero_rows():
    rows = [{"s": "x" * MAX_RESULT_BYTES}, {"a": 1}]
    trimmed = trim_rows_to_byte_cap(rows)
    assert trimmed.rows == []
    assert trimmed.dropped_rows == 2
    assert trimmed.kept_bytes == 2  # "[]"


# --- collect_rows: the engines' early stop ---------------------------------------------------


def _counting(rows):
    seen = []

    def generate():
        for row in rows:
            seen.append(row)
            yield row

    return generate(), seen


def test_collect_rows_stops_at_the_sentinel_row_and_trims_to_max_rows():
    rows_iter, seen = _counting([{"a": index} for index in range(10)])
    rows, row_cap_hit = collect_rows(rows_iter, max_rows=3)
    assert rows == [{"a": 0}, {"a": 1}, {"a": 2}]
    assert row_cap_hit is True
    assert len(seen) == 4  # max_rows + 1: the sentinel, nothing past it


def test_collect_rows_exactly_max_rows_is_not_a_hit():
    rows, row_cap_hit = collect_rows(iter([{"a": 1}, {"a": 2}]), max_rows=2)
    assert rows == [{"a": 1}, {"a": 2}]
    assert row_cap_hit is False


def test_collect_rows_exhausted_before_the_cap_is_not_a_hit():
    rows, row_cap_hit = collect_rows(iter([{"a": 1}] * 5), max_rows=1000)
    assert len(rows) == 5
    assert row_cap_hit is False


def test_collect_rows_stops_on_the_size_estimate_before_the_row_cap():
    big = {"s": "x" * (1024 * 1024 - 100)}  # ~1 MiB per row: nine cross the 8 MiB cap
    rows_iter, seen = _counting([big] * 100)
    rows, row_cap_hit = collect_rows(rows_iter, max_rows=1000)
    assert len(rows) == 9
    assert len(seen) == 9  # the iterator is left where it stopped
    # A size stop is reported as a hit: the fetch ended before the result was exhausted, so the
    # tool refuses even if the exact measurement were ever to come in under the cap.
    assert row_cap_hit is True


def test_collect_rows_checks_the_sentinel_before_the_size_estimate():
    huge = {"s": "x" * MAX_RESULT_BYTES}
    rows, row_cap_hit = collect_rows(iter([{"a": 1}, {"a": 2}, huge]), max_rows=2)
    assert rows == [{"a": 1}, {"a": 2}]  # the sentinel row is dropped, not kept for size
    assert row_cap_hit is True


def test_collect_rows_without_max_rows_stops_only_on_size():
    small, _ = collect_rows(iter([{"a": 1}] * 5000), max_rows=None)
    assert len(small) == 5000
    big = {"s": "x" * (1024 * 1024 - 100)}
    rows, row_cap_hit = collect_rows(iter([big] * 100), max_rows=None)
    assert len(rows) == 9
    assert row_cap_hit is True


class _RowLike:
    """A mapping the way bigquery.Row is one: keys()/__getitem__, not a dict."""

    def __init__(self, values):
        self._values = values

    def keys(self):
        return self._values.keys()

    def __getitem__(self, key):
        return self._values[key]


def test_collect_rows_returns_new_dicts_and_leaves_the_input_untouched():
    source = {"a": 1}
    rows, _ = collect_rows(iter([source, _RowLike({"b": 2})]), max_rows=None)
    assert rows == [{"a": 1}, {"b": 2}]
    assert rows[0] is not source
    assert all(type(row) is dict for row in rows)
    assert source == {"a": 1}


# --- iter_fetchmany: the paged fetch --------------------------------------------------------


def _pager(rows):
    remaining = iter(rows)
    return lambda size: list(itertools.islice(remaining, size))


def test_iter_fetchmany_pages_until_the_first_empty_page():
    sizes = []
    pager = _pager(range(7))

    def fetchmany(size):
        sizes.append(size)
        return pager(size)

    assert list(iter_fetchmany(fetchmany, page_size=3)) == list(range(7))
    # [0,1,2], [3,4,5], [6], then the empty page that ends the fetch: a short page is not
    # taken as the end (no driver assumption), only an empty one is.
    assert sizes == [3, 3, 3, 3]


def test_iter_fetchmany_defaults_to_the_fetch_page_size():
    sizes = []

    def fetchmany(size):
        sizes.append(size)
        return []

    assert list(iter_fetchmany(fetchmany)) == []
    assert sizes == [FETCH_PAGE_SIZE]


def test_iter_fetchmany_calls_between_pages_with_the_count_before_every_page_after_the_first():
    seen = []
    list(iter_fetchmany(_pager(range(7)), page_size=3, between_pages=seen.append))
    assert seen == [3, 6, 7]  # never before page 1; before pages 2, 3 and the empty 4th


def test_iter_fetchmany_never_calls_between_pages_when_the_first_page_is_empty():
    seen = []
    list(iter_fetchmany(_pager([]), page_size=3, between_pages=seen.append))
    assert seen == []


def test_iter_fetchmany_propagates_between_pages_exceptions_and_stops_fetching():
    sizes = []
    pager = _pager(range(7))

    def fetchmany(size):
        sizes.append(size)
        return pager(size)

    def deadline(fetched):
        raise TimeoutError(f"after {fetched}")

    with pytest.raises(TimeoutError, match="after 3"):
        list(iter_fetchmany(fetchmany, page_size=3, between_pages=deadline))
    assert sizes == [3]  # the first page is never pre-empted; the second is never requested


def test_iter_fetchmany_stops_requesting_pages_when_the_consumer_stops():
    sizes = []
    pager = _pager(range(100))

    def fetchmany(size):
        sizes.append(size)
        return pager(size)

    rows, row_cap_hit = collect_rows(
        ({"n": value} for value in iter_fetchmany(fetchmany, page_size=10)), max_rows=25
    )
    assert len(rows) == 25
    assert row_cap_hit is True
    assert sizes == [10, 10, 10]  # the sentinel is the 26th row, inside page 3


# --- the hints -------------------------------------------------------------------------------


def test_size_cap_hint_names_the_cap_the_counts_and_the_remedy():
    trimmed = trim_rows_to_byte_cap([{"s": "x" * MAX_RESULT_BYTES}])
    hint = size_cap_hint(trimmed, fetched_rows=1, row_cap_hit=True, max_rows=1000)
    assert f"{trimmed.total_bytes} bytes" in hint
    assert f"{MAX_RESULT_BYTES}-byte (8 MiB)" in hint
    assert "first 0 of the 1 rows fetched" in hint
    assert "max_rows=1000" not in hint  # one row fetched: the row cap was not reached
    assert "not retrieved" in hint
    assert "LIMIT" in hint
    assert "narrower columns" in hint


def test_size_cap_hint_names_the_row_cap_only_when_it_was_reached():
    rows = [{"s": "x" * 9000}] * 1000
    trimmed = trim_rows_to_byte_cap(rows)
    reached = size_cap_hint(trimmed, fetched_rows=1000, row_cap_hit=True, max_rows=1000)
    assert "the row cap max_rows=1000 was reached too" in reached
    exhausted = size_cap_hint(trimmed, fetched_rows=1000, row_cap_hit=False, max_rows=1000)
    assert "max_rows=1000" not in exhausted
    # Both hints are true whatever stopped the fetch: no claim about rows nobody saw.
    for hint in (reached, exhausted):
        assert "rows beyond the 1000 fetched, if any, were not retrieved" in hint


def test_row_cap_hint_is_unchanged_when_the_sentinel_row_was_seen():
    assert row_cap_hint(max_rows=2, row_count=2) == (
        "Query returned more than max_rows=2 rows; showing the first 2. Add a LIMIT to your "
        "query or raise max_rows."
    )


def test_row_cap_hint_says_the_fetch_stopped_early_when_fewer_rows_than_max_rows_came_back():
    # Only reachable if the engine's lower-bound estimate stopped the fetch and the exact
    # measurement then came in under the cap - a broken bound. Still a refusal, never a
    # silent ok, and the text does not claim max_rows was exceeded.
    hint = row_cap_hint(max_rows=1000, row_count=7)
    assert not hint.startswith("Query returned more than")
    assert "stopped after 7 rows" in hint
    assert "max_rows=1000" in hint
    assert "may be missing" in hint
    assert "LIMIT" in hint
