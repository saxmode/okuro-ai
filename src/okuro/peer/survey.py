# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deterministic People-survey v2 scorer — profession-agnostic lens, no LLM.
# index:
#   constants (palette, sliders, tiers)
#   def survey_form
#   def score_survey
#   def survey_to_delta
# AGENT_HEADER_END -->
"""Deterministic scorer for the People survey v2 (profession-agnostic lens).

v1 hardcoded an engineer↔designer topic bank. v2 works for ANYONE: the
person declares their own knowledge areas + depth (horizontal sliders),
answers a handful of profession-agnostic cognitive/communication sliders,
and states position/profession/seniority. The output is a person-stable
LENS used to rewrite documents (e.g. a slide deck dropped on them).

Pure + DB-free + reproducible (no LLM). Research backing in
docs/research/people-profiling-v2/{knowledge,cognitive,communication,role-priors}.md.

Captured directly (slider answers, high confidence):
  need_for_cognition (NCS-6), rational + experiential (REI), density (CLT),
  construal (Trope & Liberman), regulatory_focus (Higgins).
Mapped from a trait (medium confidence):
  format ← visual↔verbal (OSIVQ), decision_framing ← ambiguity/closure (MSTAT),
  information_depth ← NfC, time_horizon ← construal.
Role prior (low confidence, overridden by answers):
  lead_with + pace ← seniority (BLUF/Minto; Jaques stratified systems).
Knowledge → PER-AREA jargon (expertise-reversal, Kalyuga 2003) + global floor,
  with a regression-to-mean shrink for self-assessment inflation (DK is mostly
  an artifact — Gignac & Zajenkowski 2020; no quartile correction).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from okuro.peer.cognitive_profile import SLIDER_NAMES

# ── confidence tiers ────────────────────────────────────────────────────
CONF_DIRECT = 0.95      # answered as a slider, used as-is
CONF_MAPPED = 0.75      # derived from one answered trait via a reasoned mapping
CONF_PRIOR = 0.50       # role/seniority prior, meant to be overridden
CONF_OVERCLAIM = 0.60   # derived jargon, self-assessment looked inflated

_SOURCE = "questionnaire"

# ── STRUCTURE lives here, WORDS live in peer.survey_i18n ────────────────
#
# This module owns which axes exist, in what order, on which step, at which
# tier. Every recipient-facing string is in the catalogue. The split is what
# makes a fifth language a data change rather than a twelve-copy edit.
#
# The English catalogue is also the source of the module-level constants
# below, so there is still exactly ONE definition of the axis vocabulary —
# it just lives next to its translations now.
from okuro.peer.survey_i18n import catalogue as _catalogue

_EN_STRINGS = _catalogue("en").strings

_COGNITIVE_IDS: tuple[str, ...] = (
    "need_for_cognition", "rational", "experiential", "visual_verbal",
    "density", "ambiguity", "numeracy", "graph_literacy",
)
_ANGLE_IDS: tuple[str, ...] = ("construal", "regulatory_focus")
_MODULE_KEYS: tuple[str, ...] = ("knowledge", "register", "quant", "angle")
# Stored values, not display text — the scorer reads these keys, so they stay
# English in every language. Only their LABELS are translated.
_RECENCY_KEYS: tuple[str, ...] = ("current", "rusty", "long ago")


def _bank(ids: tuple[str, ...]) -> dict[str, tuple[str, str, str]]:
    axes = _EN_STRINGS["axes"]
    return {
        sid: (axes[sid]["statement"], axes[sid]["low"], axes[sid]["high"])
        for sid in ids
    }


# ── universal palette (profession-agnostic) ─────────────────────────────
UNIVERSAL_DOMAINS: tuple[str, ...] = tuple(_EN_STRINGS["knowledge"]["domains"])
FUNCTION_OPTIONS: tuple[str, ...] = (
    UNIVERSAL_DOMAINS + (_EN_STRINGS["knowledge"]["function_other"],)
)
SENIORITY_OPTIONS: tuple[str, ...] = tuple(_EN_STRINGS["knowledge"]["seniority"])

# Depth slider anchors (0-5), labels only — the continuous value is the datum.
DEPTH_ANCHORS: tuple[str, ...] = tuple(_EN_STRINGS["knowledge"]["depth_anchors"])

# Cognitive + angle item banks — DERIVED from the English catalogue, so the
# axis vocabulary has one definition and it sits next to its translations.
# The two quant axes carry one item each from the validated short forms
# (subjective numeracy, Fagerlin et al. 2007 / SNS-3; subjective graph
# literacy, Garcia-Retamero et al. 2016). ONE ITEM IS A SOFT PRIOR, NOT THE
# VALIDATED SCORE — the full instruments are 3 and 4 items.
COGNITIVE_SLIDERS: dict[str, tuple[str, str, str]] = _bank(_COGNITIVE_IDS)
ANGLE_SLIDERS: dict[str, tuple[str, str, str]] = _bank(_ANGLE_IDS)

# ── step sequence: ONE definition of order, copy and tier ───────────────
#
# The survey is a 3-minute CORE plus skippable progressive MODULES. That split
# needs exactly one definition or the two transports drift on what "core"
# means — the same hand-maintained-mirror class that already shipped an
# 8-of-17 MCP schema and a card bank per renderer.
#
# CORE measures two axes and derives four. need_for_cognition alone yields
# information_depth + lead_with (both CONF_MAPPED in score_survey) and itself
# (CONF_DIRECT); visual_verbal yields format. So four of the axes a rewrite
# leans on hardest come out of two taps.
#
# `axes` lists the axis ids a step MEASURES — the contract the card banks in
# questionnaire.tsx and offline_survey.py are pinned against by
# tests/peer/test_survey_form_parity.py. Steps that collect identity, free
# text or navigation measure nothing and carry an empty list.
_STEPS: tuple[dict[str, Any], ...] = (
    {"id": "intro", "tier": "core", "axes": []},
    {"id": "you", "tier": "core", "axes": []},
    {"id": "nfc", "tier": "core",
     "axes": ["need_for_cognition"]},
    {"id": "format", "tier": "core", "axes": ["visual_verbal"]},
    # The gate. Everything after it is optional and the recipient is told so.
    {"id": "more", "tier": "core", "axes": []},

    {"id": "areas", "tier": "module",
     "module": "knowledge", "axes": []},
    {"id": "depth", "tier": "module",
     "module": "knowledge", "axes": []},
    {"id": "weak", "tier": "module",
     "module": "knowledge", "axes": []},
    {"id": "confidence", "tier": "module",
     "module": "knowledge", "axes": []},

    {"id": "decide", "tier": "module",
     "module": "register", "axes": ["rational", "experiential"]},
    {"id": "density", "tier": "module",
     "module": "register", "axes": ["density"]},
    {"id": "ambiguity", "tier": "module",
     "module": "register", "axes": ["ambiguity"]},

    {"id": "numeracy", "tier": "module",
     "module": "quant", "axes": ["numeracy"]},
    {"id": "graph_literacy", "tier": "module",
     "module": "quant", "axes": ["graph_literacy"]},

    {"id": "construal", "tier": "module",
     "module": "angle", "axes": ["construal"]},
    {"id": "framing", "tier": "module",
     "module": "angle", "axes": ["regulatory_focus"]},

    {"id": "recap", "tier": "core", "axes": []},
)

# The gate screen's offer order is _MODULE_KEYS; the names and blurbs come
# from the catalogue, like every other recipient-facing string.


# Seniority → weak prior for the two axes we don't ask directly. Keys are the
# normalized seniority token. Only lead_with + pace (BLUF/altitude); everything
# else is measured or mapped and wins over this.
_SENIORITY_PRIORS: dict[str, dict[str, int]] = {
    "executive": {"lead_with": 5, "pace": 4},
    "founder": {"lead_with": 5, "pace": 4},
    "director": {"lead_with": 4, "pace": 4},
    "manager": {"lead_with": 4, "pace": 3},
    "lead": {"lead_with": 4, "pace": 3},
    "ic": {"lead_with": 3, "pace": 3},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_round(value: float) -> int:
    return int(round(min(5.0, max(1.0, value))))


def _to15(v0_5: float) -> int:
    """Map a 0-5 UI slider to okuro's 1-5 scale."""
    return _clamp_round(1.0 + (max(0.0, min(5.0, v0_5)) / 5.0) * 4.0)


