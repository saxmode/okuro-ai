# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Translate text from the user's cognitive shape to a communication partner's profile via bridge_invoke.
# index:
#   imports
#   def _new_id
#   def _compose_prompt
#   def _log_translation
#   def person_translate
#   def translation_stats
#   def translation_intent
# AGENT_HEADER_END -->
"""Translate text from the user's cognitive shape to a person's profile.

`person_translate(person_id, source, context?)` is the load-bearing function
behind /people edges — click an edge, paste what you were going to send,
get it reformatted for the recipient's cognitive + communication profile.

Flow:
    1. Load person profile (communication + cognitive + role + relation).
    2. Load user profile (cognitive_style + communication).
    3. Compose a translate prompt that preserves facts, changes shape.
    4. bridge_invoke(capability="translate"). Unknown capability falls back to
       the default-routed provider — see bridge/providers.py:resolve_provider.
    5. Log the outcome to translation_log (powers edge stats on the graph).

We never raise. Failures return a dict with success=False and log an error
row so the edge stats still reflect attempts.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

log = logging.getLogger("okuro.peer.translate")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _compose_prompt(user_profile: dict, recipient_cognitive: dict, source: str, context: str | None) -> str:
    """Build the translate prompt from the user's profile + recipient's
    ANONYMIZED cognitive profile.

    The recipient is read through ``cognitive_profile_for_llm`` upstream
    so this function never sees the recipient's name, organization, email,
    or any free-text relation — only the cognitive shape (sliders, format
    prefs, jargon tolerance, decision style, relation archetype, topic
    interests). PII firewall is enforced by construction: nothing here
    can leak what isn't passed in.

    The sender's identity stays in the prompt — it's the user's own
    profile, not a third party's.
    """
    from okuro.peer.cognitive_profile import format_sliders_for_prompt

    from okuro.peer.sender import sender_block

    parts: list[str] = [
        "You are a TRANSLATOR from sender to recipient.",
        "Preserve every fact and every ask. Change ONLY format, tone, length, and vocabulary to fit the recipient.",
        "The recipient is anonymous to you — you only see their cognitive shape.",
        "",
    ]

    # Sender block — the user's own profile (their data, no firewall needed),
    # rendered on the SAME axis registry as the recipient so both halves of
    # the translation share coordinates instead of one side having adjectives.
    #
    # THE NEUROTYPE LABEL IS GONE, DELIBERATELY. This used to emit
    # "Sender neurotype: ...", which makes the model write a persona rather
    # than apply a constraint and measurably degrades rewrite fidelity
    # (Q1 :95). peer.sender still READS it — "monotropic attention" is why
    # pace derives to 1 — and the label stops there.
    #
    # It also fixes a live defect: communication.format_preferences is a dict
    # of {preferred, avoid}, and the previous `', '.join(prefs or [])`
    # iterated its KEYS, so every prompt carried the literal string
    # "avoid, preferred" as the sender's format preference.
    block = sender_block(user_profile)
    if block:
        parts.append(block)
        parts.append("")

    # Recipient block — fed only the anonymized cognitive projection.
    recipient_lines = ["## RECIPIENT (anonymous cognitive profile)"]
    archetype = recipient_cognitive.get("relation_archetype")
    if archetype and archetype != "unknown":
        recipient_lines.append(f"- relation archetype: {archetype}")
    if recipient_cognitive.get("formality_baseline"):
        recipient_lines.append(f"- formality baseline: {recipient_cognitive['formality_baseline']}")
    if recipient_cognitive.get("jargon_tolerance"):
        recipient_lines.append(f"- jargon: {recipient_cognitive['jargon_tolerance']}")
    if recipient_cognitive.get("decision_style"):
        recipient_lines.append(f"- decision style: {recipient_cognitive['decision_style']}")
    if recipient_cognitive.get("attention_span"):
        recipient_lines.append(f"- attention span: {recipient_cognitive['attention_span']}")
    if recipient_cognitive.get("learning_style"):
        recipient_lines.append(f"- learning style: {recipient_cognitive['learning_style']}")
    them_fmt = recipient_cognitive.get("format_preferences") or []
    if them_fmt:
        recipient_lines.append(f"- format preferences: {', '.join(map(str, them_fmt))}")
    avoid = recipient_cognitive.get("avoid_structural") or []
    if avoid:
        recipient_lines.append(f"- avoid structurally: {', '.join(map(str, avoid))}")
    slider_text = format_sliders_for_prompt(recipient_cognitive)
    if slider_text:
        recipient_lines.append(f"- sliders: {slider_text}")
        if "(prior)" in slider_text:
            recipient_lines.append(
                "- axes marked (prior) are assumptions from a role default, "
                "not this person's stated preference — weight them lightly."
            )
    topics = recipient_cognitive.get("topic_interests") or []
    if topics:
        topic_labels = [t.get("topic", "") for t in topics if isinstance(t, dict) and t.get("topic")]
        if topic_labels:
            recipient_lines.append(f"- topics they care about: {', '.join(topic_labels[:5])}")
    parts.extend(recipient_lines)
    parts.append("")

    if context:
        parts.append(f"Channel / situation: {context}")
        parts.append("")
    parts.append("---")
    parts.append("SOURCE TEXT (from SENDER, raw):")
    parts.append(source.strip())
    parts.append("---")
    parts.append("")
    parts.append(
        "Output ONLY the translated message as the recipient would best receive it. "
        "No preamble, no explanation, no code fences. Preserve facts exactly."
    )
    return "\n".join(parts)


def _log_translation(
    person_id: str,
    source: str,
    translated: str | None,
    context: str | None,
    provider: str | None,
    model: str | None,
    duration_ms: int,
    success: bool,
    error: str | None = None,
) -> str:
    """Insert a row into translation_log and return its id."""
    from okuro.db import get_db

    db = get_db()
    log_id = _new_id()
    db.execute(
        """INSERT INTO translation_log (
               id, person_id, source_text, translated_text, context,
               capability, provider, model, duration_ms, success, error
           ) VALUES (?, ?, ?, ?, ?, 'translate', ?, ?, ?, ?, ?)""",
        (
            log_id,
            person_id,
            source,
            translated,
            context,
            provider,
            model,
            duration_ms,
            1 if success else 0,
            error,
        ),
    )
    db.conn.commit()
    return log_id


def person_translate(
    person_id: str,
    source: str,
    context: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Translate `source` for `person_id`, log the attempt, return the result.

    Returns:
        {
            success: bool,
            translated: str | None,
            log_id: str,
            provider: str | None,
            model: str | None,
            duration_ms: int,
            error: str | None,
            person: {id, display_name},
        }
    """
    from okuro.db import get_db

    if not source or not source.strip():
        return {"success": False, "error": "source is empty", "person_id": person_id}

    db = get_db()
    row = db.fetchone(
        "SELECT id, display_name FROM persons WHERE id = ? AND active = 1", (person_id,)
    )
    if not row:
        return {"success": False, "error": f"Person not found: {person_id}", "person_id": person_id}

    # PII firewall — prompt is built from the anonymized cognitive
    # projection only. Display name stays in this scope (for the return
    # envelope + telemetry) but never reaches `_compose_prompt`.
    from okuro.peer.cognitive_profile import cognitive_profile_for_llm

    recipient_cognitive = cognitive_profile_for_llm(person_id)
    display_name = row["display_name"]

    try:
        from okuro.yu.profile import get_profile_raw

        user_profile = get_profile_raw() or {}
    except Exception as exc:  # pragma: no cover — profile read should rarely fail
        log.debug("profile read failed: %s", exc)
        user_profile = {}

    prompt = _compose_prompt(user_profile, recipient_cognitive, source, context)

    start = time.monotonic()
    try:
        from okuro.bridge.invoke import invoke

        result = invoke(prompt, capability="translate", provider=provider)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        log_id = _log_translation(
            person_id, source, None, context, None, None,
            duration_ms, False, f"bridge invoke raised: {exc}",
        )
        return {
            "success": False,
            "translated": None,
            "log_id": log_id,
            "provider": None,
            "model": None,
            "duration_ms": duration_ms,
            "error": str(exc),
            "person": {"id": person_id, "display_name": display_name},
        }

    duration_ms = int(result.get("duration", (time.monotonic() - start)) * 1000)
    provider_id = result.get("provider")
    model_name = result.get("model")
    success = bool(result.get("success"))
    translated = (result.get("output") or "").strip() if success else None
    error = None if success else (result.get("error") or "bridge returned no output")

    log_id = _log_translation(
        person_id, source, translated, context, provider_id, model_name,
        duration_ms, success, error,
    )

    # Informational equivalence. The prompt says "preserve every fact and
    # every ask" and nothing checked, so a dropped figure or deadline
    # shipped as a perfectly fluent message — the failure is invisible
    # exactly because the output reads well.
    #
    # It REPORTS, it does not reject: an atom may legitimately vanish with
    # a sentence the recipient's profile says to suppress. The caller
    # decides; the gate makes the loss visible instead of silent.
    equivalence = None
    if success and translated:
        from okuro.peer.equivalence import equivalence_clause, equivalence_report
        from okuro.peer.flags import people_strict_enabled, warn_lenient

        if people_strict_enabled():
            equivalence = equivalence_report(source, translated)
            clause = equivalence_clause(equivalence)
            if clause:
                log.warning("person_translate %s: %s", person_id, clause)
        else:
            warn_lenient(
                "peer.translate.equivalence",
                "rewrites are not checked for dropped facts",
            )

    return {
        "success": success,
        "translated": translated,
        "log_id": log_id,
        "provider": provider_id,
        "model": model_name,
        "duration_ms": duration_ms,
        "error": error,
        "equivalence": equivalence,
        "person": {"id": person_id, "display_name": display_name},
    }


