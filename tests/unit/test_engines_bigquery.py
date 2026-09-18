import base64
import concurrent.futures
from unittest.mock import MagicMock, patch

import pytest

from cost_guard_mcp.engines.bigquery import (
    check_credentials,
    dry_run,
    execute_bounded,
    is_capacity_billed,
)
from cost_guard_mcp.errors import SanitizedEngineError
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

    mock_query_job.result.assert_called_once_with(timeout=120)


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
