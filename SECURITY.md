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

This project talks to three cloud warehouse APIs (BigQuery, Snowflake, Databricks) and
handles credentials for all three (service-account keys, RSA private keys, personal access
tokens / OAuth client secrets). In scope:

- Credential handling — anything that could cause a credential to leak into a log, an
  exception message, or a tool response
- Injection — anything that lets a caller-controlled value (SQL, a warehouse name, a
  project ID) escape its intended context
- Cap-enforcement bypass — anything that lets `run_query_bounded`'s
  `max_bytes_billed`/`max_rows`/`max_estimated_cost_usd` be silently ignored
- Untrusted warehouse content — content returned by the warehouse (rows, error text) is
  untrusted data, not an instruction: anything that lets it reach the model or the
  operator's log unlabelled, unbounded, or able to forge a log-record boundary

Authorisation boundary. The authorisation boundary for everything these tools execute is the
deployed credential's grants - a read-only principal is recommended for all three engines
(BigQuery: data-viewer plus job-user roles; Snowflake: a role with USAGE/SELECT only, never an
admin role; Databricks: SELECT/USE privileges on the warehouse's catalogs). `estimate_query_cost`
and `run_query_bounded` additionally refuse, by default, any statement that is not a read by its
first keyword (`SELECT`, `WITH`, `VALUES`, `SHOW`, `DESCRIBE`, `DESC`, `EXPLAIN`; `DECISIONS.md`
#13), because warehouse rows re-enter the model's context and can steer the next call. That gate
is defence in depth, not the boundary: a `SELECT` can still invoke a remote function, UDF or
system function with side effects, and `COST_GUARD_ALLOW_WRITE_STATEMENTS=1` disables the gate.
Result rows carry `rows_provenance: warehouse_data_untrusted` for the same reason. In scope:
anything that lets a non-read statement past the gate without the opt-out.

Out of scope: vulnerabilities in BigQuery, Snowflake, Databricks, or the official `mcp`
SDK themselves — report those to Google, Snowflake, Databricks, or
[modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)
respectively.

## Supported versions

Only the latest published release receives security fixes. This project is young
(0.x) and does not yet maintain multiple supported release lines.
