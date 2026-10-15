# AGENTS.md

Instructions for AI coding agents working in this repository.

## Project overview

`cost-guard-mcp` is a Python MCP server: four tools (`check_credentials`,
`describe_engine_capabilities`, `estimate_query_cost`, `run_query_bounded`) that give an AI
agent a pre-flight query-cost and result-size estimate for BigQuery and Snowflake before a
query actually runs (`tests/unit/test_agents_md.py` holds this sentence and the registered
tool set in lockstep). Local stdio transport only, built on the official `mcp` SDK v2. See
`ARCHITECTURE.md` for the component/data-flow map and `DECISIONS.md` for why the design
looks the way it does.

## Setup

```bash
uv sync --all-groups   # the otel dependency-group is needed for mypy and tests/unit/test_server.py
```

## Build, lint, test

```bash
uv run ruff check src tests        # lint (includes flake8-bandit security rules)
uv run mypy src                    # type check
uv run pytest tests/unit -v --cov=cost_guard_mcp --cov-report=term-missing --cov-fail-under=80
```

All three must pass before committing. CI (`.github/workflows/tests.yml`) runs the same
three commands on every push/PR — there is no separate local-vs-CI command set to keep in
sync. A `.pre-commit-config.yaml` runs ruff automatically on commit (install with
`uv run pre-commit install`) — deliberately just ruff, not mypy or pytest, to keep
pre-commit fast; the full check set still runs in CI regardless.

Dependency vulnerability scanning (`.github/workflows/audit.yml`), CodeQL, and OpenSSF
Scorecard also run in CI but are not required checks — see `SECURITY.md`.

Live-account integration tests (`tests/integration/`) require real BigQuery/Snowflake
credentials and are excluded from the default `pytest` run (`testpaths = ["tests/unit"]`
in `pyproject.toml`). Run them explicitly only when you have credentials configured:

```bash
uv run pytest tests/integration/test_bigquery_live.py -v
uv run pytest tests/integration/test_snowflake_live.py -v
```

These also run in CI (`.github/workflows/integration.yml`) — weekly on a schedule, or
on demand via `gh workflow run integration.yml` — against real accounts, using secrets
configured in the `live-integration` GitHub Environment
(`BIGQUERY_SA_KEY_JSON`/`BIGQUERY_PROJECT`, `SNOWFLAKE_ACCOUNT`/`USER`/`ROLE`/
`PRIVATE_KEY`). No Databricks job exists there yet — only BigQuery and Snowflake. Not a
required check; failures here don't block PRs, but a real one shouldn't be ignored. See
`DECISIONS.md` for why real credentials live in a public repo's CI secrets and what
scopes them down.

### Releasing

A release is a `v*` tag on reviewed `main` history (`DECISIONS.md` #10). In order:

1. Release PR: bump `pyproject.toml`, both `server.json` version fields and `uv.lock`; move
   the `CHANGELOG.md` `[Unreleased]` block into `changelog/vX.Y.Z.md` and add the index entry;
   `tests/unit/test_release_metadata.py` must be green (it pins the version triple, the
   manifests and both release workflows).
2. Live gates (owner, real credentials): `gh workflow run integration.yml --ref main` against
   the merge commit, both jobs green, before tagging (`live-integration` admits `main` only).
3. Tag the merge commit (`git tag -a vX.Y.Z -m vX.Y.Z <sha> && git push origin vX.Y.Z`). Never
   push a test `v*` tag: any such push starts the release. `release.yml` then runs `build`
   (ancestry + version gates, then the hash-pinned backend install from
   `.github/requirements-build.txt` and `uv build --no-build-isolation`; `DECISIONS.md` #11) ->
   `publish` (PyPI, environment `pypi`) -> `github-release` and `registry-publish` (MCP Registry
   listing, environment `mcp-registry`; `DECISIONS.md` #12).
4. If `registry-publish` fails, PyPI and the GitHub Release are untouched; fix the cause, then
   re-publish the same tag with
   `gh workflow run mcp-registry-publish.yml --ref main -f tag=vX.Y.Z -f dry_run=false`. The
   same command with `dry_run=true` (the default) stops after the registry login and
   `validate` without publishing - run it once with the previous tag after any change to the
   workflow or the `mcp-registry` environment, before the next release tag.

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
