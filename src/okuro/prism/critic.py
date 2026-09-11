# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism Critic — the review gate of the pipeline. Verifies
#   faithfulness (every statement traces to a claim) and ladder coherence (each
#   rung a superset of the one before) and returns concrete defects. Stage 7 of
#   the root-doc→deck pipeline. Implements the prism-critic role charter.
# index: critique_faithfulness | critique_ladder
# AGENT_HEADER_END -->
"""Prism Critic / QA.

The gate that makes the pipeline an orchestrator rather than a prompt chain. It
produces no deck content — it verifies produced content against the ground-truth
claims and returns actionable defects. Two core gates implemented here:

* ``critique_faithfulness`` — LLM-judged: any statement not supported by the
  claims is a hallucination defect.
* ``critique_ladder`` — is each rung a superset of the previous (adds detail,
  no restatement/contradiction, density rising)?

Uses opus (capability="quality") — the critic is the one stage worth the tier.
Mirrors the ``prism-critic`` charter (roles/catalog).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.critic")

from okuro.prism.rungs import RUNGS as _RUNGS

_FAITHFUL_SYSTEM = (
    "You are okuro·prism's CRITIC on the FAITHFULNESS gate. You are given a set of CLAIMS "
    "(the only permitted ground truth) and a TEXT built from them. Find every statement in "
    "the TEXT that is NOT supported by the claims — an invented fact, figure, name, or "
    "capability. A faithful paraphrase or a reasonable summary of claims is SUPPORTED; only "
    "flag genuine additions the claims do not back. Output ONLY a JSON object.\n\n"
    'Return: {"verdict":"pass"|"redo","unsupported":[{"statement":str,"why":str} ...],'
    '"note":str}. verdict is "redo" if any unsupported statement is a real fact/figure/'
    "name (not mere framing); else \"pass\". Be strict on invented NUMBERS and NAMES."
)

_LADDER_SYSTEM = (
    "You are okuro·prism's CRITIC on the LADDER gate. You are given a topic's four levels "
    "(L1, L2, L3, L4). Verify they form a PROGRESSIVE-DEPTH ladder where "
    "DEPTH MEANS MORE DATA, NOT MORE WORDS. Judge each non-empty rung against the one "
    "above it. Empty rungs (above the audience's depth ceiling) are fine — not a defect. "
    "Output ONLY a JSON object.\n\n"
    "Flag a rung (verdict redo) when ANY holds:\n"
    "  - PADDING: it adds words but few or no NEW facts/figures/mechanisms over the rung "
    "above — it re-explains the same points at greater length (the cardinal defect);\n"
    "  - RESTATEMENT: it largely repeats the prior rung's content;\n"
    "  - CONTRADICTION: it conflicts with a shallower rung;\n"
    "  - INVERSION: it is thinner / carries less substance than the rung above it.\n"
    "A deeper rung EARNS its place only by introducing genuinely NEW, finer information "
    "(specific figures, named mechanisms, edge cases, failure modes). A short rung with a "
    "few new facts is BETTER than a long one that pads — do not flag brevity itself.\n\n"
    'Return: {"verdict":"pass"|"redo","issues":[{"rung":str,"issue":str} ...],"note":str}. '
    'issue names WHAT new substance the rung should add (or what padding to cut).'
)


_SCENARIO_SYSTEM = (
    "You are okuro·prism's CRITIC on the SCENARIO gate. You are given the CLAIMS (ground "
    "truth) and a running SCENARIO (a case + per-topic beats) that threads through the deck. "
    "A scenario is NARRATIVE: invented character names, specific times, offices, and concrete "
    "everyday moments are EXPECTED and fine — DO NOT flag them. Judge ONLY whether the "
    "scenario is PLAUSIBLE given the claims and does not CONTRADICT them or assert a "
    "CAPABILITY/OUTCOME the claims deny or clearly do not support (e.g. a feature the system "
    "does not have, a wrong domain). Output ONLY a JSON object.\n\n"
    'Return: {"verdict":"pass"|"redo","contradictions":[{"beat":str,"conflict":str} ...],'
    '"note":str}. verdict is "redo" ONLY if the scenario contradicts a claim or invents a '
    "capability/domain the claims do not support; a merely embellished but consistent story PASSES."
)


def _judge(system: str, user: str, provider: Optional[str], *, thinking: int = 0) -> dict[str, Any]:
    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = str(thinking)
    try:
        res = invoke(prompt=user, system_prompt=system, provider=provider, capability="quality", timeout=240)
    finally:
        if _prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = _prev
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")
    try:
        data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
        return data if isinstance(data, dict) else {}
    except (ValueError, KeyError) as exc:
        logger.warning("critic: parse failed: %s", exc)
        return {}


def critique_faithfulness(
    text: str, claims: list[dict[str, Any]], *, provider: Optional[str] = None
) -> dict[str, Any]:
    """Flag statements in ``text`` not supported by ``claims``. Returns
    {passed, unsupported[], note}."""
    text = (text or "").strip()
    stmts = [c.get("statement", "").strip() for c in (claims or []) if (c.get("statement") or "").strip()]
    if not text or not stmts:
        return {"passed": True, "unsupported": [], "note": "empty input"}
    claim_block = "\n".join(f"- {s}" for s in stmts)
    user = f"CLAIMS (ground truth):\n{claim_block}\n\nTEXT to check:\n{text}\n\nReturn ONLY the JSON object."
    data = _judge(_FAITHFUL_SYSTEM, user, provider)
    unsupported = [u for u in (data.get("unsupported") or []) if isinstance(u, dict)]
    return {
        "passed": data.get("verdict") != "redo" and not unsupported,
        "unsupported": unsupported,
        "note": (data.get("note") or "").strip(),
    }


def critique_scenario(
    case: str, beats: list[str], claims: list[dict[str, Any]], *, provider: Optional[str] = None
) -> dict[str, Any]:
    """Judge a running scenario by PLAUSIBILITY (not literal fact-matching): does it
    contradict the claims or invent a capability/domain they don't support? Narrative
    embellishment is allowed. Returns {passed, contradictions[], note}."""
    case = (case or "").strip()
    beats = [b.strip() for b in (beats or []) if (b or "").strip()]
    stmts = [c.get("statement", "").strip() for c in (claims or []) if (c.get("statement") or "").strip()]
    if not case or not stmts:
        return {"passed": True, "contradictions": [], "note": "empty scenario or claims"}
    claim_block = "\n".join(f"- {s}" for s in stmts)
    beat_block = "\n".join(f"- {b}" for b in beats)
    user = f"CLAIMS (ground truth):\n{claim_block}\n\nSCENARIO CASE:\n{case}\n\nBEATS:\n{beat_block}\n\nReturn ONLY the JSON object."
    data = _judge(_SCENARIO_SYSTEM, user, provider)
    contradictions = [c for c in (data.get("contradictions") or []) if isinstance(c, dict)]
    return {
        "passed": data.get("verdict") != "redo" and not contradictions,
        "contradictions": contradictions,
        "note": (data.get("note") or "").strip(),
    }


_CRAFT_SYSTEM = (
    "You are okuro·prism's CRITIC on the CRAFT gate. You are given a DECK SUMMARY: each "
    "facet's title, a FIT score (0-1, how well its modules match their claim shapes — lower "
    "is worse; '?' means unscored), and, per rung, the list of visual MODULE TYPES the "
    "arranger chose (or 'prose-only'). You judge VISUAL CRAFT, not facts. A board-grade deck "
    "EARNS a DISTINCT visual form per topic, gives each facet ONE clear focal point, and "
    "carries at least one high-impact relational/interactive module across the whole deck "
    "(its 'hero' that proves depth). Output ONLY a JSON object.\n\n"
    "Flag a facet (add it to weak_facets) when ANY holds AND its content plausibly supports "
    "a better form:\n"
    "  - its DEEPEST rung is prose-only, or a single thin module (a lone stat/quote) where "
    "the title implies structured content;\n"
    "  - it repeats the SAME module shape as most other facets (visual SAMENESS — e.g. every "
    "facet is stat+table);\n"
    "  - a topic about relationships / architecture / flow / options / a decision / risks / "
    "trade-offs shows NONE of graph, diagram, flow, lens, options, decisionrecord, risk, "
    "compare, matrix;\n"
    "  - NO FOCAL POINT: its deepest rung stacks 3+ co-equal heavy modules (e.g. table + "
    "matrix + chart + options) with no single dominant one — a screen with everything "
    "shouting has no hero; suggest promoting ONE and demoting or splitting the rest;\n"
    "  - LOW FIT: its fit score is below 0.5 — a module is fighting its claim shape (e.g. a "
    "chart where the point is one number); suggest the form the shape wants.\n\n"
    "DECK-LEVEL: set deck_missing_hero=true if NOT ONE facet, on any rung, uses any of "
    "{graph, diagram, flow, lens, simulator, smallmultiples}. Never invent data — only flag "
    "facets whose CONTENT (per its title/claims) would support the better module; a genuinely "
    "simple topic may stay simple, and one strong module is a valid focal point.\n\n"
    'Return: {"verdict":"pass"|"redo","weak_facets":[{"facet_id":str,"issue":str,'
    '"suggest":str} ...],"deck_missing_hero":bool,"note":str}. suggest NAMES the module '
    'type(s) the facet content would support. verdict is "redo" if weak_facets is non-empty '
    "or deck_missing_hero is true; else \"pass\"."
)


def critique_craft(
    deck_summary: list[dict[str, Any]], *, provider: Optional[str] = None
) -> dict[str, Any]:
    """Judge a built deck's VISUAL CRAFT: per-topic distinctness, module richness on
    the deepest rung, and presence of at least one 'hero' relational/interactive module.
    ``deck_summary`` = [{facet_id, title, rungs:{rung:[block_type,...]}} ...]. Returns
    {passed, weak_facets[], missing_hero, note}. Content-truth is NOT judged here."""
    facets = [f for f in (deck_summary or []) if isinstance(f, dict) and f.get("facet_id")]
    if len(facets) < 2:
        return {"passed": True, "weak_facets": [], "missing_hero": False, "note": "too few facets to judge craft"}
    lines = []
    for f in facets:
        rungs = f.get("rungs") or {}
        rung_txt = "; ".join(
            f"{r}: {', '.join(rungs[r]) if rungs.get(r) else 'prose-only'}"
            for r in _RUNGS if r in rungs
        )
        fit = f.get("fit")
        fit_txt = f"{fit:.2f}" if isinstance(fit, (int, float)) else "?"
        lines.append(f"- [{f['facet_id']}] {f.get('title', '')} (fit {fit_txt}) — {rung_txt}")
    user = "DECK SUMMARY (facet — per-rung module types):\n" + "\n".join(lines) + "\n\nReturn ONLY the JSON object."
    # Craft is a judgment call across the whole deck — give it a real thinking budget.
    data = _judge(_CRAFT_SYSTEM, user, provider, thinking=8000)
    weak = [w for w in (data.get("weak_facets") or []) if isinstance(w, dict) and w.get("facet_id")]
    missing_hero = bool(data.get("deck_missing_hero"))
    return {
        "passed": data.get("verdict") != "redo" and not weak and not missing_hero,
        "weak_facets": weak,
        "missing_hero": missing_hero,
        "note": (data.get("note") or "").strip(),
    }


_ENTAILMENT_SYSTEM = (
    "You are okuro·prism's CRITIC on the ADAPTATION-ENTAILMENT gate. Audience adaptation "
    "(re-wording a finished deck for a different recipient / density / jargon / language) MUST "
    "be a NON-DESTRUCTIVE overlay on an immutable truth: it may re-frame, compress, simplify, "
    "reorder, omit, or translate, but it must NOT introduce a fact, figure, number, name, date, "
    "or claim the SOURCE text does not already support, and must not STRENGTHEN a hedged claim "
    "into a certain one. You are given SOURCE (the trusted, pre-adaptation text) and ADAPTED "
    "(after re-tailoring, possibly in another language). Find every fact ADAPTED asserts that "
    "SOURCE does not entail. Re-wording, reordering, omission, plain-language substitution, and "
    "translation are NOT defects — only ADDED or ALTERED facts are. Output ONLY a JSON object.\n\n"
    'Return: {"verdict":"pass"|"redo","added":[{"fact":str,"why":str} ...],"note":str}. '
    'verdict is "redo" if ADAPTED asserts any fact/figure/name SOURCE does not support (or '
    "over-states its certainty); else \"pass\". Be strict on invented NUMBERS and NAMES."
)


def critique_entailment(
    source_text: str, adapted_text: str, *, provider: Optional[str] = None
) -> dict[str, Any]:
    """Verify an audience-adapted deck is ENTAILED by its pre-adaptation source —
    the orthogonality invariant's re-verify step. Flags any fact ADAPTED asserts
    that SOURCE does not support (re-wording / omission / translation are fine).
    Returns {passed, added[], note}."""
    source_text = (source_text or "").strip()
    adapted_text = (adapted_text or "").strip()
    if not source_text or not adapted_text:
        return {"passed": True, "added": [], "note": "empty input"}
    user = (
        f"SOURCE (trusted ground truth):\n{source_text}\n\n"
        f"ADAPTED (check this for added/altered facts):\n{adapted_text}\n\n"
        "Return ONLY the JSON object."
    )
    data = _judge(_ENTAILMENT_SYSTEM, user, provider)
    added = [a for a in (data.get("added") or []) if isinstance(a, dict)]
    return {
        "passed": data.get("verdict") != "redo" and not added,
        "added": added,
        "note": (data.get("note") or "").strip(),
    }


def sibling_sameness(
    deck_summary: list[dict[str, Any]], *, max_share: float = 0.34
) -> dict[str, str]:
    """Deterministic deck-level anti-sameness: flag facets whose DEEPEST rung is
    LED by a module type that already leads too many sibling facets (the exact
    'decisionrecord leads 7/9 L4 levels' defect the per-facet craft gate can't
    see). Returns {facet_id: overused_type} for the EXCESS facets (the first
    ``max_share`` share that lead with a type are kept; the rest are flagged to
    rotate). Empty when variety is fine or there are <3 facets."""
    from collections import Counter

    leads: dict[str, str] = {}
    for f in deck_summary:
        fid = f.get("facet_id")
        rungs = f.get("rungs") or {}
        deep = next((rungs[r] for r in reversed(_RUNGS) if rungs.get(r)), None)
        if fid and deep:
            leads[fid] = deep[0]
    n = len(leads)
    if n < 3:
        return {}
    counts = Counter(leads.values())
    allowed = max(1, int(n * max_share))
    flagged: dict[str, str] = {}
    for t, c in counts.items():
        if c >= 2 and c / n > max_share:
            same = [fid for fid, lt in leads.items() if lt == t]
            for fid in same[allowed:]:
                flagged[fid] = t
    return flagged


def critique_ladder(rungs: dict[str, str], *, provider: Optional[str] = None) -> dict[str, Any]:
    """Verify a topic's rungs form a superset ladder. Returns {passed, issues[], note}."""
    present = {r: (rungs.get(r) or "").strip() for r in _RUNGS if (rungs.get(r) or "").strip()}
    if len(present) < 2:
        return {"passed": True, "issues": [], "note": "fewer than 2 rungs — nothing to compare"}
    block = "\n\n".join(f"[{r}]\n{present[r]}" for r in _RUNGS if r in present)
    user = f"RUNGS:\n{block}\n\nReturn ONLY the JSON object."
    data = _judge(_LADDER_SYSTEM, user, provider)
    issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
    return {
        "passed": data.get("verdict") != "redo" and not issues,
        "issues": issues,
        "note": (data.get("note") or "").strip(),
    }
