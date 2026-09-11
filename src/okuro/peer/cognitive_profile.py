# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Canonical cognitive-profile schema + PII firewall projection.
# index:
#   constants
#   class Sliders
#   class CognitiveProfile
#   def cognitive_profile_for_llm
#   def role_default_sliders
# AGENT_HEADER_END -->
"""Cognitive profile — canonical shape used to translate sender content for a recipient.

Two responsibilities:

1. Define the schema written by Mode A (questionnaire), Mode B (sliders),
   Mode C (pairwise) — same shape regardless of input mode.

2. Provide ``cognitive_profile_for_llm(person_id)`` — the **PII firewall**.
   Every prompt sent to a model that translates content for a person MUST
   read the recipient through this function. It strips identifying fields
   (name, organization, email/phone, free-text relation, notes) and emits
   only the cognitive shape. The model never sees who the person is.

The ``persons.cognitive`` JSON column carries this shape (plus legacy
fields for back-compat). New writers should populate the keys defined
here; legacy keys remain readable so existing rows keep working.

THE FIREWALL CONTRACT
=====================

Written down because this function is the one place a redesign is likely to
"clean up", and three of its behaviours look accidental while being the
reason it works. Every clause below is enforced by
``tests/peer/test_pii_firewall.py`` — this section says WHY, the tests say
whether it still holds.

WHAT IT EXPOSES
    The cognitive shape only: the 17-axis slider vector, per-axis
    ``slider_confidence``, format preferences, jargon tolerance, decision
    style, relation archetype, topic interests, knowledge areas, and typed
    safety constraints. Nothing here identifies anybody.

WHAT IT HIDES
    Name, organization, email, phone, any free-text relation, and notes.
    Filtering is by KEY, over a denylist of name segments (``_PII_PARTS``),
    applied recursively — which is why registry fields are ``axis_id`` /
    ``label_low`` and never ``name``: a field called ``name`` would be
    silently deleted from every projection. Free-text VALUES are not
    scrubbed by the key filter, which is why ``safety_constraints`` is typed
    ``{kind, target_ref}`` rather than a string: it would otherwise have
    been an unfiltered PII channel straight into every prompt.

    The lens MAP never leaves this function. Its keys are company ids and
    project slugs, so shipping it would tell the model which organisations
    the person is affiliated with — exactly the identity the rest of this
    withholds. The lens is resolved here and only merged VALUES travel.

TWO INVARIANTS
    *Never raises.* Every path is wrapped; a broken row, a malformed JSON
    blob or a missing DB yields ``{}``. A firewall that crashes takes the
    translation path down with it, and the failure mode of a translation
    that did not happen is worse than one that ran on defaults.

    *``{}`` on miss.* An absent or inactive person is an empty projection,
    not an error and not a partial dict.

THE THREE BEHAVIOURS A REDESIGN MUST NOT LOSE
    They read like leftovers. They are not.

    1. LEGACY COERCION (``_normalize_legacy_cognitive``). Rows predate this
       schema. Dropping the coercion would not throw — it would silently
       project ``{}`` for the oldest and most-used profiles, and every
       consumer would carry on with neutral defaults, reporting nothing
       wrong.

    2. COMMUNICATION-COLUMN PULL. ``format_preferences``,
       ``decision_style`` and ``formality`` historically live in the
       ``communication`` column, not ``cognitive``. They are cognitive
       shape, not identity, so they are pulled across — and only when
       ``cognitive`` has not already supplied them, so a newer write always
       wins. Remove this and profiles written before the split go
       format-blind.

    3. ROLE SEEDING AT READ. A row with no stored sliders is seeded from
       ``role_default_sliders(role)`` at projection time, never persisted.
       The ROLE STRING is not shipped — only the vector derived from it —
       because "CEO at X" identifies, while a slider vector does not. Seeded
       axes are rated as priors by the confidence loop, so a seeded profile
       is honestly labelled rather than passed off as measured. Persisting
       the seed instead would make an assumption indistinguishable from an
       answer on the next read.

CONFIDENCE IS DERIVED OVER THE PROJECTED AXIS SET, NOT THE STORED ONE.
    The Pydantic gate manufactures a neutral 3 for every axis a row lacks.
    Rating only the stored axes left those manufactured neutrals with no
    confidence entry, so a partially profiled person looked MORE confident
    than an empty one. Verified live: a partial profile now reports exactly
    the axes it never measured as ``(prior)``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, create_model

from okuro.peer.axis_registry import axis_defaults, axis_ids, axis_labels


# ── slider dimensions (Mode B) ─────────────────────────────────────────
#
# Seventeen 5-step sliders. Pole 1 = LEFT label, pole 5 = RIGHT label. Order
# of definition is the canonical order — UI renderers and Mode-C posterior
# vectors both use this order. New axes are APPENDED (never inserted) so the
# ordinal positions of existing axes — and any stored posterior vectors —
# stay stable.
#
# The last three axes (certainty / rational / experiential) are backed by
# validated instruments and added by the People-survey redesign:
#   - certainty    ← MSTAT-II ambiguity tolerance (McLain 2009)
#   - rational     ← REI rationality (Pacini & Epstein 1999, JPSP)
#   - experiential ← REI experientiality (orthogonal to rational — both can be high)
#
# People-survey v2 (profession-agnostic lens) appends four research-backed
# axes and DERIVES/MERGES others:
#   - need_for_cognition ← NCS-6 (Coelho 2020): how much justification survives
#   - density            ← Cognitive Load Theory (Sweller 2019): chunking
#   - construal          ← Trope & Liberman 2010: why/strategy ↔ how/steps (also derives time_horizon)
#   - regulatory_focus   ← Higgins 1997: opportunity ↔ risk-guard framing (absorbs risk_framing)
# certainty + risk_framing are RETAINED for back-compat (existing rows) but v2
# scoring no longer sets them: certainty merges into decision_framing, risk_framing
# into regulatory_focus. lead_with + time_horizon are DERIVED, not asked.
# Both derived from peer.axis_registry, which is now the single definition.
# They keep their names and shapes so no call site changes — the registry
# replaced the hand-written literals underneath, not the interface above.
SLIDER_NAMES = axis_ids()
SLIDER_LABELS = axis_labels()


# ── role defaults — Mode B seeds the sliders from these on Add Person ──
#
# Numbers are 1-5. Edit when feedback shows a default mis-fits in practice.
# Add a new role by extending the dict; missing roles fall back to "default".
ROLE_DEFAULT_SLIDERS: dict[str, dict[str, int]] = {
    "default": {
        "information_depth": 3, "format": 3, "decision_framing": 3,
        "time_horizon": 3, "risk_framing": 3, "jargon": 3,
        "lead_with": 4, "pace": 3,
    },
    "ceo": {
        "information_depth": 5, "format": 3, "decision_framing": 5,
        "time_horizon": 5, "risk_framing": 3, "jargon": 2,
        "lead_with": 4, "pace": 4,
    },
    # Governance lens: strategy + risk + long horizon, low jargon, decision-led.
    # A board reads for oversight, not execution — concise, outcome-framed.
    "board": {
        "information_depth": 4, "format": 3, "decision_framing": 5,
        "time_horizon": 5, "risk_framing": 4, "jargon": 2,
        "lead_with": 4, "pace": 4,
    },
    # Top management: like ceo but a touch more operational detail tolerance.
    "top_management": {
        "information_depth": 4, "format": 3, "decision_framing": 5,
        "time_horizon": 4, "risk_framing": 3, "jargon": 2,
        "lead_with": 4, "pace": 4,
    },
    "cto": {
        "information_depth": 4, "format": 4, "decision_framing": 4,
        "time_horizon": 4, "risk_framing": 4, "jargon": 5,
        "lead_with": 5, "pace": 4,
    },
    "engineer": {
        "information_depth": 2, "format": 4, "decision_framing": 3,
        "time_horizon": 2, "risk_framing": 4, "jargon": 5,
        "lead_with": 4, "pace": 3,
    },
    "designer": {
        "information_depth": 3, "format": 4, "decision_framing": 4,
        "time_horizon": 3, "risk_framing": 3, "jargon": 4,
        "lead_with": 4, "pace": 4,
    },
    "product_manager": {
        "information_depth": 3, "format": 4, "decision_framing": 5,
        "time_horizon": 4, "risk_framing": 3, "jargon": 4,
        "lead_with": 5, "pace": 4,
    },
    "marketer": {
        "information_depth": 4, "format": 3, "decision_framing": 4,
        "time_horizon": 3, "risk_framing": 2, "jargon": 2,
        "lead_with": 4, "pace": 3,
    },
    "sales": {
        "information_depth": 4, "format": 3, "decision_framing": 5,
        "time_horizon": 3, "risk_framing": 2, "jargon": 2,
        "lead_with": 5, "pace": 4,
    },
    "client": {
        "information_depth": 4, "format": 3, "decision_framing": 5,
        "time_horizon": 3, "risk_framing": 3, "jargon": 2,
        "lead_with": 4, "pace": 3,
    },
}


# ── relation archetype — anonymized stand-in for free-text relation ────
#
# Free-text ``relation_to_user`` ("my CTO at Acme") leaks identity by
# implication. The archetype is a closed set that conveys the relational
# tone without naming entities.
RELATION_ARCHETYPES = (
    "peer", "boss", "report", "client", "vendor",
    "external", "family", "friend", "unknown",
)


def _fill_missing_axes(seed: dict[str, int]) -> dict[str, int]:
    """Overlay a role seed onto a neutral (3) base for EVERY canonical axis.

    Lets ``ROLE_DEFAULT_SLIDERS`` stay terse — a role dict only lists the
    axes it has an opinion about; axes added later (e.g. certainty / rational
    / experiential) auto-default to neutral 3 without touching nine role
    dicts. No assumptions are baked into the new axes — they start neutral
    and are set by the survey, not guessed from role.
    """
    return {**{axis: 3 for axis in SLIDER_NAMES}, **seed}


def role_default_sliders(role: Optional[str]) -> dict[str, int]:
    """Return the slider seed for a role. Falls back to ``default`` for unknowns.

    Lookup is case-insensitive and tolerant of common variants
    (``"CEO"`` / ``"Chief Executive Officer"`` / ``"ceo"`` all match). Every
    returned vector covers all canonical axes (missing axes neutral-filled).
    """
    if not role:
        return _fill_missing_axes(ROLE_DEFAULT_SLIDERS["default"])
    key = role.lower().strip()
    if key in ROLE_DEFAULT_SLIDERS:
        return _fill_missing_axes(ROLE_DEFAULT_SLIDERS[key])
    # crude alias pass for common verbose titles
    aliases = {
        "chief executive officer": "ceo",
        "chief technology officer": "cto",
        "software engineer": "engineer",
        "developer": "engineer",
        "product manager": "product_manager",
        "pm": "product_manager",
        "board member": "board",
        "verwaltungsrat": "board",
        "board of directors": "board",
        "top management": "top_management",
        "executive board": "top_management",
        "geschäftsleitung": "top_management",
    }
    return _fill_missing_axes(
        ROLE_DEFAULT_SLIDERS.get(aliases.get(key, "default"), ROLE_DEFAULT_SLIDERS["default"])
    )


# ── pydantic models ────────────────────────────────────────────────────


# GENERATED from the registry, not hand-declared.
#
# This model is the LLM-read gate: cognitive_profile_for_llm builds it and
# Pydantic drops any key the model does not declare. A hand-written mirror
# therefore fails SILENTLY — an axis appended to the vocabulary and forgotten
# here is swallowed on the recipient-read path with no error anywhere. That
# has already happened once (commit 9bcb06bb added a guard test; a test
# catches drift, generation prevents it).
#
# Defaults stay INT, never Optional[int] = None. See axis_registry.axis_defaults
# for why: exclude_none would delete unmeasured axes from every consumer's
# dict, and the honesty signal belongs in slider_confidence instead.
Sliders = create_model(  # type: ignore[call-overload]
    "Sliders",
    __doc__=(
        f"{len(axis_ids())}-axis 5-step slider vector, generated from "
        "peer.axis_registry. Pole 1 = low label, pole 5 = high label."
    ),
    **{
        axis_id: (
            int,
            Field(default=default, ge=1, le=5,
                  description="1={}, 5={}".format(*axis_labels()[axis_id])),
        )
        for axis_id, default in axis_defaults().items()
    },
)


class CognitiveProfile(BaseModel):
    """Canonical shape of ``persons.cognitive``.

    All fields optional: a partial profile is valid (Mode B might fill
    only sliders; Mode A might fill only free fields). Renderers must
    skip absent fields gracefully.
    """

    sliders: Sliders = Field(default_factory=Sliders)
    relation_archetype: str = Field(default="unknown")  # closed set, see RELATION_ARCHETYPES
    formality_baseline: Optional[str] = None  # informal / neutral / formal
    format_preferences: list[str] = Field(default_factory=list)
    avoid_structural: list[str] = Field(default_factory=list)
    jargon_tolerance: Optional[str] = None  # layman / business / expert
    decision_style: Optional[str] = None
    learning_style: Optional[str] = None
    attention_span: Optional[str] = None  # short / medium / long

    # `mode_c_posterior` was removed 2026-08-03. It had zero readers anywhere
    # while its comment asserted a reconciliation ("Mode-C wins for axes it
    # observed; Mode-B fills the rest") that nothing implemented — a field
    # whose documentation promised a guarantee the code never made. Fable's
    # v1 plan called it out under DP09; it survived v1->v7 untouched.
    # Stored rows may still carry the key; it is simply not projected.

    # Topic interests — used by translation to flag relevance ("recipient
    # also cares about Y, weave it in"). Each entry: {"topic": str, "weight": float}.
    # Topic strings must be GENERIC ("AI infrastructure") not personal
    # ("Anna's startup") — the firewall enforces this on read.
    topic_interests: list[dict[str, Any]] = Field(default_factory=list)

    # Per-axis confidence derived from slider_provenance (or a role-seed prior).
    # 1.0 = user-set; ~0.4 = role default not yet confirmed. Lets the lens flag
    # priors so the agent treats them as assumptions, not observations. Carries
    # no PII (axis name → float), so it passes the firewall untouched.
    slider_confidence: dict[str, float] = Field(default_factory=dict)

    # People-survey v2 — profession-agnostic knowledge map. Per-area depth +
    # detail drives PER-AREA jargon/explain-level (expertise-reversal, Kalyuga
    # 2003): jargon is topic-relative, not one global number. Weaknesses tell
    # the rewriter what to expand-with-examples vs compress/omit. Area names are
    # generic capability labels, not identity — they pass the firewall.
    knowledge_areas: list[dict[str, Any]] = Field(default_factory=list)
    weaknesses: list[dict[str, Any]] = Field(default_factory=list)

    # Role priors (generic, non-identifying): seed weak priors + give the
    # rewriter altitude/jargon context. profession is free-text but generic.
    profession: Optional[str] = None
    function: Optional[str] = None
    seniority: Optional[str] = None

    # PER-VALUE usage: what this PERSON says about an axis of their own
    # profile — {axis_id: required|preferred|optionally_use|prohibited}.
    #
    # Distinct from the axis-level `usage` in axis_registry, which says what a
    # CONSUMER must do with an axis in general. This one is a personal
    # declaration: "expert vocabulary is not a preference for me, it is a
    # requirement". It is the thing that makes "required never floors" in
    # group aggregation implementable — without it there is no way to tell a
    # non-negotiable from a leaning, so a group merge silently lowers both.
    #
    # Axis name -> enum. Carries no PII, passes the firewall untouched.
    value_usage: dict[str, str] = Field(default_factory=dict)

    # Hard constraints on what the output may say. TYPED, never free text.
    #
    # The firewall strips KEYS, never VALUES (_strip_pii below). A free-text
    # constraint like "don't mention the Acme lawsuit" therefore travels
    # verbatim into every prompt builder that reads this profile — the field
    # meant to protect the person becomes a new PII channel pointed straight
    # at an LLM. So the shape is {kind, target_ref} where target_ref is an
    # opaque id resolved renderer-side, and a phrase cannot be expressed at
    # all. Enforced on read by _scrub_safety_constraints, not by convention.
    safety_constraints: list[dict[str, Any]] = Field(default_factory=list)


# ── PII firewall ───────────────────────────────────────────────────────


# Two-layer PII denylist:
#  - FULL keys: exact, case-insensitive ("id" must NOT match "sliders").
#  - PARTS:    word-segment match ("display_name" splits to {"display",
#              "name"}; "name" in PARTS triggers the strip). Catches
#              future variants like ``email_v2`` / ``contact_info`` /
#              ``user_phone`` without re-listing every shape.
_PII_FULL_KEYS = frozenset({
    "id", "display_name", "relation_to_user", "notes",
})
_PII_PARTS = frozenset({
    "name", "email", "phone", "address", "handle",
    "organization", "org", "company", "employer",
    "contact",
})


def _key_has_pii(key: str) -> bool:
    """True when the key names an identifying field (exact or part match)."""
    kl = str(key).lower()
    if kl in _PII_FULL_KEYS:
        return True
    parts = kl.replace("-", "_").split("_")
    return any(p in _PII_PARTS for p in parts)


# ── safety constraints — typed, so a phrase cannot be expressed ────────

# The closed set of things a person can forbid. A kind a renderer does not
# understand must make that renderer REFUSE, not proceed — see
# unsupported_constraints().
SAFETY_CONSTRAINT_KINDS = frozenset({
    "do_not_mention",     # a topic/entity, by ref
    "do_not_raise",       # a decision/ask, by ref
    "do_not_compare",     # a comparison against a ref
    "no_attribution",     # do not name who said it
    "no_numbers",         # no raw figures for this ref
})

# An opaque id: either a namespaced handle (`topic:pricing`) or a bare hex
# row id. A sentence, a person's name, or "the Acme lawsuit" cannot match —
# which is the entire point of typing this field.
_TARGET_REF_RE = re.compile(
    r"^(?:[a-z][a-z0-9]{0,15}:[a-z0-9][a-z0-9_.-]{0,63}|[a-f0-9]{8,32})$"
)

# Namespaces that name a PERSON or ORG. A constraint may still travel — the
# renderer must know it exists — but its ref is redacted, because resolving
# identity is the renderer's job and the LLM has no business seeing it.
_IDENTITY_NAMESPACES = frozenset({"person", "people", "company", "org", "contact"})

_REDACTED_REF = "redacted"


def _scrub_safety_constraints(items: Any) -> list[dict[str, Any]]:
    """Reduce constraints to {kind, target_ref}, dropping anything unsound.

    Fail-closed on the item: an unknown kind, a malformed ref, or any extra
    key means the item does not survive. A constraint that cannot be
    expressed safely is not silently downgraded to a free-text hint.
    """
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        ref = item.get("target_ref")
        if kind not in SAFETY_CONSTRAINT_KINDS:
            continue
        if not isinstance(ref, str) or not _TARGET_REF_RE.match(ref):
            continue
        namespace = ref.split(":", 1)[0] if ":" in ref else ""
        if namespace in _IDENTITY_NAMESPACES:
            ref = _REDACTED_REF
        out.append({"kind": kind, "target_ref": ref})
    return out


def unsupported_constraints(
    profile: dict[str, Any] | None, supported: set[str] | frozenset[str]
) -> list[str]:
    """Constraint kinds present on ``profile`` that ``supported`` cannot honour.

    Fail-closed contract: a renderer calls this and REFUSES when the list is
    non-empty. Proceeding while ignoring a constraint the person set is worse
    than producing nothing — the person was told it would be respected.
    """
    constraints = (profile or {}).get("safety_constraints") or []
    kinds = {c.get("kind") for c in constraints if isinstance(c, dict)}
    return sorted(k for k in kinds if k and k not in supported)


def _strip_pii(obj: Any) -> Any:
    """Recursively drop any dict key whose name matches the denylist."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if _key_has_pii(key):
                continue
            out[key] = _strip_pii(value)
        return out
    if isinstance(obj, list):
        return [_strip_pii(v) for v in obj]
    return obj


