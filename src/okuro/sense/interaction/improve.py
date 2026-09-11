### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Route interaction markers to the okuro surface that can fix them; propose concrete changes into the signals queue.
# index: imports | SURFACES | MARKER_ROUTES | harvest_role_adoptions | index_session_roles | _baseline | _gather | _PROMPT | propose_improvements | verify_improvements | improvement_report
# AGENT_HEADER_END -->
"""Improvement router — findings become proposals against a changeable surface.

:mod:`.detect` says what happened. :mod:`.analyze` says what it means. Neither
says *what to change*, and a finding with no addressable target is advice, not
improvement.

This module closes that gap in three stages:

1. **Route.** Each marker maps deterministically to the okuro surface capable
   of fixing it (:data:`MARKER_ROUTES`). Routing is not left to the model —
   the mapping is a property of the system's architecture, not a judgement
   call, and a model asked to invent it drifts between runs.

2. **Propose.** A model writes the concrete change for each surface, given
   only the markers routed there. Proposals land in the existing ``signals``
   queue (``source='proactive'``), which already carries
   ``open → promoted/discarded`` and a web UI. No parallel approval surface.

3. **Verify.** Every proposal stamps its marker rate at proposal time and is
   re-measured after promotion.

Why stage 3 is not optional
---------------------------
Measured over eight months, frustration markers do not trend down::

    2026-02  318      2026-05   88
    2026-03  181      2026-06  172
    2026-04  171      2026-07  107

A system that only proposes cannot distinguish a fix that worked from one that
was promoted and forgotten. Proposals whose metric does not move are marked
``ineffective`` rather than left to look successful.

Autonomy
--------
Propose-only. Nothing here mutates a role, a profile, a principle, or the
bootstrap packet. Every proposal waits for explicit promotion through the
signals queue. The surfaces this reaches are the most load-bearing config in
okuro; automatic edits to them would change the system's behaviour between
sessions with no review.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

BASELINE_WINDOW_DAYS = 30
VERIFY_AFTER_DAYS = 21
MIN_MARKERS_PER_SURFACE = 3
# A promoted proposal must move its metric by at least this fraction to count
# as confirmed. Below it, session-mix noise explains the change just as well.
MIN_EFFECT_SIZE = 0.20


@dataclass(frozen=True)
class Surface:
    """A part of okuro that can be changed, and how."""

    id: str
    title: str
    mutation: str        # how a promoted proposal would be applied
    guidance: str        # what a good proposal against this surface looks like


SURFACES: tuple[Surface, ...] = (
    Surface(
        "subagent_brief",
        "Subagent brief template",
        "Add required fields to the spawn template; add a pre-spawn validator "
        "that blocks incomplete briefs.",
        "Name the exact field(s) missing and where the template lives. A good "
        "proposal here is mechanical and verifiable, not advisory.",
    ),
    Surface(
        "enforcement_hook",
        "okuro MCP middleware gate (provider-agnostic)",
        "Add or tighten a gate in okuro.sense.mcp_middleware "
        "(pre_tool_call / post_tool_call), the orchestrator finalize path, or "
        "okuro.sense.approval_gate.",
        "Enforcement SHOULD live in okuro's own MCP layer, which every "
        "provider passes through — it survives any front-end change and is "
        "the only tier that may carry an invariant alone. State the trigger "
        "condition and the refusal. Prefer a gate over an instruction: "
        "instructions are advisory, gates are not.\n\n"
        "A provider-native hook is permitted ONLY as an accelerator on top of "
        "that, and only when BOTH hold: (a) it is COMPILED from a "
        "provider-agnostic contract in okuro (see compile_system_state_routes "
        "in sense/providers/claude.py, generated from the tool_routing actions "
        "in data/agent_rules.yaml) rather than hand-written for one CLI, and "
        "(b) providers lacking the capability get a DECLARED degradation, "
        "never silent absence — cursor.py logs \"no OS-level hook system; "
        "instructions-only enforcement\", which is the pattern.\n\n"
        "Why the carve-out exists: mcp_middleware sees only okuro MCP tool "
        "dispatch. It is structurally blind to Bash, Read and Edit, so for "
        "native-tool routing there is NOTHING it can gate. A rule that forbids "
        "the only surface capable of enforcing that class does not protect "
        "provider-agnosticism — it guarantees the class stays unenforced.",
    ),
    Surface(
        "role",
        "Role body or role_knowledge",
        "Attach a `pitfall` entry via role_knowledge, or amend the role body.",
        "Name the role_id. The pitfall must be specific enough that a future "
        "agent adopting that role would act differently.",
    ),
    Surface(
        "user_profile",
        "Behavioral contract / user profile",
        "Amend the stored profile via update_profile.",
        "Only propose this when the user restated the same directive across "
        "MULTIPLE sessions — that is evidence the contract is missing it, not "
        "that one session went badly.",
    ),
    Surface(
        "principle",
        "Principle set (DP / SYS / ORCH)",
        "Amend a principle, or flag one that is stated but unenforced.",
        "A principle violated at scale is an enforcement gap, not a wording "
        "problem. Say which it is.",
    ),
    Surface(
        "bootstrap",
        "Bootstrap packet composition",
        "Change what the packet surfaces or suppresses.",
        "Propose what to stop injecting as readily as what to add — packet "
        "budget is finite and re-injecting settled corrections wastes it.",
    ),
    Surface(
        "memory_hygiene",
        "Memory decay / dedupe / acknowledgement",
        "Adjust confidence decay, supersede rules, or mark corrections landed.",
        "Target the lifecycle, not an individual memory row.",
    ),
)

_SURFACE_BY_ID = {s.id: s for s in SURFACES}

# Deterministic marker → surface routing. A marker may feed several surfaces;
# each gets only the markers routed to it, so proposals stay grounded.
MARKER_ROUTES: dict[str, tuple[str, ...]] = {
    "brief_underspecified": ("subagent_brief", "principle"),
    "no_closeout": ("enforcement_hook", "principle"),
    "frustration": ("role", "user_profile"),
    "correction": ("role",),
    "rejection": ("role",),
    "restated_directive": ("user_profile", "memory_hygiene"),
    "re_explanation": ("bootstrap", "memory_hygiene"),
    "abandonment": ("enforcement_hook",),
    "escalating_friction": ("user_profile", "role"),
}

# Provider-native mechanisms. A proposal naming one of these binds okuro to a
# single front-end and violates the charter's provider-agnostic guarantee
# ("one system, many front-ends ... no lock-in: if a provider raises prices or
# ships a worse model, switch").
#
# This is a deterministic reject rather than prompt guidance alone. Guidance
# drifts run to run — the first live batch produced "add a PreCompact/Stop
# hook" and "add a PreToolUse hook" against a surface whose description did
# not forbid it. A check does not drift.
#
# okuro's own provider-agnostic equivalents, which proposals SHOULD target:
#   okuro.sense.mcp_middleware.pre_tool_call / post_tool_call  (hard gate —
#     every provider reaches okuro through the MCP server)
#   okuro.sense.approval_gate                                  (inline pause)
#   the orchestrator finalize path                             (subtask gate)
_PROVIDER_SPECIFIC: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(?:Pre|Post)ToolUse\b", re.I), "Claude Code hook event"),
    (re.compile(r"\bPreCompact\b", re.I), "Claude Code hook event"),
    (re.compile(r"\bUserPromptSubmit\b", re.I), "Claude Code hook event"),
    (re.compile(r"\bSessionStart\b", re.I), "Claude Code hook event"),
    (re.compile(r"\bStop\s+hook\b", re.I), "Claude Code hook event"),
    (re.compile(r"\bSubagentStop\b", re.I), "Claude Code hook event"),
    (re.compile(r"\.claude/|\bCLAUDE\.md\b", re.I), "Claude Code config path"),
    (re.compile(r"\bsettings\.local\.json\b", re.I), "Claude Code settings file"),
    (re.compile(r"\bslash command\b", re.I), "provider-native command surface"),
    (re.compile(r"\bAGENTS\.md\b|\.codex/", re.I), "Codex config path"),
    # Google path = Antigravity (`agy`), NOT Gemini CLI. Gemini CLI was retired
    # 2026-07-18 (accounts closed 2026-06-18) and migration 102 already dropped
    # it as a valid provider; guarding GEMINI.md would defend a front-end okuro
    # no longer runs while leaving the live one open. Antigravity is a VS Code
    # fork, so its surface is config directories rather than a rules file —
    # none of the .md files under it are agent rule files, so none is claimed
    # here.
    (re.compile(r"\.antigravity\b|\.config/Antigravity\b|"
                r"antigravity-server\b|\.gemini/antigravity", re.I),
     "Antigravity config path"),
    (re.compile(r"\.cursorrules\b|\.cursor/", re.I), "Cursor config path"),
)


def validate_proposal(proposal: dict) -> str | None:
    """Return a rejection reason if the proposal is provider-specific, else None.

    Checked against title, body, rationale and target_ref together — a
    proposal that says "add a gate" in the body but names ``hooks.Stop`` as
    its target is still provider-bound.
    """
    blob = " ".join(
        str(proposal.get(k) or "")
        for k in ("title", "proposal", "rationale", "target_ref")
    )
    for pattern, label in _PROVIDER_SPECIFIC:
        m = pattern.search(blob)
        if m:
            return f"provider-specific ({label}): {m.group(0)!r}"
    return None


_ROLE_CALL_RE = re.compile(r'"role_id"\s*:\s*"([a-z0-9_-]+)"', re.IGNORECASE)
_ROLE_ADOPT_RE = re.compile(r"Adopt this role:\s*`?([a-z0-9_-]+)`?", re.IGNORECASE)


def harvest_role_adoptions(session_ids=None) -> dict:
    """Scan raw text for role adoptions and persist every hit.

    The only place that reads ``agent_events.text`` for role attribution, and
    registered in :mod:`.extractors` so ``compact_traces`` refuses to null
    bodies this has not seen.

    Two exact sources, both echoed into ``tool_result`` bodies — i.e. exactly
    what the cool tier nulls: an explicit ``roles_get`` call, and the bootstrap
    packet's own ``Adopt this role:`` line. Additive by contract; see
    :func:`okuro.sense.interaction.bridge.harvest_session_links` for why a
    session compacted before its first harvest is not recoverable.

    Args:
        session_ids: Restrict the scan to these native session ids. None
            scans the whole corpus.
    """
    from okuro.db import get_db

    from . import extractors

    db = get_db()

    scope_ids = list(session_ids) if session_ids is not None else None
    if scope_ids is not None and not scope_ids:
        return {"extractor": "role_adoption", "sessions": 0, "hits": 0}
    if scope_ids is None:
        scope_ids = [
            r["session_id"]
            for r in db.fetchall("SELECT session_id FROM agent_sessions")
        ]

    # Positions before the scan — see bridge.harvest_session_links.
    positions = extractors.session_positions(db, scope_ids)

    scope_clause = ""
    scope_params: tuple = ()
    if session_ids is not None:
        scope_clause = f" AND session_id IN ({','.join('?' * len(scope_ids))})"
        scope_params = tuple(scope_ids)

    harvested: list[tuple] = []
    hits_by_session: dict[str, int] = {}

    def _collect(sql: str, regex, source: str) -> None:
        for row in db.fetchall(sql, scope_params):
            m = regex.search(row["text"] or "")
            if not m:
                continue
            sid = row["session_id"]
            harvested.append(
                (sid, row["ord"], m.group(1).lower(), {"source": source})
            )
            hits_by_session[sid] = hits_by_session.get(sid, 0) + 1

    _collect(
        f"""
        SELECT session_id, ord, text
        FROM agent_events
        WHERE tool_name LIKE '%roles_get%' AND text IS NOT NULL{scope_clause}
        """,
        _ROLE_CALL_RE,
        "roles_get",
    )
    _collect(
        f"""
        SELECT session_id, ord, text
        FROM agent_events
        WHERE text LIKE '%Adopt this role%' AND text IS NOT NULL{scope_clause}
        """,
        _ROLE_ADOPT_RE,
        "bootstrap",
    )

    written = extractors.store_extracts(db, "role_adoption", harvested)

    # Backfill from the materialised index — see the same step in
    # bridge.harvest_session_links for the measurement that forced it. A
    # raw-text harvest can only rescue text that still exists; an attribution
    # already in session_roles is durable regardless of what happened to the
    # transcript it came from.
    backfill = [
        (r["native_session_id"], r["ord"] if r["ord"] is not None else -1,
         r["role_id"], {"source": "index_backfill",
                        "original_source": r["source"]})
        for r in db.fetchall(
            "SELECT native_session_id, role_id, source, ord FROM session_roles"
        )
    ]
    backfilled = extractors.store_extracts(db, "role_adoption", backfill)

    scanned = extractors.record_scan(db, "role_adoption", hits_by_session,
                                     scope_ids, positions)

    return {
        "extractor": "role_adoption",
        "sessions": scanned,
        "sessions_with_hits": len(hits_by_session),
        "hits": len(harvested),
        "rows_written": written,
        "index_backfilled": backfilled,
    }


def index_session_roles(rescan: bool = False) -> dict:
    """Populate ``session_roles`` — which role was driving each session.

    Without this, "improve the roles" has no addressable target: friction is
    observable per session, but roles are the thing that can actually be
    edited. Two transcript sources, both exact: an explicit ``roles_get``
    call, and the bootstrap packet's own role assignment.

    Harvests first, then rebuilds from ``trace_text_extracts`` rather than from
    raw text. ``rescan=True`` does an unconditional ``DELETE FROM
    session_roles``, so before this change it destroyed the attribution for
    every session whose bodies compaction had already nulled. Reading the
    persisted harvest makes that path lossless.
    """
    from okuro.db import get_db

    from . import extractors

    db = get_db()

    harvest = harvest_role_adoptions()

    if rescan:
        with db.write():
            db.execute("DELETE FROM session_roles")

    # Precedence: an explicit roles_get call beats the bootstrap assignment,
    # matching the original scan order rather than the ordinal. A backfilled
    # row ranks last — it is the answer a previous extraction gave, and
    # rescan=True exists to let a new extraction overrule it.
    _RANK = {"roles_get": 0, "bootstrap": 1, "index_backfill": 2}
    found: dict[tuple[str, str], tuple[str, int]] = {}
    ranked: dict[tuple[str, str], int] = {}
    for row in extractors.read_extracts(db, "role_adoption"):
        try:
            extra = json.loads(row["extra"] or "{}") or {}
        except (TypeError, ValueError):
            extra = {}
        source = extra.get("source", "")
        # session_roles.source is CHECK-constrained to the two real derivation
        # routes, so a backfilled row writes the route it originally came from,
        # never the fact that it was backfilled. That provenance belongs to the
        # extract, not to the index.
        if source == "index_backfill":
            stored_source = extra.get("original_source") or "bootstrap"
        else:
            stored_source = source or "bootstrap"
        key = (row["native_session_id"], (row["value"] or "").lower())
        rank = _RANK.get(source, 2)
        if key in ranked and ranked[key] <= rank:
            continue
        ranked[key] = rank
        found[key] = (stored_source, row["ord"])

    written = 0
    with db.write():
        for (sid, role_id), (source, ord_) in found.items():
            db.execute(
                """
                INSERT INTO session_roles (native_session_id, role_id, source, ord)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(native_session_id, role_id) DO NOTHING
                """,
                (sid, role_id, source, ord_),
            )
            written += 1

    distinct_roles = db.fetchone(
        "SELECT COUNT(DISTINCT role_id) AS n FROM session_roles"
    ) or {}
    return {
        "links": written,
        "distinct_roles": distinct_roles.get("n", 0),
        "rescanned": rescan,
        "harvest": harvest,
    }


def _marker_rate(markers: list[str], window_days: int,
                 role_id: str | None = None) -> float:
    """Markers per scanned session over the window — the effectiveness metric.

    A rate, not a count: session volume swings week to week, so raw counts
    cannot distinguish "fewer problems" from "fewer sessions".
    """
    from okuro.db import get_db

    if not markers:
        return 0.0

    db = get_db()
    placeholders = ",".join("?" * len(markers))
    params: list = list(markers) + [f"-{int(window_days)} days"]

    role_join = ""
    if role_id:
        role_join = (
            " JOIN session_roles r ON r.native_session_id = m.native_session_id "
            " AND r.role_id = ? "
        )
        params = list(markers) + [role_id, f"-{int(window_days)} days"]

    hits = db.fetchone(
        f"""
        SELECT COUNT(*) AS n
        FROM interaction_markers m {role_join}
        WHERE m.marker IN ({placeholders})
          AND m.ts >= date('now', ?)
        """,
        tuple(params),
    ) or {}

    sessions = db.fetchone(
        """
        SELECT COUNT(*) AS n FROM interaction_scanned
        WHERE scanned_at >= date('now', ?)
        """,
        (f"-{int(window_days)} days",),
    ) or {}

    n_sessions = sessions.get("n") or 0
    if not n_sessions:
        return 0.0
    return round((hits.get("n") or 0) / n_sessions, 4)


def _gather_by_surface(window_days: int) -> dict[str, dict]:
    """Group recent markers under the surfaces that could fix them."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """
        SELECT m.marker, m.evidence, m.native_session_id, m.ts,
               a.project_path, r.role_id
        FROM interaction_markers m
        LEFT JOIN agent_sessions a ON a.session_id = m.native_session_id
        LEFT JOIN session_roles  r ON r.native_session_id = m.native_session_id
        WHERE m.ts >= date('now', ?)
        ORDER BY m.ts DESC
        """,
        (f"-{int(window_days)} days",),
    )

    by_surface: dict[str, dict] = {}
    for r in rows:
        for surface_id in MARKER_ROUTES.get(r["marker"], ()):
            bucket = by_surface.setdefault(
                surface_id, {"hits": [], "markers": set(), "sessions": set(),
                             "roles": {}, "projects": {}}
            )
            bucket["hits"].append(dict(r))
            bucket["markers"].add(r["marker"])
            bucket["sessions"].add(r["native_session_id"])
            if r["role_id"]:
                bucket["roles"][r["role_id"]] = bucket["roles"].get(r["role_id"], 0) + 1
            if r["project_path"]:
                p = r["project_path"]
                bucket["projects"][p] = bucket["projects"].get(p, 0) + 1
    return by_surface


_PROMPT = """You are proposing concrete improvements to okuro, an agent infrastructure system, based on measured problems in its own session transcripts.

