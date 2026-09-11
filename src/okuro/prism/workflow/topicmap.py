# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism workflow NODE 3 (topicmap) — reason out the topics THIS
#   recipient needs from THIS document, with an arc. Replaces compiler/outline.py:
#   no claim clustering, no strict partition (a section may serve many topics), and
#   no topic cap. Code repairs identity/refs only — it never drops a topic.
# index: topic_map | _TOPICMAP_SCHEMA | _TOPICMAP_SYSTEM | _repair
# AGENT_HEADER_END -->
"""Node 3 — topicmap: Understanding x RecipientBrief -> ``TopicMap``.

This is the node that most sharply departs from the old compiler. ``outline.py``
took the surviving *claims* and partitioned them: every claim in exactly one
topic, enforced in code. That produced topics that were artifacts of clustering
— a claim's home decided by which bucket had room, not by what a reader needs.

Here topics are REASONED from two inputs the old outline never had together: what
the document is (Understanding) and who is reading it (RecipientBrief). Three
invariants follow, and each one is the inverse of an old failure:

* **No partition.** ``section_refs`` may overlap freely between topics. One
  section legitimately feeds several topics, and the old strict-partition repair
  is deliberately absent — there is nothing here to repair.
* **No cap.** The schema sets no ``maxItems`` on topics. A rich reference yields
  many; a memo, few. The 4-7 ceiling is what crushed a 46k-word source into
  seven topics.
* **No silent drops.** A topic whose refs do not resolve keeps its place and
  records them in ``unresolved_refs``. Dropping it would be a hidden cap wearing
  a repair's clothing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.workflow.types import RecipientBrief, TopicMap, Understanding, WorkflowTopic

logger = logging.getLogger(__name__)

# NOTE: no `maxItems` on `topics` — deliberately. Topic count is the model's
# judgement from content x recipient. See the module docstring.
_TOPICMAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["arc", "topics"],
    "properties": {
        "arc": {"type": "string"},
        "topics": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["title", "question_it_answers", "section_refs"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string", "minLength": 1},
                    "question_it_answers": {"type": "string"},
                    "why_this_recipient": {"type": "string"},
                    "section_refs": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "number"},
                },
            },
        },
    },
}

_TOPICMAP_SYSTEM = """You are the TOPIC-MAP node of the prism deck workflow. You \
have a holistic understanding of one document and a brief on the person who will \
read it. Decide what TOPICS this deck should have for THIS reader, and the ARC \
that connects them.

A topic is a question this reader has, that this document can answer. Name it that \
way. For each topic give:
- title: short and specific. Never "Introduction", "Overview", "Other".
- question_it_answers: the reader's actual question, in their terms.
- why_this_recipient: why this topic earns their attention — tie it to what they \
care about or decide. If you cannot state this, the topic does not belong.
- section_refs: the section ids from the SECTION MAP whose material this topic \
needs. A section MAY appear in several topics — one fact can serve more than one \
question, and you should reuse it rather than force a clean split.
- priority: 0..1, how much this reader needs it. Ordering weight, not a filter.

How many topics: exactly as many as the material and this reader genuinely warrant. \
There is NO limit and no target. A long, rich source for an engaged reader yields \
many topics; a short memo yields few. Do not compress distinct themes together to \
reach a tidy number, and do not split one theme to pad the count. Coverage is \
judged against what the reader needs, not against a size.

