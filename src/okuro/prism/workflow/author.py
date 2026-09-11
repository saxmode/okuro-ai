# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism workflow NODE 5 (author) — write each topic's four depth
#   levels BY JOB, not by claim density. L1-L3 in one call (they must cohere: L2
#   explains L1's elements), L4 prose in its own call so length is never squeezed.
#   Replaces authoring/ladder.py select_kept_levels, whose density rule collapsed
#   levels. Emits AuthoredTopic; picks NO components (that is Phase D).
# index: author_topic | author_all | LEVEL_JOBS | _repair_level | _one_focal
# AGENT_HEADER_END -->
"""Node 5 — author: gathered material -> four levels, each doing its own job.

The old ladder decided depth by INFORMATION LOAD: L2 was admitted only if it
carried strictly more than L1 and composed differently, else the topic emitted a
shorter ladder (``ladder.py`` ``select_kept_levels``). A thin topic therefore lost
its deep levels — the deck went quiet exactly where a reader was digging in.

Here the levels are DEFINED BY THEIR JOB, and a job does not evaporate when the
material is thin:

===  ============================================================================
L1   What this topic IS, plus its 3-5 key elements. Hero-short — lands in seconds.
L2   Explains those key elements. Still short.
L3   Adds every relevant factor. A readable slide; verbosity is held down.
L4   Full prose. A navigable book-entry, not a slide.
===  ============================================================================

From ANY level the reader can open the full original artifact as an overlay, so
no level has to be complete on its own — ``L4Doc.artifact_id`` carries that link.

**The 3-5 in L1's job is a JOB SPEC, not an output cap.** It says what L1 is for —
an overview a reader takes in at a glance — and it is stated in the prompt. It is
deliberately NOT a schema ``maxItems``: caps belong nowhere in this workflow, and
a topic that genuinely has six key elements must be able to say so.

Two solver constraints are repaired in code rather than trusted from the model,
because ``solver/schema.py:assert_valid`` rejects a plan that breaks them and the
failure would only surface in Phase D: exactly ONE focal block at L1/L2 (line 128),
and that focal must be tier-0 (line 148).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.compiler.ir import SHAPES
from okuro.prism.solver.schema import EMPHASIS
from okuro.prism.workflow.kit_budget import budget_prose, unrenderable
from okuro.prism.workflow.types import (
    AuthoredBlock,
    AuthoredLevel,
    AuthoredTopic,
    GatheredFact,
    L4Doc,
    L4Section,
    RecipientBrief,
    TopicGathering,
    WorkflowTopic,
)

logger = logging.getLogger(__name__)

SLIDE_LEVELS: tuple[str, ...] = ("L1", "L2", "L3")

LEVEL_JOBS: dict[str, str] = {
    "L1": "Say what this topic IS and name its key elements — the 3-5 things this "
          "topic is made of. Hero-short: a reader takes it in at a glance. Name the "
          "elements; do not explain them yet.",
    "L2": "Explain the key elements L1 named — what each one means and why it "
          "matters to this reader. Still short. Do not introduce new elements.",
    "L3": "Add every factor that is genuinely relevant: conditions, caveats, "
          "numbers, trade-offs, exceptions. A slide a reader STUDIES — so it must "
          "still be readable. Hold verbosity down; density is fine, waffle is not.",
}

# No `maxItems` anywhere: block counts follow the material and the level's job.
_LEVELS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["levels"],
    "properties": {
        "levels": {
            "type": "array", "minItems": 3,
            "items": {
                "type": "object",
                "required": ["level", "blocks"],
                "properties": {
                    "level": {"type": "string", "enum": list(SLIDE_LEVELS)},
                    "job": {"type": "string"},
                    "blocks": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["text", "shape"],
                            "properties": {
                                "job": {"type": "string"},
                                "text": {"type": "string", "minLength": 1},
                                "shape": {"type": "string", "enum": list(SHAPES)},
                                "emphasis": {"type": "string", "enum": list(EMPHASIS)},
                                "tier": {"type": "integer"},
                                "items_content": {
                                    "type": "array",
                                    "items": {"type": "array", "items": {"type": "string"}},
                                },
                                "fact_ids": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
            },
        },
    },
}

_L4_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sections"],
    "properties": {
        "sections": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["heading", "body"],
                "properties": {
                    "heading": {"type": "string", "minLength": 1},
                    "body": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

_LEVELS_SYSTEM = f"""You are the AUTHOR node of the prism deck workflow. You write \
ONE topic across three slide levels. Each level has a JOB, and you write to the job \
— not to a word count and not to how much material happens to exist.

