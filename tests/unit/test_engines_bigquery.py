from unittest.mock import MagicMock, patch

from cost_guard_mcp.engines.bigquery import is_capacity_billed


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
