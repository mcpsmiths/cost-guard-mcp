# Changelog

All notable changes to this project, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/). Full detail for each release lives in
[`changelog/`](changelog/) — this file is the index.

## [Unreleased]

### Security

- Dependency relock: `uv.lock` moves `certifi` 2026.7.22 -> 2026.10.23 (PYSEC-2026-4320),
  `filelock` 3.32.6 -> 4.2.4 (thirteen CVE-2026-1129xx advisories) and `oauthlib` 3.3.1 -> 4.0.0
  (PYSEC-2026-4114, PYSEC-2026-4113, CVE-2026-49272), together with the two connectors that gate
  them: `snowflake-connector-python` 4.8.0 -> 4.8.1 (4.8.0 capped `filelock<4`; still below the
  `<5` cap of `DECISIONS.md` #23) and `databricks-sql-connector` 4.6.0 -> 4.8.0
  (4.6.0 capped `oauthlib<4.0.0`). `pip-audit` over the exported lock now reports nothing, so
  both `--ignore-vuln` flags and their re-check date are gone from `audit.yml`, and a test fails
  if an ignore comes back unreviewed. No `pyproject.toml` range changed.

## [0.6.0] - 2026-10-19

The follow-up release to 0.5.0 (#110, #112, #114, #120). **Behaviour changes:**
`run_query_bounded` defaults to 1000 rows and 8 MiB (pass `max_rows`; new `executed` field and
`result_size_exceeded` reason), and a BigQuery dollar cap refuses when the dry run has no usable
byte figure (use `max_bytes_billed`). Plus pinned third-party loggers with redaction of every
stderr record, a real `serverInfo.version`, a hash-pinned release build, a larger registry publish
retry budget, and `snowflake-connector-python<5` while 5.x is a public preview. Full detail:
[`changelog/v0.6.0.md`](changelog/v0.6.0.md).

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

[Unreleased]: https://github.com/mcpsmiths/cost-guard-mcp/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.6.0
[0.5.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.4.0
[0.3.3]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.3
[0.3.2]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.2
[0.3.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.0