L1 — {LEVEL_JOBS['L1']}
L2 — {LEVEL_JOBS['L2']}
L3 — {LEVEL_JOBS['L3']}

The levels are one continuous zoom, not three summaries of different lengths. L2 \
must explain the SAME elements L1 named, in the same order. L3 must deepen what L2 \
explained. A reader moving L1 -> L2 -> L3 should feel resolution INCREASE, never a \
change of subject.

All three levels exist, always. If the material is thin, L1 still names what it can \
and L3 still adds every factor there is — a thin topic produces short levels, never \
missing ones.

Each level is a list of BLOCKS. For each block:
- job: what this block does on this level, in a few words.
- text: the actual authored sentence(s). This is deck copy — write it properly.
- shape: the data shape, one of: {", ".join(SHAPES)}.
- items_content: the REAL per-item [label, detail, meta] triples. Carry the actual \
numbers and names through from the gathered facts. Never invent a value; use an \
empty string where the source gives none.
- emphasis: one of focal | primary | supporting | aside. EXACTLY ONE block on L1 \
and exactly one on L2 must be "focal" — the thing the eye lands on first. Give the \
focal block tier 0.
- tier: 0 for the most important block on the level, 1 otherwise.
- fact_ids: which gathered facts this block draws on.

WHAT EACH RUNG CAN ACTUALLY RENDER. The component kit is not uniform across \
depths, and a block the kit cannot carry is a block that ships as fallback text \
however well it is written. Per rung, the shapes available and the item counts \
they hold:

{budget_prose(SHAPES)}

Write inside that. If a topic's L1 idea is a comparison or a relationship, \
either express it at L2/L3 or reframe it for L1 — a verdict or a set of named \
elements usually carries the same point at a glance. If a set has more items \
than the rung holds, that is a signal the rung is being asked to do L2's job: \
name the grouping at L1 and enumerate deeper.

Every statement must be supported by the gathered facts. You are writing them up \
for this reader, not adding to them.
Return ONLY {{"levels": [{{"level","job","blocks"}}]}} with all three levels."""

_L4_SYSTEM = """You are the AUTHOR node of the prism deck workflow, writing level \
L4 — the full-resolution reading level. This is NOT a slide. It is a navigable \
book-entry the reader opens when they want the whole thing.

Write the topic out in full prose, in titled sections a reader can navigate. Cover \
everything the gathered material holds for this topic — L4 is where nothing is \
held back. Write properly: paragraphs, not bullet fragments.

