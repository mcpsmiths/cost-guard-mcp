# Changelog

All notable changes to this project, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/). Full detail for each release lives in
[`changelog/`](changelog/) — this file is the index.

## [Unreleased]

## [0.4.0] - 2026-10-14

The 2026-10 security-audit remediation (PR #86, 59 commits, three runtime-verification
passes). **Breaking:** one SQL statement per call (opt-out
`COST_GUARD_ALLOW_MULTI_STATEMENT_SQL=1`), JSON-safe result rows (bytes as base64, non-finite
floats as strings), Snowflake sessions pinned to `SNOWFLAKE_ROLE` only (opt-out
`SNOWFLAKE_SECONDARY_ROLES=ALL`; admin roles rejected), and Snowflake/Databricks estimates
priced at the observed warehouse, so unmodelled warehouse types and statistics-less Databricks
plans get no dollar figure and a `max_estimated_cost_usd` cap refuses there. Also:
provider-side deadlines and bounded retries on every engine, a server-side BigQuery byte cap
derived from the dollar cap, an in-flight cap (`COST_GUARD_MAX_IN_FLIGHT_QUERIES`),
labelled/bounded/quoted warehouse text, pinned build inputs, a release gate bound to `main`,
and the CodeQL token-permission scoping (#79). Full detail:
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
