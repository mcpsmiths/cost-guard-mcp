import asyncio
from unittest.mock import MagicMock, patch

from cost_guard_mcp.server import mcp


def test_server_is_named_cost_guard_mcp():
    assert mcp.name == "cost-guard-mcp"


def test_server_lists_all_four_registered_tools():
    # Regression test: the original version of this test only asserted
    # isinstance(tools, list), which passes whether 0 or 4 tools are registered - it
    # provided zero signal on actual tool registration. This asserts the real, current
    # tool set by name.
    tools = asyncio.get_event_loop().run_until_complete(mcp.list_tools())
    tool_names = {t.name for t in tools}
    assert tool_names == {
        "check_credentials",
        "describe_engine_capabilities",
        "estimate_query_cost",
        "run_query_bounded",
    }


def _call_tool(name, arguments):
    return asyncio.get_event_loop().run_until_complete(mcp.call_tool(name, arguments))


def test_call_tool_describe_engine_capabilities_end_to_end():
    # Exercises the real @mcp.tool()-registered function (annotations + as_tool_error +
    # the actual implementation) through the SDK's own call_tool dispatch, not just the
    # underlying tools/*.py function directly - proves the full decorator stack and
    # argument passing actually work together.
    result = _call_tool("describe_engine_capabilities", {"engine": "bigquery"})
    assert result.structured_content["engine"] == "bigquery"
    assert result.structured_content["default_accuracy_tier"] == "PRECISE"


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_call_tool_estimate_query_cost_end_to_end(mock_bq_module, _mock_capacity):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_query_job = MagicMock()
    mock_query_job.total_bytes_processed = 100
    mock_query_job._properties = {
        "statistics": {"query": {"totalBytesProcessedAccuracy": "PRECISE"}}
    }
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    result = _call_tool("estimate_query_cost", {"engine": "bigquery", "sql": "SELECT 1"})

    assert result.structured_content["engine"] == "bigquery"
    assert result.structured_content["accuracy_tier"] == "PRECISE"


@patch("cost_guard_mcp.engines.bigquery.is_capacity_billed", return_value=False)
@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_call_tool_run_query_bounded_end_to_end(mock_bq_module, _mock_capacity):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_query_job = MagicMock()
    mock_query_job.total_bytes_processed = 100
    mock_query_job._properties = {
        "statistics": {"query": {"totalBytesProcessedAccuracy": "PRECISE"}}
    }
    mock_result = MagicMock()
    mock_result.__iter__.return_value = iter([{"a": 1}])
    mock_query_job.result.return_value = mock_result
    mock_client.query.return_value = mock_query_job
    mock_bq_module.Client.return_value = mock_client
    mock_bq_module.QueryJobConfig.return_value = MagicMock()

    result = _call_tool(
        "run_query_bounded", {"engine": "bigquery", "sql": "SELECT 1", "max_rows": 5}
    )

    assert result.structured_content["status"] == "ok"
    assert result.structured_content["row_count"] == 1


@patch("cost_guard_mcp.server._bootstrap_opentelemetry")
@patch("cost_guard_mcp.server.mcp")
def test_main_calls_mcp_run(mock_mcp, mock_bootstrap):
    from cost_guard_mcp.server import main

    main()
    mock_bootstrap.assert_called_once()
    mock_mcp.run.assert_called_once()


def test_bootstrap_opentelemetry_is_a_noop_without_the_endpoint_env_var(monkeypatch):
    # No OTEL_EXPORTER_OTLP_ENDPOINT set - must return without importing/touching the
    # opentelemetry SDK at all, so a deployment that never opts in never needs it installed.
    from cost_guard_mcp.server import _bootstrap_opentelemetry

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    with patch("opentelemetry.trace.set_tracer_provider") as mock_set_provider:
        _bootstrap_opentelemetry()

    mock_set_provider.assert_not_called()


def test_bootstrap_opentelemetry_registers_a_tracer_provider_when_endpoint_is_set(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider

    from cost_guard_mcp.server import _bootstrap_opentelemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    with patch("opentelemetry.trace.set_tracer_provider") as mock_set_provider:
        _bootstrap_opentelemetry()

    mock_set_provider.assert_called_once()
    registered_provider = mock_set_provider.call_args.args[0]
    assert isinstance(registered_provider, TracerProvider)


@patch("cost_guard_mcp.engines.bigquery.bigquery")
def test_call_tool_check_credentials_end_to_end(mock_bq_module):
    mock_client = MagicMock()
    mock_client.project = "my-project"
    mock_client.get_service_account_email.return_value = "sa@my-project.iam.gserviceaccount.com"
    mock_bq_module.Client.return_value = mock_client

    result = _call_tool("check_credentials", {"engine": "bigquery"})

    assert result.structured_content["engine"] == "bigquery"
    assert result.structured_content["ok"] is True
