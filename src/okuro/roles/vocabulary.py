# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Tier + tool-name vocabulary gate for roles, run against the table.
# index:
#   imports
#   RETIRED_TOOL_ALIASES
#   def canonical_tiers
#   def live_tool_names
#   def validate_role_vocabulary
#   def validate_role_row
# AGENT_HEADER_END -->
"""The vocabulary gate, re-homed from the catalog onto the roles table.

This check used to live in the deleted seeder and ran over YAML at seed time.
The files are gone, and deleting them would have taken the gate with them —
which is the quiet half of a deletion: the content moves, and the rule that
guarded the content does not. It guards
two failures that both degrade SILENTLY, which is why they are checked at the
write boundary rather than at the point of use:

* a non-canonical ``tier`` — the role's declared tier disagrees with the only
  tier vocabulary okuro has, so every surface that shows it (role cards,
  ``roles_list``, ``load_role_index``, the derived tags) presents a word the
  rest of the system does not recognise;
* a dead tool name — the adopting agent receives a complete, confident method
  built on a tool that hard-errors.

WHAT A BAD TIER DOES **NOT** DO, corrected 2026-09-17. This docstring used to
say a non-canonical tier makes dispatch "silently run the role as standard".
It does not: ``roles.tier`` never reaches ``resolve_tier_model``. Its only two
callers are ``dispatcher_streaming.py:612``, which passes
``resolve_unit_tier(subtask, task)`` — derived from the SUBTASK's complexity,
not from the role — and ``:2652``, which passes the literal ``"fast"``. The
claim was inherited from the pre-migration-106 world and repeating it made the
gate look like it was guarding a dispatch defect rather than a vocabulary one.
The gate is still worth having; it is just honest about what it catches.

KNOWN BACKLOG, measured 2026-09-16 and deliberately not fixed here: 20 of the
87 shipped roles carry a pre-migration-106 tier and 5 declare ``tm-eichi``.
The live rows carried the same values before migration 155, so propagating
them regressed nothing — but it means this gate cannot be made fatal over the
whole table until that backlog is cleared. It is fatal on the NEW-role path,
where there is no backlog and never should be.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("okuro.roles.vocabulary")

#: MCP servers / CLI aliases the runtime no longer has. A role that declares
#: one hands every adopting agent a confident method built on a tool that
#: cannot answer. Measured 2026-08-25: neither is configured for any provider,
#: and the eichi role DB that `tm-eichi` names was replaced by okuro.db.
#: `tm-launcher` is deliberately NOT listed — 52 shipped roles still declare
#: it and its liveness was not measured; that is its own sweep.
RETIRED_TOOL_ALIASES = frozenset({"tm-eichi", "tm-nightbird"})

#: An okuro MCP verb: lowercase snake_case with at least one underscore.
#: Anything else in `tools` names an MCP server ("filesystem"), a CLI alias
#: ("tm-cortex") or a harness tool ("Read") and is out of scope.
_OKURO_VERB = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


#: How long the registry read may take before the gate gives up on it. A
#: vocabulary check must never be the reason a role write hangs.
_REGISTRY_READ_TIMEOUT_S = 5.0


def canonical_tiers() -> tuple[str, ...]:
    """The tier vocabulary, from its single source of truth.

    Lives here because this module is already the home of "what words may a
    role use", and because both the MCP tool schema and the Phase 0 prompts
    need to RENDER the list rather than retype it. Two private copies of this
    helper is how the retyped list comes back.

    Imported lazily: ``get_tools`` runs at MCP handshake, before most of okuro
    is loaded, and ``orchestrator.config`` pulls in the provider/tier
    machinery. A module-level import would make the roles tool surface depend
    on the orchestrator booting.
    """
    from okuro.orchestrator.config import CANONICAL_TIERS

    return tuple(CANONICAL_TIERS)


def live_tool_names() -> set[str]:
    """Tool names the running MCP registry serves; empty set if unreadable.

    WHY THIS IS NOT A ONE-LINER. ``list_tools_impl`` is a coroutine, and the
    obvious ``asyncio.run(...)`` raises ``RuntimeError: asyncio.run() cannot
    be called from a running event loop`` whenever the caller is already
    inside one. Every MCP tool handler is. So the original one-liner ALWAYS
    raised on the path that matters, the blanket ``except`` swallowed it, and
    the empty set it returned is exactly what disables the dead-tool half of
    ``validate_role_row`` — its branch reads ``elif live and ...``.

    Net effect, measured 2026-09-17: no role written through any MCP verb was
    ever checked for a dead tool name. The gate reported nothing because it
    never ran, which is the worst shape of failure — indistinguishable from
    a clean pass.

    So the running loop is DETECTED rather than collided with. With no loop,
    ``asyncio.run`` is still the cheapest path. With one, the coroutine runs
    on its own loop in a worker thread: ``list_tools_impl`` is ``_load_all()``
    plus a list return, holds nothing bound to the calling loop, and the
    thread is joined before this returns, so nothing outlives the call.

    The empty-set fallback stays for a genuinely unreadable registry — an
    import failure, or a read that times out — because refusing a role write
    when the tool registry cannot be consulted would make role creation
    depend on a subsystem it has nothing to do with. What changed is that the
    fallback is now taken for that reason ONLY, and says so in the log.
    """
    import asyncio
    import concurrent.futures

    try:
        from okuro.mcp import _registry
    except Exception as exc:  # noqa: BLE001 — advisory check, never fatal
        log.warning("vocabulary: tool registry not importable (%s)", exc)
        return set()

    def _read() -> set[str]:
        return {t.name for t in asyncio.run(_registry.list_tools_impl())}

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop on this thread — the direct path.
        try:
            return _read()
        except Exception as exc:  # noqa: BLE001
            log.warning("vocabulary: tool registry unreadable (%s)", exc)
            return set()

    # A loop is already running here. Give the coroutine its own loop on a
    # worker thread instead of returning an empty set and silently disabling
    # the check.
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="okuro-vocab"
        ) as pool:
            return pool.submit(_read).result(timeout=_REGISTRY_READ_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vocabulary: tool registry unreadable from inside a running loop "
            "(%s) — the dead-tool check is SKIPPED for this write",
            exc,
        )
        return set()


def _tools_of(role: dict[str, Any]) -> list[Any]:
    raw = role.get("tools")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw or []


def validate_role_row(
    role: dict[str, Any], *, live: set[str] | None = None
) -> list[str]:
    """One message per vocabulary violation in a single role row or payload."""
    from okuro.orchestrator.config import CANONICAL_TIERS

    rid = role.get("role_id") or "?"
    problems: list[str] = []

    tier = role.get("tier")
    if tier is not None and tier not in CANONICAL_TIERS:
        problems.append(
            f"{rid}: tier {tier!r} is outside the canonical set "
            f"{tuple(CANONICAL_TIERS)} — every surface that shows a role's "
            f"tier will present a word okuro does not recognise"
        )

    for name in _tools_of(role):
        if not isinstance(name, str):
            continue
        if name in RETIRED_TOOL_ALIASES:
            problems.append(
                f"{rid}: tools declares {name!r}, a retired alias the "
                f"runtime no longer has"
            )
        elif live and _OKURO_VERB.match(name) and name not in live:
            problems.append(
                f"{rid}: tools declares okuro verb {name!r}, which the "
                f"live MCP registry does not serve"
            )

    return problems


def validate_role_vocabulary(db=None, *, check_tools_live: bool = False) -> list[str]:
    """One message per vocabulary violation across every role in the table.

    ``check_tools_live`` resolves okuro-verb-shaped names against the running
    MCP registry. Off by default: importing the registry is neither cheap nor
    guaranteed to succeed.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    live = live_tool_names() if check_tools_live else set()
    problems: list[str] = []
    for row in db.fetchall("SELECT role_id, tier, tools FROM roles ORDER BY role_id"):
        problems.extend(validate_role_row(dict(row), live=live))
    return problems
