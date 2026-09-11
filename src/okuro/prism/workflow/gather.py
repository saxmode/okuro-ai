# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism workflow NODE 4 (gather) — for each topic in the TopicMap,
#   pull the artifact material that topic needs. Runs PER TOPIC over the sections
#   the topic references (full body when it references none), so a fact may be
#   gathered for several topics. Emits TopicGathering with real items_content.
# index: gather_topic | gather_all | _GATHER_SYSTEM | _slice_for_topic
# AGENT_HEADER_END -->
"""Node 4 — gather: TopicMap x artifact -> per-topic material.

Step 4 of the design loop, and the inverse of mining. Mining ran ONCE over the
whole document and produced claims that then had to be dealt out into topics.
Gathering runs ONCE PER TOPIC and asks a different question: *this topic needs
to answer this reader's question — what in the document serves it?*

Two consequences of that inversion:

* **Sharing is free.** A fact that serves three topics is gathered three times,
  once per topic, and each copy records ``for_topics``. There is no ownership to
  arbitrate because nothing is being partitioned.
* **A topic with no resolved sections is still gatherable.** ``topicmap`` keeps
  such topics on purpose (see its ``unresolved_refs``); here they fall back to
  the FULL body rather than being skipped, which is what makes keeping them
  meaningful rather than decorative.

Facts carry an ``evidence`` span copied verbatim, so Phase E's faithfulness gate
has something to check against. Grounding is measured and reported, never used to
silently drop material.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.compiler.ir import SHAPES
from okuro.prism.compiler.mine import fetch_artifact_body
from okuro.prism.workflow.types import (
    GatheredFact,
    RecipientBrief,
    TopicGathering,
    Understanding,
    WorkflowTopic,
)

logger = logging.getLogger(__name__)

#: Below this share of facts tracing VERBATIM to the source, a topic's evidence
#: spans are paraphrases. Kept at the value the original log line used so the
#: threshold is unchanged — what changed is that the signal now survives the
#: cache and reaches the deck instead of dying in a logger.
GROUNDING_FLOOR = 0.6

_MAX_CHARS = 120_000

# No `maxItems`: how much material a topic needs is the topic's business.
_GATHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["text", "shape"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "shape": {"type": "string", "enum": list(SHAPES)},
                    "items_content": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                    "evidence": {"type": "string"},
                    "salience": {"type": "number"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "coverage_note": {"type": "string"},
    },
}

_GATHER_SYSTEM = f"""You are the GATHER node of the prism deck workflow. You are \
given ONE topic — a question a specific reader has — and the stretch of source \
document that topic draws on. Pull out everything in that material which SERVES \
this topic.

You are not summarizing the document and not cataloguing it. You are answering: \
what does this source give me that helps answer this reader's question?

For each piece of material, emit:
- text: one self-contained sentence stating it. Specific, not a label. "Costs fell \
92% after the migration" — never "information about costs".
- shape: the data shape, exactly one of: {", ".join(SHAPES)}.
- items_content: the REAL per-item content as [label, detail, meta] triples — the \
actual numbers, names, and steps from the source. A 4-option comparison gives 4 \
triples. A single metric gives 1. Leave empty ONLY when the material genuinely has \
no item structure (a bare assertion). Never invent a value to fill a slot; if the \
source does not give a detail, use an empty string in that position.
- evidence: a VERBATIM span copied from the source that supports it (>= 12 chars). \
Copy exactly, do not paraphrase.
- salience: 0..1 — how much this matters TO THIS TOPIC (not to the document).
- source_refs: which of the given section ids it came from.

Take everything that serves the topic, at whatever volume the material warrants. \
Material that serves this topic AND others is still taken here — you are not \
dividing the document up, you are serving one topic.
Return ONLY {{"facts": [...], "coverage_note": "..."}}."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _slice_for_topic(topic: WorkflowTopic, understanding: Understanding,
                     body: str) -> tuple[str, list[str]]:
    """The section view this topic draws on, plus the ids it covers.

    A topic whose refs resolved to nothing gets the WHOLE body — topicmap kept it
    deliberately, so gather has to actually try rather than return empty.
    """
    by_id = {s.id: s for s in understanding.sections}
    refs = [r for r in topic.section_refs if r in by_id]
    if not refs:
        logger.info("gather: topic %r has no resolved section refs — gathering from "
                    "the full body", topic.id)
        return body[:_MAX_CHARS], []
    view = "\n\n".join(
        f"[{by_id[r].id}] {by_id[r].heading}\n{by_id[r].gist}\n"
        f"covers: {', '.join(by_id[r].covers)}"
        for r in refs
    )
    return view[:_MAX_CHARS], refs