def _normalize_legacy_cognitive(cog_raw: Any) -> dict[str, Any]:
    """Coerce legacy ``persons.cognitive`` shapes into CognitiveProfile-compatible.

    Legacy shape (pre-2026-05-01): free-form dict with keys like
    ``learning_style``, ``attention_span``, ``pet_peeves``, ``expertise_level``.
    The new shape adds ``sliders`` + ``relation_archetype``.
    Missing keys default; extras are dropped via the model's strict schema.
    """
    if not isinstance(cog_raw, dict):
        return {}
    return {
        "learning_style": cog_raw.get("learning_style"),
        "attention_span": cog_raw.get("attention_span"),
        "avoid_structural": cog_raw.get("pet_peeves") or cog_raw.get("avoid_structural") or [],
        # Pass through new keys when present.
        "sliders": cog_raw.get("sliders") or {},
        "relation_archetype": cog_raw.get("relation_archetype") or "unknown",
        "formality_baseline": cog_raw.get("formality_baseline"),
        "format_preferences": cog_raw.get("format_preferences") or [],
        "jargon_tolerance": cog_raw.get("jargon_tolerance"),
        "decision_style": cog_raw.get("decision_style"),
        "topic_interests": cog_raw.get("topic_interests") or [],
        "knowledge_areas": cog_raw.get("knowledge_areas") or [],
        "weaknesses": cog_raw.get("weaknesses") or [],
        "value_usage": cog_raw.get("value_usage") or {},
        # Scrubbed HERE, on the read path, not trusted from storage: the
        # column is schemaless JSON that several writers touch.
        "safety_constraints": _scrub_safety_constraints(
            cog_raw.get("safety_constraints")
        ),
        "profession": cog_raw.get("profession"),
        "function": cog_raw.get("function"),
        "seniority": cog_raw.get("seniority"),
        # Provenance is read here to DERIVE slider_confidence; it is not a model
        # field, so pydantic drops the raw map after the derivation below.
        "slider_provenance": cog_raw.get("slider_provenance") or {},
    }