## Surface under consideration
**{surface_title}** — {surface_mutation}

Guidance for this surface: {surface_guidance}

## Measured evidence ({hits} marker hits across {sessions} sessions, last {window} days)
Markers routed here: {markers}

{concentration}

### Sample evidence (verbatim from transcripts)
{evidence}

## Hard constraint: provider-agnostic only
okuro runs the same brain across Claude Code, Codex, Antigravity (`agy`, the Google path — Gemini CLI was retired 2026-07-18) and Cursor, and its charter guarantees no lock-in. A change that only works on one front-end is therefore invalid however well it would work there.

NEVER propose a HAND-WRITTEN provider-native mechanism: a Claude Code hook, settings.json entry, CLAUDE.md / AGENTS.md / .cursorrules edit, anything under .antigravity/ or .config/Antigravity/, or a provider-native slash command, authored for one CLI. Those bind okuro to one front-end.

A provider-native hook IS valid when it is COMPILED from a provider-agnostic contract that lives in okuro, and when providers without the capability get a declared degradation rather than silent absence. The spec is shared; only the emitter is per-provider — the same shape the comm-protocol already uses across four harnesses. Worked example: `compile_system_state_routes` (sense/providers/claude.py) projects the `tool_routing` actions in data/agent_rules.yaml into a gate, so the routing table an agent reads and the gate that enforces it are one definition. Propose the CONTRACT change; the emitter follows.

