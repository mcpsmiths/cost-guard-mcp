# Warehouse-History Cost Calibration — Design

## Context

Snowflake's and Databricks' cost estimates (`AccuracyTier.UPPER_BOUND` / `HEURISTIC`) compute a dollar
figure from `rate * price * scale_runtime_hours(estimated_bytes, baseline_hours=30/3600)` — a coarse,
deliberately conservative size-tier multiplier on a flat 30-second assumption (shipped in the post-launch
hardening plan, `src/cost_guard_mcp/pricing/runtime_scaling.py`). It has never used real runtime data,
because the obvious source — `ACCOUNT_USAGE.QUERY_HISTORY` (Snowflake) / `system.query.history`
(Databricks) — requires elevated, account-admin-gated privileges this project's own rule (`AGENTS.md`:
no default role, never `ACCOUNTADMIN`) refuses to request.

A follow-up `/deep-research` pass (2026-09-14) found this blocker doesn't apply everywhere: Snowflake's
`INFORMATION_SCHEMA.QUERY_HISTORY` table function returns the *calling role's own* query history with
**no elevated privilege** (confirmed against current Snowflake docs — `ACCOUNTADMIN` doesn't appear
anywhere on that function's reference page), and Databricks' Query History REST API
(`databricks-sdk`'s `WorkspaceClient.query_history.list()`, confirmed present in the installed SDK,
2026-09-15) exposes a query's own owner's history via the `query-history` OAuth scope — also no admin
grant. Both are real, least-privilege-compatible signal sources this project doesn't use today.

**Goal:** use real historical runtime data for a query when it exists, without adding a persistence layer
of our own — the warehouse's own history *is* the store — and without ever making an estimate call worse
or meaningfully slower than today's when no history is found.

**Non-goals:** BigQuery is out of scope (its `dryRun` is already `PRECISE`, an exact measure — no
calibration need). Fuzzy/table-level matching is out of scope for this iteration (exact-SQL-text match
only — see "Rejected/deferred" below). No new persisted state, no new files, no new required
configuration.

## Architecture

A new per-engine private lookup function, called from each engine's existing `explain_estimate`, right
before the existing `scale_runtime_hours(...)` call:

```
explain_estimate(sql_text, warehouse, warehouse_size, ...)
  │
  ├─ (existing) EXPLAIN / EXPLAIN COST → estimated_bytes
  │
  ├─ NEW: historical = _lookup_historical_runtime(conn_or_client, sql_text, warehouse)
  │         → HistoricalRuntimeSignal | None, bounded by its own short timeout,
  │           never raises - any failure returns None
  │
  ├─ if historical is not None:
  │      runtime_hours = historical.avg_runtime_hours
  │      caveat: "Cost is informed by N historical run(s) of this exact query,
  │               averaging {X}s - more reliable than the coarse size-tier heuristic below."
  │  else:
  │      runtime_hours = scale_runtime_hours(estimated_bytes, baseline_hours=_ASSUMED_RUNTIME_HOURS)
  │      caveat: (existing, unchanged)
  │
  └─ estimated_cost_usd = rate * price * runtime_hours
```

`AccuracyTier` is unchanged (`UPPER_BOUND` for Snowflake, `HEURISTIC` for Databricks) — this feature
makes an existing tier's number better-informed, it does not introduce a new precision category.

### New shared type

```python
# src/cost_guard_mcp/types.py — add
@dataclass(frozen=True)
class HistoricalRuntimeSignal:
    avg_runtime_hours: float
    sample_count: int
```

Kept engine-agnostic and tiny (two fields) so both engines' lookup functions return the same shape and
`explain_estimate` in each file blends it identically.

## Per-engine mechanics

### Snowflake — `src/cost_guard_mcp/engines/snowflake.py`

```python
_HISTORY_LOOKUP_TIMEOUT_SECONDS = 5
_HISTORY_LOOKUP_RESULT_LIMIT = 500  # recent window, not the function's 10,000-row max

def _normalize_sql_for_matching(sql_text: str) -> str:
    """Collapse whitespace runs to single spaces and strip - deliberately NOT case-folded,
    since quoted identifiers in Snowflake are case-sensitive and case-folding risks
    conflating genuinely different queries."""
    return " ".join(sql_text.split())


def _lookup_historical_runtime(
    conn: "snowflake.connector.SnowflakeConnection", sql_text: str
) -> HistoricalRuntimeSignal | None:
    """Best-effort - any failure (permission, network, malformed response, timeout) returns
    None, never raises. Bounded by _HISTORY_LOOKUP_TIMEOUT_SECONDS so a slow lookup never
    makes the whole estimate call noticeably slower than today."""
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
    except Exception:  # noqa: BLE001 - best-effort, matches cancel()/close() discipline
        return None

    matches_ms = [
        elapsed_ms
        for query_text, elapsed_ms, bytes_scanned in rows
        if _normalize_sql_for_matching(query_text) == normalized_target
        and bytes_scanned > 0  # excludes likely result-cache hits, see "Cache-hit exclusion" below
    ]
    if not matches_ms:
        return None
    avg_hours = (sum(matches_ms) / len(matches_ms)) / 1000 / 3600
    return HistoricalRuntimeSignal(avg_runtime_hours=avg_hours, sample_count=len(matches_ms))
```

(the `SELECT` above must be widened to also fetch `BYTES_SCANNED` for the cache-hit filter to work —
shown corrected in "Cache-hit exclusion" below)

**Verification spike needed at implementation time** (do not trust this spec's column names blindly,
per this project's own established discipline): confirm `TOTAL_ELAPSED_TIME`'s unit (assumed
milliseconds, matching `ACCOUNT_USAGE.QUERY_HISTORY`'s documented convention) and `EXECUTION_STATUS`'s
exact success-value string against a real call to `INFORMATION_SCHEMA.QUERY_HISTORY` on the live
Snowflake trial account before writing the implementation's tests.

**Timeout mechanism**: `conn.cursor()`'s synchronous `execute()` has no per-call timeout parameter:
reuse the exact same pattern `execute_bounded` already uses (Phase 3 of the hardening plan) — run the
query in a background thread, `join(timeout=_HISTORY_LOOKUP_TIMEOUT_SECONDS)`, treat "still running" as
a lookup failure (return `None`, no cancellation drama needed since this is a cheap metadata read, not a
warehouse-billed data scan).

### Databricks — `src/cost_guard_mcp/engines/databricks.py`

```python
_HISTORY_LOOKUP_TIMEOUT_SECONDS = 5
_HISTORY_LOOKUP_MAX_RESULTS = 500

def _lookup_historical_runtime(sql_text: str, warehouse_id: str | None) -> HistoricalRuntimeSignal | None:
    """Best-effort - mirrors the Snowflake function's contract exactly: never raises,
    bounded by its own timeout."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import QueryFilter, QueryStatus

    normalized_target = _normalize_sql_for_matching(sql_text)
    try:
        w = WorkspaceClient()  # reuses the same auth path as _connect() - PAT or OAuth M2M
        filter_by = QueryFilter(statuses=[QueryStatus.FINISHED])
        if warehouse_id:
            filter_by.warehouse_ids = [warehouse_id]
        response = _run_with_timeout(
            lambda: list(w.query_history.list(filter_by=filter_by, max_results=_HISTORY_LOOKUP_MAX_RESULTS)),
            _HISTORY_LOOKUP_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001
        return None

    matches_ms = [
        q.duration
        for q in response
        if q.query_text is not None
        and _normalize_sql_for_matching(q.query_text) == normalized_target
        and q.cache_query_id is None  # excludes result-cache hits, see "Cache-hit exclusion" below
        and q.duration is not None
    ]
    if not matches_ms:
        return None
    avg_hours = (sum(matches_ms) / len(matches_ms)) / 1000 / 3600
    return HistoricalRuntimeSignal(avg_runtime_hours=avg_hours, sample_count=len(matches_ms))
```

**Confirmed today (2026-09-15) against the installed `databricks-sdk`** (not assumed): `WorkspaceClient`
exposes `.query_history` (a `QueryHistoryAPI`); `.list()`'s real signature is
`(self, *, filter_by=None, include_metrics=None, max_results=None, page_token=None)`; `QueryFilter`'s
real fields are `query_start_time_range`, `statement_ids`, `statuses`, `user_ids`, `warehouse_ids` — **no
text-filter field exists**, confirming client-side filtering is required, not optional; `QueryInfo`'s
real fields include `query_text`, `duration`, `status`, `warehouse_id`; `QueryStatus`'s real values are
`CANCELED, COMPILED, COMPILING, FAILED, FINISHED, QUEUED, RUNNING, STARTED`.

**Verification spike still needed at implementation time**: `duration`'s exact unit is not yet confirmed
against a live call (assumed milliseconds, matching Databricks' general API convention) — confirm against
the live Free Edition account before trusting it in the implementation's cost math.

`_run_with_timeout` is a new tiny shared helper (thread+join, same pattern as Snowflake's) — or, simpler:
check whether `databricks-sdk`'s underlying HTTP client accepts a request-level timeout kwarg during the
implementation spike; prefer that over a manual thread wrapper if it exists.

## Cache-hit exclusion (found during spec self-review, not in the original research)

Both warehouses can serve a repeated identical query from a result cache — Snowflake's query result
cache, Databricks' equivalent — returning near-instantly without re-executing. A query an agent runs
repeatedly (the exact scenario exact-text matching is designed to help most) is *especially* likely to
hit this cache on its 2nd+ run. Averaging a cache-hit's near-zero duration in with real execution times
would systematically **underestimate** future runtime — the opposite of this project's deliberate bias
(Phase 2 of the hardening plan chose coarse tiers that only ever overestimate, never underestimate,
specifically because this tool's purpose is preventing surprise bills, not being falsely reassuring).
Silently corrupting real calibration data with cache-hit noise would be a worse outcome than the coarse
heuristic it is meant to improve on.

Mitigation, confirmed feasible today:
- **Databricks**: `QueryInfo.cache_query_id` (confirmed present in the installed `databricks-sdk`,
  `Optional[str]`) is non-null when a query's result came from cache — exclude those rows.
- **Snowflake**: no single documented boolean flag was confirmed for `INFORMATION_SCHEMA.QUERY_HISTORY`
  specifically. Using `BYTES_SCANNED > 0` as a proxy — a full result-cache hit scans zero bytes since it
  never touches the underlying tables. This is a heuristic, not a confirmed-exact mechanism; the
  verification spike below must confirm it holds before trusting it in production math.

## Data flow (both engines)

1. Caller invokes `estimate_query_cost("snowflake"|"databricks", sql, warehouse, ...)`.
2. `explain_estimate` runs its existing EXPLAIN/EXPLAIN COST call exactly as today, producing
   `estimated_bytes`.
3. `explain_estimate` calls the new `_lookup_historical_runtime(...)`.
4. If a signal comes back with `sample_count >= 1`, use `signal.avg_runtime_hours` as the runtime factor
   and append a caveat naming the sample count and averaged seconds.
5. Otherwise, call the existing `scale_runtime_hours(estimated_bytes, baseline_hours=...)` exactly as
   today — **zero behavior change for the no-match case**, which will be the common case for any
   genuinely novel, agent-generated SQL.

## Error handling

- The lookup function's contract is: **it never raises**. Every failure mode (permission error despite
  the least-privilege grant assumption, network timeout, malformed response, an empty/None result) 
  returns `None`, which the caller treats identically to "no history found."
- This mirrors the exact best-effort discipline already established in this codebase for
  `cur.cancel()` (Databricks `execute_bounded`) and `conn.close()` (both engines, hardening-plan Phase 3)
  — a defensive function whose failure must never mask or worsen the primary operation's outcome.
- The lookup's own timeout (5s) is deliberately short relative to `_MAX_EXECUTION_WAIT_SECONDS` (120s,
  the existing execution-timeout constant) — this is a cheap metadata read, not a data-scanning query, so
  if it takes anywhere near that long something is already wrong and falling back immediately is correct.

## Testing

Per engine, mocking the warehouse client:
1. **Exact match found, single sample** — history contains one row with identical (whitespace-differing)
   SQL text and a known elapsed time → assert the returned `estimated_cost_usd` uses that real runtime,
   not the tiered heuristic, and the caveat mentions "1 historical run."
2. **Exact match found, multiple samples** — assert the AVERAGE across samples is used, and the caveat's
   count matches.
3. **No match** — history contains only different queries → assert behavior is byte-for-byte identical
   to today's (existing tiered-heuristic tests must still pass unchanged).
4. **Lookup raises an exception** (permission error, network error) → assert graceful fallback to the
   tiered heuristic, no exception propagates out of `explain_estimate`.
5. **Lookup times out** (thread still alive after the bounded wait) → assert graceful fallback, same as
   the exception case.
6. **Whitespace-only differences still match** — one row with extra/different whitespace than the query
   being estimated → assert it still counts as a match (proves the normalization function works).
7. **Case differences do NOT match** — one row differing only in case → assert it does NOT count as a
   match (proves the deliberate no-case-folding decision from the design phase).
8. **Cache-hit rows are excluded from the average** — a history mock with two matching rows, one a real
   execution (`bytes_scanned > 0` / `cache_query_id is None`) and one a cache hit
   (`bytes_scanned == 0` / `cache_query_id` set) → assert the returned average uses ONLY the real
   execution's runtime, and `sample_count == 1`, not 2. This is the single most important test in this
   whole feature - it is the difference between the calibration helping and it silently corrupting itself.
9. **All matches are cache hits** — every matching row is a cache hit → assert this counts as "no usable
   signal" (falls back to the tiered heuristic), not as zero-runtime signal.

## Rejected / deferred alternatives

- **Own persistence layer** (SQLite file, recording every `run_query_bounded` execution over time) —
  rejected for this iteration: genuinely more powerful (works even before a query has run against the
  warehouse's own retained history window, and isn't bounded by Snowflake's 7-day/10,000-row cap), but
  introduces a real new architectural component (schema, retention/cleanup policy, a question of where
  state lives for a local stdio process) with no existing precedent in this codebase. Revisit if the
  warehouse-history approach's exact-match hit rate turns out too low in practice to be worth keeping.
- **Table-level fuzzy matching** (parse SQL via `sqlglot`, match on referenced tables rather than exact
  text) — rejected for this iteration: meaningfully broader coverage, but adds a new dependency and a new
  parsing-failure surface (CTEs, subqueries, dialect quirks) for a first version. Worth reconsidering once
  exact-match's real-world hit rate is observed — if agents mostly generate novel one-off SQL (the
  expected common case), exact-match will rarely fire, and table-level matching would be the natural next
  iteration.
- **Opt-in via an env var** — rejected: the lookup only reads metadata (no billed bytes scanned) and
  degrades safely on any failure, so there is no meaningful risk to gate behind a flag, consistent with
  how the byte-size-tier fix itself shipped without one.

## Open questions carried into implementation (not blocking, but must be resolved by a verification spike, not assumed)

1. Exact unit and null-handling of Snowflake's `TOTAL_ELAPSED_TIME` and Databricks' `duration` fields —
   confirm against each live trial/Free-Edition account before trusting the ms→hours conversion math.
2. Whether `databricks-sdk`'s HTTP layer accepts a request-level timeout kwarg (preferred) versus needing
   a manual thread-based timeout wrapper (fallback, mirroring the Snowflake implementation).
3. Snowflake's `INFORMATION_SCHEMA.QUERY_HISTORY`'s exact `EXECUTION_STATUS` success-value string — this
   spec assumes `'SUCCESS'` matching `ACCOUNT_USAGE.QUERY_HISTORY`'s documented convention, but the
   `INFORMATION_SCHEMA` variant has not been independently confirmed to use the identical string.
4. Whether `BYTES_SCANNED > 0` is actually a reliable result-cache-hit proxy on
   `INFORMATION_SCHEMA.QUERY_HISTORY` specifically - confirm by running the exact same query twice in a
   row against the live Snowflake trial account and checking whether the second run's `BYTES_SCANNED`
   drops to 0. If it does not reliably do so, find the correct signal (Snowflake's query profile / plan
   metadata may expose a more explicit cache indicator) before shipping - do not ship the byte-scanned
   heuristic un-verified, since a wrong assumption here would silently defeat the entire point of the
   cache-hit exclusion.
