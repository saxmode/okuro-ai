# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Shared MCP tool dispatch — stdio and HTTP servers both mount this.
# index: imports | _MODULES | dispatch state | _probe_handler_imports | _load_all | getters | build_mcp_server
# AGENT_HEADER_END -->
"""Shared MCP tool dispatch.

Both the stdio server (``okuro.mcp.server``) and the HTTP server
(``okuro.mcp.http_server``) delegate here. The MCP Server instance exposed
by :func:`build_mcp_server` is the one the SDK's transport layers wrap —
StreamableHTTPSessionManager for HTTP, ``stdio_server`` for stdio.

Adding a new server module means:
  1. Append to ``_MODULES``.
  2. The module must expose ``get_tools()`` and ``async handle_tool(name, args)``.
  3. Tool names must be globally unique across modules (first wins on conflict).

Failure policy
--------------
Default ("lenient") mode: if a module fails to import, fails the AST probe,
or raises while ``get_tools()`` is called, that module is dropped, the
failure is recorded in ``FAILED_MODULES``, and the rest of the registry
continues to load. Duplicate tool names follow first-wins and are recorded
in ``DUPLICATE_TOOL_NAMES``. The synthetic ``mcp_health`` tool surfaces
both lists at runtime so operators can see what happened.

Strict mode (``OKURO_MCP_STRICT=1``): legacy behavior — raise on the first
failure or duplicate. Used in CI / dev so regressions don't quietly slip
into production.
"""

import ast
import importlib
import inspect
import logging
import os
import time as _time
from typing import Awaitable, Callable


def _iso_now() -> str:
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


# Cache: session_id -> is_agentic. Sessions are immutable on this field
# (set at create time, never updated), so a process-lifetime cache is
# safe and avoids a DB read per tool call. Size grows with the number
# of distinct sessions seen during one okuro lifetime — trivial.
_AGENTIC_SESSION_CACHE: dict[str, bool] = {}


def _session_is_agentic(session_id: str) -> bool:
    """True if the inline session was created by an orchestrator subagent
    (i.e. there is no human at the approval gate). Cached for the
    process lifetime — column is immutable post-create.

    Defaults to False (gate ACTIVE) on any error / missing row so
    human-driven sessions never accidentally bypass the gate.
    """
    cached = _AGENTIC_SESSION_CACHE.get(session_id)
    if cached is not None:
        return cached
    try:
        from okuro.db import get_db
        row = get_db().fetchone(
            "SELECT is_agentic FROM sessions_inline WHERE id = ?",
            (session_id,),
        )
        agentic = bool(row and int(row["is_agentic"] or 0) == 1)
    except Exception:
        agentic = False
    _AGENTIC_SESSION_CACHE[session_id] = agentic
    return agentic

from mcp.server import Server
from mcp.types import CallToolResult, Tool, TextContent


class ToolResult(list):
    """``list[TextContent]`` that can additionally carry ``isError``.

    A plain list cannot express failure, and returning a bare CallToolResult
    instead breaks every in-process caller: iterating a pydantic model yields
    (key, value) tuples, so ``for c in result: c.text`` raises AttributeError.
    That regression was caught by tests/mcp/test_agentic_bypass_logs_invocations
    — call_tool_impl's contract is "surface the error rather than crash", and
    it must therefore keep returning something list-shaped.

    So the flag rides ALONGSIDE the list. In-process callers see exactly what
    they saw before (indexing, iteration, ``result[0].text``); the transport
    wrapper in build_mcp_server reads ``is_error`` and emits a real
    CallToolResult. One shape everywhere, isError where it matters.
    """

    is_error: bool = False


def _error(msg: str) -> "ToolResult":
    """A tool result the client can actually recognise as a failure.

    Every failure path here previously returned a plain list[TextContent],
    which the MCP SDK wraps as isError=False. Measured 2026-07-19: a bogus
    argument, an unknown tool name, a missing keyring key and a missing
    project ALL came back isError=False — `get_project` on a nonexistent slug
    returned the 4 bytes `null`, indistinguishable from "this project has no
    data".

    Client retry logic, the model's own error-awareness, and okuro's telemetry
    all key off isError. Held permanently false, a broken tool is
    indistinguishable from an empty world — which is precisely how a retrieval
    outage stayed invisible for two months.

    Returns a ToolResult (a list) rather than a CallToolResult; the transport
    wrapper in build_mcp_server does the conversion. See ToolResult for why.
    """
    r = ToolResult([TextContent(type="text", text=msg)])
    r.is_error = True
    return r

