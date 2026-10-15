"""The third-party logger pins and the redacting filter at the real surface (#95).

Spawns the real stdio server as a subprocess (the pattern of test_server_cancellation.py) with
the BigQuery client library mocked INSIDE that subprocess so that the service-account lookup
behind check_credentials logs one INFO and one secret-shaped WARNING record through a
`google.cloud.bigquery` logger - exactly what a real client library does on the root
StreamHandler the mcp SDK installs. The server's stderr is captured and must show the WARNING
redacted, no INFO, the dummy secret nowhere, and the project's own INFO outcome record intact.
No network, no credentials.
"""

import base64
import sys
from pathlib import Path

from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

# "sa-" + a fixed opaque string; base64 so no secret-shaped literal appears in this file.
_DUMMY_SECRET = base64.b64decode(b"c2EtbG9nLWh5Z2llbmUtZHVtbXktc2VjcmV0").decode()
_INFO_TEXT = "google client info chatter that must stay off stderr"

_LOGGING_SERVER_SCRIPT = f"""
import logging
from unittest.mock import MagicMock

from cost_guard_mcp.engines import bigquery as bigquery_engine

_google_logger = logging.getLogger("google.cloud.bigquery")


def _lookup_with_library_logging(*args, **kwargs):
    _google_logger.info({_INFO_TEXT!r})
    _google_logger.warning("refreshing credentials, " + "pass" + "word=%s", {_DUMMY_SECRET!r})
    return "sa@test-project.iam.gserviceaccount.com"


_mock_client = MagicMock()
_mock_client.project = "test-project"
_mock_client.get_service_account_email.side_effect = _lookup_with_library_logging
bigquery_engine.bigquery.Client = MagicMock(return_value=_mock_client)

from cost_guard_mcp.server import main

main()
"""


async def test_real_server_stderr_carries_the_redacted_warning_and_no_third_party_info(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "logging_bigquery_server.py"
    script_path.write_text(_LOGGING_SERVER_SCRIPT)
    stderr_path = tmp_path / "server.stderr.log"
    params = StdioServerParameters(command=sys.executable, args=[str(script_path)])

    with stderr_path.open("w", encoding="utf-8") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("check_credentials", {"engine": "bigquery"})

    assert result.structured_content["ok"] is True
    stderr = stderr_path.read_text(encoding="utf-8")
    assert _DUMMY_SECRET not in stderr
    assert _INFO_TEXT not in stderr
    assert "refreshing credentials, password=***REDACTED***" in stderr
    assert "tool=check_credentials outcome=success" in stderr