def _invert(v1_5: int) -> int:
    return 6 - v1_5


def _num(d: dict, key: str) -> Optional[float]:
    raw = d.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# Legacy English display strings, for rows written before seniority became a
# key. Ordered longest-first so "c-level" cannot be shadowed, and matched as
# WORDS, never as bare substrings — the old loop tested `"ic" in s`, which
# made "Bereichsleitung" and "Vertice aziendale" (both senior) resolve to
# `ic`. A top executive scored as an individual contributor is worse than no
# prior at all.
_LEGACY_SENIORITY = (
    ("founder", "founder"), ("owner", "founder"),
    ("c-level", "executive"), ("executive", "executive"),
    ("director", "director"), ("vp", "director"),
    ("manager", "manager"),
    ("lead", "lead"), ("senior", "lead"),
    ("hands-on", "ic"), ("ic", "ic"),
)


def _seniority_key(seniority: Optional[str]) -> Optional[str]:
    """Resolve a seniority answer to a prior key.

    The survey now SENDS the key ("executive"), because the options are
    offered in four languages and the stored value must not depend on which
    one the recipient read. This is the same rule the recency options and the
    card values follow: translate the label, never the datum.

    The legacy branch below exists only for rows captured before that change,
    when the English display string was stored verbatim.
    """
    if not seniority:
        return None
    s = seniority.strip().lower()
    if s in _SENIORITY_PRIORS:
        return s
    # Legacy rows: word-boundary match on the old English labels.
    words = set(re.split(r"[^a-z-]+", s))
    for token, key in _LEGACY_SENIORITY:
        if token in words:
            return key
    return None


