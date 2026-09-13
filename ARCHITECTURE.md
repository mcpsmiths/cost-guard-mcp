# Architecture

Current-state design map. For *why* things are shaped this way, see `DECISIONS.md`.

## Shape

A single Python package, one stdio process, no external infrastructure. The server
process is the whole deployment — no database, no gateway, no background workers.

```
MCP client (Claude Desktop, Claude Code, Cursor, ...)
        │  stdio (JSON-RPC)
        ▼
  server.py  ── registers 3 tools on a module-level `mcp: MCPServer`
        │
        ├─ tools/describe_engine_capabilities.py  (pure, static lookup)
        ├─ tools/estimate_query_cost.py            ──┐
        └─ tools/run_query_bounded.py               ─┼─ dispatch by `engine`
                                                       ▼
                                          engines/bigquery.py | engines/snowflake.py
                                                       │
                                          pricing/bigquery_pricing.py | pricing/snowflake_pricing.py
                                                       │
                                          google-cloud-bigquery | snowflake-connector-python
```

## Components

| Module | Responsibility |
|---|---|
| `server.py` | MCP server bootstrap, tool registration, stdio entrypoint. No business logic. |
| `types.py` | Shared Pydantic models: `AccuracyTier`, `CostEstimate`, `EngineCapabilities`, `RefusalReason`, `BoundedQueryResult`. Every tool response is one of these. |
| `errors.py` | `sanitize_exceptions` — wraps every warehouse-client call so no raw exception (which may embed credentials) escapes to a log or tool response. |
| `config.py` | Per-engine env-var loading and validation, run at call time (not import time). |
| `engines/bigquery.py` | `is_capacity_billed`, `dry_run`, `execute_bounded` — thin wrapper over `google-cloud-bigquery` + `google-cloud-bigquery-reservation`. |
| `engines/snowflake.py` | `_connect`, `explain_estimate`, `execute_bounded` — thin wrapper over `snowflake-connector-python`. |
| `pricing/bigquery_pricing.py` | On-demand `$/TiB` constant. |
| `pricing/snowflake_pricing.py` | Gen1 warehouse credit-rate table + `$/credit` by edition. |
| `tools/*.py` | Business logic for each of the 3 MCP tools — pure functions, independently testable, imported (not duplicated) by `server.py`'s tool-registration wrappers. |

## Data flow: `run_query_bounded`

1. `estimate_query_cost(engine, sql, warehouse)` is called first, internally — every
   bounded run starts with a real pre-flight estimate, never a bare execution.
2. If a cap was requested (`max_estimated_cost_usd` / `max_bytes_billed`) and the
   corresponding estimate field is `None`, refuse (fail-closed — see `DECISIONS.md`).
3. If the estimate exceeds the cap, refuse with a `hint` and the estimate attached.
4. Otherwise, execute via the engine's `execute_bounded`, which wraps the query in a
   `LIMIT max_rows + 1` subquery so row-capping is enforced on the wire, not by fetching
   everything and slicing in Python.
5. If the wrapped fetch returned more than `max_rows`, refuse (even post-execution) and
   return the truncated rows with a `row_cap_exceeded` reason.

## The accuracy-tier contract

Every `CostEstimate` carries `accuracy_tier: AccuracyTier` — `PRECISE`, `UPPER_BOUND`, or
`HEURISTIC`. This is the project's core differentiator, so it's enforced structurally
(a required Pydantic field, not a convention):

- **BigQuery** → `PRECISE`, from a real `dryRun` API call, downgraded to `UPPER_BOUND`
  when BigQuery's own `totalBytesProcessedAccuracy` reports non-precise, or the project is
  capacity-billed (Editions), in which case there's no dollar figure at all — only bytes.
- **Snowflake** → always `UPPER_BOUND`, from `EXPLAIN USING JSON` against the target
  warehouse. Never `PRECISE` — Snowflake bills by warehouse-time, not bytes, and
  `EXPLAIN`'s own figures are documented upper bounds.
- **Databricks** → `HEURISTIC`, not yet implemented (deferred past v1).

## Transport and credentials

Stdio only — no remote HTTP transport exists in this codebase. Credentials load from
environment variables only, validated per-call by `config.py`, never accepted as tool
parameters and never written to a committed file. See the README's "A note on credentials
with MCP hosts" section for why a client's config file (not a shell profile) is where
these actually need to live.
