# Decisions

Architecture Decision Records for this project, condensed [MADR](https://adr.github.io/madr/)-style.
Kept as one file for now — the real-world convention for larger projects is a numbered
`docs/adr/` directory (one file per decision), but that's disproportionate for a handful
of records on a single-maintainer v1 project. Split into `docs/adr/` if this file grows
past ~15-20 entries.

Each entry: Context → Decision → Consequences.

## 1. Python + the official `mcp` SDK, not `fastmcp` or TypeScript

**Context:** The three warehouse client libraries this project needs
(`google-cloud-bigquery`, `google-cloud-bigquery-reservation`, `snowflake-connector-python`)
are all Python-first, with 10-25x more adoption than their Node/TS equivalents. The
official `mcp` SDK auto-generates output schemas from Pydantic return types, which maps
directly onto the accuracy-tier contract this project depends on.

**Decision:** Python 3.12+, the official `modelcontextprotocol/python-sdk`, not the
third-party `fastmcp` package a locally-installed skill recommended.

**Consequences:** All three official warehouse clients are synchronous, requiring
`asyncio.to_thread`-style wrapping if this ever becomes a high-concurrency remote server
instead of a local stdio process. Not a problem today; revisit if the transport model
changes.

## 2. Local stdio only for v1, no remote HTTP transport

**Context:** Credentials are per-user, per-engine, and sensitive (GCP service-account
keys, Snowflake private keys). A remote multi-tenant server would need a credential
broker or per-tenant isolation neither engine's client library was designed around.

**Decision:** Stdio transport only. Each user runs their own process with their own env
vars.

**Consequences:** Zero infrastructure to operate, but no shared/hosted deployment option.
Revisit only if there's real demand for a multi-tenant hosted version.

## 3. A machine-readable accuracy tier on every estimate

**Context:** BigQuery's `dryRun` API can be exact; Snowflake's `EXPLAIN` fundamentally
cannot be (Snowflake bills warehouse-time, not bytes, and `EXPLAIN`'s own figures are
documented as upper bounds). No existing warehouse MCP disclosed this distinction in a
way an agent could act on programmatically.

**Decision:** Every `CostEstimate` carries a required `accuracy_tier` field
(`PRECISE`/`UPPER_BOUND`/`HEURISTIC`), enforced by the Pydantic schema, not by convention.

**Consequences:** This is the project's entire differentiation. Any new engine
integration (Databricks, Redshift, etc.) must honestly classify its own tier rather than
defaulting to `PRECISE` for optimism.

## 4. `run_query_bounded` refusals are normal results, not `isError: true`

**Context:** The MCP SDK has no idiomatic way to combine `isError: true` with a populated
`structured_content` in the high-level decorator API — the SDK's own docs state a failed
call has no return value to structure.

**Decision:** A refusal (cap exceeded, cap unenforceable) is a normal, successful tool
result: `BoundedQueryResult(status="refused", reason, estimate, hint)`. `ToolError` is
reserved for genuine exceptions with nothing meaningful to structure (bad credentials,
client-side SQL parse errors).

**Consequences:** Callers must check `result.status`, not rely on the MCP error channel,
to detect a refusal. This matches the dominant real-world precedent found across other
shipped cost-guard-style MCP servers, not just this project's own preference.

## 5. Cap checks are fail-closed

**Context:** An automated security review caught a real gap: the original cap-check logic
used a flat `and`-chain that fell through to "allow" when a cap was requested but its
matching estimate field was `None` (e.g. `max_estimated_cost_usd` on a capacity-billed
BigQuery project, which has no dollar estimate at all).

**Decision:** If a cap is requested and the corresponding estimate field is `None`,
refuse — never silently execute with that cap unenforced.

**Consequences:** A caller who sets `max_estimated_cost_usd` against a project where no
dollar estimate is possible always gets refused, even if the query would have been cheap.
This is the correct trade-off: an unenforceable cap must never look enforced.

## 6. Validate any identifier before it enters a query-language context

**Context:** Two real vulnerabilities shipped from skipping this: unvalidated Snowflake
warehouse names interpolated into `USE WAREHOUSE {warehouse}` (a caller-supplied MCP tool
parameter — a real SQL-injection surface, not theoretical), and later an unvalidated GCP
project ID interpolated into a Reservation API filter string
(`assignee=projects/{project}`), flagged by a second automated security review.

**Decision:** Any identifier — warehouse name, project ID — that gets string-interpolated
into a query-language or API-filter context is validated against an identifier-format
allowlist regex first (`_validate_warehouse`, `_validate_project_id`), and construction
fails loudly (`ValueError`, sanitized to `SanitizedEngineError`) rather than proceeding.

**Consequences:** Any future engine integration that builds a query or filter string from
a caller-supplied or otherwise external value needs the same treatment before it ships,
not after a review catches it.

## 7. Snowflake role has no default, and it is never `ACCOUNTADMIN`

**Context:** Google's own reference Snowflake connector sample defaults to
`ACCOUNTADMIN` for convenience — a real anti-pattern this project deliberately does not
copy.

**Decision:** `SNOWFLAKE_ROLE` must be set explicitly; `load_snowflake_config()` raises a
clear `ConfigError` if it's missing, rather than silently defaulting to any role.

**Consequences:** Slightly more setup friction for a new user, in exchange for making it
structurally impossible to run this tool as an accidental `ACCOUNTADMIN`.

## 8. PyPI releases publish via Trusted Publishing, not a stored API token

**Context:** v0.1.0 published via a manually-run `uv publish` using a locally-stored PyPI
API token. `uv publish` has native, built-in support for PyPI Trusted Publishing
(OIDC) — no twine, no third-party action, no credential stored anywhere.

**Decision:** `.github/workflows/release.yml`, triggered by pushing a `v*` tag, builds the
package, publishes via `uv publish` under Trusted Publishing (`id-token: write` on the
publish job only, split from the build job to limit the OIDC credential's blast radius),
and creates the GitHub Release with the built artifacts plus a CycloneDX SBOM
(`uv export --format cyclonedx1.5`, an experimental uv feature as of this writing).

**Consequences:** No PyPI credential exists anywhere in this project's CI or on disk
after this migration — a real reduction in what a compromised CI run or leaked secret
could do. Requires a one-time PyPI-side setup (Trusted Publisher config naming this repo
and workflow file) before the first Trusted-Publishing release; see the comment at the
top of `release.yml`.
