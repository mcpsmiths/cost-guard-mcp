import base64
import concurrent.futures
from fractions import Fraction
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import BadRequest, InternalServerError
from google.cloud import bigquery

from cost_guard_mcp.engines import bigquery as bigquery_engine
from cost_guard_mcp.engines.bigquery import (
    ProviderByteCapExceeded,
    check_credentials,
    cost_cap_to_max_bytes_billed,
    dry_run,
    execute_bounded,
    is_capacity_billed,
)
from cost_guard_mcp.errors import SanitizedEngineError
from cost_guard_mcp.pricing.bigquery_pricing import ON_DEMAND_USD_PER_TIB, TIB_IN_BYTES
from cost_guard_mcp.types import AccuracyTier


@patch("cost_guard_mcp.engines.bigquery.bigquery_reservation_v1")
def test_is_capacity_billed_true_when_assignment_exists(mock_reservation_module):
    mock_client = MagicMock()
    mock_client.search_all_assignments.return_value = [MagicMock()]  # one assignment found
    mock_reservation_module.ReservationServiceClient.return_value = mock_client

    assert is_capacity_billed("my-project") is True


@patch("cost_guard_mcp.engines.bigquery.bigquery_reservation_v1")
def test_is_capacity_billed_false_when_no_assignment(mock_reservation_module):
    mock_client = MagicMock()
    mock_client.search_all_assignments.return_value = []
    mock_reservation_module.ReservationServiceClient.return_value = mock_client

    assert is_capacity_billed("my-project") is False


@patch("cost_guard_mcp.engines.bigquery.bigquery_reservation_v1")
def test_is_capacity_billed_passes_assignee_filter_matching_project(mock_reservation_module):
    mock_client = MagicMock()
    mock_client.search_all_assignments.return_value = []
    mock_reservation_module.ReservationServiceClient.return_value = mock_client

    is_capacity_billed("my-project")

    _, kwargs = mock_client.search_all_assignments.call_args
    assert kwargs["request"]["query"] == "assignee=projects/my-project"


@patch("cost_guard_mcp.engines.bigquery.bigquery_reservation_v1")
def test_is_capacity_billed_rejects_invalid_project_id(mock_reservation_module):
    # is_capacity_billed is wrapped by @sanitize_exceptions, so the ValueError raised by
    # _validate_project_id surfaces to callers as SanitizedEngineError, not a bare ValueError
    # — same pattern as _validate_warehouse in engines/snowflake.py.
    with pytest.raises(SanitizedEngineError, match="Invalid GCP project ID"):
        is_capacity_billed("my project; DROP TABLE x")

    mock_reservation_module.ReservationServiceClient.assert_not_called()


def _mock_query_job(total_bytes_processed: int, accuracy: str = "PRECISE", referenced_tables=None):
    job = MagicMock()
    job.total_bytes_processed = total_bytes_processed
    job._properties = {"statistics": {"query": {"totalBytesProcessedAccuracy": accuracy}}}
    job.referenced_tables = [] if referenced_tables is None else referenced_tables
    return job


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_on_demand_precise(
    mock_bq_module,
    _mock_capacity,
):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(
        total_bytes_processed=1024**4, accuracy="PRECISE"
    )
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT * FROM t")

    assert estimate.engine == "bigquery"
    assert estimate.accuracy_tier == AccuracyTier.PRECISE
    assert estimate.estimated_bytes == 1024**4
    assert estimate.estimated_cost_usd == 6.25
    assert estimate.caveats == []


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_downgrades_accuracy_when_bigquery_reports_non_precise(
    mock_bq_module, _mock_capacity
):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(
        total_bytes_processed=500, accuracy="LOWER_BOUND"
    )
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT * FROM external_table")

    assert estimate.accuracy_tier == AccuracyTier.UPPER_BOUND
    assert any("LOWER_BOUND" in c for c in estimate.caveats)


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=True)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_capacity_billed_returns_no_dollar_estimate(mock_bq_module, _mock_capacity):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(
        total_bytes_processed=1024**4, accuracy="PRECISE"
    )
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT * FROM t")

    assert estimate.estimated_cost_usd is None
    assert estimate.estimated_bytes == 1024**4
    assert any("capacity" in c.lower() for c in estimate.caveats)


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_treats_unrecognized_accuracy_value_as_upper_bound(mock_bq_module, _mock_capacity):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(
        total_bytes_processed=500, accuracy="SOME_FUTURE_VALUE"
    )
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT * FROM t")

    assert estimate.accuracy_tier == AccuracyTier.UPPER_BOUND
    assert any("SOME_FUTURE_VALUE" in c for c in estimate.caveats)


