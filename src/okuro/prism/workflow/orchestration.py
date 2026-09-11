# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The prism deck workflow AS A DRAWN WORKFLOW DOCUMENT — a node graph
#   seeded into the orchestrator's workflow store, compiled by the generic
#   flow_compiler like any other. Prism is the FIRST INSTANCE of manually
#   arranged workflows, never a parallel implementation of them.
# index: PRISM_WORKFLOW_ID | prism_workflow_graph | seed_prism_workflow
# AGENT_HEADER_END -->
"""The prism deck workflow, expressed as a workflow document.

This module used to build orchestrator plan dicts directly, because prism needed
a static DAG before a general mechanism for them existed. That mechanism now
exists — ``okuro.orchestrator.flow_compiler`` compiles any drawn graph, and
``okuro.orchestrator.workflow_store`` persists them — so prism stops being
special and becomes what it always should have been: the first workflow document.

What survives from the bespoke version is the part that carried the thinking —
the per-node briefs. They encode the owner's internal-dialog spec (verbatim in
artifact ``5e14100b``) and are unchanged here. What is gone is the duplicated
plan-building machinery: phases, ids, dependency wiring and the fan-out are now
the compiler's job, driven by ``node.data``.

**Two dimensions, as before.** The procedure never varies, so the graph is
authored here in code and reviewed like code. The only dynamic axis is the topic
count, expressed as ``fan_out`` on the per-topic node — the compiler withholds it
until ``workflow_run.expand_fanout`` supplies the items.

**It is a normal workflow.** Once seeded it appears in ``/workflows``, can be
opened in the designer, edited, versioned and restored like any other. Editing it
there is legitimate: this seed is a starting point, not a lock.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PRISM_WORKFLOW_ID = "prism-deck"
PRISM_WORKFLOW_NAME = "Prism deck"

# Roles resolved from the live catalogue — all three prism roles already exist and
# are scoped exactly to these jobs (prism-depth-writer is per-topic by definition).
ROLE_COMPREHEND = "researcher"
ROLE_AUDIENCE = "prism-audience-strategist"
ROLE_TOPIC = "prism-depth-writer"
ROLE_ASSEMBLE = "prism-deck-author"
ROLE_VERIFY = "reviewer"

# Parameters a run supplies via Task.workflow_params.
PARAMS = ("artifact", "who")

_TOOL_NOTE = (
    "Read ~/.okuro/TOOL-PROTOCOL.md first. Drive the prism tools yourself — you "
    "are the reasoning loop, they are your instruments. Do NOT re-implement what "
    "they do."
)


def _brief(body: str) -> str:
    return f"{body}\n\n{_TOOL_NOTE}"


def _node(node_id: str, *, y: float, phase: int, role: str, prompt: str,
          **data: Any) -> dict[str, Any]:
    """One subtask node. ``y`` is load-bearing: the compiler numbers subtasks in
    canvas order, so vertical position IS reading order."""
    # NO "type" key on purpose. The designer derives the ReactFlow node type from
    # the data (``isSubtask`` -> "subtask" | "note") and only falls back to a
    # stored ``type`` if one is present. Writing a type here that the canvas does
    # not register makes ReactFlow render its DEFAULT node — a blank white box
    # with none of the role, phase or fan-out on it. Keeping the seed free of
    # frontend node-type names also keeps the store presentation-agnostic.
    return {
        "id": node_id,
        "position": {"x": 0, "y": y},
        "data": {"kind": "subtask", "phase": phase, "role": role,
                 "prompt": _brief(prompt), **data},
    }


def prism_workflow_graph() -> dict[str, Any]:
    """The prism deck workflow as a ReactFlow graph.

    Placeholders ``{artifact}`` and ``{who}`` are filled from
    ``Task.workflow_params``; ``{item.title}`` / ``{item.id}`` come from each
    fan-out item.
    """
    nodes = [
        _node(
            "understand", y=0, phase=1, role=ROLE_COMPREHEND,
            phase_name="Understand the artifact and map the reader's topics",
            serialize=True,
            artifact_name="understanding", outputs=["understanding.md"],
            acceptance_criteria=[
                "prism_deck_understand returned a section map with at least one section",
                "The report states the document kind and its thesis",
            ],
            prompt=(
                "Comprehend okuro artifact '{artifact}' as a WHOLE before any deck "
                "exists.\n\n"
                "1. Call prism_deck_open(artifact_id='{artifact}', recipients for "
                "{who}) and keep the run_id.\n"
                "2. Call prism_deck_understand(run_id). It reads the FULL document "
                "(chunked, never truncated) and returns the section map, thesis, "
                "through-lines and open questions.\n"
                "3. Report what this document IS, what it argues, and anything the "
                "understanding got wrong or thin — you have read the source, the "
                "tool has not been audited.\n\n"
                "Do not think about slides, topics or the reader yet. This step is "
                "recipient-agnostic on purpose so the same source can serve several "
                "audiences later."
            ),
        ),
        _node(
            "recipient", y=260, phase=1, role=ROLE_AUDIENCE,
            artifact_name="recipient-brief", outputs=["recipient-brief.md"],
            acceptance_criteria=[
                "prism_deck_recipient returned a brief, or a needs_disambiguation "
                "question was surfaced instead of a guess",
                "The brief names what to lead with for this reader",
            ],
            prompt=(
                "Work out what {who} needs from this document.\n\n"
                "1. Re-open the run: prism_deck_open with the SAME artifact_id and "
                "recipients returns the same run_id and resumes.\n"
                "2. Call prism_deck_recipient(run_id). It resolves the reader from "
                "the okuro people graph (role, brand, cognitive lens through the PII "
                "firewall) and reasons what they need here.\n"
                "3. If it returns error=needs_disambiguation, STOP and surface the "
                "brand options — a multi-hat reader is a decision for the human, "
                "never a guess. Re-open with brand_pins once answered.\n"
                "4. Report what they care about, what they decide, what to lead with "
                "and what to suppress — and say which of those the profile actually "
                "supports versus what you inferred."
            ),
        ),
        _node(
            "topicmap", y=520, phase=1, role=ROLE_AUDIENCE, risk="MED",
            artifact_name="topic-map", outputs=["topic-map.md", "topic-map.json"],
            acceptance_criteria=[
                "prism_deck_topics returned at least one topic with an arc",
                "Every topic states the reader question it answers",
                "artifacts/topic-map.json exists and holds {\"topics\": [{id, title}, …]}",
            ],
            prompt=(
                "Decide the topics {who} needs, and the arc joining them.\n\n"
                "1. Call prism_deck_topics(run_id).\n"
                "2. Judge the result as an editor, not a rubber stamp: does every "
                "topic answer a question THIS reader actually has? Is the arc the "
                "right order for them? Is anything they need missing?\n"
                "3. There is NO target topic count. A rich source yields many, a memo "
                "few. Do not compress distinct themes to reach a tidy number.\n"
                "4. Topics may share sections — one fact can serve several questions. "
                "That overlap is correct; do not try to partition it away.\n"
                "5. Report the topic list with, for each, the question it answers and "
                "why it earns this reader's attention.\n"
                "6. WRITE artifacts/topic-map.json — exactly "
                '{\"topics\": [{\"id\": \"t1\", \"title\": \"…\"}, …]} in the order '
                "you decided. This file is not documentation: the orchestrator reads "
                "it to create ONE SUBTASK PER TOPIC. No file, no topic slides — the "
                "deck stops here."
            ),
        ),
        _node(
            "topic", y=780, phase=2, role=ROLE_TOPIC, risk="MED",
            phase_name="Author and design each topic",
            # THE one dynamic axis: one subtask per topic, count unknown until the
            # topic map exists. The compiler withholds this node until items
            # arrive; `items_from` is how they arrive WITHOUT a human in the loop —
            # the engine reads this artifact when phase 1 closes and expands.
            fan_out={"over": "topics",
                     "items_from": "topic-map.json",
                     "items_key": "topics"},
            artifact_name="topic-{item.id}", outputs=["topic-{item.id}.md"],
            acceptance_criteria=[
                "All four levels L1-L4 exist for topic {item.id}",
                "Each level names its chosen components with a reader-grounded reason",
                "prism_library was consulted before components were chosen",
            ],
            prompt=(
                "Build the topic '{item.title}' (id {item.id}) end to end. Carry it "
                "the whole way yourself — do not hand off between steps.\n\n"
                "1. prism_deck_open(artifact_id='{artifact}', recipients for {who}) "
                "-> run_id (resumes the existing run).\n"
                "2. prism_deck_gather(run_id, topic_id='{item.id}') — the material "
                "this topic needs, with real per-item content and verbatim evidence.\n"
                "3. prism_deck_author(run_id, topic_id='{item.id}') — the four "
                "levels, each written to its JOB:\n"
                "   L1 what the topic IS + its 3-5 key elements, hero-short.\n"
                "   L2 explains those elements. Still short.\n"
                "   L3 adds every relevant factor, still a readable slide.\n"
                "   L4 full prose, a navigable book-entry.\n"
                "   All four always exist. Thin material means SHORT levels, never "
                "missing ones.\n"
                "4. NOW choose how each level is carried. Call prism_library() for "
                "the overview and its decision guides, then REASON: which components "
                "carry this content for THIS reader, archetype or free composition, "
                "and which layout for that number and type of components. The "
                "authored blocks give you shape + real item data and deliberately no "
                "component — the choice is yours. Do NOT pick from a lookup table.\n"
                "5. Report, per level, the components you chose AND why, in the "
                "reader's terms. A choice you cannot justify for this reader is the "
                "wrong choice."
            ),
        ),
        _node(
            "assemble", y=1040, phase=3, role=ROLE_ASSEMBLE, risk="MED",
            phase_name="Assemble the deck — title, hero, index matrix",
            serialize=True,
            artifact_name="deck-assembly", outputs=["deck-assembly.md"],
            acceptance_criteria=[
                # The destination is no longer judged from this text — assemble
                # asserts it in code (deck_store.assert_landed) and refuses to
                # return an id it cannot point at a file for. This criterion now
                # asks the reviewer to REPORT the machine's evidence, so a run
                # cannot pass on a plausible sentence the way task-20260727-005417
                # did with all three of its text criteria green.
                "prism_deck_assemble returned store='deck2' and a 'path' under "
                "~/.okuro/prism-deck2/ — quote the returned path verbatim",
                "No validation defects outstanding — the save was ACCEPTED, not "
                "worked around with any other tool",
                "The title + subtext read as this reader's entry point, not a filename",
            ],
            prompt=(
                "Every topic is built. Now SAVE the deck — there is exactly one way.\n\n"
                "1. prism_deck_open with the SAME artifact_id and recipients — the "
                "run resumes with all authored topics cached.\n"
                "2. Decide the deck TITLE and one-line SUBTEXT for this reader — "
                "their entry point, not a filename.\n"
                "3. For every topic and every level L1-L3, choose ONE component per "
                "authored block by reasoning over prism_library() — which component "
                "carries this block's shape and content for THIS reader.\n"
                "4. Call prism_deck_assemble(run_id, title, subtext, components) with "
                "your full choices. It validates against the solver boundary, runs "
                "the ladder gate + accuracy accounting, composes the 37-component "
                "kit, and lands the deck at /prism/deck.\n"
                "5. If it returns defects: fix exactly what each defect names and "
                "call it again. NEVER route around it — every legacy write tool is "
                "sealed and will refuse.\n"
                "6. Report the deck id, the returned store + path (verbatim — that "
                "path IS the destination proof), title, subtext, per-level overflow "
                "flags and any gate warnings it returned."
            ),
        ),
        _node(
            "verify", y=1300, phase=4, role=ROLE_VERIFY, risk="HIGH",
            phase_name="Verify the deck against the source",
            serialize=True,
            artifact_name="faithfulness-report", outputs=["faithfulness-report.md"],
            acceptance_criteria=[
                "Every topic's levels were checked against the source artifact",
                "Each finding cites the slide and the source span",
                "Suppression and distortion are reported as distinct categories",
            ],
            prompt=(
                "Final holistic check on the assembled deck: is the content TRUE, and "
                "has it drifted from the source?\n\n"
                "1. Read the original artifact '{artifact}' — the deck is not the "
                "source of truth about itself.\n"
                "2. For every level of every topic, check each statement traces to "
                "the artifact. The gathered facts carry verbatim evidence spans; use "
                "them.\n"
                "3. Flag: invented numbers, entities the source never names, causal "
                "claims the source does not make, and any place the audience rewrite "
                "changed a FACT rather than its framing.\n"
                "4. Suppression is legitimate (detail this reader does not need); "
                "DISTORTION is not. Report the difference explicitly.\n"
                "5. Report every drift you find with the slide and the source span. A "
                "clean report with no evidence of having checked is a failure."
            ),
        ),
    ]
    edges = [
        {"id": "e-understand-recipient", "source": "understand", "target": "recipient"},
        {"id": "e-recipient-topicmap", "source": "recipient", "target": "topicmap"},
        {"id": "e-topicmap-topic", "source": "topicmap", "target": "topic"},
        {"id": "e-topic-assemble", "source": "topic", "target": "assemble"},
        {"id": "e-assemble-verify", "source": "assemble", "target": "verify"},
    ]
    return {"nodes": nodes, "edges": edges}


def seed_prism_workflow(*, overwrite: bool = False):
    """Ensure the prism workflow exists in the orchestrator's workflow store.

    Idempotent. By default an EXISTING document is left alone: once it is in the
    store a human may have edited it in the designer, and silently replacing
    their work on the next import would make the designer a lie. Pass
    ``overwrite=True`` to force the seed back to this definition.
    """
    from okuro.orchestrator.workflow_store import get_workflow, save_workflow

    existing = get_workflow(PRISM_WORKFLOW_ID)
    if existing is not None and not overwrite:
        logger.debug("prism workflow already seeded (rev %s) — leaving it alone",
                     existing.rev)
        return existing

    doc = save_workflow(
        id=PRISM_WORKFLOW_ID,
        name=PRISM_WORKFLOW_NAME,
        description=(
            "Top-down, recipient-driven prism deck build: understand the artifact, "
            "load the reader, map their topics, author four depth levels per topic "
            "and choose components by reasoning over prism_library, then assemble "
            "the hero and index matrix and check the whole thing for drift."
        ),
        graph=prism_workflow_graph(),
        origin="prism-seed",
        allow_empty=False,
    )
    logger.info("prism workflow seeded as %r (rev %d)", doc.id, doc.rev)
    return doc