Do not read this as a licence for provider-specific work. The test is: could a second provider's emitter be generated from the same spec without editing the spec? If no, it is lock-in and invalid.

Enforcement belongs in okuro's own layer wherever that layer can see the action — but note it CANNOT see everything. `mcp_middleware` observes only okuro MCP tool dispatch; it is structurally blind to Bash, Read and Edit. For native-tool routing there is nothing in okuro's own layer to gate, so forbidding the compiled-hook path would not protect provider-agnosticism, it would guarantee that class stays unenforced. Measured: the cortex routing rule sat in the packet for months while shell hunting went unpoliced.

okuro's own enforcement surfaces, preferred wherever they can observe the action:
- `okuro.sense.mcp_middleware.pre_tool_call` / `post_tool_call` — the hard gate; pre_tool_call can refuse a call outright
- `okuro.sense.approval_gate` — pauses a call for human approval
- the orchestrator finalize path — gates subtask completion
- the bootstrap packet and role bodies — carried to every provider identically

## Task
Propose at most 2 concrete changes to THIS surface. A proposal must be specific enough to implement without further investigation — name the file, field, role_id, or rule.

Reject your own idea if:
- it names a HAND-WRITTEN provider-specific mechanism, i.e. one whose emitter could not be generated for a second provider from the same okuro-side spec (see the hard constraint above)
- the evidence does not support it across at least 2 distinct sessions
- it is advice rather than a change ("be more careful" is not a proposal)
- it belongs to a different surface than the one above

