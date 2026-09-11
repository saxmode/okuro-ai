# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.outline — slider-driven outline_for_recipient.
#   Maps SourceDocument + cognitive_profile -> Outline (typed structure
#   the channel renderers consume). The ONLY LLM hop is delegated to
#   peer.translate.person_translate for per-section text adaptation.
# index: imports | dataclass Outline | def outline_for_recipient |
#   def _section_count_for_depth | def _maybe_lead_with_tldr |
#   def _decision_framing_lead | def _format_for_recipient | def _flatten
# AGENT_HEADER_END -->
"""Slider-driven outline construction.

The cognitive_profile_for_llm() call returns the recipient's anonymized
sliders; the outline stage uses them deterministically to size and
shape the structure (slide count, TLDR-first vs context-first,
recommendation-first vs options-first, bullets vs prose). Text
adaptation per section is delegated to peer.translate (the only LLM
hop in the pipeline — HR-C2).

Sliders consumed (1-5 scale, see peer/cognitive_profile.py):
    information_depth -> section/slide count (1=detailed -> 15, 5=concept -> 3)
    decision_framing  -> recommendation-first vs options-first
    lead_with         -> TLDR section presence
    jargon            -> caller hint (translate prompt uses this directly)
    pace              -> bullet density per section

``information_depth`` is the canonical axis name. This module read
``info_depth`` — a key that has never existed in SLIDER_NAMES — from the
day it shipped, so every delivery ever rendered used the same default
7-section structure and recorded a fabricated 3 in its snapshot. The
count map was ALSO inverted against the axis labels, so fixing the key
alone would have handed concept-skimmers 15 sections. Both are corrected
here, together, behind ``people.strict`` (peer/flags.py) — the OFF path
reproduces the old behaviour exactly, for rollback.

Format preferences (`format_preferences` list) drive bullets vs prose
at the block level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Outline shape
# ----------------------------------------------------------------------


@dataclass
class OutlineBlock:
    """One renderable block inside a section."""

    type: str  # "bullets" | "prose" | "table" | "kpi" | "quote" | "callout" | "cta"
    content: str = ""
    data: list[Any] = field(default_factory=list)


@dataclass
class OutlineSection:
    """One slide / page / spoken section."""

    id: str
    heading: str
    blocks: list[OutlineBlock] = field(default_factory=list)


@dataclass
class Outline:
    """Channel-agnostic structure renderable as md / slides / pdf / html / audio."""

    title: str
    subtitle: str = ""
    tldr: str = ""
    sections: list[OutlineSection] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    audience_metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "tldr": self.tldr,
            "sections": [
                {
                    "id": s.id,
                    "heading": s.heading,
                    "blocks": [
                        {"type": b.type, "content": b.content, "data": b.data}
                        for b in s.blocks
                    ],
                }
                for s in self.sections
            ],
            "next_steps": list(self.next_steps),
            "audience_metadata": dict(self.audience_metadata),
        }


# ----------------------------------------------------------------------
# Slider helpers
# ----------------------------------------------------------------------


def _slider(profile: dict, name: str, default: int = 3) -> int:
    """Read a slider value from the cognitive profile, with backstop default.

    ``profile`` is the output of ``cognitive_profile_for_llm`` which is
    PII-firewalled. Sliders live under ``profile["sliders"]``; absent
    sliders return ``default`` so we never crash on a sparse profile.
    """
    sliders = profile.get("sliders") if isinstance(profile, dict) else None
    if not isinstance(sliders, dict):
        return default
    val = sliders.get(name)
    if isinstance(val, (int, float)) and 1 <= val <= 5:
        return int(val)
    return default


# information_depth: 1 = "detailed", 5 = "high-level concept"
# (cognitive_profile.SLIDER_LABELS). A detail-seeker wants the structure kept;
# a concept-skimmer wants it compressed. Floor 3, ceiling 15.
_DEPTH_TARGETS = {1: 15, 2: 10, 3: 7, 4: 5, 5: 3}

# The pre-fix map, kept ONLY to reproduce shipped behaviour when
# people.strict is off. It reads the axis backwards.
_DEPTH_TARGETS_LEGACY = {1: 3, 2: 5, 3: 7, 4: 10, 5: 15}


def _information_depth(profile: dict) -> int:
    """Read the depth axis under its canonical name.

    Behind ``people.strict``: the lenient path keeps reading the key that
    does not exist, which means the default 3 for every recipient — the
    exact behaviour every delivery shipped with until this fix.
    """
    from okuro.peer.flags import people_strict_enabled, warn_lenient

    if people_strict_enabled():
        return _slider(profile, "information_depth", default=3)
    warn_lenient(
        "delivery.outline.information_depth",
        "reading the non-existent `info_depth` key — the depth lever stays dead",
    )
    return _slider(profile, "info_depth", default=3)  # axis-literal: legacy


def _profile_version() -> str:
    """A stable fingerprint of the axis vocabulary this render used.

    Change a slider and last month's delivery becomes unexplainable — the
    system keeps no record of the profile that shaped any output. This is
    the smallest honest step: not the person's values (those belong in a
    history table), but WHICH VOCABULARY was in force, so a future reader
    knows whether the recorded axes still mean what they meant.

    NOT builtins.hash(): str hashing is salted per process (PYTHONHASHSEED),
    so that version would differ between two runs of the same code — the
    opposite of a fingerprint.
    """
    import hashlib

    from okuro.peer.axis_registry import axis_ids

    ids = axis_ids()
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()[:8]
    return f"axes-{len(ids)}-{digest}"


def _section_count_for_depth(information_depth: int, source_count: int) -> int:
    """Map information_depth (1-5) + source section count to a target count.

    Floor 3, ceiling 15. More depth wanted -> retain or split; less depth
    -> compress (merge sibling sections). Source count caps the upper
    bound — we never invent sections that don't exist.
    """
    from okuro.peer.flags import people_strict_enabled, warn_lenient

    if people_strict_enabled():
        targets = _DEPTH_TARGETS
    else:
        warn_lenient(
            "delivery.outline.depth_polarity",
            "section count runs inverted against the axis labels",
        )
        targets = _DEPTH_TARGETS_LEGACY
    target = targets.get(information_depth, 7)
    if source_count <= 0:
        return 0
    return max(1, min(target, source_count))


def _format_for_recipient(profile: dict) -> str:
    """Return preferred block type for body content: 'bullets' or 'prose'.

    Reads ``format_preferences`` (list of strings) from the firewalled
    profile. 'bullets' / 'tables' / 'lists' -> bullets. 'prose' /
    'paragraphs' -> prose. Default 'bullets' (matches okuro's own pref).
    """
    prefs = profile.get("format_preferences") if isinstance(profile, dict) else None
    if not isinstance(prefs, list):
        return "bullets"
    bag = " ".join(str(p).lower() for p in prefs)
    if "prose" in bag or "paragraph" in bag or "narrative" in bag:
        return "prose"
    return "bullets"


def _flatten(sections: list, max_depth: int = 2) -> list:
    """Walk the SourceDocument section tree breadth-first, capped at depth."""
    flat = []

    def walk(nodes, depth):
        for n in nodes:
            flat.append(n)
            if depth < max_depth:
                walk(getattr(n, "children", []), depth + 1)

    walk(sections, 1)
    return flat


def _maybe_tldr(profile: dict, source_intro: str, source_summary: str) -> str:
    """Build a TLDR string when the recipient prefers answer-first."""
    lead_with = _slider(profile, "lead_with", default=4)
    # 4-5 = strong preference for answer/TLDR upfront
    if lead_with < 4:
        return ""
    candidates = [source_summary.strip(), source_intro.strip()]
    for c in candidates:
        if c:
            # Cap TLDR at ~280 chars — single-tweet density.
            return c[:280]
    return ""


def _decision_framing_blocks(
    profile: dict,
    source_section,
    fmt: str,
) -> list[OutlineBlock]:
    """Apply decision-framing slider to a section's body.

    decision_framing 4-5 = recommendation-first (lead with the call,
    evidence in support). 1-2 = options-first (lay out alternatives,
    let the recipient choose). 3 = balanced.
    """
    body = (source_section.body or "").strip()
    if not body:
        return []

    framing = _slider(profile, "decision_framing", default=3)

    if fmt == "bullets":
        # Cheap heuristic: lines that already look like bullets get reused;
        # otherwise we synthesize one bullet per non-empty line/paragraph.
        bullets = _to_bullets(body)
        if framing >= 4 and len(bullets) >= 2:
            # Recommendation-first: keep order (assume source already
            # leads with the recommendation; outline preserves it).
            return [OutlineBlock(type="bullets", data=bullets)]
        if framing <= 2 and len(bullets) >= 2:
            # Options-first: prefix with a "Options:" callout if absent.
            return [
                OutlineBlock(type="callout", content="Options"),
                OutlineBlock(type="bullets", data=bullets),
            ]
        return [OutlineBlock(type="bullets", data=bullets)]
    return [OutlineBlock(type="prose", content=body)]


def _to_bullets(text: str) -> list[str]:
    """Normalise prose into a flat bullet list. Best-effort."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("- ", "* ", "+ ")):
            out.append(s[2:].strip())
        elif s[:2].isdigit() and s[2:3] in (". ", ") "):
            out.append(s[3:].strip())
        else:
            # Treat as one bullet per paragraph break — collapse multi-line
            # paragraphs to one bullet.
            if out and not s.startswith("#"):
                out[-1] = (out[-1] + " " + s).strip()
            else:
                out.append(s)
    return [b for b in out if b]


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def outline_for_recipient(
    source,                  # SourceDocument
    cognitive_profile: dict,
    *,
    translate: bool = True,
    person_id: str | None = None,
    context: str | None = None,
) -> Outline:
    """Build an Outline from a SourceDocument shaped for one recipient.

    When ``translate=True`` and ``person_id`` is set, each section's
    rendered body is run through ``peer.translate.person_translate``
    so vocabulary and register match the recipient. When ``translate``
    is False (tests / dry runs), the source text passes through
    unmodified — sliders still drive structure.

    Returns an Outline, never raises. A failed translate falls back to
    the source text per HR-C3 (best-effort).
    """
    information_depth = _information_depth(cognitive_profile)
    fmt = _format_for_recipient(cognitive_profile)

    flat = _flatten(source.sections, max_depth=2)
    target = _section_count_for_depth(information_depth, len(flat))
    selected = flat[:target] if target else flat

    sections: list[OutlineSection] = []
    for i, src_section in enumerate(selected, start=1):
        blocks = _decision_framing_blocks(cognitive_profile, src_section, fmt)
        if translate and person_id and blocks:
            blocks = _translate_blocks(blocks, person_id, context)
        sections.append(OutlineSection(
            id=f"s{i}",
            heading=src_section.heading,
            blocks=blocks,
        ))

    tldr = _maybe_tldr(cognitive_profile, source.intro, source.summary)
    if translate and person_id and tldr:
        translated = _translate_text(tldr, person_id, context)
        if translated:
            tldr = translated

    title = source.title or "Untitled"
    subtitle = (source.summary or "")[:160]

    # The snapshot must name the axis it actually read. Under the legacy
    # path it read `info_depth`, so it keeps saying so — a snapshot that
    # renames the field without changing the value would be a second lie
    # on top of the first.
    from okuro.peer.flags import people_strict_enabled

    depth_key = "information_depth" if people_strict_enabled() else "info_depth"

    audience_metadata = {
        depth_key: information_depth,
        # Which axis vocabulary shaped this render. deliveries.outline is a
        # JSON blob, so this needs no migration — and without it a delivery
        # cannot be explained after the registry moves under it. Readers
        # must tolerate its absence: every delivery made before now lacks it.
        "profile_version": _profile_version(),
        "format": fmt,
        "decision_framing": _slider(cognitive_profile, "decision_framing", default=3),
        "lead_with": _slider(cognitive_profile, "lead_with", default=4),
        "jargon": _slider(cognitive_profile, "jargon", default=3),
        "pace": _slider(cognitive_profile, "pace", default=3),
        "translated": bool(translate and person_id),
        "person_id": person_id,
    }

    return Outline(
        title=title,
        subtitle=subtitle,
        tldr=tldr,
        sections=sections,
        next_steps=[],  # populated by channel renderers from "Next steps" headings
        audience_metadata=audience_metadata,
    )


# ----------------------------------------------------------------------
# Translation helpers (the only LLM hop)
# ----------------------------------------------------------------------


def _translate_text(text: str, person_id: str, context: str | None) -> str | None:
    """Best-effort per-recipient translation. Returns None on failure."""
    if not text or not text.strip():
        return None
    try:
        from okuro.peer.translate import person_translate
        result = person_translate(person_id, text, context=context)
    except Exception as exc:
        log.debug("person_translate raised: %s", exc)
        return None
    if not isinstance(result, dict):
        return None
    if not result.get("success"):
        return None
    out = (result.get("translated") or "").strip()
    return out or None


def _translate_blocks(
    blocks: list[OutlineBlock],
    person_id: str,
    context: str | None,
) -> list[OutlineBlock]:
    """Translate each block's text fields. Falls back to source on failure."""
    out: list[OutlineBlock] = []
    for b in blocks:
        if b.type == "bullets" and b.data:
            joined = "\n".join(f"- {line}" for line in b.data if line)
            translated = _translate_text(joined, person_id, context)
            if translated:
                bullets = [
                    line.lstrip("-* ").strip()
                    for line in translated.splitlines()
                    if line.strip() and line.strip()[:1] in "-*"
                ] or [translated.strip()]
                out.append(OutlineBlock(type="bullets", data=bullets))
                continue
        if b.type == "prose" and b.content:
            translated = _translate_text(b.content, person_id, context)
            if translated:
                out.append(OutlineBlock(type="prose", content=translated))
                continue
        out.append(b)
    return out
