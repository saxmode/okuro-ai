# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Roles module tools — role framework.
# index: imports | def _text | def create_role | def promote_role | def get_tools | async def handle_tool | def _handle_action_tool
# AGENT_HEADER_END -->
"""Roles module tools — role framework.

Extracted from roles/mcp_server.py for use by the unified okuro.mcp.server.
"""

import json
import logging
import re

from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _canonical_tiers() -> tuple[str, ...]:
    """The tier vocabulary — one helper, in the vocabulary module.

    This used to be a private copy here and a second private copy in
    ``orchestrator.capability_gap``, which is the same retyping this whole
    change exists to stop, one level up: two helpers that read the same
    constant today and drift the moment either grows a filter.
    """
    from okuro.roles.vocabulary import canonical_tiers

    return canonical_tiers()


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


#: A role id is used verbatim as a dict key, a URL path segment, a CLI
#: argument and — when the orchestrator demanded the role by name — as the
#: token the panel re-includes the task by. Kebab-case is what all of those
#: survive; the vocabulary gate checks tiers and tool names and has never
#: looked at the id.
_ROLE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _stored_maturity(role_id: str) -> str | None:
    """The maturity the TABLE holds, or None when there is no such role.

    Every response reports from here rather than echoing what the caller
    asked for. The two agree today; reading the row is what keeps a future
    change to the write path from turning a response into a claim about an
    intention instead of a fact about the store.
    """
    from okuro.db import get_db

    row = get_db().fetchone(
        "SELECT maturity FROM roles WHERE role_id = ?", (role_id,)
    )
    return row["maturity"] if row else None


