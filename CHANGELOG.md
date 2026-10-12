# Changelog

All notable changes to this project, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/). Full detail for each release lives in
[`changelog/`](changelog/) — this file is the index.

## [Unreleased]

### Security

- **Log records and `SanitizedEngineError` text can no longer be forged by control characters.**
  `sanitize_exceptions` now `repr()`-quotes the (already secret-redacted) exception text
  before logging it or raising it, so a newline, carriage return, ESC or NUL in a
  model-supplied `warehouse_size`/`edition` or in warehouse-returned error text is escaped
  as `\xNN` instead of starting a new, prefix-free stderr line that reads as a genuine
  `tool=... outcome=...` or `run_query_bounded refused ...` record. The three pricing
  `ValueError`s echo the offending argument with `!r` for the same reason.
- **`redact_secrets` is bounded and linear.** It now scans at most 16 KiB of exception text
  (the tail is replaced by a `...[truncated N chars]` marker) and its PEM/quoted-value
  patterns no longer backtrack quadratically, so a crafted `warehouse_size`/`edition` or a
  crafted warehouse error can no longer freeze the single-process server (and its
  watchdog threads) for seconds per call - ~4 s per 32 KB before, x4 per doubling. A quoted
  secret cut by the bound still redacts to end of text; the pricing errors echo at most 64
  characters of the offending argument.

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

[Unreleased]: https://github.com/mcpsmiths/cost-guard-mcp/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.2
[0.3.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/mcpsmiths/cost-guard-mcp/releases/tag/v0.1.0
