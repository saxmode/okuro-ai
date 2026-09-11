# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance shared grounding — deterministic context from specific
#   artifacts (the docs ingested for a goal). Used by the PCO builder + gap engine.
# index: def context_from_artifacts
# AGENT_HEADER_END -->
"""Deterministic grounding for the Resonance stages.

The intake→prepare and intake→analyze edges must ground on the EXACT documents
ingested for a goal — not a fuzzy brain-wide semantic sweep (which ranks
established artifacts above a just-ingested one). Every stage that reads the
assembled context goes through here.
"""

from __future__ import annotations

from typing import Optional


def context_from_artifacts(artifact_ids: list[str], budget: int = 9000
                           ) -> tuple[str, list[str]]:
    """Assemble markdown context from specific artifact bodies.

    Returns (context_markdown, source_labels). Missing/empty artifacts are
    skipped. Each artifact gets an equal char slice (min 1500).
    """
    from okuro.sense.artifacts import artifact_get

    parts: list[str] = []
    labels: list[str] = []
    per = max(budget // max(len(artifact_ids), 1), 1500)
    for aid in artifact_ids or []:
        a = artifact_get(aid)
        if isinstance(a, dict) and (a.get("body") or "").strip():
            parts.append(
                f"## {a.get('kind', 'evidence')}: {a.get('title', '')}\n"
                f"{a['body'][:per]}")
            labels.append(f"artifact:{(a.get('title') or aid)[:48]}")
    return "\n\n".join(parts), labels
