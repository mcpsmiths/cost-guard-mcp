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

- `check_credentials(engine, warehouse?)` — verifies credentials/connectivity with one lightweight session-context statement (no real query, no dry run); call this first after configuring a new engine. Supports BigQuery, Snowflake, and Databricks. On Snowflake/Databricks it runs on the named (or default/configured) warehouse and can resume a suspended or start a stopped warehouse, billing compute — see Known limitations.
- `describe_engine_capabilities(engine)` — what's exact vs. approximate for this engine.
- `estimate_query_cost(engine, sql, warehouse?, warehouse_size?, edition?)` — pre-flight cost estimate, tagged with its accuracy tier. Supports BigQuery, Snowflake, and Databricks. Accepts exactly one SQL statement per call (see Known limitations). `warehouse_size` (Snowflake/Databricks) and `edition` (Snowflake) can only raise the estimate — the dollar figure is priced at the warehouse the query is pinned to, as the server observes it (the `SHOW WAREHOUSES` size/type/generation, or the Databricks Warehouses API), and at the operator-configured `SNOWFLAKE_EDITION`, whichever is higher. Warehouse types without a modelled rate (Snowpark-optimized, Databricks Classic/Pro) get a byte estimate but no dollar figure.
- `run_query_bounded(engine, sql, max_bytes_billed?, max_rows?, max_estimated_cost_usd?, warehouse?, warehouse_size?, edition?)` — refuses to run if the estimate exceeds your bound. Supports BigQuery, Snowflake, and Databricks. Accepts exactly one SQL statement per call (see Known limitations). A `max_estimated_cost_usd` cap is checked against that observed-warehouse estimate and refuses when no dollar figure exists.

## Setup

### BigQuery
Set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account key file path (or run `gcloud auth application-default login`).

### Snowflake
Set `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE` (required — no default, never `ACCOUNTADMIN`), and either `SNOWFLAKE_PRIVATE_KEY_PATH` (preferred) or `SNOWFLAKE_PASSWORD` (discouraged). Optionally set `SNOWFLAKE_EDITION` to the account's edition — `standard` (default), `enterprise`, `business_critical` or `vps` — which sets the USD/credit price of every estimate; the per-call `edition` argument can only raise it.

- `SNOWFLAKE_ROLE` is the *only* authorization boundary for the SQL an agent sends through
  this server, so it is enforced, not just documented. `ACCOUNTADMIN`, `SECURITYADMIN` and
  `ORGADMIN` are rejected (case-insensitively, quoted or not) before any Snowflake
  connection is opened: the check runs when the Snowflake configuration is first loaded,
  which is on the first Snowflake tool call, so the server process itself starts regardless
  of the value and a bad role surfaces as `check_credentials` returning `ok: false` (or as a
  tool error from `estimate_query_cost`/`run_query_bounded`), not as a startup exit. Every
  session also activates **only** that role — secondary roles are pinned to `NONE` in the
  login request, so a user whose Snowflake `DEFAULT_SECONDARY_ROLES` is `('ALL')` no longer
  lends every other role it holds to model-written SQL. `SNOWFLAKE_USER` should ideally hold
  no role other than `SNOWFLAKE_ROLE`.
- `SNOWFLAKE_SECONDARY_ROLES` (optional, default `NONE`; the only other value is `ALL`) is
  the opt-out for deployments that reached objects only through a secondary role. Prefer
  granting those objects to `SNOWFLAKE_ROLE` instead; set `ALL` only if you accept that the
  agent's SQL runs with every role the user holds. `check_credentials` reports the active
  secondary-role set (`CURRENT_SECONDARY_ROLES()`) so you can confirm what is in force.
- Every session also carries a Snowflake-side statement deadline
  (`STATEMENT_TIMEOUT_IN_SECONDS`: 60 s for `check_credentials`/`estimate_query_cost`,
  120 s for `run_query_bounded`, matching its watchdog) so a query cannot keep running —
  and billing — if this process dies mid-call. The value is applied *read-then-lower*: the
  server reads the session's effective value first and only issues `ALTER SESSION` when
  that value is 0 (unbounded), unparseable, or above the target. A stricter account- or
  user-level policy (say 30 s) is therefore never raised; note that a session-level value
  would otherwise override account/user levels in Snowflake's parameter hierarchy (only the
  warehouse level combines lowest-wins).
