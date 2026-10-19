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

**Amended (#101, 2026-10):** "refused" no longer always means "did not run". Two refusals
are produced after a billed execution and carry a truncated prefix of its rows:
`row_cap_exceeded` and `result_size_exceeded` (#21). `BoundedQueryResult.executed` states, on
every result, whether the warehouse ran the statement; the tool description says which
reasons imply it. The MCP error channel is still never used for these.

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

Amended after the 2026-10 security audit, which found the role pin did not bound the
session's authority: `role=` only sets Snowflake's *primary* role, and each statement is
authorized against the primary role plus every *active* secondary role — which, for users
created after behavior-change bundle 2024_07, defaults to `('ALL')` of the user's grants.
So the documented boundary (the configured role) was not the enforced one. Three
consequences now follow from this decision rather than from prose alone:

- `load_snowflake_config()` rejects `ACCOUNTADMIN`, `SECURITYADMIN` and `ORGADMIN` (after
  strip/unquote/upper-case normalisation) with a static `ConfigError` — "never
  `ACCOUNTADMIN`" is a check, not a request. The loader runs on the first Snowflake tool
  call, not at process start, so the refusal is a failed `check_credentials` or a tool
  error issued before any connection is opened — the process itself starts regardless.
- Every session pins secondary roles to `NONE` in the login request itself (the
  connector's `secondary_roles` parameter, present since 4.3.0), so the pin is in force
  before `USE WAREHOUSE` or any model-supplied text, with no extra round trip.
  `SNOWFLAKE_SECONDARY_ROLES=ALL` is the explicit, documented opt-out for deployments that
  reached objects only via a secondary role (granting those objects to `SNOWFLAKE_ROLE` is
  the preferred fix). `SNOWFLAKE_USER` should hold no role other than `SNOWFLAKE_ROLE`.
- `check_credentials` reports `CURRENT_SECONDARY_ROLES()` next to `CURRENT_ROLE()`, so the
  one in-tool identity check shows the authority actually in force.

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

`live-integration` (decision #9): `schedule` runs the default branch's own copy of
`integration.yml`, but `workflow_dispatch` runs the copy at whichever branch or tag the
dispatching write-access actor selects, so before 2026-10-15 a dispatch-capable write
principal could run a modified `integration.yml` from a branch with the live secrets in
scope. The `main`-only deployment-branch policy applied 2026-10-15 (#98) closes that path:
a dispatch from any other ref is refused before the first step. #98 filed it as hardening
rather than a finding because the write-principal inventory below shows that only the
maintainer can dispatch today; the policy's value is bounded by that inventory, not nil.
`release.yml` differs in kind: its trigger is a tag push by any write principal at any
commit, which is why `pypi` needs its policy regardless of the inventory.

**Consequences:** A release requires the tagged commit to be merged history on `main` -
which the PR gate already made true of every release to date, so no release procedure
changes. Steps 1-3 are applied by the maintainer out of band (the owner steps in the audit's
remediation plan, which inventory the write principals first and verify the PyPI publisher
row); step 4 alone cannot be the control, because the pusher controls `release.yml` at the
tagged commit, but it turns an unmerged tag into a loud, early failure and records the
intent in source. Never push a test `v*` tag to try it - any such push starts the release
workflow.

**Applied (hosted), recorded 2026-10-15 (#98):** read back with `gh api` on 2026-10-15.
1. Tag ruleset `release-tags` (id 25072354), target `tag`, pattern `refs/tags/v*`,
   enforcement active, rules: creation, update, deletion and non-fast-forward. Bypass
   actor: the **Organization admin** role (`actor_type: OrganizationAdmin`), not the
   "Repository admin only" the decision wrote. GitHub offers both in an organisation-owned
   repository (`actor_type: RepositoryRole`, `actor_id: 5` is Repository admin;
   `OrganizationAdmin` is merely unavailable in personal repositories). Organization admin
   was kept deliberately as the narrower set: every organisation owner is already a
   repository admin, so it covers the same single maintainer today while excluding any
   future admin collaborator or admin team. A second organisation owner could bypass under
   either option, so adding one is the event that reopens this decision. The 0.4.0 tag
   push logged that bypass, as expected.
2. `pypi` environment: custom deployment-branch policies with the single tag pattern `v*`
   and no branch entry; no required reviewers. Write-principal inventory (`gh api`,
   2026-10-15): repository collaborators = `sairam0424` (admin) only, no teams, no pending
   invitations; the organisation has one member, who is its only owner. GitHub App
   installations on all organisation repositories with `contents: write`: `coderabbitai`
   (also `actions: read`, `checks`/`pull_requests`/`statuses`/`issues`: write) and
   `pre-commit-ci` (also `workflows: write`, `pull_requests`/`statuses`: write); `codecov`
   and `m8ven-verify` hold `contents: read`; Dependabot is GitHub-native. `main` carries
   classic branch protection with one required status check, no force pushes and no
   deletions - but no required pull-request reviews and `enforce_admins` off, so an App with
   `contents: write` and `pull_requests: write` could land a commit on `main` without a
   human (the status check only requires CI to have passed on that commit). This meets step
   2's literal trigger (third-party Apps with `contents: write`) - the deviation: reviewers
   were not added because none of these principals can reach a deployment. The
   `release-tags` bypass list names only the Organization admin role (no `Integration`
   actor), so neither App can create a `refs/tags/v*` ref, and `release.yml`'s only trigger
   is that tag push; neither App holds `actions: write`, so neither can call the
   workflow-dispatch API that `live-integration` and `mcp-registry` admit from `main`. What
   the inventory does not close is `main` itself: the "reviewed `main` history" this
   decision binds releases to is the maintainer's practice, enforced by the tag ruleset
   (only the maintainer can release) rather than by `main`'s protection, and a dispatch the
   maintainer later runs from `main` runs whatever `main` holds. Closing that - a `main`
   ruleset that requires a pull request and restricts updates to the Organization admin
   bypass, which also stops the two Apps pushing or merging to `main` - changes how every
   merge is done and is left to the maintainer, recorded as open in #98. Reviewers become
   required the day an App gains `actions: write` or a ruleset bypass, or a second human
   gets write access.
3. PyPI's Trusted Publisher row for `cost-guard-mcp` names workflow `release.yml` and
   environment `pypi`; the 0.4.0 distributions' PEP 740 provenance (served at
   `https://pypi.org/integrity/cost-guard-mcp/0.4.0/<file>/provenance`) records that
   environment in the publisher claim, which is the binding step 3 asked for.
4. `tests/unit/test_release_metadata.py` pins the in-repo gate (see above) and, since
   decision #12 landed, the identical gate in `mcp-registry-publish.yml`.

Also applied 2026-10-15: the `mcp-registry` environment (decision #12) with the tag
pattern `v*` plus `main`, and the `live-integration` `main`-only policy noted above.

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

**Amended 2026-10-16 (#97): the release build installs the backend hash-pinned.** The version
pins above bind *which* hatchling the release build asks for, not *which bytes* it accepts: an
index, or anything on the path to it, serving a different artefact under
`hatchling-1.32.4-py3-none-any.whl` was taken, and `uv build` resolved the backend's closure from
PyPI inside an isolated environment at run time. Now `.github/requirements-build.txt` lists
hatchling 1.32.4 and its closure (packaging 26.3, pathspec 1.1.1, pluggy 1.7.0, tomlkit 0.15.1,
trove-classifiers 2026.9.21.13) with the SHA-256 of each member's `py3-none-any` wheel and sdist,
generated from the single input `hatchling==1.32.4` with
`uvx uv@0.12.23 pip compile --generate-hashes --universal --python-version 3.12 -o
.github/requirements-build.txt -` (the `--universal` and linux/3.12 resolutions are identical;
hatchling has no marker-gated dependency at >=3.12; pass that command text to
`--custom-compile-command` when regenerating so the header stays stable). The `build` job of
`release.yml` creates a venv under `$RUNNER_TEMP` - outside the checkout, so hatchling's sdist, which
includes non-ignored untracked files, never picks it up - installs the file with
`uv pip install --require-hashes --only-binary :all: --no-deps`, and runs
`uv build --no-build-isolation --python <that venv>`: the backend comes from the interpreter's
installed packages, `--no-deps` keeps the installed set to exactly the reviewed file, and
`--only-binary` means no sdist, and so no arbitrary build code, is ever run. A wrong or missing
hash fails the install step before anything is built - proven locally with uv 0.12.23 against
pypi.org, never with a `v*` tag: a changed wheel hash gives "Hash mismatch for
`hatchling==1.32.4`", an unhashed entry "In `--require-hashes` mode, all requirements must have a
hash", and an empty venv fails the build with no index fallback.

`tests/unit/test_release_metadata.py` pins the file's hatchling entry to the `[build-system]` pin
and to `build-constraint-dependencies` (one value in three places), requires every logical line
to be an exact pin followed only by `--hash=sha256:` tokens (no option line can redirect the
install), pins the build job's step texts and order, and runs the install step's own script
offline against a hand-built fixture wheel with a correct, a wrong and a missing hash. Update
path: `.github/dependabot.yml` gains a weekly `pip` entry on `/.github`; Dependabot rewrites the
version and `--hash` lines, and the lockstep test fails that PR until `[build-system].requires`,
`build-constraint-dependencies` and `uv.lock` move in the same PR - the same intended coupling
as the pre-commit autoupdate above.

Still unbound, deliberately: the uv binary (`setup-uv` is SHA-pinned with `version: "0.12.23"`;
the action verifies the archive it downloads against a checksum table compiled into its own SHA-pinned source, so uv's integrity rests on that pinned action rather than on a digest in this repository), the
runner's interpreter and image, and the editable builds in `tests.yml`, `integration.yml` and the
Dockerfile, which keep resolving hatchling by version through `build-constraint-dependencies`
because nothing they build is signed or published. `uv build --build-constraints <file>
--require-hashes`, native in uv 0.12.23, was considered and left for a later revisit: the
two-step form puts the fail-closed point in a step the unit suite can run offline.
`[tool.uv] required-version` remains unadopted.

## 12. The MCP Registry listing is published from the tagged release, not from a personal host

**Status:** Decided 2026-10-15 (#99). The channel is the `registry-publish` job in
`release.yml` calling the reusable workflow `.github/workflows/mcp-registry-publish.yml`.
0.4.0 shipped with the in-repo gate below but its listing was never published (the manual
step was skipped, as for 0.3.1 and 0.3.3); the first entry this job publishes is the next
tag, so the live listing's latest entry stays 0.3.2 until then.

**Context:** Three install channels are advertised: PyPI (`uvx cost-guard-mcp`), the GitHub
Release, and the official MCP Registry listing `io.github.mcpsmiths/cost-guard-mcp` (live
since 0.1.0, 2026-09-12; third-party directories ingest it automatically). PyPI and the
GitHub Release are produced only by `release.yml` from a `v*` tag under the `pypi`
environment, OIDC, PEP 740 and SLSA attestations (decision #8). The listing had no in-repo
producer: it was published by a manual `mcp-publisher` run after a GitHub device-flow login,
from whatever `server.json` sat on the publisher's host, with no tag, version, environment
or attestation gate - and the workflow's version check read only `server.json`'s top-level
`version`, never `packages[0].version` or `packages[0].identifier`, the fields a registry
consumer actually installs from. The 2026-10 security audit ran the gate's own one-liners
against a `server.json` with `packages[0].version = 0.0.1` and `identifier =
someone-elses-package` for tag v9.9.9 and it passed. The README marker
`<!-- mcp-name: ... -->` proves PyPI-project ownership to the registry but binds no version
and no commit.

**Decision (applied now, no-regret):** the release gate requires
`server.json.version == packages[0].version == tag`, `packages[0].registryType == "pypi"`
and `packages[0].identifier == pyproject [project].name`, so any `server.json` that reaches
a tag names this PyPI project at the tag's version. `tests/unit/test_release_metadata.py`
runs the gate step's own script against fixture manifests (the `0.0.1` case is the negative
control), pins the README marker, and requires every `SNOWFLAKE_`/`DATABRICKS_`/
`COST_GUARD_`/`GOOGLE_` environment variable read in `src/` to appear in `server.json`'s
`environmentVariables` (and, except for the alternative-credential variables, in
`.mcp.json.example`), so no operator knob ships undocumented in the listing.

**Decision (2026-10-15):** `release.yml` gains `registry-publish` (`needs: publish`,
`permissions: {contents: read, id-token: write}`) which calls
`mcp-registry-publish.yml`; that workflow's one job runs under the new `mcp-registry`
environment (separate from `pypi`, so its OIDC token cannot satisfy PyPI's environment
claim - decision #8's blast-radius split, kept), checks out exactly `refs/tags/<tag>`
without persisting credentials, refuses a checkout that is not the tagged commit or whose
commit is not an ancestor of `refs/remotes/origin/main` (the same ancestry gate as decision
#10's, including the `origin/main`-named-tag case), runs the release version gate verbatim
against the tag's `pyproject.toml` and `server.json` (`tests/unit/test_release_metadata.py`
holds the two texts in lockstep), waits until PyPI serves the version with the `mcp-name`
ownership marker, installs `mcp-publisher` 1.8.1 as a pinned release whose SHA256 is recorded
in the workflow, authenticates with `mcp-publisher login github-oidc`, validates and
publishes the checked-out `server.json`, then reads the entry back from the registry API and
checks the package record. `server.json` is verified, never rewritten in the job: a version
drift is a refusal, not something a `jq` sync papers over. The publish step is idempotent
for the version the tag names: if the registry already serves that exact version (a lost
success response, or a re-publish dispatched for an already-published tag) it reports so
and the read-back check decides - it compares the package record (`registryType`,
`identifier`, both version fields) and requires status `active`, nothing more; an entry that
differs in description or repository fields is not detected, which is acceptable because
every entry is produced from a tagged `server.json` the gate verified.

Preconditions, answered read-only on 2026-10-15:
- *Does `github-oidc` grant `io.github.mcpsmiths/*` to a workflow in an org-owned
  repository?* Yes, in production, in this organisation: `mcpsmiths/tracehub-mcp`'s release
  run 38609621720 (2026-10-14) published `io.github.mcpsmiths/tracehub-mcp` 0.13.0 through
  exactly this login from its own reusable workflow. The rule is in the registry's source,
  not its docs: `internal/api/handlers/v0/auth/github_oidc.go` (`buildPermissions`, v1.8.1,
  read 2026-10-15) grants `publish` on `io.github.<repository_owner>/*` from the OIDC
  token's `repository_owner` claim. The user-auth rule that only an organisation *Owner*
  may publish under an org namespace applies to device-flow and PAT logins, which is what
  the project's earlier notes about `io.github.<user>/*` and `read:org` recorded. For this
  repository the acceptance test is a `workflow_dispatch` of `mcp-registry-publish.yml`
  with `tag: v0.4.0`, `dry_run: true`: it must reach `login github-oidc` and `validate`
  and publish nothing, before the first tag that exercises the job.
- *Live listing:* `GET /v0.1/servers?search=io.github.mcpsmiths/cost-guard-mcp` lists
  0.1.0, 0.1.1, 0.2.0, 0.3.0 and 0.3.2; `isLatest` is 0.3.2 (not 0.3.3 as the issue
  assumed), status `active`, `packages[0]` = `pypi` / `cost-guard-mcp` / `0.3.2`, transport
  stdio - a stale but correct pointer at this PyPI project.
- *Validation and overwrite:* the registry verifies PyPI ownership at publish time by the
  `mcp-name: <name>` string in the PyPI description (README.md:3) and checks that the
  version exists on PyPI, on its own network path (a fresh upload can still 404 there for
  about 30 s after PyPI's JSON API serves it; the first real run, v0.5.0 on 2026-10-15,
  needed 2 attempts, ~60-70 s). The publish step retries only the errors the registry's
  PyPI validator labels transient - that not-yet-visible 404, a PyPI 429 or 5xx, an
  inconclusive package probe, PyPI unreachable (`internal/validators/registries/pypi.go`
  at v1.8.1) - up to 8 attempts 30 s apart (raised from 5 after that run, #111: headroom
  for a slower day while the job's designed worst case stays about 20 minutes) and fails at
  once on everything else; `tests/unit/test_release_metadata.py` pins that scope against
  the registry's own message texts and pins the 8 x 30 s budget.
  A version string must be unique and is immutable once published, so nothing is ever
  overwritten: a wrong entry is corrected by publishing a new version. Lifecycle is a status
  (`active`, `deprecated`, `deleted`, with an optional message) set with `mcp-publisher
  status` or `PATCH /v0.1/servers/{name}/versions/{version}/status`, requiring publish or
  edit permission on the namespace; `deleted` hides the entry from default listings but it
  stays retrievable with `include_deleted=true`, and metadata is never purged.
- *Publisher binary:* `mcp-publisher_linux_amd64.tar.gz` of release v1.8.1 (the latest,
  2026-08-06) was downloaded independently on 2026-10-15; its SHA256 matches the pin in the
  workflow and the one tracehub-mcp has used in production, and its sigstore bundle verified
  with `cosign verify-blob --certificate-identity
  https://github.com/modelcontextprotocol/registry/.github/workflows/release.yml@refs/tags/v1.8.1
  --certificate-oidc-issuer https://token.actions.githubusercontent.com` (cosign 3.1.3,
  exact identity, not a pattern; the same command with `@refs/tags/v1.8.0` fails as the
  negative control). Moving the pin means repeating that verification and changing both the
  version and the digest in one reviewed diff.

Break-glass and rollback: a manual re-publish is `workflow_dispatch` of
`mcp-registry-publish.yml` from `main` with the tag and `dry_run: false` - still CI
identity, the environment gate and the tag-pinned checkout; the personal-token procedure
(`mcp-publisher login github --token <read:org PAT>` from a personal host) is retired. A bad
entry is deprecated or deleted by status and superseded by the next version; it cannot be
edited in place.

Rejected: keeping manual publishing with a written runbook. Weaker on every axis the
decision cares about - a personal token, no CI identity, no guarantee that PyPI has the
version first, and no record that the published `server.json` was the tagged one - and it is
how 0.3.1, 0.3.3 and 0.4.0 came to be missing from the listing.

**Consequences:** The listing gains a verifiable publisher identity and is always produced
from the reviewed, tagged commit after PyPI has the version; a failing registry job leaves
PyPI and the GitHub Release untouched (it runs after `publish`, in parallel with
`github-release`, and holds no `contents: write`), and is repaired by the dispatch path once
the cause is fixed. The hosted part - the `mcp-registry` environment with the `v*` tag and
`main` deployment-branch policies - was applied on 2026-10-15 (recorded under decision
#10). HANDOFF.md's distribution note points here instead of describing the manual run as the
procedure; the README's registry sentence names `release.yml` as the publisher.


## 13. The tools accept only read statements by default; result rows are labelled as untrusted data

**Context:** Warehouse row content re-enters the model's context, and a model that has just read
a row can be steered by it into any statement the deployed credential allows. The 2026-10 audit
left this as an owner decision (`warehouse-content-to-unrestricted-sql-under-operator-credential`);
0.4.0 shipped the single-statement scanner (#15) and labelled error text (#18) but no
statement-type gate. The owner's decision (2026-10-14, #93): a read-statement allowlist on both
tools with an operator opt-out, a schema-visible provenance marker on result rows, and
documentation that the credential's grants are the boundary.

**Decision:** `sql_scope_keywords.assert_read_statement(sql, engine)` runs in both tools
immediately after `assert_single_statement`. It admits a statement only when its first ASCII
keyword - after comments and any opening parentheses, through the lexer shared with #15 - is
`SELECT`, `WITH`, `VALUES`, `SHOW`, `DESCRIBE`, `DESC` or `EXPLAIN`; a `WITH` is admitted only when
the walk over its CTE list (`name [(cols)] AS (body)`, comma separated, `RECURSIVE` allowed, every
body itself resolving to `SELECT`/`VALUES`, nested `WITH`s resolved the same way) finds `SELECT` or
`VALUES` as the main statement - Spark's grammar allows `WITH ... INSERT|UPDATE|DELETE|MERGE`, so a
first-keyword check alone would admit a write on Databricks. The walk is bounded at 16 nested
`WITH`s (`_MAX_CTE_NESTING`, counting CTE bodies and the parenthesised main statement it descends
into) and refuses deeper text with its own labelled message: it re-scans the enclosed text once per
level, so the unbounded first cut was quadratic in a model-controlled string (496 levels padded to
500 KB cost 47 s inside a `run_query_bounded` in-flight slot - eight such calls exhaust
`COST_GUARD_MAX_IN_FLIGHT_QUERIES`) and past ~497 levels overran the interpreter's recursion limit,
where the `RecursionError` surfaced as the generic tool error rather than this gate's refusal.
Sixteen exceeds any real query by an order of magnitude and caps the worst case at 16 scans of the
text; the walk's loops also skip every character that cannot open a comment or literal
(`sql_scope.LEXER_OPENERS`) before consulting the shared lexer. Under the multi-statement opt-out
every statement in the text must pass. Refusals are `UserVisibleError`s with an offset and the
opt-out's name, never the SQL (#15's rule). `COST_GUARD_ALLOW_WRITE_STATEMENTS=1`, exactly, read
per call, is the opt-out (`read_statement_check=skipped|refused` INFO records mirror #15's).
`BoundedQueryResult.rows_provenance` is the constant `"warehouse_data_untrusted"` with a schema
description stating that rows are data, never instructions - additive, the intended `tools/list`
diff; no class docstring (it would add a top-level description to the outputSchema). SECURITY.md
and README state that the authorisation boundary is the credential's grants and recommend a
read-only principal per engine. Rejected: a write-keyword denylist (fails open on every keyword it
does not list - `COPY INTO`, `EXPORT DATA`, `EXECUTE IMMEDIATE`, `CALL`, ...); per-dialect
allowlists (`SHOW`/`DESCRIBE` are Snowflake/Databricks-only, `EXPLAIN` does not exist on BigQuery -
the providers reject them as syntax errors and the gate's job is to refuse writes, not to validate
reads; one owner-confirmed set is smaller and no less safe); a SQL parser (#15's reasoning);
gating `rows` behind a new field for the #105 note (would widen the schema diff beyond the one
property).

**Consequences:** Breaking for operators who ran writes through `run_query_bounded`: they set the
opt-out (and should re-check the credential's grants). BigQuery scripts (`DECLARE`/`SET`/`BEGIN`)
need both opt-outs. False positives in the safe direction: `TABLE t`, Spark `FROM t SELECT`,
BigQuery pipe syntax, `SET`/`USE` session statements, `WITH ... MATERIALIZED`, CTEs nested more
than 16 levels deep. Admitted is not
supported: `SHOW`/`DESCRIBE`/`EXPLAIN` fail at the estimate on BigQuery and Snowflake and cannot be
combined with `max_rows` (the LIMIT wrapper is `SELECT`-shaped). A `SELECT` may still have side
effects through a remote function, UDF or system function the credential can call - the gate does
not claim otherwise. `EXPLAIN <DML>` is admitted (plan only on the engines that have EXPLAIN).
`hint` on an `ok` result is now possible (#17 amendment, #105). Ships in the next minor (0.5.0
planned: a breaking 0.x default change bumps the minor, as 0.4.0 did).

## 14. `run_query_bounded` cancellation: prompt detach, pre-dispatch check, provider-side deadlines, in-flight cap

**Context:** `run_query_bounded` is the only tool with a monetary side effect. Since v0.3.0
its blocking warehouse call runs in an anyio worker thread dispatched with
`abandon_on_cancel=True`, so an MCP `notifications/cancelled` detaches the request promptly
instead of being shielded until the warehouse call returns. The 2026-10 security audit
showed what that detach left open: (1) a cancel that arrived during the estimate did not
stop the abandoned thread from submitting the execution afterwards, into a cancelled future
nobody reads; (2) the only deadline on a submitted job was the client-side 120 s watchdog
running in that thread, so a host that escalates to SIGTERM/SIGKILL after closing stdin
(the reference MCP client does, 2-4 s after EOF; in Docker the server is PID 1 and ignores
SIGTERM) orphaned the query on the warehouse with no bound at all; and (3) every BigQuery
API round-trip outside the result wait inherited the client library's 600 s default retry
(doubled for job submission), so the job begin could still happen minutes after the cancel;
and (4) the detach returned anyio's thread-limiter token (the only, incidental, 40-token
concurrency bound) while the thread, its connection and its query stayed pinned, so a host
that cancelled and re-issued grew the number of live warehouse queries without any
in-process bound.

**Decision:** Keep `abandon_on_cancel=True` - a stop button that returns promptly is the
right behaviour for the host - and close the gaps around it instead of forwarding the
cancel into the engines:

- The tool body takes a keyword-only `before_execute: Callable[[], None] | None` hook
  that `server.py` binds to `anyio.from_thread.check_cancelled`, called exactly once after
  the estimate and every cap check and immediately before dispatch. A cancel received
  before execution never submits the job. The hook is injected by the server because
  `check_cancelled` raises outside an anyio worker thread, and it reads the thread's
  cancel-scope chain - the mcp dispatcher's `peer_cancel_mode="interrupt"` cancels the
  handler's `CancelScope`, which is what trips it.
- Every submitted job carries a provider-side deadline equal to the watchdog: BigQuery
  `QueryJobConfig.job_timeout_ms = 120000`; Snowflake `STATEMENT_TIMEOUT_IN_SECONDS` and
  Databricks `STATEMENT_TIMEOUT` as session parameters, lowered-only (a session value
  supersedes an account/user policy in Snowflake's hierarchy, so the effective value is
  read first and only ever lowered). The 120 s bound therefore survives the thread and the
  process. Equal to the watchdog, not watchdog + 10 s: the README promised 120 s and the
  bound itself is the point. Which side observes the deadline first is a race; either way
  the caller gets a sanitized error and exactly one WARNING record (the watchdog's own, or
  the `warehouse_client_call_failed` record when the warehouse's cancellation wins).
- BigQuery API calls carry a 30 s retry bound and `job_retry=None` (see
  `engines/bigquery.py`), so the pre-dispatch window is seconds, not minutes. Since #103
  (fixed after 0.4.0) that includes the polling RPCs inside `QueryJob.result()`, which had
  inherited the 600 s default; and when those retries are exhausted (`RetryError`, a
  `GoogleAPIError` that is not a `GoogleAPICallError`) the same bounded best-effort
  `QueryJob.cancel` the watchdog issues runs first - a `getQueryResults` 5xx storm does not
  imply `jobs.cancel` is unreachable, and one bounded cancel RPC is cheap against up to
  `jobTimeoutMs` of billing.
- An explicit in-flight cap, `COST_GUARD_MAX_IN_FLIGHT_QUERIES` (default 8, positive
  integer, rejected at startup otherwise), implemented as a `threading.BoundedSemaphore`
  whose slot is acquired non-blocking and released inside the worker thread. Acquire and
  release both live in the thread because anyio's `WorkerThread.run` skips the function
  when the future is already cancelled (an async-side acquire could leak a slot) and a
  blocking acquire would park a shared thread-pool token. A cancelled call therefore keeps
  counting until its thread exits. Over-cap calls raise `UserVisibleError`, which the
  existing `as_tool_error` wrapper turns into a `ToolError` naming the variable - not a
  `status="refused"` result, because no estimate exists yet (decision #4: `ToolError` for
  genuine exceptions). Default 8 rather than 40 (anyio's incidental limiter): the model
  should rarely need more than a handful of concurrent 120 s billable queries, and the
  variable exists for the host that does.

Deferred: forwarding the cancel to the engines' own cancel RPCs (`QueryJob.cancel`,
`SYSTEM$CANCEL_QUERY`, `Cursor.cancel`) and a SIGTERM/lifespan handler that cancels
in-flight jobs on exit. Both need a job registry threaded through all three engine
signatures and signal handling inside the mcp/anyio run loop, and neither helps on
SIGKILL - the provider-side deadline does.

**Consequences:** The install example uses `docker run --init` and the flag is
load-bearing (PID 1 otherwise ignores SIGTERM). A cancelled call still bills for a query
that was already running when the cancel arrived, for at most the 120 s watchdog. The
pre-dispatch check is only as good as the host's cancellation path through the dispatcher's
cancel scope; a host that simply drops the connection relies on the provider-side deadline.
A host issuing more than eight concurrent billable calls, or cancelling and re-issuing past
the cap, now sees a `ToolError` that names `COST_GUARD_MAX_IN_FLIGHT_QUERIES` and explains
that cancelled calls still count; raising the variable is the opt-out.

## 15. The tools accept exactly one SQL statement, enforced by a fail-closed scanner

**Context:** A security audit traced the cost-cap gate end to end: `run_query_bounded`
prices the submitted text once (one dry run / one `EXPLAIN`), compares two numbers, and
hands the identical text to the engine. Nothing pinned the statement count, so
`SELECT 1; DELETE FROM big_t` would be gated by whatever subset of the script the provider's
estimate happened to price and then execute in full. Whether each provider's dry run covers
every child statement is a provider fact the audit could not settle offline; a tool-layer
pin enforces the invariant regardless.

**Decision:** `sql_scope.assert_single_statement(sql, engine)` runs first in both
`estimate_query_cost` and `run_query_bounded`, before any estimate or execute call. It is a
small scanner (no SQL-parser dependency: the text is model-controlled input and a parser's
error path is its own attack surface) that models only the literal/comment forms each
dialect is known to have, and refuses whenever two plausible readings could disagree about
where a literal or comment ends - an identifier glued to a quote (a raw/bytes/hex prefix in
some dialects), a backslash right before the closing delimiter of a raw/hex literal or
quoted identifier, a `/*` inside a block comment, or any unterminated construct. A single
trailing `;` stays allowed (the LIMIT wrappers already strip it). The refusal is a
`UserVisibleError` -> `ToolError` (a client-side parse rejection, nothing to structure -
consistent with decision #4). `COST_GUARD_ALLOW_MULTI_STATEMENT_SQL=1`, read at call time,
is the operator opt-out.

**Consequences:** Multi-statement scripts (BigQuery `DECLARE ...; SELECT ...;`) are refused
unless the operator opts out. The opt-out disables only this scanner: on Snowflake the
per-call `MULTI_STATEMENT_COUNT=1` pin in `engines/snowflake.py` still rejects the text, and
`max_rows` wraps the whole submitted text in one LIMIT subquery, so a script has to run
without a row cap (both observed by the PR #86 runtime verification). A handful of
legitimate single statements are refused as
deliberate false positives in the safe direction (`THEN'x'` without whitespace,
`DATE'2020-01-01'`, raw strings ending in an odd run of backslashes, nested block comments)
and the hint says how to rewrite them. Every scanner mistake must stay in the refuse
direction: a new dialect construct is added only once its quoting rules are certain, and an
uncertain construct is refused, never guessed. One review finding against the first cut
illustrates the rule: the odd-backslash test runs on every candidate closing delimiter
before the doubled-delimiter skip, so `r'\''; DROP TABLE t; --'` is refused as ambiguous
rather than read as a doubled quote. A second finding against the first cut shows the rule's
limit: line comments were ended at any `str.splitlines()` boundary on the premise that ending
a comment earlier than an engine does is always the refuse direction, and the 2026-10-14 audit
re-run disproved that premise (#104) - a quote right after an early end opens a literal the
engine never sees, and that literal swallows the engine's top-level `;`, so two-statement text
was admitted. A line comment now ends where the dialect's lexer is known to end it: CR or LF
on Databricks (Spark SQL's `SIMPLE_COMMENT : '--' ('\\\n' | ~[\r\n])* '\r'? '\n'?`), LF only
on BigQuery and Snowflake (Snowflake's lexer is unpublished and ZetaSQL's rule could not be
re-located for citation, so a bare CR inside a comment is refused there as ambiguous - flip
`_BIGQUERY.line_comment_terminators` once it is), and a comment containing one of the eight
other `str.splitlines()` code points, or a backslash before its terminator (Spark's
continuation), is refused outright. Ending a comment too early is not always safe; refusing
the text whose comment end is uncertain is.

## 16. BigQuery dollar caps are also enforced server-side, and the provider's refusal is a normal result

**Context:** `run_query_bounded`'s dollar gate trusts the dry-run figure, which BigQuery
itself flags as uninformative in several shapes: row-level-security-protected tables always
report 0 bytes (a $0.00 `PRECISE` estimate), `LOWER_BOUND` accuracy is a floor, and absent
statistics were coerced to 0. The job was submitted with `maximumBytesBilled` only when the
caller supplied `max_bytes_billed`, so a cost-cap-only call ran with no server-side bound at
all. Whether real runs of those shapes bill above the cap is a provider fact the audit could
only observe with credentials; attaching a server-side cap makes the gate hold either way.

**Decision:** When `max_estimated_cost_usd` is finite and a dollar estimate exists, the
BigQuery job carries `maximumBytesBilled = max(ceil_to_1MiB(cap / $6.25 per TiB * TiB),
10 MiB)` (`cost_cap_to_max_bytes_billed`), lowered to a caller-supplied `max_bytes_billed`
when that is smaller and never raised above it. Rounding up to the next MiB mirrors how
BigQuery rounds `totalBytesBilled` before comparing, so "the gate admits => the provider
admits" holds for single-table queries; the 10 MiB floor is BigQuery's own per-query
minimum. The gate comparison and the dry-run accuracy tiering are unchanged (RLS queries
still run, now server-bounded). BigQuery's `bytesBilledLimitExceeded` rejection - matched by
`errors[].reason`, because the pinned client raises `BadRequest` on the API path but maps the
same reason to `InternalServerError` on the job path - is raised as
`ProviderByteCapExceeded(SanitizedEngineError)` and mapped to
`BoundedQueryResult(status="refused")` with `reason=cost_cap_exceeded` when the effective
limit came from the dollar cap and `byte_cap_exceeded` when it was the caller's own byte cap,
extending decision #4's "a cap refusal is a normal result" to provider-enforced caps; when no
cap of ours was attached (no `max_bytes_billed`, no finite dollar cap) the same provider
rejection is re-raised as the `SanitizedEngineError` it is, since there is no caller cap to
attribute it to. A `NaN` `max_estimated_cost_usd` is rejected with a `UserVisibleError`
before any comparison (it compares as under-cap for every estimate, and the mcp SDK's JSON
parser accepts a bare `NaN`); `+inf` is left alone because it compares correctly and means
"no dollar cap".

**Consequences:** BigQuery enforces the approved spend regardless of how the unresolved
provider facts turn out; nothing changes for Snowflake/Databricks (`execute_bounded`
signatures untouched). Residual, documented in README: BigQuery's 10 MiB-per-referenced-table
minimum means a tight cap on a many-table query can still be provider-rejected after passing
the gate - it now surfaces as the structured refusal rather than a provider error string. A
caller-supplied `max_bytes_billed=0` is passed through unchanged, as before.
Amended (#96, 2026-10): the RLS-shaped and `LOWER_BOUND` dry runs no longer carry a dollar figure
and are refused under a dollar cap before dispatch (#22), so "RLS queries still run, now
server-bounded" holds only for a byte cap; the derived `maximumBytesBilled` remains the second
layer for the figures that pass (`UNKNOWN`/unrecognised accuracy, a statement that references no
table, the dry-run-versus-run gap). `+inf` means "no dollar cap" for the comparison and the
derivation; with no dollar figure at all it is still a requested cap and refuses (#22).

## 17. Result rows are normalised to JSON-safe values at one choke point, after execution

**Context:** `BoundedQueryResult.rows` is typed `list[dict]` and all three `execute_bounded`
implementations return connector-native cell values. The pinned `mcp` SDK serialises a tool
result twice (pydantic `to_json` for the text content, `model_dump(mode="json")` for the
structured content), and both raise on a non-UTF-8 `BYTES`/`BINARY` cell or a type pydantic
does not know (BigQuery `INTERVAL` arrives as a `dateutil` `relativedelta`), while a `NaN`
float silently becomes JSON `null`. That failure happens *after* the billed query has run,
and the model then sees only the generic `Error executing tool run_query_bounded`.

**Decision:** `tools/run_query_bounded.py` normalises every row immediately after the engine
dispatch and before either return path (`ok` and `row_cap_exceeded`), so one function covers
three engines and both paths: bytes-like cells -> standard base64 text (always, never
"decode if valid UTF-8", which is ambiguous for consumers); `NaN`/`Inf` -> `"nan"`/`"inf"`/
`"-inf"`; `dict`/`list`/`tuple`/`set` recursed with non-string keys stringified; `Decimal`,
`datetime`/`date`/`time`/`timedelta` and `UUID` passed through because pydantic renders them
natively; anything else -> `str()`. It returns new objects and never raises. `BoundedQueryResult`
additionally carries `ConfigDict(ser_json_bytes="base64", ser_json_inf_nan="strings")` as a
backstop for a future path that bypasses the normaliser - serialisation-only, so the
generated `outputSchema` (and therefore `tools/list`) is unchanged; note pydantic's base64
mode uses the URL-safe alphabet and cannot help with unknown types, which is why the
normaliser is the fix and the config only a backstop.

**Consequences:** A billed execution always yields a structured result or an actionable
refusal. Documented behaviour change for consumers: valid-UTF-8 `BYTES` that used to arrive
as plain text (type erased) now arrive base64-encoded, and `NaN`/`Inf` are strings rather
than `null`. Row-size/row-count defaults are a separate hardening item, deliberately not
folded in here. Amended (#105): the normaliser also caps nesting depth at 64 container
levels per cell, replacing the deeper remainder with the constant placeholder
`<nested value omitted: deeper than 64 levels>` and reporting the count of capped cells in
`hint` (and in one INFO record); pydantic_core's recursion guard (~250 levels including the
`CallToolResult` envelope) otherwise failed the JSON-RPC response or the tool result after
the billed execution, which no in-repo code saw. `hint` is therefore no longer refusal-only
and `status` is the discriminator; a new field was rejected because decision #13 allows
exactly one additive `outputSchema` property. 64 is far above BigQuery STRUCT's 15-level
limit and an order of magnitude below the guard; raising it is a one-line change.
Amended (#94, 2026-10): a third post-execution path, `result_size_exceeded`, goes through the
same choke point; the size cap is measured on its output - #21. The normaliser now lives in
`tools/result_rows.py` (split for size; the tool imports what it uses, the tests import the
normaliser from there).

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

## 19. The cap-gate rate is bound to the observed warehouse; caller inputs can only raise it

**Context:** A security audit (fingerprint
`cap-gate-rate-from-caller-asserted-warehouse-size-not-observed-warehouse`) showed that
`run_query_bounded`'s only Snowflake/Databricks spend control compared `max_estimated_cost_usd`
with an estimate whose credit/DBU rate came solely from the model-supplied
`warehouse_size`/`edition` strings (defaulting to the cheapest tier), although the server
already ran `SHOW WAREHOUSES` for the pinned warehouse and discarded its size. An omitted size
admitted a query costing up to 512x/174x the cap while reporting `ok` and `UPPER_BOUND` — the
condition decision #5 forbids. The design (README, tool docstrings) had delegated the rate's
correctness to the model, the very principal the gate exists to constrain.

**Decision:** The estimate is priced at the warehouse the query will execute on as the server
observes it — Snowflake: the `SHOW WAREHOUSES` row (size/type/generation, read via
`fetchall()`/`description`, matched by its exact identifier - the upper-cased caller argument,
or the `CURRENT_WAREHOUSE()` name verbatim - because LIKE's `_` is a wildcard and a quoted
lower-case sibling is a different warehouse; a result that lists rows but none named exactly
like the pinned warehouse, or more than one, fails closed with no dollar estimate), resolving
`CURRENT_WAREHOUSE()` when no warehouse argument is given; Databricks:
`GET /api/2.0/sql/warehouses/{id}` for the `DATABRICKS_HTTP_PATH` warehouse, with `auth_type`
pinned and 60 s bounds — at `max(caller-asserted, observed)`. Edition is operator configuration
(`SNOWFLAKE_EDITION`, default `standard`) because the server cannot observe it without
ORGADMIN; the tool argument may only raise it. Warehouse types the rate table does not model
(non-STANDARD Snowflake, non-Serverless Databricks, unrecognised sizes) and a plan without a
usable `bytesAssigned` yield `estimated_cost_usd=None`, so a dollar cap refuses (#5). A failed
lookup keeps the caller-asserted/default rate with a caveat so roles without
`SHOW WAREHOUSES`/`CAN_USE` rights keep working — failing closed on lookup failure was
rejected because it would refuse every cost-capped call for such roles. Assuming the pricier
Gen2 rate when the generation is undeterminable stays a separate, open decision.

**Consequences:** Classic/Pro and Snowpark-optimized deployments lose the dollar figure (omit
the dollar cap to run there) — a breaking change released as 0.4.0. The
`estimate_query_cost`/`run_query_bounded` descriptions changed (an intended, description-only
`tools/list` diff). Release gates: `tests/integration/test_snowflake_live.py` pins the
`SHOW WAREHOUSES` size/type text shape and `CURRENT_WAREHOUSE()` resolution; a manual Databricks
run must confirm `warehouses.get` succeeds with `CAN_USE` on the warehouse.

## 20. Read-only tools do not wake a warehouse: fail closed on a non-running or unreadable state, opt in per engine

**Context:** `check_credentials` and `estimate_query_cost` carry `readOnlyHint: true` but run
on the pinned/configured warehouse session. Databricks: the Thrift session itself can start a
STOPPED Classic/Pro SQL warehouse, and after 0.4.0's 60 s connector bound a read-only call
against a STOPPED warehouse started (and billed) it and then failed with no result (PR #86,
behaviour change 16; issue #91). Snowflake: `SELECT CURRENT_REGION()`, the
`INFORMATION_SCHEMA.QUERY_HISTORY` table-function `SELECT` and the session-context `SELECT` can
resume a suspended `AUTO_RESUME` warehouse (issue #92). 0.4.0 shipped disclosure only (audit
records `read-only-tools-auto-start-stopped-warehouse-no-spend-bound` and
`read-only-tools-resume-suspended-warehouse-no-spend-bound`); the guards were left as owner
decisions, confirmed 2026-10-14 in #91 and #92. The state is already observed for pricing (#19):
Databricks `_lookup_warehouse` (`GET /api/2.0/sql/warehouses/{id}`, control plane, does not
start the warehouse) and the Snowflake `SHOW WAREHOUSES` row (`state`, `auto_resume`).

**Decision:**
- Databricks: `check_credentials` and `estimate_query_cost` open a session only when
  `_lookup_warehouse` reports `state == RUNNING`. Every other state (`STOPPED`, `STARTING`,
  `STOPPING`, `DELETING`, `DELETED`) and an unreadable state (lookup failed, non-warehouse
  `DATABRICKS_HTTP_PATH`, no state field) refuse fail closed: `check_credentials` returns
  `ok=false` with a detail naming the presented warehouse id and state (one WARNING record,
  the detail logged as-is - it is server-authored, bounded and one line; the id is the last
  `DATABRICKS_HTTP_PATH` segment - allowlisted to `[A-Za-z0-9]+` but unbounded operator
  configuration - so it goes through the same redact -> present chain as every other
  identifier, and a response that carries no state field gets its own hint - retry, or check
  the workspace UI - because neither "fix the path" nor "grant CAN_VIEW/CAN_USE" applies when
  the GET succeeded); `estimate_query_cost`
  returns `estimated_bytes=None`, `estimated_cost_usd=None` and exactly one caveat - no
  `EXPLAIN COST` runs, and no pricing or statistics sentence is attached because none of that
  happened. `check_credentials` therefore gains one bounded control-plane call (about
  2 x 60 s worst case with PAT auth; OAuth M2M keeps the databricks-sdk's own OIDC discovery
  budget on this call as on the session, the residual already documented for `_connect()`).
  `COST_GUARD_DATABRICKS_ALLOW_WAREHOUSE_START=true` (exactly) restores the 0.4.0 control flow,
  including #19's caller-asserted-rate fallback on a failed lookup. This narrows #19's
  fail-open on the read-only paths for principals without `CAN_VIEW`/`CAN_USE`: they now get a
  refusal unless they opt in; #19's rationale ("would refuse every cost-capped call for such
  roles") is untouched because cost-capped calls are `run_query_bounded`, which is not gated.
- Snowflake: the three statements are gated behind the pinned warehouse's `SHOW WAREHOUSES`
  `state == STARTED` or `COST_GUARD_SNOWFLAKE_ALLOW_WAREHOUSE_RESUME=true` (exactly):
  `SELECT CURRENT_REGION()`, the `QUERY_HISTORY` read, and `check_credentials`' context `SELECT`.
  Unknown state - no pinned warehouse (`CURRENT_WAREHOUSE()` NULL), no or ambiguous SHOW row, a
  name the allowlist refuses, a NULL state column, or a failed lookup - counts as not STARTED;
  a session with no current warehouse is treated as unknown state (not as provably free) so
  the rule stays uniform, even though a context-function `SELECT` has nothing to resume there.
  When gated, the estimate keeps the SHOW row's size/type/generation (so #19's observed-size
  floor and its fail-closed types still apply) but has no cloud provider, so it prices at the
  Gen1 rate with the byte-tier heuristic and says so in a caveat naming the warehouse and its
  presented state (a NULL state column gets its own wording: "reports no state");
  `check_credentials` returns `ok=true` (login succeeded) with a detail built from the SHOW row
  only, labelled "session identity not probed". The configured role and account are NOT
  echoed into that detail: config.py's rule is that operator configuration never travels back
  to the MCP client, and the SHOW row carries neither value anyway (the owner's recommended
  "role/account/warehouse/state" detail would have needed exactly the gated statement).
  `USE WAREHOUSE`, `EXPLAIN USING JSON`, `SHOW WAREHOUSES`, `SELECT CURRENT_WAREHOUSE()` and the
  `_connect()` session-parameter statements are never gated.
- **Named deviations from the issue text (owner-visible, listed in the PR body):**
  1. The no-argument rule is implemented literally as #92 states it - "When `warehouse is
     None`, skip the QUERY_HISTORY lookup unless opted in" - i.e.
     `history_allowed = allow_resume or (warehouse is not None and state == STARTED)`. The
     design draft had read it as "when `CURRENT_WAREHOUSE()` is NULL", under which a
     no-argument estimate on a STARTED session warehouse would still run the history read. The
     literal reading is the fail-closed one and costs only history calibration on no-argument
     calls; the session warehouse is still resolved and observed (SHOW, and CURRENT_REGION on a
     STARTED row) so #19's observed-warehouse pricing is kept, and the caveat says why history
     was skipped. Pinned by
     `test_explain_estimate_skips_history_on_a_started_session_warehouse_without_an_argument`.
  2. `SELECT CURRENT_WAREHOUSE()` is left ungated. It is not in the owner's three-statement
     gated set and not in PR #86's six-statement attribution list; it already ran on every
     no-argument estimate for #19, and without it the no-argument paths cannot name the
     warehouse whose state to read (it is new on `check_credentials`' no-argument path). It is
     the same class of statement as the gated `CURRENT_REGION()` (a context-function-only
     `SELECT`), so the attribution test below measures seven statements, not six. Gating it
     instead would blind every no-argument path (the rejected alternative).
- Casing: both guards compare the state text stripped and upper-cased against the running
  value (`STARTED` / `RUNNING`), so a differently-cased running value passes and any other text
  refuses. Snowflake normalises once when it reads the SHOW row (`_text_or_none(upper=True)`,
  pre-#20, so the stored and presented state is already canonical); Databricks normalises in
  the predicate only and keeps the SDK value as returned for the texts - the SDK's `State` enum
  is upper-case by construction and its parser turns an unknown or differently-cased wire value
  into `None` ("reports no state", refused), so the rule only matters for a non-SDK value.
  Pinned by one lower-case-state test per engine.
- Both opt-ins are read at call time in `config.py` and accept exactly `true`; any other value
  leaves the guard on silently and the refusal text spells the value. Default off. Not fields
  on the frozen config dataclasses (the unit suite builds those as `MagicMock`s, whose unknown
  attributes are truthy). One INFO record per guard decision
  (`engine=<e> warehouse_state_guard=<refused|enforced> path=<p> state=<presented> [skipped=...]`
  and `warehouse_state_guard=skipped reason=<VAR>=true` when the env var - not
  `allow_warehouse_wake` - caused a bypass), mirroring `single_statement_check=`; the success
  paths stay log-free, and a Databricks `check_credentials` refusal keeps only its WARNING.
- `run_query_bounded` is untouched: `tools/run_query_bounded.py` takes its estimate with
  `allow_warehouse_wake=True` (a keyword-only flag threaded through
  `tools.estimate_query_cost` to both engines' `explain_estimate`, never a tool argument),
  because the execution that follows wakes the warehouse anyway and starting it is the
  accepted cost of an executed query under caps. The published tool signatures are unchanged
  (`tools/list` byte-identical except the two read-only tools' descriptions, pinned by
  `test_tools_list_input_schemas_are_unchanged_by_the_warehouse_state_guard`). `execute_bounded`
  has no guard code.
- Rejected: pricing a known-Gen2 warehouse with an unknown provider at the highest Gen2
  multiplier instead of Gen1 (would be the safer upper bound but is a pricing change beyond the
  issue; the gated estimate never feeds a cap) - kept as a candidate follow-up; gating
  `SELECT CURRENT_WAREHOUSE()` (see deviation 2); echoing the configured role/account in the
  gated detail (see above); a retry loop around the state read (adds latency to a read-only
  tool for a sub-second race window).

**Consequences:** On a stopped Databricks warehouse the read-only tools now return promptly with
no spend instead of starting it; on a suspended Snowflake warehouse the estimate is a Gen1
heuristic (possibly below the real Gen2 cost by up to 1.35x - acceptable for a read-only
estimate that never gates a cap) and `check_credentials` proves login but not the session
identity. Transitional states (`STARTING`, `RESUMING`, `SUSPENDING`, `RESIZING`) refuse; the
hints say to wait and retry. The state read and the gated statement are not atomic: a
warehouse that suspends in between is resumed by the statement (accepted; the window is one
round-trip). Under the Snowflake opt-in the statement list is 0.4.0's for a resolved row; for
an empty, unresolved or allowlist-refused SHOW result it is one (unused) `CURRENT_REGION()`
statement shorter, and a `CURRENT_REGION()` failure on a STARTED warehouse still discards the
whole observation for pricing (0.4.0's contract: caller-asserted size at the Gen1 rate; the
guard then also skips the history read - fail-closed direction, pinned) - but the caveat and
the INFO record say what happened (`state='STARTED' current_region=failed
skipped=query_history`, "SELECT CURRENT_REGION() failed"), not that the state could not be
read: the SHOW row was read and is carried as `_RegionLookupFailed`. Documented in README
(Tools, Setup, Known limitations), `server.json`,
`.mcp.json.example`, `describe_engine_capabilities`, the two tool descriptions and the CHANGELOG.
`engines/snowflake.py` grows past the 500-line guideline it already exceeded (955 -> ~1200);
splitting it is out of scope for this change.

**Recorded follow-up (owner, not part of the PR):** the per-statement attribution tests from
PR #86, items 2 and 3, on development accounts only. Databricks: a throwaway smallest warehouse,
stopped; call `check_credentials` once while polling `GET /api/2.0/sql/warehouses/{id}`, repeat
with `estimate_query_cost`, then (with the opt-in set) once more each; read
`system.billing.usage` for the windows; expected: no start and no usage row for the guarded
calls. Snowflake: a throwaway X-Small warehouse W with `AUTO_RESUME`/`AUTO_SUSPEND` and an
observation warehouse O; re-suspend W before each statement and run, one at a time from a
session pinned to W: `USE WAREHOUSE W`, `SELECT CURRENT_WAREHOUSE()`, `SHOW WAREHOUSES LIKE 'W'`,
`EXPLAIN USING JSON SELECT 1`, `SELECT CURRENT_REGION()`, the `QUERY_HISTORY` table-function
`SELECT`, and the four-function context `SELECT` (seven statements); after each, read
`SHOW WAREHOUSES` state and `QUERY_HISTORY_BY_WAREHOUSE` from O; then end-to-end
`estimate_query_cost`/`check_credentials` calls with and without the opt-in, and
`WAREHOUSE_METERING_HISTORY` an hour later. Record the result here (which of the seven
statements resumed W), next to the `_WarehouseFacts` docstring in `engines/snowflake.py` and in
README. If a statement this decision leaves ungated resumes the warehouse, gate it (or, for
`CURRENT_WAREHOUSE()`, resolve the session warehouse another way); if a gated one is proven
free, un-gate it - both as a follow-up PR, each changing only the statement set, never the
fail-closed default. `tests/integration/test_snowflake_live.py` already asserts the `state`
column is present; a suspended-state shape test needs W.

## 21. `run_query_bounded` bounds its result by default: 1000 rows, 8 MiB serialised, and says whether the query ran

**Context:** #94 (audit `unbounded-fetchall-row-size-default`): `max_rows` defaulted to `None`;
Snowflake and Databricks fetched the whole result with `fetchall`, BigQuery iterated `result()` to
exhaustion, and nothing bounded the serialised size of `rows` - the #17 normaliser only guarantees
serialisability. The audit's mocked run returned 100,000 rows unbounded and an 8.4 MB response for
one row, while `server.json` advertised "result-size guardrails". #101 (audit
`row-cap-refusal-after-billed-run-described-as-not-run`): the tool description said every refusal
"did NOT run" while `row_cap_exceeded` follows a billed execution, so a model could re-run a query
believing nothing had been spent. Owner decisions 2026-10-15; held for 0.6.0.

**Decision:**

- *Default row cap.* When the caller omits `max_rows` the tool applies `DEFAULT_MAX_ROWS = 1000`
  before the existing `1..2^63-2` validation, so the effective value is what is validated and
  dispatched; an explicit value is honoured unchanged and a larger one is the opt-out. The
  inputSchema is unchanged (`max_rows: int | None = None`): the default lives in the tool.
- *Size cap.* `MAX_RESULT_BYTES = 8 MiB`, measured exactly in the tool on the normalised rows -
  #17's choke point is the only place every cell is already JSON-shaped, so what is measured is
  what is sent - as compact JSON (`separators=(",", ":")`, `ensure_ascii=False`, `default=str`)
  in UTF-8 bytes, one `json.dumps` per row plus list framing. Over the cap, the longest prefix of
  whole rows that fits is returned (zero rows when the first alone is over; never a partial row)
  with the new `RefusalReason.RESULT_SIZE_EXCEEDED`. Size is checked before the row cap: the row-cap
  hint's "showing the first N" would be false once rows were dropped for size.
- *A new reason value, not a new status and not a reused reason.* The remedies differ - a model
  that reads `row_cap_exceeded` raises `max_rows`, which cannot fix a size problem and would
  re-bill the query - and `reason` is the machine-readable field, `hint` is prose. Every client
  discriminates on `status: Literal["ok", "refused"]` (#4); a third status would break that
  contract and the pinned snapshot non-additively, while a new enum member only extends a list.
- *`executed`.* `BoundedQueryResult.executed: bool`, required (no default) so a forgotten return
  path fails at construction instead of silently claiming either direction, with a property-level
  description (never a class docstring, #13's rule). `True` on `ok`, `row_cap_exceeded` and
  `result_size_exceeded`; `False` on the four pre-execution refusals and on BigQuery's
  `maximumBytesBilled` rejection - the service evaluates that limit before running the job, so the
  statement never ran even when a failed job id exists (#16). `outputSchema.required` becomes
  `["status", "executed"]`. The description now says which reasons imply a billed run.
- *Engine-side early stop.* One shared helper, `result_bounds.collect_rows`, consumes rows one at
  a time and stops at the `max_rows + 1` sentinel row (checked first; the engine returns exactly
  `max_rows` rows and `row_cap_hit=True`) or once a running size estimate crosses the cap.
  Snowflake and Databricks page with `fetchmany(500)` (`result_bounds.iter_fetchmany`, until an
  empty page - a short page is not taken as the end; Databricks opens its cursor with
  `arraysize=500`); BigQuery passes `max_results=max_rows + 1` and `page_size=500` so the very first
  `jobs.getQueryResults` download is bounded too. The estimate (`approx_row_size`) is a strict lower
  bound on the exact measurement, by construction and by a property test over the normaliser's
  whole type table (`tests/unit/test_result_bounds.py`; a new branch in `_json_safe_value` must
  extend it): a size stop therefore never produces an `ok`. It counts the framing compact JSON
  always renders - braces, brackets, every key's and string's quotes, colons, commas and the
  inter-row comma - so it is exact for rows of bare primitives (the first cut skipped these and let
  narrow rows run ~2.7x past the cap; fix-up review 2026-10-15). It still under-estimates by up to
  ~6x for text made of control characters (six JSON bytes per estimated byte), 4/3 for binary, so in
  serialised bytes an engine may hold up to ~6x the cap plus one driver page before the stop fires.
  `collect_rows` reports a
  hit on a size stop as well as on the sentinel, and the tool refuses - with an honest "the fetch
  stopped after N rows" hint and one WARNING - if the exact measurement ever comes in under the cap
  after such a stop (a broken bound; the one known theoretical case is a cell whose dict keys
  collide after stringification). The fail-closed direction is a truncation refusal, never a
  silent `ok`.
- *Large explicit `max_rows`.* `result_bounds.LARGE_MAX_ROWS_WARNING_THRESHOLD = 100_000`: an
  explicit value above it is honoured and logged once at WARNING (counts only, no row content). No
  refusal and no new knob - the explicit value is the opt-out - but the byte cap bounds the
  serialised rows, not the process memory and post-billing CPU they cost (Consequences), and the
  operator should see when a caller reaches for millions of rows.
- *LIMIT wrap rule.* `sql_scope_keywords.is_single_query_statement`: the text is exactly one
  statement (`sql_scope.is_single_statement`, the gate's scanner, never raising), its main
  keyword - resolved through any CTE list by the read gate's own walker - is `SELECT` or `VALUES`,
  and its own `WITH` is not `WITH RECURSIVE` (amendment of 2026-10-19 below). Only then does the
  engine enclose it in `SELECT * FROM (...) AS cost_guard_row_cap LIMIT max_rows + 1`; a recursive
  CTE, `SHOW`/`DESCRIBE`/`DESC`/`EXPLAIN` (Databricks), opt-out scripts and `WITH ... INSERT`
  (Spark allows it; the write opt-out skips the gate) run unwrapped and are capped by the paged
  fetch. This keeps the documented opt-out configurations working now that a row cap always
  applies, and an explicit `max_rows` on those statements now works instead of failing at the
  provider. Decided once in the tool, with the gates' lexer, so the engines cannot drift.
- *Fetch under the watchdog.* A monotonic deadline is armed at submission (`execute_async` on
  Snowflake, before `thread.start()` on Databricks) and checked only before a page request that
  follows a full page; the poll loop's nominal counter (which ignores status-call latency) is left
  untouched. The first page is never pre-empted (Snowflake's connector issues the `RESULT_SCAN`
  lazily inside the first `fetchmany`), and a short page means the connector has exhausted the
  result (Snowflake's `fetchmany` loops `fetchone` until `size` rows or `None`; Databricks'
  `fetchmany_arrow` fills from the server while `has_more_rows`), so the end-of-result probe that
  follows it is never checked: a complete, billed result already in hand is never discarded because
  its only page overran (the first cut checked before the probe too and discarded such results -
  fix-up review 2026-10-15). No cancel on a fetch timeout: the query is done, only its download
  overran.
- *Placement.* `result_bounds.py` sits at package level next to `sql_scope.py`: the engines must
  not import from the tool layer, and `sql_scope.py` is the precedent for engine-shared helpers
  there (no import cycle exists either way - `tools/__init__.py` is empty; this is layering, not
  necessity). `tools/run_query_bounded.py` would have crossed the 500-line guideline, so the #17
  normaliser moved to `tools/result_rows.py` (the tests import it from there; the depth-note INFO
  record stays under the tool's logger). No new environment variable: the explicit `max_rows` is the
  opt-out, the byte cap has none.

**Rejected / deferred:** `status: "truncated"`; reusing `row_cap_exceeded` with a distinct hint;
a configurable cap via env var (would need `server.json`/`.mcp.json.example` plumbing for a knob
nobody asked for; a model cannot usefully consume more than 8 MiB of rows per call); a fourth
`execute_bounded` return value `stopped_on_size` (two sources of truth, every mock in the suite
changes - the lower-bound estimator plus the hit-on-size-stop rule keeps one); an unconditional wrap
(makes `SHOW`/scripts unusable); skipping the wrap whenever `max_rows` was defaulted and relying on
the paging stop alone (unbounded server-side materialisation on Snowflake - the wrap is what bounds
the result the warehouse computes); replacing `get_results_from_sfqid` with our own
`cur.execute("SELECT * FROM TABLE(RESULT_SCAN('<uuid>'))", timeout=remaining, num_statements=1)`
(a hard bound on the one round trip the deadline cannot pre-empt, at the price of replacing a
pinned public API with an interpolated statement - the drop-in if the residual below ever matters);
Databricks `Connection.cursor(row_limit=max_rows + 1)` (`TExecuteStatementReq.resultRowLimit`, a
server-side row cap that would bound `SHOW`/`DESCRIBE` results at the server with no wrap) - deferred:
no live Databricks job exists to verify `resultRowLimit` semantics, and an unverified Thrift field on
the billable path with no gate catching a regression is the wrong risk; owner follow-up.

**Consequences:** behaviour change for callers relying on unlimited rows (pass an explicit
`max_rows`; no opt-out from the byte cap). The wrapped statement must be valid as a derived table;
the README and the tool description say so, and the live cases in
`tests/integration/test_bigquery_live.py` pin what BigQuery does with the wrapped form. The 0.6.0
pre-tag live run (`integration.yml` 39275182240) answered two of the three questions the fix-up
review had flagged as unverified at merge time (amendment of 2026-10-19 below): a recursive CTE is refused inside the wrap and now runs
unwrapped, and a duplicate output column name is accepted. The third case pins whether an inner
`ORDER BY` survives the outer `LIMIT max_rows + 1` (if not, the tool description gains "put the
LIMIT inside your query when order matters"). `SHOW`/`DESCRIBE`/scripts gain a working row
cap. The `tools/list` diff is exactly: `+ "result_size_exceeded"` in
`outputSchema.$defs.RefusalReason.enum`, the new `executed` property, `required` becomes
`["status", "executed"]`, and the description text; the inputSchema and the other three tools are
byte-identical. The wire response is not the measured size: the SDK renders the rows twice -
compact in `structuredContent` and `indent=2` in a text block that is JSON-string-escaped again in
the frame - so the frame is 2x-4x `total_bytes` (measured 2.0x on ~8 KB rows, 3.8x on narrow
seven-column rows; one-column integer rows approach 5x): an 8 MiB result leaves the process as
roughly 17-32 MiB. The bounds above are in serialised bytes: what an engine holds
before the stop fires is at most ~6x the cap in JSON terms (the control-character worst case) plus
one driver page (BigQuery REST pages <= ~10 MB and `maxResults=500`; Snowflake lazily downloaded
chunks; Databricks `FetchResults(max_rows=500, max_bytes=100 MB)`). Process memory is another
matter: the Python objects behind narrow rows weigh ~25x their JSON size, and the normalise + trim
pass over them runs after the billed execution, outside any watchdog, on the single-process stdio
server with the GIL held. Measured on the development machine with `{"a": 1}` rows and an explicit
`max_rows=5,000,000` (the schema allows it): the fetch stops at 1,048,577 rows after 1.7 s at a
223 MB peak, normalise + trim take 2.3 s more and peak at 444 MB
(`scratch-pad/rq-caps-2026-10/fixup/probe_narrow_rows_cost.log`; before the estimator counted the
JSON framing the same probe collected 2.8M rows at 539 MB and spent 52 s and 1.77 GB in normalise +
trim - the fix-up review's finding). An explicit `max_rows` in the millions therefore costs
hundreds of MB and seconds of post-billing CPU per call: keep explicit values in the tens of
thousands; the tool logs one WARNING above 100,000; the default 1000 is nowhere near any of this.
For unwrapped statements Snowflake still computes the full result server-side (bounded by the 120 s
statement timeout); the client downloads only the chunks it iterates. The Snowflake and Databricks
fetch phase can exceed 120 s by what the deadline cannot pre-empt: Snowflake's `RESULT_SCAN`
(connector network timeout 60 s, `STATEMENT_TIMEOUT_IN_SECONDS` 120 s) plus the first chunk; a page
requested within budget while it downloads; and a result whose last page was full (exactly a
multiple of 500 rows) still pays the check before its end-of-result probe, since the client cannot
tell it from a result with more pages. `tests/unit/test_result_bounds.py`'s property test is the
guard on the estimator; the hit-on-size-stop rule is the fail-closed fallback behind it. Ships in 0.6.0
(additive schema; a default change on a 0.x line that already moved defaults in 0.5.0).

**Amended 2026-10-19 (#124): a recursive CTE runs unwrapped; the BigQuery duplicate-column remedy
is withdrawn.** The 0.6.0 pre-tag live gate (`integration.yml` run 39275182240 on `main` 36151ad)
answered two of the provider questions above, before the tag. (1) `run_query_bounded("bigquery",
"WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT n FROM t")`
dispatched `SELECT * FROM (WITH RECURSIVE ...) AS cost_guard_row_cap LIMIT 1001` and BigQuery
refused it at compile time, nothing billed: `400 WITH RECURSIVE is only allowed at the top level of
the SELECT, CREATE TABLE AS SELECT, CREATE VIEW, INSERT, EXPORT DATA statements. (reason:
invalidQuery)`. In 0.5.0 the same text ran unwrapped and was capped by the fetch; the #94 fix-up's
`_main_keyword_after_ctes` skips the `RECURSIVE` word, so `is_single_query_statement` admitted the
statement to the wrap. The rule now: `is_single_query_statement` answers "may the engine enclose
this text in the wrap?" and is False when the statement's own `WITH` - the first keyword after
whitespace, comments and opening parentheses, in any case - is followed by `RECURSIVE`. The tool
then passes `wrap_in_limit=False`, all three engines dispatch the text unchanged, and the paged
fetch (the `max_rows + 1` sentinel -> `row_cap_exceeded`, `executed: true`) and the 8 MiB cap
remain the bounds - exactly as for `SHOW`, scripts and `WITH ... INSERT`. Only the outermost
`WITH` counts (a recursive CTE nested inside a CTE body is the warehouse's to accept or refuse,
wrapped or not), and a CTE merely named `recursive_t` is an ordinary `WITH`. The read gate
(`assert_read_statement`) is untouched: `WITH RECURSIVE ... SELECT` is still admitted and
`WITH RECURSIVE ... INSERT` is still refused as a write. Unit tests pin the truth table, the text
each engine dispatches (through SDK-level fakes) and the 1,500 -> 1,000-row truncation of an
unwrapped recursive result; the live case now asserts the unwrapped run. (2) `SELECT * FROM
(SELECT 1 AS a, 2 AS a) AS cost_guard_row_cap LIMIT 1001` was accepted: the live case expecting
`Duplicate column names in the result are not supported` reported DID NOT RAISE, and the returned
column names were not observed. The remedy "BigQuery rejects duplicate output column names inside
the wrap - alias them" was therefore an unsupported provider claim; it is withdrawn from the
README, the tool description and the 0.6.0 release notes, which now state only what holds: the
wrapped statement must be valid as a derived table. That live case is now a pin of the observed
behaviour (`status: ok`, one row, `executed: true`) whose assertion messages print the rows, so a
regression shows what BigQuery returned. The column names of a passing wrapped run are still
unobserved: a passing assertion prints nothing, so the case also prints the rows unconditionally
and `integration.yml` runs the BigQuery job with `-rA`, which shows captured output for passed
tests; the next live run records them.

## 22. BigQuery estimates without a usable byte figure produce no dollar figure, so a dollar cap refuses

**Context:** Issue #96 (post-remediation triage; PR #86's "accepted for now" bullet 5). Three
provider facts reached `run_query_bounded`'s dollar gate as if they were estimates. (1) BigQuery's
dry run reports 0 bytes processed for any query against a table protected by row-level security -
Google's cost documentation: "dry runs always return 0 bytes to prevent side-channel attacks"; its
RLS best practices: BigQuery "hides sensitive statistics on all queries against tables with
row-level security", including "the number of bytes ... processed" and "the number of bytes
billed" - and `dry_run` tagged that 0 `PRECISE`, priced it at $0.00 and added a caveat, so the
figure passed every `max_estimated_cost_usd`. (2) `totalBytesProcessedAccuracy: LOWER_BOUND`, which
the REST reference (`JobStatistics2.totalBytesProcessedAccuracy`) defines as "estimate is lower
bound of what the query would cost", was relabelled `UPPER_BOUND` and priced: a floor presented as
a cap. (3) `is_capacity_billed` searched reservation assignments in `US` only, so a project whose
reservation lives elsewhere was priced on-demand. #16's derived `maximumBytesBilled` bounded the
run server-side, which is why #86 accepted (1) and (2). Owner decision 2026-10-16: fail closed when
a dollar cap is requested (AGENTS.md: a requested cap must never go silently unenforced) and look
the billing model up at the job's location.

**Decision:**

- *The RLS shape.* `dry_run` treats `totalBytesProcessed` of 0 - or absent, coerced to 0 as
  before - on a statement whose `referencedTables` is non-empty as the row-level-security shape:
  `estimated_bytes` stays `0` as reported, `accuracy_tier` becomes `HEURISTIC`,
  `estimated_cost_usd` is `None`, and the caveat keeps the facts (always 0 bytes, by design, to
  prevent a side-channel; the $0.00 must not be trusted) and names the remedy. A statement that
  references no table (`SELECT 1`) legitimately prices at $0.00 `PRECISE` and is unchanged. The
  predicate is deliberately coarse: `totalBytesProcessed` and `referencedTables` are the only
  fields the dry-run response offers, and they cannot tell a hidden figure from a genuinely free
  one (Consequences, below), so the caveat describes what was observed (0 bytes on a statement
  that references a table) and lists row-level security among the known causes of that shape
  instead of asserting it.
- *LOWER_BOUND.* Mapped to `HEURISTIC` - the only one of #3's three tiers that promises no bound
  in either direction - with `estimated_bytes` the reported floor and `estimated_cost_usd=None`;
  the caveat names the accuracy and says floor-not-cap. `UNKNOWN` ("accuracy of the estimate is
  unknown"), `UPPER_BOUND` and unrecognised values keep today's `UPPER_BOUND` label and their
  dollar figure: nothing shows UNKNOWN to be a floor, and #16's derived cap bounds those runs.
- *The gate.* Nothing new in the tool: both shapes arrive at #5's existing
  `estimated_cost_usd is None` branch and are refused before dispatch (`reason:
  cost_cap_exceeded`, `executed: false`, INFO `detail=no_cost_estimate_available`); the hint now
  names the two BigQuery shapes and the remedy (in its BigQuery-only sentence; the generic
  sentence stays engine-neutral). `estimate_query_cost` shares `dry_run`, so the read-only
  estimate shows the same facts; `describe_engine_capabilities` says so in `known_gaps`.
  `max_estimated_cost_usd=+inf` reaches this branch too: #16's sentinel compares as "no dollar
  cap" against any dollar figure and derives no server-side byte cap, but the parameter was
  given, so with no dollar figure to compare the call is refused like any other dollar cap -
  omit the parameter, or use `max_bytes_billed`. That was already the branch's behaviour for a
  capacity-billed project; #22 widens the set of estimates it applies to, and the README's
  sentinel sentence now says so. Making `+inf` skip the branch instead is a one-line change
  (`math.isfinite` before refusing, mirroring the derivation) left to the owner.
- *Why `estimated_bytes` stays as reported, so `max_bytes_billed` keeps working.* The byte gate
  refuses only on `estimated_bytes is None`; with 0 (or the floor) it admits, and the job carries
  `maximumBytesBilled = max_bytes_billed` (#16), which BigQuery evaluates before running the job.
  The byte cap's promise ("never bill more than N bytes") is delivered by the provider in its own
  unit; the pre-flight comparison being uninformative does not make the cap unenforced. The dollar
  cap's promise ("refuse if the estimated cost exceeds $X") presupposes an estimate, and a $0.00
  built from a hidden statistic is not one - reporting it as compared is the condition #5 forbids.
  `0` is also the true datum: it is what the API returned; the caveat says it is not the bytes the
  query would read.
- *Location.* `is_capacity_billed(project, location="US")` keeps its signature; `dry_run` passes
  `QueryJob.location` (`jobReference.location` - the location whose slots the query would consume;
  the installed `google-cloud-bigquery` 3.46.1 returns `None` when the service sent none) and falls
  back to `US` only then, adding a caveat. `SearchAllAssignments` is per region (the installed
  `google-cloud-bigquery-reservation` 1.28.0 documents it as a lookup "for a particular region";
  the parent's only wildcard is the project), so there is no all-locations call. The location is
  service-returned text entering a hand-built resource path, so it is allowlisted
  (`^[A-Za-z][A-Za-z0-9-]{0,63}$`) like the project id (#6) before interpolation; a failure
  surfaces through `@sanitize_exceptions` as the existing "could not verify billing model" path (no
  dollar figure). Every dry run logs one INFO record `engine=bigquery dry_run billing_model_check
  location=<presented> location_source=job|fallback outcome=capacity_billed|on_demand|check_failed`,
  and the capacity-billed, check-failed and fallback caveats carry the location.
- *Placement.* The pure rules (accuracy table, location allowlist, job-location resolution, the
  remote-billing text heuristic) live in `engines/bigquery_estimate_rules.py` so
  `engines/bigquery.py` stays under the 500-line guideline; every SDK-calling function stays where
  the tests patch it.

**Rejected / deferred:** a fourth `AccuracyTier.LOWER_BOUND` value (#3's structural three-value
contract, pinned by `tests/unit/test_types.py`, and a published `outputSchema` enum - an intended
`tools/list` diff for no gain: the consumer's action is the same as for HEURISTIC); keeping the
pass-through with #16's server-side cap as the only control (the gate would keep reporting a
fictitious $0.00 / floor as if it had been compared - the condition #5 forbids - and the issue's
own alternative (b)); `estimated_bytes=None` for the RLS shape (would make `max_bytes_billed`
refuse too, leaving RLS users no cap to run under); keeping `UPPER_BOUND` for LOWER_BOUND with the
figure dropped (still a label promising `actual <= figure` about a documented floor); relabelling
`UNKNOWN` as HEURISTIC or dropping its figure (not shown to be a floor; owner's call, deferred);
failing closed on the location fallback (the check still ran, for the pre-#96 location; the caveat
records the doubt); querying every location (no such API call; N round-trips per estimate); a text
heuristic exempting `COUNT(*)`-style aggregates from the RLS shape (a second guess layered on the
first, in the admit direction - an RLS-protected table's `COUNT(*)` dry run reports the same 0 -
when the byte cap already runs them).

**Consequences:** behaviour change for BigQuery callers whose queries touch RLS-protected tables or
produce `LOWER_BOUND` estimates: `max_estimated_cost_usd` now refuses them (`executed: false`,
nothing billed) where it used to admit a $0.00 / floor figure - bound such runs with
`max_bytes_billed`, which BigQuery enforces server-side; the read-only estimate shows
`estimated_cost_usd: null`, `accuracy_tier: HEURISTIC` and the caveat. Accepted false positives,
in the safe direction: any dry run reporting 0 bytes on a statement that references a table is
treated as this shape, including aggregates BigQuery answers from table metadata such as an
unfiltered `COUNT(*)`, a `SELECT` over an empty table and a partition filter that prunes every
partition - base ran those at $0.00 under a dollar cap; a dollar cap now refuses them too, and
`max_bytes_billed` runs them. The caveat therefore describes the shape and lists these causes
alongside row-level security rather than asserting it (pinned by
`test_dry_run_zero_byte_aggregate_answered_from_metadata_is_treated_as_the_rls_shape`). No schema
change (`AccuracyTier` and every tool description unchanged, so `tools/list` is byte-identical); the
`describe_engine_capabilities` result text for BigQuery changed. Capacity-billed projects whose
reservation is assigned in the query's own location are now detected there (before: only `US`); a
project with a reservation in a location other than the one the query runs in is priced on-demand
for that query, which is what that query is. The `US` fallback is observable (caveat and
`location_source=fallback`). Unit tests drive the real `dry_run` through both tools for each shape
(`tests/unit/test_engines_bigquery.py`, `test_tools_run_query_bounded.py`,
`test_tools_estimate_query_cost.py`); the runtime probes against the real stdio server with the
fake SDK are recorded in `scratch-pad/bq-cap-gate-2026-10/evidence/`. Ships in 0.6.0 (with #21;
the same 0.x reasoning - the release PR fixed the number for both).

## 23. `snowflake-connector-python` is capped below 5 until Snowflake ships a GA 5.x

**Context:** Issue #121. `snowflake-connector-python` 5.0.0 reached PyPI on 2026-10-15 under a plain
version number (classifier `Development Status :: 5 - Production/Stable`), while its own long
description opens with a caution that the version is a public preview, not ready for use with
production data and may be unstable; the project's GitHub repository has no release or tag for it
(latest: v4.8.0). It is an FFI-based rewrite. Any resolver satisfying the `>=4.7.1` floor picked it
from that day, including a fresh `uvx cost-guard-mcp` of 0.5.0. Against this codebase it fails
`mypy` (the base cursor types `fetchone()` as `Row | DictRow | None`) and the three pins in
`tests/unit/test_snowflake_dependency_contract.py`, which exist for exactly this case: the
`DEFAULT_CONFIGURATION` table the pins read is empty in 5.0.0 (connection options moved to a
`ConnectionConfig` dataclass; `secondary_roles` and `session_parameters` still exist there), and
`execute_async` gained its own `num_statements` parameter instead of forwarding `**kwargs` to
`execute`. The security controls those pins protect (#7's role pin via `secondary_roles`, the
single-statement pin via `session_parameters` and `num_statements`) have not been verified on the
new layout, and the unit suite with fake SDKs cannot verify them.

**Decision:** Owner decision 2026-10-19: `snowflake-connector-python>=4.7.1,<5` in `pyproject.toml`
from 0.6.0, and a Dependabot `ignore` of the connector's semver-major updates so the grouped
`python-dependencies` PR stays green. The cap is lifted only after: a GitHub release exists for the
5.x line and the PyPI caution is gone; the login-time `secondary_roles` / `session_parameters` path
and the unknown-kwarg handling are re-verified in 5.x source; the contract tests are rewritten for
both layouts; the six `fetchone()` results in `engines/snowflake.py` are narrowed so `mypy` passes
under both stubs; and the live Snowflake gate (#100) has run on 5.x. Alternatives rejected: making
0.6.0 compatible with 5.0.0 now (ships against a driver its vendor calls unstable, for a
compatibility that cannot be live-tested in this programme); leaving the range open (fresh installs
keep resolving the preview driver); a 0.5.1 hotfix carrying only the cap (two releases a few days
apart for one line - 0.6.0 is days away and carries the same line).

**Consequences:** Users on the 4.x line see no change; a user who already installed 5.0.0 moves back
to 4.8.x on upgrade. The cap is visible in the published `Requires-Dist` and recorded in
`changelog/v0.6.0.md`. The checklist for lifting it lives on #121; this entry is amended when the
range changes.