def create_role(arguments: dict) -> dict | str:
    """Birth a role row from an MCP call, or refuse and write nothing.

    This closes a gap, not a convenience: the two functions that insert a role
    (``designer.store_designed_role``, ``drafter.draft_role``) had no MCP verb,
    and the only other birth path is ``POST /api/roles`` — localhost HTTP,
    which a dispatched subagent has no client for. The Phase 0 role-designer
    was therefore instructed to persist via ``okuro.roles.registry``, a module
    with no INSERT at all. An instruction that names a module which cannot
    perform the act it commands is the class; exposing the verb is the fix.

    WHY THE STRUCTURE CHECK RUNS BEFORE THE WRITE. ``maturity='active'`` makes
    the role reachable by ``roles_match`` the moment the row lands, so an
    incomplete body would be handed to an adopting agent as binding operating
    context. "Refuse" therefore has to mean nothing was written — validating
    after the upsert would leave the bad row live while reporting a refusal.
    ``draft`` is the opposite case on purpose: it is invisible to matching, so
    an incomplete body is a work item rather than a hazard, and the missing
    sections come back as ``structure_advisory`` so the authoring agent can
    see what it still owes.

    Refusals are returned as ``REJECTED: ...`` text rather than raised, which
    is how every other refusing okuro verb reports (``artifact_write``,
    ``delivery_send``, ``role_diary_write``): an exception crossing the MCP
    boundary reaches the agent as a transport error with no remedy in it.
    """
    from okuro.roles.designer import DOMAIN_SCHEDULES, gate_role_body
    from okuro.roles.write import RoleExists, RoleWriteRefused, insert_role

    role_id = (arguments.get("role_id") or "").strip()
    if not _ROLE_ID_RE.match(role_id):
        return (
            f"REJECTED: roles_create — role_id {role_id!r} is not kebab-case "
            "(lowercase letters, digits and single hyphens, e.g. "
            "'legal-advisor'). The id is the routing key on every surface "
            "that dispatches this role."
        )

    domain = (arguments.get("domain") or "").strip()
    if not domain:
        return (
            f"REJECTED: roles_create({role_id}) — domain is required. Call "
            "`roles_domains` for the domains already in use."
        )

    description = (arguments.get("description") or "").strip()
    if not description:
        return (
            f"REJECTED: roles_create({role_id}) — description is required. "
            "It is the ONLY text that gets embedded, so a role without one "
            "can never be found by `roles_match`."
        )

    maturity = (arguments.get("maturity") or "draft").strip().lower()
    if maturity not in ("draft", "active"):
        return (
            f"REJECTED: roles_create({role_id}) — maturity {maturity!r} is "
            "not one of ('draft', 'active')."
        )

    # The pointer guard AND the structure audit, through the one shared gate.
    # The pointer half used to be missing here while store_designed_role and
    # draft_role both had it: an agent that had just written a long artifact
    # could create a role whose whole body was "see artifact <id>", and only
    # this surface would take it.
    gate = gate_role_body(
        {
            "full": arguments.get("prompt") or "",
            "lean": arguments.get("lean_prompt") or "",
            "micro": arguments.get("micro_prompt") or "",
        }
    )
    if gate["error"]:
        return f"REJECTED: roles_create({role_id}) — {gate['error']}"

    if maturity == "active" and not gate["ok"]:
        return (
            f"REJECTED: roles_create({role_id}) — maturity='active' requires "
            "a body that passes the canonical structure audit in all three "
            "grades. NOTHING WAS WRITTEN.\n"
            + "\n".join(f"  - missing {line}" for line in gate["flat_missing"])
            + "\nEither supply the missing sections, or create the role with "
            "maturity='draft' and promote it with roles_promote once the "
            "body is complete."
        )

    try:
        written = insert_role(
            {
                "role_id": role_id,
                "domain": domain,
                "description": description,
                "tier": arguments.get("tier"),
                "model": arguments.get("model"),
                "tools": arguments.get("tools"),
                "prompt": arguments.get("prompt"),
                "lean_prompt": arguments.get("lean_prompt"),
                "micro_prompt": arguments.get("micro_prompt"),
                "panel_eligible": arguments.get("panel_eligible"),
                # The schedule a domain implies, the way store_designed_role
                # has always done it. Falling back to the column default
                # ('monthly') would have put every agent-created engineering
                # or quality role on a third of the review cadence its
                # hand-made peers get.
                "maintenance_schedule": (
                    arguments.get("maintenance_schedule")
                    or DOMAIN_SCHEDULES.get(domain, "monthly")
                ),
                "maturity": maturity,
            },
            actor="mcp:roles_create",
        )
    except RoleExists:
        # The DATABASE decided this, in the same statement that would have
        # written the row. A SELECT first and an upsert second would have
        # overwritten a role created between the two.
        return (
            f"REJECTED: roles_create({role_id}) — a role with this id already "
            "exists and NOTHING WAS WRITTEN. This verb never overwrites a "
            f"role. Read it with roles_get('{role_id}') and decide: edit it "
            "through PUT /api/roles/{id}, or pick a different id."
        )
    except RoleWriteRefused as exc:
        return (
            f"REJECTED: roles_create({role_id}) — the role vocabulary gate "
            "refused this row. NOTHING WAS WRITTEN.\n"
            + "\n".join(f"  - {p}" for p in exc.problems)
        )
    except ValueError as exc:
        return f"REJECTED: roles_create({role_id}) — {exc}"

    # Report the maturity the TABLE holds, never the one that was asked for.
    # They are the same today; saying so from the row is what keeps them the
    # same after the next change to the write path.
    stored = _stored_maturity(role_id) or maturity

    if stored == "draft":
        hint = (
            f"{role_id} is a DRAFT: invisible to roles_match and to the "
            "deliberation panel until it is promoted. Call "
            f"roles_promote('{role_id}') when the body is complete. The "
            "promote button in the web UI does the same thing."
        )
    else:
        hint = (
            f"{role_id} is ACTIVE: roles_match can route to it now, and the "
            "planner may propose it for a panel."
        )

    payload: dict = {
        "role_id": written["role_id"],
        "created": written["created"],
        "maturity": stored,
        "embedded": written["embedded"],
        "tags": written["tags"],
        "structure": {
            "ok": gate["ok"],
            "missing": gate["missing"],
            "advisory": gate["advisory"],
        },
        "hint": hint,
    }
    if not gate["ok"]:
        # Draft only, by construction: the active path refused above. Named
        # `structure_missing` and not `structure_advisory` because these are
        # the REQUIRED sections — the non-gating ones are one level down,
        # under structure.advisory, and one word for both made the gating list
        # read as optional.
        payload["structure_missing"] = gate["flat_missing"]
    return payload


