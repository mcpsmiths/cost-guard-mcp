# Changelog

All notable changes to this project, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/). Full detail for each release lives in
[`changelog/`](changelog/) — this file is the index.

## [Unreleased]

### Security

- Release build: `release.yml` now installs the build backend hash-pinned - `hatchling==1.32.4`
  and its closure from `.github/requirements-build.txt` under
  `uv pip install --require-hashes --only-binary :all: --no-deps`, into a venv outside the
  checkout - and runs `uv build --no-build-isolation` with it, so the published sdist and wheel
  can only be produced by the exact wheels whose SHA-256 the reviewed file lists; a wrong or
  missing hash fails the build job before anything is built. Dependabot tracks the file (`pip`,
  `/.github`) and a lockstep test keeps its hatchling pin equal to `pyproject.toml`'s
  (`DECISIONS.md` #11, amended; #97).

### Added

- Redacting log filter and pinned third-party loggers: at server start the
  `snowflake.connector`, `databricks`, `databricks.sql`, `databricks.sdk`, `google`, `urllib3`,
  `httpx` and `httpcore` loggers are set to WARNING, and every record that reaches the stderr
  handler — the `mcp` SDK's and the client libraries' included — has its message, exception
  traceback and stack text rendered once and passed through `redact_secrets` before it is
  written (never drops a record, never raises). The project's own `tool=<name> outcome=` INFO
  records are unchanged; no new configuration (#95).
- `run_query_bounded` result size cap: 8 MiB of compact JSON over the normalised rows; larger
  results return the longest prefix of whole rows with the new `reason: result_size_exceeded`
  (additive `outputSchema` enum value); `executed` boolean on every `BoundedQueryResult`
  (additive, required property) stating whether the warehouse ran the statement; one WARNING
  when an explicit `max_rows` exceeds 100,000 (the byte cap bounds the serialised rows, not the
  process memory behind millions of narrow rows) (`DECISIONS.md` #21; #94, #101).

### Changed

- MCP Registry publish (`mcp-registry-publish.yml`): the retry budget that absorbs the
  registry's own PyPI lag is 8 attempts 30 s apart, was 5 — the first real run (v0.5.0) used
  2 of 5 waiting ~60-70 s; the job's designed worst case stays about 20 minutes
  (`DECISIONS.md` #12; #111).
- BigQuery dollar caps refuse on row-level-security-shaped and `LOWER_BOUND` dry runs: a dry run
  that reports 0 bytes on a statement referencing a table (BigQuery hides the figure for
  row-level-security-protected tables, by design) or `totalBytesProcessedAccuracy: LOWER_BOUND`
  (a floor, not a cap) now yields `estimated_cost_usd: null`, `accuracy_tier: HEURISTIC` and a
  caveat saying why, so `max_estimated_cost_usd` refuses before dispatch (`reason:
  cost_cap_exceeded`, `executed: false`) instead of admitting a $0.00 / floor figure;
  `estimated_bytes` stays as reported and `max_bytes_billed` keeps working, enforced server-side
  as `maximumBytesBilled`. `estimate_query_cost` shows the same facts; the no-dollar-estimate
  refusal hint and `describe_engine_capabilities` name the shapes and the remedy. `UNKNOWN`
  accuracy is unchanged. Any 0-byte dry run on a statement that references a table is treated as
  the row-level-security shape - including an unfiltered `COUNT(*)` answered from table
  metadata, an empty table and a fully pruned partition filter - so a dollar cap refuses those
  too (use `max_bytes_billed`); the caveat names these causes rather than asserting row-level
  security. `max_estimated_cost_usd=+inf` still counts as a requested dollar cap when no dollar
  figure exists and refuses, as it already did for capacity-billed projects (`DECISIONS.md` #22;
  #96).
- `run_query_bounded`: `max_rows` now defaults to 1000 when omitted (behaviour change for
  callers relying on unbounded results - pass an explicit `max_rows`, up to 2^63-2); a single
  `SELECT`/`WITH`/`VALUES` statement is wrapped in `LIMIT max_rows + 1` as before and must be
  valid as a derived table (BigQuery rejects duplicate output column names there - alias them);
  other admitted statements and opt-out scripts run unwrapped and are capped by the fetch;
  Snowflake and Databricks fetch results in pages of 500 and stop early, BigQuery passes
  `max_results`/`page_size`; the Snowflake post-poll fetch (and Databricks' post-join fetch) now
  honour the remaining 120 s budget, checked before each page that follows a full page, so a
  complete result that fits in one page is never discarded for a deadline its only page overran
  (`DECISIONS.md` #21; #94).

### Fixed

- `serverInfo.version` in the `initialize` handshake was `""` on every release so far (the
  server was constructed without a version); it is now the installed package version from
  `importlib.metadata`, or `0.0.0+unknown` when run from an uninstalled source tree. `AGENTS.md`
  now lists all four tools, pinned to the registrations by a unit test (#111).
- BigQuery billing-model check (`is_capacity_billed`) searched reservation assignments in `US`
  only, so a project whose reservation lives in another location was priced on-demand. It now
  searches the dry-run job's own location (`QueryJob.location`), falls back to `US` only when
  the job reports none (with a caveat), allowlists the service-returned location before it
  enters the Reservations parent path, and records the location, its source and the outcome in
  one INFO record per dry run and in the capacity-billed / check-failed caveats (#96).
- `run_query_bounded` tool description claimed every `refused` result "did NOT run";
  `row_cap_exceeded` (and now `result_size_exceeded`) follow a billed execution and carry
  truncated rows. The description, `DECISIONS.md` #4 and README now say which refusals ran
  (check `executed`) (#101).

## [0.5.0] - 2026-10-15

The follow-up release to the 2026-10 audit remediation (#106, #107 and the registry-channel
PR). **Breaking:** read statements only by default (opt-out
`COST_GUARD_ALLOW_WRITE_STATEMENTS=1`) and read-only tools that no longer wake a warehouse
(opt-ins `COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true`,
`COST_GUARD_SNOWFLAKE_ALLOW_WAREHOUSE_RESUME=true`). Plus the line-comment scanner fix, bounded
BigQuery result polling, a 64-level cap on nested result cells, and the MCP Registry listing
published by `release.yml` from the tag. Full detail:
[`changelog/v0.5.0.md`](changelog/v0.5.0.md).

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

[Unreleased]: https://github.com/mcpsmiths/cost-guard-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.4.0
[0.3.3]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.3
[0.3.2]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.2
[0.3.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.0