The ARC is the throughline joining the topics in order — the reason this sequence \
is the right one for this reader (e.g. situation -> options -> recommendation -> ask).
Order the topics so the arc reads start to finish.
Return ONLY {"arc": "...", "topics": [...]}."""


def _repair(topics: list[WorkflowTopic], valid_ids: set[str]) -> list[WorkflowTopic]:
    """Fix topic IDENTITY and REFERENCE validity. Never drops a topic, never dedups
    a section across topics — the overlap is the point (see module docstring)."""
    out: list[WorkflowTopic] = []
    used: set[str] = set()
    for i, t in enumerate(topics):
        if not t.title.strip():
            logger.warning("topicmap: dropping a topic with an empty title (index %d)", i)
            continue
        tid = (t.id or "").strip()
        if not tid or tid in used:
            tid = f"t{i + 1}"
            while tid in used:                      # only on a genuine id collision
                tid = f"{tid}x"
        used.add(tid)
        t.id = tid
        # Keep refs that resolve; record the rest instead of discarding them
        # silently, so a hallucinated ref is visible rather than a quiet gap.
        keep, lost = [], []
        for r in t.section_refs:
            (keep if r in valid_ids else lost).append(r)
        t.section_refs, t.unresolved_refs = keep, lost
        if lost:
            logger.warning("topicmap: topic %r references %d unknown section id(s): %s",
                           t.id, len(lost), lost)
        if not keep:
            # Kept on purpose. Phase C's gather can still search the full body for
            # it; dropping it here would be a cap in a repair's clothing.
            logger.warning("topicmap: topic %r resolved to NO sections — kept, "
                           "gather will have to source it from the full body", t.id)
        out.append(t)
    for i, t in enumerate(out):
        t.order = i
    return out


def topic_map(
    understanding: Understanding,
    brief: RecipientBrief,
    *,
    provider: Optional[str] = None,
    invoke_fn=None,
) -> TopicMap:
    """Reason out the recipient-relevant topics for this document, with an arc."""
    if not understanding.sections:
        raise ValueError("topicmap: understanding has no sections")

    doc_view = {
        "title": understanding.source_title,
        "kind": understanding.document_kind,
        "what_it_is": understanding.what_it_is,
        "thesis": understanding.thesis,
        "domain": understanding.domain,
        "through_lines": understanding.through_lines,
        "open_questions": understanding.open_questions,
        "word_count": understanding.word_count,
    }
    section_view = [{"id": s.id, "heading": s.heading, "gist": s.gist, "covers": s.covers}
                    for s in understanding.sections]
    # Reader NAMES go to the LLM only when the deck is allowed to name its
    # readers. Everything else about a recipient is firewalled, so the name
    # is an opt-in too — prism owns the setter (and the no-attribution lint);
    # people ships the field and this gate.
    reader_view = brief.project_for_prompt()
    if brief.mention_audience_members:
        reader_view["recipients"] = brief.recipients
    user = (
        "DOCUMENT:\n" + llm.dumps(doc_view) +
        f"\n\nSECTION MAP ({len(section_view)} sections, document order):\n"
        + llm.dumps(section_view) +
        "\n\nREADER BRIEF:\n" + llm.dumps(reader_view) +
        "\n\nDecide the topics this reader needs, and the arc. A section id may be "
        "used by more than one topic. Return ONLY the JSON."
    )
    data = llm.call_json(
        system_prompt=_TOPICMAP_SYSTEM, user_prompt=user, schema=_TOPICMAP_SCHEMA,
        stage="wf.topicmap", provider=provider, capability="standard",
        # 600s: reasons over the whole section map at once, so it scales with the
        # source the same way mine/project/outline do.
        timeout=600, thinking_tokens=8000, invoke_fn=invoke_fn,
    )

    topics = [
        WorkflowTopic(
            id=str(t.get("id") or "").strip(),
            title=str(t.get("title") or "").strip(),
            question_it_answers=str(t.get("question_it_answers") or "").strip(),
            why_this_recipient=str(t.get("why_this_recipient") or "").strip(),
            section_refs=[str(r).strip() for r in (t.get("section_refs") or []) if str(r).strip()],
            priority=float(t.get("priority", 0.5) or 0.5),
        )
        for t in data.get("topics") or []
    ]
    topics = _repair(topics, set(understanding.section_ids()))
    if not topics:
        raise ValueError("topicmap: no usable topics returned")
    logger.info("topicmap: %d topics over %d sections (overlap allowed, no cap)",
                len(topics), len(understanding.sections))
    return TopicMap(arc=(data.get("arc") or "").strip(), topics=topics)
