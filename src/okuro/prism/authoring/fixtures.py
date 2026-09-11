# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W3 E2E fixture — ONE invented board topic authored in the shape
#   a real mined topic has (the W2 benchmark reference shape). "contextual-
#   intelligence" (swiss/board). Every claim's source_quote is a verbatim slice of
#   the topic text, and the master doc is those slices assembled — so the L4-coverage
#   gate proves "all M in L4" against the fixture's own body text, not a doc that
#   happens to contain it. Content is FICTIONAL by design: okuro ships no customer's
#   words, figures or competitor claims.
# index:
#   REFERENCE_DIR / contextual_intelligence() / e2e_topics()
# AGENT_HEADER_END -->
"""E2E authoring fixture — one invented topic in real mined shape.

The claims below are written as if mined (each carries a ``source_quote`` slice)
and tiered by importance; the master doc is the assembled slices (the L4 reading
body + the granularity reservoir the ladder resolves down from). Deterministic —
the E2E proof reproduces without a live LLM.

The subject — a fictional smart-venue assistant for the invented vendor
``Northwind`` — exists to exercise every shape the ladder must handle: a verdict,
a five-item set, a set with a REAL content gap (four of five entries carry no
detail), an eight-item set that overflows a shallow rung, and three metric
triplets. Nothing here describes a real company, product, price or competitor.
"""
from __future__ import annotations

import os
from pathlib import Path

from okuro.prism.authoring.topic import ClaimGrounding, TopicSource
from okuro.prism.solver.schema import Claim

#: Directory holding an optional hand-authored reference render for the vision
#: judge (``proof.py --judge``). okuro ships no reference deck, so this is
#: opt-in: point ``OKURO_PRISM_REFERENCE_DIR`` at a directory containing
#: ``contextual-intelligence.html``. Absent, the judge records a render failure
#: and continues — the E2E gate never depends on it.
REFERENCE_DIR = Path(
    os.environ.get("OKURO_PRISM_REFERENCE_DIR")
    or Path(__file__).resolve().parents[1] / "kit" / "gallery"
)

# Per-item content (label, detail, meta). GROUNDING RULE: only values present in
# the source; where the source gives no per-item detail (the 4 non-Halcyon
# competitors), the field is left "" — the accuracy accounting flags it, never an
# invented value.
_CAPS = [
    ("Person Enrichment", "Research arriving guests from public sources; map personality signals to a welcome scene before they ring the bell.", "Claude Haiku 4.5"),
    ("Ambient Triggers", "Classify crowd noise, music energy, and voice density in real time. Act without a command.", "AudioTag on edge board"),
    ("Scene Personalization", "Translate enrichment signals into typed Northwind actor commands: lights, music, blinds, temperature.", "typed schema"),
    ("Competitor Gap", "Every competitor is reactive. The Concierge is not.", "moat"),
    ("Model Tier", "Right model for right task. Not the biggest model.", "routing"),
]
_COMPETITORS = [
    ("Halcyon", "Closest — HalcyonGPT 3.0, fluent natural language, on-prem for privacy; but cannot research a guest.", ""),
    ("Belltower", "", ""),
    ("Quartzline", "", ""),
    ("Fernbank", "", ""),
    ("Umbra", "", ""),
]
_DSG = [
    ("Lawful basis", "Legitimate interest — hospitality personalization using publicly available data.", "legal basis"),
    ("Non-sensitive data", "Career, public interests, cultural background — not a sensitive category.", "no sensitive data"),
    ("Deletion", "Enrichment data deleted after the visit (proportionality).", "retention"),
    ("Sensitive categories", "Health, political views, religion — never processed unless publicly shared.", "excluded"),
    ("Public data only", "Encyclopaedia, news search, press — publicly available sources.", "sourcing"),
    ("Anonymisation", "Scene template retained only if explicitly anonymised.", "retention"),
    ("Silent fallback", "No useful signals -> silent fallback to the venue default.", "graceful"),
    ("Counsel sign-off", "Recommend privacy counsel sign-off before production.", "governance"),
]
_COST = [
    ("Person enrichment", "CHF 0.58/mo", "24 guests × CHF 0.024/task"),
    ("Ambient (AudioTag)", "~CHF 0", "3.7 MB, <12 ms, zero cloud cost"),
    ("Model tier", "Haiku 4.5", "right model for right task"),
]
_LATENCY = [
    ("AudioTag classify", "<12 ms", "400 audio classes on an edge board"),
    ("Party mode", "20 min", "crowd noise sustained ≥0.75"),
    ("Context window", "17K tokens", "8.5% of Haiku 200K"),
]
_CTX = [
    ("T2 research task", "17K tokens", "worst case"),
    ("Headroom", "182K tokens", "left free on Haiku 200K"),
    ("Fits 32K models", "yes", "small-context class"),
]
_AMBIENT = [
    ("Detect", "Crowd noise rising over 20 minutes", "AudioTag"),
    ("Threshold", "≥0.75 score sustained 15 min", "no single spikes"),
    ("Act", "Triggers party mode autonomously", "host can disable"),
]

