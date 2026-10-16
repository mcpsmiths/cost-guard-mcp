"""The rules `engines.bigquery.dry_run` applies to a dry-run response, with no SDK call in them:
how BigQuery's own `totalBytesProcessedAccuracy` maps to an `AccuracyTier`, which location the
billing-model check is made for, and the text heuristic for remote billing.

Split out of `engines/bigquery.py` in #96 to keep that module under the 500-line guideline. The
functions that call the SDK (`dry_run`, `is_capacity_billed`, ...) stay there, so the tests keep
patching `cost_guard_mcp.engines.bigquery.bigquery` and `.is_capacity_billed`.
"""

import re

from google.cloud import bigquery

from cost_guard_mcp.types import AccuracyTier

# BigQuery location ids: the multi-regions "US"/"EU" and regions such as "us-central1" or
# "northamerica-northeast1". The dry-run job's location is service-returned text that flows into
# the hand-built Reservations parent - validate it like the project id (DECISIONS.md #6).
_BQ_LOCATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]{0,63}$")
# The pre-#96 behaviour, kept for library callers that pass no location and for a dry-run job
# that reports none (dry_run then says so in a caveat).
DEFAULT_RESERVATION_LOCATION = "US"


def validate_location(location: str) -> str:
    if not _BQ_LOCATION_RE.fullmatch(location):
        raise ValueError(f"Invalid BigQuery location: {location!r}")
    return location


# Google's definitions (REST JobStatistics2.totalBytesProcessedAccuracy): PRECISE "estimate is
# precise"; LOWER_BOUND "estimate is lower bound of what the query would cost"; UPPER_BOUND
# "estimate is upper bound of what the query would cost"; UNKNOWN "accuracy of the estimate is
# unknown". A floor is not a cap: LOWER_BOUND is HEURISTIC - the one tier that promises no bound
# in either direction - and dry_run derives no dollar figure from it (#96, DECISIONS.md #22).
# UNKNOWN keeps the conservative UPPER_BOUND label and its figure (nothing shows it to be a
# floor), as does any value this table does not know.
BQ_ACCURACY_TO_TIER = {
    "PRECISE": AccuracyTier.PRECISE,
    "LOWER_BOUND": AccuracyTier.HEURISTIC,
    "UPPER_BOUND": AccuracyTier.UPPER_BOUND,
    "UNKNOWN": AccuracyTier.UPPER_BOUND,
}
BQ_LOWER_BOUND = "LOWER_BOUND"
USE_BYTE_CAP_INSTEAD = (
    "Bound the run with max_bytes_billed instead; BigQuery enforces it server-side as "
    "maximumBytesBilled."
)


def job_location(query_job: bigquery.QueryJob) -> tuple[str, str]:
    """The location the dry-run job ran in (jobReference.location) and where the value came
    from: (location, "job"), or (DEFAULT_RESERVATION_LOCATION, "fallback") when the service
    reported none - QueryJob.location is None then; anything but a non-empty str is treated the
    same way."""
    location = query_job.location
    if isinstance(location, str) and location:
        return location, "job"
    return DEFAULT_RESERVATION_LOCATION, "fallback"


def references_remote_billing(sql: str) -> bool:
    """Conservative textual heuristic for queries that likely trigger BigQuery remote
    functions or BigQuery ML remote-model inference (`ML.GENERATE_TEXT`), both of which
    incur separate Cloud Run/Vertex AI billing that a byte-based dollar estimate cannot
    see.

    Deliberately unconditional and text-based rather than derived from the dry-run
    response's `statistics.query.referencedRoutines` field (researched 2026-09-18):
    that field is real and documented (BigQuery's own v2 Discovery Document -
    googleapis.com/discovery/v1/apis/bigquery/v2/rest - `JobStatistics2.referencedRoutines`,
    an array of `{projectId, datasetId, routineId}`), but no Google source states whether
    it populates on a *dry run* specifically, only that `referencedTables` (its sibling
    field, read by dry_run for the RLS caveat) does. More importantly,
    `referencedRoutines` can NEVER detect `ML.GENERATE_TEXT` at all - BQML remote-model
    inference references a `ModelReference`, and `JobStatistics2` has no
    `referencedModels`-equivalent field - so a structured read could only ever supplement,
    never replace, this text check for the remote-function half. Next concrete step for a
    maintainer with real BigQuery credentials: run a dry-run query that invokes an
    existing remote function (no `CREATE FUNCTION` DDL) and inspect
    `query_job._properties["statistics"]["query"].get("referencedRoutines")` - if it's
    populated, add a supplemental read there (each returned routine still needs a
    `routines.get` call, since the reference alone carries no `remoteFunctionOptions`),
    OR'd with this text check rather than replacing it.
    """
    upper_sql = sql.upper()
    if "ML.GENERATE_TEXT" in upper_sql:
        return True
    return "CREATE FUNCTION" in upper_sql and "REMOTE" in upper_sql
