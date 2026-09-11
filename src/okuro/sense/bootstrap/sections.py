# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bootstrap section builders — each produces (name, content, token_estimate).
# index:
#   imports
#   def _first_rule_text
#   def _comm_summary
#   def build_status_banner
#   def _cap_memory_lines
#   def build_profile
#   def build_conventions
#   def build_principles
#   def build_protocol
#   def build_tools
#   def build_tool_protocol
#   def build_hardware
#   def build_memory
#   def build_project
#   def build_thoughts
#   def build_roles
#   def build_codebase
#   def build_during_work
#   def build_progress
#   def build_design_profile
#   def build_stack
#   def _project_has_brand
#   def build_brand
#   def build_behavioral_section
#   def build_tool_protocol_doc
# AGENT_HEADER_END -->
"""Bootstrap section builders — each produces (name, content, token_estimate)."""

import json
import logging
import re
from pathlib import Path

from .budget import estimate_tokens
from ._safe import _safe
from okuro.db.engine import okuro_home

log = logging.getLogger("okuro.sense.bootstrap.sections")

_MEMORY_LINE_CAP = 500  # Max chars per memory line in bootstrap output

# Split of the 9 bootstrap memory pointer slots. Was 6 unconditional / 3
# relevant, i.e. two thirds of the surface ignored the task entirely. Now
# inverted: a small floor of cross-cutting system facts, the rest ranked
# against the task hint.
_BOOTSTRAP_SYSTEM_SLOTS = 2
_BOOTSTRAP_RELEVANT_SLOTS = 7

# Pointer body budgets (bytes, UTF-8 safe). Relevant rows get more room —
# 80 chars is not enough to decide whether a hit is worth opening.
_POINTER_BODY_SYSTEM = 80
_POINTER_BODY_RELEVANT = 200

# Per-assemble cache of memory IDs already rendered as bodies in build_memory.
# build_memory_index reads this to avoid surfacing the same memory twice
# (once as a one-line pointer, once as a full body). Cleared at the start
# of every build_memory call so successive assembles don't leak state.
_RENDERED_BODY_IDS: set[str] = set()


def _first_rule_text(item) -> str:
    """Coerce a rule entry (string or {rule,rationale,…} dict) to plain rule text."""
    if isinstance(item, dict):
        return str(item.get("rule", "")).strip()
    if isinstance(item, str):
        # Strip inline rationale (em-dash splitter) for banner compactness.
        return item.split(" — ", 1)[0].strip()
    return str(item).strip()


def _comm_summary(profile: dict) -> str:
    """One-line distillation of the user's communication profile.

    Used by the bootstrap status banner so agents (and the user) can
    verify at a glance that the profile loaded. Pulls from the same
    fields the behavioral contract reads, so the banner never
    contradicts the contract.
    """
    comm = profile.get("communication", {}) or {}
    parts: list[str] = []

    rl = comm.get("response_length")
    if rl:
        parts.append(str(rl))

    fp = comm.get("format_preferences")
    preferred: list = []
    if isinstance(fp, dict):
        preferred = list(fp.get("preferred") or [])
    elif isinstance(fp, list):
        preferred = list(fp)
    if preferred:
        pref_labels = [p for p in (_first_rule_text(x) for x in preferred[:2]) if p]
        if pref_labels:
            parts.append("/".join(pref_labels))

    directness = comm.get("directness") or comm.get("tone")
    if directness:
        parts.append(str(directness))

    return ", ".join(parts) or "default"


def _comm_prose(profile: dict) -> str:
    """Prose distillation of comm preferences for the greeting banner.

    Produces e.g. "concise communication with bullet points and tables".
    Meant to read as natural English — not a terse CSV like
    :func:`_comm_summary`.
    """
    comm = profile.get("communication", {}) or {}
    rl = str(comm.get("response_length") or "").strip()

    fp = comm.get("format_preferences")
    preferred: list = []
    if isinstance(fp, dict):
        preferred = list(fp.get("preferred") or [])
    elif isinstance(fp, list):
        preferred = list(fp)

    # Take up to three preferred formats, human-join them.
    labels = [p for p in (_first_rule_text(x) for x in preferred[:3]) if p]
    if len(labels) == 0:
        format_phrase = ""
    elif len(labels) == 1:
        format_phrase = labels[0]
    elif len(labels) == 2:
        format_phrase = f"{labels[0]} and {labels[1]}"
    else:
        format_phrase = f"{', '.join(labels[:-1])}, and {labels[-1]}"

    if rl and format_phrase:
        return f"{rl} communication with {format_phrase}"
    if rl:
        return f"{rl} communication"
    if format_phrase:
        return format_phrase
    return "structured, direct responses"


def build_status_banner(
    *,
    user: str | None,
    profession: str | None,
    comm_prose: str,
    provider: str | None,
    version: str,
    author: str | None = None,
    author_contact: str | None = None,
) -> str:
    """Proof-of-boot greeting emitted at the top of the packet.

    Shape::

        **OKURO <version>**
        <provider> is bootstrapped

        user is **<name>**, works as **<profession>**, likes **<comm>**.

        created by <author>
        <author_contact>

    The credit block is DATA, not a literal. ``author`` comes from
    ``identity.author`` in ``~/.okuro/config.yaml`` when set, else the
    profile name; ``author_contact`` is config-only. With neither, the
    banner falls back to a neutral product line and names nobody --
    a redistributed build must not ship one machine's owner as a
    hardcoded string.

    Agents quote this verbatim as the first block of their opening
    reply (see the directive inserted by the assembler). Internal blank
    lines are intentional — they survive reassembly because each \\n\\n
    inside the banner string stays inside the banner string; the
    packet's ``"\\n\\n".join(parts)`` joins between parts, not inside.
    """
    provider_label = provider or "an agent"
    name = user or "unknown"
    prof = profession or "unspecified"
    comm = comm_prose or "structured, direct responses"

    if author:
        credit = f"created by {author}"
        if author_contact:
            credit = f"{credit}  \n{author_contact}"
    else:
        credit = "created with okuro"

    return (
        f"**OKURO {version}**\n"
        f"{provider_label} is bootstrapped\n"
        f"\n"
        f"user is **{name}**, works as **{prof}**, likes **{comm}**.\n"
        f"\n"
        f"{credit}"
    )


def _cap_memory_lines(text: str, cap: int = _MEMORY_LINE_CAP) -> str:
    """Truncate individual memory content lines to cap chars. Defense in depth."""
    lines = text.split("\n")
    result = []
    for line in lines:
        if len(line) > cap and line.startswith("- "):
            result.append(line[:cap] + "...")
        else:
            result.append(line)
    return "\n".join(result)


# Sections that ``build_behavioral`` already renders verbatim. Excluded
# from the profile section to avoid stamping the same content twice
# (Cognitive Style + Communication appeared once under the operative
# Behavioral Contract and a second time under the Profile heading,
# burning budget and triggering the user's "repetition" pet peeve).
_PROFILE_DUP_SKIP = (
    # Operative blocks — rendered as directives in the Behavioral Contract, so
    # a second copy here is pure repetition (the user's #1 pet peeve).
    "cognitive_style", "communication",
    "expertise", "boundaries", "error_handling",
    # okuro web-UI configuration — not agent-actionable, and the useful design
    # info ships separately as the Design Profile section.
    "ui_adaptations", "onboarding", "welcome", "design",
    # THE THIRD "PRINCIPLES" HEADING. `yu.profile._format_markdown` titles every
    # top-level profile key, so `profile.principles` — a bare list of ids the
    # user selected — rendered its own `## Principles` H2. CORE therefore
    # shipped THREE headings containing the word, with three different
    # membership lists: the per-response core, this id list, and the standing
    # set that carries the bodies. An agent reading the packet as structure
    # cannot tell which one binds. The bodies render in `build_principles`;
    # the id list adds nothing an agent acts on.
    "principles",
)


def build_profile() -> tuple[str, str, int]:
    """Build profile section from okuro.yu, minus blocks already rendered
    by ``build_behavioral`` (Cognitive Style, Communication).

    Why the skip: those two sections drive the operative Behavioral
    Contract — agents read them as directives, not reference data. Showing
    them a second time inside Profile burns token budget without adding
    information and trips the user's "repetition" pet peeve. Identity,
    Expertise, Work Style, Decision Style, UI Adaptations, Boundaries,
    Onboarding, Welcome, Error Handling all stay — they're complementary,
    not duplicate.
    """
    try:
        from okuro.yu.profile import get_profile
        content = get_profile(
            format="markdown",
            section=None,
            exclude_sections=_PROFILE_DUP_SKIP,
        )
        return ("profile", content, estimate_tokens(content))
    except Exception as e:
        log.warning("bootstrap section %r failed: %s",
                    "sections.build_profile", e)
        from ._safe import FAILED_SECTIONS
        FAILED_SECTIONS.append(("sections.build_profile", str(e)))
        return ("profile", f"Profile unavailable: {e}", 10)


def build_behavioral() -> tuple[str, str, int]:
    """Build OPERATIVE behavioral contract — not reference data.

    These rules govern every response in the session. Framed as directive,
    not informational, so agents treat them as hard constraints.
    """
    body = build_behavioral_section()
    # THE ANCHOR IS LOAD-BEARING, not decoration. The per-turn hook ships a
    # COMPRESSED form of this contract — rules without their rationale — and
    # closes by pointing here for the reasoning. A pointer that names a heading
    # which later gets renamed is worse than no pointer: it tells the model to
    # look somewhere that does not exist. Both ends read
    # _profile_compliance.COMM_ANCHOR, so they cannot drift.
    from okuro.sense.providers._profile_compliance import COMM_ANCHOR

    header = (
        f"## Behavioral Contract [{COMM_ANCHOR}]\n\n"
        "**These rules govern EVERY response in this session. "
        "Re-check before each reply — format, autonomy, interruption policy.**\n"
    )
    content = f"{header}\n{body}"
    return ("behavioral", content, estimate_tokens(content))


def build_conventions() -> tuple[str, str, int]:
    """Build conventions section from config."""
    try:
        from okuro.yu.conventions import format_conventions_markdown
        content = format_conventions_markdown()
        return ("conventions", content, estimate_tokens(content))
    except Exception as e:
        log.warning("bootstrap section %r failed: %s",
                    "sections.build_conventions", e)
        from ._safe import FAILED_SECTIONS
        FAILED_SECTIONS.append(("sections.build_conventions", str(e)))
        return ("conventions", f"Conventions unavailable: {e}", 10)


def build_people(limit: int = 30) -> tuple[str, str, int]:
    """Build the people roster — who's around the user.

    Awareness layer: name · role/persona · org · relation. The role text
    carries each person's essence (their 'lens'); the full persona + sliders
    live behind the people module. Lets any agent answer 'who is X' and
    reason in a person's perspective without a tool call."""
    try:
        from okuro.orchestrator.api.people import list_people

        people = (list_people() or {}).get("people", [])
        if not people:
            return ("people", "", 0)
        lines = ["## People (who's around — name · persona · relation)"]
        for p in people[:limit]:
            name = p.get("display_name") or p.get("id") or "?"
            role = (p.get("role") or "").strip()
            if len(role) > 260:
                role = role[:257].rstrip() + "…"
            org = (p.get("organization") or "").strip()
            rel = (p.get("relation_to_user") or p.get("relation_type") or "").strip()
            head = f"- **{name}**" + (f" — {role}" if role else "")
            meta = " · ".join([b for b in (org, rel) if b])
            if meta:
                head += f" ({meta})"
            lines.append(head)
        content = "\n".join(lines)
        return ("people", content, estimate_tokens(content))
    except Exception as e:
        log.warning("bootstrap section %r failed: %s", "sections.build_people", e)
        from ._safe import FAILED_SECTIONS
        FAILED_SECTIONS.append(("sections.build_people", str(e)))
        return ("people", "", 0)


def _always_included_principle_ids() -> list[str]:
    """System-level principle IDs that MUST appear in bootstrap regardless
    of the user's selection list.

    Two families:
      * every active ``ORCH-*`` principle (orchestration / subagent
        coordination rules) — provider-agnostic operating rules for how
        agents coordinate subagents, applying to Claude Code, Cursor,
        Antigravity and Codex alike;
      * every active ``source = 'epistemic'`` principle (DP11-DP13:
        SYSTEM-OVER-SYMPTOM, NO-ASSUMPTIONS, PROOF-OVER-EVERYTHING) —
        rules about how to establish that something is TRUE.

    Neither should be similarity-gated or hidden behind a user preference.
    The epistemic set in particular must not be optional: an agent that has
    not been told to separate measured from inferred will confidently report
    a schema default as evidence, which is the failure that motivated them.
    """
    try:
        from okuro.db import get_db
        rows = get_db().fetchall(
            "SELECT id FROM principles "
            "WHERE (id LIKE 'ORCH-%' OR source = 'epistemic') AND active = 1"
        )
        return [r["id"] for r in rows]
    except Exception as exc:
        log.warning("bootstrap section %r failed: %s",
                    "sections._always_included_principle_ids", exc)
        from ._safe import FAILED_SECTIONS
        FAILED_SECTIONS.append(
            ("sections._always_included_principle_ids", str(exc))
        )
        return []


# Principles that govern EVERY response, as opposed to reference principles
# consulted when relevant. These are protected from budget truncation the same
# way the Behavioral Contract is, and are EXCLUDED from the main principles
# section so they appear exactly once.
#
# Why this split exists: principles rendered as one 1,148-token block at
# priority 5, so at the 5000 budget the section was cut to nothing and at 2000
# it never rendered. DP03 — "never ask without 3 options" — sat in the
# truncated tail, which is why agents kept asking open questions at a user
# whose profile says open questions trigger demand avoidance. Operative rules
# must not be droppable; that was the same defect the behavioral contract had.
#
# THE MEMBERSHIP LIST USED TO LIVE HERE, AS A TUPLE. That made promotion a code
# edit, and the comment above records DP03 being promoted that way — one
# principle at a time, by whoever noticed. DP10 (SYSTEMATIC-NOT-SPECIFIC) was
# never noticed, so the class-vs-instance rule stayed in the truncatable tail
# while its near-twin DP11 sat at the top; DP10's non-application is what
# triggered the 2026-07-29 instruction-system investigation. Promoting DP10 by
# editing a tuple would have been the third instance of one defect.
#
# Membership is now `principles.per_response_rank` (migration 119) — an
# attribute of the principle, read through
# okuro.sense.principles.per_response_principle_ids.
def _per_response_principle_ids() -> list[str]:
    """Per-response principle IDs in render order, from the principles data."""
    def _read():
        from okuro.sense.principles import per_response_principle_ids
        return per_response_principle_ids()

    return _safe("sections._per_response_principle_ids", _read, default=[]) or []


# Role-match confidence bands. match_roles() returns cosine against role
# descriptions embedded raw; SIMILARITY_THRESHOLD there is 0.40, which is the
# floor for "worth showing at all", NOT the bar for "adopt this".
_ROLE_STRONG = 0.55   # a role genuinely designed for this task
_ROLE_WEAK = 0.40     # something adjacent exists; do not adopt it silently
# Cap on the inlined brief. micro_prompt runs 171-708 tok across the catalog;
# `prompt` runs 4-6k and would dominate the packet, so it is never inlined.
_ROLE_BRIEF_MAX_TOKENS = 800