- Snowflake sessions are also pinned to one statement per call: `EXPLAIN USING JSON` and the
  `run_query_bounded` execution are submitted with `num_statements=1` and the session logs in
  with `MULTI_STATEMENT_COUNT=1`, so Snowflake itself rejects `;`-separated text that the
  server-side scanner (see Known limitations) did not already refuse. There is no opt-out;
  multi-statement Snowflake text was never supported.

### Databricks
Set `DATABRICKS_SERVER_HOSTNAME` and `DATABRICKS_HTTP_PATH` (from the SQL warehouse's
Connection Details tab), and either `DATABRICKS_TOKEN` (a personal access token,
simplest) or `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` (OAuth machine-to-machine
via a service principal, preferred for automated use). Only Serverless SQL warehouses are
priced — a Classic/Pro warehouse gets a byte estimate but no dollar figure, see Known
Limitations.

### Concurrency (optional)
`COST_GUARD_MAX_IN_FLIGHT_QUERIES` (default `8`) caps how many `run_query_bounded` calls may
be in flight at once — counting calls the host has already cancelled, until their worker
thread exits (at most the 120 s watchdog). A call over the cap fails immediately with a tool
error naming this variable, before any warehouse connection is opened. Must be a positive
integer (the server refuses to start otherwise); raise it only if your host legitimately runs
more concurrent billable queries.

### A note on credentials with MCP hosts

