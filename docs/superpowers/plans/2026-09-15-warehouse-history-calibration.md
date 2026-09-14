# Warehouse-History Cost Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use each warehouse's own query history (Snowflake `INFORMATION_SCHEMA.QUERY_HISTORY`, Databricks Query History REST API) to calibrate `estimate_query_cost`'s runtime assumption with real data when an exact-text match exists, falling back to the existing byte-size-tier heuristic otherwise — no new persistence layer.

**UPDATE (2026-09-15, after running Phase 1's live verification spikes): this ships Snowflake-only.** Databricks' Query History REST API returns `query_text: "<REDACTED>"` unconditionally on the live Free Edition account tested — confirmed even with `include_metrics=True`, and confirmed via direct SDK source read that this is server-side redaction, not something a client parameter can bypass. Since exact-text matching requires reading the text back to match against, this is impossible on this account tier, not merely a risk. Free Edition's documented "locked... enterprise admin" restriction makes it unlikely this account could ever disable that redaction. Phase 3 below is kept in this document for reference but marked SKIPPED — see its own note for exactly what was found and how to re-open this if a future paid workspace confirms the redaction is a toggleable setting there.

**Architecture:** A new per-engine `_lookup_historical_runtime()` function, called from each engine's existing `explain_estimate()` right before the existing `scale_runtime_hours(...)` call. Returns a shared `HistoricalRuntimeSignal | None`. Never raises — any failure (permission, timeout, malformed response, no match) degrades silently to today's unchanged behavior.

**Tech Stack:** Same as the rest of the repo — Python 3.12+, `snowflake-connector-python`, `databricks-sdk`, `pytest`, `ruff`, `mypy`.

**Spec:** `docs/superpowers/specs/2026-09-15-warehouse-history-calibration-design.md` (this plan implements it end-to-end — read both; the spec has the full reasoning, including the cache-hit-exclusion correctness fix found during its own self-review).

## Global Constraints

- Never request or default to an elevated/admin role or grant — this feature exists specifically because `INFORMATION_SCHEMA.QUERY_HISTORY` (Snowflake) and the Query History REST API (Databricks) both work for the caller's own least-privilege identity. Do not substitute `ACCOUNT_USAGE.QUERY_HISTORY` or `system.query.history` anywhere.
- The lookup function's contract is absolute: **it must never raise**. Every failure mode returns `None`. This mirrors the existing `cur.cancel()`/`conn.close()` best-effort discipline already in both engine files.
- The lookup must be bounded by its own short timeout (5s) so a slow lookup never makes an `estimate_query_cost` call noticeably slower than today.
- Cache-hit rows must be excluded from any runtime average — averaging in a near-instant cached result would silently corrupt calibration toward *underestimating* future runtime, the opposite of this project's deliberate conservative-bias philosophy (see spec's "Cache-hit exclusion" section).
- `AccuracyTier` does not change (`UPPER_BOUND` for Snowflake, `HEURISTIC` for Databricks) — this feature informs an existing tier's number, it does not add a new precision category.
- BigQuery is out of scope — its `dryRun` is already `PRECISE`.
- No new dependency, no new persisted file/database, no new required env var — this feature is on by default with zero new configuration.
- Coverage floor is 80% (CI-enforced); do not drop below it. Every new branch (match found, no match, exception, timeout, cache-hit-only) needs an explicit test — the cache-hit-exclusion test is the single most important test in this whole feature per the spec.
- `uv run ruff check src tests` and `uv run mypy src` must stay clean after every phase.
- No direct commits to `main` — feature branch, PR, CI-green, squash-merge per `AGENTS.md`.

---

## Phase 1: Verification spikes (resolve the spec's 3 open questions before writing production code)

**Why first:** The spec explicitly flags three unverified assumptions (column units, success-status string, cache-hit proxy reliability) as "must resolve by a verification spike, not assumed." Writing tests against wrong assumptions would produce tests that pass for the wrong reason — exactly the class of mistake this project has repeatedly caught and fixed all session (the Databricks `dict(row)` bug, the `run_query_bounded` dispatch-order test bug). Do not skip this phase.