def build_role_assignment(task_hint: str | None) -> tuple[str, str, int]:
    """Decide which role — if any — this session should adopt.

    okuro has 91 roles and a role designer, and neither was reachable from a
    session start: bootstrap rendered the role CATALOG (a count and some domain
    names) and left the agent to work out whether one applied. So every session
    ran unroled by default, including the ones a purpose-built role existed for.

    This section makes the decision explicit and, when it is not clear-cut,
    hands it to the user as options rather than guessing — DP03: never ask
    without three options, each with approach, pros/cons and a recommendation.
    A silently auto-adopted wrong role is the role-shaped version of the
    wrong-project bind: confident, coherent and incorrect.
    """
    if not task_hint:
        return ("role_assignment", "", 0)

    def _gather():
        from okuro.roles.resolver import match_roles
        matches = match_roles(task_hint, top_k=4)
        if not matches:
            return ""

        best = matches[0]
        runners = matches[1:3]

        def _line(m):
            return f"`{m['id']}` ({m['domain']}, match {m['similarity']:.2f})"

        if best["similarity"] >= _ROLE_STRONG:
            # INLINE the brief rather than instructing a fetch. Measured
            # 2026-07-19 across claude / codex / antigravity on an identical
            # task: the packet named the role and told all three to call
            # roles_get, and 1 of 3 did. Same shape as the memory pointers —
            # anything behind a second tool call lands at roughly a third,
            # anything already in the payload lands at 100%. The compliance
            # gap was not a provider difference; all three otherwise scored
            # 7-8/8 on brain usage.
            brief = ""
            try:
                from okuro.roles.registry import get_role
                role = get_role(best["id"]) or {}
                # micro_prompt is the operative form (171-708 tok observed);
                # `prompt` is 4-6k and would dominate the packet.
                # SECOND SEAM. roles_get is the other one, and both must
                # sanitise — a body inlined here is never passed through
                # _format_role_assumption, so fixing only that path leaves the
                # packet carrying "# DO NOT EDIT" and `tools: [... grep ...]`
                # (measured first-person in a live packet, 2026-09-09).
                from okuro.roles.body_sanitize import sanitize_role_body

                brief = sanitize_role_body(
                    (role.get("micro_prompt") or role.get("content") or "").strip()
                )
            except Exception:
                log.warning("build_role_assignment: could not load brief for %s",
                            best["id"], exc_info=True)

            out = [
                "## Role for this session",
                "",
                f"**Adopt this role: {_line(best)}** — its brief is inlined "
                "below, so no further call is needed to start.",
            ]
            if brief:
                truncated = ""
                if estimate_tokens(brief) > _ROLE_BRIEF_MAX_TOKENS:
                    keep = _ROLE_BRIEF_MAX_TOKENS * 4
                    brief = brief[:keep].rstrip()
                    truncated = (
                        f"\n\n_[brief trimmed to fit — full version via "
                        f"`roles_get('{best['id']}')`]_"
                    )
                out += ["", brief + truncated]
            else:
                out += ["", f"Brief unavailable — load it with "
                            f"`roles_get('{best['id']}')` before planning."]
            if runners:
                alts = " · ".join(_line(m) for m in runners)
                out += ["", f"Close alternatives: {alts} — switch via "
                            f"`roles_get` if this one fights the task."]
            return "\n".join(out)

        # No role is clearly designed for this. Present the choice.
        band = "nothing close" if best["similarity"] < _ROLE_WEAK else "only an adjacent role"
        recommend = (
            "Option 1 — the task is specific enough that a purpose-built role "
            "will pay for itself across future sessions."
            if best["similarity"] < _ROLE_WEAK else
            f"Option 2 — `{best['id']}` is adjacent and cheap to try; design a "
            "role only if it turns out to fight the task."
        )
        return "\n".join([
            "## Role for this session — ASK BEFORE PROCEEDING",
            "",
            f"No role is designed for this task ({band}; closest is "
            f"{_line(best)}). Put these three options to the user and wait — "
            "do not pick one silently.",
            "",
            "| # | Option | Pros | Cons |",
            "|---|---|---|---|",
            "| 1 | Design a role now — `roles_learn` / the role-designer role | "
            "fits the task exactly; reusable for every future session on it | "
            "costs one turn up front |",
            f"| 2 | Adopt the closest existing role — `roles_get('{best['id']}')` | "
            "immediate, no setup | approximate fit; may carry constraints that "
            "do not apply |",
            "| 3 | Proceed as the general agent | zero overhead; right for "
            "one-off or exploratory work | no domain scaffolding, no role "
            "knowledge, no tool shortlist |",
            "",
            f"**Recommendation:** {recommend}",
        ])

    content = _safe("sections.build_role_assignment", _gather, default="")
    content = content or ""
    return ("role_assignment", content, estimate_tokens(content))


def build_recipient(task_hint: str | None) -> tuple[str, str, int]:
    """Point at the person profile when the task names a recipient.

    "draft the Q2 deck for Marco" should not make an agent guess who Marco is
    or how to pitch to him — okuro already stores his communication and
    cognitive profile. Nothing connected the two: the People section lists
    everyone unconditionally (measured 0% relevance to technical tasks, 393
    tokens) while the ONE case where a person matters — being named in the task
    — got no pointer at all.

    Deliberately a literal whole-token name match, not semantic similarity.
    With 13 people in the store, a semantic matcher would attach a person to
    tasks that merely sound social, and binding the wrong recipient is exactly
    the confident-and-wrong failure this packet is being repaired for.
    """
    if not task_hint:
        return ("recipient", "", 0)

    def _gather():
        from okuro.db import get_db
        import re as _re
        rows = get_db().fetchall(
            "SELECT id, display_name, organization, relation_to_user "
            "FROM persons WHERE active = 1"
        )
        hint = task_hint.lower()
        hits = []
        for r in rows:
            name = (r["display_name"] or "").strip()
            if not name:
                continue
            # Match on the full name OR any single name part >= 3 chars, so
            # "for Marco" resolves without the surname.
            parts = [name.lower()] + [p.lower() for p in name.split() if len(p) >= 3]
            for part in dict.fromkeys(parts):
                if _re.search(rf"(?<![\w-]){_re.escape(part)}(?![\w-])", hint):
                    hits.append(r)
                    break
        if not hits:
            return ""
        lines = ["## Recipient named in this task", ""]
        for r in hits[:3]:
            who = r["display_name"]
            org = f", {r['organization']}" if r["organization"] else ""
            rel = f" — {r['relation_to_user']}" if r["relation_to_user"] else ""
            lines.append(f"- **{who}**{org}{rel}")
        lines += [
            "",
            "Before writing anything addressed to them, load the profile: "
            "`person_get('<name>')` for the record, `person_lens('<name>')` "
            "for how to pitch to them specifically. Their communication "
            "preferences override the user's own format rules for that "
            "content — the user's rules govern how you talk to HIM.",
        ]
        return "\n".join(lines)

    content = _safe("sections.build_recipient", _gather, default="")
    content = content or ""
    return ("recipient", content, estimate_tokens(content))


def build_principles_core() -> tuple[str, str, int]:
    """The four principles that apply to every single response.

    Kept small (~210 tok) precisely so it can be uncompressible. Anything that
    is merely good advice belongs in `principles`, not here — this section's
    value comes from being short enough that it always survives.
    """
    def _gather():
        from okuro.sense.principles import get_principles
        ids = _per_response_principle_ids()
        if not ids:
            return ""
        # preserve_id_order: the order is per_response_rank, not stored
        # priority. Sorting by priority would drop DP10 (priority 2) below the
        # epistemic trio (priority 1) — back to the bottom of the block, which
        # is the position defect this section exists to prevent.
        body = get_principles(ids=ids, preserve_id_order=True)
        if not body.strip():
            return ""
        return (
            "## Working Principles — these govern EVERY response\n\n"
            + body
        )

    content = _safe("sections.build_principles_core", _gather, default="")
    content = content or ""
    return ("principles_core", content, estimate_tokens(content))


def build_principles() -> tuple[str, str, int]:
    """Build principles section from DB.

    If the user has selected and prioritized principles in their profile,
    emit those in that order. Otherwise emit all principles.

    ORCH-* principles are ALWAYS included on top of the user's selection
    (system-level orchestration rules — see ``_always_included_principle_ids``).
    """
    try:
        from okuro.sense.principles import get_principles

        # Check for user's principle selection
        def _load_selected():
            from okuro.yu.profile import get_profile_raw
            profile = get_profile_raw()
            user_princ = profile.get("principles", {})
            return user_princ.get("selected", []), user_princ

        loaded = _safe(
            "sections.build_principles.user_selection",
            _load_selected,
            default=([], {}),
        )
        selected, user_princ = loaded if loaded is not None else ([], {})

        if selected:
            # Force-include system-level ORCH-* rules in every bootstrap,
            # regardless of the user's selection list.
            final_ids = list(selected)
            for forced_id in _always_included_principle_ids():
                if forced_id not in final_ids:
                    final_ids.append(forced_id)
            content = get_principles(ids=final_ids)
            # Append custom principles if any
            custom = user_princ.get("custom", [])
            if custom:
                custom_lines = [
                    f"**{c.get('id', 'CUSTOM')}: {c.get('name', '?')}** — {c.get('description', '')}"
                    for c in custom
                ]
                content += "\n" + "\n".join(custom_lines)
        else:
            # No user selection — get_principles() already emits every active
            # principle (ORCH-* included).
            content = get_principles()

        # Drop the per-response core — build_principles_core already renders
        # it, uncompressibly. Emitting it twice burns budget and trips the
        # user's most-cited pet peeve.
        per_response = _per_response_principle_ids()
        content = "\n".join(
            ln for ln in content.splitlines()
            if not any(f"**{pid}:" in ln for pid in per_response)
        )

        # ITS OWN H2. Without one this block had no heading at all, so 17
        # principles rendered underneath whatever section happened to precede
        # them — in the live packet, "## Cross-project bridges —
        # tunnel_find(concept) for bodies". An agent reading the packet as
        # structure sees the value system filed under a code-navigation helper,
        # and okuro's own bootstrap-redesign data index already carried this as
        # a measurement caveat ("rows below Cross-project bridges are skewed —
        # the headerless DP block buckets under the preceding H2"). It was
        # noticed as a measurement artefact and never as a delivery one.
        content = content.strip()
        if content:
            # THE HEADING STAYS, ITS PARENTHETICAL DOES NOT. The H2 was added
            # deliberately (commit 2fb87eff) because without one these 17
            # principles rendered under whatever section happened to precede
            # them — in the live packet, "## Cross-project bridges". That fix
            # is untouched. What went is "(consult when relevant)", which made
            # the agent the judge of whether a principle applied to it.
            #
            # The set IS narrower in scope than the per-response core — SYS-LAN
            # bears on networking, DP01 on spending — and saying so is
            # precision, not hedging. The difference is WHEN each binds, never
            # WHETHER.
            content = (
                "## Standing Principles — each binds; they differ only in "
                "when they apply\n\n"
                + content
            )

        return ("principles", content, estimate_tokens(content))
    except Exception as e:
        log.warning("bootstrap section %r failed: %s",
                    "sections.build_principles", e)
        from ._safe import FAILED_SECTIONS
        FAILED_SECTIONS.append(("sections.build_principles", str(e)))
        return ("principles", f"Principles unavailable: {e}", 10)


def build_protocol() -> tuple[str, str, int]:
    """Build universal operating rules — applies to ALL agents.

    Rule text is sourced from data/agent_rules.yaml (rule id `close_out`), not
    a literal here, so the wording is dynamic settings data.
    """
    from .rules import render_rule
    content = render_rule("close_out")
    return ("protocol", content, estimate_tokens(content))


def _m5_legacy_bootstrap() -> bool:
    """OKURO_LEGACY_BOOTSTRAP=1 forces the pre-M5 full-fat sections.

    Mirrors the M4 OKURO_LEGACY_ROLE_INJECTION escape hatch. Per-task
    override (task.legacy_bootstrap=True) lives in the assembler caller
    chain; this flag is the global, one-off CLI override.
    """
    import os as _os
    return _os.environ.get("OKURO_LEGACY_BOOTSTRAP", "").lower() in (
        "1", "true", "yes",
    )


def build_tools(provider: str = "unknown") -> tuple[str, str, int]:
    """Build tools section — M5 metadata-only catalog by default.

    Three modes:
      - **Legacy** (OKURO_LEGACY_BOOTSTRAP=1): returns the pre-M5 full
        hardcoded reference with grouped tables and per-tool descriptions.
        ~7,175 chars / ~1,800 tokens.
      - **claude-code**: emits a 1-line directive pointing to the host's
        native ``ToolSearch`` (Claude Code's built-in deferred-schema
        loader). Claude Code already surfaces tool names in the system
        prompt; only schemas are deferred — no reason to duplicate the
        catalog. Smallest output.
      - **anything else** (antigravity/codex/cursor/local/unknown): emits a
        compact module index (module name + 1-line + per-tool name list,
        no schemas). Schemas via ``canon_get_tool(name)`` on demand.

    Default target: ≤1 KB. M5 success-check fixture for the tools sink.
    """
    if _m5_legacy_bootstrap():
        return _build_tools_legacy_full()
    if provider == "claude-code":
        return _build_tools_claude_directive()
    return _build_tools_module_index()


def _live_module_taxonomy() -> str:
    """Render the running registry's module taxonomy as markdown bullets.

    Generated, never hand-maintained. Three copies of this list used to be
    written by hand — two in this file, one in the canon ``okuro.yaml``
    entry — and all three still claimed 7 modules long after the
    registry served 21, so agents were briefed
    that whole subsystems (orchestrator, trace, prism, notes) did not
    exist. Falls back to a pointer line if the registry cannot be read,
    because a missing list beats a wrong one.
    """
    try:
        from okuro.mcp._registry import get_module_taxonomy
        entries = get_module_taxonomy()
    except Exception:  # pragma: no cover — registry unavailable in odd hosts
        return "- call `mcp_catalog()` for the live module list\n"
    if not entries:  # pragma: no cover — empty registry
        return "- call `mcp_catalog()` for the live module list\n"
    return "".join(
        f"- **{e['module']}** ({e['tool_count']}) — {e['description']}\n"
        for e in entries
    )


def _build_tools_claude_directive() -> tuple[str, str, int]:
    """Tools section for Claude Code — module taxonomy only.

    Claude Code 2.x natively defers schemas: callers see tool names in
    the available-tools list at session start, and ``ToolSearch`` loads
    full JSONSchema only when needed. The bootstrap directive only
    needs to remind the agent of the module taxonomy + the canonical
    flow (browse names → ToolSearch → call). Schemas are NOT injected.
    """
    content = (
        "## MCP Tools (okuro unified server)\n\n"
        "**Tools surface** — names appear in your available-tools list "
        "automatically; schemas load on demand via host-native "
        "`ToolSearch` (deferred-tools mechanism). Do NOT re-list "
        "schemas here.\n\n"
        "**Module taxonomy** — live, generated from the running registry. "
        "For the tools inside a module call "
        "`mcp_catalog(module='<name>')`; add `detail='full'` for schemas. "
        "`canon_list_tools` is NOT this — it registers installable CLIs "
        "and MCP servers and has no entry for an individual okuro tool.\n"
        + _live_module_taxonomy()
        + "\nWhen a tool you need is NOT in your loaded-tool list, call "
        "`ToolSearch(query=\"select:<name>[,<name>]\")` to load the "
        "schema, then invoke."
    )
    return ("tools", content, estimate_tokens(content))


def _build_tools_module_index() -> tuple[str, str, int]:
    """Tools section for non-Claude providers — live module taxonomy.

    No ToolSearch on Antigravity/Codex/Cursor/local. Tool NAMES still
    reach the model: MCP's own ``tools/list`` handshake puts every name
    and schema in context regardless of provider, so re-listing ~70 of
    them here was duplication that had also gone stale.

    What this section owes the agent is the taxonomy (which module owns
    what) plus a working way to enumerate and load schemas on demand.
    That used to be ``canon_get_tool(name)`` — which returns "Tool not
    found" for every one of okuro's own tools, because canon registers
    installable CLIs and MCP servers, not individual tools. ``mcp_catalog``
    is the working equivalent.
    """
    content = (
        "## MCP Tools (okuro unified server)\n\n"
        "Every tool name + schema is already in your context via the MCP "
        "`tools/list` handshake. The taxonomy below says which module owns "
        "what. To enumerate a module call `mcp_catalog(module='<name>')`; "
        "add `detail='full'` for descriptions + JSONSchema.\n\n"
        "`canon_list_tools` / `canon_get_tool` are NOT the schema source — "
        "they register installable CLIs and MCP servers, and return "
        "\"not found\" for an individual okuro tool.\n\n"
        "**Modules** — live, generated from the running registry:\n"
        + _live_module_taxonomy()
    )
    return ("tools", content, estimate_tokens(content))


