### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.sense.interaction — mine agent transcripts for interaction problems, record findings, tier the traces.
# index: imports | re-exports
# AGENT_HEADER_END -->
"""Interaction analysis — find problems in how humans and agents actually talk.

The pipeline this package implements, end to end::

    log  →  bridge  →  turns  →  detect  →  analyze  →  record  →  compact

``log`` already exists (:mod:`okuro.trace`, daemon task ``trace-ingest``,
every 10 minutes). Everything downstream of it lives here.

Why it exists
-------------
:mod:`okuro.sense.retros` scores *agent compliance*: did it call bootstrap, did
it call session_report. That measures whether the agent followed protocol. It
cannot see whether the human had to repeat themselves, correct a wrong answer
three times, re-explain context okuro was supposed to have served, or abandon
the session outright. Those are the failures that actually cost the user time,
and they are legible only in the transcript.

The okuro charter names this directly: *"kill re-explanation and ephemeral
work"*. Until now nothing measured whether that was working. This package does.

Two populations, two questions
------------------------------
Roughly half of all traced sessions are subagent runs (``session_id`` prefixed
``agent-``), where the "user" turn is a parent agent's brief rather than a
human. Both populations matter, and they fail differently:

``human-facing``
    Is the human fighting the agent? Corrections, repeats, re-explanation,
    frustration, abandonment, rejected deliverables.

``subagent``
    Did the subagent do what its brief asked? Under-specified briefs,
    scope drift, truncated returns, missing close-out.

:mod:`.detect` runs the matching detector family per population; findings from
both land in the same table with a ``session_kind`` dimension so they can be
sliced together or apart.

Storage discipline
------------------
Nothing here copies transcript text. Human turns stay in ``agent_events``
(2.8% of the corpus, never compacted); this package writes only the join, the
derived markers, the findings, and the tier state. See migration
``113_interaction_analysis.sql``.
"""

from __future__ import annotations

from okuro.sense.interaction.bridge import bridge_sessions, resolve_telemetry
from okuro.sense.interaction.detect import DETECTORS, scan_sessions
from okuro.sense.interaction.lifecycle import compact_traces, tier_report
from okuro.sense.interaction.turns import embed_turns, iter_turns

__all__ = [
    "bridge_sessions",
    "resolve_telemetry",
    "DETECTORS",
    "scan_sessions",
    "iter_turns",
    "embed_turns",
    "compact_traces",
    "tier_report",
]