def slider(source: Any, axis: str, default: int = 3) -> int:
    """Read ONE axis by name. The single typed way in.

    Every wrong-axis defect in this codebase was a string literal handed to
    an untyped ``.get()``: ``info_depth`` (never existed, killed the depth
    lever for the life of the module), ``information_depth`` read where
    ``jargon`` was meant, ``.sliders`` read off a preset that has no such
    key. A ``.get()`` cannot tell a typo from a legitimate absence — it
    returns the default for both, which is exactly why those three ran
    undetected.

    This one refuses an axis that is not in ``SLIDER_NAMES``. A typo is a
    ValueError at the call site instead of a plausible default three stages
    downstream. The AST guard in tests/peer/test_axis_literal_scanner.py
    catches the same class statically; this catches what reaches runtime by
    a path the scanner cannot see.

    ``source`` may be a firewalled profile (``{"sliders": {...}}``) or a
    bare slider dict — both shapes are passed around today.

    Validation delegates to ``axis_registry.axis()``, so the accessor and
    the registry cannot disagree about what an axis is.
    """
    from okuro.peer.axis_registry import axis as _axis_entry

    _axis_entry(axis)  # raises ValueError on an unknown id
    if not isinstance(source, dict):
        return default
    sliders = source.get("sliders") if isinstance(source.get("sliders"), dict) else source
    value = sliders.get(axis)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value) if 1 <= value <= 5 else default