Returning an empty list is a valid and useful answer.

For each proposal:
- `title`: short imperative, max 80 chars
- `target_ref`: the specific role_id / file / field / rule it changes, or null
- `proposal`: the concrete change, 1-3 sentences
- `rationale`: why the evidence supports it, citing what you saw

## Output
STRICT JSON only, no prose, no fence:
{{"proposals": [{{"title": "...", "target_ref": "...", "proposal": "...", "rationale": "..."}}]}}
"""


def _render_concentration(bucket: dict) -> str:
    """Where the markers cluster — roles and projects rank the target."""
    lines: list[str] = []
    roles = sorted(bucket["roles"].items(), key=lambda kv: kv[1], reverse=True)[:5]
    if roles:
        lines.append("Roles active in these sessions: " + ", ".join(
            f"{r} ({n})" for r, n in roles))
    projects = sorted(bucket["projects"].items(), key=lambda kv: kv[1], reverse=True)[:5]
    if projects:
        lines.append("Projects: " + ", ".join(f"{p} ({n})" for p, n in projects))
    return "\n".join(lines) or "(no role/project concentration)"


def _parse(output: str) -> dict:
    """Parse model output into ``{"proposals": [...]}``.

    Salvage matters here in a way it does not for the analysis stage. A
    proposal batch is one model call per surface, so a single malformed
    response silently drops that entire surface from the run — observed live:
    ``memory_hygiene`` produced no proposals because one string 1869
    characters in contained an unescaped quote.

    Strategy: strict parse, then object-by-object salvage. A batch of five
    good proposals and one broken one should yield five, not zero.
    """
    text = (output or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in model output")

    blob = text[start : end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError as exc:
        # Bind the message now: Python unbinds the exception name when the
        # except block exits, so referencing `exc` after it raises
        # UnboundLocalError instead of reporting the real parse error.
        strict_error = str(exc)
        log.warning("improve: strict parse failed (%s); salvaging", strict_error)

    # Salvage: pull the INNER proposal objects with a balanced-brace scan and
    # keep the ones that parse. Regex cannot do this — proposal bodies contain
    # braces and quotes.
    #
    # Objects are captured at depth 1, not depth 0: depth 0 is the envelope
    # ({"proposals": [...]}), which is precisely the thing that failed to
    # parse. Scanning at depth 0 recovers exactly one candidate — the broken
    # envelope — and salvages nothing.
    salvaged: list[dict] = []
    depth = 0
    obj_start: int | None = None
    for i, ch in enumerate(blob):
        if ch == "{":
            if depth == 1 and obj_start is None:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 1 and obj_start is not None:
                fragment = blob[obj_start : i + 1]
                try:
                    candidate = json.loads(fragment)
                except json.JSONDecodeError:
                    candidate = None
                if isinstance(candidate, dict) and candidate.get("title"):
                    salvaged.append(candidate)
                obj_start = None

    if salvaged:
        log.info("improve: salvaged %d proposal(s) from malformed output",
                 len(salvaged))
        return {"proposals": salvaged}

    raise ValueError(f"unparseable model output: {strict_error}")


def propose_improvements(
    window_days: int = BASELINE_WINDOW_DAYS,
    provider: str | None = "claude",
    surfaces: list[str] | None = None,
) -> dict[str, Any]:
    """Route markers to surfaces and emit proposals into the signals queue.

    Propose-only by construction: nothing is mutated here. Each proposal is
    registered as an open signal for explicit promotion, and stamped with the
    marker rate at proposal time so its effect can be judged later.
    """
    from okuro.bridge.invoke import invoke
    from okuro.sense.signals import signal_add

    batch_id = str(uuid.uuid4())
    by_surface = _gather_by_surface(window_days)
    targets = surfaces or [s.id for s in SURFACES]

    emitted: list[dict] = []
    rejected: list[dict] = []
    skipped: dict[str, str] = {}

    for surface_id in targets:
        surface = _SURFACE_BY_ID.get(surface_id)
        bucket = by_surface.get(surface_id)
        if surface is None:
            skipped[surface_id] = "unknown surface"
            continue
        if not bucket or len(bucket["hits"]) < MIN_MARKERS_PER_SURFACE:
            skipped[surface_id] = (
                f"{len(bucket['hits']) if bucket else 0} markers; "
                f"need >= {MIN_MARKERS_PER_SURFACE}"
            )
            continue

        sample = bucket["hits"][:10]
        evidence = "\n".join(
            f'- [{h["marker"]}] "{(h["evidence"] or "")[:200]}"' for h in sample
        )

        prompt = _PROMPT.format(
            surface_title=surface.title,
            surface_mutation=surface.mutation,
            surface_guidance=surface.guidance,
            hits=len(bucket["hits"]),
            sessions=len(bucket["sessions"]),
            window=window_days,
            markers=", ".join(sorted(bucket["markers"])),
            concentration=_render_concentration(bucket),
            evidence=evidence,
        )

        result = invoke(
            prompt=prompt,
            provider=provider,
            system_prompt=(
                "You propose concrete, implementable changes to agent "
                "infrastructure. Ground every proposal in the evidence shown. "
                "Output only valid JSON matching the requested schema."
            ),
        )
        if not result.get("success"):
            skipped[surface_id] = f"invoke failed: {result.get('error')}"
            continue

        try:
            parsed = _parse(result.get("output") or "")
        except Exception as exc:
            skipped[surface_id] = f"parse failed: {exc}"
            continue

        markers = sorted(bucket["markers"])
        baseline = _marker_rate(markers, window_days)

        for p in parsed.get("proposals") or []:
            title = str(p.get("title") or "").strip()
            proposal = str(p.get("proposal") or "").strip()
            if not title or not proposal:
                continue
            # Deterministic gate. The prompt forbids provider-specific
            # mechanisms; this makes it true regardless of whether the model
            # complied on any given run.
            reason = validate_proposal(p)
            if reason:
                rejected.append({"title": title, "surface": surface_id,
                                 "reason": reason})
                log.info("improve: rejected %r — %s", title, reason)
                continue
            emitted.append(
                _register(
                    batch_id=batch_id,
                    surface=surface,
                    proposal=p,
                    markers=markers,
                    baseline=baseline,
                    window_days=window_days,
                    model=result.get("model") or "",
                    signal_add=signal_add,
                    evidence_bucket=bucket,
                )
            )

    return {
        "batch_id": batch_id,
        "proposals": emitted,
        "rejected": rejected,
        "surfaces_considered": len(targets),
        "skipped": skipped,
    }


def _register(*, batch_id, surface, proposal, markers, baseline, window_days,
              model, signal_add, evidence_bucket) -> dict:
    """Persist one proposal and register it as an open signal."""
    from okuro.db import get_db

    db = get_db()
    imp_id = str(uuid.uuid4())
    title = str(proposal.get("title") or "").strip()[:200]
    body = str(proposal.get("proposal") or "").strip()
    rationale = str(proposal.get("rationale") or "").strip()
    target_ref = proposal.get("target_ref") or None

    signal_id = None
    try:
        # source='proactive' rather than a new enum value: the signals CHECK
        # constraint is closed, and adding a value would mean rebuilding the
        # table for no behavioural gain. source_ref carries the real origin.
        sig = signal_add(
            source="proactive",
            severity="info",
            summary=f"[{surface.id}] {title}",
            source_ref=f"interaction_improvement:{imp_id}",
            evidence={
                "surface": surface.id,
                "target_ref": target_ref,
                "markers": markers,
                "marker_hits": len(evidence_bucket["hits"]),
                "sessions": len(evidence_bucket["sessions"]),
                "baseline_rate": baseline,
                "rationale": rationale,
            },
            suggested_action=body,
        )
        signal_id = sig.get("id") if isinstance(sig, dict) else None
    except Exception as exc:
        log.warning("improve: signal_add failed (%s)", exc)

    with db.write():
        db.execute(
            """
            INSERT INTO interaction_improvements
                (id, batch_id, signal_id, surface, target_ref, title, proposal,
                 rationale, markers, baseline_value, baseline_window,
                 verify_after, status, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    date('now', ?), 'proposed', ?)
            """,
            (
                imp_id, batch_id, signal_id, surface.id, target_ref, title, body,
                rationale, json.dumps(markers), baseline, f"{window_days}d",
                f"+{VERIFY_AFTER_DAYS} days", model,
            ),
        )

    return {
        "id": imp_id,
        "signal_id": signal_id,
        "surface": surface.id,
        "target_ref": target_ref,
        "title": title,
        "proposal": body,
        "baseline_rate": baseline,
    }


def verify_improvements(window_days: int = BASELINE_WINDOW_DAYS) -> dict:
    """Re-measure promoted proposals and judge whether they actually worked.

    Without this the pipeline accumulates advice. A proposal whose metric did
    not move is marked ``ineffective`` — which is itself a finding, and a more
    useful one than another proposal against the same surface.
    """
    from okuro.db import get_db

    db = get_db()

    due = db.fetchall(
        """
        SELECT i.*, s.status AS signal_status
        FROM interaction_improvements i
        LEFT JOIN signals s ON s.id = i.signal_id
        WHERE i.verified_at IS NULL
          AND i.verify_after IS NOT NULL
          AND date(i.verify_after) <= date('now')
        """
    )

    judged = {"confirmed": 0, "ineffective": 0, "inconclusive": 0, "not_promoted": 0}
    # A judged proposal is no longer a live suggestion. Until this existed the
    # verifier read the signal to decide the outcome and then left it open
    # forever — okuro measured its own advice and kept nagging about it either
    # way. Only decided outcomes close the signal; 'inconclusive' means the
    # measurement could not judge it, so the suggestion stands.
    _DECIDED = ("confirmed", "ineffective")
    signals_expired = 0

    for row in due:
        markers = json.loads(row["markers"] or "[]")
        baseline = row["baseline_value"]
        signal_status = row["signal_status"]

        if signal_status in ("discarded", "ignored"):
            outcome, current = "not_promoted", None
        else:
            current = _marker_rate(markers, window_days)
            if baseline is None or baseline == 0:
                outcome = "inconclusive"
            else:
                delta = (baseline - current) / baseline
                if delta >= MIN_EFFECT_SIZE:
                    outcome = "confirmed"
                elif delta <= -MIN_EFFECT_SIZE:
                    outcome = "ineffective"   # got worse
                else:
                    outcome = "ineffective" if signal_status == "promoted" else "inconclusive"

        judged[outcome] = judged.get(outcome, 0) + 1
        with db.write():
            db.execute(
                """
                UPDATE interaction_improvements
                SET verified_at = datetime('now'),
                    verified_value = ?,
                    outcome = ?,
                    status = 'verified'
                WHERE id = ?
                """,
                (current, outcome, row["id"]),
            )

        if outcome in _DECIDED and row["signal_id"]:
            # `AND status='open'` keeps this idempotent and keeps the user's
            # own verdicts theirs — a promoted/discarded row is untouched.
            with db.write():
                cur = db.execute(
                    "UPDATE signals SET status = 'expired' "
                    "WHERE id = ? AND status = 'open'",
                    (row["signal_id"],),
                )
            closed = getattr(cur, "rowcount", 0) or 0
            signals_expired += closed
            if closed:
                log.info(
                    "improve: expired signal %s — proposal verified %s",
                    row["signal_id"], outcome,
                )

    return {"due": len(due), "judged": judged, "signals_expired": signals_expired}


def run_improvements() -> dict:
    """Daemon entry point: index roles, propose, then verify what is due."""
    roles = index_session_roles()
    proposed = propose_improvements()
    verified = verify_improvements()
    return {"roles": roles, "proposed": proposed, "verified": verified}


def improvement_report(limit: int = 50) -> list[dict]:
    """Proposals with their surface, status, and measured effect."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """
        SELECT id, surface, target_ref, title, status, outcome,
               baseline_value, verified_value, created_at
        FROM interaction_improvements
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    return [dict(r) for r in rows]