### Task 1.1: Snowflake spike — confirm columns, status string, and the cache-hit proxy

**Files:** None modified — this is a live, read-only investigation against the real Snowflake trial account (credentials already in `.env`, per this project's established pattern this session).

- [ ] **Step 1: Run a live query twice in a row and inspect its own history entry**

```bash
source .env && uv run python -c "
import snowflake.connector, os, time
conn = snowflake.connector.connect(
    account=os.environ['SNOWFLAKE_ACCOUNT'], user=os.environ['SNOWFLAKE_USER'],
    role=os.environ.get('SNOWFLAKE_ROLE', 'COST_GUARD_READER'),
    private_key_file=os.environ.get('SNOWFLAKE_PRIVATE_KEY_PATH'),
    password=os.environ.get('SNOWFLAKE_PASSWORD'),
)
sql = 'SELECT COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER'
with conn.cursor() as cur:
    cur.execute('USE WAREHOUSE COMPUTE_WH')
    cur.execute(sql)   # first run - real execution
    time.sleep(2)
    cur.execute(sql)   # second run - likely a cache hit
    time.sleep(2)
    cur.execute(
        \"SELECT QUERY_TEXT, TOTAL_ELAPSED_TIME, BYTES_SCANNED, EXECUTION_STATUS \"
        \"FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => 20)) \"
        \"WHERE QUERY_TEXT ILIKE '%TPCH_SF1.CUSTOMER%' ORDER BY START_TIME DESC\"
    )
    for row in cur.fetchall():
        print(row)
"
```

- [ ] **Step 2: Record the findings** as a short comment block to carry into Task 2.1/2.2 (do not proceed on assumption):
  - Exact `EXECUTION_STATUS` string for a successful query (expected `'SUCCESS'` — confirm or correct).
  - `TOTAL_ELAPSED_TIME`'s unit (expected milliseconds — confirm by comparing the printed value against real wall-clock time observed).
  - Whether the second (repeated) run's `BYTES_SCANNED` actually drops to 0 or near-0 relative to the first — this is the cache-hit proxy the whole "Cache-hit exclusion" design depends on. If it does NOT reliably drop, STOP and re-open the spec's "Cache-hit exclusion" section before writing Task 2.2 — do not ship an unverified heuristic.

### Task 1.2: Databricks spike — confirm `duration`'s unit and the SDK's timeout support

**Files:** None modified — live investigation against the real Databricks Free Edition account.

- [ ] **Step 1: Run a live query twice and inspect its own history entry via the SDK**

```bash
source .env && uv run python -c "
from databricks import sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import QueryFilter
import os, time

conn = sql.connect(
    server_hostname=os.environ['DATABRICKS_SERVER_HOSTNAME'],
    http_path=os.environ['DATABRICKS_HTTP_PATH'],
    access_token=os.environ.get('DATABRICKS_TOKEN'),
)
with conn.cursor() as cur:
    cur.execute('SELECT 1')
    time.sleep(2)
    cur.execute('SELECT 1')
    time.sleep(3)

w = WorkspaceClient()
for q in list(w.query_history.list(max_results=20)):
    if q.query_text and 'SELECT 1' in q.query_text:
        print(q.query_id, q.duration, q.cache_query_id, q.status, q.query_text[:40])
"
```

- [ ] **Step 2: Record findings**: `duration`'s unit (expected milliseconds — confirm against observed wall-clock time), and whether the second run shows a non-`None` `cache_query_id`. If `cache_query_id` never populates for this trivial query, try a slightly heavier query (e.g. against a real table) before concluding the field does not work as expected.

- [ ] **Step 3: Check for an SDK-native timeout**, preferred over a manual thread wrapper:

```bash
uv run python -c "
from databricks.sdk import WorkspaceClient
import inspect
print(inspect.signature(WorkspaceClient.__init__))
"
```
Look for any `timeout`/`http_timeout_seconds`-shaped constructor or config parameter. Record whether one exists — Task 3.1 uses it if present, falls back to the thread+join pattern (mirroring Task 2.1's Snowflake implementation) if not.

- [ ] **Step 4: Commit nothing** — this phase is pure investigation. Carry the recorded findings into Phase 2/3's task briefs verbatim (do not let a fresh implementer re-derive or re-guess them).

## Phase 1 findings (both spikes run live, 2026-09-15)

**Task 1.1 (Snowflake) — fully confirmed, design proceeds as written:**
- `EXECUTION_STATUS = 'SUCCESS'` is the exact correct string (also observed `'FAILED_WITH_ERROR'`, `'RUNNING'` as other real values).
- `TOTAL_ELAPSED_TIME` is in milliseconds — confirmed by comparing against observed wall-clock time.
- `BYTES_SCANNED > 0` as a cache-hit-exclusion proxy is **confirmed working**: a real execution of `SELECT SUM(C_ACCTBAL) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER WHERE C_MKTSEGMENT = 'BUILDING'` showed `360ms, 10,741,184 bytes scanned`; the immediate repeat (a cache hit) showed `54ms, 0 bytes scanned`. The design's proxy correctly separates them.
- Caveat found along the way: `SELECT COUNT(*)` (no `WHERE`) always shows `BYTES_SCANNED = 0` even on a genuinely fresh execution, because Snowflake answers it from partition metadata without a real scan — this is not a cache-hit false-negative, just means COUNT(*)-shaped queries will rarely get calibration benefit (safe, not dangerous — they simply always fall back to the heuristic).
- `INFORMATION_SCHEMA.QUERY_HISTORY` requires an active `USE DATABASE` (or a fully-qualified `<db>.INFORMATION_SCHEMA.QUERY_HISTORY` reference) — `INFORMATION_SCHEMA` is per-database, not global. Task 2.1's implementation must account for this (the production `explain_estimate` already runs after a real query context is established via the caller's own SQL, so this is a minor implementation detail, not a design blocker — confirm the connection's active database is set before calling the lookup, or fully-qualify the table function call against a database the role is guaranteed to have access to).

**Task 1.2 (Databricks) — BLOCKED, do not implement Phase 3 as originally designed:**
- `WorkspaceClient()` needs an explicit `host`/`token` (or `DATABRICKS_HOST`/`DATABRICKS_TOKEN`-named env vars, not this project's own `DATABRICKS_SERVER_HOSTNAME` naming) — a real wiring detail, not a blocker, but note it for whoever eventually revisits this.
- `w.query_history.list(...)` returns a `ListQueriesResponse` wrapper — the actual rows are `.res`, NOT directly iterable as the original spec/plan draft assumed. (Fixed if this phase is ever re-opened.)
- **Hard blocker**: `QueryInfo.query_text` came back as the literal string `"<REDACTED>"` for every one of 9 real query-history rows on the live Free Edition account tested — including trivial queries with no PII-sensitive content. Confirmed this is server-side, not a missing parameter: `include_metrics=True` was tried and made no difference; a direct read of the installed `databricks-sdk` source (`sql.py`) shows zero client-side redaction logic, confirming the redaction happens on Databricks' backend before the response ever reaches the SDK. Exact-text matching is impossible without the text to match against — this blocks Phase 3 entirely as designed, not just as a risk.
- Bonus finding for whenever this is revisited: `QueryMetrics.result_from_cache: bool` (available via `include_metrics=True`) is a far more reliable, explicit cache-hit signal than the `cache_query_id`/bytes-scanned proxies the spec guessed at — use this instead of `cache_query_id` if this feature ever becomes viable.
- **Decision (confirmed with the user 2026-09-15): ship Snowflake-only.** Skip Phase 3 below entirely. Revisit only if a future paid Databricks workspace confirms this redaction setting is admin-toggleable there (Free Edition's documented admin-console lockout makes it unlikely this specific account tier ever could disable it).

---

## Phase 2: Snowflake calibration

### Task 2.1: `HistoricalRuntimeSignal` type + Snowflake `_lookup_historical_runtime`

**Files:**
- Modify: `src/cost_guard_mcp/types.py` (add `HistoricalRuntimeSignal`)
- Modify: `src/cost_guard_mcp/engines/snowflake.py` (add `_normalize_sql_for_matching`, `_lookup_historical_runtime`, wire into `explain_estimate`)
- Test: `tests/unit/test_engines_snowflake.py`

**Interfaces:**
- Produces: `HistoricalRuntimeSignal(avg_runtime_hours: float, sample_count: int)` (consumed by both engines' `explain_estimate` and by Task 3.1's Databricks implementation).
- Produces: `_lookup_historical_runtime(conn, sql_text: str) -> HistoricalRuntimeSignal | None`.

- [ ] **Step 1: Add the shared type**

```python
# src/cost_guard_mcp/types.py — add near CostEstimate
@dataclass(frozen=True)
class HistoricalRuntimeSignal:
    avg_runtime_hours: float
    sample_count: int
```

- [ ] **Step 2: Write the failing tests** (use Task 1.1's spike findings for the exact `EXECUTION_STATUS` string and column units — if they confirmed `'SUCCESS'` and milliseconds, the tests below are correct as written; otherwise adjust to match the real spike findings before proceeding)

```python
# tests/unit/test_engines_snowflake.py — append
import json
from unittest.mock import MagicMock, patch

from cost_guard_mcp.engines.snowflake import _lookup_historical_runtime, _normalize_sql_for_matching
from cost_guard_mcp.types import HistoricalRuntimeSignal


def test_normalize_sql_collapses_whitespace_but_not_case():
    assert _normalize_sql_for_matching("SELECT   1\n  FROM t") == "SELECT 1 FROM t"
    assert _normalize_sql_for_matching("select 1 from t") != _normalize_sql_for_matching(
        "SELECT 1 FROM t"
    )


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_finds_single_real_match(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("SELECT 1 FROM t", 4000, 1024),  # real execution: 4000ms, bytes_scanned > 0
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    signal = _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    assert signal == HistoricalRuntimeSignal(avg_runtime_hours=4000 / 1000 / 3600, sample_count=1)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_averages_multiple_matches(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("SELECT 1 FROM t", 4000, 1024),
        ("SELECT   1 FROM t", 6000, 2048),  # whitespace-differing, still matches
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    signal = _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    assert signal.sample_count == 2
    assert signal.avg_runtime_hours == (5000 / 1000 / 3600)


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_excludes_cache_hits(mock_connect):
    # The single most important test in this feature (per the design spec) - a cache hit
    # (bytes_scanned == 0) must never be averaged in, or calibration silently corrupts
    # itself toward underestimating future runtime.
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("SELECT 1 FROM t", 4000, 1024),  # real execution
        ("SELECT 1 FROM t", 5, 0),  # cache hit - near-instant, zero bytes scanned
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    signal = _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t")

    assert signal.sample_count == 1  # only the real execution counted
    assert signal.avg_runtime_hours == 4000 / 1000 / 3600


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_returns_none_when_all_matches_are_cache_hits(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("SELECT 1 FROM t", 5, 0)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    assert _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t") is None


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_returns_none_when_no_text_match(mock_connect):
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("SELECT * FROM other_table", 4000, 1024)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connect.return_value = mock_conn

    assert _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t") is None


@patch("cost_guard_mcp.engines.snowflake._connect")
def test_lookup_historical_runtime_returns_none_on_exception(mock_connect):
    mock_conn = MagicMock()
    mock_conn.cursor.side_effect = RuntimeError("permission denied")
    mock_connect.return_value = mock_conn

    assert _lookup_historical_runtime(mock_conn, "SELECT 1 FROM t") is None
```

- [ ] **Step 3: Run to verify they fail** — `uv run pytest tests/unit/test_engines_snowflake.py -v -k lookup_historical` → `ImportError`/`AttributeError`.

- [ ] **Step 4: Implement** (adjust the `EXECUTION_STATUS` string/column units to match Task 1.1's actual spike findings, not blindly the placeholder below)

```python
# src/cost_guard_mcp/engines/snowflake.py — append
from cost_guard_mcp.types import HistoricalRuntimeSignal

_HISTORY_LOOKUP_TIMEOUT_SECONDS = 5
_HISTORY_LOOKUP_RESULT_LIMIT = 500


def _normalize_sql_for_matching(sql_text: str) -> str:
    """Collapse whitespace runs to single spaces and strip - deliberately NOT case-folded,
    since quoted identifiers in Snowflake are case-sensitive and case-folding risks
    conflating genuinely different queries."""
    return " ".join(sql_text.split())


def _lookup_historical_runtime(conn, sql_text: str) -> HistoricalRuntimeSignal | None:
    """Best-effort - any failure (permission, network, malformed response, timeout) returns
    None, never raises. Cache-hit rows (bytes_scanned == 0) are excluded - averaging in a
    near-instant cached result would corrupt calibration toward underestimating future
    runtime."""
    normalized_target = _normalize_sql_for_matching(sql_text)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT QUERY_TEXT, TOTAL_ELAPSED_TIME, BYTES_SCANNED "
                "FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY("
                f"RESULT_LIMIT => {_HISTORY_LOOKUP_RESULT_LIMIT})) "
                "WHERE EXECUTION_STATUS = 'SUCCESS'"
            )
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001, S110 - best-effort, matches cancel()/close() discipline
        return None

    matches_ms = [
        elapsed_ms
        for query_text, elapsed_ms, bytes_scanned in rows
        if _normalize_sql_for_matching(query_text) == normalized_target and bytes_scanned > 0
    ]
    if not matches_ms:
        return None
    avg_hours = (sum(matches_ms) / len(matches_ms)) / 1000 / 3600
    return HistoricalRuntimeSignal(avg_runtime_hours=avg_hours, sample_count=len(matches_ms))
```

**Timeout note**: if Task 1.1's spike shows this metadata query can be slow, wrap the `cur.execute`/`fetchall` block in the exact same background-thread + `join(timeout=_HISTORY_LOOKUP_TIMEOUT_SECONDS)` pattern `execute_bounded` already uses elsewhere in this file — treat "still running after the timeout" as another `return None` path (no cancellation needed, this is a cheap metadata read).

- [ ] **Step 5: Run to verify they pass** — `uv run pytest tests/unit/test_engines_snowflake.py -v -k lookup_historical` → 7 passed.

- [ ] **Step 6: Wire it into `explain_estimate`** — find the existing line (post-hardening-plan):
```python
runtime_hours = scale_runtime_hours(bytes_assigned, baseline_hours=_ASSUMED_RUNTIME_HOURS)
estimated_cost_usd = rate * price * runtime_hours
```
Change to:
```python
historical = _lookup_historical_runtime(conn, sql)
if historical is not None:
    runtime_hours = historical.avg_runtime_hours
else:
    runtime_hours = scale_runtime_hours(bytes_assigned, baseline_hours=_ASSUMED_RUNTIME_HOURS)
estimated_cost_usd = rate * price * runtime_hours
```
Add a caveat branch: if `historical is not None`, append a caveat like
`f"Cost is informed by {historical.sample_count} historical run(s) of this exact query, averaging {historical.avg_runtime_hours * 3600:.1f}s - more reliable than the coarse size-tier heuristic."`
instead of (not in addition to) the existing "Cost assumes a baseline..." caveat for that call.

- [ ] **Step 7: Add an integration-level test proving the wiring** (not just the isolated lookup function) — mock `_lookup_historical_runtime` directly and assert `explain_estimate`'s returned `estimated_cost_usd` and caveats reflect the historical path when a signal is returned, and the existing tiered path when `None` is returned (this second case must reuse the exact existing passing tests unchanged — run the full file, not just new tests, to confirm no regression).

- [ ] **Step 8: Run full suite + lint + typecheck**

```bash
uv run pytest tests/unit -v --cov=cost_guard_mcp --cov-report=term-missing --cov-fail-under=80
uv run ruff check src tests && uv run mypy src
```

- [ ] **Step 9: Commit**

```bash
git add src/cost_guard_mcp/types.py src/cost_guard_mcp/engines/snowflake.py tests/unit/test_engines_snowflake.py
git commit -m "feat: calibrate Snowflake cost estimates from the caller own query history"
```

---

## Phase 3: Databricks calibration (mirrors Phase 2) — **SKIPPED, do not implement**

**Blocked as of 2026-09-15 — see "Phase 1 findings" above for the full evidence.** Databricks' Query
History REST API returns `query_text: "<REDACTED>"` unconditionally on the account tested, confirmed
server-side (not bypassable via any client parameter, confirmed via direct SDK source read). Exact-text
matching cannot work without the text to match against. Kept below for reference only — do not execute
these steps unless a future investigation confirms a real workspace can retrieve unredacted query text
for the caller's own queries.

### Task 3.1 (reference only, not executed): Databricks `_lookup_historical_runtime`

**Files:**
- Modify: `src/cost_guard_mcp/engines/databricks.py`
- Test: `tests/unit/test_engines_databricks.py`

**Interfaces:**
- Consumes: `HistoricalRuntimeSignal` (Task 2.1).
- Produces: `_lookup_historical_runtime(sql_text: str, warehouse_id: str | None) -> HistoricalRuntimeSignal | None`.

- [ ] **Step 1: Write the failing tests** (mirror Task 2.1's Snowflake test set exactly, adapted to the SDK's object shape — use Task 1.2's spike findings for `duration`'s confirmed unit)

```python
# tests/unit/test_engines_databricks.py — append
from unittest.mock import MagicMock, patch

from cost_guard_mcp.engines.databricks import _lookup_historical_runtime
from cost_guard_mcp.types import HistoricalRuntimeSignal


def _mock_query_info(query_text, duration_ms, cache_query_id=None):
    q = MagicMock()
    q.query_text = query_text
    q.duration = duration_ms
    q.cache_query_id = cache_query_id
    return q


@patch("cost_guard_mcp.engines.databricks.WorkspaceClient")
def test_lookup_historical_runtime_finds_single_real_match(mock_ws_client_cls):
    mock_ws_client_cls.return_value.query_history.list.return_value = [
        _mock_query_info("SELECT 1 FROM t", 4000),
    ]

    signal = _lookup_historical_runtime("SELECT 1 FROM t", warehouse_id=None)

    assert signal == HistoricalRuntimeSignal(avg_runtime_hours=4000 / 1000 / 3600, sample_count=1)


@patch("cost_guard_mcp.engines.databricks.WorkspaceClient")
def test_lookup_historical_runtime_excludes_cache_hits(mock_ws_client_cls):
    mock_ws_client_cls.return_value.query_history.list.return_value = [
        _mock_query_info("SELECT 1 FROM t", 4000, cache_query_id=None),
        _mock_query_info("SELECT 1 FROM t", 5, cache_query_id="cached-original-id"),
    ]

    signal = _lookup_historical_runtime("SELECT 1 FROM t", warehouse_id=None)

    assert signal.sample_count == 1
    assert signal.avg_runtime_hours == 4000 / 1000 / 3600


@patch("cost_guard_mcp.engines.databricks.WorkspaceClient")
def test_lookup_historical_runtime_returns_none_when_all_matches_are_cache_hits(mock_ws_client_cls):
    mock_ws_client_cls.return_value.query_history.list.return_value = [
        _mock_query_info("SELECT 1 FROM t", 5, cache_query_id="cached-original-id"),
    ]

    assert _lookup_historical_runtime("SELECT 1 FROM t", warehouse_id=None) is None


@patch("cost_guard_mcp.engines.databricks.WorkspaceClient")
def test_lookup_historical_runtime_returns_none_when_no_text_match(mock_ws_client_cls):
    mock_ws_client_cls.return_value.query_history.list.return_value = [
        _mock_query_info("SELECT * FROM other_table", 4000),
    ]

    assert _lookup_historical_runtime("SELECT 1 FROM t", warehouse_id=None) is None


@patch("cost_guard_mcp.engines.databricks.WorkspaceClient")
def test_lookup_historical_runtime_returns_none_on_exception(mock_ws_client_cls):
    mock_ws_client_cls.side_effect = RuntimeError("auth error")

    assert _lookup_historical_runtime("SELECT 1 FROM t", warehouse_id=None) is None


@patch("cost_guard_mcp.engines.databricks.WorkspaceClient")
def test_lookup_historical_runtime_filters_by_warehouse_id_when_given(mock_ws_client_cls):
    mock_list = mock_ws_client_cls.return_value.query_history.list
    mock_list.return_value = [_mock_query_info("SELECT 1 FROM t", 4000)]

    _lookup_historical_runtime("SELECT 1 FROM t", warehouse_id="wh-123")

    called_filter = mock_list.call_args.kwargs["filter_by"]
    assert called_filter.warehouse_ids == ["wh-123"]
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** (use Task 1.2's spike finding for whether an SDK-native timeout kwarg exists — if yes, use it directly on `WorkspaceClient(...)` or the `.list()` call; if no, use the thread+join pattern shown as the fallback below)

```python
# src/cost_guard_mcp/engines/databricks.py — append
import threading

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import QueryFilter, QueryStatus

from cost_guard_mcp.engines.snowflake import (
    _normalize_sql_for_matching,
)  # or duplicate locally, see note below
from cost_guard_mcp.types import HistoricalRuntimeSignal

_HISTORY_LOOKUP_TIMEOUT_SECONDS = 5
_HISTORY_LOOKUP_MAX_RESULTS = 500


def _lookup_historical_runtime(
    sql_text: str, warehouse_id: str | None
) -> HistoricalRuntimeSignal | None:
    """Best-effort - mirrors the Snowflake function's contract exactly: never raises,
    excludes cache hits via cache_query_id."""
    normalized_target = _normalize_sql_for_matching(sql_text)
    result: list = []
    error: list[BaseException] = []

    def _run() -> None:
        try:
            w = WorkspaceClient()
            filter_by = QueryFilter(statuses=[QueryStatus.FINISHED])
            if warehouse_id:
                filter_by.warehouse_ids = [warehouse_id]
            result.extend(
                w.query_history.list(filter_by=filter_by, max_results=_HISTORY_LOOKUP_MAX_RESULTS)
            )
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=_HISTORY_LOOKUP_TIMEOUT_SECONDS)
    if thread.is_alive() or error:
        return None

    matches_ms = [
        q.duration
        for q in result
        if q.query_text is not None
        and _normalize_sql_for_matching(q.query_text) == normalized_target
        and q.cache_query_id is None
        and q.duration is not None
    ]
    if not matches_ms:
        return None
    avg_hours = (sum(matches_ms) / len(matches_ms)) / 1000 / 3600
    return HistoricalRuntimeSignal(avg_runtime_hours=avg_hours, sample_count=len(matches_ms))
```

**Note on `_normalize_sql_for_matching`**: importing it from `snowflake.py` into `databricks.py` creates an odd cross-engine dependency for a 3-line pure function. Prefer duplicating the tiny helper locally in `databricks.py` (matches this codebase's existing precedent of small, self-contained engine modules rather than a shared-utils module for something this small) - adjust the test file's patch target accordingly if you duplicate it (`cost_guard_mcp.engines.databricks._normalize_sql_for_matching`).

- [ ] **Step 4: Run to verify they pass.**

- [ ] **Step 5: Wire it into `explain_estimate`** — same pattern as Task 2.1 Step 6, using `max_size_in_bytes`-derived caveats and passing the warehouse's actual ID (check what `_connect()`/config expose — likely need to extract the warehouse ID from `DATABRICKS_HTTP_PATH`, e.g. `/sql/1.0/warehouses/<id>` — parse it with the existing regex-validation discipline this codebase already uses for other identifiers, or pass `warehouse_id=None` if extracting it cleanly isn't straightforward, since the filter is optional).

- [ ] **Step 6: Add the same integration-level wiring test as Task 2.1 Step 7.**

- [ ] **Step 7: Run full suite + lint + typecheck; commit.**

```bash
uv run pytest tests/unit -v --cov=cost_guard_mcp --cov-report=term-missing --cov-fail-under=80
uv run ruff check src tests && uv run mypy src
git add src/cost_guard_mcp/engines/databricks.py tests/unit/test_engines_databricks.py
git commit -m "feat: calibrate Databricks cost estimates from the caller own query history"
```

---

## Phase 4: Live verification + docs

### Task 4.1: End-to-end live verification against the real Snowflake trial account — DONE (2026-09-15)

- [x] **Step 1**: Ran the same query (`SELECT SUM(C_ACCTBAL)... WHERE C_MKTSEGMENT = 'FURNITURE'`) — first `explain_estimate` call (no history yet) showed the unchanged tiered-heuristic caveat; after actually executing the raw SQL for real (simulating `run_query_bounded`), the next `explain_estimate` call correctly showed "Cost is informed by 1 historical run(s) of this exact query, averaging 0.3s." Confirms the whole wired path works, not just the mocked unit tests.
- [x] **Step 2**: Confirmed implicitly by Step 1's first call — a query with no prior history produces the exact same caveat/estimate shape as before this feature shipped.
- [x] **Step 3**: Ran the same query 2 more times (both genuine cache hits, confirmed via direct history inspection: `119ms/0 bytes` and `84ms/0 bytes`, versus the real execution's `272ms/10,741,184 bytes`) — the cache-hit exclusion logic is correct. **Real finding along the way**: a follow-up `explain_estimate` call ~4 seconds after those repeat runs found NO historical signal at all, even though the real execution's row was present and correctly shaped — root-caused to Snowflake's query-history ingestion lag (an isolated direct query a short time later found all 3 rows, including the real one, cleanly). This is a genuine, previously-unknown-to-the-design latency characteristic of `INFORMATION_SCHEMA.QUERY_HISTORY` itself, not a code bug — the lookup's existing fail-safe design already handles it correctly (falls back to the heuristic rather than erroring or returning a wrong answer), so no code change was needed, only a README callout (Task 4.2) so users understand why a just-run repeat might not immediately show calibration.

### Task 4.2: README update — DONE (2026-09-15)

- [x] Added two bullets to the existing "Known limitations" section: (1) Snowflake calibration mechanics, cache-hit exclusion, and the query-history ingestion-lag finding from Task 4.1 Step 3; (2) Databricks calibration investigated and found blocked by server-side query-text redaction.
- [x] Commit (bundled with the feature implementation commit).

---

## Self-review (performed while writing this plan)

- **Placeholder scan**: no TBD/TODO; the "Verification spike" tasks are deliberate investigation steps with concrete commands, not placeholders for missing content.
- **Type consistency**: `HistoricalRuntimeSignal(avg_runtime_hours, sample_count)` used identically in both engines' code and both test files.
- **Spec coverage**: every section of the design spec (architecture, per-engine mechanics, cache-hit exclusion, testing list, rejected alternatives) maps to a task above; the three "open questions" map directly to Phase 1's two spike tasks.