def slider_json_schema_properties() -> dict[str, dict[str, Any]]:
    """JSON-Schema properties for every canonical axis, generated.

    The MCP contract for ``person_update_sliders`` hand-listed eight axes
    while the function validated seventeen. The nine newer ones worked and
    were invisible — which is precisely why they sat at zero coverage
    across every real person: no agent could discover them.

    Generated from ``SLIDER_NAMES`` + ``SLIDER_LABELS`` so an appended axis
    reaches the contract by construction. Hand-maintained mirrors of this
    vocabulary drift; this one cannot.
    """
    props: dict[str, dict[str, Any]] = {}
    for axis in SLIDER_NAMES:
        low, high = SLIDER_LABELS.get(axis, ("low", "high"))
        props[axis] = {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": f"1={low}, 5={high}",
        }
    return props


# Below this, an axis is a prior rather than an observation. Matches the
# threshold person_lens has always used for its "unconfirmed axes" list.
PRIOR_CONFIDENCE_THRESHOLD = 0.6

# SOFT decay (D-PM3). A profile is a claim about a person, and people change
# jobs, seniority and subject matter. An axis nobody has re-confirmed in a
# year is not wrong — it is UNVERIFIED, and the honest response is to trust
# it less, not to delete it. Over-retention is the measured failure mode of
# every profile store; silent deletion is the failure mode of the systems
# built to fix that, and it is worse, because the person cannot see what was
# lost. So: multiply, floor, never remove.
DECAY_AFTER_DAYS = 365
DECAY_FACTOR = 0.7
DECAY_FLOOR = 0.3


