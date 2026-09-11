# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism TOP-DOWN WORKFLOW package — the recipient-driven deck loop
#   that orchestrates prism's existing primitives (solver render, compose_ladder,
#   faithfulness gate, prism_library, studio, matrix) instead of rebuilding them.
#   Public surface: PrismWorkflow / run_phase_b + the workflow IR.
# index: PrismWorkflow | run_phase_b | NODE_ORDER | Understanding | RecipientBrief
#   | TopicMap
# AGENT_HEADER_END -->
"""okuro·prism top-down, recipient-driven deck workflow.

The compiler (``okuro.prism.compiler``) is bottom-up: shred the source into atomic
claims, partition the claims into topics, build depth by claim density, pick
components from a fixed fit-table. This package is the inversion of all four —
understand the document whole, load the recipient, reason out the topics they
need, author each depth level by the JOB that level does, and choose components
by REASONING over ``prism_library``.

It targets engine B (``deck2`` / ``compile_authored_deck_doc`` -> ``/prism/deck``)
only. The legacy facet-tree engine A is never touched.

Implemented: **Phase B** — understand -> recipient -> topicmap. **Phase C** —
gather -> author (four levels BY JOB; no component is chosen here).
Planned: Phase D (component selection by reasoning over ``prism_library``, then
``solve()`` + ``compose_slide_html``), Phase E (assemble + hero + matrix +
holistic faithfulness check).
"""

from typing import TYPE_CHECKING

from okuro._lazy import install as _install

if TYPE_CHECKING:  # static analysers still see the full surface
    from okuro.prism.workflow.run import (
        NODE_ORDER,
        PrismWorkflow,
        run_phase_b,
        run_phase_c,
    )
    from okuro.prism.workflow.types import (
        AuthoredBlock,
        AuthoredLevel,
        AuthoredTopic,
        GatheredFact,
        L4Doc,
        RecipientBrief,
        Section,
        TopicGathering,
        TopicMap,
        Understanding,
        WorkflowTopic,
    )

# Deferred: importing .run pulls the whole prism compiler (31 files) into every
# process that merely registers the MCP tool surface. See okuro._lazy.
_RUN = "okuro.prism.workflow.run"
_TYPES = "okuro.prism.workflow.types"
_EXPORTS = {
    "NODE_ORDER": (_RUN, "NODE_ORDER"),
    "PrismWorkflow": (_RUN, "PrismWorkflow"),
    "run_phase_b": (_RUN, "run_phase_b"),
    "run_phase_c": (_RUN, "run_phase_c"),
    **{n: (_TYPES, n) for n in (
        "AuthoredBlock", "AuthoredLevel", "AuthoredTopic", "GatheredFact",
        "L4Doc", "RecipientBrief", "Section", "TopicGathering", "TopicMap",
        "Understanding", "WorkflowTopic",
    )},
}

__getattr__, __dir__ = _install(globals(), _EXPORTS)

__all__ = [
    "NODE_ORDER",
    "PrismWorkflow",
    "run_phase_b",
    "run_phase_c",
    "AuthoredBlock",
    "AuthoredLevel",
    "AuthoredTopic",
    "GatheredFact",
    "L4Doc",
    "RecipientBrief",
    "Section",
    "TopicGathering",
    "TopicMap",
    "Understanding",
    "WorkflowTopic",
]