def gather_topic(
    topic: WorkflowTopic,
    understanding: Understanding,
    brief: RecipientBrief,
    body: str,
    *,
    provider: Optional[str] = None,
    invoke_fn=None,
) -> TopicGathering:
    """Pull the artifact material that serves ONE topic."""
    view, refs = _slice_for_topic(topic, understanding, body)
    user = (
        f"TOPIC: {topic.title!r}\n"
        f"THE READER'S QUESTION: {topic.question_it_answers}\n"
        f"WHY IT MATTERS TO THEM: {topic.why_this_recipient}\n"
        f"READER: {llm.dumps(brief.project_for_prompt())}\n\n"
        f"SOURCE MATERIAL{' (sections ' + ', '.join(refs) + ')' if refs else ' (full document)'}:\n"
        f"{view}\n\n"
        "Pull everything here that serves this topic. Return ONLY the JSON."
    )
    data = llm.call_json(
        system_prompt=_GATHER_SYSTEM, user_prompt=user, schema=_GATHER_SCHEMA,
        stage="wf.gather", provider=provider, capability="standard",
        timeout=600, thinking_tokens=4000, invoke_fn=invoke_fn,
    )

    body_norm = _norm(body)
    facts: list[GatheredFact] = []
    for i, f in enumerate(data.get("facts") or [], 1):
        ev = (f.get("evidence") or "").strip()
        items = [[str(x) for x in (row or [])][:3] for row in (f.get("items_content") or [])]
        items = [row + [""] * (3 - len(row)) for row in items if any(str(c).strip() for c in row)]
        # The shape default is no longer SILENT. `narrative`'s fit-set is 8/10
        # pure text, so a defaulted shape is a block that will render as prose
        # whatever the agent reasons — and until now nothing recorded that it
        # had happened. It stays a substitution (a hard raise arrives in P3,
        # where a claim can be typed correctly instead of merely refused).
        raw_shape = str(f.get("shape") or "").strip()
        defaulted = raw_shape not in SHAPES
        facts.append(GatheredFact(
            id=f"{topic.id}.f{i}",
            text=(f.get("text") or "").strip(),
            shape="narrative" if defaulted else raw_shape,
            shape_defaulted=defaulted,
            shape_raw=raw_shape if defaulted else "",
            items_content=items,
            source_refs=[str(r).strip() for r in (f.get("source_refs") or refs) if str(r).strip()],
            evidence=ev,
            salience=float(f.get("salience", 0.5) or 0.5),
            for_topics=[topic.id],
        ))

    grounded = sum(1 for f in facts if len(_norm(f.evidence)) >= 12
                   and _norm(f.evidence) in body_norm)
    rate = (grounded / len(facts)) if facts else 1.0
    if facts and rate < GROUNDING_FLOOR:
        logger.warning("gather: topic %r low grounding — %d/%d facts trace verbatim",
                       topic.id, grounded, len(facts))
    defaulted = [f.id for f in facts if f.shape_defaulted]
    if defaulted:
        logger.warning("gather: topic %r — %d fact(s) had no usable shape, "
                       "substituted 'narrative': %s", topic.id, len(defaulted), defaulted)
    return TopicGathering(topic_id=topic.id, facts=facts, grounding_rate=rate,
                          coverage_note=(data.get("coverage_note") or "").strip())


def gather_all(
    topics: list[WorkflowTopic],
    understanding: Understanding,
    brief: RecipientBrief,
    *,
    provider: Optional[str] = None,
    invoke_fn=None,
    body: Optional[str] = None,
) -> dict[str, TopicGathering]:
    """Gather material for every topic. Fail-soft per topic, never silently empty.

    A topic whose gather fails keeps an EMPTY gathering rather than disappearing —
    the authoring node then reports a topic it could not source, which is a visible
    deficit instead of a deck that is quietly short a topic.
    """
    if body is None:
        _, body = fetch_artifact_body(understanding.source_artifact_id)
    out: dict[str, TopicGathering] = {}
    for t in topics:
        try:
            out[t.id] = gather_topic(t, understanding, brief, body,
                                     provider=provider, invoke_fn=invoke_fn)
        except Exception as exc:  # noqa: BLE001 — one topic must not sink the deck
            logger.warning("gather: topic %r failed, emitting empty gathering: %s",
                           t.id, exc)
            out[t.id] = TopicGathering(topic_id=t.id, facts=[],
                                       coverage_note=f"gather failed: {exc}")
    shared = sum(1 for t in topics for f in out[t.id].facts if len(f.source_refs) > 1)
    logger.info("gather: %d topics, %d facts total (%d span >1 section)",
                len(topics), sum(len(g.facts) for g in out.values()), shared)
    return out
