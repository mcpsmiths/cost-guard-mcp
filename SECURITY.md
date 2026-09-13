# Security Policy

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

Use GitHub's private vulnerability reporting instead: go to the
[Security tab](https://github.com/mcpsmiths/cost-guard-mcp/security) →
**Report a vulnerability**. This opens a private advisory only the maintainer can see,
and is the preferred channel — most real-world vulnerability disclosure processes route
through a private channel rather than a public issue, for the obvious reason that a
public issue is a public exploit disclosure.

Please include:

- The affected file(s)/function(s) and version (git commit or release tag)
- Reproduction steps or proof-of-concept
- The potential impact (e.g. credential exposure, unauthorized query execution, cost/DoS)

## Response process

1. **Acknowledgment** within 72 hours of receipt.
2. **Assessment** — confirm the report, determine severity and affected versions.
3. **Remediation** — a fix, released as a patch version.
4. **Disclosure** — a public advisory only after a fix is released, crediting the
   reporter unless they ask not to be.

## Scope

This project talks to two cloud warehouse APIs (BigQuery, Snowflake) and handles
credentials for both (service-account keys, RSA private keys). In scope:

- Credential handling — anything that could cause a credential to leak into a log, an
  exception message, or a tool response
- Injection — anything that lets a caller-controlled value (SQL, a warehouse name, a
  project ID) escape its intended context
- Cap-enforcement bypass — anything that lets `run_query_bounded`'s
  `max_bytes_billed`/`max_rows`/`max_estimated_cost_usd` be silently ignored

Out of scope: vulnerabilities in BigQuery, Snowflake, or the official `mcp` SDK
themselves — report those to Google, Snowflake, or
[modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)
respectively.

## Supported versions

Only the latest published release receives security fixes. This project is young
(v0.1.x) and does not yet maintain multiple supported release lines.
