# Context

A terminology glossary for contributors. This project spans two cloud data warehouses
with genuinely different billing/execution models — the same-sounding word often means
something different depending on which engine you're reading about.

## Cross-engine terms that don't mean the same thing

- **Warehouse** — BigQuery: a project-level resource, not user-provisioned (on-demand
  billing is fully managed). Snowflake: a user-provisioned, sized compute cluster
  (`XSMALL`...`X6LARGE`) that must be explicitly started/scaled and is what's actually
  billed by the hour/credit.
- **Project** vs. **Account** — BigQuery's billing/identity unit is a GCP *project*.
  Snowflake's is an *account* (itself living inside an *organization*). Not
  interchangeable in code or in this doc.
- **Slot** vs. **Credit** — BigQuery Editions bills in slot-hours (compute capacity
  units). Snowflake bills in credits (a different, warehouse-size-dependent unit; see
  `pricing/snowflake_pricing.py`'s credit-rate table). There is no fixed conversion
  between the two.
- **Dry run** (BigQuery) vs. **`EXPLAIN`** (Snowflake) — both are pre-flight estimate
  mechanisms, but with different guarantees. BigQuery's `dryRun` can be exact
  (`totalBytesProcessedAccuracy: PRECISE`). Snowflake's `EXPLAIN` is *never* exact — it
  estimates bytes/partitions that runtime optimization can reduce, which is exactly why
  this project always tags Snowflake estimates `UPPER_BOUND`, never `PRECISE`.

## Terms specific to this project (not standard across the MCP ecosystem)

- **Accuracy tier** — this project's own three-value contract
  (`PRECISE`/`UPPER_BOUND`/`HEURISTIC`) on every `CostEstimate`, describing how much to
  trust the number — not a BigQuery or Snowflake concept, and not the same as BigQuery's
  own `totalBytesProcessedAccuracy` field (which has four values and describes a
  different thing — see `engines/bigquery.py`'s mapping between the two).
- **Bounded** (as in `run_query_bounded`) — refers to the caller-supplied caps
  (`max_bytes_billed`, `max_rows`, `max_estimated_cost_usd`), checked *before* execution.
  Not related to SQL's `LIMIT` clause, though `LIMIT` is the mechanism used internally to
  enforce the row cap on the wire.
- **Fail-closed** (as used in `DECISIONS.md` and code comments) — when a cap is requested
  but the tool can't produce the matching estimate field to check it against, the query is
  refused, not run unbounded. The opposite (fail-open) would silently ignore an
  unenforceable cap.