def _build_tools_legacy_full() -> tuple[str, str, int]:
    """Pre-M5 full hardcoded catalog. Kept verbatim for legacy mode."""
    # Always emit the full reference — canon-based listing was too sparse (just names, no descriptions)
    content = """## MCP Tools (64 tools via okuro unified server)

### Codebase Navigation (cortex)
| Tool | Use for |
|------|---------|
| `cortex_scope()` | Introspection — indexed roots + per-project counts. Call FIRST in multi-repo workspaces to know which projects are actually indexed |
| `cortex_search(query, n?, file_type?, project?)` | Semantic search — finds files by meaning. Results tagged with `project` slug |
| `cortex_search_code(query, path?, project?, n?)` | Literal/regex pattern search (ripgrep). Pass `project=<slug>` to scope the walk |
| `cortex_route(query, project?)` | Quick lookup — top 5 files for a concept. "Where does X live?" |
| `cortex_read_header(path)` | File purpose + section index. Read this BEFORE the full file. Accepts absolute or any-registered-root-relative paths |
| `cortex_read_section(path, start_line, end_line)` | Read specific line range from a file |
| `cortex_read_file(path)` | Read entire file (prefer header+section for large files) |
| `cortex_navigate(path, direction)` | List directory contents (up/down/siblings) |
| `cortex_context(path)` | Quick preview: header + first 2000 chars |

### Memory & Knowledge (sense)
| Tool | Use for |
|------|---------|
| `write_memory(topic, content, project?, confidence?)` | Store factual learning for future agents (gotcha/convention/decision/architecture) |
| `read_memory(query?, topic?, project?, limit?)` | Search persistent cross-agent learnings |
| `capture_thought(content, category?, project?)` | Save idea/observation/question for the USER to revisit |
| `search_thoughts(query?, category?, status?)` | Search through captured thoughts |
| `update_thought(thought_id, status?, content?)` | Update thought status or content |
| `daily_digest(limit?)` | Unresolved thoughts, action items, forgotten ideas |

### Progress & Projects (sense)
| Tool | Use for |
|------|---------|
| `project_status(slug)` | **Where a project stands** — phase plan, progress, inventory, sessions, staleness verdict |
| `log_progress(project, status, summary, files_touched?, next_steps?, phase?)` | Record work milestone |
| `get_progress(project, agent?, include_history?, history_limit?)` | Get recent progress for a project |
| `project_phases_set(project, phases, replace?)` | Declare/advance the phase plan — makes "how far along" queryable |
| `list_projects()` | List active projects |
| `get_project(slug)` | Get project details (path, stack, ports, roles) |

### Session & Compliance (sense)
| Tool | Use for |
|------|---------|
| `session_report(feedback?)` | **MANDATORY** end-of-session. Rate tools used/useful |
| `session_score()` | Check your current session compliance status |
| `session_history(project?, provider?, limit?)` | Recent sessions with compliance scores |
| `tool_performance(tool_name?, server?, days?)` | Tool usage stats from telemetry |
| `compliance_scorecard(provider?, days?)` | Provider compliance trends |

### People & Communication (sense)
| Tool | Use for |
|------|---------|
| `person_lens(person_id, context?)` | Communication guidance for a person (tone, formality, preferences) |
| `person_match(query, limit?)` | Find people matching a description |
| `person_add/get/list/update` | CRUD for communication partners |

### Todos (sense)
**Reach for this when you discover an actionable item the user needs to see later** — not a persistent learning (→ `write_memory`), not an ephemeral idea for the user to revisit (→ `capture_thought`), not a time-scheduled nudge (→ `set_reminder`). Todos surface on the Now page.

| Tool | Use for |
|------|---------|
| `todo_add(title, detail?, priority?, project?, due_at?)` | Register an actionable item (source defaults to 'agent') |
| `todo_list(status?, project?, limit?)` | List active todos (default: open+doing by priority) |
| `todo_get(todo_id)` | Fetch one |
| `todo_update(todo_id, ...)` | Patch — title/detail/status/priority/project/due_at |
| `todo_done(todo_id)` | Mark done (stamps completed_at) |

### Reminders (sense)
| Tool | Use for |
|------|---------|
| `set_reminder(what, when, urgency?, channels?, repeat?)` | Create reminder with auto-cascade |
| `list_reminders(status?, upcoming_hours?)` | List reminders with filters |
| `snooze_reminder(id, duration_min?)` | Snooze (default: 15min for ADHD profile) |
| `dismiss_reminder(id)` / `acknowledge_reminder(id)` | Dismiss or mark as seen |
| `accept_suggestion(suggestion_id)` / `reject_suggestion(suggestion_id)` | Handle system-proposed reminders |

### Secrets (keyring)
| Tool | Use for |
|------|---------|
| `keyring_get(name)` | Retrieve secret (API keys, tokens, passwords) |
| `keyring_set(name, value)` | Store secret |
| `keyring_list()` | List secret names (no values) |
| `keyring_delete(name)` | Delete secret |

### Expert Roles (roles)
**Adopt a role when ANY of these is true** — not on every task:
- Task will touch **2+ files** or span **20+ minutes** of work
- Task requires **domain judgment** (security, design, architecture, quality, research, writing, marketing, content)
- User used an expertise verb: *"design / plan / architect / review / audit / research / write X"*
- User explicitly named a role or domain

**Skip role adoption** for one-line answers, single mechanical edits, lookups, status checks, meta/debug tasks. Overhead beats benefit below the threshold.

Flow when triggered: `roles_match(task)` → `roles_get(role_id)` → operate as the returned role for the remainder of the task.

| Tool | Use for |
|------|---------|
| `roles_match(task, top_k?)` | **Start here** — semantic match from task to best role |
| `roles_get(role_id)` | Adopt the role; the response is a binding operating directive, not reference material |
| `roles_list(domain?)` | Browse all roles (56 across 11 domains) |
| `roles_domains()` | List role domains |
| `roles_knowledge(role_id, task_hint?)` | Inspect a role's accumulated learnings |
| `roles_maintenance(role_id?)` | Get research mandate for stale roles (auto-flagged in `roles_get`) |

### System State (system)
| Tool | Use for |
|------|---------|
| `sysinfo_gpu_status(gpu_id?)` | GPU VRAM, temp, power, processes |
| `sysinfo_storage_status()` | Disk usage, RAID health, free space |
| `sysinfo_docker_status(category?)` | Docker container states |
| `sysinfo_port_status(range_start?, range_end?)` | Find available ports |
| `sysinfo_service_health(services?)` | HTTP health checks on services |
| `sysinfo_system_overview()` | Combined GPU + storage summary |

### LLM Bridge (bridge)
| Tool | Use for |
|------|---------|
| `bridge_invoke(prompt, capability?, provider?)` | Route prompt to best LLM (claude/codex/antigravity/local) |
| `bridge_providers()` | List providers and routing table |
| `bridge_status()` | Bridge health check |

### Other (sense)
| Tool | Use for |
|------|---------|
| `bootstrap(task_hint?, provider?)` | Full agent context (you already called this) |
| `brain_advise(task_hint)` | LLM-powered task briefing (local model, free) |
| `get_profile(section?)` / `update_profile(...)` | Read/update user profile |
| `get_principles(ids?)` | Get working principles |
| `run_maintenance()` | Run system hygiene cycle |

### Registry (canon)
| Tool | Use for |
|------|---------|
| `canon_list_tools()` / `canon_get_tool(name)` | Browse registered tool definitions |
| `canon_list_skills()` / `canon_get_skill(name)` | Browse registered skill definitions |
| `canon_validate()` | Validate registry consistency |"""
    return ("tools", content, estimate_tokens(content))


def _nested_coverage(db, project_slug: str) -> tuple[str, int, Path] | None:
    """Is this project's code indexed under a DIFFERENT root's slug?

    Returns ``(host_slug, doc_count, project_path)`` when the project has a
    real path on disk that falls inside another registered root which holds
    documents for it, else ``None``.

    Exists because "indexed" is a question about a PATH, while cortex_docs is
    keyed by the owning root's SLUG. Asking the slug question and reporting the
    path answer is what produced the false "NOT indexed" for every nested
    project. The longest containing root wins, matching
    :func:`okuro.cortex.roots.project_for_path`.
    """
    row = db.fetchone("SELECT path FROM projects WHERE id = ?", (project_slug,))
    raw = row["path"] if row else None
    if not raw:
        return None
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, ValueError):
        return None
    if not p.exists():
        return None

    from okuro.cortex.roots import registered_roots

    best = None
    best_len = -1
    for root in registered_roots():
        if not root.project or root.project == project_slug:
            continue
        try:
            if p.is_relative_to(root.path) and len(root.path.parts) > best_len:
                best_len = len(root.path.parts)
                best = root
        except (AttributeError, ValueError):
            continue
    if best is None:
        return None

    cnt = db.fetchone(
        "SELECT COUNT(*) n FROM cortex_docs "
        "WHERE project = ? AND file_path LIKE ? AND deleted_at IS NULL",
        (best.project, f"{p}/%"),
    )
    n = (cnt or {})["n"] if cnt else 0
    return (best.project, n, p) if n else None


def build_tool_protocol(project_slug: str | None = None) -> tuple[str, str, int]:
    """Tell agents WHERE TO LOOK — with the coverage fact that makes it binding.

    Rewritten 2026-07-19 after measuring, across an identical task on three
    providers, that claude made ZERO cortex calls in two runs while codex made
    2-4 and antigravity 3 every time. The packet was the cause, not the model:

      * it cited an enforcement that exists only in Claude Code — "Grep
        (blocked by hook for broad searches)" — which is meaningless to codex
        and antigravity, and inert in claude itself whenever the cwd has no
        .okuro-index.yaml (check-grep.py gates on that file, not on whether
        cortex is reachable);
      * it then handed the agent a self-declared escape hatch — "If cortex is
        unavailable (no index), Grep and Glob work normally" — whose
        precondition the agent has no way to check. Faced with an unverifiable
        condition and a familiar native tool, the rational move is to grep.

    So the section now states the coverage FACT for this project, cites the
    measured difference, and drops every provider-specific claim. A rule an
    agent can verify and whose cost it can see is followed; a rule that
    asserts unverifiable enforcement is treated as advice.
    """
    def _coverage() -> str:
        from okuro.db import get_db
        db = get_db()
        if project_slug:
            row = db.fetchone(
                "SELECT COUNT(*) n FROM cortex_docs "
                "WHERE project = ? AND deleted_at IS NULL", (project_slug,)
            )
            n = (row or {})["n"] if row else 0
            if n:
                return (
                    f"**This project is indexed: {n:,} documents under "
                    f"`{project_slug}`.** cortex covers the code you are about "
                    f"to work on — you do not need to check first."
                )

            # A slug miss is NOT an index miss. cortex_docs.project holds the
            # slug of the ROOT that owns the file, so every project whose code
            # lives INSIDE another registered root counts zero here while being
            # fully indexed under the parent. Measured 2026-08-30 on a tool
            # project nested four levels inside a workspace repo: bootstrap
            # said "NOT in the cortex index" while cortex_search returned that
            # project's own cli.py at 0.875 — and the agent then hit the
            # check-grep hook, which gates on the index and was correctly
            # refusing the grep bootstrap had just recommended. Bootstrap and
            # the hook contradicted each other; the hook was right.
            nested = _safe(
                "sections.build_tool_protocol.nested",
                lambda: _nested_coverage(db, project_slug),
                default=None,
            )
            if nested:
                host_slug, host_n, host_path = nested
                return (
                    f"**This project IS indexed — under the slug "
                    f"`{host_slug}`, not `{project_slug}`.** Its code lives at "
                    f"`{host_path}`, inside the `{host_slug}` root, so cortex "
                    f"stores it there: {host_n:,} documents under that prefix. "
                    f"Pass `project=\"{host_slug}\"` to `cortex_search`, and "
                    f"prefer `cortex_search_code` for an exact symbol — it "
                    f"walks all roots and needs no scope."
                )

            return (
                f"**`{project_slug}` has no documents in the cortex index, and "
                f"no indexed root contains its path.** Confirm with "
                f"`cortex_scope()` before concluding code is absent — a "
                f"project with an unset or stale `path` reports this way even "
                f"when its code is indexed. Native Grep/Glob/Read are correct "
                f"only once you have confirmed that."
            )
        tot = db.fetchone(
            "SELECT COUNT(*) n FROM cortex_docs WHERE deleted_at IS NULL"
        )
        n = (tot or {})["n"] if tot else 0
        return (
            f"**No project bound this session. {n:,} documents are indexed "
            f"across all roots** — call `cortex_scope()` once to see which, "
            f"before reaching for Grep."
        )

    coverage = _safe("sections.build_tool_protocol.coverage", _coverage,
                     default="") or ""

    # Rule text sourced from data/agent_rules.yaml (`tool_routing`); only the
    # live coverage line (doc count / project binding) is dynamic per session
    # and is injected as the prefix. No routing prose is hardcoded here.
    from .rules import render_rule
    content = render_rule("tool_routing", prefix=coverage)
    return ("tool_protocol", content, estimate_tokens(content))


def build_hardware() -> tuple[str, str, int]:
    """Build hardware section — live system snapshot."""
    lines = ["## System"]

    # Try live GPU status
    def _gpu_lines():
        from okuro.system.gpu import get_gpu_status
        gpu_data = get_gpu_status()
        out = []
        for gpu in gpu_data.get("gpus", []):
            name = gpu.get("name", f"GPU{gpu.get('id', '?')}")
            hw = gpu.get("model", "unknown")
            vram = gpu.get("vram", {})
            vram_used = vram.get("used_mb", 0)
            vram_total = vram.get("total_mb", 0)
            temp = gpu.get("temperature_c", "?")
            out.append(f"- **{name}**: {hw} — {vram_used}/{vram_total} MB VRAM, {temp}°C")
        return out

    lines.extend(_safe("sections.build_hardware.gpu", _gpu_lines, default=[]) or [])

    # Try live storage status
    def _storage_lines():
        from okuro.system.storage import get_storage_status
        storage = get_storage_status()
        out = []
        for fs in storage.get("mounts", []):
            mount = fs.get("path", "?")
            use_pct = fs.get("usage_percent", "?")
            avail = fs.get("free_gb", "?")
            label = fs.get("label", mount)
            out.append(f"- **{label}** ({mount}): {use_pct}% used, {avail} GB free")
            if isinstance(use_pct, (int, float)) and use_pct > 85:
                out.append(f"  - WARNING: {label} storage above 85%")
        return out

    lines.extend(_safe("sections.build_hardware.storage", _storage_lines, default=[]) or [])

    if len(lines) == 1:
        lines.append("- System info: use system tools for live data")

    content = "\n".join(lines)
    return ("hardware", content, estimate_tokens(content))


def build_tunnels(project_slug: str = None, limit: int = 8) -> tuple[str, str, int]:
    """Bootstrap surface for cross-project memory tunnels.

    M5 default: top-3 concepts, one-line heading, ``concept (Nm/Np)``
    per row — bodies via ``tunnel_find(concept)``. Target ≤300 B.
    Legacy mode (OKURO_LEGACY_BOOTSTRAP=1) restores the prior verbose
    heading + 8-row default.
    """
    legacy = _m5_legacy_bootstrap()
    effective_limit = limit if legacy else 3
    parts: list[str] = []

    def _gather():
        from okuro.sense.tunnels import tunnel_concepts_touching, tunnel_concepts

        if project_slug:
            rows = tunnel_concepts_touching(
                project=project_slug, min_project_count=2,
                limit=effective_limit,
            )
        else:
            rows = tunnel_concepts(min_count=2, project=None,
                                   limit=effective_limit)
            rows = [r for r in rows if (r.get("project_count") or 0) >= 2]
        if not rows:
            return

        if legacy:
            if project_slug:
                heading = (
                    f"## Cross-project insights for {project_slug}\n"
                    "*Concepts tunnel-linked across projects — call "
                    "tunnel_find(concept) to load the cross-project memories.*"
                )
            else:
                heading = (
                    "## Cross-project insights (no active project — top bridges)\n"
                    "*Concepts tunnel-linked across multiple projects — call "
                    "tunnel_find(concept) to load the cross-project memories.*"
                )
            parts.append(heading)
            for r in rows:
                parts.append(
                    f"- **{r['concept']}** — {r['memory_count']} memories "
                    f"across {r['project_count']} projects"
                )
        else:
            parts.append(
                "## Cross-project bridges — `tunnel_find(concept)` for bodies"
            )
            for r in rows:
                parts.append(
                    f"- {r['concept']} ({r['memory_count']}m/"
                    f"{r['project_count']}p)"
                )

    _safe("sections.build_tunnels", _gather)
    if parts:
        content = "\n".join(parts)
        return ("tunnels", content, estimate_tokens(content))
    return ("tunnels", "", 0)


