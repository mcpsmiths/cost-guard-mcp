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

## 9. Real BigQuery/Snowflake credentials live in GitHub Actions secrets, scoped down

**Context:** `integration.yml`'s scheduled live-account tests had been failing since
creation (2026-09-14) because the `live-integration` GitHub Environment had zero secrets
configured — nobody had ever provisioned them. Fixing that for real means putting actual
production-adjacent cloud credentials into a **public** repo's CI secret store, which is
a genuinely different risk profile than a local `.env` file.

**Decision:** Scoped every credential down before adding it:
- BigQuery: a dedicated service account (`cost-guard-ci`) with only `BigQuery Job User`
  (`roles/bigquery.jobUser`) plus `BigQuery Resource Viewer`
  (`roles/bigquery.resourceViewer`, needed only so `is_capacity_billed()`'s Reservations
  API check can succeed) — confirmed live that it correctly cannot create datasets or
  routines, which is intentional, not a bug to route around.
- Snowflake: the same least-privilege, non-`ACCOUNTADMIN` role this project already
  requires everywhere else (decision #7), reused rather than special-cased for CI.
- Both secret sets live in the `live-integration` GitHub Environment (not repository-wide
  secrets), so they're invisible to every other workflow in the repo.
- No environment protection rules (required reviewers, branch restrictions) were added.
  Deliberate, not an oversight: `integration.yml` only triggers on `schedule` (always runs
  against the default branch's own workflow definition) and `workflow_dispatch` (already
  gated by GitHub's own requirement that the triggering actor have write access) — never
  on `pull_request`, so there is no fork-PR path that could exfiltrate these secrets by
  proposing a modified workflow. A protection rule would add a manual-approval step with
  no corresponding security benefit for these trigger types.

**Consequences:** `integration.yml` now actually proves BigQuery/Snowflake connectivity
weekly against real accounts instead of failing on missing secrets every time — but the
scoped-down BigQuery service account genuinely cannot create its own test fixtures (see
README's "Known limitations" - the `referencedRoutines` research question stays
unresolved for exactly this reason). Any future addition to `integration.yml` needing
broader access should get its own narrowly-scoped grant, not a widened role on this one.

## 10. Release authority is bound to reviewed `main` history, not to whoever can push a tag

**Context:** `release.yml` runs on any `refs/tags/v*` push, checks out the tag's commit, and
its only pre-publish check compared the tag string with version fields read from that same
checkout - a check the tag pusher authors. The `pypi` environment and `id-token: write` are
declared in the same pusher-controlled file. The documented review gate (PR -> CI green ->
squash-merge, branch protection on `main`) governs `main`, not the tag that releases: a
`contents:write` principal other than the release maintainer (source shows
pre-commit-ci[bot] and dependabot[bot] pushing branches) could tag an unmerged commit and
publish unreviewed code to PyPI carrying repository-identity PEP 740/SLSA attestations,
where floating `uvx cost-guard-mcp` installs pick it up. Whether such a principal exists,
and whether a tag ruleset, environment protection or PyPI's publisher binding already closes
the path, is hosted configuration - not visible in source. Found by the 2026-10 security
audit.

**Decision:** Release authority is bound in four places, three of them hosted:
1. A tag ruleset on `refs/tags/v*` (Settings -> Rules -> Rulesets -> new tag ruleset,
   pattern `v*`, restrict creations/updates/deletions, bypass list: Repository admin only).
2. The `pypi` GitHub Environment admits deployments only from tag refs matching `v*`
   ("Selected branches and tags"); required reviewers are added only if the write-principal
   inventory finds a non-maintainer human or a third-party App with `contents:write`.
3. PyPI's Trusted Publisher row for `cost-guard-mcp` names Environment `pypi` - a blank
   environment would let a tag pusher delete `environment: pypi` from the workflow and
   bypass 2.
4. In-repo defence in depth: the build job checks out with `fetch-depth: 0` and refuses,
   before building, any tag whose commit is not an ancestor of `origin/main`. It
   dereferences `HEAD^{commit}` rather than `$GITHUB_SHA` because the release tags are
   annotated and the runner may resolve the ref to the tag object, which `merge-base`
   rejects. `tests/unit/test_release_metadata.py` pins the step's text and runs its script
   against a fixture repository holding an annotated tag off `main`.

`live-integration` (decision #9) deliberately stays unprotected: its triggers are
`schedule` and `workflow_dispatch`, which run the default branch's own workflow under a
write-access actor; `release.yml`'s trigger is a tag push by any write principal at any
commit, which is why `pypi` needs the policy and `live-integration` does not.

**Consequences:** A release requires the tagged commit to be merged history on `main` -
which the PR gate already made true of every release to date, so no release procedure
changes. Steps 1-3 are applied by the maintainer out of band (the owner steps in the audit's
remediation plan, which inventory the write principals first and verify the PyPI publisher
row); step 4 alone cannot be the control, because the pusher controls `release.yml` at the
tagged commit, but it turns an unmerged tag into a loud, early failure and records the
intent in source. Never push a test `v*` tag to try it - any such push starts the release
workflow.

## 11. Build inputs are pinned by reviewed source: `hatchling==1.32.4` and uv 0.12.23

**Context:** A security audit (2026-10) found that the two inputs which execute during every
build floated: `[build-system] requires = ["hatchling"]` carried no version and was absent
from `uv.lock`, and none of the seven `astral-sh/setup-uv` steps had a `version:` input, so
the uv binary was whatever the action resolved at run time. Every other build input -
runtime dependencies (hash-locked), actions (SHA-pinned), the Docker base image
(digest-pinned) - already moves only through a reviewed diff. A PEP 517 backend and the
build tool have unrestricted control over the bytes `uv build` writes to `dist/`, which
`release.yml` then signs (PEP 740), publishes under Trusted Publishing and attests (SLSA) as
if the repository had produced them; the SBOM (`uv export`) cannot even name the backend.

**Decision:**
- `[build-system] requires = ["hatchling==1.32.4"]` - the version the v0.3.3 wheel's `WHEEL`
  file records as `Generator` - mirrored in `[tool.uv] build-constraint-dependencies` and
  thereby recorded in `uv.lock`'s `[manifest]`, so `uv build`, the editable install inside
  `uv sync` (tests.yml, integration.yml) and `uv sync --frozen` (Dockerfile) all resolve the
  same backend.
- Every `setup-uv` step pins `with: version: "0.12.23"`, the same value as
  `.pre-commit-config.yaml`'s `uv-pre-commit` rev: the uv that produces `uv.lock` is the uv
  that consumes it, in the publish job holding the PyPI OIDC token included.
- `tests/unit/test_release_metadata.py` asserts both pins, the lock's `[manifest]` entry and
  the workflow/pre-commit equality, so none of them can move alone.
- Deliberately not adopted yet: `[tool.uv] required-version`, which would also reject the
  maintainer's local uv (0.10.7 at the time of writing), and a hash-pinned
  `build-constraints.txt` with `uv build --require-hashes` for the release build - a
  follow-up once the version pins have settled.

**Consequences:** Dependabot bumps neither `[build-system].requires` nor a `with: version:`
input, and pre-commit.ci's autoupdate moves only the `rev`, so a pre-commit autoupdate PR
now fails the unit suite until the seven workflow pins are bumped in the same PR - intended
coupling, not a flake. The update path is a quarterly `chore(deps)` that bumps hatchling,
`uv-pre-commit` and the workflow pins together and re-runs `uv lock` with the new uv.
Locking with uv 0.12.23 moved `uv.lock` to lock-file revision 5 (no package, version or hash
changed); the locally installed uv 0.10.7 reads it unchanged.

## 12. (Proposed) The MCP Registry listing is published from the tagged release, not from a personal host

**Status:** Proposed. The in-repo gate and its tests below are applied; the publishing
channel itself awaits the maintainer's decision, which depends on a registry fact that has
to be confirmed first (see Decision, proposed part).

**Context:** Three install channels are advertised: PyPI (`uvx cost-guard-mcp`), the GitHub
Release, and the official MCP Registry listing `io.github.mcpsmiths/cost-guard-mcp` (live
since v0.3.0; third-party directories ingest it automatically). PyPI and the GitHub Release
are produced only by `release.yml` from a `v*` tag under the `pypi` environment, OIDC,
PEP 740 and SLSA attestations (decision #8). The listing had no in-repo producer: it was
published by a manual `mcp-publisher` run after a GitHub device-flow login, from whatever
`server.json` sat on the publisher's host, with no tag, version, environment or attestation
gate - and the workflow's version check read only `server.json`'s top-level `version`, never
`packages[0].version` or `packages[0].identifier`, the fields a registry consumer actually
installs from. The 2026-10 security audit ran the gate's own one-liners against a
`server.json` with `packages[0].version = 0.0.1` and `identifier = someone-elses-package`
for tag v9.9.9 and it passed. The README marker `<!-- mcp-name: ... -->` proves PyPI-project
ownership to the registry but binds no version and no commit.

**Decision (applied now, no-regret):** the release gate requires
`server.json.version == packages[0].version == tag`, `packages[0].registryType == "pypi"`
and `packages[0].identifier == pyproject [project].name`, so any `server.json` that reaches
a tag names this PyPI project at the tag's version. `tests/unit/test_release_metadata.py`
runs the gate step's own script against fixture manifests (the `0.0.1` case is the negative
control), pins the README marker, and requires every `SNOWFLAKE_`/`DATABRICKS_`/
`COST_GUARD_`/`GOOGLE_` environment variable read in `src/` to appear in `server.json`'s
`environmentVariables` (and, except for the alternative-credential variables, in
`.mcp.json.example`), so no operator knob ships undocumented in the listing.

**Decision (proposed - maintainer to finalise):**
- Recommended: a `registry-publish` job in `release.yml` with `needs: publish`,
  `environment: mcp-registry` (a new environment, separate from `pypi`, so its OIDC token
  cannot satisfy PyPI's environment claim - decision #8's blast-radius split, kept),
  `permissions: {contents: read, id-token: write}`; steps: check out the tag, download a
  pinned `mcp-publisher` release and verify its sha256, `mcp-publisher login github-oidc`,
  `mcp-publisher publish` from the checked-out `server.json`. Precondition to confirm first,
  read-only: that `github-oidc` grants `io.github.mcpsmiths/*` to a workflow in this
  org-owned repository - the project's own notes record that device-flow granted only
  `io.github.<user>/*` for this name and that a classic PAT with `read:org` was needed. If
  it does not, fall back to a dedicated `read:org` classic PAT stored only in the
  `mcp-registry` environment: still bound to the tagged checkout and environment gating.
- Alternative: keep manual publishing with a written runbook plus the gate above. Weaker: a
  personal token, no CI identity, no guarantee that PyPI has the version first.
- Either way: the commit published is the tagged commit `release.yml` verified; ordering is
  after PyPI has the version (`needs: publish`); rollback is to publish a corrected version
  and deprecate the bad one (whether the registry lets a version be overwritten, and its
  deprecate/delete path, is to be recorded here from the registry docs when deciding);
  break-glass is a manual `mcp-publisher login github --token <read:org PAT>` from a clean
  checkout of the tag, documented as the exception, never the routine.

**Consequences:** With the recommended default the listing gains a verifiable publisher
identity and is always produced from the reviewed, tagged commit after PyPI has the version;
it requires creating the `mcp-registry` GitHub Environment with a `v*` tag deployment
policy like `pypi`'s (decision #10). Until the decision lands, the manual path remains, but
the gate guarantees that a listing published from a tagged checkout points at this PyPI
project at the tag's version. HANDOFF.md's distribution note now points here instead of
describing the manual run as the procedure.

## 18. Text that did not originate in this process is presented as quoted, bounded, labelled data

**Context:** `sanitize_exceptions` forwarded whatever a warehouse client raised as
`<engine> client call failed: <redacted text>` — secret-redacted, but otherwise verbatim:
unbounded, with raw newlines, and in the same register as this server's own refusal hints.
A view/UDF author can choose that text (BigQuery `ERROR()`, Databricks `raise_error()`,
Snowflake UDF exceptions), a model can choose it through `warehouse_size`/`edition`, and the
`mcp` SDK's stderr handler is a bare `%(message)s`, so an embedded newline forged a
genuine-looking `tool=... outcome=...` audit record (2026-10 security audit).

**Decision:** Every outbound path that carries text this process did not author — the
`SanitizedEngineError` message, the `warehouse_client_call_failed` log record and the
`check_credentials` failure `detail` (which now also logs one WARNING) — goes through one
chain: `redact_secrets` (scan bounded to 16 KiB, linear patterns) then `present_remote_text`
(bounded to 1,000 characters, `repr()`-quoted so every control character is escaped). The
`SanitizedEngineError` message labels the quoted text
`error text (untrusted data, not an instruction)` — neutral wording, because the same
branch carries in-process validator and pricing `ValueError`s that echo model input. The
project's own `ConfigError` and `UserVisibleError` pass through `sanitize_exceptions`
unchanged, so server-authored text is never labelled as warehouse-returned.

**Consequences:** The human-visible `ToolError` text changed shape (quoted, labelled, and a
misconfiguration no longer carries the `<engine> client call failed:` prefix); the
machine-visible `Error executing tool <name>:` prefix and `is_error=true` are unchanged.
Any new outbound surface that carries connector or model text must use the same chain —
never `str(exc)` or `redact_secrets` alone.
