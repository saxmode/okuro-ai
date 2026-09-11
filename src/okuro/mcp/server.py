# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: stdio entrypoint — thin wrapper around the shared MCP registry.
# index: imports | async def main
# AGENT_HEADER_END -->
"""stdio MCP server — one subprocess per client session.

Dispatch, middleware, and tool registration live in
:mod:`okuro.mcp._registry`. This module is just the stdio transport.

The HTTP entry point is :mod:`okuro.mcp.http_server` and shares the same
registry so every module's tools are reachable identically over both
transports.
"""

import logging
import os
import signal
import subprocess

from mcp.server.stdio import stdio_server

from ._registry import build_mcp_server

logger = logging.getLogger("okuro.mcp.server")


def _reap_orphaned_servers() -> None:
    """SIGTERM sibling ``okuro.mcp.server`` processes that have been orphaned.

    A stdio MCP server is bound to the Claude process that spawned it via the
    stdin/stdout pipes. When that parent dies the server should exit on stdin
    EOF — but a server stuck inside a blocking call (historically the untimed
    in-process embedding fallback) couldn't, so orphans piled up (6 seen, aged
    up to 54h). The fail-fast fallback fixes the root cause; this reaps any
    that still slip through.

    Safe across concurrent sessions: an ACTIVE session's server always has a
    live parent (ppid != 1), so only init-reparented orphans are killed. The
    current process is never targeted. Best-effort — failures are swallowed.
    """
    self_pid = os.getpid()
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or "okuro.mcp.server" not in parts[2]:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if pid == self_pid or ppid != 1:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            logger.warning(
                "reaped orphaned okuro.mcp.server pid=%d (parent dead)", pid
            )
        except OSError:
            pass


async def main() -> None:
    _reap_orphaned_servers()
    server = build_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
