# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Pick, persist and re-apply THIS install's service ports.
# index:
#   imports
#   def port_is_free
#   def pick_install_ports
#   def write_ports
#   def ensure_install_ports
#   def reassign_ports
# AGENT_HEADER_END -->
"""Pick, persist and re-apply this install's service ports.

``port_registry`` READS ports (env → config → default). This module
WRITES them, and re-applies a written set to everything that carries a
copy: the unit files and the MCP client configs. Those three had to move
together — a config that says 13403 while the unit still exports 13333
is a collision that survives the fix.

A FRESH install allocates through :func:`ensure_install_ports`: it keeps
the canonical 13333/13334 when they are free (so every existing
single-install host is unchanged) and drops to
:data:`~okuro.system.port_registry.ALT_INSTALL_RANGE` when they are not.
An EXISTING install never has its declared ports changed implicitly —
only ``okuro ports reassign`` re-picks, because Caddy vhosts and client
configs are pinned to the ports an install already published.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from typing import Optional

from okuro.system.port_registry import (
    ALT_INSTALL_RANGE,
    EMBED_PORT_DEFAULT,
    MCP_HTTP_RANGE,
    ORCHESTRATOR_PORT_DEFAULT,
    config_path,
    read_config,
)

log = logging.getLogger("okuro.system.port_assign")

SERVICE_KEYS = ("orchestrator", "embed", "daemon")


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Bind-test ``port``. True when nothing holds it.

    Deliberately a real bind rather than a connect: a port held by a
    listener in another uid refuses nothing on connect and everything on
    bind, and bind is the operation the service will actually perform.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _first_free(candidates, taken: set[int], host: str = "127.0.0.1") -> Optional[int]:
    for port in candidates:
        if port in taken:
            continue
        if port_is_free(port, host):
            return port
    return None


def pick_install_ports(host: str = "127.0.0.1") -> dict:
    """Choose a free {orchestrator, embed, daemon} set for this install.

    Canonical pair first, so nothing changes on a host with one install.
    Otherwise an adjacent free pair in the alternate band — adjacent
    because an operator reading ``ss`` output should be able to see at a
    glance that two ports belong to the same install.

    Raises ``RuntimeError`` when no set can be found; a silent fallback
    to the busy default is exactly the failure this module exists to
    stop.
    """
    taken: set[int] = set()

    orchestrator = None
    embed = None
    if port_is_free(ORCHESTRATOR_PORT_DEFAULT, host) and port_is_free(
        EMBED_PORT_DEFAULT, host
    ):
        orchestrator, embed = ORCHESTRATOR_PORT_DEFAULT, EMBED_PORT_DEFAULT
    else:
        for base in range(ALT_INSTALL_RANGE.start, ALT_INSTALL_RANGE.stop - 1, 2):
            if port_is_free(base, host) and port_is_free(base + 1, host):
                orchestrator, embed = base, base + 1
                break
    if orchestrator is None or embed is None:
        raise RuntimeError(
            "no free orchestrator/embed port pair "
            f"(tried {ORCHESTRATOR_PORT_DEFAULT}/{EMBED_PORT_DEFAULT} and "
            f"{ALT_INSTALL_RANGE.start}-{ALT_INSTALL_RANGE.stop - 1})"
        )
    taken.update({orchestrator, embed})

    daemon = _first_free(MCP_HTTP_RANGE, taken, host)
    if daemon is None:
        raise RuntimeError(f"no free daemon HTTP-MCP port in {MCP_HTTP_RANGE}")

    return {"orchestrator": orchestrator, "embed": embed, "daemon": daemon}


def write_ports(ports: dict, path: Optional[Path] = None) -> Path:
    """Persist ``ports`` into this install's config.yaml.

    ``ports.orchestrator`` / ``ports.embed`` are the new block;
    ``mcp.http.port`` is the daemon's pre-existing key and keeps its
    spelling so the daemon, ``mcp_config._http_url`` and every existing
    reader keep working unchanged.
    """
    import yaml

    path = path or config_path()
    cfg = read_config() if path == config_path() else (
        yaml.safe_load(path.read_text()) or {} if path.is_file() else {}
    )

    block = cfg.setdefault("ports", {})
    if not isinstance(block, dict):
        block = {}
        cfg["ports"] = block
    if ports.get("orchestrator"):
        block["orchestrator"] = int(ports["orchestrator"])
    if ports.get("embed"):
        block["embed"] = int(ports["embed"])
    if ports.get("daemon"):
        cfg.setdefault("mcp", {}).setdefault("http", {})["port"] = int(ports["daemon"])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    log.info("wrote install ports to %s: %s", path, ports)
    return path


def ensure_install_ports(host: str = "127.0.0.1") -> dict:
    """Ports for a FRESH install — allocate and persist on first run.

    Idempotent. When the config already declares a port, that port is
    kept: an install that has published 13333 to Caddy and to five MCP
    client configs does not get silently moved because something else
    happened to be listening during a re-run.
    """
    cfg = read_config()
    declared = cfg.get("ports") if isinstance(cfg.get("ports"), dict) else {}
    mcp_declared = ((cfg.get("mcp") or {}).get("http") or {}).get("port")

    have = {
        "orchestrator": declared.get("orchestrator"),
        "embed": declared.get("embed"),
        "daemon": mcp_declared,
    }
    if all(have.values()):
        return {k: int(v) for k, v in have.items()}

    picked = pick_install_ports(host)
    merged = {k: int(have[k]) if have.get(k) else picked[k] for k in SERVICE_KEYS}
    write_ports(merged)
    return merged


def _reinstall_units(ports: dict) -> dict:
    """Regenerate this install's unit files so they carry ``ports``."""
    from okuro.system.install import install_all_services

    return install_all_services(start=False)


def _rewrite_client_configs(base_home: Optional[Path] = None) -> dict:
    """Re-point every MCP client config at the new daemon port."""
    from okuro.cli.mcp_config import write_all_configs

    return write_all_configs(base_home=base_home)


def reassign_ports(
    host: str = "127.0.0.1",
    apply_units: bool = True,
    apply_clients: bool = True,
    base_home: Optional[Path] = None,
) -> dict:
    """Re-pick free ports for THIS install and re-apply them everywhere.

    Returns ``{"before", "after", "config", "units", "clients",
    "restart"}``. ``restart`` is the ordered list of units the operator
    must restart — this function never restarts anything itself, because
    the install whose ports are being moved is frequently the one the
    operator is currently talking through.
    """
    from okuro.system.port_registry import service_ports

    before = service_ports()
    after = pick_install_ports(host)
    path = write_ports(after)

    units: dict = {}
    clients: dict = {}
    if apply_units:
        try:
            units = _reinstall_units(after)
        except Exception as exc:  # noqa: BLE001
            log.exception("port reassign: unit rewrite failed")
            units = {"_error": str(exc)}
    if apply_clients:
        try:
            clients = _rewrite_client_configs(base_home)
        except Exception as exc:  # noqa: BLE001
            log.exception("port reassign: client config rewrite failed")
            clients = {"_error": str(exc)}

    return {
        "before": before,
        "after": after,
        "config": str(path),
        "units": units,
        "clients": clients,
        # embed first, then daemon, then orchestrator — the order
        # okuro.system.backup._START_ORDER already uses.
        "restart": ["okuro-embed", "okuro-daemon", "okuro-orchestrator"],
    }
