# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler STAGE 5 (author) — per topic, an LLM authors ONE
#   master doc (the full L3 body) + a per-level narrative spine (L0/L1/L2 summaries)
#   + a tier 0-3 for each claim. Then the CODE SLICER (pure function, no LLM)
#   derives per-level claim slices and enforces the HARD invariants: strict
#   superset chain, L3 == master doc, exactly one tier-0 per topic, and prose word
#   budgets L0<=12 / L1<=60 / L2<=150 (narrative spine only; module data excluded).
# index: author | build_slices | assert_invariants | _word_count | _enforce_tier0
#   | _enforce_budgets
# AGENT_HEADER_END -->
"""Stage 5 — author (LLM) + slicer (code).

Separation of concerns: the LLM writes prose and decides how much of a topic each
depth reveals (the tier map + spine); the CODE slicer turns that into the
level slices and refuses to pass anything that breaks an invariant. The author
pass GUARANTEES the invariants by construction (it forces exactly one tier-0 and
trims each spine to its word budget), so ``assert_invariants`` — which the golden
tests run standalone on fixtures — always passes on real output and RAISES on a
hand-broken fixture. Module data (a claim's structured ``fields``) is never part
of the spine, so it never counts against the prose budget.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.compiler.ir import (
    LEVELS,
    PROSE_BUDGET,
    Authored,
    AuthoredTopic,
    Outline,
    Projection,
    SlicedCell,
    Slices,
    SlicerError,
    TieredClaim,
)

logger = logging.getLogger(__name__)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["master_doc", "spine", "tiers"],
    "additionalProperties": False,
    "properties": {
        "master_doc": {"type": "string", "minLength": 1},
        "spine": {
            "type": "object",
            "required": ["l0", "l1", "l2"],
            "additionalProperties": False,
            "properties": {"l0": {"type": "string"}, "l1": {"type": "string"},
                           "l2": {"type": "string"}},
        },
        # A live model emits the tier map either as an array of {claim_id, tier}
        # OR as a {claim_id: tier} object; accept both (normalized in author()).
        # The slicer's assert_invariants re-derives + enforces the real tier
        # invariants regardless of input shape, so this is pure robustness — same
        # class as ir.lenient_fields for mine, extended to the author stage.
        "tiers": {"type": ["array", "object"]},
    },
}

_SYSTEM = f"""You are the AUTHOR stage of the prism deck compiler, writing ONE \
topic of a progressive-disclosure deck. A topic is read at four depths (L0..L3); \
deeper = more detail, and each depth is a SUPERSET of the one above.

Produce:
1. master_doc: the full L3 body for this topic as markdown prose (this is the \
deepest read — complete, well-structured, faithful to the claims).
2. spine: three progressively longer summaries of THIS topic:
   - l0: the single headline. AT MOST {PROSE_BUDGET[0]} words. One idea.
   - l1: the short version. AT MOST {PROSE_BUDGET[1]} words.
   - l2: the fuller version. AT MOST {PROSE_BUDGET[2]} words.
   These are NARRATIVE text only — do not paste tables/numbers/lists; the deck \