def decay_confidence(
    confidence: float, last_confirmed_at: Any, now: Any = None
) -> float:
    """Discount a confidence that has gone unconfirmed past the window.

    Returns ``confidence`` unchanged when it is fresh, when the timestamp is
    missing or unparseable (absence of evidence about age is not evidence of
    staleness), or when it already sits at or below the floor.
    """
    from datetime import datetime, timezone

    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return 0.0
    if confidence <= DECAY_FLOOR or not last_confirmed_at:
        return float(confidence)
    try:
        stamp = last_confirmed_at
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return float(confidence)
    current = now or datetime.now(timezone.utc)
    if getattr(current, "tzinfo", None) is None:
        current = current.replace(tzinfo=timezone.utc)
    if (current - stamp).days < DECAY_AFTER_DAYS:
        return float(confidence)
    return max(DECAY_FLOOR, float(confidence) * DECAY_FACTOR)


def _ledger_confidence(db: Any, person_id: str) -> dict[str, tuple[float, Any]]:
    """``{axis: (confidence, last_confirmed_at)}`` from person_sources.

    person_sources is meant to be THE provenance ledger, and until now no
    generation path read it for anything — it recorded who claimed what and
    then no decision ever consulted it. This is the read side: per-axis rows
    (migration 123) carry both the confidence a source was trusted with and
    when it last said so, which is exactly what a decay policy needs.

    Never raises: a missing column on an un-migrated DB must not break the
    firewall, whose whole contract is that it cannot fail a translation.
    """
    try:
        rows = db.fetchall(
            "SELECT axis, confidence, created_at FROM person_sources "
            "WHERE person_id = ? AND applied = 1 AND axis IS NOT NULL "
            "AND field_path = 'cognitive.sliders' "
            "ORDER BY created_at ASC, rowid ASC",
            (person_id,),
        )
    except Exception:  # noqa: BLE001 — un-migrated DB, or no table at all
        return {}
    out: dict[str, tuple[float, Any]] = {}
    for row in rows or []:
        try:
            out[row["axis"]] = (float(row["confidence"]), row["created_at"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def format_sliders_for_prompt(profile: dict[str, Any]) -> str:
    """Render a firewalled profile's sliders as one prompt-ready string.

    THE point of this function: an axis nobody ever measured must not read
    like a value the person chose. Five call sites independently emitted
    ``f"{k}={v}/5"``, so `slider_confidence` — the only signal separating
    an observation from a manufactured neutral — reached person_lens and
    nothing else. Priors travelled into every translate, prism, delivery
    and resonance prompt wearing the costume of facts.

    Under ``people.strict`` an axis whose confidence is below
    :data:`PRIOR_CONFIDENCE_THRESHOLD` is marked ``(prior)``. A profile
    with no ``slider_confidence`` (a group aggregate, a legacy row) is
    rendered unannotated — absence of the signal is not evidence against
    an axis.

    Returns "" when there are no sliders, so callers can test truthiness.
    """
    from okuro.peer.flags import people_strict_enabled, warn_lenient

    sliders = profile.get("sliders") if isinstance(profile, dict) else None
    if not isinstance(sliders, dict) or not sliders:
        return ""

    if not people_strict_enabled():
        warn_lenient(
            "cognitive_profile.format_sliders_for_prompt",
            "priors enter prompts unlabelled, indistinguishable from observations",
        )
        return ", ".join(f"{k}={v}/5" for k, v in sliders.items())

    conf = profile.get("slider_confidence")
    conf = conf if isinstance(conf, dict) else {}
    bits: list[str] = []
    for axis, value in sliders.items():
        rating = conf.get(axis)
        is_prior = isinstance(rating, (int, float)) and rating < PRIOR_CONFIDENCE_THRESHOLD
        bits.append(f"{axis}={value}/5 (prior)" if is_prior else f"{axis}={value}/5")
    return ", ".join(bits)


def _resolve_lens(cog_raw: dict[str, Any], lens_key: str | None) -> dict[str, int]:
    """Slider overrides for one context, from the person's lens sets.

    A person is not one shape in every room: the same CTO wants depth from
    engineering and altitude on a board seat. Context-scoped sets express
    that — ``cognitive.lenses.<lens_key>`` holds only the axes that DIFFER
    in that context, layered over the base vector.

    ``lens_key`` is any context id: a company id, a project slug, a target
    group. The legacy ``cognitive.overlays.<company_id>`` map is read as a
    lens under the same key, so the one orphaned consumer keeps working and
    old rows need no migration.

    Returns ``{}`` for no key, an unknown key, or a malformed set.
    """
    if not lens_key or not isinstance(cog_raw, dict):
        return {}
    merged: dict[str, int] = {}
    # overlays first, so a lens set written under the new name wins.
    for bucket in ("overlays", "lenses"):
        entries = cog_raw.get(bucket)
        if not isinstance(entries, dict):
            continue
        overrides = entries.get(lens_key)
        if not isinstance(overrides, dict):
            continue
        for axis, value in overrides.items():
            if axis in SLIDER_NAMES and isinstance(value, int) \
                    and not isinstance(value, bool) and 1 <= value <= 5:
                merged[axis] = value
    return merged


def cognitive_profile_for_llm(
    person_id: str, lens_key: str | None = None
) -> dict[str, Any]:
    """Return the ANONYMIZED cognitive profile for ``person_id``.

    THIS is the only function any LLM-bound prompt builder may call to
    read a recipient. Returns sliders + format prefs + jargon tolerance +
    decision style + relation archetype + topic interests — never name,
    org, contact, free-text relation, or notes.

    Returns ``{}`` when the person is missing or has no cognitive data.
    Raises nothing — the firewall must never crash a translation path.
    """
    try:
        from okuro.db import get_db

        db = get_db()
        row = db.fetchone(
            "SELECT cognitive, communication, role FROM persons "
            "WHERE id = ? AND active = 1",
            (person_id,),
        )
        if not row:
            return {}

        cog_raw = row["cognitive"]
        if isinstance(cog_raw, str) and cog_raw:
            try:
                cog_raw = json.loads(cog_raw)
            except json.JSONDecodeError:
                cog_raw = {}

        comm_raw = row["communication"]
        if isinstance(comm_raw, str) and comm_raw:
            try:
                comm_raw = json.loads(comm_raw)
            except json.JSONDecodeError:
                comm_raw = {}

        normalized = _normalize_legacy_cognitive(cog_raw)
        # Pull format / decision / jargon from the communication column too —
        # these are cognitive-shape, not identity, but historically lived under
        # ``communication``. Safe to surface to the LLM.
        if isinstance(comm_raw, dict):
            if not normalized.get("format_preferences") and comm_raw.get("format_preferences"):
                normalized["format_preferences"] = comm_raw["format_preferences"]
            if not normalized.get("decision_style") and comm_raw.get("decision_style"):
                normalized["decision_style"] = comm_raw["decision_style"]
            if not normalized.get("formality_baseline") and comm_raw.get("formality"):
                normalized["formality_baseline"] = comm_raw["formality"]

        # Seed sliders from role default if the row didn't store them yet.
        # Role itself is generic ("CEO" — not identifying) but we don't
        # ship it; we ship the SLIDER VECTOR derived from it.
        role_seeded = False
        if not normalized.get("sliders"):
            normalized["sliders"] = role_default_sliders(row["role"])
            role_seeded = True

        # Context-scoped lens, layered over the base vector.
        #
        # Resolved HERE rather than by the caller, because the lens MAP must
        # never leave this function: its keys are company ids and project
        # slugs, so shipping the map would tell the LLM which organisations
        # the person is affiliated with — precisely the identity the rest of
        # this firewall exists to withhold. Only the merged VALUES travel.
        #
        # crm.engagement_resolve used to do this itself by re-reading
        # persons.cognitive raw, which was a second read path around the
        # firewall and the only consumer of a field nothing ever wrote.
        lens = _resolve_lens(cog_raw if isinstance(cog_raw, dict) else {}, lens_key)
        if lens:
            normalized["sliders"] = {**(normalized.get("sliders") or {}), **lens}
            # A lens value is a deliberate statement about this context, so
            # it is an observation, not a prior — recorded after the
            # confidence loop below via `lens_axes`.
        lens_axes = set(lens)

        # Derive per-axis confidence over the PROJECTED axis set, not the
        # stored one. The Pydantic gate below manufactures a neutral 3 for
        # every axis the row does not carry; iterating only the stored keys
        # left those manufactured neutrals with no confidence entry at all,
        # so person_lens could not name them — and a partially profiled
        # person looked MORE confident than an unprofiled one. More data
        # made the honesty signal worse.
        #
        # Rules, in order:
        #   role-seeded row      -> every axis is a prior (0.4)
        #   stored provenance    -> its recorded confidence
        #   stored, no provenance-> UNRATED (the legacy carve-out: these are
        #                           real user-set values from before the
        #                           ledger existed; flagging them would be a
        #                           false accusation)
        #   not stored at all    -> 0.4, because Pydantic is about to invent
        #                           a value for it
        from okuro.peer.flags import people_strict_enabled, warn_lenient

        prov = normalized.get("slider_provenance") or {}
        stored_axes = set(normalized.get("sliders") or {})
        # The ledger is the preferred source: it covers every write path and
        # carries WHEN a source last said so, which the inline
        # slider_provenance map cannot (it is last-write-wins with no age).
        ledger = _ledger_confidence(db, person_id)
        confidence: dict[str, float] = {}

        if people_strict_enabled():
            axes_to_rate: tuple[str, ...] | set[str] = SLIDER_NAMES
        else:
            warn_lenient(
                "cognitive_profile.slider_confidence",
                "rating only stored axes, so manufactured neutrals stay unflagged",
            )
            axes_to_rate = stored_axes

        for axis in axes_to_rate:
            if axis in lens_axes:
                # Someone set this axis FOR THIS CONTEXT. That is a
                # deliberate statement, not a role prior — even on a person
                # whose base vector is entirely role-seeded.
                confidence[axis] = 1.0
            elif role_seeded:
                confidence[axis] = 0.4
            elif axis in ledger:
                rated, confirmed_at = ledger[axis]
                confidence[axis] = decay_confidence(rated, confirmed_at)
            elif isinstance(prov.get(axis), dict) and "confidence" in prov[axis]:
                try:
                    confidence[axis] = decay_confidence(
                        float(prov[axis]["confidence"]), prov[axis].get("updated_at")
                    )
                except (TypeError, ValueError):
                    pass
            elif axis not in stored_axes:
                confidence[axis] = 0.4
        if confidence:
            normalized["slider_confidence"] = confidence

        profile = CognitiveProfile(**normalized).model_dump(exclude_none=True)
        return _strip_pii(profile)

    except Exception:
        # PII firewall must never break translate. On any error: empty
        # profile → bridge gets a generic prompt, no leak.
        return {}


__all__ = [
    "SLIDER_NAMES",
    "SLIDER_LABELS",
    "ROLE_DEFAULT_SLIDERS",
    "RELATION_ARCHETYPES",
    "Sliders",
    "CognitiveProfile",
    "cognitive_profile_for_llm",
    "role_default_sliders",
]