# (claim_id, text=authored label, shape, tier, items, source_quote, items_content)
_CI_CLAIMS: list[tuple] = [
    ("moat", "Commands are commodity. Context is the advantage.", "verdict", 0, 1,
     "Commands are commodity.Context is the advantage.", ()),
    ("caps", "Five capabilities no competitor has", "set", 0, 5,
     "Five capabilities.Zero competitors have any of them.", _CAPS),
    ("competitors", "Halcyon, Belltower, Quartzline, Fernbank, Umbra", "set", 1, 5,
     "Halcyon, Belltower, Quartzline, Fernbank, Umbra — what they do, what they miss, where Northwind wins.", _COMPETITORS),
    ("cost", "CHF 0.58/month for full person enrichment", "metric", 1, 3,
     "24 guests × 1 research task × CHF 0.024/task = CHF 0.58/month for complete person enrichment.", _COST),
    ("latency", "400 audio classes in under 12 ms on edge", "metric", 1, 3,
     "AudioTag classifies 400 audio event classes in under 12 ms on an edge board.", _LATENCY),
    ("dsg", "Privacy safeguards under house policy", "set", 1, 8,
     "Legitimate interest is the lawful basis. Hospitality personalization for arriving guests using publicly available data.", _DSG),
    ("model", "Right model for right task. Not the biggest model.", "verdict", 2, 1,
     "Right model for right task. Not the biggest model.", ()),
    ("ctx", "17K tokens = 8.5% of Haiku 4.5's 200K window", "metric", 2, 3,
     "17K tokens = 8.5% of Claude Haiku 4.5's 200K context window. Even the most research-heavy query leaves 182K tokens headroom.", _CTX),
    ("summary", "All five achievable within the Belvedere budget", "narrative", 2, 1,
     "Person enrichment, ambient intelligence, scene personalization, compliant data handling, and open MCP architecture are all achievable within the Belvedere budget.", ()),
    ("ambient", "Crowd noise over 20 min triggers party mode autonomously", "relationship", 2, 3,
     "Crowd noise rising over 20 minutes triggers party mode autonomously.", _AMBIENT),
]


def contextual_intelligence() -> TopicSource:
    claims: dict[str, Claim] = {}
    grounding: dict[str, ClaimGrounding] = {}
    for cid, text, shape, tier, items, quote, content in _CI_CLAIMS:
        claims[cid] = Claim(cid, text, shape, tier=tier, items=items, chars=len(text),
                            items_content=tuple(content))
        grounding[cid] = ClaimGrounding(source_quote=quote)
    # master doc = the assembled verbatim slices (every source_quote is a substring).
    master_doc = "\n\n".join(row[5] for row in _CI_CLAIMS)
    return TopicSource(
        topic_id="contextual-intelligence",
        title="Commands are commodity. Context is the advantage.",
        claims=claims, master_doc=master_doc, grounding=grounding,
        content_character="analytical", audience_mood="board",
    )


# Optional reference slide (the L2-density comparator) for the level-matched judge.
# Absent by default — see REFERENCE_DIR.
def reference_html() -> Path:
    return REFERENCE_DIR / "contextual-intelligence.html"


def e2e_topics() -> list[TopicSource]:
    return [contextual_intelligence()]


__all__ = [
    "REFERENCE_DIR", "contextual_intelligence", "reference_html", "e2e_topics",
]
