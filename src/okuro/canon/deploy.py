# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deploy okuro's agent-facing surface (MCP registration + instruction
#   files + hooks) to every detected provider. One implementation, many callers.
# index: imports | def deploy_surface
# AGENT_HEADER_END -->
"""Write okuro's agent-facing surface to the providers on this machine.

Canon owns what an agent sees: the instruction files that tell it how to behave
AND the MCP registration that gives it the tools those instructions reference.
Deploying one without the other produces a half-configured agent — told to call
tools it cannot see, or handed tools it was never told about.

This module exists because that pair had drifted apart by caller:

    okuro init            MCP + instructions + hooks   (+ scoped agent home)
    POST /canon/deploy    MCP + instructions + hooks
    okuro canon deploy    MCP only                     <- added 2026-07-17, wrong
    daemon refresh (5m)   instructions + hooks only
    install.sh/update.sh  nothing at all

The CLI was the newest and the weakest: wiring install.sh to it would have
produced installs with okuro's tools registered and no CLAUDE.md telling any
agent to use them. Rather than copy the endpoint's body into the CLI — the
duplication that let the "Claude-only" hook note rot in two places at once —
both now call this.

Deliberately NOT covered here: the daemon's 5-minute ``refresh`` re-runs
instructions and hooks but never re-runs MCP registration, so MCP is the one
surface with no reconciler. That asymmetry is a scheduling decision, not a
code-sharing one; see the canon-drift work.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


def deploy_surface(
    targets: Optional[Iterable[str]] = None,
    base_home: Optional[Path] = None,
    transport: Optional[str] = None,
) -> dict:
    """Write MCP configs, instruction files and hooks for detected providers.

    Args:
        targets: provider names to limit to (adapter ids, e.g. ``{"claude"}``).
            None deploys to every detected provider. Only filters the
            instruction/hook pass — MCP config writing is per-config-file and
            has its own provider set (it covers desktop apps that have no
            adapter).
        base_home: write MCP configs under this home instead of the real user
            home — for the scoped agent home at ``~/.okuro-agent-home``.
            **MCP ONLY.** Passing it SKIPS the instruction/hook pass entirely,
            because the adapter interface cannot honour it: ``generate_
            instructions()`` and ``install_hooks()`` take no arguments and
            always write to ``Path.home()``. Running them under a base_home
            would silently scribble on the real home while the caller believed
            it was isolated — which is exactly what this argument looks like it
            prevents. ``okuro init`` already treats the scoped home as
            MCP-only; this matches it.
        transport: ``stdio`` | ``http`` | ``auto`` for the MCP entries. None
            takes ``$OKURO_MCP_TRANSPORT``, else stdio.

    Returns:
        ``{provider: {...}}`` plus ``_mcp_error`` / ``_tool_protocol`` /
        ``_scope`` keys. MCP results and adapter results merge under the same
        provider key where the names coincide, so ``results["claude"]`` carries
        both its ``mcp`` counts and its ``instructions``/``hooks`` paths. Names
        that exist on only one side (``claude_desktop`` has no adapter;
        ``antigravity`` has no MCP path yet) carry only their half.
    """
    from okuro.cli.mcp_config import write_all_configs
    from okuro.sense.providers import list_providers

    results: dict[str, dict] = {}

    # 1. MCP registration — per provider config file.
    try:
        mcp = write_all_configs(base_home, transport)
        for provider, (added, updated) in mcp.items():
            results.setdefault(provider, {})["mcp"] = {
                "added": added,
                "updated": updated,
            }
    except Exception as exc:  # noqa: BLE001
        log.exception("MCP config write failed")
        results["_mcp_error"] = {"error": str(exc)}

    if base_home is not None:
        # See base_home in the docstring: the adapters would ignore it and
        # write to the real home. Refuse rather than mislead.
        results["_scope"] = {
            "mcp_only": True,
            "reason": (
                "base_home given; instruction files and hooks always target "
                "the real home, so they were skipped"
            ),
        }
        return results

    # 2. Instruction files + hooks, per adapter.
    wanted = set(targets) if targets else None
    for adapter in list_providers():
        if wanted is not None and adapter.name not in wanted:
            continue
        r = results.setdefault(adapter.name, {})
        try:
            if not adapter.detect():
                r["skipped"] = "not detected"
                continue
            r["instructions"] = adapter.generate_instructions()
            r["hooks"] = adapter.install_hooks()
        except Exception as exc:  # noqa: BLE001
            r["error"] = str(exc)

    # 3. Provider-independent TOOL-PROTOCOL.md.
    try:
        from okuro.sense.bootstrap.sections import generate_tool_protocol
        results["_tool_protocol"] = {"path": generate_tool_protocol()}
    except Exception as exc:  # noqa: BLE001
        results["_tool_protocol"] = {"error": str(exc)}

    return results
