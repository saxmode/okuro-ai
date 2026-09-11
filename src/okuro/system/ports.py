# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Port status collector (cross-platform via psutil).
# index: imports | def get_port_status | def _listening_ports
# AGENT_HEADER_END -->
"""Port status collector (cross-platform via psutil)."""

from datetime import datetime, timezone
from typing import Optional

import psutil


def get_port_status(
    check_port: Optional[int] = None,
    range_start: int = 10000,
    range_end: int = 10100,
) -> dict:
    """Get port status and availability.

    Uses psutil.net_connections() — works on Linux, macOS, and Windows.
    On macOS, may return partial info without elevated privileges; that's
    fine for this use case (we only need listening ports).
    """
    try:
        listening = _listening_ports()

        result: dict = {"timestamp": datetime.now(timezone.utc).isoformat()}

        if check_port is not None:
            result["checked_port"] = {
                "port": check_port,
                "available": check_port not in listening,
            }

        available = [
            p
            for p in range(range_start, min(range_end + 1, range_start + 100))
            if p not in listening
        ]
        result["available_range"] = available[:20]
        result["next_available"] = available[0] if available else None

        return result

    except (psutil.AccessDenied, PermissionError) as e:
        return {
            "error": f"insufficient permissions to enumerate sockets: {e}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def _listening_ports() -> set[int]:
    """Return the set of TCP+UDP ports in LISTEN state (or bound for UDP)."""
    ports: set[int] = set()
    for conn in psutil.net_connections(kind="inet"):
        # TCP listening sockets report status == 'LISTEN'.
        # UDP has no listen concept; any bound local port counts as "in use".
        if conn.type == 1:  # SOCK_STREAM (TCP)
            if conn.status != psutil.CONN_LISTEN:
                continue
        if conn.laddr:
            ports.add(conn.laddr.port)
    return ports