def promote_role(arguments: dict) -> dict | str:
    """Make a draft role active, or refuse and change nothing.

    Thin over ``write.promote_role``, which owns the gate, the vector and the
    tags. Thin is the point: the previous shape of this defect was three
    promotion paths each deciding for itself what promotion guarantees.

    Until this verb existed an MCP-only agent could CREATE a role and then had
    no way to finish it. The draft hint pointed at an HTTP endpoint it has no
    client for and a Python function it cannot import — the same "an
    instruction that names a path the caller cannot use" that the creation
    verb was built to fix, reintroduced one step later in the same workflow.
    """
    from okuro.roles.write import RolePromotionRefused
    from okuro.roles.write import promote_role as _promote

    role_id = (arguments.get("role_id") or "").strip()
    if not role_id:
        return "REJECTED: roles_promote — role_id is required."

    current = _stored_maturity(role_id)
    if current is None:
        return (
            f"REJECTED: roles_promote({role_id}) — no such role. Create it "
            "with roles_create first."
        )
    if current == "active":
        return {
            "role_id": role_id,
            "maturity": "active",
            "status": "already_active",
            "hint": f"{role_id} was already active; nothing to do.",
        }

    try:
        result = _promote(role_id, actor="mcp:roles_promote")
    except RolePromotionRefused as exc:
        return (
            f"REJECTED: roles_promote({role_id}) — the stored body does not "
            "pass the canonical structure audit, so the role stays a DRAFT "
            "and NOTHING WAS CHANGED.\n"
            + "\n".join(f"  - missing {line}" for line in exc.missing)
            + "\nFill these in through PUT /api/roles/"
            + role_id
            + " (or the web UI), then call roles_promote again."
        )
    except KeyError:
        return f"REJECTED: roles_promote({role_id}) — no such role."

    return {
        "role_id": role_id,
        "maturity": _stored_maturity(role_id) or "active",
        "status": "promoted",
        "embedded": result["embedded"],
        "tags": result["tags"],
        "structure": result["structure"],
        "hint": (
            f"{role_id} is ACTIVE: roles_match can route to it now, and the "
            "planner may propose it for a panel."
        ),
    }


