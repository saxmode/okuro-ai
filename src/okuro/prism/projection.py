# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism STORY PROJECTION — select & reframe the audience-NEUTRAL
#   master story's claims for ONE recipient kind, WITHOUT rewriting the facts.
#   The prism-side mirror of resonance.projection.project_pco: same policy
#   (prism.audience constants), same semantics, over the story dict shape.
# index: project_story | _serves | _directive_for
# AGENT_HEADER_END -->
"""Per-recipient projection over the master story (Stage 2 of the redesign).

``story.build_story`` produces ONE audience-neutral, depth-complete story. This
module PROJECTS it onto a single audience ``kind`` — it SELECTS the claims that
serve that reader (dropping the rest) and attaches a per-claim phrasing
``directive`` (gain/loss frame, why/how altitude). It never rewrites a claim:
truth ⊥ audience ⊥ brand — audience is a non-destructive overlay.

Mirrors ``resonance.projection.project_pco`` deliberately (so a future
convergence is trivial) and shares its exact policy via ``prism.audience``
(``CONCISE_KINDS`` / ``KIND_CONSTRUAL`` / ``KIND_FRAME``) — one source of truth
(DP10). The differences are only structural: a story's claims live in a dict
keyed by id and its beats are a topic TREE, not a flat beat list.

Pure + total:
  * ``kind=None`` → the story object is returned unchanged (the neutral path).
  * The input story and every claim dict it holds are never mutated — the same
    story can be projected for many recipients.
  * A LEAF topic that keeps no claim is dropped (mirrors project_pco dropping an
    empty beat); a topic that PARENTS another is always kept so the tree is
    never orphaned. Untagged claims are universal, so an un-enriched story
    projects to itself for every kind (additive).
"""

from __future__ import annotations

from typing import Any, Optional

from okuro.prism.audience import CONCISE_KINDS, KIND_CONSTRUAL, KIND_FRAME
from okuro.prism.shapes import group_shapes


def _serves(claim: dict[str, Any], kind: str) -> bool:
    """A claim serves a kind when it is universal (no ``audiences`` tag) or lists
    it. An un-enriched claim (no tag) is universal — projection stays additive."""
    audiences = claim.get("audiences") or []
    return not audiences or kind in audiences


def _directive_for(claim: dict[str, Any], kind: str) -> Optional[str]:
    """The phrasing directive for this claim under this audience kind — the SAME
    construction as ``resonance.projection._directive_for``, over a claim dict.

    Emits only when the claim carries the relevant reframe material AND it
    differs from how the claim is already stated, so untagged claims stay quiet
    (additive) and no redundant instruction is spent.
    """
    parts: list[str] = []
    frame_key = KIND_FRAME.get(kind)
    frame = claim.get("frame")
    if frame_key and isinstance(frame, dict) and frame.get(frame_key):
        parts.append(f"frame as {frame_key}: {frame[frame_key]}")
    target = KIND_CONSTRUAL.get(kind)
    construal = claim.get("construal")
    if target and construal and construal != target:
        altitude = "outcome / strategic" if target == "why" else "mechanism / how"
        parts.append(f"phrase at the {target} altitude ({altitude}), not {construal}")
    return "; ".join(parts) or None


def project_story(
    story: dict[str, Any], kind: Optional[str], *, concise: Optional[bool] = None
) -> dict[str, Any]:
    """Project ``story`` onto one audience ``kind`` → a new, audience-fit story.

    * ``kind=None`` → the story is returned unchanged (no profiled audience).
    * SELECT: keep claims that serve ``kind``; for concise readers (board/exec)
      also drop ``weight="nice"`` claims so the claim SET shrinks, not just the
      wording.
    * REFRAME: attach a per-claim ``directive`` (gain/loss, why/how); never edits
      the claim's ``statement``.
    * Topics keep only their surviving claims; ``value_core_ids`` are filtered and
      ``shape_groups`` recomputed over the kept claims. A leaf topic that keeps
      nothing is dropped; a parent topic is preserved (never orphan a child).
    * ``measured`` / ``unproven`` are filtered to the kept set.

    Returns a fresh story dict — the input and its claim dicts are never mutated.
    """
    if not kind:
        return story
    if concise is None:
        concise = kind in CONCISE_KINDS

    src_claims: dict[str, Any] = story.get("claims") or {}
    kept: dict[str, Any] = {}
    for cid, claim in src_claims.items():
        if not isinstance(claim, dict):
            continue
        if not _serves(claim, kind):
            continue
        if concise and claim.get("weight") == "nice":
            continue
        kept[cid] = {**claim, "directive": _directive_for(claim, kind)}

    topics_in: list[dict[str, Any]] = story.get("topics") or []
    parent_ids = {t.get("parent") for t in topics_in if t.get("parent")}
    new_topics: list[dict[str, Any]] = []
    for t in topics_in:
        cids = [c for c in (t.get("claim_ids") or []) if c in kept]
        # Drop only a LEAF topic that keeps nothing — a topic that parents another
        # stays so the tree is never orphaned (project_pco drops empty flat beats;
        # a story's beats form a tree, so the drop is guarded).
        if not cids and t.get("id") not in parent_ids:
            continue
        tclaims = [kept[c] for c in cids]
        new_topics.append({
            **t,
            "claim_ids": cids,
            "value_core_ids": [c for c in (t.get("value_core_ids") or []) if c in kept],
            "shape_groups": group_shapes(tclaims),
        })

    kept_ids = set(kept)
    return {
        **story,
        "claims": kept,
        "topics": new_topics,
        "measured": [c for c in (story.get("measured") or []) if c in kept_ids],
        "unproven": [c for c in (story.get("unproven") or []) if c in kept_ids],
    }


__all__ = ["project_story"]