# ── Edge intent ──────────────────────────────────────────────────────
#
# The graph edge label should tell the user WHAT the translation will focus
# on for this recipient — not a raw "2×" counter, and not a vague promise.
# Derived deterministically from the recipient's profile; no LLM per paint.
#
# PRIMARY SOURCE IS THE SLIDERS. Profiling moved to the fixed 8-axis vector
# (Mode B / SlidersPanel, persons.cognitive.sliders) plus the extended
# cognitive axes, and that is where real data now lives. The legacy
# free-text fields (response_length, formality, decision_style,
# attention_span) are still read as a fallback for anyone profiled the old
# way, but reading ONLY those was the bug: 13 of 15 real people had no such
# fields and every one of them rendered the same useless "I'll tune it to
# their style", including people carrying a complete slider vector.
#
# An axis at 3 is neutral and says nothing, so phrases are ranked by
# distance from centre — the axis a person is most extreme on is the one
# worth naming. Ties break on _AXIS_PRIORITY so the label is stable across
# renders rather than reshuffling on every paint.

# axis -> (phrase when low (1-2), phrase when high (4-5)).
_AXIS_PHRASES: dict[str, tuple[str, str]] = {
    "lead_with": ("context first", "answer first"),
    "format": ("flowing prose", "bullet points"),
    "information_depth": ("full detail", "high-level only"),
    "decision_framing": ("options laid out", "recommendation first"),
    "jargon": ("plain language", "expert vocabulary"),
    "risk_framing": ("upside first", "risks up front"),
    "time_horizon": ("tactical specifics", "strategic framing"),
    "pace": ("one deep thread", "scannable blocks"),
    "certainty": ("caveated claims", "definitive claims"),
    "need_for_cognition": ("conclusion only", "show the reasoning"),
    "density": ("one idea at a time", "dense and packed"),
    "construal": ("concrete steps", "the why, not the how"),
    "numeracy": ("gist over numbers", "numbers welcome"),
    "graph_literacy": ("no charts", "charts land well"),
    "rational": ("intuition-friendly", "analysis-backed"),
    "regulatory_focus": ("risk-guarded framing", "opportunity framing"),
    "experiential": ("data over anecdote", "story over data"),
}

