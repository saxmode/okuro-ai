# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bridge module tools — unified LLM bridge.
# index: imports | def _text | def get_tools
# AGENT_HEADER_END -->
"""Bridge module tools — unified LLM bridge.

Extracted from bridge/mcp_server.py for use by the unified okuro.mcp.server.
"""

import asyncio
import json
import logging

from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

# Per-MCP-process serialization for bridge_invoke. Without this, a
# subagent that fires multiple bridge_invoke calls in rapid succession
# (observed: 3 calls in 11s during a deliberate task) spawns
# concurrent nested CLI subprocesses that collide on the parent's
# process group (start_new_session=True) and end up SIGTERM-ing the
# subagent itself with exit 143. One in-flight at a time keeps the
# nested-CLI tree well-formed; queued calls add latency but never
# corrupt the run.
_BRIDGE_INVOKE_LOCK = asyncio.Lock()


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="bridge_invoke",
            description=(
                "Route a prompt to the best LLM provider based on capability. "
                "Pass `images` to have the model LOOK at local image files and "
                "answer about them (capability='vision')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The prompt to send (or use prompt_file)"},
                    "prompt_file": {"type": "string", "description": "Path to a file whose contents are the prompt — for large prompts (e.g. full bootstrap packets). Takes precedence over prompt."},
                    "capability": {"type": "string", "description": "Routing capability", "default": "quality"},
                    "provider": {"type": "string", "description": "Force specific provider"},
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Absolute paths to local images (png/jpg/jpeg/webp/gif) "
                            "for the model to look at. Use capability='vision'. "
                            "This is image INPUT — for image GENERATION the tool "
                            "is comfy_generate / illustrate, not this."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="bridge_providers",
            description="List available LLM providers and routing table.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="bridge_status",
            description="Check bridge health and provider availability.",
            inputSchema={"type": "object", "properties": {}},
        ),
        # ------------------------------------------------------------------
        # Wave 2 — streaming surface (claude only). gemini/codex return a
        # structured ``adapter_not_implemented`` error. Wave 3 wires real
        # MCP HTTP target + approval gate; wave 4 promotes polling to SSE.
        # ------------------------------------------------------------------
        Tool(
            name="bridge_stream_start",
            description=(
                "Spawn a streaming CLI session for inline web chat. "
                "Returns the session_id used by the other bridge_stream_* tools."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "CLI provider id (wave 2: 'claude' only)",
                    },
                    "model": {"type": "string", "description": "Provider model (e.g. sonnet)"},
                    "system": {"type": "string", "description": "Optional system prompt"},
                    "messages": {
                        "type": "array",
                        "description": "Optional initial user turns",
                        "items": {"type": "object"},
                    },
                    "tools": {
                        "type": "array",
                        "description": "Reserved for wave-3 MCP-tool whitelist; ignored in wave 2",
                        "items": {"type": "object"},
                    },
                    "todo_id": {
                        "type": "string",
                        "description": "Parent todos.id; a stub is created when omitted",
                    },
                },
                "required": ["provider"],
            },
        ),
        Tool(
            name="bridge_stream_event",
            description=(
                "Poll unified events from a streaming session. "
                "Returns events with seq >= since_seq, plus next_seq and a done flag. "
                "Polling delivery — SSE upgrade is wave 4."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "since_seq": {"type": "integer", "default": 0},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="bridge_stream_input",
            description="Push one user message into a running streaming session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["session_id", "message"],
            },
        ),
        Tool(
            name="bridge_stream_cancel",
            description="SIGTERM the streaming session's CLI subprocess (2s grace, then SIGKILL).",
            inputSchema={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
        ),
        # ------------------------------------------------------------------
        # Wave 3a — approval gate + audit ledger
        # ------------------------------------------------------------------
        Tool(
            name="bridge_stream_approve",
            description=(
                "Resolve a pending tool-approval request on a streaming session. "
                "decision ∈ approve|deny|edit. edited_args replaces the original "
                "args when decision='edit'. Returns {ok, invocation_id, decision}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "invocation_id": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": ["approve", "deny", "edit"],
                    },
                    "edited_args": {
                        "type": "object",
                        "description": "Required when decision='edit'.",
                    },
                },
                "required": ["session_id", "invocation_id", "decision"],
            },
        ),
        Tool(
            name="list_tool_invocations",
            description=(
                "Audit ledger for an inline session — returns rows from "
                "tool_invocations newest-first. Used by the approval UI and "
                "the cost dashboard."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["session_id"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "bridge_invoke":
        from okuro.bridge import invoke
        async with _BRIDGE_INVOKE_LOCK:
            # invoke is sync (spawns nested CLI subprocess). Run it in a
            # threadpool so the MCP event loop stays responsive to other
            # tool calls while the LLM works.
            result = await asyncio.to_thread(
                invoke,
                prompt=arguments.get("prompt"),
                prompt_file=arguments.get("prompt_file"),
                capability=arguments.get("capability", "quality"),
                provider=arguments.get("provider"),
                images=arguments.get("images"),
            )
        return _text(result)

    if name == "bridge_providers":
        from okuro.bridge import list_providers, get_routing_table
        return _text({"providers": list_providers(), "routing": get_routing_table()})

    if name == "bridge_status":
        from okuro.bridge import list_providers
        providers = list_providers()
        return _text({"status": "ok", "providers": len(providers)})

    # ----------------------------------------------------------------------
    # Streaming surface — wave 2 + wave 3a
    # ----------------------------------------------------------------------
    if name in (
        "bridge_stream_start",
        "bridge_stream_event",
        "bridge_stream_input",
        "bridge_stream_cancel",
        "bridge_stream_approve",
        "list_tool_invocations",
    ):
        return await _handle_stream_tool(name, arguments)

    return _text(f"Unknown tool: {name}")


async def _handle_stream_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch the four bridge_stream_* tools.

    All errors are caught and surfaced as structured JSON payloads so
    the web layer (and downstream agents) can branch on the ``error``
    discriminator rather than parsing free-text.
    """
    from okuro.bridge.streaming import (
        AdapterNotImplemented,
        SessionAlreadyTerminal,
        SessionNotFound,
        get_registry,
    )

    registry = get_registry()

    try:
        if name == "bridge_stream_start":
            provider = arguments.get("provider")
            if not provider:
                return _text({"error": "missing_provider"})
            session_id = registry.create(
                provider=provider,
                model=arguments.get("model"),
                system=arguments.get("system"),
                messages=arguments.get("messages"),
                tools=arguments.get("tools"),
                todo_id=arguments.get("todo_id"),
            )
            return _text({"session_id": session_id})

        if name == "bridge_stream_event":
            session_id = arguments.get("session_id")
            if not session_id:
                return _text({"error": "missing_session_id"})
            since_seq = int(arguments.get("since_seq", 0) or 0)
            return _text(registry.events(session_id, since_seq=since_seq))

        if name == "bridge_stream_input":
            session_id = arguments.get("session_id")
            message = arguments.get("message")
            if not session_id or message is None:
                return _text({"error": "missing_args"})
            return _text(registry.send_input(session_id, message))

        if name == "bridge_stream_cancel":
            session_id = arguments.get("session_id")
            if not session_id:
                return _text({"error": "missing_session_id"})
            return _text(registry.cancel(session_id))

        if name == "bridge_stream_approve":
            session_id = arguments.get("session_id")
            invocation_id = arguments.get("invocation_id")
            decision = arguments.get("decision")
            edited_args = arguments.get("edited_args")
            if not session_id or not invocation_id or not decision:
                return _text({"error": "missing_args"})
            from okuro.sense.approval_gate import get_gate

            return _text(
                get_gate().resolve(
                    invocation_id=invocation_id,
                    decision=decision,
                    edited_args=edited_args,
                )
            )

        if name == "list_tool_invocations":
            session_id = arguments.get("session_id")
            if not session_id:
                return _text({"error": "missing_session_id"})
            limit = int(arguments.get("limit", 50) or 50)
            from okuro.sense.approval_gate import list_invocations

            return _text(
                {
                    "session_id": session_id,
                    "invocations": list_invocations(session_id, limit=limit),
                }
            )

    except AdapterNotImplemented as exc:
        return _text(
            {
                "error": "adapter_not_implemented",
                "provider": exc.provider,
                "wave": exc.wave,
            }
        )
    except SessionNotFound:
        return _text({"error": "session_not_found"})
    except SessionAlreadyTerminal as exc:
        return _text({"error": "session_already_terminal", "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("bridge_stream tool %s failed", name)
        return _text({"error": "internal", "detail": str(exc)})

    return _text({"error": "unknown_tool", "tool": name})
