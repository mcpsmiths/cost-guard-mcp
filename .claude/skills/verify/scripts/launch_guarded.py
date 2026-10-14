#!/usr/bin/env python3
"""Start the REAL cost-guard-mcp stdio server behind a loopback-only socket guard.

    <worktree>/.venv/bin/python .claude/skills/verify/scripts/launch_guarded.py

stdout is the MCP transport; everything else goes to stderr. Before `cost_guard_mcp` is
imported this script (1) makes every socket.connect to a non-loopback address raise OSError
(so a mis-patched path can never reach a real warehouse) and (2) optionally installs a
fake-SDK module:

    CG_VERIFY_FAKE_PATH=<dir>      added to sys.path first (e.g. the archive's harness/lead)
    CG_VERIFY_FAKE_MODULE=<name>   imported, then its install() is called (e.g. fake_backends)

No credential is set here. Dummy SNOWFLAKE_* / DATABRICKS_* values belong in a 0600 env
file passed with `stdio_rpc.py --env-file`; BigQuery needs nothing when its Client is faked.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from typing import Any


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def install_network_guard() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address: Any) -> None:
        host = address[0] if isinstance(address, tuple) and address else None
        if isinstance(host, str) and not _is_loopback(host):
            print(f"network_guard: refused connect to {host!r}", file=sys.stderr, flush=True)
            raise OSError(f"network guard: refused connect to {host!r} (loopback only)")

    def guarded_connect(self: socket.socket, address: Any) -> None:
        _check(address)
        real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return real_connect_ex(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]


def main() -> None:
    install_network_guard()
    fake_module = os.environ.get("CG_VERIFY_FAKE_MODULE")
    if fake_module:
        fake_path = os.environ.get("CG_VERIFY_FAKE_PATH")
        if fake_path and fake_path not in sys.path:
            sys.path.insert(0, fake_path)
        __import__(fake_module).install()
    if "cost_guard_mcp" in sys.modules:
        raise SystemExit("cost_guard_mcp was imported before the guard/fakes were installed")
    from cost_guard_mcp.server import main as server_main

    print(
        f"launch_guarded: pid={os.getpid()} network_guard=on fake={fake_module or 'none'} "
        f"server={sys.modules['cost_guard_mcp.server'].__file__}",
        file=sys.stderr,
        flush=True,
    )
    server_main()


if __name__ == "__main__":
    main()