def survey_form(lang: str | None = None) -> dict[str, Any]:
    """Return the v2 survey definition — ONE source both transports render.

    Every recipient-facing WORD comes from ``peer.survey_i18n``; this
    function owns the STRUCTURE (which axes exist, in what order, on which
    step, at which tier) and nothing else. That split is what makes four
    languages a data change instead of a twelve-copy edit.

    ``lang`` falls back to English for an unknown code — a recipient opening
    a link must always get a usable form. The resolved language is returned
    so the caller (and the document's own ``lang`` attribute) can tell what
    they got.

    REVIEW STATUS IS DELIBERATELY NOT ON THIS PAYLOAD. It belongs to the
    DELIVERY decision, which ``peer.survey_guard`` owns and enforces before a
    survey is handed out. Returning it here would put a field on a
    recipient-facing payload that no renderer can honestly act on — showing
    "this translation is unreviewed" to the recipient is not the answer, and
    anything else is a field nobody reads.

    Profession-agnostic; no hardcoded domain topic list (the person declares
    their own areas, seeded by the universal palette).
    """
    from okuro.peer.survey_i18n import catalogue

    cat = catalogue(lang)
    s = cat.strings

    def _items(ids: tuple[str, ...]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sid in ids:
            a = s["axes"][sid]
            out.append({
                "id": sid,
                "statement": a["statement"],
                "low": a["low"],
                "high": a["high"],
                "scale": [0, 5],
                # The card VALUES are never translated — they are the datum.
                # Only label and helper are language-dependent.
                "cards": [
                    {"value": v, "label": lbl, "helper": helper}
                    for v, lbl, helper in a["cards"]
                ],
            })
        return out

    steps = []
    for step in _STEPS:
        copy = s["steps"].get(step["id"], {})
        steps.append({**step, **{k: v for k, v in copy.items()}})

    know = s["knowledge"]
    return {
        "lang": cat.lang,
        "identity": {
            "function_options": [*know["domains"], know["function_other"]],
            # Stable keys, identical in every language — the scorer reads
            # these. Only the labels below are translated.
            "seniority_options": list(know["seniority"]),
            "seniority_labels": dict(know["seniority"]),
        },
        "knowledge": {
            "starter_domains": list(know["domains"]),
            "depth_scale": [0, 5],
            "depth_anchors": list(know["depth_anchors"]),
            "depth_helpers": list(know["depth_helpers"]),
            "detail_prompt": know["detail_prompt"],
            # Keys stay English — they are stored values the scorer reads.
            # Only the labels are translated.
            "recency_options": list(_RECENCY_KEYS),
            "recency_labels": dict(know["recency"]),
            "confidence_scale": [0, 100],
        },
        "cognitive": _items(_COGNITIVE_IDS),
        "angle": _items(_ANGLE_IDS),
        # Order, tier and measured axes — both transports render from this
        # rather than restating it. Copied out, not aliased: a renderer
        # mutating the returned dict must not reach the module.
        "steps": steps,
        "modules": [
            {"key": m, **s["modules"][m]} for m in _MODULE_KEYS
        ],
        "ui": dict(s["ui"]),
    }


def _score_knowledge(knowledge: dict[str, Any]) -> dict[str, Any]:
    """Areas → per-area jargon + topic_interests + global jargon floor.

    Regression-to-mean shrink on self-rated depth (0.85·d + 0.15·2.5) before
    deriving jargon — corrects inflation without Dunning-Kruger quartile logic.
    """
    areas_out: list[dict[str, Any]] = []
    topic_interests: list[dict[str, Any]] = []
    adj_depths: list[float] = []

    for item in knowledge.get("areas") or []:
        if not isinstance(item, dict) or not item.get("area"):
            continue
        d = _num(item, "depth")
        d = 0.0 if d is None else max(0.0, min(5.0, d))
        adj = 0.85 * d + 0.15 * 2.5                      # shrink toward the mean
        adj_depths.append(adj)
        area = {
            "area": str(item["area"])[:80],
            "depth": round(adj, 2),
            "jargon": _to15(adj),                        # PER-AREA jargon (expertise-reversal)
            "recency": item.get("recency"),
            "detail": (str(item.get("detail"))[:280] if item.get("detail") else None),
        }
        areas_out.append(area)
        if adj >= 1.0:
            topic_interests.append({
                "topic": area["area"],
                "weight": round(adj / 5.0, 2),
                "detail": area["detail"],
            })

    weaknesses = [
        {"area": str(w["area"])[:80], "mode": (w.get("mode") or "explain")}
        for w in (knowledge.get("weaknesses") or [])
        if isinstance(w, dict) and w.get("area")
    ]

    # Over-claim heuristic: broad high self-rating + high stated confidence.
    confidence = _num(knowledge, "confidence")
    mean_raw = (sum(adj_depths) / len(adj_depths)) if adj_depths else None
    over_claim = bool(
        confidence is not None and confidence >= 80
        and mean_raw is not None and mean_raw >= 3.8
    )

    global_jargon = _to15(mean_raw) if mean_raw is not None else None
    return {
        "knowledge_areas": areas_out,
        "topic_interests": topic_interests,
        "weaknesses": weaknesses,
        "global_jargon": global_jargon,
        "over_claim": over_claim,
    }


def score_survey(response: dict[str, Any]) -> dict[str, Any]:
    """Score a v2 survey response into a lens (sliders + knowledge + identity).

    Expected ``response`` (all sections optional)::

        {
          "identity": {"display_name","profession","function","seniority"},
          "knowledge": {
            "areas": [{"area","depth":0-5,"recency","detail"}],
            "weaknesses": [{"area","mode":"explain"|"skip"}],
            "confidence": 0-100
          },
          "cognitive": {need_for_cognition,rational,experiential,visual_verbal,density,ambiguity},  # 0-5
          "angle": {construal,regulatory_focus}  # 0-5
        }
    """
    sliders: dict[str, int] = {}
    confidence: dict[str, float] = {}

    def set_axis(axis: str, value: int, conf: float) -> None:
        if axis not in SLIDER_NAMES:
            return
        sliders[axis] = max(1, min(5, value))
        confidence[axis] = conf

    identity = response.get("identity") or {}
    cog = response.get("cognitive") or {}
    ang = response.get("angle") or {}

    # 1) Role priors first (lowest precedence) — overridden by everything below.
    skey = _seniority_key(identity.get("seniority"))
    for axis, val in (_SENIORITY_PRIORS.get(skey or "", {})).items():
        set_axis(axis, val, CONF_PRIOR)

    # 2) Mapped/derived from a single answered trait (medium precedence).
    nfc = _num(cog, "need_for_cognition")
    if nfc is not None:
        nfc15 = _to15(nfc)
        set_axis("information_depth", _invert(nfc15), CONF_MAPPED)  # high NfC → detailed
        set_axis("lead_with", _invert(nfc15), CONF_MAPPED)          # high NfC → ok with context
    vv = _num(cog, "visual_verbal")
    if vv is not None:
        set_axis("format", _to15(vv), CONF_MAPPED)                  # visual → bullets/diagram
    amb = _num(cog, "ambiguity")
    if amb is not None:
        set_axis("decision_framing", _invert(_to15(amb)), CONF_MAPPED)  # low tolerance → recommendation
    cl = _num(ang, "construal")
    if cl is not None:
        set_axis("time_horizon", _to15(cl), CONF_MAPPED)            # abstract/why → strategic

    # 3) Direct trait sliders (highest precedence — answered as-is).
    #    numeracy + graph_literacy are asked under their registry ids, so they
    #    land here rather than needing a mapping in step 2.
    for axis in ("need_for_cognition", "rational", "experiential", "density",
                 "numeracy", "graph_literacy"):
        v = _num(cog, axis)
        if v is not None:
            set_axis(axis, _to15(v), CONF_DIRECT)
    if cl is not None:
        set_axis("construal", _to15(cl), CONF_DIRECT)
    rf = _num(ang, "regulatory_focus")
    if rf is not None:
        set_axis("regulatory_focus", _to15(rf), CONF_DIRECT)

    # 4) Knowledge → jargon (global floor) + per-area + topics + weaknesses.
    know = _score_knowledge(response.get("knowledge") or {})
    if know["global_jargon"] is not None:
        set_axis("jargon", know["global_jargon"],
                 CONF_OVERCLAIM if know["over_claim"] else CONF_DIRECT)

    # Provenance for every set axis.
    stamp = _now()
    provenance = {
        axis: {"value": sliders[axis], "confidence": confidence[axis],
               "source": _SOURCE, "updated_at": stamp}
        for axis in sliders
    }

    learning_style = None
    if vv is not None:
        learning_style = "visual" if _to15(vv) >= 4 else ("text" if _to15(vv) <= 2 else None)

    return {
        "sliders": sliders,
        "slider_confidence": confidence,
        "slider_provenance": provenance,
        "topic_interests": know["topic_interests"],
        "knowledge_areas": know["knowledge_areas"],
        "weaknesses": know["weaknesses"],
        "learning_style": learning_style,
        "over_claim_flags": 1 if know["over_claim"] else 0,
        "identity": {
            "profession": (identity.get("profession") or None),
            "function": (identity.get("function") or None),
            "seniority": (identity.get("seniority") or None),
        },
    }


def _merge_topics(existing: list, incoming: list) -> list:
    by_topic: dict[str, dict] = {}
    for t in existing:
        if isinstance(t, dict) and t.get("topic"):
            by_topic[t["topic"]] = t
    for t in incoming:
        if isinstance(t, dict) and t.get("topic"):
            by_topic[t["topic"]] = t
    return list(by_topic.values())


def survey_to_delta(
    response: dict[str, Any],
    existing_cognitive: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Package a scored v2 survey into an ``apply_profile_delta`` delta.

    apply_profile_delta REPLACES a key's whole value, so sliders / provenance /
    topic_interests are merged against ``existing_cognitive`` here and emitted
    complete. knowledge_areas / weaknesses REPLACE (latest survey wins).
    """
    scored = score_survey(response)
    existing = existing_cognitive or {}

    # Confidence-aware per-axis merge: a weak role PRIOR (0.5) must not clobber
    # a stronger existing value (user edit / prior survey). Override only when
    # the new signal is at least as confident. Legacy values without a recorded
    # confidence are treated as fairly strong (0.8) so priors can't overwrite them.
    merged_sliders = dict(existing.get("sliders") or {})
    merged_prov = dict(existing.get("slider_provenance") or {})
    for axis, val in scored["sliders"].items():
        new_conf = scored["slider_confidence"].get(axis, 0.0)
        old = merged_prov.get(axis) if isinstance(merged_prov.get(axis), dict) else None
        old_conf = old.get("confidence", 0.8) if old else None
        if old_conf is None or new_conf >= old_conf:
            merged_sliders[axis] = val
            merged_prov[axis] = scored["slider_provenance"][axis]

    cognitive: dict[str, Any] = {
        "sliders": merged_sliders,
        "slider_provenance": merged_prov,
    }
    topics = _merge_topics(list(existing.get("topic_interests") or []),
                           scored["topic_interests"])
    if topics:
        cognitive["topic_interests"] = topics
    if scored["knowledge_areas"]:
        cognitive["knowledge_areas"] = scored["knowledge_areas"]
    if scored["weaknesses"]:
        cognitive["weaknesses"] = scored["weaknesses"]
    if scored["learning_style"]:
        cognitive["learning_style"] = scored["learning_style"]
    for k in ("profession", "function", "seniority"):
        if scored["identity"].get(k):
            cognitive[k] = scored["identity"][k]

    return {"communication": {}, "cognitive": cognitive}


__all__ = [
    "UNIVERSAL_DOMAINS",
    "FUNCTION_OPTIONS",
    "SENIORITY_OPTIONS",
    "COGNITIVE_SLIDERS",
    "ANGLE_SLIDERS",
    "survey_form",
    "score_survey",
    "survey_to_delta",
]
