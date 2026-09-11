# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W3 — the authoring INPUT model. A TopicSource is one topic
#   mined from an okuro artifact: a pool of tiered Claims (real authored content —
#   the LLM/human content side), per-claim grounding (the verbatim source slice +
#   synthesized/fallback flags), the L4 MASTER DOC (the topic's full artifact text,
#   the granularity reservoir depth resolves DOWN from — pre-check REC-2), and the
#   content-character / audience-mood signals that pick the grid family (answer 7).
# index:
#   ClaimGrounding / TopicSource / mine helpers (grounded fixture + bridge hook)
# AGENT_HEADER_END -->
"""The authoring input: a mined topic.

The resolution ladder is resolved DOWNWARD from ``master_doc`` (pre-check REC-2:
deeper levels add real detail EXTRACTED from the L4 master, never prose-padding of
flat claims). Every mined claim carries a ``source_quote`` — a verbatim slice of
the artifact — so the L4-coverage gate can prove "all M in L4" is a fact, not an
assertion (pre-check REQ-1). Claims the author synthesises (a headline title, a
fallback cell) are flagged and exempt from verbatim grounding but surfaced by the
accuracy accounting so nothing is silently invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from okuro.prism.solver.schema import Claim


@dataclass(frozen=True)
class ClaimGrounding:
    """Provenance for one claim. ``source_quote`` is a verbatim slice of the
    artifact (present in the master doc); ``synthesized`` = authored chrome not
    mined from the source (title/kicker); ``fallback`` = a fail-soft placeholder."""

    source_quote: str = ""
    synthesized: bool = False
    fallback: bool = False


@dataclass
class TopicSource:
    """One topic to author into an L1-L4 ladder.

    ``claims`` are the immutable information atoms (tier 0 = most important).
    ``master_doc`` is the topic's full text VERBATIM from the artifact — the L4
    reading body and the granularity reservoir. ``grounding`` maps claim id ->
    provenance; a mined claim (not synthesized) MUST have its source_quote present
    in master_doc (the L4-coverage gate enforces this).
    """

    topic_id: str
    title: str
    claims: dict[str, Claim]
    master_doc: str
    grounding: dict[str, ClaimGrounding] = field(default_factory=dict)
    content_character: str = "analytical"
    audience_mood: str = "technical"

    def ground(self, claim_id: str) -> ClaimGrounding:
        return self.grounding.get(claim_id, ClaimGrounding())

    def mined_claim_ids(self) -> list[str]:
        """Claim ids that are MINED (not synthesized) — the M in 'N of M'. Stable
        order (tier, id)."""
        ids = [cid for cid in self.claims if not self.ground(cid).synthesized]
        return sorted(ids, key=lambda c: (self.claims[c].tier, c))

    def tier0_headline(self) -> str | None:
        """The most-important narrative/verdict claim id — the natural title. None
        if the topic has no tier-0 headline-shaped claim (author synthesises one)."""
        cands = [
            cid for cid, c in self.claims.items()
            if c.tier == 0 and c.shape in ("narrative", "verdict")
        ]
        if not cands:
            return None
        return sorted(cands, key=lambda c: (self.claims[c].shape != "narrative", c))[0]


__all__ = ["ClaimGrounding", "TopicSource"]
