# Architecture

Current-state design map. For *why* things are shaped this way, see `DECISIONS.md`.

## Shape

A single Python package, one stdio process, no external infrastructure. The server
process is the whole deployment — no database, no gateway, no background workers.

```
MCP client (Claude Desktop, Claude Code, Cursor, ...)
        │  stdio (JSON-RPC)
        ▼
  server.py  ── registers 4 tools on a module-level `mcp: MCPServer`
        │
        ├─ tools/describe_engine_capabilities.py  (pure, static lookup)
        ├─ tools/check_credentials.py              ──┐
        ├─ tools/estimate_query_cost.py             ─┤
        └─ tools/run_query_bounded.py               ─┼─ dispatch by `engine`
                                                       ▼
                       engines/bigquery.py | engines/snowflake.py | engines/databricks.py
                                                       │
              pricing/bigquery_pricing.py | pricing/snowflake_pricing.py | pricing/databricks_pricing.py
                                                       │
         google-cloud-bigquery | snowflake-connector-python | databricks-sql-connector + databricks-sdk
```

## Components

| Module | Responsibility |
|---|---|
| `server.py` | MCP server bootstrap, tool registration, stdio entrypoint. No business logic; owns the process-level concerns around it: `run_query_bounded` runs in a cancellable worker thread behind an in-flight cap (`COST_GUARD_MAX_IN_FLIGHT_QUERIES`, default 8; `DECISIONS.md` #14), `log_hygiene` is installed at import, and `_bootstrap_opentelemetry` registers an OTLP exporter only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (README, "OpenTelemetry tracing"). |
| `types.py` | Shared Pydantic models: `AccuracyTier`, `CostEstimate`, `EngineCapabilities`, `RefusalReason`, `BoundedQueryResult`, `CredentialCheckResult`, and the `Engine` literal (`bigquery`, `snowflake`, `databricks`) every dispatch keys on. Every tool response is one of these. |
| `errors.py` | `sanitize_exceptions` — wraps every warehouse-client call so no raw exception (which may embed credentials) escapes to a log or tool response; `as_tool_error` turns this project's own errors into tool errors; `redact_secrets` and `present_remote_text` (`DECISIONS.md` #18). |
| `log_hygiene.py` | `configure_log_hygiene`: pins the warehouse-client libraries' loggers to WARNING and puts a redacting filter on every root handler, so a library record cannot carry a credential to stderr. |
| `config.py` | Per-engine env-var loading and validation, run at call time (not import time). |
| `sql_scope.py` | Fail-closed single-statement scanner (`assert_single_statement`, `single_statement_body`) and the dialect-aware lexer helpers (`comment_end`, `literal_end`, `dialect_for`) both gates share. `DECISIONS.md` #15. |
| `sql_scope_keywords.py` | Read-statement gate (`assert_read_statement`): first-keyword allowlist plus the `WITH` CTE walk; opt-out `COST_GUARD_ALLOW_WRITE_STATEMENTS=1`; `is_single_query_statement`, the LIMIT-wrap predicate. `DECISIONS.md` #13, #21. |
| `result_bounds.py` | `run_query_bounded`'s result bounds: the default row cap (`DEFAULT_MAX_ROWS`, 1000), the 8 MiB serialised-size cap (`MAX_RESULT_BYTES`; exact measurement and prefix trim, tool side) and the engines' shared early stop (`collect_rows`, `iter_fetchmany`, the lower-bound size estimator). `DECISIONS.md` #21. |
| `engines/bigquery.py` | `is_capacity_billed`, `dry_run`, `execute_bounded`, `check_credentials` — thin wrapper over `google-cloud-bigquery` + `google-cloud-bigquery-reservation`. |
| `engines/bigquery_estimate_rules.py` | The SDK-free rules `dry_run` applies to a dry-run response: BigQuery's `totalBytesProcessedAccuracy` to `AccuracyTier`, the job location the billing-model check is made for (`validate_location`, `job_location`), and the remote-billing text heuristic. Split out of `engines/bigquery.py` for size; `DECISIONS.md` #22. |
| `engines/snowflake.py` | `_connect`, `explain_estimate`, `execute_bounded`, `check_credentials` — thin wrapper over `snowflake-connector-python`. |
| `engines/databricks.py` | `explain_estimate`, `execute_bounded`, `check_credentials` — thin wrapper over `databricks-sql-connector` and the `databricks-sdk` Warehouses API. |
| `pricing/bigquery_pricing.py` | On-demand `$/TiB` constant. |
| `pricing/snowflake_pricing.py` | Gen1 warehouse credit-rate table + `$/credit` by edition. |
| `pricing/databricks_pricing.py` | Serverless SQL warehouse DBU-per-hour table by size + the `$/DBU` constant. |
| `pricing/runtime_scaling.py` | `scale_runtime_hours`: the coarse byte-size tiers that scale the Snowflake and Databricks baseline runtime up, never down. |
| `tools/check_credentials.py`, `tools/describe_engine_capabilities.py`, `tools/estimate_query_cost.py`, `tools/run_query_bounded.py` | Business logic for each of the 4 MCP tools — pure functions, independently testable, imported (not duplicated) by `server.py`'s tool-registration wrappers. |
| `tools/result_rows.py` | `run_query_bounded`'s row normaliser (`DECISIONS.md` #17), split out for size. |

## Data flow: `run_query_bounded`

1. The text must be exactly one statement and a read statement (`sql_scope`,
   `sql_scope_keywords`), then `estimate_query_cost(engine, sql, warehouse)` is called
   internally — every bounded run starts with a real pre-flight estimate, never a bare
   execution.
2. If a cap was requested (`max_estimated_cost_usd` / `max_bytes_billed`) and the
   corresponding estimate field is `None`, refuse (fail-closed — see `DECISIONS.md`).
3. If the estimate exceeds the cap, refuse with a `hint` and the estimate attached.
4. Otherwise, execute via the engine's `execute_bounded`. `max_rows` defaults to 1000; a
   single `SELECT`/`WITH`/`VALUES` statement is wrapped in a `LIMIT max_rows + 1` subquery so
   row-capping is enforced on the wire, other admitted statements run unwrapped. Every engine
   fetches in pages (`result_bounds.collect_rows`) and stops at the `max_rows + 1` sentinel row
   or once a lower-bound size estimate crosses the 8 MiB cap.
5. The rows are normalised to JSON-safe values (`tools/result_rows.py`), then measured
   exactly (`result_bounds.trim_rows_to_byte_cap`). If they serialise over 8 MiB, refuse
   post-execution with `result_size_exceeded`; otherwise, if more than `max_rows` rows came
   back, with `row_cap_exceeded`. Both carry `executed: true` and the truncated prefix of
   whole rows; the size check runs first because the row-cap hint ("showing the first N")
   would be false once rows were dropped for size. `executed` is `false` on every
   pre-execution refusal, and on a BigQuery refusal under the attached `maximumBytesBilled`
   (`DECISIONS.md` #16).

## The accuracy-tier contract

Every `CostEstimate` carries `accuracy_tier: AccuracyTier` — `PRECISE`, `UPPER_BOUND`, or
`HEURISTIC`. This is the project's core differentiator, so it's enforced structurally
(a required Pydantic field, not a convention):

- **BigQuery** → `PRECISE`, from a real `dryRun` API call, downgraded to `UPPER_BOUND`
  when BigQuery's own `totalBytesProcessedAccuracy` reports `UPPER_BOUND` or `UNKNOWN`, to
  `HEURISTIC` with no dollar figure when it reports `LOWER_BOUND` or 0 bytes for a statement
  that references a table (the row-level-security shape; `DECISIONS.md` #22), and carries no
  dollar figure at all when the project is capacity-billed (Editions) — only bytes.
- **Snowflake** → always `UPPER_BOUND`, from `EXPLAIN USING JSON` against the target
  warehouse. Never `PRECISE` — Snowflake bills by warehouse-time, not bytes, and
  `EXPLAIN`'s own figures are documented upper bounds.
- **Databricks** → always `HEURISTIC`, from `EXPLAIN COST` (Spark's cost-based-optimiser
  size estimate) priced at the Serverless DBU rate. There is no dry-run equivalent, so it is
  never `PRECISE` or `UPPER_BOUND`. On a warehouse that is not `RUNNING` no session is
  opened and the estimate carries no byte or dollar figure (`DECISIONS.md` #20).

## Transport and credentials

Stdio only — no remote HTTP transport exists in this codebase. Credentials load from
environment variables only, validated per-call by `config.py`, never accepted as tool
parameters and never written to a committed file. See the README's "A note on credentials
with MCP hosts" section for why a client's config file (not a shell profile) is where
these actually need to live.
