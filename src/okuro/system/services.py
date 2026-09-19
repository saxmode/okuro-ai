# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Service health check collector.
# index: imports | def _okuro_default_services | def get_service_health
# AGENT_HEADER_END -->
"""Service health check collector."""

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Union

log = logging.getLogger("okuro.system.services")

# Default service definitions. Populated lazily from port_registry +
# daemon runtime config — see _okuro_default_services() below. The MCP
# tool schema accepts a list of names (e.g. ["okuro-embed"]); resolving
# those names without a registry would yield nothing, so the registry
# carries the canonical port + health-path for every okuro service.
DEFAULT_SERVICES: dict[str, dict] = {}


def _okuro_default_services() -> dict[str, dict]:
    """Build the canonical okuro service registry on demand.

    Returns ``{name: {port, health, category}}``. Tolerant of pieces being
    unavailable: if the daemon HTTP port hasn't been allocated yet (no
    config file), that entry is omitted rather than raising. orchestrator
    and embed use static port_registry defaults so they always appear.
    """
    out: dict[str, dict] = {}

    try:
        from okuro.system.port_registry import orchestrator_port, embed_port
        out["okuro-orchestrator"] = {
            "port": orchestrator_port(),
            # /api/health, not /health: the SPA owns /health as a client-side
            # route, so the old path was answered by the SPA catch-all — a
            # 200 that proves a static file exists, not that the API is up.
            "health": "/api/health",
            "category": "core",
            "service": "orchestrator",
        }
        out["okuro-embed"] = {
            "port": embed_port(),
            "health": "/health",
            "category": "core",
            "service": "embed",
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("port_registry unavailable (%s)", exc)

    # Daemon MCP HTTP port is allocated dynamically on first boot and
    # persisted to ~/.okuro/mcp.yaml. Read it without forcing a fresh
    # allocation — _read_config returns {} if the file doesn't exist yet.
    try:
        from okuro.mcp.http_server import _read_config
        cfg = _read_config()
        port = (cfg.get("mcp", {}) or {}).get("http", {}).get("port")
        if port:
            out["okuro-daemon"] = {
                "port": int(port),
                "health": "/health",
                "category": "core",
                "service": "daemon",
            }
    except Exception as exc:  # noqa: BLE001
        log.debug("daemon mcp config unavailable (%s)", exc)

    return out


def _annotate_owner(entry: dict, raw: bytes, service: Optional[str], port: int) -> None:
    """Stamp ``install`` / ``foreign`` onto a health entry.

    An entry answered by another okuro install is downgraded to
    ``unhealthy`` with the one-line reason: from the caller's point of
    view THIS install's service is not running, whatever the socket says.
    Non-okuro services (the caller's own dict form, no ``service`` key)
    are left exactly as they were.
    """
    if not service:
        return
    try:
        from okuro.system.install_identity import (
            describe_install,
            identity_from_payload,
            is_own_install,
            socket_owner_uid,
            this_install,
        )

        payload = json.loads(raw.decode("utf-8", "replace")) if raw else None
        identity = identity_from_payload(payload)
        mine = this_install()
        if identity is not None:
            if is_own_install(identity, mine):
                entry["install"] = "this"
                return
            entry["install"] = describe_install(identity)
            entry["foreign"] = True
        else:
            uid = socket_owner_uid(port)
            if uid is None or uid == int(mine["uid"]):
                entry["install"] = "this" if uid is not None else "unknown"
                return
            entry["install"] = describe_install(None, uid)
            entry["foreign"] = True
        entry["status"] = "unhealthy"
        entry["error"] = (
            f"port {port} is held by another okuro install: {entry['install']}"
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("owner annotation failed for port %s (%s)", port, exc)


def get_service_health(
    services: Union[list[str], dict[str, dict], None] = None,
    filter_names: Optional[list[str]] = None,
) -> dict:
    """Check health of okuro services.

    Args:
        services:
            - ``None``: check every service in the built-in registry.
            - ``list[str]``: filter the built-in registry to these names.
            - ``dict[str, dict]``: use these definitions directly
              (``{name: {port, health, category}}``). Power-user path —
              the MCP tool only exposes the list form.
        filter_names: Legacy keyword still honored when ``services`` is a
            dict — filter that dict to these names. Ignored otherwise.
    """
    if isinstance(services, list):
        svc_map = _okuro_default_services()
        names_in = [n for n in services if isinstance(n, str)]
        filter_names = names_in or None
    elif isinstance(services, dict):
        svc_map = services
    else:
        svc_map = _okuro_default_services()

    if not svc_map:
        return {
            "services": [],
            "summary": {"total": 0, "healthy": 0, "unhealthy": 0},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    names = filter_names or list(svc_map.keys())
    names = [n for n in names if n in svc_map]

    results = []
    for name in names:
        cfg = svc_map[name]
        port = cfg.get("port", 0)
        health_path = cfg.get("health", "/health")
        url = f"http://localhost:{port}{health_path}"

        entry: dict = {
            "name": name,
            "port": port,
            "category": cfg.get("category", "other"),
            "url": f"http://localhost:{port}",
        }

        try:
            t0 = time.time()
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                elapsed = (time.time() - t0) * 1000
                raw = resp.read(65536)
                entry["status"] = "healthy" if resp.status < 400 else "unhealthy"
                entry["response_time_ms"] = round(elapsed, 1)
                entry["http_status"] = resp.status
            # VERIFY BEFORE TRUST. A 200 says something is listening, not that
            # it is OURS. Without this, a second install's services report as
            # this install's — healthy, green, and answering for someone else.
            _annotate_owner(entry, raw, cfg.get("service"), port)
        except Exception as e:
            entry["status"] = "unhealthy"
            entry["error"] = str(type(e).__name__)

        results.append(entry)

    healthy = sum(1 for s in results if s["status"] == "healthy")
    return {
        "services": results,
        "summary": {"total": len(results), "healthy": healthy, "unhealthy": len(results) - healthy},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
