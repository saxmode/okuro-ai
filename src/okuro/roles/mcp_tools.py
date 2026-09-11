# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Roles module tools — role framework.
# index: imports | def _text | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
"""Roles module tools — role framework.

Extracted from roles/mcp_server.py for use by the unified okuro.mcp.server.
"""

import json
import logging

from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _format_role_assumption(role: dict, maintenance: dict) -> str:
    """Render a role payload as a role-assumption directive.

    LLMs treat tool results as inputs, not instructions. By leading with
    "You are now operating as X" and framing the body as a directive, the
    model conditions on the role as operating context rather than reference
    data. The structured metadata tail preserves machine-readable fields
    for agents that want both.
    """
    role_id = role.get("id", "unknown")
    domain = role.get("domain", "")
    tier = role.get("tier", "standard")
    model = role.get("model", "sonnet")
    level = role.get("level", "micro")
    description = role.get("description", "").strip()
    # The body is about to be framed as "binding instructions", so it may not
    # carry maintainer comments, generation banners, or entries that
    # contradict okuro's own routing and deliverable rules. See
    # roles/body_sanitize.py for what is removed and why the catalog alone
    # cannot be the fix.
    from okuro.roles.body_sanitize import sanitize_role_body

    content = sanitize_role_body(role.get("content", "").strip())

    stale = maintenance.get("stale", False)
    stats = maintenance.get("stats") or {}
    mandate = maintenance.get("mandate")

    header_bits = [f"## Role Assumption: {role_id}"]
    if domain:
        header_bits[0] += f" ({domain})"

    lines = [
        header_bits[0],
        "",
        f"You are now operating as the **{role_id}** role. Adopt the "
        "operating principles below as your working context for this "
        "task — treat them as binding instructions, not reference material.",
        "",
        f"- **Domain:** {domain or 'n/a'}",
        f"- **Tier / model:** {tier} ({model})",
        f"- **Prompt level:** {level}",
    ]
    if description:
        lines.append(f"- **Summary:** {description}")

    if stale:
        # "role body is returned so routine work can proceed" was a licence,
        # and it let the agent grade its own task as routine. The body is
        # returned either way; that is a fact about the response, not a
        # permission the agent needs stated.
        lines.append(
            "- **Knowledge freshness:** STALE. Call "
            f"`roles_maintenance('{role_id}')` and act on the research "
            "mandate before any high-stakes execution in this role."
        )
    else:
        lines.append("- **Knowledge freshness:** current")

    if stats:
        entry_count = (
            stats.get("total_entries")
            or stats.get("learnings")
            or 0
        )
        if entry_count:
            lines.append(
                f"- **Accumulated learnings:** {entry_count} "
                "(inspect via `roles_knowledge`)"
            )

    lines.extend(["", "### Role Directive", "", content or "(empty role body)"])

    if mandate:
        lines.extend(
            [
                "",
                # "(advisory)" labelled this section non-binding INSIDE a
                # payload whose opening line says "treat them as binding
                # instructions" — the document contradicted itself 40 lines
                # apart, and the agent resolves that toward the weaker half.
                "### Maintenance Mandate — research required before "
                "high-stakes execution",
                "",
                "```json",
                json.dumps(mandate, indent=2, default=str),
                "```",
            ]
        )

    return "\n".join(lines)


