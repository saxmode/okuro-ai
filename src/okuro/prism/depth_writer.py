# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism Depth Writer — for one topic, author the four-level depth
#   ladder L1→L4 (what → how → how-exactly → full detail) as ONE coherent superset,
#   grounded in that topic's claims, up to the audience depth ceiling. Stage 4 of
#   the root-doc→deck pipeline. Implements the prism-depth-writer role charter.
# index: _WRITER_SYSTEM | write_ladder
# AGENT_HEADER_END -->
"""Prism Depth Writer.

Fourth pipeline stage: for a single topic it writes the four levels L1→L4 as one
coherent PROGRESSIVE-DEPTH ladder — each level a superset of the one before —
grounded strictly in that topic's claims, in the audience's voice, and only as
deep as the audience brief's depth ceiling (a board stops at L2; an engineer
reaches L4). Writes prose only; components come later.

Mirrors the ``prism-depth-writer`` charter (roles/catalog).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.depth_writer")

from okuro.prism.rungs import RUNGS as _RUNGS


def _expertise_clause(jargon: str) -> str:
    """Structural (not merely lexical) expertise adaptation for the ladder
    (Kalyuga expertise-reversal): an EXPERT reader wants the basics OMITTED and
    redundancy STRIPPED (redundant explanation adds load for them); a NON-EXPERT
    wants SCAFFOLDING kept. Keyed off the strategist's jargon level; '' for
    balanced. This gives the Build path the structural expertise adaptation the
    Generate path gets from audience.narrative_clause."""
    if jargon == "technical":
        return (
            "EXPERTISE (expert reader): OMIT fundamentals they already know and STRIP "
            "restated context / step-by-step scaffolding — for an expert, redundant "
            "explanation only ADDS cognitive load. Spend the reclaimed room on DATA "
            "DENSITY: the specific figures, named mechanisms, parameters, failure modes "
            "and non-obvious trade-offs from the deeper-tier claims — more distinct FACTS, "
            "not more words about the same fact."
        )
    if jargon == "plain":
        return (
            "EXPERTISE (non-expert reader): SCAFFOLD — introduce each new term before "
            "using it, build from the familiar to the new, and KEEP the worked-through "
            "explanation. Do NOT compress; a novice learns from guidance, not brevity."
        )
    return ""


_WRITER_SYSTEM = (
    "You are okuro·prism's DEPTH WRITER. For ONE topic you write its four rungs as a "
    "single coherent PROGRESSIVE-DEPTH ladder, grounded strictly in that topic's CLAIMS. "
    "Output ONLY a JSON object.\n\n"
    "DEPTH MEANS MORE DATA, NOT MORE WORDS. Each rung is given its OWN set of NEW claims "
    "(tiered by granularity). A deeper rung must EARN its depth by introducing THOSE new "
    "claims — finer figures, named mechanisms, edge cases — while carrying forward the "
    "substance above. Re-stating a shallower point at greater length is the cardinal DEFECT.\n\n"
    "The ladder — four levels L1→L4 (each introduces its NEW claims on top of the one before):\n"
    "  L1 = WHAT it is/solves — the outcome in ONE punchy BLUF sentence (headline claim). This "
    "is the COVER line: it must stand alone as a single memorable idea.\n"
    "  L2 = HOW — the approach/shape, adding the supporting claims (the key points).\n"
    "  L3 = HOW EXACTLY — the mechanism and the SPECIFIC figures/parameters (detail claims).\n"
    "  L4 = THE HARD EDGES — edge cases, failure modes, boundary conditions and "
    "non-obvious trade-offs (edge claims); the densest, most specific level.\n\n"
    "HARD RULES:\n"
    "  1. Every sentence must trace to a claim; NEVER invent facts, figures, or names.\n"
    "  2. Each level must INTRODUCE the NEW claims listed for it. A level that adds words but "
    "no NEW claim over the level above is a DEFECT — cut it back, do not pad.\n"
    "  3. Anti-padding: do not re-explain a shallower point at greater length; if a level has "
    "few new claims, keep it SHORT — brevity beats restatement.\n"
    "  4. STOP at the DEPTH CEILING: write levels up to and including it; leave DEEPER levels "
    "as empty strings. (ceiling=L2 → fill L1+L2, leave L3+L4 empty.)\n"
    "  5. Apply the audience JARGON, TONE and ANGLE. Make the L1 line memorable, not generic.\n"
    "  6. Prose only — no markdown headings, no bullet dumps unless the claims are a list.\n\n"
    'Return exactly: {"L1":str,"L2":str,"L3":str,"L4":str}. Rungs beyond the '
    "ceiling are empty strings."
)


_TIER_IDX = {"headline": 0, "supporting": 1, "detail": 2, "edge": 3}


def _partition_claims_by_rung(
    topic_claims: list[dict[str, Any]], ceil_idx: int
) -> dict[str, list[dict[str, Any]]]:
    """Assign each claim to the ONE rung where it first surfaces = min(its
    granularity tier, the depth ceiling). Capping at the ceiling means a low
    ceiling (e.g. a board's brief) folds the finer claims UP rather than dropping
    them, so no data is lost — the deeper rungs simply carry the finer tiers when
    they exist. Returns {rung: [claims introduced there]}."""
    alloc: dict[str, list[dict[str, Any]]] = {r: [] for r in _RUNGS}
    for c in topic_claims:
        tier = _TIER_IDX.get(c.get("granularity"), 1)
        alloc[_RUNGS[min(tier, ceil_idx)]].append(c)
    return alloc


def write_ladder(
    topic: dict[str, Any],
    claims: list[dict[str, Any]],
    *,
    brief: dict[str, Any],
    feedback: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict[str, str]:
    """Author the 4-rung ladder for one topic.

    ``topic`` = {title, claim_ids}; ``claims`` = the full claim list; ``brief`` =
    the audience brief (uses depth_ceiling, jargon, angle, tone, takeaway).
    ``feedback`` (from a Critic redo) is folded in as a fix-these instruction.
    Returns {L1, L2, L3, L4} — levels beyond the ceiling are ''.
    """
    title = (topic.get("title") or "").strip()
    ids = set(topic.get("claim_ids") or [])
    topic_claims = [c for c in claims if c.get("id") in ids and (c.get("statement") or "").strip()]
    if not title or not topic_claims:
        return {r: "" for r in _RUNGS}

    ceiling = brief.get("depth_ceiling") if brief.get("depth_ceiling") in _RUNGS else "L4"
    ceil_idx = _RUNGS.index(ceiling)

    # Per-rung claim allocation (granularity → rung) — the reservoir that lets a
    # deeper rung add DATA, not prose. Each rung's block lists ONLY the claims it
    # introduces; the writer carries forward the substance above and adds these.
    alloc = _partition_claims_by_rung(topic_claims, ceil_idx)
    rung_blocks = []
    for i, r in enumerate(_RUNGS):
        if i > ceil_idx:
            break
        cs = alloc[r]
        body = "\n".join(f"  - {c['statement'].strip()}" for c in cs) if cs else "  (no new claims — keep this rung SHORT, do not pad)"
        rung_blocks.append(f"[{r}] NEW claims to introduce here:\n{body}")
    claim_alloc_block = "\n\n".join(rung_blocks)

    exp_clause = _expertise_clause(brief.get("jargon") or "balanced")
    user = (
        f"TOPIC: {title}\n\n"
        f"DEPTH CEILING: {ceiling} (write rungs up to and including this; leave deeper rungs empty)\n"
        f"JARGON: {brief.get('jargon') or 'balanced'}  ·  TONE: {brief.get('tone') or 'clear'}  ·  "
        f"ANGLE: {brief.get('angle') or '-'}\n"
        f"DECK TAKEAWAY (frame toward it): {brief.get('takeaway') or '-'}\n"
        + (f"{exp_clause}\n" if exp_clause else "")
        + "\nCLAIM ALLOCATION — each rung INTRODUCES its listed claims (its only source) on "
        "top of the substance above. Ground every sentence in a claim; never invent:\n\n"
        + f"{claim_alloc_block}\n\n"
        + (f"FIX THESE issues from the previous attempt:\n{feedback}\n\n" if feedback else "")
        + "Write the ladder. Return ONLY the JSON object."
    )

    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "0"
    try:
        res = invoke(
            prompt=user, system_prompt=_WRITER_SYSTEM, provider=provider,
            capability="standard", timeout=240,
        )
    finally:
        if _prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = _prev

    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")

    out = {r: "" for r in _RUNGS}
    try:
        data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
        if isinstance(data, dict):
            for r in _RUNGS:
                v = data.get(r)
                if isinstance(v, str):
                    out[r] = v.strip()
    except (ValueError, KeyError) as exc:
        logger.warning("write_ladder(%s): parse failed: %s", title, exc)

    # Enforce the ceiling in code — never trust the model to stop on its own.
    for i, r in enumerate(_RUNGS):
        if i > ceil_idx:
            out[r] = ""
    return out
