# AGENTS.md

Instructions for AI coding agents working in this repository.

## Project overview

`cost-guard-mcp` is a Python MCP server: three tools (`describe_engine_capabilities`,
`estimate_query_cost`, `run_query_bounded`) that give an AI agent a pre-flight query-cost
and result-size estimate for BigQuery and Snowflake before a query actually runs. Local
stdio transport only, built on the official `mcp` SDK v2. See `ARCHITECTURE.md` for the
component/data-flow map and `DECISIONS.md` for why the design looks the way it does.

## Setup

```bash
uv sync --all-extras
```

## Build, lint, test

```bash
uv run ruff check src tests        # lint
uv run pytest tests/unit -v --cov=cost_guard_mcp --cov-report=term-missing --cov-fail-under=80
```

Both must pass before committing. CI (`.github/workflows/tests.yml`) runs the same two
commands on every push/PR — there is no separate local-vs-CI command set to keep in sync.

Live-account integration tests (`tests/integration/`) require real BigQuery/Snowflake
credentials and are excluded from the default `pytest` run (`testpaths = ["tests/unit"]`
in `pyproject.toml`). Run them explicitly only when you have credentials configured:

```bash
uv run pytest tests/integration/test_bigquery_live.py -v
uv run pytest tests/integration/test_snowflake_live.py -v
```

## Package manager

Use `uv` only. Never `pip install` directly into this project — dependencies go in
`pyproject.toml`, and `uv sync` resolves them via `uv.lock`.

## Testing rules

- Coverage floor is 80%, enforced by CI (`--cov-fail-under=80`) — a PR that drops below
  it fails the `tests` workflow, not just a local lint nag.
- Every bug fix needs a regression test added in the same commit, not deferred.
- Mock the warehouse client libraries (`google.cloud.bigquery`, `snowflake.connector`) in
  `tests/unit/` — never make real network calls there. Real-account behavior belongs in
  `tests/integration/` only.

## Conventions specific to this project

- Every function that calls a warehouse client library must be wrapped with
  `@sanitize_exceptions(engine_name)` (see `src/cost_guard_mcp/errors.py`). This is a
  security control, not a style preference — `snowflake-connector-python` has a confirmed
  history (issue #1323) of embedding credentials in raw exception text.
- Every `CostEstimate` must carry a real `accuracy_tier` (`PRECISE`, `UPPER_BOUND`, or
  `HEURISTIC`) — this is enforced structurally by the Pydantic model, not by convention,
  and it is the entire point of this project. Don't add a code path that could produce a
  `CostEstimate` without one.
- `run_query_bounded`'s cap checks are fail-closed: if a cap is requested
  (`max_estimated_cost_usd`/`max_bytes_billed`) but the corresponding estimate field is
  `None`, refuse. Never let a requested cap silently go unenforced.
- Any identifier that gets string-interpolated into a query-language context (a Snowflake
  warehouse name, a GCP project ID used in an API filter string) must be validated against
  an allowlist regex first. This project has shipped two real vulnerabilities from skipping
  this — see `DECISIONS.md`.
- Snowflake's role has no default anywhere in this codebase, and must never be
  `ACCOUNTADMIN`. If you're tempted to add a default role for convenience, don't.

## Git workflow

- Feature branches off `main`, named `feat/...`, `fix/...`, or `docs/...`.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`, `test:`).
- PR → CI green → squash-merge. No direct commits to `main`.
