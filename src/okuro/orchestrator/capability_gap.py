# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Capability-gap escalation primitive — detect missing prerequisites and route them to creator roles instead of stalling.
# index: imports | dataclass CapabilityGap | CREATORS_BY_KIND | def _llm_confirm_fit | def _parse_fit_ids | def detect_capability_gap_from_panel | def build_phase0_subtasks | def _phase0_description
# AGENT_HEADER_END -->
"""Capability-gap escalation primitive.

When a discovery step in the orchestrator detects that something needed
to make progress is missing — a role, a tool, a data source, a migration —
the engine must NOT stall silently. It must escalate: emit an
``open_question(blocking=true)`` event AND inject a Phase 0 sub-graph
that calls the appropriate creator roles, gated on user approval.

This module is decision-pure: it returns ``CapabilityGap`` descriptors
and Phase 0 subtask shapes. The engine performs side effects (event
emission, state mutation, DAG node creation) using the descriptors.

New gap kinds plug in by extending ``CREATORS_BY_KIND`` and adding a
branch in :func:`_phase0_description`. The engine never grows
kind-specific code — per DP10 SYSTEMATIC-NOT-SPECIFIC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Kind = Literal["missing_role", "missing_tool", "missing_data", "missing_migration"]


@dataclass
class CapabilityGap:
    """A discovered prerequisite the orchestrator cannot fill on its own."""

    kind: Kind
    summary: str
    slot_descriptions: list[str] = field(default_factory=list)
    creator_roles: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


CREATORS_BY_KIND: dict[str, list[str]] = {
    "missing_role": ["role-researcher", "role-designer"],
    # Future kinds plug in here without engine changes:
    # "missing_tool":      ["tool-author"],
    # "missing_data":      ["data-fetcher"],
    # "missing_migration": ["migration-author"],
}


# P2-2 — keyword-driven role-class hints. Audio task showed the gap card
# said "2 missing roles" because only the initial deliberation panel was
# inspected; the eventual plan also needed qa-engineer + license-auditor.
# Honest gap reporting surfaces every role-class the task description
# references so the user sees the full surface, and so role-researcher
# knows to enumerate beyond the initial panel slots.
#
# Keyword → role-class id mapping. Add hints incident-driven, same DP10
# discipline as _PER_ROLE_TIMEOUT_BASE and _ROLE_PREREQUISITES. Lower-case
# substring match on the task description; word boundaries are not
# enforced because "performance benchmark", "test coverage", "license
# audit" all read fine as substrings.
_ROLE_CLASS_KEYWORDS: dict[str, list[str]] = {
    "qa-engineer": ["qa ", "quality assurance", "acceptance criteria", "test plan", "regression"],
    "security-auditor": ["security", "vulnerab", "threat model", "penetrat"],
    "license-auditor": ["license", "licensing", "licence", "third-party", "dependency audit"],
    "reviewer": ["code review", "peer review", "design review"],
    "performance-engineer": ["performance benchmark", "latency budget", "throughput target"],
    "documentation-engineer": ["readme", "documentation", "doc site", "developer guide"],
}


def _detect_expected_role_classes(task_description: str) -> list[str]:
    """Return the role-class ids whose keyword hits the task description.

    Order matches _ROLE_CLASS_KEYWORDS insertion order so the gap card
    renders the same shape every time. Hits are de-duplicated implicitly
    (one entry per role-class id).
    """
    if not task_description:
        return []
    lower = task_description.lower()
    found: list[str] = []
    for role_id, keywords in _ROLE_CLASS_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            found.append(role_id)
    return found


# P-ROLE-1 — quality floor for the deliberate panel. The match_type="matched"
# threshold (resolver.SIMILARITY_THRESHOLD = 0.40) is low: a finance/strategy
# task can get a 0.55–0.61 panel of off-target roles that read as "matched" and
# seed a weak deliberation (no CFO/financial-strategist role exists). When the
# BEST match is below this floor, the engine offers the create-role gap card
# instead — the user can still decline and proceed. The function default is 0.0
# (back-compat for unit tests); the engine passes PANEL_QUALITY_FLOOR.
PANEL_QUALITY_FLOOR = 0.62


# P-ROLE-2 — LLM precision gate. Embedding cosine saturates ~0.6 for
# instruction-tuned models: an off-target role and the right role both read
# as "matched", and PANEL_QUALITY_FLOOR (an absolute bar) cannot separate
# them — a semantically wrong role at 0.634 clears a 0.62 floor. After the
# score filter keeps recall high, a single bridge LLM call decides precision:
# which score-qualified roles GENUINELY fit the task. Judge confirms none →
# capability gap → the role-research/create card appears. Embedding handles
# recall; the judge handles precision.
#
# The judge routes through the bridge's capability routing (best available
# provider — claude / gpt / … — NOT pinned to local inference, which may be
# offline). Degrades to score-only when no provider is reachable or the reply
# is unparseable — never blocks task launch.
ROLE_MATCH_LLM_GATE_ENV = "OKURO_ROLE_MATCH_LLM_GATE"
# Capability tier for the judge. "fast-draft" routes to a cheap, fast model
# (claude haiku per default routing) — sufficient for a role-fit classification
# and DP01 money-efficient. Falls back across detected providers if the primary
# is unavailable.
_LLM_GATE_CAPABILITY = "fast-draft"
_LLM_GATE_TIMEOUT_SECONDS = 90


def _gate_disabled() -> bool:
    """True when the ops kill-switch env var disables the LLM gate."""
    import os

    return os.environ.get(ROLE_MATCH_LLM_GATE_ENV, "1").strip().lower() in {
        "0", "false", "no", "off",
    }


def _parse_fit_ids(raw: str, *, valid_ids: set[str]) -> Optional[set[str]]:
    """Extract confirmed role ids from the judge's JSON reply.

    Tolerant of fenced code blocks and surrounding prose: grabs the first
    ``{...}`` object. The returned set is intersected with ``valid_ids`` —
    the judge cannot invent roles. A VALID but empty ``fit`` list returns an
    empty set (judge says "none fit" → triggers the gap). An UNPARSEABLE
    reply returns ``None`` (judge unavailable → keep the score decision), so
    a malformed response never silently nukes a good panel.
    """
    import json

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    fit = obj.get("fit")
    if not isinstance(fit, list):
        return None
    return {str(x).strip() for x in fit if str(x).strip() in valid_ids}


def _llm_confirm_fit(
    task_description: str,
    candidates: list[dict],
    *,
    timeout: int = _LLM_GATE_TIMEOUT_SECONDS,
) -> Optional[set[str]]:
    """Ask a local LLM which candidate roles genuinely fit the task.

    ``candidates`` are the score-qualified panel items (each carries
    ``role_id``/``id``, ``domain``, ``description``). Returns the set of role
    ids the judge confirms as a real expertise fit (possibly empty). Returns
    ``None`` — meaning "judge unavailable, keep the score-based decision" — on
    any failure: kill-switch set, no usable candidates, no provider reachable,
    timeout, or unparseable output. NEVER raises; role matching must not break
    on a flaky judge.

    Routes via the bridge's capability routing (:data:`_LLM_GATE_CAPABILITY`)
    to the best available provider — not pinned to local inference.
    """
    if _gate_disabled():
        return None

    valid_ids = {
        (c.get("role_id") or c.get("id") or "").strip()
        for c in candidates
    }
    valid_ids.discard("")
    if not valid_ids:
        return None

    lines: list[str] = []
    for c in candidates:
        rid = (c.get("role_id") or c.get("id") or "").strip()
        if not rid:
            continue
        domain = c.get("domain", "")
        desc = (c.get("description") or c.get("why") or "").strip().replace("\n", " ")
        lines.append(f"- {rid} (domain: {domain}): {desc[:240]}")
    roster = "\n".join(lines)

    system_prompt = (
        "You are a strict role-matching judge for a multi-agent system. "
        "Given a TASK and candidate expert ROLES, return only the roles whose "
        "core expertise genuinely covers the actual work the task requires. "
        "Shared vocabulary or topical overlap is NOT a fit — a role must be "
        "able to lead or substantively contribute to THIS task. If no candidate "
        "genuinely fits, return an empty list. Reply with JSON only."
    )
    prompt = (
        f"TASK:\n{task_description[:1500]}\n\n"
        f"CANDIDATE ROLES:\n{roster}\n\n"
        'Respond with JSON exactly: {"fit": ["role-id", ...]}. '
        "Include a role id only when its expertise genuinely fits the task."
    )

    try:
        from okuro.bridge.invoke import invoke

        result = invoke(
            prompt,
            capability=_LLM_GATE_CAPABILITY,
            system_prompt=system_prompt,
            timeout=timeout,
        )
    except Exception:
        return None

    if not result or not result.get("success"):
        return None
    raw = (result.get("output") or "").strip()
    if not raw:
        return None
    return _parse_fit_ids(raw, valid_ids=valid_ids)


def detect_capability_gap_from_panel(
    panel_proposal: list[dict],
    *,
    task_description: str,
    min_matched_required: int = 1,
    quality_floor: float = 0.0,
    llm_gate: bool = False,
) -> Optional[CapabilityGap]:
    """Inspect a panel proposal and return a gap if it cannot be filled.

    A gap is raised when fewer than ``min_matched_required`` items carry
    ``match_type == "matched"`` AND similarity ``>= quality_floor``. The
    remaining sub-threshold matches are attached to the payload as ``closest``
    so the role-researcher can use them as inspiration. ``quality_floor=0.0``
    (default) preserves the legacy match_type-only behavior; the engine passes
    :data:`PANEL_QUALITY_FLOOR` so a weak-but-above-0.40 panel still triggers
    the create-role card (P-ROLE-1).

    P2-2 — payload also carries ``expected_role_classes`` derived from
    keyword hits in the task description. The summary cites the count so
    the user sees an honest estimate instead of one inferred from the
    panel size alone. The role-researcher consumes this list as a
    starting checklist when enumerating slots to fill.

    P-ROLE-2 — when ``llm_gate`` is True, the score-qualified ``matched`` set
    is further filtered by an LLM judge (:func:`_llm_confirm_fit`, routed via
    the bridge to the best available provider) that keeps only roles whose
    expertise genuinely fits the task. This catches the
    "wrong role at 0.634 clears a 0.62 floor" case the absolute threshold
    cannot. The judge can only DEMOTE (precision); recall stays with the
    embedding score. Default ``False`` preserves score-only behavior for unit
    tests; the engine passes ``llm_gate=True``.

    Returns ``None`` when the panel is good enough to proceed.
    """
    matched = [
        p for p in panel_proposal
        if p.get("match_type") == "matched"
        and float(p.get("similarity") or 0.0) >= quality_floor
    ]
    # P-ROLE-2 precision gate. None return = judge unavailable → keep the
    # score-based matched set unchanged (graceful degradation).
    if llm_gate and matched:
        confirmed = _llm_confirm_fit(task_description, matched)
        if confirmed is not None:
            matched = [
                m for m in matched
                if (m.get("role_id") or m.get("id")) in confirmed
            ]
    if len(matched) >= min_matched_required:
        return None

    closest = [
        {
            "id": p.get("role_id") or p.get("id"),
            "domain": p.get("domain", ""),
            "similarity": p.get("similarity", 0.0),
        }
        for p in panel_proposal[:5]
    ]
    expected_classes = _detect_expected_role_classes(task_description)
    if expected_classes:
        summary = (
            f"No role in the registry matches this task above the similarity "
            f"threshold. The task description references at least "
            f"{len(expected_classes)} role-class(es) "
            f"({', '.join(expected_classes)}) — role-researcher will "
            f"enumerate ALL slots needed, not just the panel proposal."
        )
    else:
        summary = (
            "No role in the registry matches this task above the similarity "
            "threshold. User approval needed to create the missing role(s) "
            "via role-researcher + role-designer before deliberation can "
            "continue."
        )
    return CapabilityGap(
        kind="missing_role",
        summary=summary,
        slot_descriptions=[task_description[:200]],
        creator_roles=CREATORS_BY_KIND["missing_role"],
        payload={
            "closest": closest,
            "task_description": task_description,
            "expected_role_classes": expected_classes,
        },
    )


_NAMED_ROLE_TIMEOUT_SECONDS = 60


def _build_named_role_prompt(description: str, role_index_text: str) -> str:
    return (
        "You identify roles a user EXPLICITLY demands for a task and decide "
        "whether the existing role catalog can fill each one.\n\n"
        "## Existing roles (id — domain — purpose)\n"
        f"{role_index_text or '(none)'}\n\n"
        "## Task\n"
        f"{description}\n\n"
        "## Instructions\n"
        "1. Extract ONLY roles the user explicitly asks for — phrases like "
        "'use a X', 'I need a X', 'an X specialist', 'a beast in X', 'a X "
        "advisor'. Do NOT invent roles the user did not request.\n"
        "2. For each demanded role, decide whether an existing catalog role "
        "genuinely fills it (same domain AND expertise). A loose keyword "
        "overlap is NOT a fill — a security-auditor does NOT fill a 'legal "
        "advisor'.\n"
        "3. Return ONLY the demanded roles that have NO adequate existing "
        "role.\n\n"
        "Respond with STRICT JSON, no prose, no code fence:\n"
        '{"missing_roles": [{"name": "<kebab-case-id>", "why": "<one line>", '
        '"domain": "<one of: c-level, content, design, documentation, '
        'engineering, marketing, planning, quality, research, system>", '
        '"tier": "<specialist|standard|support|fast>"}]}\n'
        "If every demanded role is already covered, or the user demanded no "
        'specific role, return {"missing_roles": []}.'
    )


def _parse_named_roles(raw: str) -> list[dict]:
    import json
    import re

    text = (raw or "").strip()
    if not text:
        return []
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    for r in (data.get("missing_roles") or []):
        if isinstance(r, dict) and str(r.get("name", "")).strip():
            out.append({
                "name": str(r["name"]).strip(),
                "why": str(r.get("why", "")).strip(),
                "domain": str(r.get("domain", "")).strip(),
                "tier": str(r.get("tier", "")).strip(),
            })
    return out


def detect_named_role_gaps(
    task_description: str,
    *,
    role_index_text: str,
    capability: str = "fast-draft",
) -> Optional[CapabilityGap]:
    """Detect roles the user EXPLICITLY demands that the registry cannot fill.

    Unlike :func:`detect_capability_gap_from_panel` (whole-task zero-match),
    this fires per explicitly-named role even when other roles match. One LLM
    call reads the task plus the existing role catalog and returns demanded
    roles that have no adequate existing role — judged against the real
    catalog, not an embedding threshold (a short "legal advisor" query would
    otherwise spuriously match security-auditor).

    FAIL-OPEN: any LLM/parse failure returns ``None`` so an availability
    hiccup never blocks a task. Returns ``None`` when nothing is demanded or
    every demand is already covered.
    """
    desc = (task_description or "").strip()
    if not desc:
        return None
    prompt = _build_named_role_prompt(desc, role_index_text)
    try:
        from okuro.bridge.invoke import invoke as bridge_invoke
        result = bridge_invoke(
            prompt=prompt,
            capability=capability,
            timeout=_NAMED_ROLE_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("success"):
        return None
    missing = _parse_named_roles(result.get("output") or "")
    if not missing:
        return None
    names = [m["name"] for m in missing]
    summary = (
        f"The task explicitly demands {len(names)} role(s) the registry "
        f"cannot fill: {', '.join(names)}. Approve Phase 0 to research and "
        f"design them (role-researcher + role-designer) before work continues."
    )
    return CapabilityGap(
        kind="missing_role",
        summary=summary,
        slot_descriptions=names,
        creator_roles=CREATORS_BY_KIND["missing_role"],
        payload={
            "demanded_roles": missing,
            "task_description": desc,
            "trigger": "named_role_demand",
        },
    )


def build_phase0_subtasks(gap: CapabilityGap) -> list[dict]:
    """Render a ``CapabilityGap`` into Phase 0 subtask descriptors.

    The output shape matches the dicts the engine already feeds into
    ``create_execution_nodes`` (id, role, description, risk, complexity,
    artifact_name, dependencies), so no engine-side adapter is needed.
    Steps are serialised — each depends on the previous one — because
    creator-role pipelines are not embarrassingly parallel.
    """
    subs: list[dict] = []
    prev_id: Optional[str] = None
    for i, role in enumerate(gap.creator_roles):
        sub_id = f"phase0-{gap.kind}-{i + 1}"
        subs.append({
            "id": sub_id,
            "role": role,
            "description": _phase0_description(gap, role, step=i + 1),
            "risk": "medium",
            "complexity": "medium",
            "artifact_name": f"phase0-{gap.kind}-step-{i + 1}",
            "dependencies": [prev_id] if prev_id else [],
        })
        prev_id = sub_id
    return subs


def _phase0_description(gap: CapabilityGap, role: str, *, step: int) -> str:
    if gap.kind == "missing_role":
        # When the gap was raised by an explicit user demand (named_role_demand
        # trigger), the creator roles must build EXACTLY the demanded slots —
        # not infer the domain from scratch. Surface them in both steps.
        demanded = gap.payload.get("demanded_roles") or []
        demand_note = ""
        if demanded:
            lines = "\n".join(
                f"- {d.get('name', '?')} (domain={d.get('domain', '?')}, "
                f"tier={d.get('tier', '?')}): {d.get('why', '')}"
                for d in demanded
            )
            demand_note = (
                "\n\nThe user EXPLICITLY demanded these role(s) — create "
                "exactly these, do not substitute an existing role. Persist "
                "each with its role_id set VERBATIM to the kebab-case name "
                "below (the panel re-includes the task by that exact id):\n"
                + lines
            )
        if role == "role-researcher":
            return (
                "Research the domain implied by the user's task description "
                "and identify the role(s) the registry is missing. For each "
                "missing slot deliver: purpose, expertise list, tier "
                "(specialist/standard/support/fast), model tier "
                "(sonnet/haiku), tool requirements, and ≥3 evidence "
                "citations (frameworks / specs / canonical libraries).\n\n"
                f"Original task:\n{gap.payload.get('task_description', '')}\n\n"
                "Closest existing roles (sub-threshold — use as "
                "negative examples for what does NOT fit):\n"
                f"{gap.payload.get('closest', [])}\n\n"
                "Persist the draft as an artifact (kind=plan); the next "
                "step (role-designer) reads it directly."
            ) + demand_note
        if role == "role-designer":
            return (
                "Consume the role-researcher draft from the previous "
                "Phase 0 step. Author production-ready role definitions "
                "(V3 structure: AGENT_HEADER, purpose, expertise, "
                "protocol, tools, constraints). Persist each new role "
                "via okuro.roles.registry with maturity='draft'. After "
                "the user approves them in the deliberation panel they "
                "promote to 'active' and the original task's role-match "
                "is re-run."
            ) + demand_note
    return f"Phase 0 step {step}: gap={gap.kind}"