# Which axis to name first when several are equally extreme. Ordered by how
# much each changes the SHAPE of a message: where it opens and how it is
# laid out matter more to a reader than tone-level nuance.
_AXIS_PRIORITY: tuple[str, ...] = (
    "lead_with",
    "format",
    "information_depth",
    "decision_framing",
    "jargon",
    "risk_framing",
    "time_horizon",
    "pace",
    "need_for_cognition",
    "density",
    "certainty",
    "construal",
    "numeracy",
    "graph_literacy",
    "rational",
    "regulatory_focus",
    "experiential",
)

_MAX_PHRASES = 3


def audience_focus_phrases(
    sliders: dict[str, Any] | None, limit: int = _MAX_PHRASES
) -> list[str]:
    """Public name for the slider→words mapping, for callers outside the
    edge label.

    target_groups embeds an audience partly as prose, and this vocabulary
    ("answer first", "expert vocabulary") is exactly the language a search
    query would use. Sharing it means a group's embedded description and a
    person's edge label describe the same shape in the same words, rather
    than two drifting phrasings of one idea.
    """
    return _slider_phrases(sliders, limit=limit)


def _slider_phrases(
    sliders: dict[str, Any] | None, limit: int = _MAX_PHRASES
) -> list[str]:
    """Focus phrases for the axes this person is most extreme on."""
    if not isinstance(sliders, dict):
        return []

    scored: list[tuple[int, int, str]] = []
    for axis, phrases in _AXIS_PHRASES.items():
        raw = sliders.get(axis)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            continue
        value = int(raw)
        if value < 1 or value > 5:
            continue
        distance = abs(value - 3)
        if distance == 0:
            continue  # neutral — naming it would say nothing
        try:
            rank = _AXIS_PRIORITY.index(axis)
        except ValueError:
            rank = len(_AXIS_PRIORITY)
        scored.append((-distance, rank, phrases[0] if value < 3 else phrases[1]))

    scored.sort()
    return [phrase for _, _, phrase in scored[:limit]]

_LENGTH_ORDER = {
    "short": 0,
    "concise": 0,
    "terse": 0,
    "moderate": 2,
    "standard": 2,
    "balanced": 2,
    "moderate-long": 3,
    "long": 4,
    "verbose": 4,
}