@patch(
    "cost_guard_mcp.engines.bigquery.is_capacity_billed",
    side_effect=SanitizedEngineError("bigquery client call failed: reservation API unavailable"),
)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_degrades_gracefully_when_capacity_check_fails(mock_bq_module, _mock_capacity):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(
        total_bytes_processed=1024**4, accuracy="PRECISE"
    )
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT * FROM t")

    assert estimate.estimated_bytes == 1024**4  # byte estimate still returned
    assert estimate.estimated_cost_usd is None  # but no dollar figure - billing model unknown
    assert any("billing model" in c.lower() for c in estimate.caveats)


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_warns_about_rls_when_zero_bytes_but_tables_referenced(
    mock_bq_module, _mock_capacity
):
    # BigQuery's own documented behavior: a dry run against a row-level-security-protected
    # table always reports 0 bytes processed, by design, to prevent a side-channel. A naive
    # reading of that 0 would tag this PRECISE and $0.00 - exactly the "silently defeats the
    # cost cap" case this caveat exists to prevent.
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(
        total_bytes_processed=0,
        accuracy="PRECISE",
        referenced_tables=[MagicMock()],
    )
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT * FROM rls_protected_table")

    assert estimate.estimated_bytes == 0
    assert estimate.estimated_cost_usd == 0.0
    assert any(
        "row-level security" in c.lower() and "must not" in c.lower() for c in estimate.caveats
    )


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_no_rls_caveat_when_zero_bytes_and_no_tables_referenced(
    mock_bq_module, _mock_capacity
):
    # A trivial query like `SELECT 1` legitimately has 0 bytes processed and touches no
    # tables at all - that is not the RLS side-channel case, so no caveat should fire.
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(
        total_bytes_processed=0, accuracy="PRECISE", referenced_tables=[]
    )
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT 1")

    assert estimate.estimated_bytes == 0
    assert not any("row-level security" in c.lower() for c in estimate.caveats)


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_warns_about_remote_model_billing_for_ml_generate_text(
    mock_bq_module, _mock_capacity
):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(total_bytes_processed=500)
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT ml.generate_text(prompt) FROM t")  # lowercase - case-insensitive

    assert any("remote" in c.lower() and "cloud run" in c.lower() for c in estimate.caveats)


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_warns_about_remote_model_billing_for_remote_function(
    mock_bq_module, _mock_capacity
):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(total_bytes_processed=500)
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run(
        "create function My_Func(x INT64) returns INT64 remote with connection `proj.us.conn` "
        "options (endpoint = 'https://example.com')"
    )

    assert any("remote" in c.lower() and "cloud run" in c.lower() for c in estimate.caveats)


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_no_remote_billing_caveat_for_ordinary_query(mock_bq_module, _mock_capacity):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(total_bytes_processed=500)
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    estimate = dry_run("SELECT * FROM t WHERE created_function = 'remote work'")

    assert not any("cloud run" in c.lower() for c in estimate.caveats)


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_wraps_query_with_limit_when_max_rows_set(mock_bq_module):
    mock_client = MagicMock()
    mock_row = {"a": 1}
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter(
        [mock_row, mock_row, mock_row]
    )  # 3 rows for max_rows=2 -> capped
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    _rows, row_count, row_cap_hit = execute_bounded(
        "SELECT * FROM t", max_bytes_billed=None, max_rows=2
    )

    called_sql = mock_client.query.call_args[0][0]
    assert "LIMIT 3" in called_sql  # max_rows + 1
    assert row_count == 2  # truncated back down to max_rows for the caller
    assert row_cap_hit is True


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_no_cap_hit_when_fewer_rows_than_max(mock_bq_module):
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter([{"a": 1}])  # 1 row for max_rows=5 -> not capped
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    _rows, row_count, row_cap_hit = execute_bounded(
        "SELECT * FROM t", max_bytes_billed=None, max_rows=5
    )

    assert row_count == 1
    assert row_cap_hit is False


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_sets_maximum_bytes_billed_on_job_config(mock_bq_module):
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter([])
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client

    execute_bounded("SELECT * FROM t", max_bytes_billed=10_000_000, max_rows=None)

    mock_bq_module.QueryJobConfig.assert_called_once_with(maximum_bytes_billed=10_000_000)


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_no_cap_hit_when_rows_exactly_equal_max(mock_bq_module):
    mock_client = MagicMock()
    mock_row = {"a": 1}
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter([mock_row, mock_row])  # exactly 2 rows for max_rows=2
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    rows, row_count, row_cap_hit = execute_bounded(
        "SELECT * FROM t", max_bytes_billed=None, max_rows=2
    )

    assert row_count == 2
    assert row_cap_hit is False
    assert rows == [mock_row, mock_row]  # unmodified — nothing to truncate


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_strips_trailing_semicolon_before_wrapping(mock_bq_module):
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter([{"a": 1}])
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    execute_bounded("SELECT * FROM t;", max_bytes_billed=None, max_rows=5)

    called_sql = mock_client.query.call_args[0][0]
    assert ";)" not in called_sql
    assert called_sql == "SELECT * FROM (SELECT * FROM t) AS cost_guard_row_cap LIMIT 6"


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_check_credentials_ok_when_service_account_email_succeeds(mock_bq_module):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.get_service_account_email.return_value = "bq-sa@my-project.iam.gserviceaccount.com"
    mock_bq_module.Client.return_value = mock_client

    result = check_credentials()

    assert result.engine == "bigquery"
    assert result.ok is True
    assert "my-project" in result.detail
    assert "bq-sa@my-project.iam.gserviceaccount.com" in result.detail


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_check_credentials_reports_failure_as_data_not_a_raise(mock_bq_module):
    mock_client = MagicMock()
    mock_client.get_service_account_email.side_effect = Exception("401 Unauthorized")
    mock_bq_module.Client.return_value = mock_client

    result = check_credentials()

    assert result.engine == "bigquery"
    assert result.ok is False
    assert "401 Unauthorized" in result.detail


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_check_credentials_redacts_secrets_in_failure_detail(mock_bq_module):
    body = base64.b64decode(b"YWJjMTIzc2VjcmV0").decode()
    prefix = "-----BEGIN " + "PRIVATE KEY" + "-----"
    mock_client = MagicMock()
    mock_client.get_service_account_email.side_effect = Exception(
        "auth error, private_key=" + prefix + body
    )
    mock_bq_module.Client.return_value = mock_client

    result = check_credentials()

    assert result.ok is False
    assert body not in result.detail
    assert "REDACTED" in result.detail


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_cancels_and_raises_when_wait_times_out(mock_bq_module):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_query_job.job_id = "job-123"
    mock_query_job.result.side_effect = concurrent.futures.TimeoutError()
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    with pytest.raises(SanitizedEngineError, match="Query exceeded 120s and was cancelled"):
        execute_bounded("SELECT * FROM huge_table", max_bytes_billed=None, max_rows=None)

    mock_query_job.cancel.assert_called_once()


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_passes_timeout_to_result(mock_bq_module):
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter([])
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    execute_bounded("SELECT 1", max_bytes_billed=None, max_rows=None)

    # job_retry=None at result() too (not only at client.query): QueryJob.result resolves
    # job_retry from the client.query call when left at its default, so passing None at
    # both sites keeps 'the billed query is never re-submitted by the library' independent
    # of that internal plumbing.
    mock_query_job.result.assert_called_once_with(timeout=120, job_retry=None)


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_raise_survives_a_failed_cancel_attempt(mock_bq_module):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_query_job.job_id = "job-456"
    mock_query_job.result.side_effect = concurrent.futures.TimeoutError()
    mock_query_job.cancel.side_effect = Exception("cancel also failed")
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    with pytest.raises(SanitizedEngineError, match="Query exceeded 120s and was cancelled"):
        execute_bounded("SELECT * FROM huge_table", max_bytes_billed=None, max_rows=None)