def get_tools() -> list[Tool]:
    # Imported HERE rather than at module scope because this module is
    # imported by the MCP server before the database exists, and `actions`
    # is cheap but the house style in this file is local imports.
    from okuro.roles.actions import (
        AGENT_PROPOSABLE_KINDS as _PROPOSABLE,
        KINDS as ACTION_KINDS,
    )

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
                "high-stakes execution. `level` picks the fidelity grade; "
                "'full' is the on-demand references tier and is the only way "
                "to read a body that dispatch truncated."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "role_id": {"type": "string"},
                    "level": {
                        "type": "string",
                        "enum": ["micro", "lean", "full"],
                        "default": "micro",
                        "description": (
                            "Fidelity grade to return. Falls back down the "
                            "chain when the requested grade is empty, and "
                            "the response reports the grade actually served."
                        ),
                    },
                },
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
                "mark an obsolete entry as replaced.\n\n"
                "A NO-CHANGE REPORT IS A CHECK, NOT KNOWLEDGE. If you looked "
                "and nothing material had moved, pass outcome='no_change' "
                "(use 'error' if you could not look, 'skipped' if you did "
                "not). That is the CORRECT and expected output of a "
                "maintenance sweep — it is not a failure and you must not "
                "manufacture a finding to avoid it. The entry is filed in "
                "role_knowledge_checks: the role's clock still resets for "
                "'no_change', but nothing is embedded and nothing is counted "
                "as a learning, because nothing was learned.\n\n"
                "Pass `run_id` together with `source_url` when the body of "
                "that URL was stored during this run (see "
                "POST /api/roles/knowledge-fetch). Only then does the entry "
                "earn fetch_verified — a source_url on its own is a claim the "
                "store cannot check."
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
                    "outcome": {
                        "type": "string",
                        "enum": ["no_change", "error", "skipped"],
                        "description": (
                            "Set when this entry REPORTS AN OUTCOME rather "
                            "than carrying a fact. 'no_change' = looked, "
                            "nothing moved (resets the clock). 'error' = "
                            "tried to look and could not. 'skipped' = did not "
                            "look. Neither 'error' nor 'skipped' resets the "
                            "clock, so a broken feed stays visible instead of "
                            "reading as six quiet weeks. Omit it for a real "
                            "finding."
                        ),
                    },
                    "run_id": {
                        "type": "string",
                        "description": (
                            "The maintenance run this entry belongs to. "
                            "Required for fetch_verified; without it a "
                            "source_url stays an unchecked claim."
                        ),
                    },
                },
                "required": ["role_id", "learning"],
            },
        ),
        Tool(
            name="roles_create",
            description=(
                "Create a NEW role in the roles table. This is the only "
                "birth path an MCP-only agent has: `roles_learn` writes "
                "knowledge entries, never a role, and POST /api/roles is "
                "localhost HTTP that a dispatched subagent cannot reach. "
                "Refuses an id that already exists — read it with "
                "`roles_get` and edit it through the API instead. Roles are "
                "born `draft` (invisible to `roles_match` and to the "
                "deliberation panel) unless you pass maturity='active', "
                "which requires a body that passes the canonical structure "
                "audit in all three grades."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "role_id": {
                        "type": "string",
                        "description": (
                            "kebab-case identifier, e.g. 'legal-advisor'. "
                            "When the orchestrator demanded a role by name, "
                            "use that name VERBATIM — the panel re-includes "
                            "the task by this exact id."
                        ),
                    },
                    "domain": {
                        "type": "string",
                        # Deliberately a free string and not an enum, matching
                        # `roles_list(domain)` above. `get_domains()` is a
                        # SELECT DISTINCT over the live table, but tool
                        # schemas are sent once at handshake — an enum built
                        # from it would freeze the domain list at boot AND
                        # make a genuinely new domain unexpressible. The
                        # domain of a role is content, not vocabulary: the
                        # gate in roles/vocabulary.py checks tiers and tool
                        # names, never domains.
                        "description": (
                            "Domain classification. Call `roles_domains` for "
                            "the domains already in use; a new one is "
                            "allowed but should be a deliberate choice."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "One or two sentences naming purpose and "
                            "expertise. This is the ONLY text that gets "
                            "embedded, so it is what `roles_match` searches "
                            "— a vague description makes an unroutable role."
                        ),
                    },
                    "tier": {
                        "type": "string",
                        # Rendered from CANONICAL_TIERS, never retyped: the
                        # 22 non-canonical tiers in the live table came from
                        # a hand-written list in a prompt that drifted.
                        "enum": list(_canonical_tiers()),
                        "default": "standard",
                        "description": "Canonical dispatch tier.",
                    },
                    "model": {
                        "type": "string",
                        "default": "sonnet",
                        "description": "Provider model name, e.g. sonnet.",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Tool names the role may use. okuro verbs are "
                            "checked against the live MCP registry and a "
                            "retired alias is refused."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "FULL grade body (markdown). Required sections: "
                            "IDENTITY, Archetype, Traits (0-100) table, "
                            "Neurotype Balance, COGNITIVE PROFILE, "
                            "EXPERTISE, PROTOCOL, TOOLS."
                        ),
                    },
                    "lean_prompt": {
                        "type": "string",
                        "description": (
                            "LEAN grade body (markdown). Required sections: "
                            "CORE, PROTOCOL. No persona block."
                        ),
                    },
                    "micro_prompt": {
                        "type": "string",
                        "description": (
                            "MICRO grade body — flat YAML, required keys "
                            "`purpose` and `expertise`."
                        ),
                    },
                    "maturity": {
                        "type": "string",
                        "enum": ["draft", "active"],
                        "default": "draft",
                        "description": (
                            "'draft' may be structurally incomplete and is "
                            "invisible to matching until promoted; 'active' "
                            "is refused unless all three grades pass the "
                            "structure audit."
                        ),
                    },
                    "panel_eligible": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Whether the role may be proposed for a "
                            "deliberation panel."
                        ),
                    },
                    "maintenance_schedule": {
                        "type": "string",
                        "enum": ["weekly", "biweekly", "monthly"],
                        "default": "monthly",
                        "description": "How often the role's knowledge goes stale.",
                    },
                },
                "required": ["role_id", "domain", "description"],
            },
        ),
        Tool(
            name="roles_promote",
            description=(
                "Promote a draft role to active, making it reachable by "
                "roles_match and proposable to the deliberation panel. "
                "REFUSED unless the role's STORED body passes the canonical "
                "structure audit in all three grades — the refusal names "
                "every missing section and changes nothing. This is the MCP "
                "counterpart of the promote button in the web UI; an "
                "already-active role is a no-op, not an error."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "role_id": {
                        "type": "string",
                        "description": "The draft role to promote.",
                    },
                },
                "required": ["role_id"],
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
        Tool(
            name="roles_actions_list",
            description=(
                "List structural role-maintenance actions — the backlog of "
                "changes to what a role IS, as opposed to what it knows. "
                "Filter by `state` (proposed, researched, planned, critiqued, "
                "approved, implemented, verified, rejected, superseded) or by "
                "`kind` ('finding' from the researcher, 'source_health' from "
                "the poller, 'internal' from okuro's own audit of its store)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    # From the module, not a literal: a kind that can be opened
                    # and then not listed is invisible in the panel the
                    # approval happens in.
                    "kind": {"type": "string", "enum": list(ACTION_KINDS)},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="roles_actions_get",
            description=(
                "One structural action with its full append-only event log — "
                "every state it passed through, who moved it and why, "
                "including the moves backwards."
            ),
            inputSchema={
                "type": "object",
                "properties": {"action_id": {"type": "string"}},
                "required": ["action_id"],
            },
        ),
        Tool(
            name="roles_actions_propose",
            description=(
                "Open a structural finding for consideration. The gate is "
                "CODE, not a criterion, and which code depends on `kind`. "
                "kind='finding' (the default) is about an EXTERNAL source: "
                "the quoted sentence must appear in the body okuro stored for "
                "that run and source (six-word minimum). kind='internal' is "
                "okuro measuring ITSELF — no source, no quote; instead "
                "`evidence_artifact_id` must name an artifact that exists, "
                "`okuro_element` must name a section label or micro key, and "
                "every id in `affected_role_ids` is RE-MEASURED here: a role "
                "that already carries the element refuses the whole proposal. "
                "`okuro_element` must in both cases name something that "
                "exists — a designer constant, a section label, a fidelity "
                "grade or a `roles` column. A refusal writes nothing and says "
                "which gate said no. Agents propose; only the human approves."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "One line naming what changed.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(_PROPOSABLE),
                        "description": (
                            "'finding' (default) needs source_id, run_id and "
                            "quoted_sentence. 'internal' needs "
                            "evidence_artifact_id, okuro_element and "
                            "affected_role_ids instead. 'source_health' is "
                            "NOT reachable here: an agent that could mint one "
                            "could report a feed as broken without ever "
                            "fetching it."
                        ),
                    },
                    "evidence_artifact_id": {
                        "type": "string",
                        "description": (
                            "kind='internal' only: the artifact holding the "
                            "measurement. It must exist — an internal finding "
                            "whose evidence cannot be opened is a citation to "
                            "nothing."
                        ),
                    },
                    "source_id": {"type": "string"},
                    "run_id": {
                        "type": "string",
                        "description": (
                            "The poll run whose stored body the quote is "
                            "checked against. A quote from any other run has "
                            "nothing behind it."
                        ),
                    },
                    "quoted_sentence": {
                        "type": "string",
                        "description": (
                            "Verbatim from the stored body. Not a paraphrase: "
                            "the check is a substring assert, and a matcher "
                            "loose enough to accept a rewording is loose "
                            "enough to accept an invention."
                        ),
                    },
                    "okuro_element": {
                        "type": "string",
                        "description": (
                            "The ONE okuro element this bears on, e.g. "
                            "'roles.designer.FULL_REQUIRED', 'EXPERTISE', "
                            "'micro', 'roles.tier'."
                        ),
                    },
                    "evidence_url": {"type": "string"},
                    "affected_role_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Explicit role ids. A count is refused — it cannot "
                            "be checked, diffed or wrong in a way anyone notices."
                        ),
                    },
                    "research_artifact_id": {"type": "string"},
                    "created_by": {"type": "string"},
                    "reason": {"type": "string"},
                },
                # `okuro_element` is required for both kinds; the source/quote
                # trio is required only for 'finding' and is checked in the
                # dispatch, because JSON Schema cannot express "these three
                # unless kind='internal'" in a form every MCP client renders.
                "required": ["title", "okuro_element"],
            },
        ),
        Tool(
            name="roles_actions_transition",
            description=(
                "Move a structural action along its state machine. "
                "`approved` is NOT reachable here and that is deliberate: it "
                "is the single human decision gate and it lives behind a "
                "loopback endpoint. Reachable: researched, planned, critiqued "
                "(each needs its artifact id), implemented (needs a migration "
                "id or a diff artifact), verified (needs before/after fit "
                "snapshots under the same rubric), rejected and superseded "
                "(each needs a reason)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "to_state": {
                        "type": "string",
                        "enum": [
                            "researched", "planned", "critiqued",
                            "implemented", "verified", "rejected",
                            "superseded",
                        ],
                    },
                    "actor": {"type": "string"},
                    "reason": {"type": "string"},
                    "artifact_id": {
                        "type": "string",
                        "description": (
                            "The artifact this state claims exists — research, "
                            "plan, critique, or the diff for implemented."
                        ),
                    },
                    "migration_id": {"type": "string"},
                    "affected_role_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "fit_before": {"type": "array", "items": {"type": "object"}},
                    "fit_after": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["action_id", "to_state", "actor"],
            },
        ),
        # ── the update pipeline ──────────────────────────────────────
        #
        # Five verbs, one per step, and `approve` is not among them for the
        # same reason it is not among the transitions: it is the owner's. The
        # implement verb is REACHABLE by an agent and refuses on any state but
        # `approved`, which makes it the fourth fence rather than a way past
        # the other three.
        Tool(
            name="roles_update_plan",
            description=(
                "Compute what an approved-track structure action would change, "
                "and take the fit baseline it will later be judged against. "
                "Runs on a `researched` row. `instructions` carries the "
                "operations — add_section, rename_section, remove_section, "
                "rewrite_block, sweep_string, set_field, rebuild_grade — and "
                "an operation outside that vocabulary is refused rather than "
                "approximated. Writes a plan artifact and moves the row to "
                "`planned`. NOTHING in the store is changed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "actor": {"type": "string"},
                    "instructions": {
                        "type": "object",
                        "description": (
                            "{\"operations\": [{\"op\": ..., ...}]}. Omit to "
                            "re-plan with the operations already stored — "
                            "which is a repeat of the same instruction, not a "
                            "second chance to write a different one."
                        ),
                    },
                },
                "required": ["action_id", "actor"],
            },
        ),
        Tool(
            name="roles_update_critique",
            description=(
                "Hand the stored plan to a model that owns none of it, in "
                "chunks under the size a single bridge call reliably returns "
                "from. A chunk that times out is reported as a failure and "
                "never as a clean pass. Moves a `planned` row to `critiqued`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "actor": {"type": "string"},
                },
                "required": ["action_id", "actor"],
            },
        ),
        Tool(
            name="roles_update_dry_run",
            description=(
                "The diff a human approves: explicit role ids grouped by "
                "carrier (migration-carried vs database-only), five COMPLETE "
                "before/after bodies, budgets and gate readings on both sides, "
                "and the guard's answer for every body. Stored as an evidence "
                "artifact and linked on the row. Changes no state and writes "
                "no role."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "samples": {"type": "integer", "default": 5},
                },
                "required": ["action_id"],
            },
        ),
        Tool(
            name="roles_update_implement",
            description=(
                "Apply the approved plan. REFUSES unless the row is "
                "`approved` — approval is the owner's and no agent verb reaches "
                "it. Snapshots the store first, emits a migration for the "
                "roles a migration carries whose bodies the CONTENT guard "
                "passes, writes the rest directly, and moves the row to "
                "`implemented`. The generated migration file is left in the "
                "worktree for review; it is not applied here."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "actor": {"type": "string"},
                    "worktree": {
                        "type": "string",
                        "description": (
                            "Where the generated migration is written. "
                            "Defaults to the tree this okuro is running from."
                        ),
                    },
                },
                "required": ["action_id", "actor"],
            },
        ),
        Tool(
            name="roles_update_verify",
            description=(
                "Re-score the affected roles and compare against the baseline "
                "stored at plan time. The caller supplies no numbers: that is "
                "the point, because the party asking to be verified must not "
                "choose what it is measured against. Moves `implemented` to "
                "`verified`, or leaves it implemented with the refusal recorded."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "actor": {"type": "string"},
                },
                "required": ["action_id", "actor"],
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
        # `level` defaults to micro, which is what this call did before the
        # parameter existed. It is not decoration: `resolve_role` caps an
        # inlined body at ROLE_BODY_INLINE_CAP and tells the agent the rest is
        # available via roles_get(level="full") — advice that was impossible
        # to follow while this tool had no way to ask for a grade.
        role = get_role(role_id, level=arguments.get("level") or "micro")
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
            outcome=arguments.get("outcome"),
            run_id=arguments.get("run_id"),
        ))

    if name == "roles_create":
        return _text(create_role(arguments))

    if name == "roles_promote":
        return _text(promote_role(arguments))

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

    if name.startswith("roles_actions_"):
        return _text(_handle_action_tool(name, arguments))

    if name.startswith("roles_update_"):
        return _text(_handle_update_tool(name, arguments))

    return _text(f"Unknown tool: {name}")