Do not pad and do not repeat the shallower levels verbatim; this is the same topic \
at full resolution, written to be READ. Length follows the material.
Return ONLY {"sections": [{"heading", "body"}]}."""


def _facts_view(facts: list[GatheredFact]) -> list[dict[str, Any]]:
    return [{"id": f.id, "text": f.text, "shape": f.shape,
             "items_content": f.items_content, "salience": f.salience}
            for f in facts]


def _block_shape(b: dict, topic_id: str, level: str, idx: int) -> str:
    """The block's shape, substituting ``narrative`` LOUDLY rather than silently.

    This default only bites for a block that cites no fact — a block with
    ``fact_ids`` is validated against the FACT's shape at the solver boundary,
    not this one. Kept as a substitution: a hard raise arrives in P3, where a
    claim can be typed correctly instead of merely refused.
    """
    raw = str(b.get("shape") or "").strip()
    if raw in SHAPES:
        return raw
    logger.warning("author: %s.%s block %d has no usable shape (%r) — "
                   "substituting 'narrative', whose fit-set is 8/10 pure text",
                   topic_id, level, idx, raw)
    return "narrative"


def _one_focal(blocks: list[AuthoredBlock], level: str, topic_id: str) -> None:
    """Force exactly one focal, tier-0 block. Solver contract, repaired in place.

    ``solver/schema.py:assert_valid`` rejects an L1/L2 plan without exactly one
    focal (line 128) and requires the focal's claim to be tier-0 (line 148). The
    model is asked for this; code guarantees it, so Phase D cannot inherit a plan
    that fails validation for a reason Phase C could have fixed.
    """
    if level not in ("L1", "L2") or not blocks:
        return
    focals = [b for b in blocks if b.emphasis == "focal"]
    if not focals:
        # Promote the most important block: lowest tier, else the first.
        pick = min(blocks, key=lambda b: (b.tier, blocks.index(b)))
        pick.emphasis = "focal"
        focals = [pick]
        logger.info("author: %s/%s had no focal — promoted %r", topic_id, level, pick.id)
    elif len(focals) > 1:
        n_focals = len(focals)
        keep = min(focals, key=lambda b: (b.tier, blocks.index(b)))
        for b in focals:
            if b is not keep:
                b.emphasis = "primary"
        focals = [keep]
        logger.info("author: %s/%s had %d focals — kept %r",
                    topic_id, level, n_focals, keep.id)
    focals[0].tier = 0          # the focal must trace to tier-0


def _repair_level(level: str, job: str, raw_blocks: list[dict[str, Any]],
                  topic: WorkflowTopic, fact_ids: set[str]) -> AuthoredLevel:
    blocks: list[AuthoredBlock] = []
    for i, b in enumerate(raw_blocks, 1):
        text = (b.get("text") or "").strip()
        if not text:
            continue
        items = [[str(x) for x in (row or [])][:3] for row in (b.get("items_content") or [])]
        items = [row + [""] * (3 - len(row)) for row in items if any(str(c).strip() for c in row)]
        refs = [str(f) for f in (b.get("fact_ids") or []) if str(f) in fact_ids]
        raw_tier = b.get("tier")
        tier = int(raw_tier) if isinstance(raw_tier, (int, float)) else 1
        blocks.append(AuthoredBlock(
            id=f"{topic.id}.{level}.b{i}",
            job=(b.get("job") or "").strip(),
            text=text,
            # Only bites for a block citing NO fact (the synthesized-claim
            # branch in assemble); a block with fact_ids is validated against
            # the FACT's shape, not this one. Substituted, not silent.
            shape=_block_shape(b, topic.id, level, i),
            items_content=items,
            emphasis=b.get("emphasis") if b.get("emphasis") in EMPHASIS else "supporting",
            tier=tier,
            fact_ids=refs,
            source_refs=list(topic.section_refs),
        ))
    _one_focal(blocks, level, topic.id)
    return AuthoredLevel(level=level, job=job or LEVEL_JOBS.get(level, ""), blocks=blocks)


def author_topic(
    topic: WorkflowTopic,
    gathering: TopicGathering,
    brief: RecipientBrief,
    *,
    artifact_id: str = "",
    provider: Optional[str] = None,
    invoke_fn=None,
) -> AuthoredTopic:
    """Author one topic across L1-L3 (one call) and L4 (its own call).

    Raises if the model does not return all three slide levels — a missing level is
    exactly the collapse this node exists to prevent, so it is loud, not repaired
    into silence.
    """
    if not gathering.facts:
        raise ValueError(f"author: topic {topic.id!r} has no gathered facts")

    # Exhaustive by construction. This node writes all four depth levels and
    # used to be handed 5 of 9 brief fields — depth_ceiling, the one field
    # that says how deep this reader can go, was computed upstream and
    # dropped right here.
    reader = brief.project_for_prompt()
    facts_json = llm.dumps(_facts_view(gathering.facts))
    common = (
        f"TOPIC: {topic.title!r}\n"
        f"THE READER'S QUESTION: {topic.question_it_answers}\n"
        f"WHY IT MATTERS TO THEM: {topic.why_this_recipient}\n\n"
        f"READER: {llm.dumps(reader)}\n\n"
        f"GATHERED MATERIAL ({len(gathering.facts)} facts):\n{facts_json}\n\n"
    )

    data = llm.call_json(
        system_prompt=_LEVELS_SYSTEM,
        user_prompt=common + "Write L1, L2 and L3. Return ONLY the JSON.",
        schema=_LEVELS_SCHEMA, stage="wf.author.levels", provider=provider,
        capability="standard", timeout=600, thinking_tokens=8000, invoke_fn=invoke_fn,
    )
    fact_ids = {f.id for f in gathering.facts}
    by_level = {str(lv.get("level")): lv for lv in (data.get("levels") or [])}
    missing = [lv for lv in SLIDE_LEVELS if lv not in by_level]
    if missing:
        raise ValueError(
            f"author: topic {topic.id!r} is missing level(s) {missing} — every level "
            "has a job and must exist; this is the collapse Phase C replaces")

    levels = {
        lv: _repair_level(lv, (by_level[lv].get("job") or "").strip(),
                          by_level[lv].get("blocks") or [], topic, fact_ids)
        for lv in SLIDE_LEVELS
    }

    l4 = llm.call_json(
        system_prompt=_L4_SYSTEM,
        user_prompt=common + "Write the full L4 prose entry. Return ONLY the JSON.",
        schema=_L4_SCHEMA, stage="wf.author.l4", provider=provider,
        capability="standard", timeout=600, thinking_tokens=8000, invoke_fn=invoke_fn,
    )
    sections = [L4Section(heading=(s.get("heading") or "").strip(),
                          body=(s.get("body") or "").strip())
                for s in (l4.get("sections") or [])
                if (s.get("body") or "").strip()]

    return AuthoredTopic(
        topic_id=topic.id,
        title=topic.title,
        levels=levels,
        l4=L4Doc(topic_id=topic.id, title=topic.title, sections=sections,
                 # Reachable from EVERY level, not just L4 — the reader can always
                 # drop from any depth straight to the unabridged source.
                 artifact_id=artifact_id,
                 word_count=sum(len(s.body.split()) for s in sections)),
        facts=list(gathering.facts),
    )


def author_all(
    topics: list[WorkflowTopic],
    gatherings: dict[str, TopicGathering],
    brief: RecipientBrief,
    *,
    artifact_id: str = "",
    provider: Optional[str] = None,
    invoke_fn=None,
) -> list[AuthoredTopic]:
    """Author every topic. Fail-soft per topic, and it reports what it lost.

    A topic that cannot be authored is omitted from the result AND logged — the
    caller sees a count mismatch rather than a deck that is quietly one topic short.
    """
    out: list[AuthoredTopic] = []
    failed: list[str] = []
    for t in topics:
        g = gatherings.get(t.id)
        if g is None:
            failed.append(t.id)
            logger.warning("author: topic %r has no gathering — skipped", t.id)
            continue
        try:
            out.append(author_topic(t, g, brief, artifact_id=artifact_id,
                                    provider=provider, invoke_fn=invoke_fn))
        except Exception as exc:  # noqa: BLE001 — one topic must not sink the deck
            failed.append(t.id)
            logger.warning("author: topic %r failed: %s", t.id, exc)
    if failed:
        logger.warning("author: %d/%d topics authored; FAILED: %s",
                       len(out), len(topics), failed)
    if not out:
        raise ValueError("author: no topic could be authored")
    return out
