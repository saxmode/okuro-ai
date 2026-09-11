# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Canonical port allocation for okuro long-running services.
# index: imports | constants | def orchestrator_port | def embed_port | def wizard_port_preferred | def wizard_fallback_range | def embed_url
# AGENT_HEADER_END -->
"""Canonical port allocation for okuro long-running services.

Single source of truth — no other module should hardcode an okuro port.
Every consumer (CLI launcher, plist generator, embed server/client,
doctor diagnostics, services API) imports the helpers below.

Allocation (LAN-only, 127.0.0.1 binds):

    3090–3099   daemon HTTP MCP server  (per system/conventions.yaml)
    13333       persistent orchestrator API + SPA  (OKURO_PORT)
    13334       embedding service                  (OKURO_EMBED_PORT)
    13300–13332 local inference tenants (llama-server / vLLM / ComfyUI, dynamic)
    13335       wizard (transient, only during ``okuro init``)
    13336–13399 wizard fallback range when 13335 is busy

Why three separate ports rather than one shared port:

    The wizard runs the SAME FastAPI app as the persistent orchestrator,
    but it can't run on the orchestrator port because /complete starts
    the persistent service immediately and "address in use" would kill
    that startup. So wizard takes its own port.

    The embed service must NOT collide with the wizard either — pre-fix,
    wizard preferred=13334 stomped on the embed plist's port and every
    embed lookup returned HTTP 405 because the wizard was answering
    instead of the embed app.

Every helper honors an env-var override (``OKURO_PORT``,
``OKURO_EMBED_PORT``, ``OKURO_WIZARD_PORT``, ``OKURO_EMBED_URL``) so
operators can move things if they have to.
"""

from __future__ import annotations

import os
import socket

ORCHESTRATOR_PORT_DEFAULT = 13333
EMBED_PORT_DEFAULT = 13334
WIZARD_PORT_DEFAULT = 13335
WIZARD_FALLBACK_RANGE = (13336, 13399)
# Dynamically-launched local inference engines (llama-server, vLLM, ComfyUI)
# bind here so every okuro-owned engine lands in the 133xx band, below the
# orchestrator (13333) and clear of embed/wizard — predictable for LAN policy.
LOCAL_INFERENCE_RANGE = range(13300, 13333)


def pick_free_port(port_range: range = LOCAL_INFERENCE_RANGE,
                   host: str = "127.0.0.1", *, fallback_ephemeral: bool = True) -> int:
    """First free port in ``port_range`` (bind-tested). Falls back to an OS
    ephemeral port if the band is exhausted, so a launch is never blocked purely
    by a full band. Set ``fallback_ephemeral=False`` to raise instead."""
    for port in port_range:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    if not fallback_ephemeral:
        raise RuntimeError(f"no free port in {port_range}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return s.getsockname()[1]


def _port_from_env(var: str, default: int) -> int:
    """Read a port override from the environment, falling back to ``default``.

    Every override below used to be a bare ``int(os.environ.get(...))``, which
    raises on anything unparseable. ``VAR= okuro ...`` is the ordinary shell way
    to say "no override", but it sets the variable to an empty string rather
    than unsetting it, so that spelling crashed with a bare
    ``invalid literal for int() with base 10: ''`` — no mention of which
    variable, from a stack deep inside startup. Treat empty/whitespace as unset.

    A genuinely malformed value still raises: silently falling back would bind a
    port the user did not ask for and then report success, which is worse than
    stopping. The message names the variable so the fix is obvious.
    """
    raw = os.environ.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        port = int(raw.strip())
    except ValueError:
        raise ValueError(f"{var} must be an integer port, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{var} must be a port in 1-65535, got {port}")
    return port


def orchestrator_port() -> int:
    """Port the persistent orchestrator API binds (and the wizard avoids)."""
    return _port_from_env("OKURO_PORT", ORCHESTRATOR_PORT_DEFAULT)


def embed_port() -> int:
    """Port the embedding service (``okuro-embed.plist``) binds."""
    return _port_from_env("OKURO_EMBED_PORT", EMBED_PORT_DEFAULT)


def wizard_port_preferred() -> int:
    """First port the transient onboarding wizard tries."""
    return _port_from_env("OKURO_WIZARD_PORT", WIZARD_PORT_DEFAULT)


def wizard_fallback_range() -> tuple[int, int]:
    """Inclusive (lo, hi) range searched when the preferred wizard port is busy."""
    return WIZARD_FALLBACK_RANGE


def embed_url() -> str:
    """HTTP base URL the embed client posts to."""
    return os.environ.get("OKURO_EMBED_URL", f"http://127.0.0.1:{embed_port()}")


def reserved_okuro_ports() -> set[int]:
    """Ports the wizard's free-port picker must NEVER hand out.

    Returned set covers every long-running okuro service whose plist /
    systemd unit will eventually want the port. Picking any of these
    for the transient wizard would block the persistent service from
    starting at /complete or after a restart.
    """
    return {orchestrator_port(), embed_port()}
