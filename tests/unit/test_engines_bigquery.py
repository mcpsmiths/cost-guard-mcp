from unittest.mock import MagicMock, patch

from cost_guard_mcp.engines.bigquery import dry_run, execute_bounded, is_capacity_billed
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


def _mock_query_job(total_bytes_processed: int, accuracy: str = "PRECISE"):
    job = MagicMock()
    job.total_bytes_processed = total_bytes_processed
    job._properties = {"statistics": {"query": {"totalBytesProcessedAccuracy": accuracy}}}
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