def _format_role_match(match: dict, task: str) -> str:
    """Render a roles_match result as an actionable next-step directive.

    Leads with the recommendation and the exact next tool call so the
    model closes the loop instead of stopping at "here is a list".
    """
    matched_id = match.get("id", "general")
    domain = match.get("domain", "")
    similarity = match.get("similarity", 0.0)
    match_type = match.get("match_type", "matched")
    alternatives = match.get("alternatives") or []

    lines = [
        f"## Role Match for: {task[:120]}",
        "",
        f"**Best match:** `{matched_id}`"
        + (f" ({domain})" if domain else "")
        + f" — similarity {similarity:.3f}, match_type={match_type}",
    ]

    if match_type == "fallback":
        lines.append(
            "- No domain role scored above the similarity threshold. "
            "The general-purpose role is returned; proceed without role "
            "adoption unless a better fit is explicit."
        )
    else:
        lines.extend(
            [
                "",
                f"**Next step:** call `roles_get('{matched_id}')` to adopt "
                "the role. The returned body is your binding operating "
                "context for the remainder of this task.",
            ]
        )

    if alternatives:
        alt_lines = ["", "**Alternatives:**"]
        for alt in alternatives:
            alt_lines.append(
                f"- `{alt['id']}` ({alt.get('domain', '')}) "
                f"— similarity {alt.get('similarity', 0):.3f}"
            )
        lines.extend(alt_lines)

    lines.extend(
        [
            "",
            "---",
            "",
            "```json",
            json.dumps(match, indent=2, default=str),
            "```",
        ]
    )
    return "\n".join(lines)


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="roles_list",
            description="List available expert roles.",
            inputSchema={
                "type": "object",
                "properties": {"domain": {"type": "string", "description": "Filter by domain"}},
            },
        ),
        Tool(
            name="roles_get",
            description=(
                # Tool descriptions load BEFORE any packet, in every session,
                # including Agent-tool subagents that never bootstrap at all.
                # "Advisory — the role body is always returned so work can
                # proceed" was the only hedge in ~400 descriptions, and it sat
                # on the tool whose payload says "treat this as binding".
                "Get full role definition. The returned body is your binding "
                "operating context for the task. If the role's knowledge is "
                "stale (per its maintenance_schedule), the response includes a "
                "`maintenance` block naming the research required before "
                "high-stakes execution."
            ),
            inputSchema={
                "type": "object",
                "properties": {"role_id": {"type": "string"}},
                "required": ["role_id"],
            },
        ),
        Tool(
            name="roles_match",
            description="Find best expert role for a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description"},
                    "top_k": {"type": "integer", "default": 3},
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="roles_domains",
            description="List available role domains.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="roles_learn",
            description=(
                "Persist a learning for a role. Types: insight, pitfall, "
                "source, decision, research. Writes of type `research` or "
                "`source` reset the role's weekly-research staleness clock; "
                "other types are opportunistic and do not. Pass `source_url` "
                "for research provenance. Pass `supersedes=<entry_id>` to "
                "mark an obsolete entry as replaced."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "role_id": {"type": "string"},
                    "learning": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["insight", "pitfall", "source", "decision", "research"],
                        "default": "research",
                    },
                    "source_url": {"type": "string"},
                    "session_id": {"type": "string"},
                    "supersedes": {"type": "string"},
                },
                "required": ["role_id", "learning"],
            },
        ),
        Tool(
            name="roles_maintenance",
            description=(
                "Return a research mandate for a stale role (or for all "
                "stale roles if role_id is omitted). The mandate contains "
                "domain-tuned search queries, known sources, and current "
                "knowledge stats. Call roles_learn(role_id, ...) for each "
                "finding — the last_maintained timestamp updates "
                "automatically on research/source writes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "role_id": {
                        "type": "string",
                        "description": "Omit for all stale roles",
                    },
                },
            },
        ),
        Tool(
            name="roles_knowledge",
            description=(
                "Inspect persisted knowledge entries for a role. Semantic "
                "search via task_hint, or most-recent if omitted. Debug / "
                "audit — agents normally get this injected automatically "
                "by the orchestrator dispatcher."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "role_id": {"type": "string"},
                    "task_hint": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["role_id"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "roles_list":
        from okuro.roles import list_roles
        return _text(list_roles(domain=arguments.get("domain")))

    if name == "roles_get":
        import os as _os
        from okuro.roles import get_role
        from okuro.roles.knowledge import is_stale, get_knowledge_stats
        from okuro.roles.maintainer import get_maintenance_mandate

        role_id = arguments["role_id"]
        role = get_role(role_id)
        if not role:
            return _text(f"Role not found: {role_id}")

        stale = is_stale(role_id)
        maintenance = {
            "stale": stale,
            "stats": get_knowledge_stats(role_id),
            "mandate": get_maintenance_mandate(role_id) if stale else None,
        }
        role["maintenance"] = maintenance

        # M4 telemetry — when called from inside a dispatched subagent
        # (OKURO_TASK_ID + OKURO_SUBTASK_ID env vars set by the
        # orchestrator's _build_subagent_env), emit role_body_fetched into
        # the originating task's event log so per-spawn accounting closes
        # the loop: total_input = role_slice.metadata + Σ(role_body_fetched).
        # Best-effort: telemetry failure must never break role adoption.
        # Work identity comes from the RESOLVER, never os.environ. The live
        # MCP transport is HTTP to ONE shared daemon whose environment belongs
        # to no subagent, so an env read here is False on every real call and
        # this telemetry silently never fires. F1 fixed the three sites its
        # evidence named; this one survived. Measured 2026-08-03 on
        # task-20260801-124606: bootstrap_sizes 0, role_body_fetched 0, while
        # role_slice (emitted dispatcher-side, which DOES have the env) fired.
        from okuro.sense.work_identity import resolve_work_identity as _rwi

        _wi = _rwi()
        _task_id = (_wi.task_id if _wi else "") or ""
        _subtask_id = (_wi.subtask_id if _wi else "") or ""
        if _task_id and _subtask_id:
            try:
                from okuro.sense.task_events import append_event
                _body_text = role.get("content") or ""
                append_event(
                    task_id=_task_id,
                    subtask_id=_subtask_id,
                    from_role=role_id,
                    event_type="role_body_fetched",
                    body={
                        "role_id": role_id,
                        "level": role.get("level") or "lean",
                        "body_bytes": len(_body_text.encode("utf-8")),
                    },
                    created_by="roles_get.mcp",
                )
            except Exception:
                pass

        return _text(_format_role_assumption(role, maintenance))

    if name == "roles_match":
        from okuro.roles.resolver import match_role
        task = arguments["task"]
        match = match_role(task)
        return _text(_format_role_match(match, task))

    if name == "roles_domains":
        from okuro.roles import get_domains
        return _text(get_domains())

    if name == "roles_learn":
        from okuro.roles.knowledge import write_knowledge
        return _text(write_knowledge(
            role_id=arguments["role_id"],
            learning=arguments["learning"],
            type=arguments.get("type", "research"),
            source_url=arguments.get("source_url"),
            session_id=arguments.get("session_id"),
            supersedes=arguments.get("supersedes"),
        ))

    if name == "roles_maintenance":
        from okuro.roles.maintainer import get_maintenance_mandate
        return _text(get_maintenance_mandate(role_id=arguments.get("role_id")))

    if name == "roles_knowledge":
        from okuro.roles.knowledge import read_knowledge
        return _text(read_knowledge(
            role_id=arguments["role_id"],
            task_hint=arguments.get("task_hint"),
            limit=arguments.get("limit", 5),
        ))

    return _text(f"Unknown tool: {name}")
