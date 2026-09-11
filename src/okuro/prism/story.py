# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism Master Story — assemble the audience-NEUTRAL, depth-complete
#   canonical story (throughline + topic tree + per-topic value-core + shape-groups
#   + measured/unproven split) that must exist BEFORE any slide. Stage 1 spine of
#   the prism redesign: audience is a later projection over this, never a rewrite
#   of it (truth ⊥ audience ⊥ brand).
# index: build_story | enrich_topics | partition_evidence | _value_core_ids
# AGENT_HEADER_END -->
"""okuro·prism Master Story assembly.

The redesign's first-class artifact: one canonical story, audience-neutral and
depth-complete, that every downstream stage PROJECTS from (a per-recipient hook,
a depth ladder, a component choice) instead of re-deriving truth per audience.

It COMPOSES the existing stages rather than duplicating them:
  * ``distiller.distill``  → cited claims, each tagged with a data ``shape``.
  * ``architect.structure`` (audience=None) → an audience-neutral topic tree + arc.
  * deterministic enrichment (this module) → per-topic value-core (the deep
    "why it matters" claims), shape-groups (``shapes.group_shapes`` — the chart
    opportunities), and a measured-vs-unproven split.

The LLM does the two irreducibly-semantic steps (extraction, structuring); the
enrichment is pure and offline-testable. ``build_story`` returns the story object;
persistence and wiring into ``build_deck`` are a later integration step — this
module only ASSEMBLES the spine.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from okuro.prism.shapes import group_shapes

logger = logging.getLogger("okuro.prism.story")

# Granularity tiers that carry the DEEP substance of a topic — the "value core"
# (the L3 payload: specific figures, mechanisms, caveats). Shallow topics honestly
# have none; that is scarcity, not a licence to pad.
_DEEP_TIERS = frozenset({"detail", "edge"})


def _value_core_ids(topic_claims: list[dict[str, Any]]) -> list[str]:
    """The topic's deep claims (granularity detail/edge) — the substance the expert
    rung carries and the hook must ultimately be worth. Empty when the topic is
    genuinely shallow (no deep claims), never padded."""
    return [
        c["id"] for c in topic_claims
        if c.get("id") and c.get("granularity") in _DEEP_TIERS
    ]


def partition_evidence(claims: list[dict[str, Any]] | None) -> tuple[list[str], list[str]]:
    """Split claim ids into (measured, unproven). A claim is UNPROVEN when its
    confidence is 'low' OR its evidence did not trace verbatim to the source
    (grounded is explicitly False) — the things a critic must flag before a deck
    asserts them. Everything else is measured. Missing signals default to measured
    (no penalty without evidence of weakness)."""
    measured: list[str] = []
    unproven: list[str] = []
    for c in claims or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        weak = c.get("confidence") == "low" or c.get("grounded") is False
        (unproven if weak else measured).append(c["id"])
    return measured, unproven


def enrich_topics(
    topics: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach deterministic depth signals to each topic — its value-core claim ids
    and its shape-groups (candidate multi-item visuals) — computed from ONLY that
    topic's own claims. Non-destructive: returns new topic dicts, original fields
    preserved."""
    by_id = {c["id"]: c for c in (claims or []) if isinstance(c, dict) and c.get("id")}
    out: list[dict[str, Any]] = []
    for t in topics or []:
        if not isinstance(t, dict):
            continue
        tclaims = [by_id[cid] for cid in (t.get("claim_ids") or []) if cid in by_id]
        out.append({
            **t,
            "value_core_ids": _value_core_ids(tclaims),
            "shape_groups": group_shapes(tclaims),
        })
    return out


def build_story(
    source_text: Optional[str] = None,
    *,
    claims: Optional[list[dict[str, Any]]] = None,
    kind: str = "research",
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the audience-neutral master story from a root document (or from a
    pre-distilled claim set).

    Provide EITHER ``source_text`` (distilled here) OR ``claims`` (already
    distilled, each ideally carrying a ``shape``). Returns::

        {"throughline": str,              # the narrative arc (audience-neutral)
         "topics": [{id, title, order, parent, claim_ids,
                     value_core_ids, shape_groups}],
         "claims": {claim_id: claim},     # ground truth, by id
         "measured": [claim_id], "unproven": [claim_id],
         "coverage": float, "unmapped": [claim_id]}

    Raises when neither input is given, or when no claims/topics can be produced.
    """
    if claims is None:
        if not (source_text or "").strip():
            raise ValueError("build_story requires source_text or claims")
        from okuro.prism.distiller import distill
        claims = distill(source_text, kind=kind, provider=provider).get("claims") or []
    claims = [c for c in (claims or []) if isinstance(c, dict) and c.get("id")
              and (c.get("statement") or "").strip()]
    if not claims:
        raise RuntimeError("build_story: no usable claims")

    from okuro.prism.architect import structure
    tree = structure(claims, audience=None, provider=provider)  # audience-NEUTRAL spine
    if not tree.get("topics"):
        raise RuntimeError("build_story: architect produced no topics")

    measured, unproven = partition_evidence(claims)
    return {
        "throughline": tree.get("arc", ""),
        "topics": enrich_topics(tree["topics"], claims),
        "claims": {c["id"]: c for c in claims},
        "measured": measured,
        "unproven": unproven,
        "coverage": tree.get("coverage", 0.0),
        "unmapped": tree.get("unmapped", []),
    }


__all__ = ["build_story", "enrich_topics", "partition_evidence"]