from okuro.sense.approval_gate import (
    current_inline_session_id,
    get_gate,
    record_invocation,
    update_invocation,
)
from okuro.sense.mcp_middleware import (
    _prepend_warning,
    _text,
    post_tool_call,
    pre_tool_call,
)
from okuro.features import withheld_tools as features_withheld_tools
from okuro.sense.tool_tiers import (
    load_tier_overrides,
    merge_tier_rules,
    resolve_tool_policy,
)

logger = logging.getLogger(__name__)


# ``mcp_health`` MUST stay first — it has zero okuro-internal imports so it
# is the one tool guaranteed to load even when every downstream module fails.
_MODULES = {
    "health":  "okuro.mcp.health_tool",
    "sense":   "okuro.sense.mcp_tools",
    "cortex":  "okuro.cortex.mcp_tools",
    "keyring": "okuro.keyring.mcp_tools",
    "roles":   "okuro.roles.mcp_tools",
    "bridge":  "okuro.bridge.mcp_tools",
    "system":  "okuro.system.mcp_tools",
    "canon":   "okuro.canon.mcp_tools",
    "stack":   "okuro.stack.mcp_tools",
    "assets":  "okuro.assets.icons.mcp_tools",
    "assets_media": "okuro.assets.mcp_tools",
    "trace":   "okuro.trace.mcp_tools",
    "preview": "okuro.orchestrator.preview.mcp_tools",
    "flow_designer": "okuro.flow_designer.mcp_tools",
    "slides": "okuro.slides.mcp_tools",
    "notes": "okuro.notes.mcp_tools",
    "media": "okuro.media.mcp_tools",
    "prism": "okuro.prism.mcp_tools",
    "prism_library": "okuro.prism.library.mcp_tools",
    "prism_workflow": "okuro.prism.workflow.mcp_tools",
    "handover": "okuro.handover.mcp_tools",
    "critic": "okuro.critic.mcp_tools",
}

# One line per module, keyed exactly as ``_MODULES``. Lives here, next to
# the module map, so adding a module and forgetting its description is a
# one-file oversight rather than a silent gap in the bootstrap packet.
# ``get_module_taxonomy`` is the only consumer — bootstrap renders it, so
# there is no second hand-maintained copy to go stale.
_MODULE_DESCRIPTIONS = {
    "health":         "registry health + the live catalogue of this tool surface",
    "sense":          "bootstrap, memory, artifacts, progress, todos, reminders, people, orchestrator, KG",
    "cortex":         "codebase search/navigation/read (semantic + literal)",
    "keyring":        "encrypted secrets vault",
    "roles":          "expert role framework — match tasks to domain specialists",
    "bridge":         "unified LLM routing across providers",
    "system":         "live system state — GPU, storage, Docker, ports, services",
    "canon":          "registry of INSTALLABLE tools (CLIs, MCP servers) — not okuro's own tools",
    "stack":          "tech-stack profiles, brand tokens, component slots",
    "assets":         "icon library search + tagging",
    "assets_media":   "media asset inventory",
    "trace":          "session traces — index, search, stats",
    "preview":        "orchestrator preview servers — start/stop/status/logs",
    "flow_designer":  "saved flow diagrams",
    "slides":         "slide deck generation + editing",
    "notes":          "the user's personal note surface",
    "media":          "image generation + illustration",
    "prism":          "prism decks — build, gather, author, assemble",
    "prism_library":  "prism component library",
    "prism_workflow": "prism deck workflow steps",
    "handover":       "cross-agent handover targets + send",
    "critic":         "the uninformed-critic pass — instance-vs-class verdict on your own work",
}

# Populated lazily on first call.
_dispatch: dict[str, tuple[str, Callable[[str, dict], Awaitable[list[TextContent]]]]] = {}
_all_tools: list[Tool] = []
_loaded = False

