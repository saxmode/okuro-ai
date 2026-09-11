# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism MESSAGE composer — the "document parts → messages" layer.
#   For a topic, write the single crisp ASSERTION HEADLINE + a lede per depth
#   level from that level's claims. This is the text-brilliance layer templates
#   render: a headline is the slide's one 'so what' (specific, punchy, leads with
#   the sharpest fact/number), never the generic topic nav-title.
# index: _SYS | compose_facet_messages | stamp_messages
# AGENT_HEADER_END -->
"""okuro·prism message composer (the crisp-headline layer).

The diagnosis (2026-07-22): templates render designed grids, but the TEXT was the
generic topic title + trimmed prose — flat copy on a sharp layout. The gold
standard leads every slide with an assertion ("A 3× recall jump at 1/12 the
context"), not a nav label ("Finding Things: Cortex Beats Grep").

This LLM stage writes, per present depth level of a topic, a single ASSERTION
HEADLINE (the slide's one 'so what', 5-11 words, leading with the sharpest
fact/number from the claims) and a one-line LEDE. Grounded strictly in the
level's claims — it may only sharpen wording, never invent a figure. Stamped onto
the rung as ``message = {headline, lede}``; the template binder prefers it over
the topic title. One batched call per facet.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.messages")

from okuro.prism.rungs import RUNGS

_SYS = (
    "You are a world-class editorial headline writer for data-dense executive slide decks "
    "(think a hand-crafted board deck, not generic slideware). For ONE topic you are given "
    "its depth levels (L1 cover → L4 detail), each with its prose and the CLAIMS that ground "
    "it. For every level present, write:\n"
    "  • headline — the slide's SINGLE assertion, its one 'so what'. 5-11 words. Lead with the "
    "sharpest concrete fact, number, or name from that level's claims. It must be a CLAIM, not "
    "a label: 'Cortex hits 0.92 recall at 1/12 grep's context', never 'Finding Things: Cortex "
    "vs Grep'. No colon-prefixed topic names, no hedging, no fluff.\n"
    "  • lede — one crisp framing sentence, ≤ 18 words, that sets up the data. Not a restatement "
    "of the headline.\n\n"
    "HARD RULES: use ONLY facts present in that level's claims — real numbers, real names; never "
    "invent or round away a figure. Each level's headline must differ (rising specificity with "
    "depth). L1 may be the punchiest single idea. Output ONLY a JSON object.\n"
    'Return exactly: {"L1":{"headline":str,"lede":str}, "L2":{...}, ...} — only for levels given.'
)


def _facts(blocks: list[dict[str, Any]] | None) -> str:
    """A compact one-line-per-datum digest of a rung's blocks — the concrete
    numbers/names the headline should lead with. Self-grounding: these already
    passed the build's faithfulness gates."""
    out: list[str] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "stat":
            out += [f"{i.get('value')} {i.get('label','')}".strip() for i in (b.get("items") or [])]
        elif t == "compare":
            bl, al = b.get("before") or {}, b.get("after") or {}
            out.append(f"{bl.get('label','A')}: {', '.join(bl.get('items') or [])}")
            out.append(f"{al.get('label','B')}: {', '.join(al.get('items') or [])}")
        elif t == "cards":
            out += [f"{c.get('title')} — {c.get('body','')}".strip(" —") for c in (b.get("cards") or [])]
        elif t == "meter":
            out += [f"{i.get('label')}: {i.get('value')}{('/' + str(i.get('max'))) if i.get('max') else ''}" for i in (b.get("items") or [])]
        elif t in ("statement", "quote"):
            v = (b.get("text") or b.get("quote") or "").strip()
            if v:
                out.append(v)
    return "\n".join(f"- {x}" for x in out[:24] if x)


def compose_facet_messages(
    facet: dict[str, Any],
    claims: Optional[list[dict[str, Any]]] = None,
    *,
    provider: Optional[str] = None,
) -> dict[str, dict[str, str]]:
    """Write {level: {headline, lede}} for a facet's present levels. Batched — one
    LLM call per facet. Returns {} on failure (caller keeps the topic title).

    Self-grounding: sources on each level's PROSE + the concrete data points on its
    blocks (both already claim-grounded by the build's gates), so it works on any
    stored deck. ``claims`` is folded in as extra grounding when available."""
    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    rungs = facet.get("rungs") or {}
    present = [r for r in RUNGS if isinstance(rungs.get(r), dict)
               and ((rungs[r].get("body") or "").strip() or rungs[r].get("blocks"))]
    if not present:
        return {}

    def _level(r: str) -> str:
        rc = rungs[r]
        body = (rc.get("body") or "").strip()
        facts = _facts(rc.get("blocks"))
        parts = [f"[{r}]"]
        if body:
            parts.append(body[:700])
        if facts:
            parts.append("DATA:\n" + facts)
        return "\n".join(parts)

    ids = set(facet.get("claim_ids") or [])
    fclaims = [c for c in (claims or []) if c.get("id") in ids and (c.get("statement") or "").strip()]
    claim_block = ("\nEXTRA CLAIMS:\n" + "\n".join(f"- {c['statement'].strip()}" for c in fclaims[:30])) if fclaims else ""
    level_block = "\n\n".join(_level(r) for r in present)
    user = (
        f"TOPIC: {(facet.get('headline') or facet.get('title') or '').strip()}\n\n"
        f"LEVELS (prose + the concrete data each carries — your only source of facts):\n{level_block}\n"
        f"{claim_block}\n\n"
        "Write the headline + lede for each level given. Return ONLY the JSON object."
    )
    try:
        res = invoke(prompt=user, system_prompt=_SYS, provider=provider, capability="standard", timeout=180)
        if not (isinstance(res, dict) and res.get("success")):
            return {}
        data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
    except Exception as exc:  # noqa: BLE001 — message copy never blocks a build
        logger.warning("compose_facet_messages failed: %s", exc)
        return {}
    out: dict[str, dict[str, str]] = {}
    if isinstance(data, dict):
        for r in present:
            m = data.get(r)
            if isinstance(m, dict) and (m.get("headline") or "").strip():
                out[r] = {
                    "headline": str(m["headline"]).strip().strip('"'),
                    "lede": str(m.get("lede") or "").strip().strip('"'),
                }
    return out


def stamp_messages(doc: Any, claims: list[dict[str, Any]], *, provider: Optional[str] = None) -> int:
    """Compose + stamp ``message`` on each facet's present rungs, in place. Returns
    the count. One LLM call per facet; a facet that yields nothing is left as-is
    (templates fall back to the topic title). Best-effort per facet."""
    if not isinstance(doc, dict):
        return 0
    facets = doc.get("facets")
    if not isinstance(facets, dict):
        return 0
    n = 0
    for facet in facets.values():
        if not isinstance(facet, dict):
            continue
        try:
            msgs = compose_facet_messages(facet, claims, provider=provider)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stamp_messages: facet skipped: %s", exc)
            continue
        rungs = facet.get("rungs") or {}
        for level, m in msgs.items():
            if isinstance(rungs.get(level), dict):
                rungs[level]["message"] = m
                n += 1
    return n


__all__ = ["compose_facet_messages", "stamp_messages"]