# --- server-side byte cap derived from the dollar cap ---------------------------------------

_MIB = 1024**2


def test_cost_cap_to_max_bytes_billed_one_tib_cap_maps_to_exactly_one_tib():
    assert cost_cap_to_max_bytes_billed(6.25) == TIB_IN_BYTES


@pytest.mark.parametrize("cap", [0.0, 0.0000001])
def test_cost_cap_to_max_bytes_billed_floors_at_bigquery_minimum_billing(cap):
    # BigQuery bills a 10 MiB minimum per on-demand query; a smaller cap would fail every
    # uncached query, so the derived cap never goes below it.
    assert cost_cap_to_max_bytes_billed(cap) == 10 * _MIB


def test_cost_cap_to_max_bytes_billed_rounds_up_to_the_next_mib():
    # BigQuery rounds totalBytesBilled UP to the next MiB before comparing it with
    # maximumBytesBilled, so the derived cap must round the same way or a query estimated
    # within a MiB of the cap passes the gate and is then rejected by the provider.
    exactly_20_mib = Fraction(20 * _MIB, TIB_IN_BYTES) * Fraction(25, 4)
    one_byte_over = Fraction(20 * _MIB + 1, TIB_IN_BYTES) * Fraction(25, 4)
    assert cost_cap_to_max_bytes_billed(float(exactly_20_mib)) == 20 * _MIB
    assert cost_cap_to_max_bytes_billed(float(one_byte_over)) == 21 * _MIB