# Public diagnostics (read via getters; mcp_health serializes them).
LOADED_MODULES: list[str] = []
FAILED_MODULES: list[dict] = []        # [{"name": str, "module": str, "error": str}]
DUPLICATE_TOOL_NAMES: list[dict] = []  # [{"name": str, "kept": str, "dropped": str}]

# Tools left out because their feature is off (okuro.features). Recorded so
# "where did that tool go" is answerable from the registry rather than from a
# bisect — the same reason DUPLICATE_TOOL_NAMES exists.
WITHHELD_TOOL_NAMES: list[str] = []

# Modules that imported cleanly but ended up contributing NO tool — every one
# of them withheld, or dropped as a duplicate. They are deliberately absent
# from LOADED_MODULES: the catalogue and the taxonomy are both derived from
# _dispatch, so a module with no dispatch entry has no catalogue key, and
# calling it "loaded" made get_module_taxonomy() advertise a module that
# get_catalog() could not describe. Recorded here so the module is still
# ACCOUNTED for — the same "where did it go" contract as the two lists above.
EMPTY_MODULE_NAMES: list[str] = []


def _strict_mode() -> bool:
    """Return True iff OKURO_MCP_STRICT is set to a truthy value."""
    val = os.environ.get("OKURO_MCP_STRICT", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _probe_enabled() -> bool:
    """Whether to run the import probe during ``_load_all``.

    OFF by default since 2026-07-29, and that is the point — see
    ``_probe_handler_imports`` for the measurement. Strict mode still probes
    (CI/dev want the loud version), and ``OKURO_MCP_PROBE=1`` forces it on for
    tests that exercise the drop/raise behaviour directly.
    """
    if _strict_mode():
        return True
    return os.environ.get("OKURO_MCP_PROBE", "").lower() in ("1", "true", "yes")


def _probe_handler_imports(mod_key: str, mod: object) -> list[str]:
    """Eagerly resolve every from-import in one tool module.

    Tool dispatchers use lazy imports inside ``handle_tool`` if-cascades to keep
    startup fast. The cost: a typo like ``from X import wrong_name`` survives
    until a user invokes that specific branch. This probe walks the module's
    AST once at registration time and resolves every ``from X import Y`` —
    surfacing failures so the caller can decide whether to drop or raise.

    NOT RUN AT BOOT BY DEFAULT — it is a test, and it now lives in one
    (``tests/mcp/test_probe_catches_typos.py``). Measured 2026-07-29, the
    reason:

      * Resolving every lazy import at boot means they are no longer lazy.
        Files loaded by ``_load_all``: 272 WITH the probe, 138 without.
      * Everything loaded is everything the strict-freshness gate can be made
        stale by, and that gate refuses server-wide. Over 30 days / 1,026
        commits touching src/okuro, the inflated base turned 386 commits into
        refusals versus 234 — so the probe alone caused 39% of them, for a
        check that never fired in production (FAILED_MODULES is empty).
      * The failure mode it buys is also the harsher one: a single typo'd lazy
        import DROPS THE WHOLE MODULE, taking ~150 sense tools out of the
        surface. Without the probe that same typo is one failed tool call with
        the ImportError in the response — smaller blast radius, and CI catches
        it before it ships either way.

    Policy:
      - ``ImportError`` (target module missing) → log warning, skip. Some
        modules legitimately handle optional deps via try/except.
      - ``AttributeError`` (target module imports but symbol missing) →
        return as a failure string. This is the typo class.

    Returns:
        list[str]: human-readable failure lines (empty if module probes clean).
    """
    failures: list[str] = []

    try:
        source = inspect.getsource(mod)
    except (OSError, TypeError):
        return failures
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        failures.append(f"{mod_key}: AST parse failed: {e}")
        return failures

    mod_package = getattr(mod, "__package__", None) or ""

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        # Resolve relative imports against the module's package, so
        # ``from .roots import X`` is checked just like absolute imports.
        try:
            if node.level:
                target = importlib.import_module(
                    "." * node.level + node.module, package=mod_package
                )
            else:
                target = importlib.import_module(node.module)
        except ImportError as e:
            logger.warning(
                "registry probe: %s imports from %s (skipped: %s)",
                mod_key, node.module, e,
            )
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            if hasattr(target, alias.name):
                continue
            # ``from pkg import name`` is valid when ``name`` is a submodule
            # that the package __init__ doesn't eagerly re-export. hasattr on
            # the (already-imported) package misses it, so attempt the submodule
            # import before recording a failure — this is exactly what Python's
            # own import machinery does for ``from pkg import name``.
            target_name = getattr(target, "__name__", node.module)
            try:
                importlib.import_module(f"{target_name}.{alias.name}")
                continue
            except ImportError:
                pass
            failures.append(
                f"{mod_key}: cannot import {alias.name!r} from "
                f"{node.module!r} (line {node.lineno})"
            )

    return failures


def _record_failure(mod_key: str, mod_path: str, error: str) -> None:
    """Append a structured failure record."""
    entry = {"name": mod_key, "module": mod_path, "error": error}
    FAILED_MODULES.append(entry)
    logger.error("MCP registry: dropped module %s (%s): %s", mod_key, mod_path, error)


def _load_all() -> None:
    """Import every module and build the flat dispatch table.

    Lenient (default) policy:
      - Per-module import / probe / get_tools failures → drop the module,
        record the failure in ``FAILED_MODULES``, continue.
      - Duplicate tool names → first wins, conflict recorded in
        ``DUPLICATE_TOOL_NAMES``.

    Strict policy (``OKURO_MCP_STRICT=1``):
      - Any failure or duplicate raises ``RuntimeError``. Used by CI / dev.
    """
    global _loaded
    if _loaded:
        return

    strict = _strict_mode()
    seen_names: dict[str, str] = {}  # tool_name -> module_name

    for mod_key, mod_path in _MODULES.items():
        # 1. Import the module.
        try:
            mod = importlib.import_module(mod_path)
        except Exception as e:
            err = f"import failed: {e!r}"
            if strict:
                raise RuntimeError(f"MCP module {mod_key} ({mod_path}): {err}") from e
            _record_failure(mod_key, mod_path, err)
            continue

        # 2. Probe handler imports (typo-class detection).
        #    Skipped unless strict/OKURO_MCP_PROBE — running it here defeats the
        #    lazy imports it walks and doubles what the freshness gate watches.
        #    The check itself now runs in CI; see _probe_handler_imports.
        try:
            probe_failures = (
                _probe_handler_imports(mod_key, mod) if _probe_enabled() else []
            )
        except Exception as e:
            err = f"probe crashed: {e!r}"
            if strict:
                raise RuntimeError(f"MCP module {mod_key} ({mod_path}): {err}") from e
            _record_failure(mod_key, mod_path, err)
            continue
        if probe_failures:
            err = "probe failures: " + " | ".join(probe_failures)
            if strict:
                raise RuntimeError(
                    "MCP registry probe found broken handler bindings:\n"
                    + "\n".join(f"  - {f}" for f in probe_failures)
                )
            _record_failure(mod_key, mod_path, err)
            continue

        # 3. Collect tools + handler.
        try:
            tools = mod.get_tools()
            handler = mod.handle_tool
        except Exception as e:
            err = f"get_tools/handle_tool failed: {e!r}"
            if strict:
                raise RuntimeError(f"MCP module {mod_key} ({mod_path}): {err}") from e
            _record_failure(mod_key, mod_path, err)
            continue

        # 4. Register tools (first-wins on duplicate names).
        #
        # A tool belonging to a feature that is OFF is withheld here rather
        # than refused at call time: the tool list is sent once at handshake,
        # so a name that is absent is a name the agent never learns. This is
        # the one place that reaches every tool at any granularity — the
        # largest module returns its tools as a single literal list with no
        # per-tool table inside it, so a filter in the modules would not.
        withheld = features_withheld_tools()
        registered = 0
        for tool in tools:
            if tool.name in withheld:
                WITHHELD_TOOL_NAMES.append(tool.name)
                continue
            if tool.name in seen_names:
                dup = {
                    "name": tool.name,
                    "kept": seen_names[tool.name],
                    "dropped": mod_key,
                }
                if strict:
                    raise RuntimeError(
                        f"Duplicate tool name '{tool.name}' — "
                        f"defined in both '{seen_names[tool.name]}' and '{mod_key}'"
                    )
                DUPLICATE_TOOL_NAMES.append(dup)
                logger.warning(
                    "MCP registry: duplicate tool '%s' kept from '%s', "
                    "dropped from '%s'",
                    tool.name, seen_names[tool.name], mod_key,
                )
                continue
            seen_names[tool.name] = mod_key
            _dispatch[tool.name] = (mod_key, handler)
            _all_tools.append(tool)
            registered += 1

        # A module that contributed nothing is not part of the served surface.
        # The first case to reach this was a FEATURE owning a whole module
        # (okuro.features gating all six flow_designer_* tools), but the same
        # hole was already reachable via duplicate-drops — get_catalog() keys
        # off _dispatch, so "loaded" without a dispatch entry made the
        # taxonomy name a module the catalogue had no entry for.
        if registered:
            LOADED_MODULES.append(mod_key)
        else:
            EMPTY_MODULE_NAMES.append(mod_key)
            logger.info(
                "MCP registry: module '%s' contributed no tools "
                "(all withheld or dropped) — not advertised.",
                mod_key,
            )

    _loaded = True
    logger.info(
        "okuro MCP registry loaded %d tools from %d/%d modules "
        "(failed=%d, duplicates=%d, strict=%s)",
        len(_all_tools),
        len(LOADED_MODULES), len(_MODULES),
        len(FAILED_MODULES), len(DUPLICATE_TOOL_NAMES),
        strict,
    )


# --- Public diagnostics getters (read by mcp_health and tests) ---

def get_loaded_modules() -> list[str]:
    """Modules whose tools were successfully registered."""
    return list(LOADED_MODULES)


def get_failed_modules() -> list[dict]:
    """Modules dropped during load. Each: {name, module, error}."""
    return [dict(entry) for entry in FAILED_MODULES]


def get_duplicate_tool_names() -> list[dict]:
    """Duplicate tool names resolved first-wins. Each: {name, kept, dropped}."""
    return [dict(entry) for entry in DUPLICATE_TOOL_NAMES]


def get_tool_count() -> int:
    """Total tools currently exposed by the registry."""
    return len(_all_tools)


def get_module_taxonomy() -> list[dict]:
    """Live module taxonomy for the bootstrap packet.

    Each entry: ``{module, description, tool_count}``, loaded modules only,
    sorted by ``_MODULES`` declaration order. Bootstrap renders this instead
    of carrying its own list — the copies it replaced had drifted to 7
    modules while the registry served 21.
    """
    _load_all()
    counts: dict[str, int] = {}
    for _tool_name, (mod_key, _handler) in _dispatch.items():
        counts[mod_key] = counts.get(mod_key, 0) + 1
    return [
        {
            "module": mod_key,
            "description": _MODULE_DESCRIPTIONS.get(mod_key, ""),
            "tool_count": counts.get(mod_key, 0),
        }
        for mod_key in _MODULES
        if mod_key in LOADED_MODULES
    ]


def get_catalog(
    module: str | None = None,
    search: str | None = None,
    detail: str = "names",
) -> dict:
    """The live catalogue of this server's OWN tool surface.

    Derived from ``_dispatch`` / ``_all_tools`` at call time, so it can
    never drift from what the process actually serves. This exists
    because three places used to hand-maintain a copy of the module
    taxonomy (the two bootstrap builders and the canon ``okuro.yaml``
    entry) and every copy went stale independently — all three still
    listed 7 modules long after the registry reached 21.

    NOT to be confused with ``canon_list_tools``: canon is a registry of
    *installable* tools (CLIs, MCP servers you can deploy). It has no
    entry for an individual okuro tool, so ``canon_get_tool("read_memory")``
    correctly returns "not found" — it was never the schema source.

    Args:
        module: restrict to one registry module key (see ``_MODULES``).
        search: case-insensitive substring match on tool name + description.
        detail: ``"names"`` → module → [tool names]. ``"full"`` → module →
            [{name, description, inputSchema}]. ``"full"`` is only allowed
            with a ``module`` or ``search`` filter; unfiltered it would
            serialize every schema on the server.

    Returns:
        ``{tool_count, module_count, modules, filtered}`` — or an
        ``{error: ...}`` envelope for an unknown module / unfiltered
        ``full``.
    """
    _load_all()

    if detail not in ("names", "full"):
        return {"error": "detail must be 'names' or 'full'"}
    if module is not None and module not in _MODULES:
        return {
            "error": f"unknown module {module!r}",
            "known_modules": sorted(_MODULES),
        }
    if detail == "full" and not module and not search:
        return {
            "error": (
                "detail='full' requires a module or search filter — "
                "returning every schema on the server would be ~300 KB. "
                "Call with detail='names' first to find the module."
            ),
            "known_modules": sorted(_MODULES),
        }

    needle = search.lower() if search else None
    by_name = {t.name: t for t in _all_tools}

    modules: dict[str, list] = {}
    matched = 0
    for tool_name, (mod_key, _handler) in _dispatch.items():
        if module and mod_key != module:
            continue
        tool = by_name.get(tool_name)
        if needle:
            haystack = f"{tool_name} {getattr(tool, 'description', '') or ''}".lower()
            if needle not in haystack:
                continue
        matched += 1
        if detail == "names":
            modules.setdefault(mod_key, []).append(tool_name)
        else:
            modules.setdefault(mod_key, []).append({
                "name": tool_name,
                "description": getattr(tool, "description", None),
                "inputSchema": getattr(tool, "inputSchema", None),
            })

    if detail == "names":
        for names in modules.values():
            names.sort()
    else:
        for entries in modules.values():
            entries.sort(key=lambda e: e["name"])

    return {
        "tool_count": matched,
        "module_count": len(modules),
        "modules": dict(sorted(modules.items())),
        "filtered": bool(module or search),
    }


def is_strict_mode() -> bool:
    """Whether OKURO_MCP_STRICT is set."""
    return _strict_mode()


def _reset_for_tests() -> None:
    """Test hook: clear cached registry state so _load_all reruns cleanly."""
    global _loaded
    _dispatch.clear()
    _all_tools.clear()
    LOADED_MODULES.clear()
    FAILED_MODULES.clear()
    DUPLICATE_TOOL_NAMES.clear()
    WITHHELD_TOOL_NAMES.clear()
    EMPTY_MODULE_NAMES.clear()
    _loaded = False


async def list_tools_impl() -> list[Tool]:
    _load_all()
    return _all_tools


async def call_tool_impl(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch one tool call through the middleware chain.

    The current session id must already be set in the contextvar (by the
    transport layer) before this is called. Middleware reads it implicitly
    via ``get_session_state()``.

    Wave 3a — when the request is an inline-session HTTP MCP call (the
    contextvar ``current_inline_session_id`` is set), the dispatcher
    consults the tier-based approval gate BEFORE running the handler and
    stamps the resulting ``tool_invocations`` audit row with executed_at
    / result / error / cost_ms on completion. Direct stdio MCP (legacy
    CLI sessions) bypasses the gate entirely — the contextvar stays None.
    """
    _load_all()

    t0 = _time.time()
    ok = True
    inline_session_id = current_inline_session_id.get()
    invocation_id: str | None = None

    try:
        allow, message = pre_tool_call(name, arguments)
        if not allow:
            ok = False
            # A rejection is a failure to execute, so it carries isError too.
            # Without this the gate masks the isError fix entirely: any tool
            # called before bootstrap — including a NONEXISTENT one — is
            # rejected here first and reported as success, so a client still
            # cannot tell a typo'd tool name from a legitimate gate refusal.
            return _error(message or f"Tool `{name}` rejected by middleware.")

        if name not in _dispatch:
            ok = False
            return _error(f"Unknown tool: {name}")

        mod_key, handler = _dispatch[name]

        # Tier-based approval gate (inline sessions only).
        # SKIP for agentic sessions — there is no human at the gate for
        # orchestrator subagents; consulting it would deadlock every
        # write-tier MCP call. Set at session-creation time via
        # `registry.create(is_agentic=True)`. Audit 2026-05-29 identified
        # this as the root cause of ~all 20 task failures: every
        # artifact_write / log_progress / session_report from a subagent
        # waited 300s for human approval that never came.
        if inline_session_id and not _session_is_agentic(inline_session_id):
            rule_overrides, policy_overrides = load_tier_overrides()
            merged_rules = merge_tier_rules(rule_overrides)
            tier, policy = resolve_tool_policy(
                name,
                arguments,
                rule_overrides=merged_rules,
                policy_overrides=policy_overrides,
            )
            decision = await get_gate().consult(
                session_id=inline_session_id,
                tool_name=name,
                args=arguments,
                tier=tier,
                policy=policy,
            )
            invocation_id = decision.invocation_id
            if not decision.allow:
                ok = False
                return _text(
                    {
                        "error": "approval_denied",
                        "invocation_id": decision.invocation_id,
                        "tool_name": name,
                        "tier": tier,
                        "policy": policy,
                        "status": decision.status,
                    }
                )
            # Edited args take precedence when the user approved with edits.
            arguments = decision.args
        elif inline_session_id:
            # Theme I — agentic sessions skip the gate (correct, per
            # migration 053) but STILL need a tool_invocations audit row
            # so observability stays intact. Without this INSERT the
            # audit table froze at the pre-spawn count for the duration
            # of every subagent run, gate-bypass regressions stayed
            # invisible (no row → no anomaly detectable), and the
            # approval UI's "what is this agent doing?" view went
            # silent. approval_status='agentic_auto' (migration 054
            # extends the CHECK constraint) keeps the auto-vs-bypass
            # distinction explicit for downstream filters. The row is
            # stamped with executed_at / result / cost_ms below — same
            # code path the consult flow uses, just minted up-front
            # with no human-approval round-trip.
            import uuid as _uuid
            invocation_id = _uuid.uuid4().hex
            try:
                record_invocation(
                    invocation_id=invocation_id,
                    session_id=inline_session_id,
                    tool_name=name,
                    args=arguments,
                    approval_status="agentic_auto",
                    approved_at=None,
                    executed_at=None,
                    result=None,
                    error=None,
                    cost_ms=None,
                )
            except Exception as _exc:  # noqa: BLE001 — observability never blocks
                logger.warning(
                    "agentic_auto invocation INSERT failed (session=%s "
                    "tool=%s): %r — proceeding with handler",
                    inline_session_id, name, _exc,
                )
                invocation_id = None

        try:
            result = await handler(name, arguments)
        except Exception as e:
            ok = False
            logger.exception("Tool %s (module %s) failed", name, mod_key)
            if invocation_id:
                latency = int((_time.time() - t0) * 1000)
                update_invocation(
                    invocation_id=invocation_id,
                    executed_at=_iso_now(),
                    error=str(e),
                    cost_ms=latency,
                )
            return _error(f"Error: {e}")

        if invocation_id:
            latency = int((_time.time() - t0) * 1000)
            preview = result[0].text[:2000] if result and result[0].text else None
            update_invocation(
                invocation_id=invocation_id,
                executed_at=_iso_now(),
                result=preview,
                cost_ms=latency,
            )

        # An empty result must say WHY it is empty, before anything else
        # wraps it. Four recorded sessions read a bare `[]` as evidence of
        # absence when it meant "wrong id namespace", "not instrumented on
        # this transport" or "a post-scan filter discarded everything" — see
        # sense/empty_reason.py for each. Keyed on the SHAPE of the payload,
        # not on a list of tool names, so a new list-returning tool inherits
        # the explanation instead of re-opening the hole; tools with a
        # specific answer register an explainer, the rest get the generic one.
        #
        # Sits ahead of the compliance wrapper so the two compose: the
        # annotation lands inside the payload, the warning around it.
        if result and result[0].text:
            trailing = list(result[1:])
            try:
                import json
                from okuro.sense.empty_reason import annotate_empty, is_empty_payload
                data = json.loads(result[0].text)
                if is_empty_payload(data):
                    result = [
                        *_text(annotate_empty(name, arguments, data)),
                        *trailing,
                    ]
            except (json.JSONDecodeError, ValueError):
                pass  # non-JSON payloads carry their own prose already
            except Exception:  # noqa: BLE001 — a diagnostic never breaks dispatch
                logger.warning("empty_reason annotation failed for %s", name)

        if message and result:
            if result and result[0].text:
                # The warning attaches to the FIRST block, and every block
                # after it is carried through untouched. Both branches used to
                # rebuild `result` from result[0] alone, so any tool returning
                # more than one content block had the rest silently deleted —
                # but only when a compliance message happened to be active, so
                # unit tests (which never see this wrapper) stayed green.
                #
                # Found live 2026-08-04: cortex's worktree disclosure block
                # vanished between the handler and the client. Keyed on the
                # SHAPE, not on a tool name, for the same reason the array
                # branch of _prepend_warning is — a new multi-block tool must
                # not silently re-open the hole.
                trailing = list(result[1:])
                try:
                    import json
                    data = json.loads(result[0].text)
                    data = _prepend_warning(data, message)
                    result = [*_text(data), *trailing]
                except (json.JSONDecodeError, ValueError):
                    result = [
                        TextContent(
                            type="text",
                            text=f"{message}\n\n---\n\n{result[0].text}",
                        ),
                        *trailing,
                    ]

        return result

    except Exception as e:
        ok = False
        logger.exception("Dispatch failed for tool %s", name)
        return _error(f"Error: {e}")
    finally:
        latency = int((_time.time() - t0) * 1000)
        post_tool_call(name, arguments, latency_ms=latency, ok=ok)


def build_mcp_server() -> Server:
    """Build an MCP Server instance wired up to the shared dispatch table.

    Returns a fresh Server every call. Each transport should own one.
    """
    server: Server = Server("okuro")

    # Snapshot boot-time code content so the freshness gate decides on content
    # (loaded vs disk), not on "any commit after boot". Idempotent + best-effort;
    # if it does not run, detect_code_drift falls back to the date-based proxy.
    try:
        from okuro.system.code_version import prime_baseline
        prime_baseline()
    except Exception:  # noqa: BLE001 — never block server construction
        pass

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return await list_tools_impl()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        # Per-call session identity — the fix that makes the HTTP transport
        # safe for concurrent sessions. The transport's Starlette middleware
        # sets current_session_id on the REQUEST task, but StreamableHTTP runs
        # each session's tool handler in a separate long-lived task the
        # contextvar never reaches, so without this every HTTP session collapses
        # to the module-level "stdio" state dict (proven: session B rode session
        # A's bootstrap). The low-level Server sets request_ctx in THIS task
        # around the handler and its .request carries the mcp-session-id header,
        # so read it here — the one context that actually reaches call_tool_impl.
        # Stdio has no request → sid stays None → the "stdio" default is correct.
        _sid_tok = None
        _bound_tok = None
        try:
            from okuro.sense.session_state import (
                current_session_id,
                transport_bound_session,
            )
            req = getattr(server.request_context, "request", None)
            sid = req.headers.get("mcp-session-id") if req is not None else None
            if sid:
                _sid_tok = current_session_id.set(sid)
                # Declare it BOUND, or pre_tool_call's adopt_caller_identity
                # immediately overwrites what we just set with the local hook
                # file's contents — reproduced: an HTTP session id becomes
                # "stdio" (empty file) or "agent:<whoever wrote last>", and
                # every concurrent session shares one state dict again.
                _bound_tok = transport_bound_session.set(True)
        except Exception:  # noqa: BLE001 — never fail a call over session plumbing
            pass

        try:
            result = await call_tool_impl(name, arguments)
            # Translate the in-process error flag into a real protocol-level
            # failure. Doing it HERE keeps call_tool_impl's return type
            # homogeneous for every Python caller while still giving clients,
            # retry logic and telemetry an honest isError to key off.
            if getattr(result, "is_error", False):
                return CallToolResult(content=list(result), isError=True)
            return result
        finally:
            if _sid_tok is not None:
                current_session_id.reset(_sid_tok)
            if _bound_tok is not None:
                transport_bound_session.reset(_bound_tok)

    return server