def _handle_action_tool(name: str, arguments: dict):
    """The four action verbs, sharing one refusal shape.

    A refusal comes back as ``{"refused": "<the gate that said no>"}`` rather
    than as an exception, because an MCP error is rendered to the agent as a
    tool failure and the agent retries it identically. A refusal with the
    reason in it is a refusal the agent can act on — and every message from
    ``ActionRefused`` names the gate.
    """
    from okuro.db import get_db
    from okuro.roles.actions import (
        AGENT_REACHABLE_STATES,
        ActionRefused,
        get_action,
        list_actions,
        propose,
        transition,
    )

    db = get_db()

    try:
        if name == "roles_actions_list":
            return list_actions(
                db,
                state=arguments.get("state"),
                kind=arguments.get("kind"),
                limit=arguments.get("limit", 50),
            )

        if name == "roles_actions_get":
            row = get_action(db, arguments["action_id"])
            return row or {"refused": f"no action {arguments['action_id']}"}

        if name == "roles_actions_propose":
            # `source_health` STAYS UNREACHABLE. It was the whole reason `kind`
            # used to be absent here: a source_health row is the poller's
            # observation about a feed, and an agent that could mint one could
            # report a source as broken without ever fetching it. `internal`
            # does not carry that hazard — it cannot be minted either, because
            # propose re-measures every role it names.
            kind = arguments.get("kind") or "finding"
            if kind not in ("finding", "internal"):
                return {
                    "refused": (
                        f"kind {kind!r} is not proposable over MCP. 'finding' "
                        f"needs a verified quote, 'internal' a reproducible "
                        f"measurement; 'source_health' belongs to the poller, "
                        f"which is the only thing that has fetched the feed."
                    )
                }
            if kind == "finding":
                missing = [k for k in ("source_id", "run_id", "quoted_sentence")
                           if not arguments.get(k)]
                if missing:
                    return {
                        "refused": (
                            f"kind='finding' needs {', '.join(missing)} — the "
                            f"quote is checked against the body that run "
                            f"fetched, and without all three there is no "
                            f"stored body to check it against. An internal "
                            f"audit with no vendor source is kind='internal'."
                        )
                    }
            return propose(
                db,
                kind=kind,
                title=arguments["title"],
                created_by=arguments.get("created_by") or "mcp-agent",
                source_id=arguments.get("source_id"),
                run_id=arguments.get("run_id"),
                evidence_url=arguments.get("evidence_url"),
                quoted_sentence=arguments.get("quoted_sentence"),
                okuro_element=arguments["okuro_element"],
                affected_role_ids=arguments.get("affected_role_ids"),
                research_artifact_id=arguments.get("research_artifact_id"),
                evidence_artifact_id=arguments.get("evidence_artifact_id"),
                reason=arguments.get("reason"),
            )

        if name == "roles_actions_transition":
            to_state = arguments["to_state"]
            if to_state not in AGENT_REACHABLE_STATES:
                return {
                    "refused": (
                        f"{to_state!r} is not reachable over MCP. Approval is "
                        f"the single human decision gate; it lives on a "
                        f"loopback endpoint and no agent verb reaches it."
                    )
                }
            return transition(
                db,
                arguments["action_id"],
                to_state,
                actor=arguments["actor"],
                reason=arguments.get("reason"),
                artifact_id=arguments.get("artifact_id"),
                migration_id=arguments.get("migration_id"),
                affected_role_ids=arguments.get("affected_role_ids"),
                fit_before=arguments.get("fit_before"),
                fit_after=arguments.get("fit_after"),
            )
    except ActionRefused as exc:
        return {"refused": str(exc)}
    except KeyError as exc:
        return {"refused": f"missing required argument: {exc}"}

    return {"refused": f"Unknown tool: {name}"}


