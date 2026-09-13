# cost-guard-mcp

<!-- mcp-name: io.github.mcpsmiths/cost-guard-mcp -->

[![PyPI](https://img.shields.io/pypi/v/cost-guard-mcp)](https://pypi.org/project/cost-guard-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/cost-guard-mcp)](https://pypi.org/project/cost-guard-mcp/)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.mcpsmiths%2Fcost--guard--mcp-blue)](https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.mcpsmiths/cost-guard-mcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![tests](https://github.com/mcpsmiths/cost-guard-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/mcpsmiths/cost-guard-mcp/actions/workflows/tests.yml)

Pre-flight query cost & result-size guardrails for AI agents, across BigQuery and Snowflake — before the query ever runs.

## Why

An AI agent using a warehouse MCP can silently trigger a full-table scan that costs hundreds of dollars, or return millions of rows that flood its own context window. No existing warehouse MCP tells the agent "how much will this cost" or "how much data will this return" *before* running the query.

## What makes this different

- **Every cost estimate discloses its accuracy tier** — `PRECISE` (BigQuery `dryRun`), `UPPER_BOUND` (Snowflake `EXPLAIN`), or `HEURISTIC` (Databricks, not yet shipped) — so your agent never over-trusts a heuristic number.
- **Per-call bounds** — `run_query_bounded` takes `max_bytes_billed` / `max_rows` / `max_estimated_cost_usd` on each call; no shared session state required.
- **Zero infrastructure** — a single local stdio process. No database, no gateway, no Docker Compose.

## Tools

- `describe_engine_capabilities(engine)` — what's exact vs. approximate for this engine.
- `estimate_query_cost(engine, sql, warehouse?)` — pre-flight cost estimate, tagged with its accuracy tier.
- `run_query_bounded(engine, sql, max_bytes_billed?, max_rows?, max_estimated_cost_usd?)` — refuses to run if the estimate exceeds your bound.

## Setup

### BigQuery
Set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account key file path (or run `gcloud auth application-default login`).

### Snowflake
Set `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE` (required — no default, never `ACCOUNTADMIN`), and either `SNOWFLAKE_PRIVATE_KEY_PATH` (preferred) or `SNOWFLAKE_PASSWORD` (discouraged).

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
4. **Restart your MCP client** so it picks up the new server config, then ask your agent
   to call `estimate_query_cost` against a public dataset — for example:

   > Use cost-guard-mcp's estimate_query_cost tool on bigquery for this query:
   > `SELECT name, SUM(number) AS total FROM `bigquery-public-data.usa_names.usa_1910_2013`
   > GROUP BY name ORDER BY total DESC LIMIT 10`

5. **You'll know it worked** when the response looks like this — the exact numbers will
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

If you get a `ConfigError` mentioning `GOOGLE_APPLICATION_CREDENTIALS` instead, your MCP
client didn't pass the env var through to the server process — see the credentials note
above, and double check the value is set inside the client's own server config block, not
just your shell.

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
```

## Known limitations

- Databricks is not yet supported (deferred past v1).
- Snowflake's `UPPER_BOUND` estimate excludes Cortex AI Function ("AI Credits") cost.
- BigQuery Editions/capacity-billed projects cannot get a dollar estimate — only a byte count (capacity billing has no fixed $/byte rate).

## More docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — component/data-flow map
- [`DECISIONS.md`](DECISIONS.md) — why the design looks the way it does
- [`CONTEXT.md`](CONTEXT.md) — terminology glossary (BigQuery/Snowflake concepts that sound alike but aren't)
- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`CONTRIBUTING.md`](CONTRIBUTING.md) / [`AGENTS.md`](AGENTS.md) — contributing and build/test/lint commands
- [`SECURITY.md`](SECURITY.md) — vulnerability reporting

## License

MIT