def test_cost_cap_to_max_bytes_billed_returns_an_int():
    assert type(cost_cap_to_max_bytes_billed(1.0)) is int


def _bytes_billed_limit_error(cls=BadRequest):
    return cls(
        "Query exceeded limit for bytes billed: 10485760. 20971520 or higher required.",
        errors=[{"reason": "bytesBilledLimitExceeded", "message": "x", "domain": "global"}],
    )


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_maps_provider_byte_cap_rejection_at_submit_to_structured_error(
    mock_bq_module,
):
    mock_client = MagicMock()
    mock_client.query.side_effect = _bytes_billed_limit_error(BadRequest)
    mock_bq_module.Client.return_value = mock_client

    with pytest.raises(ProviderByteCapExceeded) as excinfo:
        execute_bounded("SELECT * FROM t", max_bytes_billed=10 * _MIB, max_rows=None)

    assert excinfo.value.maximum_bytes_billed == 10 * _MIB
    assert isinstance(excinfo.value, SanitizedEngineError)  # still passes sanitize_exceptions
    assert "10485760" in str(excinfo.value)


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_maps_provider_byte_cap_rejection_at_result_to_structured_error(
    mock_bq_module,
):
    # The pinned client has no reason mapping for bytesBilledLimitExceeded, so a job that
    # fails after submission surfaces as InternalServerError (500), not BadRequest - the
    # errors[].reason is the stable signal, not the exception class.
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_query_job.result.side_effect = _bytes_billed_limit_error(InternalServerError)
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client

    with pytest.raises(ProviderByteCapExceeded):
        execute_bounded("SELECT * FROM t", max_bytes_billed=20 * _MIB, max_rows=None)


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_leaves_other_provider_errors_as_sanitized_engine_error(mock_bq_module):
    # Negative control: a BadRequest with any other reason keeps today's behaviour.
    mock_client = MagicMock()
    mock_client.query.side_effect = BadRequest(
        "Syntax error", errors=[{"reason": "invalidQuery", "message": "Syntax error"}]
    )
    mock_bq_module.Client.return_value = mock_client

    with pytest.raises(SanitizedEngineError) as excinfo:
        execute_bounded("SELEC 1", max_bytes_billed=None, max_rows=None)

    assert not isinstance(excinfo.value, ProviderByteCapExceeded)


