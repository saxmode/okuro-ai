# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler STAGE 4 (outline) — group the KEPT claims into
#   as many topics as content x audience warrant (NO fixed cap) with a narrative
#   arc, such that every kept claim lands in EXACTLY
#   one topic (a strict partition — the load-bearing invariant author/plan rely
#   on). The LLM proposes the grouping; code repairs the partition (no orphans,
#   no duplicates) so a downstream stage can trust it absolutely.
# index: outline | _SYSTEM | _repair_partition
# AGENT_HEADER_END -->
"""Stage 4 — outline: kept claims -> topics + arc.

The partition invariant (each kept claim in exactly one topic) is enforced in
code, not trusted from the model: an orphaned claim is appended to its best-guess
topic (most emphasis-similar, else the last topic), and a claim assigned twice is
kept in its first topic only. Topic COUNT is content x audience-driven (no cap):
a rich reference yields many topics, a short source few; empty topics are dropped.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.compiler.ir import Outline, Projection, Topic

logger = logging.getLogger(__name__)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["topics", "arc"],
    "additionalProperties": False,
    "properties": {
        "arc": {"type": "string"},
        "topics": {
            # NO maxItems: topic count is the LLM's job — driven by content x
            # audience, never a fixed ceiling. A 46k-word reference must be free to
            # produce many topics; a memo, few. Capping at 7 crushed a book to 7.
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["title", "claim_ids"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string", "minLength": 1},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

_SYSTEM = """You are the OUTLINE stage of the prism deck compiler. Group the kept \
claims into TOPICS that tell one coherent story, and state the ARC (the \
throughline that connects the topics in order — e.g. problem -> options -> \
recommendation -> ask).

Choose the NUMBER of topics from the CONTENT and the AUDIENCE — never a fixed \
count. Each distinct theme is its own topic: a short source yields a few topics, \
a long, rich reference yields many. Do NOT compress unrelated themes together to \
hit a small number, and do NOT split one theme to pad. Right-size for the reader.

Rules:
- Every claim id must appear in EXACTLY ONE topic. No claim left out, none in two.
- As many topics as the material genuinely needs; order them so the arc reads \
start to finish.
- Give each topic a short, specific title (not "Introduction"/"Overview").
Return ONLY {"topics": [{"id","title","claim_ids"}], "arc": "..."}."""


def _repair_partition(
    topics: list[Topic], kept_ids: list[str], emphasis: dict[str, float]
) -> list[Topic]:
    """Guarantee a strict partition of ``kept_ids`` across ``topics``."""
    kept = set(kept_ids)
    assigned: set[str] = set()
    for t in topics:
        deduped = []
        for cid in t.claim_ids:
            if cid in kept and cid not in assigned:
                deduped.append(cid)
                assigned.add(cid)
        t.claim_ids = deduped
    # Orphans: kept claims no topic took -> append to the least-full topic.
    orphans = [cid for cid in kept_ids if cid not in assigned]
    if orphans:
        if not topics:
            topics = [Topic(id="t1", title="Key points", claim_ids=[])]
        for cid in orphans:
            target = min(topics, key=lambda t: len(t.claim_ids))
            target.claim_ids.append(cid)
        logger.info("outline: reassigned %d orphan claim(s)", len(orphans))
    # Drop any topic that ended up empty.
    topics = [t for t in topics if t.claim_ids]
    for i, t in enumerate(topics):
        t.order = i
        if not t.id:
            t.id = f"t{i + 1}"
    return topics


def outline(
    projection: Projection,
    mine_out: dict[str, Any],
    *,
    provider: Optional[str] = None,
    invoke_fn=None,
) -> Outline:
    """Partition kept claims into an ordered set of topics with an arc."""
    kept_ids = projection.kept_ids()
    if not kept_ids:
        raise ValueError("outline: no kept claims")
    by_id = {c["id"]: c for c in mine_out.get("claims", [])}
    emphasis = {d.claim_id: d.emphasis for d in projection.decisions}

    kept_view = [{"id": cid, "shape": by_id[cid]["shape"],
                  "statement": by_id[cid]["statement"],
                  "takeaway": next((d.takeaway for d in projection.decisions
                                    if d.claim_id == cid), "")}
                 for cid in kept_ids if cid in by_id]
    user = (
        f"KEPT CLAIMS ({len(kept_view)}):\n" + llm.dumps(kept_view) +
        "\n\nGroup into as many topics as the content and audience genuinely warrant, "
        "with an arc. Every id in exactly one topic. Return ONLY the JSON."
    )
    data = llm.call_json(
        system_prompt=_SYSTEM, user_prompt=user, schema=_SCHEMA, stage="outline",
        # 600s (match mine/project): groups the whole KEPT claim set in one call, so
        # it scales with source size — the same full-payload stage class that timed
        # out project on a 324-claim book-length source.
        provider=provider, capability="standard", timeout=600,
        thinking_tokens=4000, invoke_fn=invoke_fn,
    )
    topics = [Topic(id=(t.get("id") or "").strip(), title=t["title"].strip(),
                    claim_ids=list(t.get("claim_ids") or []))
              for t in data.get("topics", [])]
    topics = _repair_partition(topics, kept_ids, emphasis)
    return Outline(topics=topics, arc=(data.get("arc") or "").strip())
