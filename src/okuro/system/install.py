# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: CLI-agnostic service registry + install helper for okuro background services.
# index:
#   imports
#   def project_root
#   def get_service_registry
#   def install_all_services
# AGENT_HEADER_END -->
"""CLI-agnostic service registry + install helper.

The canonical set of okuro background services (``okuro-orchestrator``,
``okuro-embed``, ``okuro-daemon``) lives here so that both ``okuro init``
(CLI) and ``POST /api/onboarding/complete`` (web wizard) can install
them without the web layer importing Click-tainted modules from
``okuro.cli``. ``okuro-daemon`` now also hosts the Streamable HTTP MCP
server — subagent #15 folded the standalone ``okuro-mcpd`` in so users
run 3 long-lived services, not 4.

Previously the registry was defined in ``okuro.cli.cmd_service`` which
made service install callable only from the CLI process; surfacing it
here keeps the entry points symmetric.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from okuro.embed.config import EmbedConfig, load_or_init as load_embed_config
from okuro.system.interpreter import ensure_okuro_interpreter
from okuro.system.port_registry import EMBED_PORT_DEFAULT
from okuro.system.service_manager import (
    DarwinServiceManager,
    ServiceSpec,
    get_service_manager,
)

logger = logging.getLogger("okuro.system.install")


# Services that need GPU pinning + tier env injection. All three okuro
# user-services run on the host with CUDA_VISIBLE_DEVICES set, because
# any of them can end up touching the embed model (orchestrator via
# bridge_invoke fallback, daemon via cortex refresh, embed obviously).
# Restricting the pin to embed alone left two un-pinned processes
# defaulting to GPU 0 (the display GPU) — see the 2026-05-10 incident.
_TIER_AWARE_SERVICES = {"okuro-embed", "okuro-daemon", "okuro-orchestrator"}


def _apply_embed_config_to_specs(
    registry: dict[str, ServiceSpec],
    cfg: EmbedConfig,
) -> None:
    """Mutate each tier-aware spec's environment to reflect ``cfg``.

    - OKURO_EMBED_TIER is always set so the service can resolve its
      tier without re-reading the config file.
    - CUDA_VISIBLE_DEVICES is set when the device is ``cuda:N``;
      explicitly UNSET (popped) otherwise so a previous "high"
      install's pin doesn't leak into a "low" reinstall.
    """
    cuda_devices = cfg.cuda_visible_devices()
    for name, spec in registry.items():
        if name not in _TIER_AWARE_SERVICES:
            continue
        spec.environment["OKURO_EMBED_TIER"] = cfg.tier
        if cuda_devices is not None:
            spec.environment["CUDA_VISIBLE_DEVICES"] = cuda_devices
        else:
            spec.environment.pop("CUDA_VISIBLE_DEVICES", None)


def project_root() -> Path:
    """Best-effort okuro project root (where the installed package lives)."""
    try:
        import okuro

        # src/okuro/__init__.py → src/okuro → src → repo root
        return Path(okuro.__file__).resolve().parent.parent.parent
    except Exception:
        return Path.cwd()


def get_service_registry(
    project_root_override: Optional[Path] = None,
) -> dict[str, ServiceSpec]:
    """Return the built-in registry of okuro services.

    Each service gets its OWN renamed Python interpreter copy whose
    filename matches the service name. On macOS those copies live
    inside ``Okuro.app/Contents/MacOS/`` so Activity Monitor pulls the
    Okuro icon from the bundle association instead of falling back to
    the generic Python rocket. On Linux they live in
    ``<venv>/libexec/``. See ``okuro.system.interpreter`` for rationale.

    Per-service binaries (rather than a shared ``okuro`` interpreter)
    let Activity Monitor / ``ps -o comm`` distinguish the orchestrator
    from the daemon from the embed server at a glance.
    """
    root = project_root_override or project_root()

    return {
        "okuro-orchestrator": ServiceSpec(
            name="okuro-orchestrator",
            description="okuro-orchestrator — merged API + SPA",
            # ``.api.serve``, NOT ``.api.main``: running main as __main__ makes
            # uvicorn's import string execute the whole 7,549-line module a
            # SECOND time under its canonical name, leaving two live copies
            # whose event_watcher / ws_manager / _API_TOKEN state diverges.
            # serve.py imports it once, canonically, and asserts the identity.
            exec_start=[
                str(ensure_okuro_interpreter("okuro-orchestrator")),
                "-m",
                "okuro.orchestrator.api.serve",
            ],
            working_directory=root,
            # Spawns claude/codex/agy for decomposition + bridge calls.
            # Those CLIs' auth credentials live in the user's keychain with
            # ACL scoped to the Aqua session — without this binding the
            # subprocesses report "Not logged in" even when the user is
            # actively using them from Terminal. See ServiceSpec
            # .requires_gui_session for the full rationale.
            requires_gui_session=True,
        ),
        "okuro-embed": ServiceSpec(
            name="okuro-embed",
            description="okuro-embed — shared embedding service",
            exec_start=[
                str(ensure_okuro_interpreter("okuro-embed")),
                "-c",
                "from okuro.embed.server import main; main()",
            ],
            working_directory=root,
            environment={
                # Sourced from port_registry — keeps the plist value in
                # lock-step with embed_port() so they can never drift apart.
                "OKURO_EMBED_PORT": str(EMBED_PORT_DEFAULT),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
            # No CLI spawning — embed is a pure local model server. It
            # should keep running across login/logout cycles for any
            # background indexing the daemon kicks off.
        ),
        "okuro-daemon": ServiceSpec(
            name="okuro-daemon",
            description=(
                "okuro-daemon — unified scheduler + HTTP MCP server "
                "(reminders, refresh, cortex, cron tasks, + Streamable "
                "HTTP MCP for Claude/Codex/Cursor/Antigravity)"
            ),
            exec_start=[
                str(ensure_okuro_interpreter("okuro-daemon")),
                "-m",
                "okuro.daemon",
            ],
            working_directory=root,
            # Hosts the HTTP MCP surface that subagents (claude/codex/
            # agy) call back into, and runs the capability harvester
            # which itself spawns claude. Same Aqua-binding requirement
            # as the orchestrator.
            requires_gui_session=True,
        ),
    }


def install_all_services(
    names: Optional[list[str]] = None,
    start: bool = False,
) -> dict[str, str]:
    """Install + enable (and optionally start) the named services.

    Args:
        names: Subset to install. ``None`` installs every service in the
            registry. Orchestrator is the only one most users need running
            immediately; embed/daemon are opt-in per-user.
        start: If True, ``start`` each service after enabling.

    Returns:
        ``{name: status}`` where status is ``"installed"``, ``"started"``,
        or an error string. Caller decides how loud to be on failures.
    """
    try:
        mgr = get_service_manager()
    except (NotImplementedError, RuntimeError) as exc:
        logger.warning("service manager unavailable: %s", exc)
        return {"_manager": f"unavailable: {exc}"}

    registry = get_service_registry()
    if names is not None:
        registry = {k: v for k, v in registry.items() if k in names}

    results: dict[str, str] = {}

    # The user's persisted tier + device choice is the single source of
    # truth for unit-file Environment lines. load_or_init() returns the
    # existing config untouched if present (the meta-requirement of v2),
    # or runs hardware detection and writes the recommendation if absent.
    # NEVER overwrites an existing file.
    try:
        embed_cfg = load_embed_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embed config load_or_init failed (%s) — services install "
            "without tier/device pinning",
            exc,
        )
        embed_cfg = None
    if embed_cfg is not None:
        _apply_embed_config_to_specs(registry, embed_cfg)
        results["_embed_config"] = (
            f"tier={embed_cfg.tier} device={embed_cfg.device}"
        )

    # On Linux, services die when the user logs out unless linger is enabled.
    # Best-effort, non-fatal: installation continues even if we couldn't turn
    # it on, but the caller gets a surface-able status line.
    from okuro.system.service_manager import LinuxServiceManager
    if isinstance(mgr, LinuxServiceManager):
        enabled, msg = mgr.ensure_linger()
        results["_linger"] = "enabled" if enabled else f"manual: {msg}"

    for name, spec in registry.items():
        try:
            if isinstance(mgr, DarwinServiceManager) and not start:
                # launchd plists have RunAtLoad=true; bootstrapping them is
                # equivalent to starting them. For deferred starts, write the
                # LaunchAgent only. launchd will load it on the next login,
                # and explicit starts can bootstrap it later.
                mgr.install(spec, load=False)
                status = "installed (start deferred)"
            else:
                mgr.install(spec)
                mgr.enable(name)
                status = "installed"
            if start:
                try:
                    mgr.start(name)
                    status = "started"
                except Exception as exc:  # noqa: BLE001
                    status = f"installed (start failed: {exc})"
            results[name] = status
        except Exception as exc:  # noqa: BLE001
            logger.warning("service install failed (%s): %s", name, exc)
            results[name] = f"error: {exc}"

    return results