# Regression tests for the bounded BigQuery API retry/timeout (audit fingerprint
# bigquery-readonly-paths-inherit-default-retry-600s-no-project-deadline). Without explicit
# kwargs every call site inherits google-cloud-bigquery's DEFAULT_RETRY (600 s deadline, no
# per-request timeout) and client.query() additionally wraps jobs.insert in a hard-coded
# 600 s job-level retry whenever job_retry is not None - so a read-only tool could block
# uncancellably for ~10-20 min offline and an abandoned run_query_bounded thread could still
# submit the billed job minutes after the host cancelled it.


def test_api_wait_bound_stays_within_documented_execution_bound():
    # Guards a future constant edit: the README promises tool calls settle within 120 s, so
    # the API-phase bound (job begin, service-account lookup, Reservations search) must stay
    # inside the execution watchdog, and _API_RETRY must actually carry that bound.
    assert bigquery_engine._MAX_API_WAIT_SECONDS <= bigquery_engine._MAX_EXECUTION_WAIT_SECONDS
    assert bigquery_engine._API_RETRY._timeout == bigquery_engine._MAX_API_WAIT_SECONDS


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_check_credentials_bounds_client_retry_and_timeout(mock_bq_module):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.get_service_account_email.return_value = "sa@my-project.iam.gserviceaccount.com"
    mock_bq_module.Client.return_value = mock_client

    check_credentials()

    kwargs = mock_client.get_service_account_email.call_args.kwargs
    assert kwargs["retry"] is bigquery_engine._API_RETRY
    assert kwargs["timeout"] == bigquery_engine._MAX_API_WAIT_SECONDS


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_dry_run_bounds_client_retry_and_disables_job_retry(mock_bq_module, _mock_capacity):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.query.return_value = _mock_query_job(total_bytes_processed=500)
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    dry_run("SELECT * FROM t")

    kwargs = mock_client.query.call_args.kwargs
    assert kwargs["retry"] is bigquery_engine._API_RETRY
    assert kwargs["timeout"] == bigquery_engine._MAX_API_WAIT_SECONDS
    # job_retry=None is mandatory, not just "a bounded job retry": a DEFAULT_JOB_RETRY with a
    # shorter timeout still makes the library run its hard-coded 600 s jobs.insert wrapper.
    assert "job_retry" in kwargs
    assert kwargs["job_retry"] is None


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_bounds_job_begin_retry_and_disables_job_retry(mock_bq_module):
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter([])
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    execute_bounded("SELECT 1", max_bytes_billed=None, max_rows=None)

    kwargs = mock_client.query.call_args.kwargs
    assert kwargs["retry"] is bigquery_engine._API_RETRY
    assert kwargs["timeout"] == bigquery_engine._MAX_API_WAIT_SECONDS
    assert "job_retry" in kwargs
    assert kwargs["job_retry"] is None


@patch("cost_guard_mcp.engines.bigquery.bigquery_reservation_v1")
def test_is_capacity_billed_bounds_reservation_search_timeout(mock_reservation_module):
    mock_client = MagicMock()
    mock_client.search_all_assignments.return_value = []
    mock_reservation_module.ReservationServiceClient.return_value = mock_client

    is_capacity_billed("my-project")

    kwargs = mock_client.search_all_assignments.call_args.kwargs
    assert kwargs["timeout"] == bigquery_engine._MAX_API_WAIT_SECONDS


# Regression tests for the provider-side execution deadline (audit fingerprint
# client-side-only-query-deadline-orphaned-on-process-exit). The client-side watchdog in
# result(timeout=120) only exists while this process's worker thread is alive; jobTimeoutMs
# makes BigQuery itself enforce the same 120 s bound after a SIGTERM/SIGKILL or an abandoned
# thread. Both tests use the real QueryJobConfig class so the assertion is on the actual
# wire field, not on a MagicMock attribute.


