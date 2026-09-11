# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism COMPOSER — the layout-intelligence layer. Turns prose-heavy
#   rungs into scannable slides WITHOUT re-selecting components (the arranger owns
#   that): (A) editorial body-condensation to a per-rung inverse-density lede
#   [audit D1], (B) audience-layout resolution so the board KPI-band path fires on
#   the build_deck path [audit D4-p1], (C) shape-group hints the arranger never
#   saw, to unlock the quantitative family [audit D4-p2]. Deterministic + extractive
#   → zero hallucination risk; every entrypoint fails soft to a no-op (never regress).
# index:
#   BUDGET
#   def extract_lede
#   def _has_data_block
#   def condense_bodies
#   def resolve_audience_layout
#   def shape_group_hints
#   def compose_deck
# AGENT_HEADER_END -->
"""okuro·prism Composer — layout-intelligence over an assembled facet tree.

The arranger (`arranger.py`) already chooses and arranges the visual MODULES per
rung, and `layout.py` packs them into a grid. What no stage did until now:

* **Condense the prose.** Every rung led with a full-width `body` wall that often
  restated the very figures the arranger emitted as modules below it
  (audit D1). The Composer rewrites each shallow rung's body to a short
  inverted-pyramid LEDE — *extractively* (first whole sentences up to a per-rung
  budget), so it can never invent. The deepest reading floor (`expert`) keeps its
  full prose; a prose-only rung (no data module to carry the substance) is left
  untouched. Visual density thus grows INVERSE to depth (the depth-model theory).

* **Light the board treatment.** `doc["audience_layout"]` was only ever set on the
  Generate path; the build_deck path left it `None`, so the KPI-band-first
  promotion in `layout.py` was dead for a deck explicitly built for a CEO
  (audit D4). The Composer resolves it from the recipient (or, for a GROUP, the
  least-technical FLOOR member — the deck's existing group philosophy).

* **Unlock the quantitative family.** `shapes.group_shapes` (N same-shape claims →
  ONE multi-item chart) was computed elsewhere but never handed to the arranger.
  The Composer surfaces it as a per-facet hint (audit D4-p2).

Design contract (mirrors `layout.py` / `shapes.py`): the Composer can only ever
IMPROVE a rung, never destroy real data, and every public entrypoint is a no-op
on failure — if the Composer yields nothing, the deck builds exactly as before.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from okuro.prism.shapes import block_datapoints, group_shapes

logger = logging.getLogger("okuro.prism.composer")

from okuro.prism.rungs import RUNGS as _RUNGS

# Per-rung prose budget in characters (inverse-density: shallow = tight lede,
# deep = generous). `expert` is the L3 reading floor — the ONLY rung allowed full
# prose — so it is never condensed (absent from this map). Budgets chosen against
# the audit's measured walls (L1 338-606, L2 233-776, L3 541-1469):
# each budget admits ~1 (L1), ~1-2 (L2), ~2-3 (L3) whole sentences.
BUDGET: dict[str, int] = {"L1": 160, "L2": 300, "L3": 520}

# A block only "carries the data" (so the prose above it is redundant and safe to
# trim) when it renders at least this many discrete datapoints. A lone statement /
# callout / decorative visualizer (0-1 datapoints) does NOT count — so a
# prose-only rung is never gutted.
_MIN_DATA_DATAPOINTS = 2

# Sentence boundary: a .!? followed by whitespace and a capital / quote / digit —
# but NOT when the period sits between digits (a decimal like `0.92`) or right
# after a lone capital (an initial like `J.`). Extractive only; never rewrites.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")
_DECIMAL = re.compile(r"\d\.\d")


def extract_lede(body: str, budget: int) -> str:
    """Return the inverted-pyramid LEDE of ``body``: the first whole sentence(s)
    up to ``budget`` characters. Purely EXTRACTIVE — it selects a prefix of the
    existing text and never rewrites a word, so it cannot introduce a fact.

    Always returns at least the first whole sentence (even if that one sentence
    alone exceeds the budget — a single true sentence beats a truncated one).
    Collapses internal whitespace/newlines in the kept prefix so a monospace wall
    becomes a clean lede line. Returns ``body`` unchanged when it is already at or
    under budget, or when it has no extractable sentence.
    """
    text = (body or "").strip()
    if not text:
        return body
    # Split into sentences, protecting decimals (0.92) from the boundary regex.
    guarded = _DECIMAL.sub(lambda m: m.group(0).replace(".", "\x00"), text)
    parts = [p.replace("\x00", ".").strip() for p in _SENTENCE_END.split(guarded)]
    sentences = [s for s in parts if s]
    if not sentences:
        return body

    kept: list[str] = []
    total = 0
    for s in sentences:
        add = len(s) + (1 if kept else 0)  # +1 for the joining space
        if kept and total + add > budget:
            break
        kept.append(s)
        total += add
    if not kept:                     # first sentence alone > budget → keep it whole
        kept = [sentences[0]]
    lede = " ".join(kept)
    lede = re.sub(r"\s+", " ", lede).strip()
    # Never return something LONGER than the source (the whitespace collapse can
    # only shrink it, but guard anyway) and never empty.
    return lede if lede and len(lede) < len(body) else (body if len(lede) >= len(body) else lede)


def _has_data_block(blocks: list[dict[str, Any]] | None) -> bool:
    """True when the rung carries a substantive DATA module — one whose datapoints
    the shed prose would otherwise duplicate. A lone statement/callout/visualizer
    (0-1 datapoints) is not enough."""
    for b in blocks or []:
        if isinstance(b, dict) and block_datapoints(b) >= _MIN_DATA_DATAPOINTS:
            return True
    return False


def condense_bodies(facets: dict[str, Any] | None) -> dict[str, int]:
    """Condense every over-budget shallow-rung ``body`` to its lede, in place.

    Faithfulness rule (see module docstring): trim a rung only when the substance
    survives elsewhere —
      * ``L1``/``L2`` — a DEEPER present rung re-covers it (superset ladder),
        OR the rung has a substantive data block;
      * ``working`` — only when the rung has a substantive data block (it may be
        the audience ceiling with nothing deeper);
      * ``expert`` — never (the L3 reading floor keeps full prose).

    Returns ``{"condensed": n_rungs, "chars_removed": n}``. Idempotent: a body
    already at/under budget (e.g. from a prior run) is left untouched.
    """
    condensed = 0
    removed = 0
    for facet in (facets or {}).values():
        rungs = (facet or {}).get("rungs") if isinstance(facet, dict) else None
        if not isinstance(rungs, dict):
            continue
        present = [r for r in _RUNGS if isinstance(rungs.get(r), dict)]
        present_set = set(present)
        for idx, r in enumerate(present):
            if r == "L4":
                continue
            rc = rungs[r]
            body = rc.get("body")
            if not isinstance(body, str) or not body.strip():
                continue
            budget = BUDGET.get(r)
            if budget is None or len(body) <= budget:
                continue
            deeper_present = any(dr in present_set for dr in _RUNGS[idx + 1:])
            has_data = _has_data_block(rc.get("blocks"))
            if r in ("L1", "L2"):
                safe = deeper_present or has_data
            else:  # working — must carry its own data
                safe = has_data
            if not safe:
                continue
            lede = extract_lede(body, budget)
            if lede and len(lede) < len(body):
                removed += len(body) - len(lede)
                rc["body"] = lede
                condensed += 1
    return {"condensed": condensed, "chars_removed": removed}


def resolve_audience_layout(
    person_id: Optional[str] = None,
    target_group_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve the deck's ``audience_layout`` knobs (`band_lead`, `density`) from
    the recipient's cognition — the fix for the build_deck path never setting it.

    * single ``person_id`` → ``layout_profile`` of their cognitive profile.
    * ``target_group_id`` → the FLOOR member (least-technical, min jargon) sets the
      surface treatment — the deck's existing group-resolution philosophy
      (`audience.group_policy`), not an average.
    * neither / <2 profiled members / any failure → ``{}`` (no knobs → unchanged).
    """
    try:
        from okuro.peer.cognitive_profile import cognitive_profile_for_llm
        from okuro.prism.audience import aggregate_members, layout_profile

        if person_id:
            return layout_profile(cognitive_profile_for_llm(person_id) or {})
        if target_group_id:
            from okuro.prism.personas import resolve_member_sliders

            members = [m for m in resolve_member_sliders(target_group_id)
                       if isinstance(m.get("sliders"), dict) and m["sliders"]]
            if len(members) >= 2:
                # Was: pick the single min-jargon MEMBER and use their whole
                # vector — a third group semantics, and one that let an
                # unrelated axis of one person decide the whole layout. Now
                # the canonical per-axis policy: floor where flooring is
                # safe, mean elsewhere.
                merged = aggregate_members([m["sliders"] for m in members])
                return layout_profile({"sliders": merged["sliders"]})
    except Exception as exc:  # noqa: BLE001 — layout resolution never blocks a build
        logger.warning("resolve_audience_layout failed: %s", exc)
    return {}


