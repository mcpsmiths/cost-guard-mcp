# Changelog

All notable changes to this project, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/). Full detail for each release lives in
[`changelog/`](changelog/) — this file is the index.

## [Unreleased]

### Security

- **Breaking: read statements only by default.** `estimate_query_cost` and `run_query_bounded`
  refuse any statement whose first keyword is not `SELECT`, `WITH` (followed by `SELECT`/`VALUES`;
  CTEs nested more than 16 levels deep are refused too, so the walk stays bounded on
  model-controlled text), `VALUES`, `SHOW`, `DESCRIBE`, `DESC` or `EXPLAIN`, before any estimate or
  execution (`DECISIONS.md` #13; #93). Opt-out: `COST_GUARD_ALLOW_WRITE_STATEMENTS=1` (exactly). With
  `COST_GUARD_ALLOW_MULTI_STATEMENT_SQL=1` every statement must be a read, so BigQuery scripts now
  need both opt-outs. `run_query_bounded` results carry the constant
  `rows_provenance: warehouse_data_untrusted` (additive `outputSchema` change, the intended
  `tools/list` diff) and both tool descriptions changed. The authorisation boundary remains the
  deployed credential's grants (read-only recommended; SECURITY.md, README).
- **Read-only tools no longer wake a warehouse by default** (`check_credentials`,
  `estimate_query_cost`; `DECISIONS.md` #20; #91, #92). Databricks: the configured SQL
  warehouse's state is read through the Warehouses API first; unless it is `RUNNING` - also
  when the state cannot be read - `check_credentials` returns `ok: false` and
  `estimate_query_cost` returns no byte or dollar figure, with no session opened. Snowflake:
  `SELECT CURRENT_REGION()`, the `QUERY_HISTORY` read and `check_credentials`' session-context
  `SELECT` run only when the pinned warehouse's `SHOW WAREHOUSES` state is `STARTED` (skipped
  when no warehouse is pinned or the state cannot be read; the `QUERY_HISTORY` read is also
  skipped whenever `estimate_query_cost` gets no `warehouse` argument); the estimate then
  prices at Gen1 rates at the observed size with the byte-tier heuristic and a caveat, and
  `check_credentials` returns `ok: true` with a detail built from the `SHOW WAREHOUSES` row.
  `USE WAREHOUSE`, `EXPLAIN USING JSON`, `SHOW WAREHOUSES` and `SELECT CURRENT_WAREHOUSE()`
  still run. `run_query_bounded` is unchanged (its estimate and execution may wake the
  warehouse). Opt-ins to waking the warehouse: `COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true`,
  `COST_GUARD_SNOWFLAKE_ALLOW_WAREHOUSE_RESUME=true` (exactly those values). Behaviour change for
  Databricks principals without `CAN_VIEW`/`CAN_USE` on the warehouse: the read-only tools now
  refuse instead of pricing at the caller-asserted size. Tool descriptions changed
  (description-only `tools/list` diff). The owner's per-statement attribution test is the
  recorded follow-up in `DECISIONS.md` #20.

### Added

- `COST_GUARD_ALLOW_WRITE_STATEMENTS`, documented in `server.json`, `.mcp.json.example` and the README
  (Setup, Known limitations, the Codex env block).
- `COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START` and `COST_GUARD_SNOWFLAKE_ALLOW_WAREHOUSE_RESUME`,
  documented in `server.json`, `.mcp.json.example` and the README (Setup, Known limitations).

### Changed

- Release process: the MCP Registry listing `io.github.mcpsmiths/cost-guard-mcp` is published by
  `release.yml` from the tagged commit, after PyPI has the version, through the new `mcp-registry`
  environment and GitHub OIDC with a pinned, SHA256-verified `mcp-publisher` (reusable workflow
  `mcp-registry-publish.yml`; its `workflow_dispatch` dry run proves the registry grant without
  publishing, and its re-publish path replaces the personal-token procedure). `DECISIONS.md` #12
  is decided; the hosted release controls - the `v*` tag ruleset, the environment deployment
  policies and the PyPI publisher binding - are recorded under #10 (#98, #99). No runtime change.

### Fixed

- Single-statement scanner: a line comment followed by a quote, backtick, `$$` or `/*` right
  after U+000B, U+000C, U+001C, U+001D, U+001E, U+0085, U+2028 or U+2029 made the scanner end
  the comment early, open a construct the engine never sees and swallow the engine's top-level
  `;`, admitting two-statement text. Line comments now end where each dialect's lexer does (CR or
  LF on Databricks; LF on BigQuery and Snowflake, where a bare CR inside a comment is refused as
  ambiguous), and any of those eight code points inside a line comment, or a backslash before its
  terminator, is refused (`DECISIONS.md` #15; #104).
- BigQuery `run_query_bounded`: the result-polling RPCs inside `query_job.result()` inherited the
  library's 600 s default retry, so a Google-side outage that began after job submission pinned
  the worker thread and one in-flight slot for ~10 min instead of surfacing within the documented
  bound; they now carry the same 30 s retry bound as every other BigQuery call, and exhausted
  polling retries issue the same bounded best-effort job cancel the 120 s watchdog does
  (`DECISIONS.md` #14; #103).
- `run_query_bounded`: a result cell nested roughly 250 or more levels deep (BigQuery JSON,
  Snowflake OBJECT/ARRAY) tripped the MCP SDK's serialiser after the billed execution, so the
  host received a protocol error or `Error executing tool run_query_bounded` and the result was
  lost; nested values are now capped at 64 levels with the placeholder
  `<nested value omitted: deeper than 64 levels>` and a `hint` naming the affected cell count -
  `hint` can therefore be set on an `ok` result; `status` remains the refusal discriminator
  (`DECISIONS.md` #17; #105).

## [0.4.0] - 2026-10-14

The 2026-10 security-audit remediation (PR #86). **Breaking:** one SQL statement per call
(opt-out `COST_GUARD_ALLOW_MULTI_STATEMENT_SQL=1`), JSON-safe result rows, Snowflake sessions
pinned to `SNOWFLAKE_ROLE` only (opt-out `SNOWFLAKE_SECONDARY_ROLES=ALL`; admin roles
rejected), and Snowflake/Databricks estimates priced at the observed warehouse (no dollar
figure, so a dollar cap refuses, on unmodelled warehouses and statistics-less Databricks
plans). Plus provider-side deadlines on every engine, bounded BigQuery/Databricks retries, an
in-flight cap, release/supply-chain gates and the CodeQL token scoping (#79). Full detail:
[`changelog/v0.4.0.md`](changelog/v0.4.0.md).

## [0.3.3] - 2026-10-10

Fixed: Snowflake private-key auth failed with "Password was given but private key is not
encrypted" when `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` was set to an empty string (what CI does
for an unset secret); the base image carried four HIGH Debian CVEs; urllib3/pyjwt advisories.
Changed: tool declarations restructured so MCP directory readers find them (no behaviour
change); dependency refresh incl. `mcp` 2.3.0. Full detail: [`changelog/v0.3.3.md`](changelog/v0.3.3.md).

## [0.3.2] - 2026-09-22

Fixed: PyPI package metadata had no `classifiers`, so shields.io's Python-version badge
showed "missing" instead of a real version. Full detail: [`changelog/v0.3.2.md`](changelog/v0.3.2.md).

## [0.3.1] - 2026-09-22

Fixed: PyPI package metadata had no `project.urls`, so the PyPI package page showed no
link back to GitHub (flagged by an MCP marketplace listing's automated scanner as a
"package-verification" finding). Full detail: [`changelog/v0.3.1.md`](changelog/v0.3.1.md).

## [0.3.0] - 2026-09-18

A CRITICAL Snowflake connector CVE fix, a Gen2 warehouse pricing-correctness fix, a working
MCP cancellation fix, opt-in structured logging/OpenTelemetry, and signed release artifacts.
Full detail: [`changelog/v0.3.0.md`](changelog/v0.3.0.md).

## [0.2.0] - 2026-09-17

Databricks engine, byte-scaled and warehouse-history cost calibration, a critical
secret-redaction fix, and everything else committed since 0.1.1 that PyPI never actually
received. Full detail: [`changelog/v0.2.0.md`](changelog/v0.2.0.md).

## [0.1.1] - 2026-09-13

Harness/governance docs, CI security tooling, and PyPI Trusted Publishing. Full detail:
[`changelog/v0.1.1.md`](changelog/v0.1.1.md).

## [0.1.0] - 2026-09-12

Initial release: BigQuery (`PRECISE`) + Snowflake (`UPPER_BOUND`) pre-flight cost
guardrails. Full detail: [`changelog/v0.1.0.md`](changelog/v0.1.0.md).

[Unreleased]: https://github.com/mcpsmiths/cost-guard-mcp/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.4.0
[0.3.3]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.3
[0.3.2]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.2
[0.3.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.0
