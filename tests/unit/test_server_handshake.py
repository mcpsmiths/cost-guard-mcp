"""serverInfo.version in the initialize handshake (#111, item 1).

Before this, server.py constructed MCPServer("cost-guard-mcp") with no version, and the
low-level SDK substitutes nothing ("an unversioned server reports an empty version", see
mcp/server/lowlevel/server.py), so every published wheel answered initialize with
serverInfo.version "" and a client could not tell releases apart. The version now comes from
the installed distribution metadata - what pyproject.toml declared at install - so the
handshake cannot disagree with the wheel's own version.
"""

import importlib.metadata
from unittest.mock import patch

from mcp.client import Client

from cost_guard_mcp import server
from cost_guard_mcp.server import SERVER_VERSION, _installed_version, mcp

_DISTRIBUTION = "cost-guard-mcp"


def test_server_version_constant_equals_the_installed_distribution_metadata():
    installed = importlib.metadata.version(_DISTRIBUTION)
    assert installed  # the editable install in .venv carries pyproject's version
    assert SERVER_VERSION == installed
    assert mcp.version == installed


def test_installed_version_falls_back_to_a_marked_unknown_when_metadata_is_missing():
    # A source tree run without an install (no dist-info) must still start, and must say so
    # rather than report "" or a guessed number.
    with patch.object(
        server.importlib.metadata,
        "version",
        side_effect=importlib.metadata.PackageNotFoundError(_DISTRIBUTION),
    ):
        assert _installed_version() == "0.0.0+unknown"


async def test_initialize_handshake_reports_the_package_version():
    # The real client-side handshake against the real server object, in process: the
    # serverInfo a host sees is the installed version, never the SDK default "".
    async with Client(mcp) as client:
        assert client.server_info is not None
        assert client.server_info.name == "cost-guard-mcp"
        assert client.server_info.version == importlib.metadata.version(_DISTRIBUTION)
        assert client.server_info.version != ""
