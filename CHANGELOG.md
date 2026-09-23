# Changelog

All notable changes to this project, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/). Full detail for each release lives in
[`changelog/`](changelog/) — this file is the index.

## [Unreleased]

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

[Unreleased]: https://github.com/mcpsmiths/cost-guard-mcp/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.2
[0.3.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.0