#: What each pipeline verb REQUIRES, checked before the call rather than
#: caught after it. See :func:`_handle_update_tool`.
_UPDATE_REQUIRED: dict[str, tuple[str, ...]] = {
    "roles_update_plan": ("action_id", "actor"),
    "roles_update_critique": ("action_id", "actor"),
    "roles_update_dry_run": ("action_id",),
    "roles_update_implement": ("action_id", "actor"),
    "roles_update_verify": ("action_id", "actor"),
}


def _handle_update_tool(name: str, arguments: dict):
    """The five pipeline verbs, sharing the action verbs' refusal shape.

    ``{"refused": …}`` rather than an exception, for the reason
    ``_handle_action_tool`` already states: an MCP error renders as a tool
    failure and the agent retries it identically, while a refusal carrying the
    gate that said no is something the agent can act on.

    ARGUMENTS ARE CHECKED UP FRONT, AND ONLY TYPED REFUSALS ARE CAUGHT. This
    used to wrap the whole pipeline call in ``except KeyError`` and report
    every one as "missing required argument" — so a ``KeyError`` raised deep
    inside the planner, by a plan payload missing a key it expected, came back
    to the agent as a complaint about ITS arguments. The agent then re-sent
    the same correct call. Worse, the real fault was hidden at the exact
    moment the row might have been left half written.

    So the arguments are validated here, against the schema this module
    publishes, and the ``except`` list below is the two exceptions the
    pipeline raises deliberately. Anything else is an internal error and
    surfaces as one, with the traceback in the log.

    ``implement`` is reachable here and refuses on any state but ``approved``.
    That is deliberate and it is not a relaxation of the human gate: the gate
    is the transition INTO approved, which no verb on this surface can make.
    An agent may carry out a decision the owner has already taken; it may not
    take it.
    """
    from okuro.db import get_db
    from okuro.roles.actions import ActionRefused
    from okuro.roles.repair_plan import EmitRefused
    from okuro.roles import structural_update as pipeline

    missing = [k for k in _UPDATE_REQUIRED.get(name, ())
               if not str(arguments.get(k) or "").strip()]
    if missing:
        return {"refused": f"missing required argument: {', '.join(missing)}"}

    db = get_db()
    try:
        if name == "roles_update_plan":
            return pipeline.plan(
                db,
                arguments["action_id"],
                arguments["actor"],
                instructions=arguments.get("instructions"),
            )

        if name == "roles_update_critique":
            return pipeline.critique(
                db, arguments["action_id"], arguments["actor"])

        if name == "roles_update_dry_run":
            return pipeline.dry_run(
                db, arguments["action_id"],
                samples=arguments.get("samples", pipeline.DIFF_SAMPLES))

        if name == "roles_update_implement":
            return pipeline.implement(
                db, arguments["action_id"], arguments["actor"],
                worktree=arguments.get("worktree"))

        if name == "roles_update_verify":
            return pipeline.verify(
                db, arguments["action_id"], arguments["actor"])
    except (ActionRefused, EmitRefused) as exc:
        # UpdateRefused subclasses ActionRefused, so one clause covers the
        # pipeline's gates, the state machine's and the emitter's.
        return {"refused": str(exc)}
    except Exception as exc:  # noqa: BLE001
        # NOT reported as a refusal. A refusal is an answer; this is a fault,
        # and calling it a refusal teaches the agent that the gate said no
        # when nothing did.
        logger.exception("pipeline verb %s failed on %s", name,
                         arguments.get("action_id"))
        return {
            "error": f"{name} failed: {type(exc).__name__}: {exc}",
            "action_id": arguments.get("action_id"),
            "note": (
                "This is an internal error, not a gate. The row was left as "
                "the last completed step put it; read it with "
                "roles_actions_get before retrying."
            ),
        }

    return {"refused": f"Unknown tool: {name}"}