def build_memory_index(task_hint: str = None, project_slug: str = None,
                       limit: int = 30,
                       exclude_ids: set[str] | None = None) -> tuple[str, str, int]:
    """Build compact AAAK-style memory pointer index.

    M5: caller-supplied ``limit`` honoured. Legacy flag pegs to 30
    (pre-M5 default); M5 default returns empty — the trimmed Memory
    pointers block (system + task-relevant top-9) already covers
    discovery, and an extra 10-row index per spawn was duplicate
    surface for the same data. Bodies always via ``read_memory``.

    Surfaces 25-40 one-line pointers ([topic] label | @entities | #flags
    | →id (scope)) — bigger surface area, smaller body than ``build_memory``.
    Agent reads this, then opens specific memories via ``read_memory``
    on-demand.

    ``exclude_ids`` (passed by the assembler from ``build_memory``) is the
    set of full memory IDs already rendered as bodies in the Memory section.
    Pointer rows for those IDs are dropped to avoid the same memory
    appearing twice (once as a one-liner, once as a body) — pure token
    waste with no extra information.
    """
    if limit <= 0:
        return ("memory_index", "", 0)

    parts: list[str] = []
    exclude = exclude_ids or set()

    def _gather():
        from okuro.sense.memory_index import read_pointers, render_pointer_lines

        # Widen the read so dedup doesn't shrink the visible surface below
        # the caller's intended `limit`.
        raw = read_pointers(query=task_hint, project=project_slug,
                            limit=limit + len(exclude))
        if not raw:
            return
        rows = [r for r in raw if r.get("memory_id") not in exclude][:limit]
        if not rows:
            return
        lines = render_pointer_lines(rows)
        if not lines:
            return
        parts.append("## Memory Index (compact pointers — open via read_memory)")
        parts.extend(lines)

    _safe("sections.build_memory_index", _gather)
    if parts:
        content = "\n".join(parts)
        return ("memory_index", content, estimate_tokens(content))
    return ("memory_index", "", 0)


def build_memory(task_hint: str = None, project_slug: str = None) -> tuple[str, str, int]:
    """Build memory section.

    M5 default: emits a single pointer block instead of full bodies.
    Bodies are loaded on demand by the subagent via
    ``read_memory(query=..., project=..., topic=...)``. Cuts ~7.5 KB
    off the bootstrap reply on a typical spawn.

    Legacy (OKURO_LEGACY_BOOTSTRAP=1): pre-M5 behaviour — system-wide
    top-10 bodies + task-relevant top-3, line-dedup'd, capped at 500
    chars per line via _cap_memory_lines.

    Side-effect: populates ``_RENDERED_BODY_IDS`` with the IDs of every
    memory rendered as a body so ``build_memory_index`` can avoid
    surfacing the same memory twice. M5 path leaves the set empty since
    no bodies are rendered.
    """
    _RENDERED_BODY_IDS.clear()

    if _m5_legacy_bootstrap():
        return _build_memory_legacy_full(task_hint, project_slug)

    parts: list[str] = []

    def _gather():
        from okuro.sense.memory import _read_memory_rows

        # System-wide pointer projection — title + topic + project +
        # one-line description (≤120 chars), no body. Mirrors the
        # roles-slice pattern from M4.
        #
        # system_only=True, NOT project=None: the latter means "no project
        # filter". Measured 2026-07-16 before this line changed — all 6
        # slots held project rows (another project's GCP-region trivia,
        # a third project's pricing) and `bootstrap_system` had surfaced 0 of the 299
        # project-IS-NULL memories in the 2 months it ran. Every session was
        # briefed with another project's trivia under a "system-wide" header.
        #
        # Ranking stays confidence DESC (the fallback's ORDER BY) rather than
        # an earned signal, deliberately: every earned signal in the schema is
        # downstream of the confidence fall-through that ran until f619baba.
        # Measured — 101/101 confidence-1.0 rows have last_accessed set vs
        # 17-39% of every other bucket, and read_memory surfaced 1.0 rows at
        # 2.98x their corpus share. surface_log has no column separating a
        # semantic hit from a fall-through hit, so the polluted period cannot
        # be mined retroactively; ranking on it would launder asserted
        # confidence into a signal that merely LOOKS earned. The signal has to
        # be re-earned post-fix before it can rank anything.
        #
        # This scope fix alone evicts the legacy 1.0s without touching a row:
        # 0 of the 70 confidence-1.0 memories are project IS NULL. Within this
        # pool confidence tops out at 0.97 and 71 rows tie at >=0.95 for 6
        # slots, so created_at DESC is the effective ranker — recency is the
        # one signal the fall-through never touched.
        # Rebalanced 2026-07-19: was limit=6 with NO query, so 6 of 9 pointers
        # — 67% of the memory surface on every spawn — were selected with zero
        # reference to the task. With 69 rows tied at confidence >=0.95 for 6
        # slots, created_at DESC was the real ranker, so the SAME six rows went
        # to every session until someone wrote a newer system memory. Agents
        # opened a Figma theming task with Ardour colour-file paths and the
        # same Mac SSH fact twice (rows 4 and 6 were near-duplicates).
        #
        # That is repetition, old information and clutter — three of the user's
        # explicitly stated hates — generated structurally on every call, and
        # the single largest contributor to "agents only remembered irrelevant
        # stuff". Worse, it is self-reinforcing: a section that is mostly noise
        # teaches agents to skip the section, after which fixing the ranking
        # recovers nothing.
        #
        # Now 2 unconditional system rows (genuinely cross-cutting facts still
        # deserve a floor) + 7 relevance-ranked. The relevance machinery
        # already worked; this surface simply never called it.
        system_rows = _read_memory_rows(limit=_BOOTSTRAP_SYSTEM_SLOTS,
                                        query=task_hint or None,
                                        system_only=True,
                                        _surface_context="bootstrap_system")
        ptrs: list[str] = []
        for r in system_rows or []:
            ptrs.append(_render_memory_pointer(r))

        task_rows = []
        if task_hint:
            task_rows = _read_memory_rows(query=task_hint,
                                          limit=_BOOTSTRAP_RELEVANT_SLOTS,
                                          project=project_slug,
                                          _surface_context="bootstrap_relevant")
        seen_ids = {r["id"] for r in (system_rows or [])}
        for r in task_rows or []:
            if r["id"] in seen_ids:
                continue
            ptrs.append(_render_memory_pointer(r, relevant=True))

        if not ptrs:
            return
        parts.append("## Agent Memory (pointers — bodies via `read_memory`)")
        parts.append(
            "*Task-relevant memories as pointers — bodies are truncated. "
            "To open one, call `read_memory(memory_id='<the →handle>')`. "
            "Do NOT pass a handle as `query`: that runs a semantic search on "
            "a hex string and returns an unrelated memory.*"
        )
        parts.extend(ptrs)

    _safe("sections.build_memory", _gather)

    if parts:
        content = "\n".join(parts)
        return ("memory", content, estimate_tokens(content))
    return ("memory", "", 0)


def _render_memory_pointer(r: dict, *, relevant: bool = False) -> str:
    """One-line memory pointer: `- [topic] @scope — <desc> →id`.

    The chevron id is the handle for the follow-up `read_memory` call. It was
    promised by this docstring and never emitted — the return statement built
    the line without it — so every pointer was an unopenable fragment. The
    sibling renderer (`sense/memory_index.render_pointer_lines`) emitted
    `→{mid}` correctly the whole time; the two disagreed and nothing caught it.

    Task-relevant rows get a longer body than the unconditional system rows:
    80 chars was too short to judge whether a hit was worth opening, which
    compounded the irrelevance problem — an agent could not tell a useful
    pointer from a useless one, so it skipped all of them.
    """
    topic = (r.get("topic") or "?").strip()
    scope = r.get("project") or "system"
    body = (r.get("content") or "").strip().replace("\n", " ")
    cap = _POINTER_BODY_RELEVANT if relevant else _POINTER_BODY_SYSTEM
    if len(body.encode("utf-8")) > cap:
        body = body.encode("utf-8")[: cap - 1].decode("utf-8", errors="ignore") + "…"
    flag = " (relevant)" if relevant else ""
    mid = (r.get("id") or "")[:8]
    handle = f" →{mid}" if mid else ""
    return f"- [{topic}]{flag} @{scope} — {body}{handle}"


def _build_memory_legacy_full(task_hint, project_slug) -> tuple[str, str, int]:
    """Pre-M5 full-body memory section. Kept verbatim for legacy flag."""
    parts: list[str] = []

    def _gather():
        from okuro.sense.memory import _read_memory_rows

        # system_only=True — same defect as the M5 path above; the legacy
        # flag must not resurrect a surface we just proved emits foreign
        # project rows under a "system-wide" header.
        system_rows = _read_memory_rows(limit=10, system_only=True,
                                        _surface_context="bootstrap_system")
        system_lines: set[str] = set()
        if system_rows:
            for r in system_rows:
                _RENDERED_BODY_IDS.add(r["id"])
            system_content = _format_memory_rows(system_rows)
            parts.append(f"## Agent Memory (system-wide)\n{_cap_memory_lines(system_content)}")
            system_lines = {ln for ln in system_content.splitlines() if ln.strip()}

        if task_hint:
            task_rows = _read_memory_rows(query=task_hint, limit=3,
                                          project=project_slug,
                                          _surface_context="bootstrap_relevant")
            if task_rows:
                task_content = _format_memory_rows(task_rows)
                novel_lines = [
                    ln for ln in task_content.splitlines()
                    if ln.strip() and ln not in system_lines
                ]
                if novel_lines:
                    novel_set = set(novel_lines)
                    for r in task_rows:
                        line = _format_memory_rows([r])
                        if line in novel_set:
                            _RENDERED_BODY_IDS.add(r["id"])
                    novel = "\n".join(novel_lines)
                    parts.append(f"## Memories Relevant to This Task\n{_cap_memory_lines(novel)}")
                    parts.append("*If you discover something non-obvious, persist it with write_memory() so future agents benefit.*")

    _safe("sections.build_memory", _gather)

    if parts:
        content = "\n\n".join(parts)
        return ("memory", content, estimate_tokens(content))
    return ("memory", "", 0)


def _format_memory_rows(rows: list[dict]) -> str:
    """Format memory rows like read_memory's text output.

    Mirrors the bullet format in okuro.sense.memory.read_memory so the
    body section text is identical whether produced via row-fetch or via
    the legacy string return path.
    """
    lines = []
    for r in rows:
        scope = f"[{r['project']}]" if r.get("project") else "[system]"
        sim = r.get("_similarity")
        if sim is not None:
            lines.append(
                f"- **{r['topic']}** {scope} (relevance: {sim:.2f}): {r['content']}"
            )
        else:
            lines.append(f"- **{r['topic']}** {scope}: {r['content']}")
    return "\n".join(lines)


def build_project(project_slug: str = None) -> tuple[str, str, int]:
    """Build project section for a specific project."""
    if not project_slug:
        return ("project", "", 0)

    def _via_helper():
        from okuro.sense.projects import get_project
        project = get_project(project_slug)
        if project:
            return ("project", f"## Project: {project}" if not project.startswith("##") else project,
                    estimate_tokens(str(project)))
        return None

    helper_result = _safe("sections.build_project.helper", _via_helper)
    if helper_result is not None:
        return helper_result

    # Fallback: direct DB query
    def _via_db():
        from okuro.db import get_db
        db = get_db()
        row = db.fetchone(
            "SELECT id, name, path, url, stack, port_range, roles, description, design_profile, charter "
            "FROM projects WHERE id = ?",
            (project_slug,),
        )
        if not row:
            return None
        lines = [f"## Project: {row['name']} ({row['id']})"]
        if row.get("description"):
            lines.append(row["description"])
            lines.append("")
        # Charter parity with get_project (DP10): the per-project sub-bootstrap
        # doctrine renders on this fallback path too, not just the helper path.
        if row.get("charter"):
            lines.append("### Charter")
            lines.append(row["charter"].strip())
            lines.append("")
        # Same sentinel rule as sense/projects.py: `unknown/<slug>` is a
        # placeholder, not a directory. Never render it as a path — an agent
        # that reads a Path acts on it.
        from okuro.sense.overview import _path_is_real

        if _path_is_real(row.get("path")):
            lines.append(f"- **Path**: `{row['path']}`")
        else:
            lines.append(
                "- **Path**: none — this project has no directory. "
                "Do not cd, search or write anywhere on its behalf."
            )
        if row.get("url"):
            lines.append(f"- **URL**: {row['url']}")
        stack = json.loads(row["stack"]) if isinstance(row.get("stack"), str) else (row.get("stack") or [])
        if stack:
            lines.append(f"- **Stack**: {', '.join(stack)}")
        if row.get("port_range"):
            lines.append(f"- **Ports**: {row['port_range']}")
        roles = json.loads(row["roles"]) if isinstance(row.get("roles"), str) else (row.get("roles") or [])
        if roles:
            lines.append(f"- **Roles**: {', '.join(roles)}")
        if row.get("design_profile"):
            lines.append(f"- **Design Profile**: `{row['design_profile']}`")
        content = "\n".join(lines)
        return ("project", content, estimate_tokens(content))

    db_result = _safe("sections.build_project.db", _via_db)
    if db_result is not None:
        return db_result

    return ("project", "", 0)


def build_todos(project_slug: str = None, limit: int = 5) -> tuple[str, str, int]:
    """Build todos section — open + doing items the user (or a prior agent)
    flagged as actionable.

    Closes the structural blindspot where todos lived only in the web Now
    page: CLI agents had no way to see "what to do next" beyond the
    free-form ``log_progress.next_steps`` string. With this section, every
    bootstrap surfaces the active project's top priority/due todos so the
    agent can pick them up without the user having to repeat them.
    """
    def _gather():
        from okuro.sense.todos import todo_list

        rows = todo_list(project=project_slug, limit=limit)
        # Fall back to system-wide todos when the project lookup is empty —
        # cross-project items (chores, follow-ups) shouldn't go invisible
        # just because today's task hint resolved to a specific project.
        if not rows and project_slug:
            rows = todo_list(project=None, limit=limit)
        if not rows:
            return None

        def _fmt(r: dict) -> str:
            prio = r.get("priority", 3)
            badge = {5: "P0", 4: "P1", 3: "P2", 2: "P3", 1: "P4"}.get(prio, f"p{prio}")
            status = r.get("status") or "open"
            scope = f" [{r['project']}]" if r.get("project") else ""
            due = f" · due {r['due_at'][:10]}" if r.get("due_at") else ""
            title = (r.get("title") or "(untitled)").strip()
            return f"- **{badge}** {status}{scope}{due} — {title}"

        lines = ["## Todos (open + doing — `todo_list` for full set, `todo_done` to close)"]
        lines.extend(_fmt(r) for r in rows)

        # Disposition surface (P0-4): a SMALL batch of the stalest open todos,
        # for a human keep-or-drop call. Never age-expired automatically — the
        # 30-90d band holds real commitments. Drained by decision, gradually.
        # De-duped against the active list already shown above.
        from okuro.sense.todos import todos_needing_review
        shown = {r.get("id") for r in rows}
        stale = [s for s in todos_needing_review(older_than_days=30, limit=8,
                                                 project=project_slug)
                 if s.get("id") not in shown][:3]
        if stale:
            lines.append("")
            lines.append(
                "**Stale — untouched >30d, review: keep (`todo_update`) or drop "
                "(`todo_update` status='dropped', reason=…):**"
            )
            for s in stale:
                age = ""
                if s.get("updated_at"):
                    age = f" · {s['updated_at'][:10]}"
                title = (s.get("title") or "(untitled)").strip()
                scope = f" [{s['project']}]" if s.get("project") else ""
                lines.append(f"- {scope} {title}{age}")

        content = "\n".join(lines)
        return ("todos", content, estimate_tokens(content))

    result = _safe("sections.build_todos", _gather)
    if result is not None:
        return result
    return ("todos", "", 0)