@pytest.mark.parametrize(
    ("max_bytes_billed", "expected_query_fields"),
    [(None, {}), (1_000_000, {"maximumBytesBilled": "1000000"})],
    ids=["uncapped_branch", "byte_capped_branch"],
)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_sets_job_timeout_ms_equal_to_the_watchdog_on_both_branches(
    mock_bq_module, max_bytes_billed, expected_query_fields
):
    mock_bq_module.QueryJobConfig = bigquery.QueryJobConfig  # the real class
    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter([])
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client

    execute_bounded("SELECT 1", max_bytes_billed=max_bytes_billed, max_rows=None)

    job_config = mock_client.query.call_args.kwargs["job_config"]
    assert isinstance(job_config, bigquery.QueryJobConfig)
    # The library stores jobTimeoutMs as its API string form; the wire field is what BigQuery
    # enforces, so assert on the full API repr rather than on a Python attribute.
    expected_ms = str(bigquery_engine._MAX_EXECUTION_WAIT_SECONDS * 1000)
    assert expected_ms == "120000"
    assert job_config.to_api_repr() == {"query": expected_query_fields, "jobTimeoutMs": expected_ms}


# --- runtime-verification follow-ups (PR #86 adversarial probes P6/P9/P13) -----------------

_INT64_MAX = 2**63 - 1


@pytest.mark.parametrize("cap", [1e200, 1e300, 1e308])
def test_cost_cap_to_max_bytes_billed_clamps_huge_finite_caps_to_int64(cap):
    # maximumBytesBilled is an int64 on the BigQuery API. A finite cap large enough to
    # overflow float->int (>= ~1e297) crashed with OverflowError, and caps between
    # ~$5.24e7 and that produced 200-digit values the real API would reject with a 400.
    # Both now clamp to int64 max, which BigQuery accepts and which is "no effective cap".
    assert cost_cap_to_max_bytes_billed(cap) == _INT64_MAX


def test_cost_cap_to_max_bytes_billed_never_exceeds_int64_at_the_boundary():
    boundary_usd = _INT64_MAX / TIB_IN_BYTES * ON_DEMAND_USD_PER_TIB
    assert cost_cap_to_max_bytes_billed(boundary_usd) <= _INT64_MAX
    assert cost_cap_to_max_bytes_billed(boundary_usd * 0.5) < _INT64_MAX


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_execute_bounded_cancel_round_trip_is_bounded_like_every_other_api_call(mock_bq_module):
    # The 120 s watchdog's QueryJob.cancel() is a BigQuery API round-trip too; without an
    # explicit retry/timeout it inherits the library's 600 s DEFAULT_RETRY and can pin the
    # worker thread (and its in-flight slot) for ten more minutes while Google is unreachable.
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_query_job.job_id = "job-789"
    mock_query_job.result.side_effect = concurrent.futures.TimeoutError()
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    with pytest.raises(SanitizedEngineError, match="Query exceeded 120s and was cancelled"):
        execute_bounded("SELECT * FROM huge_table", max_bytes_billed=None, max_rows=None)

    from cost_guard_mcp.engines.bigquery import _API_RETRY, _MAX_API_WAIT_SECONDS

    mock_query_job.cancel.assert_called_once_with(retry=_API_RETRY, timeout=_MAX_API_WAIT_SECONDS)


def test_provider_byte_cap_exceeded_without_an_attached_cap_blames_the_project_default():
    # Reachable when the project/org has a default maximumBytesBilled and this server
    # attached none (no caps, or an infinite cost cap): the message must not claim a
    # "limit of None bytes attached to the job".
    message = str(ProviderByteCapExceeded(None))
    assert "None" not in message
    assert "outside this server" in message
    assert "Nothing was billed" in message
    attached = str(ProviderByteCapExceeded(10 * _MIB))
    assert "10485760" in attached