renders a claim's data as a visual module separately.
3. tiers: assign each claim a tier 0-3 = the shallowest depth at which it appears. \
EXACTLY ONE claim is tier 0 (the topic's lead claim). Supporting claims are 1-2; \
fine detail is 3.

Be faithful: every number, name, and fact must come from the claims. Do not invent.
Return ONLY the JSON object."""


def _word_count(text: str) -> int:
    return len((text or "").split())


def _trim_words(text: str, limit: int) -> str:
    words = (text or "").split()
    if len(words) <= limit:
        return text.strip()
    trimmed = " ".join(words[:limit])
    # end on a clean token, drop a dangling punctuation-free cut
    return re.sub(r"[\s,;:–-]+$", "", trimmed).strip()


def _enforce_tier0(tiers: dict[str, int], claim_ids: list[str],
                   emphasis: dict[str, float]) -> dict[str, int]:
    """Guarantee exactly one tier-0 claim for the topic (highest emphasis)."""
    out = {cid: tiers.get(cid, 2) for cid in claim_ids}
    zeros = [cid for cid, t in out.items() if t == 0]
    lead = max(claim_ids, key=lambda c: emphasis.get(c, 0.0))
    if zeros != [lead]:
        for cid in claim_ids:
            out[cid] = 0 if cid == lead else max(1, out[cid])
    return out


def author(
    outline: Outline,
    mine_out: dict[str, Any],
    projection: Projection,
    *,
    provider: Optional[str] = None,
    invoke_fn=None,
) -> Authored:
    """Author each topic's master doc, spine, and tier map (LLM), invariants
    enforced by construction so the slicer's hard asserts always hold."""
    by_id = {c["id"]: c for c in mine_out.get("claims", [])}
    emphasis = {d.claim_id: d.emphasis for d in projection.decisions}
    takeaway = {d.claim_id: d.takeaway for d in projection.decisions}

    authored: list[AuthoredTopic] = []
    for topic in outline.topics:
        claim_ids = topic.claim_ids
        claim_view = [{"id": cid, "shape": by_id[cid]["shape"],
                       "statement": by_id[cid]["statement"],
                       "takeaway": takeaway.get(cid, ""),
                       "fields": by_id[cid]["fields"]}
                      for cid in claim_ids if cid in by_id]
        user = (
            f"TOPIC: {topic.title}\nDECK ARC: {outline.arc}\n\n"
            f"CLAIMS IN THIS TOPIC ({len(claim_view)}):\n" + llm.dumps(claim_view) +
            f"\n\nAuthor this topic. Word caps: L0<={PROSE_BUDGET[0]}, "
            f"L1<={PROSE_BUDGET[1]}, L2<={PROSE_BUDGET[2]}. Return ONLY the JSON."
        )
        data = llm.call_json(
            system_prompt=_SYSTEM, user_prompt=user, schema=_SCHEMA, stage="author",
            provider=provider, capability="standard", timeout=420,
            thinking_tokens=6000, invoke_fn=invoke_fn,
        )
        # tiers may arrive as [{claim_id, tier}, …] or as {claim_id: tier}.
        _t = data.get("tiers", [])
        _valid = set(claim_ids)
        if isinstance(_t, dict):
            raw_tiers = {cid: int(v) for cid, v in _t.items() if cid in _valid}
        else:
            raw_tiers = {t["claim_id"]: int(t["tier"]) for t in _t
                         if isinstance(t, dict) and t.get("claim_id") in _valid
                         and t.get("tier") is not None}
        tiers = _enforce_tier0(raw_tiers, claim_ids, emphasis)

        master = data["master_doc"].strip()
        sp = data.get("spine", {})
        spine = {
            0: _trim_words(sp.get("l0", ""), PROSE_BUDGET[0]),
            1: _trim_words(sp.get("l1", ""), PROSE_BUDGET[1]),
            2: _trim_words(sp.get("l2", ""), PROSE_BUDGET[2]),
            3: master,
        }
        authored.append(AuthoredTopic(
            topic_id=topic.id, master_doc=master, spine=spine,
            tiers=[TieredClaim(claim_id=cid, tier=tiers[cid]) for cid in claim_ids],
        ))
    result = Authored(topics=authored)
    assert_invariants(result, outline)  # fail fast if construction slipped
    return result


# ── the CODE slicer (pure function, no LLM) ─────────────────────────────────────
def slice_topic(at: AuthoredTopic, claim_ids: list[str]) -> list[SlicedCell]:
    """Per-level slices: Ln claims = those with tier <= n; prose = spine[n]."""
    cells: list[SlicedCell] = []
    for n in LEVELS:
        visible = [cid for cid in claim_ids if at.tier_of(cid) <= n]
        cells.append(SlicedCell(topic_id=at.topic_id, level=n,
                                claim_ids=visible, prose=at.spine.get(n, "")))
    return cells


def build_slices(authored: Authored, outline: Outline) -> Slices:
    topic_claims = {t.id: t.claim_ids for t in outline.topics}
    cells: list[SlicedCell] = []
    for at in authored.topics:
        cells.extend(slice_topic(at, topic_claims.get(at.topic_id, [])))
    return Slices(cells=cells)


def assert_invariants(authored: Authored, outline: Outline) -> None:
    """Raise SlicerError unless every hard slicer invariant holds."""
    topic_claims = {t.id: t.claim_ids for t in outline.topics}
    for at in authored.topics:
        claim_ids = topic_claims.get(at.topic_id, [])
        cells = {c.level: c for c in slice_topic(at, claim_ids)}

        # 1. strict superset chain L0 ⊆ L1 ⊆ L2 ⊆ L3
        for n in (1, 2, 3):
            prev, cur = set(cells[n - 1].claim_ids), set(cells[n].claim_ids)
            if not prev <= cur:
                raise SlicerError(
                    f"{at.topic_id}: L{n} is not a superset of L{n-1} "
                    f"(missing {prev - cur})")

        # 2. exactly one tier-0 claim per topic
        zeros = [c for c in claim_ids if at.tier_of(c) == 0]
        if len(zeros) != 1:
            raise SlicerError(f"{at.topic_id}: expected exactly 1 tier-0 claim, got {len(zeros)}")

        # 3. L3 body == master doc, and L3 shows every claim in the topic
        if cells[3].prose != at.master_doc:
            raise SlicerError(f"{at.topic_id}: L3 spine != master_doc")
        if set(cells[3].claim_ids) != set(claim_ids):
            raise SlicerError(f"{at.topic_id}: L3 does not contain every topic claim")

        # 4. prose word budgets (narrative spine only)
        for n in (0, 1, 2):
            wc = _word_count(cells[n].prose)
            if wc > PROSE_BUDGET[n]:
                raise SlicerError(
                    f"{at.topic_id}: L{n} prose is {wc} words, budget {PROSE_BUDGET[n]}")