_FORMALITY_ORDER = {
    "informal": 0,
    "casual": 0,
    "warm": 1,
    "neutral": 1,
    "brand-aligned": 2,
    "semi-formal": 2,
    "professional": 2,
    "formal": 3,
    "formal-academic": 4,
}


def _length_rank(value: str | None) -> int | None:
    if not value:
        return None
    v = value.lower().strip()
    return _LENGTH_ORDER.get(v)


def _formality_rank(value: str | None) -> int | None:
    if not value:
        return None
    v = value.lower().strip()
    return _FORMALITY_ORDER.get(v)


def _format_tokens(prefs: list[Any] | None) -> list[str]:
    if not prefs:
        return []
    return [str(p).lower() for p in prefs]


def translation_intent(user_profile: dict, person: dict) -> str:
    """Return the translation FOCUS for this recipient, e.g.

        "answer first · bullet points · high-level only"

    Sliders first (the live source of truth), legacy free-text deltas as a
    fallback for anyone profiled before Mode B. Returns a short honest
    marker when the person carries no profile at all — the label must never
    claim a tailoring that no data supports.
    """
    them_comm = person.get("communication") or {}
    them_cog = person.get("cognitive") or {}

    phrases = _slider_phrases(them_cog.get("sliders"))
    if phrases:
        return ", ".join(phrases)

    # ── Legacy path: free-text deltas against the sender's own profile ──
    me_comm = user_profile.get("communication") or {}
    me_cog = user_profile.get("cognitive_style") or {}

    verbs: list[str] = []

    # Length delta — emit a single, strongest verb.
    me_len = _length_rank(me_comm.get("response_length"))
    them_len = _length_rank(them_comm.get("response_length"))
    if me_len is not None and them_len is not None:
        diff = me_len - them_len
        if diff >= 2:
            verbs.append("cut the wall of text")
        elif diff == 1:
            verbs.append("shorten")
        elif diff == -1:
            verbs.append("expand")
        elif diff <= -2:
            verbs.append("flesh it out with detail")

    # Short attention recipient — only add if we didn't already say "cut wall of text".
    them_attn = (them_cog.get("attention_span") or "").lower()
    if them_attn == "short" and not any("wall" in v for v in verbs):
        verbs.append("front-load the ask")

    # Formality delta.
    me_form = _formality_rank(me_comm.get("formality"))
    them_form = _formality_rank(them_comm.get("formality"))
    if me_form is not None and them_form is not None:
        if them_form - me_form >= 2:
            verbs.append("formalize hard")
        elif them_form > me_form:
            verbs.append("formalize")
        elif me_form - them_form >= 2:
            verbs.append("loosen up hard")
        elif me_form > them_form:
            verbs.append("loosen up")

    # Format preferences — pick one most distinctive hint.
    fmt_tokens = _format_tokens(them_comm.get("format_preferences"))
    if any("executive" in t or "dashboard" in t for t in fmt_tokens):
        if not any("front-load" in v for v in verbs):
            verbs.append("shape it as an exec brief")
    elif any("bullet" in t for t in fmt_tokens) and not any(
        "wall" in v or "shorten" in v for v in verbs
    ):
        verbs.append("structure as bullets")
    if any(
        "citation" in t or "reference" in t or "evidence" in t for t in fmt_tokens
    ):
        verbs.append("add citations")
    if any("code" in t or "diagram" in t or "example" in t for t in fmt_tokens):
        verbs.append("ground it with examples")

    # Decision style.
    dec = (them_comm.get("decision_style") or "").lower()
    if "recommendation" in dec and not any("exec" in v for v in verbs):
        verbs.append("lead with a recommendation")
    elif "evidence" in dec and not any("citation" in v for v in verbs):
        verbs.append("anchor it in evidence")

    verbs = list(dict.fromkeys(verbs))[:_MAX_PHRASES]
    if verbs:
        return ", ".join(verbs)

    # Nothing to go on. Say so rather than promising a tailoring that no
    # data backs — the old "I'll tune it to their style" read as a feature
    # working when it was really an empty profile, and it fired for 13 of
    # 15 people. This names the gap and implies the fix.
    return "no profile yet"


def translation_stats(person_id: str | None = None) -> dict[str, Any]:
    """Aggregate stats per person (or for a single person_id).

    Returns {person_id: {count, last_at}}. Used by /api/people/graph
    to paint edge thickness / recency on the graph.
    """
    from okuro.db import get_db

    db = get_db()
    sql = (
        "SELECT person_id, COUNT(*) AS n, MAX(created_at) AS last_at "
        "FROM translation_log WHERE success = 1"
    )
    params: tuple = ()
    if person_id:
        sql += " AND person_id = ?"
        params = (person_id,)
    sql += " GROUP BY person_id"

    rows = db.fetchall(sql, params)
    return {r["person_id"]: {"count": int(r["n"]), "last_at": r["last_at"]} for r in rows}