def build_reminders(limit: int = 5, upcoming_hours: float = 168.0) -> tuple[str, str, int]:
    """Build reminders section — pending/active/snoozed within the next week.

    Reminders are time-anchored nudges (vs todos which are open-ended
    actionable items). Bootstrap surfaces the next ``limit`` so the agent
    can flag anything due during the work session — previously reminders
    were UI-only and CLI agents had no awareness of them.
    """
    def _gather():
        from okuro.sense.reminders.engine import list_reminders
        from datetime import datetime, timezone

        result = list_reminders(upcoming_hours=upcoming_hours)
        rows = (result or {}).get("reminders") or []
        rows = rows[:limit]
        if not rows:
            return None

        now = datetime.now(timezone.utc)

        def _fmt(r: dict) -> str:
            what = (r.get("what") or "(unspecified)").strip()
            when_raw = r.get("when_due") or ""
            try:
                when_dt = datetime.fromisoformat(when_raw.replace("Z", "+00:00"))
                if when_dt.tzinfo is None:
                    when_dt = when_dt.replace(tzinfo=timezone.utc)
                delta = when_dt - now
                if delta.total_seconds() < 0:
                    when_label = f"OVERDUE by {-delta.days}d" if delta.days else "OVERDUE"
                elif delta.days >= 1:
                    when_label = f"in {delta.days}d"
                else:
                    hours = int(delta.total_seconds() // 3600)
                    when_label = f"in {hours}h" if hours else "<1h"
            except Exception:
                when_label = when_raw[:16] or "?"
            urgency = r.get("urgency") or ""
            urg = f" ({urgency})" if urgency else ""
            status = r.get("status") or "pending"
            return f"- **{when_label}**{urg} — {what} _[{status}]_"

        lines = [
            "## Reminders (next 7 days — `list_reminders`, `snooze_reminder`, `acknowledge_reminder`)"
        ]
        lines.extend(_fmt(r) for r in rows)
        content = "\n".join(lines)
        return ("reminders", content, estimate_tokens(content))

    result = _safe("sections.build_reminders", _gather)
    if result is not None:
        return result
    return ("reminders", "", 0)


def build_distill_candidates(limit: int = 3) -> tuple[str, str, int]:
    """Mined lessons waiting on a human decision.

    The review surface has to PUSH. A queue that only answers when asked is a
    queue the owner will not remember to open — his words — and a lesson that is
    never reviewed never becomes a rule, which quietly turns the whole
    self-improvement loop into a table nobody reads.

    So the packet carries the count and the top few. This is NOT a new
    injection channel (the architecture decision forbids one): it is the same
    bootstrap composition that already surfaces todos and reminders, and it
    renders NOTHING when the queue is empty. Zero noise is the condition for
    being allowed to interrupt at all — a block that says "0 candidates" every
    session is one the reader learns to skip, and then the non-zero case is
    invisible too.

    Bounded to ``limit`` lines of lesson plus a header and a command, so the
    whole section stays under ten rendered lines however long the queue gets.
    """
    def _gather():
        from okuro.sense.distill.lessons import lessons_for_review

        rows = lessons_for_review(limit=max(limit, 1), status="candidate")
        if not rows:
            # Silent when empty. The packet budget is finite and re-injecting
            # an empty queue every session spends it on nothing.
            return None

        from okuro.db import get_db

        total = (get_db().fetchone(
            "SELECT COUNT(*) AS n FROM distill_lessons WHERE status = 'candidate'"
        ) or {}).get("n", len(rows))

        lines = [
            f"## Lesson candidates awaiting your review ({total})",
            "_Mined from your own sessions. None is in force until you approve "
            "it; approving is what makes it a rule._",
        ]
        for row in rows[:limit]:
            # Collapse whitespace: a mined lesson can carry newlines, and one
            # of them would break the single-line bullet this section promises.
            text = " ".join(str(row["lesson_text"]).split())
            lines.append(
                f"- **#{row['id']}** [{row['lesson_class']}] {text[:150]} "
                f"— corroborated ×{row['corroboration_count']}"
            )
        if total > limit:
            lines.append(f"- … and {total - limit} more")
        lines.append(
            "`okuro distill lessons` to read them · "
            "`okuro distill approve <id> --by <you>` · "
            "`okuro distill reject <id> --reason '<why>'`"
        )
        content = "\n".join(lines)
        return ("distill_candidates", content, estimate_tokens(content))

    result = _safe("sections.build_distill_candidates", _gather)
    if result is not None:
        return result
    return ("distill_candidates", "", 0)


#: A thought captured by the improvement pipeline before 2026-09-09 carries the
#: curation advice baked into its CONTENT: "… (6x). → Add to TOOL-PROTOCOL.md —
#: every agent needs this context." That advice is addressed to the owner, and
#: three of its variants name actions okuro's own rules forbid an agent from
#: taking — writing TOOL-PROTOCOL.md, writing CLAUDE.md, creating a .md file.
#:
#: MEASURED: 349 stored rows. The writer is fixed (improvement.py no longer
#: appends it), but those rows are already in the store and surface in every
#: packet whose task matches them. Stripping HERE rather than migrating the
#: rows keeps the advice on the owner's own surfaces — thought search, the
#: improvement report — where it is correctly addressed.
_THOUGHT_CURATION_TAIL = re.compile(r"\s*\(\d+x\)\.\s*→\s*.*$", re.DOTALL)


def build_thoughts(task_hint: str = None, project_slug: str = None) -> tuple[str, str, int]:
    """Build thoughts section — relevant unresolved thoughts."""
    def _gather():
        from okuro.sense.thoughts import surface_relevant
        if task_hint:
            content = surface_relevant(task_hint, limit=3, project=project_slug)
            if content:
                content = "\n".join(
                    _THOUGHT_CURATION_TAIL.sub("", ln) for ln in content.splitlines()
                )
                return ("thoughts", f"## Relevant Thoughts\n{content}", estimate_tokens(content))
        return None

    result = _safe("sections.build_thoughts", _gather)
    if result is not None:
        return result
    return ("thoughts", "", 0)


def build_roles() -> tuple[str, str, int]:
    """Build roles section from okuro.roles — prescriptive, not advisory.

    Roles are okuro's primary delivery mechanism for curated expertise — we
    deliberately do NOT distribute them as provider-native skills (skill
    containers cap at 8 KB body / 140 char descriptions, which would force
    content-loss for container-compliance). Keeping roles MCP-only preserves
    full prompt fidelity, single source of truth, freshness tracking, and
    richer matching. The bootstrap section must therefore ensure agents
    actually reach for `roles_match` — hence the directive framing.
    """
    try:
        from okuro.roles.registry import list_roles, get_domains
        roles = list_roles()
        domains = get_domains()
        domain_names = ", ".join(sorted(domains)) if domains else "none"
        lines = [
            "## Roles",
            "",
            f"**{len(roles)} curated expert roles** across {len(domains)} "
            "domains, each with a tuned prompt, accumulated learnings, and "
            "freshness tracking. Roles are okuro's primary delivery path for "
            "domain expertise — invoking a role beats writing generic output.",
            "",
            f"**Domains:** {domain_names}",
            "",
            "### When to adopt a role",
            "Adopt a role when **ANY** of these is true — not on every task:",
            "- Task will touch **2+ files** or span **20+ minutes** of work",
            "- Task requires **domain judgment** (security, design, "
            "architecture, quality, research, writing, marketing, content)",
            "- User used an expertise verb: *\"design / plan / architect / "
            "review / audit / research / write X\"*",
            "- User explicitly named a role or domain",
            "",
            "**Skip role adoption** for one-line answers, single mechanical "
            "edits, lookups, status checks, meta/debug tasks. Below the "
            "threshold, overhead beats benefit.",
            "",
            "### How to adopt a role",
            "1. `roles_match(task_description)` → returns best match + "
            "similarity score. If `match_type` is `fallback`, no domain role "
            "fit confidently — proceed generically.",
            "2. `roles_get(role_id)` → returns a **role-assumption directive**. "
            "Treat the returned body as your binding operating context for the "
            "remainder of the task, not as reference material.",
            "3. If the directive flags staleness, call "
            "`roles_maintenance(role_id)` for a research mandate before "
            "high-stakes execution.",
        ]
        content = "\n".join(lines)
        return ("roles", content, estimate_tokens(content))
    except Exception as exc:
        log.warning("bootstrap section %r failed: %s",
                    "sections.build_roles.registry", exc)
        from ._safe import FAILED_SECTIONS
        FAILED_SECTIONS.append(("sections.build_roles.registry", str(exc)))

    content = (
        "## Roles\n\n"
        "Call `roles_match(task)` when the task has a clear domain, then "
        "`roles_get(role_id)` to adopt the matched role. The returned body "
        "is a binding operating directive, not reference material."
    )
    return ("roles", content, estimate_tokens(content))


def _indexed_roots_block() -> str:
    """Live-rendered list of managed cortex roots for the bootstrap packet.

    Surfaces the ``default__*`` managed clones (source-of-truth code) so an
    agent sees them on turn 0 and never mistakes a bare-slug local mirror for
    the whole picture — the exact miss that let a scoped-empty search read as
    "code does not exist". Regenerated every bootstrap from the live registry,
    so it cannot rot into a stale snapshot. Best-effort: returns "" on any
    failure so bootstrap never breaks on it.
    """
    try:
        from okuro.cortex.roots import registered_roots
        from okuro.cortex.vectorstore import VectorStore
    except Exception:
        return ""
    try:
        roots = registered_roots()
    except Exception:
        return ""
    if not roots:
        return ""

    files_by_proj: dict = {}
    try:
        for p in VectorStore().get_stats().get("per_project", []):
            files_by_proj[p.get("project")] = p.get("files") or 0
    except Exception:
        pass

    total = sum(1 for r in roots if r.project)
    managed = sorted(r.project for r in roots if r.project and "__" in r.project)

    lines = [
        "### Indexed roots (cortex) — full live list via `cortex_scope`",
        f"- {total} roots indexed. A `<workspace>__<repo>` slug is a MANAGED "
        "CLONE (source-of-truth code); a bare slug is a local dir. Scope to the "
        "right slug — an empty scoped result is NOT proof code is absent; for an "
        "exact symbol use `cortex_search_code` (walks ALL roots, no scope needed).",
    ]
    if managed:
        parts = [
            f"{m} ({files_by_proj[m]})" if files_by_proj.get(m) else m
            for m in managed
        ]
        lines.append("- Managed repos: " + " · ".join(parts))
    return "\n".join(lines)


def build_codebase() -> tuple[str, str, int]:
    """Build codebase section — project directories + indexed cortex roots."""
    def _gather():
        from okuro.yu.conventions import get_convention
        lines: list[str] = []

        dirs = get_convention("directories", {})
        if dirs:
            lines.append("## Codebase")
            if isinstance(dirs, dict):
                for path, purpose in dirs.items():
                    lines.append(f"- `{path}` -- {purpose}")
            elif isinstance(dirs, list):
                for d in dirs:
                    lines.append(f"- `{d.get('path', '')}` -- {d.get('purpose', '')}")

        roots_block = _indexed_roots_block()
        if roots_block:
            if not lines:
                lines.append("## Codebase")
            lines.append(roots_block)

        if not lines:
            return None
        content = "\n".join(lines)
        return ("codebase", content, estimate_tokens(content))

    result = _safe("sections.build_codebase", _gather)
    if result is not None:
        return result
    return ("codebase", "", 0)


def build_during_work() -> tuple[str, str, int]:
    """Build agent behavior protocol — how to interact with the user and system."""
    content = """## Agent Behavior

### Context Check
- If this bootstrap packet includes an **Intake** section with questions, present P0 questions before starting work
- User can say "just go" to skip — respect that immediately

### Capture & Persist
- **User rules** — if user says NEVER/ALWAYS/must → persist as memory via `write_memory`
- **Ideas** — if user says "what if..."/"we should..." → offer to capture as thought via `capture_thought`
- **Learnings** — gotchas, conventions, non-obvious decisions → `write_memory` so future sessions benefit
- **Progress** — `log_progress` at meaningful milestones, not every tiny step

### Communication
- Lead with the answer, skip preamble
- When presenting choices, give short options with pros/cons. Limit to 2-3 with a recommendation
- Don't restate what the user said — just do it
- Don't switch topics mid-response — finish the current thread first
- If changing your approach, say why — silent behavior changes break trust"""
    return ("during_work", content, estimate_tokens(content))


def build_recent(days: int = 10, limit: int = 7) -> tuple[str, str, int]:
    """What changed RECENTLY — time-ordered, never similarity-gated.

    Every other memory surface in this packet is gated on similarity to the
    task hint. That is correct for "what do I know about X" and useless for
    "what has happened here lately", because recency and relevance are
    different questions and a task hint cannot express the second one.

    The gap is documented and was deferred: a 2026-04-21 architecture memory
    names it exactly — "hard-earned procedural truths sit in the memory tier
    where semantic luck decides whether the next agent sees them".

    It became load-bearing on 2026-05-24, when commit 61a87eb1 trimmed the
    30-row memory index out of the packet as a token saving, on the stated
    grounds that "the pointer block already covers discovery". Fourteen days
    earlier the _SIM_FLOOR misread had silently turned that pointer block into
    a recency sort, and no eval existed to catch it until 2026-07-15. Memory
    stopped arriving unasked, the fetch that replaced it returned noise, and
    no rule told anyone to fetch. Two months of confident nonsense followed.

    This section is deliberately NOT the fix for retrieval — that shipped
    2026-07-17. It is the fix for ARRIVAL: a starting agent should learn that
    the store was blind until last week, or that four gates landed yesterday,
    without having to guess the right query first.

    Cheap by construction: newest N rows, one truncated line each, no
    embedding call, no similarity maths.
    """
    def _gather():
        from okuro.db import get_db

        db = get_db()
        rows = db.fetchall(
            # Supersession is STRUCTURAL, not a flag: the NEW row carries
            # `supersedes` pointing at the old one, so a retracted memory is
            # any row somebody points at. There is no `superseded_by` column —
            # guessing that name is what made this section fail its first
            # render. Same predicate memory.py and artifacts.py use; confidence
            # alone is not enough, because a supersede leaves the old row at
            # full confidence and it would otherwise render as current.
            """SELECT id, topic, project, content, created_at
                 FROM agent_memory
                WHERE created_at > datetime('now', ?)
                  AND confidence > 0.3
                  AND id NOT IN (
                      SELECT supersedes FROM agent_memory
                       WHERE supersedes IS NOT NULL AND supersedes != ''
                  )
             ORDER BY created_at DESC
                LIMIT ?""",
            (f"-{int(days)} days", int(limit)),
        )
        if not rows:
            return None

        lines = [
            f"## Recent — what changed in the last {days} days",
            "",
            "_Newest first, across ALL projects. Time-ordered, NOT matched to "
            "your task — this is the context you would not know to ask for. "
            "Open any with `read_memory(memory_id='<handle>')`._",
            "",
        ]
        for r in rows:
            body = " ".join((r["content"] or "").split())
            if len(body) > 190:
                body = body[:190].rsplit(" ", 1)[0] + "…"
            day = (r["created_at"] or "")[:10]
            scope = r["project"] or "system"
            lines.append(
                f"- **{day}** [{r['topic']}] @{scope} — {body} →{(r['id'] or '')[:8]}"
            )
        content = "\n".join(lines)
        return ("recent", content, estimate_tokens(content))

    result = _safe("sections.build_recent", _gather)
    if result is not None:
        return result
    return ("recent", "", 0)


def build_progress(project_slug: str = None) -> tuple[str, str, int]:
    """Build progress section with session history."""
    if not project_slug:
        return ("progress", "", 0)

    def _gather():
        from okuro.sense.progress import get_progress
        # history_limit=3, not include_history=True. `include_history` renders
        # every entry at full length: measured 2026-08-07, that is 5591 tokens
        # for project `okuro` (20 entries) against a 13000-token packet that
        # already assembles to ~11.6k — 43% of the budget for a section
        # priced at 150. The compact form costs a median of 142 tokens across
        # all 45 projects holding history, p90 209, max 220.
        #
        # 3 is where the marginal entry stops paying: the 4th and 5th add
        # ~70 tokens each for work two sessions old, while the packet's whole
        # remaining headroom is ~1400.
        content = get_progress(project_slug, history_limit=3)
        if not content:
            return None
        # The nag surface. A status view that cannot flag its own staleness is
        # the failure this whole feature exists to avoid, and bootstrap is the
        # only surface EVERY session reads — so the warning costs zero extra
        # tool calls and reaches an agent that would never think to ask.
        #
        # Brain activity only, no git subprocess: this runs on the bootstrap
        # hot path and a repo probe would put a process spawn in it. The
        # cheaper signal is enough to catch the case that matters (work
        # happened, nobody logged it); project_status does the full comparison
        # including git HEAD when someone asks deliberately.
        warn = ""
        try:
            from okuro.sense.status import _brain_activity, _age_days, _STALE_DAYS
            from okuro.db import get_db
            db = get_db()
            row = db.fetchone(
                "SELECT updated_at FROM progress WHERE project = ? "
                "ORDER BY updated_at DESC LIMIT 1", (project_slug,))
            p_age = _age_days(row["updated_at"]) if row else None
            b_age = _brain_activity(db, project_slug).get("age_days")
            if p_age is not None and b_age is not None and p_age - b_age > _STALE_DAYS:
                warn = (
                    f"\n\n⚠ **Progress is {round(p_age)}d old; this project moved "
                    f"{round(b_age)}d ago.** Work happened that nobody logged — treat "
                    f"the status above as UNVERIFIED. `project_status('{project_slug}')` "
                    f"for the full comparison."
                )
        except Exception:
            warn = ""
        body = f"## Progress\n{content}{warn}"
        return ("progress", body, estimate_tokens(body))

    result = _safe("sections.build_progress", _gather)
    if result is not None:
        return result
    return ("progress", "", 0)


def build_design_profile(project_slug: str = None) -> tuple[str, str, int]:
    """Build design profile section for the resolved project.

    M5 default: emits the profile id + accent + typeface only — full
    palette + constraints + casing rules fetched via
    ``okuro.design.profiles.get_profile(id)`` on demand. Cuts ~2.6 KB
    on tasks that don't touch UI.

    Legacy mode (OKURO_LEGACY_BOOTSTRAP=1) restores the full block.

    When a brand is bound to the project, the brand section (build_brand) owns
    the design rendering — this function returns empty to avoid duplication.
    """
    import os
    import yaml

    # Brand takes precedence: the unified brand block renders the design slot
    # in its own section so don't emit twice.
    if project_slug and _project_has_brand(project_slug):
        return ("design_profile", "", 0)

    # The design system okuro ships. Was `architecture-noir`, a v0 profile,
    # until p10 deleted that layer (2026-09-06).
    _DEFAULT_PROFILE = "okuro-ds"
    profile_id = None

    # Fallback chain: project-bound projects.design_profile
    #                 → user-level profile.design.kit (then .profile, legacy)
    #                 → _DEFAULT_PROFILE (okuro-ds)
    # Project binding wins because per-project stack decisions are the
    # most specific. User-level selection applies when no project context
    # is resolved (CLI default, fresh bootstrap). Without this fallback
    # the user's onboarding Visual-Identity pick is a dead write —
    # DesignStep patches profile.design.profile but nothing reads it.

    # 1. project-bound
    if project_slug:
        def _project_design():
            from okuro.db import get_db
            db = get_db()
            row = db.fetchone(
                "SELECT design_profile FROM projects WHERE id = ?",
                (project_slug,),
            )
            if row and row.get("design_profile"):
                return row["design_profile"]
            return None

        profile_id = _safe(
            "sections.build_design_profile.project_lookup",
            _project_design,
        )

    # 2. user-level profile.design.profile
    if not profile_id:
        def _user_design():
            from okuro.yu.profile import get_profile_raw
            user_profile = get_profile_raw() or {}
            user_design = user_profile.get("design") or {}
            if isinstance(user_design, dict):
                # `design.kit` IS THE ACTIVE DESIGN SYSTEM; `design.profile` is
                # a legacy key, read only for installs made before it existed.
                # Same order and same reason as design_engine.api::_active_kit_id.
                candidate = user_design.get("kit") or user_design.get("profile")
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            return None

        profile_id = _safe(
            "sections.build_design_profile.user_profile",
            _user_design,
        )

    # 3. default
    if not profile_id:
        profile_id = _DEFAULT_PROFILE

    # RESOLVED THROUGH THE STACK REGISTRY, which is engine-first. This called
    # okuro.design.profiles directly, so this block — which appears in EVERY
    # agent packet — would have gone empty the moment v0 was retired, and gone
    # empty QUIETLY, because `_safe` swallows the failure into a FAILED_SECTIONS
    # row nobody reads. The same swallow already hid a broken import here once;
    # the comment this replaces records that.
    def _load_design():
        from okuro.stack.registry import _resolve_design_profile
        return _resolve_design_profile(profile_id)

    profile = _safe("sections.build_design_profile.yaml_load", _load_design)
    if profile is None:
        return ("design_profile", "", 0)

    if not profile:
        return ("design_profile", "", 0)

    if not _m5_legacy_bootstrap():
        accent = ""
        visual = profile.get("visual", {}) or {}
        palette = visual.get("palette", {}) or {}
        accent = palette.get("accent") or ""
        if isinstance(accent, dict):
            accent = accent.get("default") or ""
        typo = visual.get("typography", {}) or {}
        typeface = typo.get("primary") or ""
        # NO `v{version}`. That key holds the profile's SOURCE TIER — the live
        # value is the string 'user' — so the line rendered `Profile
        # `standard` vuser` in 8 of 9 project halves: a nonsense token stated
        # as a fact. A version the packet cannot state truthfully is not
        # stated.
        bits = [f"Profile `{profile_id}`"]
        if typeface:
            bits.append(f"typeface {typeface}")
        if accent:
            bits.append(f"accent {accent}")
        # The palette core is INLINED because there is no other way to reach
        # it. `profile_view` has no MCP tool (its only callers are
        # stack/registry.py and tests), and the operating rules ban both a
        # cross-project Python import and a shell into okuro's own internals.
        # The line used to point at `okuro.design_engine.profile_view` — a
        # route no agent can take.
        fg = palette.get("foreground") or {}
        bg = palette.get("background") or {}
        fg_primary = fg.get("primary") if isinstance(fg, dict) else fg
        bg_base = bg.get("base") if isinstance(bg, dict) else bg
        if fg_primary:
            bits.append(f"foreground {fg_primary}")
        if bg_base:
            bits.append(f"background {bg_base}")
        # THE FETCHER HINT NAMES THE ENGINE, not v0. Every agent packet carried
        # this line, so a stale pointer here teaches the whole fleet to reach
        # for a retired module. Casing and terminology are deliberately NOT
        # named any more: they are the brand's `voice` slot since 2026-09-03,
        # and a design pointer that promises them re-fuses the split.
        content = (
            f"## Design Profile: {profile.get('name', profile_id)}\n"
            + " · ".join(bits)
            + ". Use these values. This project has no brand bound, so there "
            "is no further palette to fetch."
        )
        return ("design_profile", content, estimate_tokens(content))

    # Format as concise markdown
    lines = [f"## Design Profile: {profile.get('name', profile_id)}"]
    lines.append(f"*{profile.get('description', '')}*")
    lines.append(f"Profile: `{profile_id}` v{profile.get('version', '?')}")

    tokens = profile.get("tokens", {})
    if tokens:
        token_paths = []
        if tokens.get("css"):
            token_paths.append(f"CSS: `{tokens['css']}`")
        if tokens.get("json"):
            token_paths.append(f"JSON: `{tokens['json']}`")
        if token_paths:
            lines.append(f"Tokens: {', '.join(token_paths)}")

    lines.append("")

    # Visual section
    visual = profile.get("visual", {})
    if visual:
        lines.append("### Visual")

        palette = visual.get("palette", {})
        if palette:
            bg = palette.get("background", {})
            fg = palette.get("foreground", {})
            accent = palette.get("accent", "")
            if isinstance(accent, dict):
                accent = accent.get("default", "")
            if bg:
                lines.append(f"- **Backgrounds**: {', '.join(f'{k}: {v}' for k, v in bg.items() if isinstance(v, str))}")
            if fg:
                lines.append(f"- **Foreground**: {', '.join(f'{k}: {v}' for k, v in fg.items() if isinstance(v, str))}")
            if accent:
                lines.append(f"- **Accent**: {accent}")
            for c in palette.get("constraints", []):
                lines.append(f"- ! {c}")

        typo = visual.get("typography", {})
        if typo:
            lines.append(f"- **Typeface**: {typo.get('primary', '?')}")
            if typo.get("fallback"):
                lines.append(f"- **Fallback**: {typo['fallback']}")
            if typo.get("hierarchy"):
                lines.append(f"- **Hierarchy**: {typo['hierarchy']}")
            for c in typo.get("constraints", []):
                lines.append(f"- ! {c}")

        layout = visual.get("layout", {})
        if layout:
            if layout.get("density"):
                lines.append(f"- **Density**: {layout['density']}")
            if layout.get("grid_base"):
                lines.append(f"- **Grid**: {layout['grid_base']} base")
            for c in layout.get("constraints", []):
                lines.append(f"- ! {c}")

        interaction = visual.get("interaction", {})
        if interaction:
            if interaction.get("philosophy"):
                lines.append(f"- **Interaction**: \"{interaction['philosophy']}\"")
            for p in interaction.get("patterns", []):
                lines.append(f"  - {p}")

        lines.append("")

    # VOICE, FROM THE BRAND'S OWN SLOT — not from the design profile.
    #
    # This section read `profile["language"]`, because v0 fused a brand's voice
    # into its design profile. The owner settled that on 2026-09-03 — "language
    # belongs to the brand not the design" — so the source moved to the `voice`
    # slot and this renders from there.
    #
    # WHY IT STILL RENDERS HERE AT ALL. build_design_profile only runs for a
    # project with NO brand bound (a brand-bound project gets build_brand
    # instead). Dropping the section outright would therefore have deleted the
    # voice from exactly the packets that have no brand block to carry it —
    # a silent loss in the case least able to absorb it.
    lines.extend(_voice_lines(project_slug))

    content = "\n".join(lines)
    return ("design_profile", content, estimate_tokens(content))


def _voice_lines(project_slug: str | None) -> list[str]:
    """The brand voice as bootstrap lines, or [] when none is resolvable.

    Fallback chain mirrors the design one: a voice bound to the project's brand
    wins, else the shipped default. Rendered from `voice_profile_rules`, whose
    kinds are open-ended — an unknown kind is shown rather than dropped, so a
    voice that grows a new rule kind reaches agents without editing this.
    """
    def _load():
        from okuro.db import get_db
        db = get_db()
        row = None
        if project_slug:
            row = db.fetchone(
                "SELECT v.* FROM voice_profiles v "
                "JOIN brand_slots s ON s.ref_id = v.id AND s.slot_kind = 'voice' "
                "JOIN stack_project_brand p ON p.brand_id = s.brand_id "
                "WHERE p.project_slug = ?",
                (project_slug,),
            )
        if row is None:
            row = db.fetchone(
                "SELECT * FROM voice_profiles WHERE status = 'active' "
                "ORDER BY id LIMIT 1"
            )
        if row is None:
            return None
        row = dict(row)
        rules: dict[str, list[dict]] = {}
        for r in db.fetchall(
            "SELECT kind, key, value FROM voice_profile_rules "
            "WHERE profile_id = ? ORDER BY kind, sort_order, key",
            (row["id"],),
        ):
            r = dict(r)
            rules.setdefault(r.pop("kind"), []).append(r)
        row["rules"] = rules
        return row

    voice = _safe("sections.build_design_profile.voice", _load)
    if not voice:
        return []

    out = ["", f"### Voice: {voice.get('name') or voice['id']}"]
    for label, key in (("Tone", "tone"), ("Locale", "locale"),
                       ("Casing", "casing"), ("Sentences", "sentences")):
        if voice.get(key):
            out.append(f"- **{label}**: {voice[key]}")

    rules = voice.get("rules") or {}
    for item in rules.get("formatting", []):
        out.append(f"- **{item['key']}**: `{item['value']}`")
    prefer = rules.get("prefer", [])
    if prefer:
        out.append("- **Prefer**: " + ", ".join(
            f"'{i['key']}' not '{i['value']}'" for i in prefer))
    for label, kind in (("Avoid", "avoid"), ("Never", "banned_phrase")):
        items = rules.get(kind, [])
        if items:
            out.append(f"- **{label}**: " + ", ".join(f"'{i['key']}'" for i in items))
    for item in rules.get("constraint", []):
        out.append(f"- ! {item['key']}")

    # An unrecognised kind is SHOWN, not dropped — the rules table is
    # deliberately open-ended, and a voice that grows a kind should reach
    # agents without a code change here.
    for kind, items in sorted(rules.items()):
        if kind in {"formatting", "prefer", "avoid", "banned_phrase", "constraint"}:
            continue
        out.append(f"- **{kind}**: " + ", ".join(
            f"{i['key']}{'=' + i['value'] if i['value'] else ''}" for i in items))
    return out


# --- Behavioral section builder (used by provider adapters) ---

def build_stack(project_slug: str = None) -> tuple[str, str, int]:
    """Build the tech-stack section for the resolved project.

    Renders the stack profile bound to the project (if any) as a compact
    grouped table: one row per approved entry, grouped by category. Silently
    returns empty if the stack module or its schema is unavailable (the
    migration may not have run yet in older DBs).

    If the project is bound to a brand, the brand section (build_brand) owns
    the stack rendering — this function returns empty to avoid duplication.
    """
    if not project_slug:
        return ("stack", "", 0)

    # Brand takes precedence: skip this section when a brand is bound so the
    # unified brand block is the single source of truth at bootstrap time.
    if _project_has_brand(project_slug):
        return ("stack", "", 0)

    def _resolve_stack():
        from okuro.stack import active_profile_for
        return active_profile_for(project_slug)

    resolved = _safe("sections.build_stack.active_profile", _resolve_stack)
    if not resolved:
        return ("stack", "", 0)

    lines = [f"## Tech Stack: {resolved.get('label', resolved['name'])}"]
    desc = resolved.get("description") or ""
    if desc:
        lines.append(f"*{desc}*")
    lines.append(f"Profile: `{resolved['name']}`")
    lines.append("")

    category_order = ["runtime", "frontend", "backend", "api", "ops"]
    by_cat = resolved.get("by_category", {})

    for cat in category_order:
        entries = by_cat.get(cat)
        if not entries:
            continue
        lines.append(f"### {cat.capitalize()}")
        lines.append("| Layer | Choice | Version | Status |")
        lines.append("|-------|--------|---------|--------|")
        for e in entries:
            version = e.get("version") or "—"
            lines.append(
                f"| {e['layer_name']} | **{e['name']}** (`{e['id']}`) "
                f"| {version} | {e['status']} |"
            )
        lines.append("")

    lines.append(
        "*Use `stack_match(need)` before installing a new dep — if the registry "
        "has an approved choice, use it. If not, `stack_propose` with a rationale.*"
    )

    content = "\n".join(lines).rstrip()
    return ("stack", content, estimate_tokens(content))


def _project_has_brand(project_slug: str) -> bool:
    """True iff the project is bound to a brand. Fail-safe: False on any
    error (stack module missing, schema not migrated, …).

    Failures are recorded via ``_safe`` so a missing stack schema surfaces
    in the degraded-mode block rather than silently routing every project
    through the legacy stack/design sections.
    """
    if not project_slug:
        return False

    def _check():
        from okuro.db import get_db
        row = get_db().fetchone(
            "SELECT brand_id FROM stack_project_brand WHERE project_slug = ?",
            (project_slug,),
        )
        return bool(row and row.get("brand_id"))

    return bool(_safe("sections._project_has_brand", _check, default=False))


def build_brand(project_slug: str = None) -> tuple[str, str, int]:
    """Build the unified brand section for the resolved project.

    M5 default: emits a pointer block (brand id + slot count + fetcher
    hint) instead of the full identity + design + stack + principles
    composition. Subagent calls ``stack_brand_get(brand_id)`` to load
    the full brand body when needed (e.g. a design-shaped subtask).
    Cuts ~3.5 KB on a brand-bound project.

    Legacy mode (OKURO_LEGACY_BOOTSTRAP=1) restores the full
    composition with design palette + typography + frontend + backend
    + principles expanded inline.
    """
    if not project_slug:
        return ("brand", "", 0)

    def _resolve_brand():
        from okuro.stack import active_brand_for
        return active_brand_for(project_slug)

    resolved = _safe("sections.build_brand.active_brand", _resolve_brand)
    if not resolved:
        return ("brand", "", 0)

    if not _m5_legacy_bootstrap():
        brand_id = resolved.get("id", "")
        name = resolved.get("name") or brand_id or "?"
        slots = resolved.get("slots", {}) or {}
        slot_summary = ", ".join(
            k for k, v in slots.items()
            if v and not (isinstance(v, dict) and v.get("missing"))
        ) or "(no resolved slots)"
        content = (
            f"## Brand: {name}\n"
            f"Brand `{brand_id}` is bound to project `{project_slug}`. "
            f"Slots resolved: {slot_summary}. Call "
            f"`stack_brand_get('{brand_id}')` to load the full design "
            f"profile + stack composition + engineering principles + the "
            f"brand's VOICE (tone, casing, banned phrases). "
            f"Skip for tasks that do not touch UI/copy/delivery."
        )
        return ("brand", content, estimate_tokens(content))

    lines = [f"## Brand: {resolved.get('name', resolved['id'])}"]
    desc = resolved.get("description") or ""
    if desc:
        lines.append(f"*{desc}*")
    lines.append(f"Brand: `{resolved['id']}` · status: {resolved.get('status', 'active')}")
    lines.append("")

    slots = resolved.get("slots", {}) or {}

    # --- Design slot ---
    design = slots.get("design")
    if design and not isinstance(design, list):
        if design.get("missing"):
            lines.append(f"### Design — *(missing: `{design.get('ref_id')}`)*")
        else:
            data = design.get("data") or {}
            lines.append(f"### Design: {data.get('name', design.get('ref_id'))}")
            visual = data.get("visual") or {}
            palette = visual.get("palette") or {}
            accent = palette.get("accent")
            if isinstance(accent, dict):
                accent = accent.get("default")
            if accent:
                lines.append(f"- **Accent**: `{accent}`")
            typo = visual.get("typography") or {}
            if typo.get("primary"):
                lines.append(f"- **Typeface**: {typo['primary']}")
            for c in (palette.get("constraints") or [])[:3]:
                lines.append(f"- ! {c}")
            lines.append(f"- Profile id: `{design.get('ref_id')}`")
        lines.append("")

    # --- Frontend + backend stack slots ---
    for slot_name, title in (("fe_stack", "Frontend"), ("be_stack", "Backend")):
        slot = slots.get(slot_name)
        if not slot or isinstance(slot, list):
            continue
        if slot.get("missing"):
            lines.append(f"### {title} Stack — *(missing: `{slot.get('ref_id')}`)*")
            lines.append("")
            continue
        data = slot.get("data") or {}
        label = data.get("label") or data.get("name") or slot.get("ref_id")
        lines.append(f"### {title} Stack: {label}")
        prof_desc = data.get("description") or ""
        if prof_desc:
            lines.append(f"*{prof_desc}*")
        lines.append(f"Profile: `{slot.get('ref_id')}`")

        by_cat = data.get("by_category") or {}
        rows: list[tuple[str, str, str, str]] = []
        for _cat, entries in by_cat.items():
            for e in entries:
                rows.append((
                    e.get("layer_name", ""),
                    f"**{e.get('name', '')}** (`{e.get('id', '')}`)",
                    e.get("version") or "—",
                    e.get("status", ""),
                ))
        if rows:
            lines.append("| Layer | Choice | Version | Status |")
            lines.append("|-------|--------|---------|--------|")
            for r in rows:
                lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |")
        lines.append("")

    # --- Voice slot ---
    #
    # THE BRAND BLOCK RENDERED NO VOICE AT ALL, and that was a silent hole
    # rather than an omission. `_voice_lines` below does render it — but only
    # from `build_design_profile`, which returns EMPTY for a brand-bound
    # project. So a project WITH a brand got the word "voice" in a slot list
    # and none of its content, and okuro's own packet was the proof: measured
    # 2026-09-05, zero of tone / casing / banned phrases reached it.
    #
    # Rendered from the rules table's own kinds, which are open-ended by
    # design, so a voice that grows a sixth kind reaches agents without an
    # edit here.
    voice = slots.get("voice")
    if voice and not isinstance(voice, list):
        if voice.get("missing"):
            lines.append(f"### Voice — *(missing: `{voice.get('ref_id')}`)*")
        else:
            data = voice.get("data") or {}
            lines.append(f"### Voice: {data.get('name', voice.get('ref_id'))}")
            for field, label in (
                ("tone", "Tone"),
                ("sentences", "Sentences"),
                ("casing", "Casing"),
                ("locale", "Locale"),
            ):
                if data.get(field):
                    lines.append(f"- **{label}**: {data[field]}")
            for kind, rows in (data.get("rules") or {}).items():
                if not rows:
                    continue
                # A pair kind reads "say X not Y"; a list kind is the key alone.
                rendered = [
                    f"{r['key']} → {r['value']}" if r.get("value") else str(r.get("key"))
                    for r in rows
                    if r.get("key")
                ]
                if rendered:
                    lines.append(
                        f"- **{kind.replace('_', ' ')}**: " + " · ".join(rendered)
                    )
        lines.append("")

    # --- Principle set slot ---
    principles = slots.get("principles")
    if principles and not isinstance(principles, list):
        if principles.get("missing"):
            lines.append(f"### Principles — *(missing: `{principles.get('ref_id')}`)*")
        else:
            data = principles.get("data") or {}
            name = data.get("name", principles.get("ref_id"))
            lines.append(f"### Principles: {name}")
            pdesc = data.get("description") or ""
            if pdesc:
                lines.append(f"*{pdesc}*")
            lines.append(f"Set: `{principles.get('ref_id')}`")
            members = data.get("principles") or []
            for m in members:
                pid = m.get("id", "?")
                title = m.get("title", "")
                descr = m.get("description", "")
                if descr:
                    lines.append(f"- **{pid} {title}** — {descr}")
                else:
                    lines.append(f"- **{pid} {title}**")
        lines.append("")

    content = "\n".join(lines).rstrip()
    return ("brand", content, estimate_tokens(content))


def build_behavioral_section() -> str:
    """Build the behavioral contract from live profile data.

    Tells agents HOW to behave — communication style, cognitive accommodations.
    Used by provider adapters (claude, codex, antigravity).
    """
    try:
        from okuro.yu.profile import get_profile_raw

        profile = get_profile_raw()
        if not profile:
            return _BEHAVIORAL_FALLBACK

        from okuro.sense.rules import load_section, render_line

        lines = []

        # Cognitive style — neurotype + implications
        cog = profile.get("cognitive_style", {})
        neurotype = cog.get("neurotype", [])
        if neurotype:
            nt = neurotype if isinstance(neurotype, list) else [neurotype]
            lines.append(f"**Neurotype:** {', '.join(nt)}")
        implications = load_section("implications", profile)
        if implications:
            lines.append("**You MUST accommodate these:**")
            for imp in implications:
                lines.append(f"- {render_line(imp)}")

        # Communication patterns
        patterns = load_section("patterns", profile)
        if patterns:
            lines.append("")
            lines.append("**Communication rules:**")
            for p in patterns:
                lines.append(f"- {render_line(p)}")

        # Communication — format preferences, length, directness
        comm = profile.get("communication", {}) or {}
        fmt_bits: list[str] = []
        if comm.get("response_length"):
            fmt_bits.append(f"**Response length:** {comm['response_length']}")

        preferred = load_section("preferred", profile)
        # Tolerate the flat-array shape written by the legacy questionnaire
        # (communication.format_preferences: [...]): if the dotted .preferred
        # path miss yielded nothing, fall back to the raw list.
        if not preferred:
            raw_fp = comm.get("format_preferences")
            if isinstance(raw_fp, list) and raw_fp:
                from okuro.sense.rules import parse_triple as _pt
                preferred = [_pt(x) for x in raw_fp]
        if preferred:
            pref_labels = [p["rule"] for p in preferred if p.get("rule")]
            if pref_labels:
                fmt_bits.append(f"**Prefer formats:** {', '.join(pref_labels)}")

        avoid = load_section("avoid", profile)
        if avoid:
            avoid_labels = [a["rule"] for a in avoid if a.get("rule")]
            if avoid_labels:
                fmt_bits.append(f"**Avoid formats:** {', '.join(avoid_labels)}")

        directness = comm.get("directness") or comm.get("tone")
        if directness:
            fmt_bits.append(f"**Tone:** {directness}")

        if fmt_bits:
            lines.append("")
            lines.extend(fmt_bits)

        # Pet peeves — what the user finds painful (distinct from hates)
        peeves = load_section("pet_peeves", profile)
        if peeves:
            peeve_labels = [p["rule"] for p in peeves if p.get("rule")]
            if peeve_labels:
                lines.append("")
                lines.append(f"**Pet peeves:** {', '.join(peeve_labels)}")

        # Work style
        ws = profile.get("work_style", {})
        hates = ws.get("hates", [])
        if hates:
            lines.append("")
            # hates renders as an inline list; entries can be strings or
            # triples — use only the rule text, not the rationale.
            from okuro.sense.rules import parse_triple as _pt
            hate_labels = [_pt(h)["rule"] for h in hates]
            lines.append(f"**Never do these — user hates:** {', '.join(hate_labels)}")

        # audit(R7 / #8): work_style.loves was listed in rules.SECTION_PATHS
        # but never rendered into the operative behavioral block — the agent
        # saw `hates` but not its positive mirror. Render the loves entries
        # the same way (rule text only), so agents know what to lean into,
        # not just what to avoid. Tolerates missing/empty fields silently.
        loves = ws.get("loves", []) if isinstance(ws, dict) else []
        if loves:
            from okuro.sense.rules import parse_triple as _pt
            love_labels = [_pt(love_item)["rule"] for love_item in loves
                           if _pt(love_item).get("rule")]
            if love_labels:
                lines.append("")
                lines.append(
                    "**You should LEAN INTO these — user loves:** "
                    f"{', '.join(love_labels)}"
                )

        # Expertise — surface strong/working areas so agents calibrate
        # explanations (don't over-explain Python to a Python expert; do
        # over-explain React to someone learning it). Entries are mixed-shape:
        # either a bare string or a {rule, rationale, evidence} triple.
        expertise = profile.get("expertise") or {}
        if isinstance(expertise, dict):
            from okuro.sense.rules import parse_triple as _pt

            def _expertise_labels(items) -> list:
                if not isinstance(items, list):
                    return []
                labels = []
                for item in items:
                    triple = _pt(item)
                    rule = triple.get("rule")
                    if rule:
                        labels.append(rule)
                return labels

            strong_labels = _expertise_labels(expertise.get("strong"))
            working_labels = _expertise_labels(expertise.get("working"))
            if strong_labels or working_labels:
                lines.append("")
                if strong_labels:
                    lines.append(f"**Expertise — Strong:** {', '.join(strong_labels)}")
                if working_labels:
                    lines.append(f"**Expertise — Working on:** {', '.join(working_labels)}")

        # audit(R7 / #8): cognitive_style.strengths was listed in
        # rules.SECTION_PATHS but never rendered into the operative
        # behavioral block. Surface it so agents know what cognitive
        # leverage the user brings (mirror of expertise — entries are
        # also mixed-shape: bare string OR {rule, rationale, evidence}).
        cog_for_strengths = profile.get("cognitive_style") or {}
        if isinstance(cog_for_strengths, dict):
            from okuro.sense.rules import parse_triple as _pt

            def _strength_labels(items) -> list:
                if not isinstance(items, list):
                    return []
                out = []
                for item in items:
                    triple = _pt(item)
                    rule = triple.get("rule")
                    if rule:
                        out.append(rule)
                return out

            strengths_labels = _strength_labels(cog_for_strengths.get("strengths"))
            if strengths_labels:
                lines.append("")
                lines.append(
                    f"**Strengths the user brings:** {', '.join(strengths_labels)}"
                )

        # Decision style — tolerate both nested dict (curated) and legacy
        # scalar (old questionnaire that stomped the dict).
        ds = profile.get("decision_style", {})
        if isinstance(ds, dict):
            if ds.get("speed_vs_quality"):
                lines.append(f"**Approach:** {ds['speed_vs_quality']}")
            if ds.get("framing"):
                lines.append(f"**Decision framing:** {ds['framing']}")
        elif isinstance(ds, str) and ds:
            lines.append(f"**Decision framing:** {ds}")

        # audit(NF-1): Error handling + Cognitive abstraction — C8 restored
        # questionnaire persistence for these keys but the renderer never
        # surfaced them, leaving 2 of 8 questionnaire-derived values invisible
        # to the agent. Rendering both closes the red-line promise end-to-end.
        error_handling = profile.get("error_handling")
        if isinstance(error_handling, str) and error_handling.strip():
            lines.append(f"**Error handling:** {error_handling}")

        cs = profile.get("cognitive_style", {})
        if isinstance(cs, dict):
            abstraction = cs.get("abstraction")
            if isinstance(abstraction, str) and abstraction.strip():
                lines.append(f"**Cognitive abstraction:** {abstraction}")

        # Same class of gap as error_handling above: persisted in the profile,
        # consumed by mcp_middleware, and rendered by NO bootstrap section — so
        # it never reached an agent's context. It is arguably the highest-value
        # rule the user has written, because it fires exactly when an agent is
        # already failing and the instinct is to cut corners to recover.
        comm_ap = (profile.get("communication") or {})
        ap = comm_ap.get("anger_protocol") if isinstance(comm_ap, dict) else None
        if isinstance(ap, str):
            # update_profile via the MCP boundary stringifies a nested-dict
            # value, so a settings write can land anger_protocol as a JSON
            # string. Tolerate it — this rule must never blank out because a
            # write arrived stringified.
            import json as _json
            try:
                ap = _json.loads(ap)
            except (ValueError, TypeError):
                ap = None
        if isinstance(ap, dict) and (ap.get("response") or ap.get("do") or ap.get("never")):
            trigger = ap.get("trigger") or "user frustration"

            def _joln(v):
                return " · ".join(v) if isinstance(v, list) else str(v)

            # Imperative frame. If the profile carries structured do/never/
            # self_check fields, render the full DO/NEVER/self-check shape
            # (highest adherence). Otherwise frame the single free-text
            # `response` as a DO line — still imperative, still 100% dynamic,
            # never breaks before the settings UI captures the structured
            # fields. All content comes from settings; no rule text is hardcoded.
            lines.append(f"**When the user is frustrated** — trigger: {trigger}.")
            do, never, check = ap.get("do"), ap.get("never"), ap.get("self_check")
            if do or never or check:
                if do:
                    lines.append(f"- DO: {_joln(do)}")
                if never:
                    lines.append(f"- NEVER: {_joln(never)}")
                if check:
                    lines.append(f"- Self-check before replying: {check}")
            else:
                lines.append(f"- DO: {ap['response']}")

        ws = profile.get("work_style") or {}
        rabbit = ws.get("rabbit_holes") if isinstance(ws, dict) else None
        if isinstance(rabbit, str) and rabbit.strip():
            lines.append(f"**Depth vs momentum:** {rabbit}")

        # Behavioral overrides — per-user strategy knobs derived from the
        # cognitive-traits onboarding step (context_format, suggestion
        # aggression, stale-day thresholds, etc.). Reminders/strategy.py
        # consumes the full dict; here we surface the agent-relevant
        # subset so agents adapt on-screen behavior without having to
        # query the reminder subsystem.
        overrides = profile.get("behavioral_overrides") or {}
        if isinstance(overrides, dict) and overrides:
            _AGENT_RELEVANT = {
                "context_format": "Context format",
                "suggestion_aggression": "Suggestion aggression",
                "protect_routines": "Protect routines",
                "re_entry_reminders": "Re-entry reminders",
                "context_switch_alert_min": "Context-switch alert (min)",
                "transition_buffer_min": "Transition buffer (min)",
            }
            override_bits: list[str] = []
            for key, label in _AGENT_RELEVANT.items():
                if key in overrides:
                    val = overrides[key]
                    if isinstance(val, bool):
                        val = "yes" if val else "no"
                    override_bits.append(f"- **{label}:** {val}")
            if override_bits:
                lines.append("")
                lines.append("**Behavioral tunings (from cognitive traits):**")
                lines.extend(override_bits)

        # audit(R7 / #8): boundaries reach the agent only via the raw
        # `build_profile` markdown dump today — they are NOT part of the
        # operative behavioral block, which means they don't make it into
        # provider pre-bootstrap context (claude.md / codex / agents.md all
        # read this section directly). Render them here as a top-level
        # `## Boundaries` block so the operative contract is complete.
        boundaries = profile.get("boundaries") or {}
        if isinstance(boundaries, dict) and boundaries:
            from okuro.sense.rules import parse_triple as _pt

            def _boundary_labels(items) -> list:
                if not isinstance(items, list):
                    return []
                out = []
                for item in items:
                    triple = _pt(item)
                    rule = triple.get("rule")
                    if rule:
                        out.append(rule)
                return out

            ok_auto = _boundary_labels(boundaries.get("ok_autonomous"))
            never_ask = _boundary_labels(boundaries.get("never_without_asking"))

            if ok_auto or never_ask:
                lines.append("")
                lines.append("## Boundaries")
                # Imperative native pair — the values are dynamic (from
                # profile.boundaries); only the ALWAYS/NEVER framing is fixed.
                if ok_auto:
                    lines.append("**ALWAYS OK without asking:**")
                    for b in ok_auto:
                        lines.append(f"- {b}")
                if never_ask:
                    if ok_auto:
                        lines.append("")
                    lines.append("**NEVER without asking:**")
                    for b in never_ask:
                        lines.append(f"- {b}")

            # Interruption policy — a governing rule that had NO dedicated
            # builder (rendered only in the generic profile dump). Surface it
            # here, imperatively, from settings (profile.boundaries.
            # interruption_policy). Dynamic free-text; framed, not hardcoded.
            interruption = boundaries.get("interruption_policy")
            if isinstance(interruption, str) and interruption.strip():
                lines.append("")
                lines.append(f"**Interruption policy** — {interruption.strip()}")

        return "\n".join(lines) if lines else _BEHAVIORAL_FALLBACK

    except Exception as exc:
        log.warning("bootstrap section %r failed: %s",
                    "sections.build_behavioral_section", exc)
        from ._safe import FAILED_SECTIONS
        FAILED_SECTIONS.append(
            ("sections.build_behavioral_section", str(exc))
        )
        return _BEHAVIORAL_FALLBACK


# ---------------------------------------------------------------------------
# audit(R4 / #13): honest provider-compliance renderer
# ---------------------------------------------------------------------------
#
# Replaces the old `mcp_middleware._build_compliance_nudges` invocation
# inside `build_session()`. Three changes versus the legacy renderer:
#
#   1. **Insufficient-data state.** When fewer than 3 SCORABLE (non-ghost)
#      sessions are recorded for the provider, surface "no data yet"
#      instead of computing a bucket label. The denominator already
#      excludes ghosts in `aggregate_provider_compliance`'s SQL; here we
#      simply require enough signal before judging.
#   2. **Honest bucketing.** Score >= 0.85 → "good", 0.55..0.85 → "ok",
#      < 0.55 → "needs attention" (NOT "poor" — less moralistic).
#   3. **Show the math.** Append "(N sessions over D days)" plus a
#      one-line "rough heuristic" disclaimer, plus the per-bucket
#      breakdown (e.g. "bootstrap 4/4 ✓, cortex 0/4, ...") so the agent
#      knows precisely which buckets to fix instead of trusting an
#      opaque score.
#
# The math (`_calculate_compliance_v2` weights, `aggregate_provider_compliance`
# SQL) is NOT touched — only the return shape (now includes a per-session
# `breakdown`) and the renderer.

def _render_provider_compliance(provider: str) -> str:
    """Render the bootstrap "Provider compliance" line(s).

    Returns "" when telemetry lookup fails (fail-soft — bootstrap should
    never crash on telemetry hiccups).
    """
    try:
        from okuro.sense.telemetry import get_provider_compliance
        pc = get_provider_compliance(provider)
    except Exception:
        return ""

    if not pc:
        return ""

    scored = int(pc.get("scored_sessions") or 0)
    window_days = int(pc.get("window_days") or 30)

    # audit #13: ghost-filter + minimum sample size before bucketing.
    # `scored_sessions` already excludes ghosts (the SQL in
    # aggregate_provider_compliance subtracts ghost rows). If we have fewer
    # than 3 real sessions, refuse to label compliance "good" or "poor" —
    # the heuristic isn't meaningful yet.
    if scored < 3:
        return (
            "- **Provider compliance:** no data yet "
            f"({scored} session{'s' if scored != 1 else ''} over {window_days} days; "
            "need at least 3 to score)\n"
        )

    avg = float(pc.get("avg_normalized") or 0.0)
    if avg >= 0.85:
        label = "good"
    elif avg >= 0.55:
        label = "ok"
    else:
        label = "needs attention"

    # Per-bucket counts derived from rates × scored denominator.
    # Buckets mirror `_calculate_compliance_v2`'s breakdown keys.
    rate_keys = (
        ("bootstrap", "bootstrap_rate"),
        ("cortex",    "cortex_rate"),
        ("memory",    "memory_rate"),
        ("progress",  "progress_rate"),
        ("report",    "report_rate"),
    )
    breakdown_bits: list[str] = []
    for bucket, rate_key in rate_keys:
        rate = float(pc.get(rate_key) or 0.0)
        count = int(round((rate / 100.0) * scored))
        check = " ✓" if count == scored and scored > 0 else ""
        breakdown_bits.append(f"{bucket} {count}/{scored}{check}")

    out_lines = [
        f"- **Provider compliance:** {label} ({avg:.2f}) "
        f"({scored} sessions over {window_days} days)",
        "  - *rough heuristic over recent sessions; calibrate, don't obey*",
        "  - " + ", ".join(breakdown_bits),
    ]
    return "\n".join(out_lines) + "\n"


def build_session() -> tuple[str, str, int]:
    """Session context section — session ID, previous score, provider compliance.

    The mandatory-3 reminder (write_memory / log_progress / session_report)
    USED to live here but was subject to budget compression. It now lives in
    `mcp_middleware._nudge_session_end` as an unbudgeted per-tool-call nudge
    that fires on EVERY call after turn 10 until `session_report()` runs.
    See A4 (directive vs reference split) and A8 (enforcement at the
    boundary) — the directive channel is the middleware, not the bootstrap
    packet.
    """
    import os

    lines = ["## Session"]

    def _session_id():
        from okuro.sense.session_state import get_session_state
        state = get_session_state()
        return state.get("session_id") or "unknown"

    sid = _safe("sections.build_session.session_id", _session_id, default="unknown")
    lines.append(f"- **Session ID:** {sid}")

    def _prev_score():
        from okuro.sense.telemetry import get_previous_session_score
        prev = get_previous_session_score()
        if prev and prev.get("score") is not None:
            return f"- **Previous session score:** {prev['score']}/5 ({prev.get('provider', '?')})"
        return None

    prev_line = _safe("sections.build_session.previous_score", _prev_score)
    if prev_line:
        lines.append(prev_line)

    # THE COMPLIANCE SCOREBOARD IS NOT DELIVERED. Removed 2026-09-09.
    #
    # It rendered "Provider compliance: ok (0.64) (1082 sessions over 30 days)",
    # a per-bucket breakdown, and the line "rough heuristic over recent
    # sessions; calibrate, don't obey" — a literal instruction not to obey,
    # inside the contract, and the last authority-degrading string left in the
    # packet after the 2026-09-08 surface audit.
    #
    # Every part of it is a MAINTAINER instrument. An aggregate over 1082 past
    # sessions says nothing this session can act on, and a rate next to a rule
    # invites the agent to weigh the rule: 0.64 reads as "mostly fine". The
    # protocol steps it scored are already stated as unconditional imperatives
    # in Operating Rules ("call all three, every session"), so nothing
    # actionable is lost.
    #
    # NOTHING IS LOST FOR THE OWNER EITHER: `compliance_scorecard(provider, days)`
    # renders the same figures with denominators, on demand, on the surface
    # where a number is a measurement rather than a licence.
    #
    # `_render_provider_compliance` is kept — the scorecard path and the
    # okuro-web surface still call it. It simply no longer reaches an agent.

    content = "\n".join(lines)
    return ("session", content, estimate_tokens(content))


# ---------------------------------------------------------------------------
# Standalone TOOL-PROTOCOL.md — generated to disk by generate_tool_protocol()
# ---------------------------------------------------------------------------

# EVERY LINE IN THIS FILE BINDS EVERY AGENT THAT READS IT.
#
# Removed from the emitted head: the AGENT_HEADER index block (generation
# metadata — a table of contents okuro's own tooling writes, not something an
# agent acts on) and "# Auto-generated by okuro. Do not edit manually."
# (an install instruction to the owner, at the top of a document three separate
# call sites tell subagents to treat as their protocol).
_TOOL_PROTOCOL_HEAD = """\
# Tool Protocol — Okuro

Every rule in this file binds you, whatever spawned you.

**Bootstrap is TWO calls.** `bootstrap` returns the CORE half. When your
task_hint resolves to a project, `bootstrap_project(slug=...)` returns that
project's half — memory, todos, progress, role assignment — and every other
okuro tool is REFUSED until it lands. No project resolved: one call, no block.

"""


# Everything after the rendered contract. The routing tables that used to sit
# here are gone on purpose — see build_tool_protocol_doc.
_TOOL_PROTOCOL_TAIL = """\

**Orientation.** `cortex_scope()` first when you do not know what is indexed;
`cortex_navigate(path, direction)` to walk related files; `sysinfo_port_status()`
for free ports. An empty scoped search result is NOT proof code is absent —
check the scope, then use `cortex_search_code`, which walks ALL roots and needs
no scope.

### Resources

| Situation | Do this | Not this |
|-----------|---------|----------|
| Before GPU work | acquire a lease via the GPU broker | Use GPU without lease |
| Need to call another LLM | `bridge_invoke(prompt, capability?)` | Direct subprocess |

### Knowledge Capture — the document buckets

The routing table above covers facts (`write_memory` topics). These cover
everything longer than a fact — and none of it belongs in a `.md` file on disk:

| Situation | Do this |
|-----------|---------|
| **Produce a document-length deliverable** — investigation, dossier, audit, research report, system map | **`artifact_write(kind=report / evidence / plan, title, body)`** |
| Hand structured context to the next agent | `write_role_handover(...)` — refs only, never inline source |
| Hit a meaningful milestone | `log_progress(project, status, summary)` |
| Something needs doing, with an owner | `todo_add(...)` |
| The USER asked you for a note | `note_create(...)` |

**Rule:** If you discovered something non-obvious, persist it with `write_memory()` so the next agent doesn't repeat the work.

**Rule — deliverables are artifacts, not notes.** Any long-form output YOU
produced belongs in `artifact_write`. `note_create` is the USER's personal
writing surface — write a note ONLY when the user explicitly asks for one.

**Rule — memory rows are facts, not documents.** If it is longer than a short
paragraph it is an artifact, not a memory.

### Shell Usage
Use the shell ONLY for runtime state no MCP tool covers — a tool-specific
`sqlite3` database, a project's own status CLI. Everything an MCP tool covers
goes through that tool: codebase search → cortex, GPU/docker/port state →
sysinfo, secrets → keyring.

---

## Starting a task

| Situation | Do this |
|-----------|---------|
| Starting a task | `brain_advise(task_hint)` for LLM-powered briefing |
| Discover project context | `project_status(slug)` — one read: phase plan, progress, inventory, staleness |
| User states a rule (NEVER/ALWAYS) | `write_memory(topic="convention", content, confidence=0.9)` |
| User shares an idea | `capture_thought(content, category="idea")` |
| Hit a meaningful milestone | `log_progress(project, status, summary)` |
| Task meets adoption triggers (2+ files, or domain judgment, or expertise verb, or explicit role ask) | `roles_match(task)` then `roles_get(id)` — adopt the returned role as binding operating context |

### Workflow: Starting a New Task
1. `brain_advise(task_hint)` — get focused briefing from local LLM
2. `roles_match(task)` — if the task meets adoption triggers (≥2 files or 20+ min, domain judgment, expertise verb, or explicit role ask), adopt via `roles_get(id)` before producing output. Skip for one-line answers, single mechanical edits, lookups.
3. `cortex_route(concept)` — orient in codebase
4. `cortex_read_header` → `cortex_read_section` — understand relevant files
5. Work — edit, test, verify
6. `log_progress(project, status, summary)` — record what you did
7. `session_report(feedback)` — rate tools you used before ending

### Workflow: Ending a Session
Call `session_report()` with feedback on tools you used:
```
session_report(feedback=[
  {tool_name: "cortex_search", used: true, useful: true},
  {tool_name: "sysinfo_gpu_status", used: false, bypass_reason: "not needed for this task"}
])
```

### Spawning Subagents
When spawning a subagent, include in its prompt:
```
read ~/.okuro/TOOL-PROTOCOL.md
```

**Artifact routing — CRITICAL:**
- The deliverable is **`artifact_write(kind="report", project=…)`**, never a message return and never a `.md` file.
- Files on disk ONLY for what the brain cannot hold — images, generated code, tests. Convention: `{project}/docs/` for assets, `{project}/tests/` for tests. The artifact references them by relative path.
- Don't use worktree isolation for tasks that produce user-facing files — outputs get stranded on temp branches
- **Never instruct a subagent to write its report to disk.** It will obey you over its own bootstrap, and you will have created two homes for one document. Name `artifact_write` in the brief instead.
"""


def build_tool_protocol_doc() -> str:
    """Compose TOOL-PROTOCOL.md — the file every subagent is told to read.

    The routing tables used to be baked into this module as a static string,
    which is why this surface still shipped ``Fallback: If cortex index is
    unavailable, Grep and Glob work normally`` months after build_tool_protocol
    removed it from the packet as measured-harmful (rationale in that
    function's docstring), and why it never learned the consult-memory rule
    restored to the contract on 2026-07-28.

    It now renders the middle from the same `tool_routing` contract the packet
    uses, so the two cannot disagree again. Head and tail carry only what is
    NOT routing: the doc header, the document-bucket table, and the
    session-owner workflows.
    """
    from .rules import render_rule
    return _TOOL_PROTOCOL_HEAD + render_rule("tool_routing") + _TOOL_PROTOCOL_TAIL


def generate_tool_protocol(target_dir: str | None = None) -> str:
    """Write TOOL-PROTOCOL.md to ``target_dir`` (default ``~/.okuro``).

    ``target_dir`` exists so a test — or an agent verifying a change from a
    worktree — can render the real document without writing over the live one
    the running session reads.
    """
    import os
    okuro_dir = target_dir or str(okuro_home())
    os.makedirs(okuro_dir, exist_ok=True)
    path = os.path.join(okuro_dir, "TOOL-PROTOCOL.md")
    with open(path, "w") as f:
        f.write(build_tool_protocol_doc())
    return path


_BEHAVIORAL_FALLBACK = """\
**Communication rules:**
- lead with the answer, then explain if needed
- never restate what the user just said
- always give options with pros/cons AND a recommendation
- use direct language, not hedging
- be consistent in terminology"""
