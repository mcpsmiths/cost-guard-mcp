# cost-guard-mcp

<!-- mcp-name: io.github.mcpsmiths/cost-guard-mcp -->

[![PyPI](https://img.shields.io/pypi/v/cost-guard-mcp)](https://pypi.org/project/cost-guard-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/cost-guard-mcp)](https://pypi.org/project/cost-guard-mcp/)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.mcpsmiths%2Fcost--guard--mcp-blue)](https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.mcpsmiths/cost-guard-mcp)
[![MCP Marketplace](https://img.shields.io/badge/MCP%20Marketplace-listed-blue)](https://mcp-marketplace.io/server/io-github-mcpsmiths-cost-guard-mcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![tests](https://github.com/mcpsmiths/cost-guard-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/mcpsmiths/cost-guard-mcp/actions/workflows/tests.yml)

Pre-flight query cost & result-size guardrails for AI agents, across BigQuery, Snowflake, and Databricks — before the query ever runs.

## Why

An AI agent using a warehouse MCP can silently trigger a full-table scan that costs hundreds of dollars, or return millions of rows that flood its own context window. No existing warehouse MCP tells the agent "how much will this cost" or "how much data will this return" *before* running the query.

## What makes this different

- **Every cost estimate discloses its accuracy tier** — `PRECISE` (BigQuery `dryRun`), `UPPER_BOUND` (Snowflake `EXPLAIN`), or `HEURISTIC` (Databricks `EXPLAIN COST`) — so your agent never over-trusts a heuristic number.
- **Per-call bounds** — `run_query_bounded` takes `max_bytes_billed` / `max_rows` / `max_estimated_cost_usd` on each call; no shared session state required.
- **Zero infrastructure** — a single local stdio process. No database, no gateway, no Docker Compose.

## Tools

- `check_credentials(engine, warehouse?)` — verifies credentials/connectivity without running any real query; call this first after configuring a new engine. Supports BigQuery, Snowflake, and Databricks.
- `describe_engine_capabilities(engine)` — what's exact vs. approximate for this engine.
- `estimate_query_cost(engine, sql, warehouse?, warehouse_size?, edition?)` — pre-flight cost estimate, tagged with its accuracy tier. Supports BigQuery, Snowflake, and Databricks. `warehouse_size` (Snowflake/Databricks) and `edition` (Snowflake) default to the smallest/standard tier if omitted — set them to match the warehouse you actually run on, or the dollar figure understates cost on a larger one.
- `run_query_bounded(engine, sql, max_bytes_billed?, max_rows?, max_estimated_cost_usd?, warehouse?, warehouse_size?, edition?)` — refuses to run if the estimate exceeds your bound. Supports BigQuery, Snowflake, and Databricks.

## Setup

### BigQuery
Set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account key file path (or run `gcloud auth application-default login`).

### Snowflake
Set `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE` (required — no default, never `ACCOUNTADMIN`), and either `SNOWFLAKE_PRIVATE_KEY_PATH` (preferred) or `SNOWFLAKE_PASSWORD` (discouraged).

### Databricks
Set `DATABRICKS_SERVER_HOSTNAME` and `DATABRICKS_HTTP_PATH` (from the SQL warehouse's
Connection Details tab), and either `DATABRICKS_TOKEN` (a personal access token,
simplest) or `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` (OAuth machine-to-machine
via a service principal, preferred for automated use). Only Serverless SQL warehouses are
priced accurately — see Known Limitations.

### A note on credentials with MCP hosts

Whatever MCP client/host you use (Claude Desktop, etc.) spawns this server as its own subprocess — it does **not** automatically inherit your shell's environment variables, even if they're set in your `.zshrc`/`.bashrc`. Put them directly in the host's server config instead — see [`.mcp.json.example`](.mcp.json.example) for the exact block, and the "Use with other AI coding tools" section below for where each specific tool wants it.

## Install

```bash
uvx cost-guard-mcp
```

Also published on the [official MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.mcpsmiths/cost-guard-mcp) as `io.github.mcpsmiths/cost-guard-mcp`.

For local development instead:

```bash
git clone https://github.com/mcpsmiths/cost-guard-mcp.git
cd cost-guard-mcp
uv sync
uv run cost-guard-mcp
```

Or via Docker:

```bash
docker build -t cost-guard-mcp .
docker run -i --rm -e GOOGLE_APPLICATION_CREDENTIALS=/creds.json -v /path/to/service-account.json:/creds.json cost-guard-mcp
```

## Quickstart (~5 minutes to your first estimate)

This walks through the fastest path to a real tool call — no data of your own required
(it uses a public BigQuery dataset), no Snowflake trial signup needed.

1. **Get a GCP project with the BigQuery API enabled.** Any project works, including the
   free-tier Sandbox mode (no billing card required to run `dryRun`, which is all
   `estimate_query_cost` does). Create one at
   [console.cloud.google.com](https://console.cloud.google.com) if you don't have one.
2. **Get Application Default Credentials**: run `gcloud auth application-default login`
   locally, or create a service-account key and point `GOOGLE_APPLICATION_CREDENTIALS` at
   its JSON file.
3. **Add the server to your MCP client** — see [`.mcp.json.example`](.mcp.json.example),
   filling in only `GOOGLE_APPLICATION_CREDENTIALS` (leave the Snowflake vars out entirely
   for this quickstart).
4. **Restart your MCP client** so it picks up the new server config, then ask your agent to
   call `check_credentials` on bigquery. This confirms your setup without running any real
   query — you should get back `"ok": true` and a detail line naming your project. If you
   get `"ok": false` instead, the `detail` field explains exactly what's missing (usually
   `GOOGLE_APPLICATION_CREDENTIALS` not making it through to the server process — see the
   credentials note above, and double check the value is set inside the client's own server
   config block, not just your shell).
5. **Ask your agent to call `estimate_query_cost`** against a public dataset — for example:

   > Use cost-guard-mcp's estimate_query_cost tool on bigquery for this query:
   > `SELECT name, SUM(number) AS total FROM `bigquery-public-data.usa_names.usa_1910_2013`
   > GROUP BY name ORDER BY total DESC LIMIT 10`

6. **You'll know it worked** when the response looks like this — the exact numbers will
   differ, but `accuracy_tier` should read `PRECISE`:

   ```json
   {
     "engine": "bigquery",
     "accuracy_tier": "PRECISE",
     "estimated_bytes": 320866545,
     "estimated_cost_usd": 0.001842,
     "currency": "USD",
     "caveats": []
   }
   ```

## Use with other AI coding tools

`cost-guard-mcp` is a standard stdio MCP server — any MCP-compatible client works, not just Claude Desktop. Every client ultimately runs the same `command`/`args`/`env`; only the wrapping file format differs, so there's one canonical definition — [`.mcp.json.example`](.mcp.json.example) — instead of a separately maintained copy per tool below.

There is no single file every tool reads automatically (each looks in its own location), but three of the four use the exact same `mcpServers` wrapper `.mcp.json.example` already has, so those need nothing more than copying it into place. Fill in your real credential values, then:

| Client | Where it goes | Change needed from `.mcp.json.example` |
|---|---|---|
| **Claude Code** | `.mcp.json` (project) | None — copy as-is, or `claude mcp add-json cost-guard-mcp '<the "cost-guard-mcp" object>'` |
| **Claude Desktop** | `claude_desktop_config.json` | None — copy as-is |
| **Cursor** | `.cursor/mcp.json` or `~/.cursor/mcp.json` | Add `"type": "stdio"` inside the server object |
| **GitHub Copilot (VS Code)** | `.vscode/mcp.json` | Rename top-level key `mcpServers` → `servers`, add `"type": "stdio"` |
| **OpenAI Codex CLI** | `~/.codex/config.toml` | Same fields, TOML syntax instead of JSON (below) — or `codex mcp add cost-guard-mcp -- uvx cost-guard-mcp` |

Codex is the one genuine exception (TOML, not JSON), so it still needs its own block:

```toml
[mcp_servers.cost-guard-mcp]
command = "uvx"
args = ["cost-guard-mcp"]

[mcp_servers.cost-guard-mcp.env]
GOOGLE_APPLICATION_CREDENTIALS = "/path/to/service-account.json"
BIGQUERY_PROJECT = "your-project-id"
SNOWFLAKE_ACCOUNT = "your-account"
SNOWFLAKE_USER = "your-user"
SNOWFLAKE_ROLE = "your-role"
SNOWFLAKE_PRIVATE_KEY_PATH = "/path/to/rsa_key.p8"
DATABRICKS_SERVER_HOSTNAME = "your-workspace.cloud.databricks.com"
DATABRICKS_HTTP_PATH = "/sql/1.0/warehouses/your-warehouse-id"
DATABRICKS_TOKEN = "your-personal-access-token"
```

## Observability

- **Structured logging (always on, no configuration needed)** — every tool call and
  warehouse-client failure is logged via Python's standard `logging` module. Since stdout
  is the MCP transport channel in stdio mode, `logging`'s default (stderr) is what this
  server relies on — never redirect these loggers to stdout. What gets logged:
  - Every tool call (`check_credentials`, `describe_engine_capabilities`,
    `estimate_query_cost`, `run_query_bounded`) logs one INFO record on completion:
    `tool=<name> outcome=<success|error> elapsed_ms=<n>`.
  - Every warehouse-client failure (BigQuery/Snowflake/Databricks) logs one WARNING record:
    `engine=<engine> warehouse_client_call_failed message=<redacted>` — `message` is always
    the same secret-redacted text the caller gets back, never the raw exception.
  - Every `run_query_bounded` refusal logs one INFO record naming the engine and the
    specific refusal reason (`cost_cap_exceeded`, `byte_cap_exceeded`, or
    `row_cap_exceeded`).
  - Each engine's 120-second execution watchdog logs one WARNING record before cancelling a
    still-running query.
  - None of the above ever logs a credential, connection string, or raw (unredacted)
    warehouse-client exception message — the same `redact_secrets` helper that sanitizes
    what a tool caller sees is applied before anything is logged.

- **OpenTelemetry tracing (opt-in, off by default)** — the underlying `mcp` SDK ships an
  `OpenTelemetryMiddleware` on by default for every server, wrapping each inbound message
  in a SERVER span, but that middleware is a documented no-op until a real exporter is
  registered — this project registers none unless you ask for it. Set
  `OTEL_EXPORTER_OTLP_ENDPOINT` to your OTel Collector's endpoint (e.g.
  `http://localhost:4317`) to turn it on: at that point `cost-guard-mcp` constructs a
  `TracerProvider` with a gRPC OTLP exporter pointed at that endpoint and registers it as
  the global tracer provider before the server starts running. Leave the env var unset and
  nothing changes — no exporter is constructed, and the two extra dependencies below never
  need to be installed. Requires the `otel` extra:
  ```bash
  uv sync --extra otel
  # or: pip install "cost-guard-mcp[otel]"
  ```

## Known limitations

- Snowflake cost estimates are calibrated from the caller's own recent query history
  (`INFORMATION_SCHEMA.QUERY_HISTORY`, no elevated privilege required) when an exact repeat
  of the same SQL text has run before - falling back to a coarse byte-size-tier heuristic
  otherwise. This only fires on an exact repeated query; a genuinely novel query always
  uses the heuristic. Result-cache hits are deliberately excluded from the average (a
  cached, near-instant repeat would otherwise corrupt calibration toward underestimating
  future runtime). Query history ingestion has its own latency - a query run moments ago
  may not yet be visible to the lookup, in which case it safely falls back to the
  heuristic rather than erroring.
- Databricks calibration was investigated and found blocked: its Query History REST API
  returns the query text as `"<REDACTED>"` unconditionally on the account tested, even for
  the caller's own queries and even with `include_metrics=True` - confirmed server-side via
  a direct SDK source read, not something a client-side parameter can bypass. Not
  implemented for Databricks as a result; may be revisited if a future paid workspace
  confirms this is a toggleable setting there.
- Databricks cost estimates are always `HEURISTIC` (the least precise tier) - Databricks
  has no dry-run, and `EXPLAIN COST`'s byte estimates are frequently unavailable.
- Databricks pricing only models Serverless SQL warehouses - Classic/Pro warehouses use
  different (lower) DBU rates plus a separate cloud VM cost not modeled here.
- Databricks has no per-query warehouse override - the SQL warehouse is fixed by
  `DATABRICKS_HTTP_PATH` at connect time.
- Snowflake's `UPPER_BOUND` estimate excludes Cortex AI Function ("AI Credits") cost.
- Snowflake warehouse generation (Gen1 vs. the newer, pricier Gen2) is detected on a
  best-effort basis via `SHOW WAREHOUSES` and `CURRENT_REGION()` (both ordinary,
  non-privileged SQL) to pick the correct credit rate — Gen2 bills ~1.35x Gen1 on AWS/GCP
  and ~1.25x on Azure. Detection needs a `warehouse` to be specified; if it isn't, or the
  lookup fails for any reason (permission, timeout, unrecognized response shape), the
  estimate safely falls back to Gen1 rates with an explicit caveat rather than erroring —
  since Gen2 is now the default for new standard warehouses in most regions, an
  undetectable generation means the real cost may be higher than this estimate. Live-
  verified 2026-09-18 against a real trial account: its default warehouse (`COMPUTE_WH`)
  is genuinely Gen2 on AWS, and this detection correctly identified it and applied the
  Gen2 rate.
- BigQuery Editions/capacity-billed projects cannot get a dollar estimate — only a byte count (capacity billing has no fixed $/byte rate).
- BigQuery dry runs always report 0 bytes processed for tables protected by row-level security, by design, to prevent a side-channel — `dry_run` adds a caveat when it sees 0 bytes against a non-empty `referenced_tables` list, but a $0.00 estimate on such a query must never be treated as proof the query is free to run.
- BigQuery remote functions and BigQuery ML remote-model inference (e.g. `ML.GENERATE_TEXT`) incur separate Cloud Run/Vertex AI billing that this byte-based dollar estimate does not include — `dry_run` flags this with a conservative text-based heuristic (`ML.GENERATE_TEXT` or `CREATE FUNCTION` + `REMOTE` in the query text) rather than the dry-run response's `referencedRoutines` field. Attempted to resolve this live (2026-09-23) once BigQuery credentials existed in CI: the deliberately least-privilege service account correctly lacks `bigquery.datasets.create`, so a throwaway routine could not be created to test dry-run population of `referencedRoutines` — and granting broader access just to answer this research question would contradict the least-privilege posture this project maintains everywhere else. Still an open follow-up for whoever next has a project where they can test this safely.
- `run_query_bounded` gives up on a still-running query after 120 seconds and cancels it (BigQuery: `QueryJob.cancel()`; Snowflake: `SYSTEM$CANCEL_QUERY`; Databricks: `Cursor.cancel()` from a watchdog thread) rather than waiting indefinitely — a query stuck behind slot contention or a cold/suspended warehouse would otherwise block the tool call, and keep burning warehouse-seconds the whole time, defeating the point of a "bounded" tool.
- The underlying `mcp` SDK can drop an in-flight tool-call response if the client closes stdin before the tool finishes (upstream issue [modelcontextprotocol/python-sdk#2678](https://github.com/modelcontextprotocol/python-sdk/issues/2678), open since 2026-05, unresolved after several attempted fixes) — no known real-world exposure for well-behaved clients that keep stdin open for the session, but worth knowing about given this server's tool calls can run up to 120 seconds.
- A client-sent MCP cancellation notification against an in-flight `run_query_bounded` call now detaches promptly at the MCP bookkeeping level, but the warehouse-side query itself keeps running in the abandoned background thread until the existing per-engine watchdog (~120s, see above) fires on its own — this fix does not by itself stop the warehouse from billing for that abandoned query any sooner.

## More docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — component/data-flow map
- [`DECISIONS.md`](DECISIONS.md) — why the design looks the way it does
- [`CONTEXT.md`](CONTEXT.md) — terminology glossary (BigQuery/Snowflake concepts that sound alike but aren't)
- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`CONTRIBUTING.md`](CONTRIBUTING.md) / [`AGENTS.md`](AGENTS.md) — contributing and build/test/lint commands
- [`SECURITY.md`](SECURITY.md) — vulnerability reporting

## License

MIT