def shape_group_hints(claims: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The chart-unlock signal for one facet: clusters of ≥2 claims sharing a
    groupable data shape (`shapes.group_shapes`) that should feed ONE multi-item
    component instead of N stats. A thin, fail-soft wrapper — the arranger sees
    this as a CANDIDATE, never a mandate. ``[]`` on empty input or any failure."""
    try:
        return group_shapes(claims)
    except Exception as exc:  # noqa: BLE001 — a hint is optional; never block arrange
        logger.warning("shape_group_hints failed: %s", exc)
        return []


def compose_deck(
    facets: dict[str, Any] | None,
    *,
    person_id: Optional[str] = None,
    target_group_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run the Composer over an assembled facet tree (in place) just before save.

    Condenses shallow-rung prose walls (A) and resolves the deck's audience-layout
    knobs (B). Returns ``{"audience_layout": dict, "condensed": int,
    "chars_removed": int}``. Fail-soft: any exception yields a no-op result so the
    deck saves exactly as it was assembled. (C) — the shape hints — is applied
    earlier, at arrange time; see `shape_group_hints`.
    """
    try:
        stats = condense_bodies(facets)
    except Exception as exc:  # noqa: BLE001 — condensation never blocks a build
        logger.warning("compose_deck: condensation skipped: %s", exc)
        stats = {"condensed": 0, "chars_removed": 0}
    audience_layout = resolve_audience_layout(person_id, target_group_id)
    return {
        "audience_layout": audience_layout,
        "condensed": stats.get("condensed", 0),
        "chars_removed": stats.get("chars_removed", 0),
    }


__all__ = [
    "BUDGET",
    "extract_lede",
    "condense_bodies",
    "resolve_audience_layout",
    "shape_group_hints",
    "compose_deck",
]
