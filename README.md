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

Whatever MCP client/host you use (Claude Desktop, etc.) spawns this server as its own subprocess — it does **not** automatically inherit your shell's environment variables, even if they're set in your `.zshrc`/`.bashrc`. Put them directly in the host's server config instead. For Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cost-guard-mcp": {
      "command": "uvx",
      "args": ["cost-guard-mcp"],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/service-account.json",
        "BIGQUERY_PROJECT": "your-project-id",
        "SNOWFLAKE_ACCOUNT": "your-account",
        "SNOWFLAKE_USER": "your-user",
        "SNOWFLAKE_ROLE": "your-role",
        "SNOWFLAKE_PRIVATE_KEY_PATH": "/path/to/rsa_key.p8"
      }
    }
  }
}
```

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

## Known limitations

- Databricks is not yet supported (deferred past v1).
- Snowflake's `UPPER_BOUND` estimate excludes Cortex AI Function ("AI Credits") cost.
- BigQuery Editions/capacity-billed projects cannot get a dollar estimate — only a byte count (capacity billing has no fixed $/byte rate).

## License

MIT