Whatever MCP client/host you use (Claude Desktop, etc.) spawns this server as its own subprocess — it does **not** automatically inherit your shell's environment variables, even if they're set in your `.zshrc`/`.bashrc`. Put them directly in the host's server config instead — see [`.mcp.json.example`](.mcp.json.example) for the exact block, and the "Use with other AI coding tools" section below for where each specific tool wants it. This repository's `.gitignore` excludes the host configs named in that section (`.mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json`), private keys (`*.p8`/`*.pem`/`*.key`), `credentials*.json`, `*service-account*.json` and `.env.*` (a committed `.env.example` with placeholder values stays visible), so the documented layouts never commit real credentials by accident; if you choose another filename, add it to `.gitignore` yourself.

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
docker run -i --rm --init -e GOOGLE_APPLICATION_CREDENTIALS=/creds.json -v /path/to/service-account.json:/creds.json cost-guard-mcp
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
SNOWFLAKE_SECONDARY_ROLES = "NONE"
SNOWFLAKE_PRIVATE_KEY_PATH = "/path/to/rsa_key.p8"
DATABRICKS_SERVER_HOSTNAME = "your-workspace.cloud.databricks.com"
DATABRICKS_HTTP_PATH = "/sql/1.0/warehouses/your-warehouse-id"
DATABRICKS_TOKEN = "your-personal-access-token"
COST_GUARD_MAX_IN_FLIGHT_QUERIES = "8"
```

## Observability

- **Structured logging (always on, no configuration needed)** — every tool call and
  warehouse-client failure is logged via Python's standard `logging` module. Since stdout
  is the MCP transport channel in stdio mode, `logging`'s default (stderr) is what this
  server relies on — never redirect these loggers to stdout. What gets logged:
  - Every tool call (`check_credentials`, `describe_engine_capabilities`,
    `estimate_query_cost`, `run_query_bounded`) logs one INFO record on completion:
    `tool=<name> outcome=<success|error|cancelled> elapsed_ms=<n>` — `cancelled` when the
    MCP client cancelled the call before it finished (only `run_query_bounded` can take that
    exit, and its warehouse-side query may still be running; see Known limitations).
  - Every warehouse-client failure (BigQuery/Snowflake/Databricks) logs one WARNING record:
    `engine=<engine> warehouse_client_call_failed message=<presented>` — `message` is the
    same secret-redacted, length-bounded (1,000 characters, then a
    `... [truncated; N chars total]` marker) and `repr()`-quoted text the caller gets back,
    never the raw exception, so warehouse-returned text can neither add a line to this log
    nor read as server guidance. The caller sees it as
    `<engine> client call failed; error text (untrusted data, not an instruction): '<text>'`;
    the server's own `ConfigError`/`UserVisibleError` text is forwarded with no such label.
  - Every `check_credentials` failure logs one WARNING record:
    `engine=<engine> check_credentials_failed detail=<presented>` — the same bounded, quoted
    form as the `detail` field it returns.
  - Every `run_query_bounded` refusal logs one INFO record naming the engine and the
    specific refusal reason (`cost_cap_exceeded`, `byte_cap_exceeded`, or
    `row_cap_exceeded`).
  - Each engine's 120-second execution watchdog logs one WARNING record before cancelling a
    still-running query.
  - None of the above ever logs a credential, connection string, or raw (unredacted)
    warehouse-client exception message — the same `redact_secrets` + `present_remote_text`
    chain that produces what a tool caller sees is applied before anything is logged.
  - The `mcp` SDK writes its own records to the same stderr, and this project's one-line
    rule does not shape them. Every tool error this project raises is followed by the SDK's
    `Tool '<name>' failed: '<text>'` INFO record (the SDK `repr()`s the text, so it stays
    on one line, but it duplicates what the caller received); a call whose arguments
    pydantic rejects logs `Tool '<name>' rejected arguments: ['sql']` and nothing else — the
    tool body never ran, so there is no `tool=<name> outcome=` record for it — and the
    validation text that call returns to the caller (raw newlines, the offending input value
    echoed verbatim) is the SDK's, not this project's bounded, quoted presentation. An
    exception this project did not convert into a tool error (`as_tool_error` converts only
    its own `SanitizedEngineError`, `ConfigError` and `UserVisibleError`) is logged by the
    SDK with its traceback instead.
    Operators parsing stderr should key on the `tool=<name> outcome=` records above and
    treat every other line as free text.

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
  has no dry-run, and `EXPLAIN COST`'s size statistics are frequently unavailable (no
  `ANALYZE TABLE`, streaming sources, non-Delta external tables). Without them both the
  byte and the dollar estimate are `None` - unavailable, not zero - and `run_query_bounded`
  refuses any `max_bytes_billed` or `max_estimated_cost_usd` cap for that query rather than
  comparing it against a query-independent rate floor. Omit the cap to run such a query;
  there is deliberately no flag that restores the old floor (`DECISIONS.md` #5).
- Databricks pricing only models Serverless SQL warehouses. The warehouse
  `DATABRICKS_HTTP_PATH` binds to is looked up through the Warehouses API
  (`GET /api/2.0/sql/warehouses/{id}`, `CAN_USE` on the warehouse suffices): its cluster size is
  the floor of the estimate (`warehouse_size` can only raise it), and a Classic/Pro warehouse
  (different DBU rates plus a separate cloud VM cost not modeled here) gets no dollar figure,
  so a `max_estimated_cost_usd` cap refuses — omit the dollar cap to run there. If the lookup
  fails (e.g. a principal without `CAN_VIEW`/`CAN_USE`, or an `http_path` that is not a
  `/sql/1.0/warehouses/<id>` path), the estimate keeps the caller-asserted size with a caveat.
- Databricks has no per-query warehouse override - the SQL warehouse is fixed by
  `DATABRICKS_HTTP_PATH` at connect time.
- Snowflake's `UPPER_BOUND` estimate excludes Cortex AI Function ("AI Credits") cost.
- Snowflake warehouse size, type and generation (Gen1 vs. the newer, pricier Gen2) are read
  from the pinned warehouse's `SHOW WAREHOUSES` row, plus `CURRENT_REGION()` for the cloud
  provider (all ordinary, non-privileged SQL); when no `warehouse` argument is given, the
  session's current warehouse (`CURRENT_WAREHOUSE()`) is used. The observed size is the floor
  of the estimate (`warehouse_size` can only raise it) and the generation picks the credit
  rate — Gen2 bills ~1.35x Gen1 on AWS/GCP and ~1.25x on Azure. A Snowpark-optimized or
  otherwise non-STANDARD warehouse, or an unrecognised size, gets a byte estimate but no
  dollar figure, so a `max_estimated_cost_usd` cap refuses — omit the dollar cap to run there.
  If the lookup fails for any reason (permission, timeout, unrecognized response shape, no
  current warehouse), the estimate falls back to the caller-asserted size at Gen1 rates with
  an explicit caveat rather than erroring — since Gen2 is now the default for new standard
  warehouses in most regions, an undetectable generation means the real cost may be higher
  than this estimate. Live-verified 2026-09-18 against a real trial account: its default
  warehouse (`COMPUTE_WH`) is genuinely Gen2 on AWS, and the detection applied the Gen2 rate;
  the `SHOW WAREHOUSES` size/type text shape and `CURRENT_WAREHOUSE()` resolution are pinned
  by `tests/integration/test_snowflake_live.py` (a release gate for 0.4.0).
- Snowflake's edition is operator configuration (`SNOWFLAKE_EDITION`, default `standard`),
  because the server cannot observe it without ORGADMIN; the per-call `edition` argument can
  only raise the USD/credit price, never lower it. A null or non-integer `bytesAssigned` in
  EXPLAIN's plan yields no byte or dollar estimate (unless a historical runtime for the exact
  query exists) rather than the information-free baseline figure.
- `check_credentials` and `estimate_query_cost` carry `readOnlyHint: true` and never run your
  query, but they are not free of compute on Snowflake and Databricks: they run lightweight
  statements (a session-context `SELECT`, `EXPLAIN`, `SHOW WAREHOUSES`, `CURRENT_REGION()`, a
  `QUERY_HISTORY` read; on Databricks `SELECT current_user()` / `EXPLAIN COST`) on the named,
  default or `DATABRICKS_HTTP_PATH` warehouse session, which can resume a suspended
  `AUTO_RESUME` warehouse or start a stopped SQL warehouse and bill compute until it suspends
  or auto-stops again. Which of these statements actually trigger a resume/start, and whether
  the read-only tools should refuse on a non-running warehouse unless opted in, is an open
  owner decision (audit records `read-only-tools-resume-suspended-warehouse` and
  `read-only-tools-auto-start-stopped-warehouse`) — until it lands, point these tools at an
  already-running warehouse if an unplanned resume would be unwelcome.
- BigQuery Editions/capacity-billed projects cannot get a dollar estimate — only a byte count (capacity billing has no fixed $/byte rate).
- BigQuery dry runs always report 0 bytes processed for tables protected by row-level security, by design, to prevent a side-channel — `dry_run` adds a caveat when it sees 0 bytes against a non-empty `referenced_tables` list, but a $0.00 estimate on such a query must never be treated as proof the query is free to run.
- On an on-demand BigQuery project, `run_query_bounded`'s `max_estimated_cost_usd` is also enforced server-side: the job carries a `maximumBytesBilled` derived from the dollar cap (cap ÷ $6.25/TiB, rounded up to the next MiB, floored at BigQuery's 10 MiB minimum), lowered further to a caller-supplied `max_bytes_billed` but never raised above it. This is what makes the cap hold for the shapes the dry run cannot price — row-level-security-protected tables (always 0 bytes), `LOWER_BOUND` estimates and absent statistics. A query that would bill more than that limit is rejected by BigQuery with no charge and surfaces as a normal `status: "refused"` result (`reason: cost_cap_exceeded` when the limit came from the dollar cap, `byte_cap_exceeded` when it was your own byte cap), not as an error. Residual: BigQuery bills a 10 MiB minimum per referenced table, so a tight cap on a many-table query can still be provider-rejected after passing the gate — it now shows up as that structured refusal rather than a provider error string. A `NaN` `max_estimated_cost_usd` is rejected outright (it would compare as "under cap" for every estimate); `+inf` still means "no dollar cap".
- BigQuery remote functions and BigQuery ML remote-model inference (e.g. `ML.GENERATE_TEXT`) incur separate Cloud Run/Vertex AI billing that this byte-based dollar estimate does not include — `dry_run` flags this with a conservative text-based heuristic (`ML.GENERATE_TEXT` or `CREATE FUNCTION` + `REMOTE` in the query text) rather than the dry-run response's `referencedRoutines` field. Attempted to resolve this live (2026-09-23) once BigQuery credentials existed in CI: the deliberately least-privilege service account correctly lacks `bigquery.datasets.create`, so a throwaway routine could not be created to test dry-run population of `referencedRoutines` — and granting broader access just to answer this research question would contradict the least-privilege posture this project maintains everywhere else. Still an open follow-up for whoever next has a project where they can test this safely.
- `estimate_query_cost` and `run_query_bounded` accept exactly one SQL statement per call. The cost cap is checked against one estimate of the submitted text, and the identical text is then handed to the engine — a second statement riding behind a top-level `;` would execute under a cap that was never checked against it. A small fail-closed scanner (`src/cost_guard_mcp/sql_scope.py`, no SQL-parser dependency) therefore refuses, before any dry run/EXPLAIN or execution, any text with a top-level `;` followed by more SQL; a single trailing `;` (optionally followed by whitespace/comments) is fine. It understands only the literal and comment forms each dialect actually has (`'...'` with `\'`/`''`, `"..."`, `--`/`/* */` everywhere; BigQuery backticks, `#` comments, triple quotes and `r`/`b`/`rb` prefixes; Databricks backticks, `r` raw strings and `X'..'` hex; Snowflake `$$...$$` and `//` comments) and refuses anything it cannot resolve with certainty: an unterminated literal/comment, an identifier glued to a quote (`THEN'x'`, `DATE'2020-01-01'` — add a space), a backslash right before the closing quote of a raw/hex literal or quoted identifier, or a `/*` inside a block comment. Those refusals are deliberate false positives in the safe direction. Operators who knowingly run multi-statement scripts (e.g. BigQuery `DECLARE ...; SELECT ...;`) can set `COST_GUARD_ALLOW_MULTI_STATEMENT_SQL=1` on the server to skip the check, accepting that the cap then prices only what the provider's dry run/EXPLAIN reported for the whole script. The opt-out only skips this server's scanner, and two limits follow from that: it does not apply on Snowflake, where the per-call `MULTI_STATEMENT_COUNT=1` pin (see Setup) still rejects the text (`Actual statement count 2 did not match the desired statement count 1.`); and it must be used without `max_rows`, because the row cap wraps the whole submitted text in one `SELECT * FROM (...) AS cost_guard_row_cap LIMIT n` subquery, which is not valid SQL for a script.
- `run_query_bounded` returns result rows in a JSON-safe form on every engine and on both the `ok` and `row_cap_exceeded` paths: `BYTES`/`BINARY` cells are standard base64 text (always, even when the bytes happen to be valid UTF-8), `NaN`/`±Inf` floats are the strings `"nan"`/`"inf"`/`"-inf"`, nested `STRUCT`/`ARRAY`/`MAP` values are recursed into with non-string keys stringified, `DECIMAL`/`DATE`/`TIME`/`TIMESTAMP`/`INTERVAL`-as-`timedelta`/`UUID` values keep their native JSON rendering, and any other driver-native type (e.g. BigQuery `INTERVAL`, which arrives as a `dateutil` `relativedelta`) is returned as its `str()`. Before this, such cells made the MCP SDK's result serialisation fail *after* the billed query had run, so the model saw only `Error executing tool run_query_bounded` and the result was lost.
- `run_query_bounded` gives up on a still-running query after 120 seconds and cancels it (BigQuery: `QueryJob.cancel()`; Snowflake: `SYSTEM$CANCEL_QUERY`; Databricks: `Cursor.cancel()` from a watchdog thread) rather than waiting indefinitely — a query stuck behind slot contention or a cold/suspended warehouse would otherwise block the tool call, and keep burning warehouse-seconds the whole time, defeating the point of a "bounded" tool. Databricks sessions additionally carry a server-side `STATEMENT_TIMEOUT` (60 s for `check_credentials`/`estimate_query_cost`, 120 s for `run_query_bounded`) so the warehouse kills the statement itself if this process dies before the watchdog fires, and the connector's retry budget is bounded to 60 s / 5 attempts (its default is 15 minutes, meant to ride out a cold start) — so a stopped Classic/Pro warehouse whose start-up exceeds ~60 s now makes `check_credentials`/`estimate_query_cost` fail fast with a retry-budget error instead of waiting; note the attempt still starts, and bills, the warehouse.
- Every other BigQuery API round-trip this server makes (the service-account lookup behind `check_credentials`, the dry-run and execution job submissions, the Reservations lookup behind the billing-model check) is bounded to 30 seconds of retries, and the client library's own job re-submission is disabled (`job_retry=None`), so the "up to 120 seconds" statements here also hold while Google is unreachable — before 0.4.0 these inherited google-cloud-bigquery's 600 s default retry (doubled for job submission) and could block a tool call for 10-20 minutes, and an abandoned `run_query_bounded` thread could still submit its billed job minutes after the host had cancelled the call.
- The underlying `mcp` SDK can drop an in-flight tool-call response if the client closes stdin before the tool finishes (upstream issue [modelcontextprotocol/python-sdk#2678](https://github.com/modelcontextprotocol/python-sdk/issues/2678), open since 2026-05, unresolved after several attempted fixes) — no known real-world exposure for well-behaved clients that keep stdin open for the session, but worth knowing about given this server's tool calls can run up to 120 seconds (Databricks OAuth M2M is the one path that can exceed that bound: the `databricks-sdk`'s OIDC discovery keeps its own 300 s retry budget and its token request is untimed, neither tunable through `Config` — an upstream limitation; PAT auth is fully bounded).
- A client-sent MCP cancellation notification against an in-flight `run_query_bounded` call detaches promptly at the MCP bookkeeping level, and since 0.4.0 the server re-checks for that cancellation once more immediately before the execution is dispatched (after the estimate and every cap check), so a cancel that arrives during the estimate phase never starts the billable query at all. A query that was already running when the cancel arrived keeps running in the abandoned background thread until the per-engine watchdog (~120 s, see above) cancels it. As a backstop that no longer depends on this process staying alive, every submitted job also carries a provider-side deadline equal to that watchdog — BigQuery `jobTimeoutMs`, Snowflake `STATEMENT_TIMEOUT_IN_SECONDS` and Databricks `STATEMENT_TIMEOUT` as session parameters (only ever lowered, never raising an operator's stricter account/user/warehouse policy; see `DECISIONS.md` #14) — so a host that escalates to SIGTERM/SIGKILL after closing stdin (the reference MCP client does, 2-4 s after EOF) cannot leave a query billing past that bound. If you run the container, keep the `--init` flag from the install example: without it the server is PID 1, ignores SIGTERM, and `docker stop` ends in SIGKILL. An MCP host that retries a timed-out or cancelled call issues a brand-new billable call; the earlier one is never resumed.
- `run_query_bounded` admits at most `COST_GUARD_MAX_IN_FLIGHT_QUERIES` (default 8) calls at once, and a cancelled call keeps its slot until its abandoned thread exits — the MCP-level detach returns the host's concurrency token immediately, so without this cap a host that cancels and re-issues could pin an unbounded number of warehouse connections and running queries (each still bounded by its own caps and the 120 s watchdog). An over-cap call fails with a tool error naming the variable rather than a `status: "refused"` result, because no estimate exists yet (`DECISIONS.md` #4 and #14).

## More docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — component/data-flow map
- [`DECISIONS.md`](DECISIONS.md) — why the design looks the way it does
- [`CONTEXT.md`](CONTEXT.md) — terminology glossary (BigQuery/Snowflake concepts that sound alike but aren't)
- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`CONTRIBUTING.md`](CONTRIBUTING.md) / [`AGENTS.md`](AGENTS.md) — contributing and build/test/lint commands
- [`SECURITY.md`](SECURITY.md) — vulnerability reporting

## License

MIT
