# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Stack module MCP tools — registry browse, profile resolve, validation, lifecycle.
# index:
#   imports
#   def _text
#   def get_tools
#   async def handle_tool
# AGENT_HEADER_END -->
"""Stack module MCP tools.

Every tool name is prefixed ``stack_`` to avoid collision in the unified
okuro MCP server. Read tools are safe; mutation tools require an ``actor``
argument (agent name) for the audit trail.
"""

import json
import logging

from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="stack_list_layers",
            description="List stack layers (taxonomy of tech decisions).",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter: runtime | frontend | backend | api | ops",
                    },
                },
            },
        ),
        Tool(
            name="stack_list",
            description=(
                "List stack entries (concrete tech choices). "
                "Filter by layer, status, or profile."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "layer":   {"type": "string"},
                    "status":  {"type": "string",
                                "enum": ["trial", "approved", "deprecated", "banned"]},
                    "profile": {"type": "string"},
                },
            },
        ),
        Tool(
            name="stack_get",
            description="Get full details for a stack entry by ID.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="stack_match",
            description=(
                "Keyword-match entries against a need description — ranks "
                "approved > trial > deprecated."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "need":  {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["need"],
            },
        ),
        Tool(
            name="stack_profile_list",
            description=(
                "List profiles (opinionated stack compositions). Filter by "
                "status or scope (frontend | backend | fullstack | agent | other)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string",
                               "enum": ["active", "draft", "archived"]},
                    "scope":  {"type": "string",
                               "enum": ["frontend", "backend", "fullstack",
                                        "agent", "other"]},
                },
            },
        ),
        Tool(
            name="stack_profile",
            description=(
                "Resolve a profile into its stack — entries grouped by "
                "category with dependency closure."
            ),
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        Tool(
            name="stack_components",
            description=(
                "What an INSTALLED stack actually ships — every component "
                "with its import path, when to use it, variants and parts. "
                "stack_profile tells you a project uses shadcn; this tells "
                "you Button lives at @/components/ui/button and has six "
                "variants. Call before writing UI against a stack. Omit "
                "`profile` to list which stacks are installed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": (
                            "Stack profile name, e.g. 'nextjs-shadcn'. "
                            "Omit to list installed stacks."
                        ),
                    }
                },
            },
        ),
        Tool(
            name="stack_active_for_project",
            description=(
                "Get the resolved stack bound to a specific project slug. "
                "Returns null if no profile is assigned."
            ),
            inputSchema={
                "type": "object",
                "properties": {"project_slug": {"type": "string"}},
                "required": ["project_slug"],
            },
        ),
        Tool(
            name="stack_validate",
            description="Whole-registry validation (dangling refs, cycles).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="stack_lint_profile",
            description=(
                "Lint a profile: banned entries, single-cardinality violations, "
                "deprecated picks."
            ),
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        Tool(
            name="stack_propose",
            description=(
                "File a proposal for human review. Kinds: new | promote | "
                "deprecate | ban | reinstate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_id":    {"type": "string"},
                    "kind":        {"type": "string",
                                    "enum": ["new", "promote", "deprecate",
                                             "ban", "reinstate"]},
                    "rationale":   {"type": "string"},
                    "proposed_by": {"type": "string"},
                    "payload":     {"type": "object"},
                },
                "required": ["entry_id", "rationale"],
            },
        ),
        Tool(
            name="stack_list_proposals",
            description="List proposals by outcome (default: pending).",
            inputSchema={
                "type": "object",
                "properties": {
                    "outcome": {"type": "string",
                                "enum": ["pending", "accepted", "rejected"]},
                    "limit":   {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="stack_decide_proposal",
            description="Accept or reject a pending proposal (human or agent).",
            inputSchema={
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string"},
                    "outcome":     {"type": "string",
                                    "enum": ["accepted", "rejected"]},
                    "decided_by":  {"type": "string"},
                },
                "required": ["proposal_id", "outcome"],
            },
        ),
        Tool(
            name="stack_assign_profile",
            description=(
                "Bind a project slug to a profile (pass profile=null to unbind)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                    "profile":      {"type": ["string", "null"]},
                },
                "required": ["project_slug"],
            },
        ),
        Tool(
            name="stack_set_status",
            description=(
                "Directly transition an entry's lifecycle status, respecting the "
                "state machine. For proposal-gated flows use stack_propose + "
                "stack_decide_proposal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id":     {"type": "string"},
                    "status": {"type": "string",
                               "enum": ["trial", "approved", "deprecated", "banned"]},
                    "actor":  {"type": "string"},
                },
                "required": ["id", "status"],
            },
        ),

        # ── Brands + slot kinds ───────────────────────────────────────────
        Tool(
            name="stack_slot_kinds",
            description=(
                "List brand slot kinds — the configurable plug points a brand "
                "can fill (design, fe_stack, be_stack, principles, …). Each "
                "kind declares its target registry and optional scope filter."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="stack_brand_list",
            description="List brands (optionally filtered by status).",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string",
                               "enum": ["active", "draft", "archived"]},
                },
            },
        ),
        Tool(
            name="stack_brand_get",
            description=(
                "Get a brand with its raw slot assignments "
                "(ref_ids, not resolved)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="stack_brand_resolve",
            description=(
                "Resolve a brand into its full composition — design tokens + "
                "frontend stack + backend stack + principles. For bootstrap "
                "and UI rendering. Missing refs are marked {missing: true}."
            ),
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="stack_brand_upsert",
            description=(
                "Create or update a brand. Slots is an object "
                "{slot_kind: ref_id | [ref_id, ...]}. Passing slots=null "
                "leaves existing assignments untouched."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id":          {"type": "string"},
                    "name":        {"type": "string"},
                    "description": {"type": "string"},
                    "status":      {"type": "string",
                                    "enum": ["active", "draft", "archived"]},
                    "slots":       {"type": ["object", "null"]},
                },
                "required": ["id", "name"],
            },
        ),
        Tool(
            name="stack_brand_assign",
            description=(
                "Bind a project slug to a brand (pass brand=null to unbind). "
                "Brand takes precedence over a project→profile binding at "
                "resolve time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                    "brand":        {"type": ["string", "null"]},
                },
                "required": ["project_slug"],
            },
        ),
        Tool(
            name="stack_brand_lint",
            description=(
                "Lint a brand: slot refs resolve, scope filters hold, "
                "required slots filled, underlying profiles lint clean."
            ),
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="stack_brand_delete",
            description=(
                "Delete a brand, its slot assignments, and any project "
                "bindings. Use with care."
            ),
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    from okuro import stack as stk

    try:
        if name == "stack_list_layers":
            return _text(stk.list_layers(category=arguments.get("category")))

        if name == "stack_list":
            return _text(stk.list_entries(
                layer=arguments.get("layer"),
                status=arguments.get("status"),
                profile=arguments.get("profile"),
            ))

        if name == "stack_get":
            entry = stk.get_entry(arguments["id"])
            return _text(entry) if entry else _text(f"Entry not found: {arguments['id']}")

        if name == "stack_match":
            return _text(stk.match_entries(
                arguments["need"], limit=int(arguments.get("limit", 5))
            ))

        if name == "stack_profile_list":
            return _text(stk.list_profiles(
                status=arguments.get("status"),
                scope=arguments.get("scope"),
            ))

        if name == "stack_components":
            from okuro.stack.components import stack_components

            return _text(stack_components(arguments.get("profile")))

        if name == "stack_profile":
            resolved = stk.resolve_profile(arguments["name"])
            return _text(resolved) if resolved else _text(
                f"Profile not found: {arguments['name']}"
            )

        if name == "stack_active_for_project":
            resolved = stk.active_profile_for(arguments["project_slug"])
            return _text(resolved) if resolved else _text(None)

        if name == "stack_validate":
            return _text(stk.validate_registry())

        if name == "stack_lint_profile":
            return _text(stk.lint_profile(arguments["name"]))

        if name == "stack_propose":
            return _text(stk.propose(
                arguments["entry_id"],
                kind=arguments.get("kind", "new"),
                proposed_by=arguments.get("proposed_by"),
                rationale=arguments["rationale"],
                payload=arguments.get("payload") or {},
            ))

        if name == "stack_list_proposals":
            return _text(stk.list_proposals(
                outcome=arguments.get("outcome"),
                limit=int(arguments.get("limit", 50)),
            ))

        if name == "stack_decide_proposal":
            return _text(stk.decide_proposal(
                arguments["proposal_id"],
                arguments["outcome"],
                decided_by=arguments.get("decided_by"),
            ))

        if name == "stack_assign_profile":
            return _text(stk.assign_project_profile(
                arguments["project_slug"], arguments.get("profile"),
            ))

        if name == "stack_set_status":
            return _text(stk.set_entry_status(
                arguments["id"], arguments["status"],
                actor=arguments.get("actor"),
            ))

        # ── Brand handlers ────────────────────────────────────────────────
        if name == "stack_slot_kinds":
            return _text(stk.list_slot_kinds())

        if name == "stack_brand_list":
            return _text(stk.list_brands(status=arguments.get("status")))

        if name == "stack_brand_get":
            brand = stk.get_brand(arguments["id"])
            return _text(brand) if brand else _text(
                f"Brand not found: {arguments['id']}"
            )

        if name == "stack_brand_resolve":
            resolved = stk.resolve_brand(arguments["id"])
            return _text(resolved) if resolved else _text(
                f"Brand not found: {arguments['id']}"
            )

        if name == "stack_brand_upsert":
            return _text(stk.upsert_brand(
                arguments["id"],
                name=arguments["name"],
                description=arguments.get("description", ""),
                status=arguments.get("status", "active"),
                slots=arguments.get("slots"),
            ))

        if name == "stack_brand_assign":
            return _text(stk.assign_project_brand(
                arguments["project_slug"], arguments.get("brand"),
            ))

        if name == "stack_brand_lint":
            return _text(stk.lint_brand(arguments["id"]))

        if name == "stack_brand_delete":
            return _text(stk.delete_brand(arguments["id"]))

    except Exception as e:
        logger.exception("stack tool %s failed", name)
        return _text({"ok": False, "error": str(e)})

    return _text(f"Unknown tool: {name}")
