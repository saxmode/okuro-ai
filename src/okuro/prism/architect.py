# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism Structure Architect — split a cited claim set into an
#   ordered, memorably-named topic tree (the facet skeleton) that reads as a
#   narrative arc. Stage 2 of the root-doc→deck pipeline. Implements the
#   prism-structure-architect role charter as callable code.
# index: _ARCH_SYSTEM | structure
# AGENT_HEADER_END -->
"""Prism Structure Architect.

Second pipeline stage: takes the distilled ``claims`` (and an optional audience
brief) and decides the SPLIT + SEQUENCE — the topic tree every later stage fills.
It writes no prose and invents no facts: each topic is a MECE cluster of existing
claim ids, named memorably, ordered to a deliberate arc.

Mirrors the ``prism-structure-architect`` charter (roles/catalog).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.architect")

_ARCH_SYSTEM = (
    "You are okuro·prism's STRUCTURE ARCHITECT. You are given a set of CITED CLAIMS "
    "(id: statement). Split them into an ordered TOPIC TREE that reads as a deliberate "
    "narrative ARC. You write NO prose and invent NO facts. Output ONLY a JSON object.\n\n"
    "Rules:\n"
    "  1. Produce 4-7 TOP-LEVEL topics (parent=null) — no more. The reader must grasp the "
    "whole deck from the top level alone. If the material is richer than 7 themes, do NOT "
    "add top-level topics: NEST the finer sections as CHILD topics (set parent to a "
    "top-level id). A board audience wants ~5-6 top-level topics; an expert audience can "
    "carry deeper nesting.\n"
    "  2. Each top-level topic must earn its slot; consolidate related source sections under "
    "one theme rather than mirroring the source's section count.\n"
    "  3. Choose an ARC and order the TOP-LEVEL topics to it. Default for a decision deck: "
    "problem → options → decision. For an ANALYSIS (no decision in the claims): what it is → "
    "what's broken → what to preserve → the worldview. Pick the arc the CLAIMS support — "
    "never invent a decision or options the claims don't contain.\n"
    "  4. Name every topic MEMORABLY and concretely (the name is the hook) — NEVER 'Topic 1'.\n"
    "  5. COVERAGE: assign every claim id to exactly ONE topic (a child topic counts). Do not "
    "drop claims. Do not invent a topic with no backing claim ids.\n"
    "  6. A single-topic tree is invalid.\n\n"
    'Return exactly: {"topics":[{"id":"t1","title":str,"claim_ids":[str,...],"order":int,'
    '"parent":null|"t<id>"}],"arc":str}. Top-level topics have parent=null; nested ones set '
    "parent to their top-level id. arc is one sentence naming the throughline."
)


def _format_claims(claims: list[dict[str, Any]], max_chars: int = 40000) -> str:
    lines: list[str] = []
    total = 0
    for c in claims:
        cid = c.get("id")
        stmt = (c.get("statement") or "").strip()
        if not cid or not stmt:
            continue
        line = f"{cid}: {stmt}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def structure(
    claims: list[dict[str, Any]],
    *,
    audience: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Cluster claims into a named, ordered topic tree with an arc.

    Returns ``{"topics": [{id, title, claim_ids, order, parent}], "arc": str,
    "unmapped": [claim_id,...], "coverage": float}``. Raises on empty claims or a
    failed model call."""
    claims = [c for c in (claims or []) if isinstance(c, dict) and c.get("id") and (c.get("statement") or "").strip()]
    if not claims:
        raise ValueError("claims required")

    brief = f"AUDIENCE: {audience}\n\n" if audience else ""
    user = (
        f"{brief}CLAIMS (id: statement):\n{_format_claims(claims)}\n\n"
        "Produce the topic tree. Return ONLY the JSON object."
    )

    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    # Reasoning budget: the split + arc is the deck's narrative spine — the one
    # stage where real thinking pays off. Not the pipeline's default 0.
    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "8000"
    try:
        res = invoke(
            prompt=user, system_prompt=_ARCH_SYSTEM, provider=provider,
            capability="standard", timeout=300,
        )
    finally:
        if _prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = _prev

    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")

    topics: list[dict[str, Any]] = []
    arc = ""
    valid_ids = {c["id"] for c in claims}
    try:
        data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
        if isinstance(data, dict):
            for i, t in enumerate(data.get("topics") or []):
                if not isinstance(t, dict) or not (t.get("title") or "").strip():
                    continue
                # keep only real claim ids; drop hallucinated references
                cids = [cid for cid in (t.get("claim_ids") or []) if cid in valid_ids]
                topics.append({
                    "id": (t.get("id") or f"t{i + 1}").strip(),
                    "title": t["title"].strip(),
                    "claim_ids": cids,
                    "order": t.get("order") if isinstance(t.get("order"), int) else i,
                    "parent": t.get("parent") if isinstance(t.get("parent"), str) else None,
                })
            arc = (data.get("arc") or "").strip()
    except (ValueError, KeyError) as exc:
        logger.warning("structure: parse failed: %s", exc)

    topics.sort(key=lambda t: t["order"])
    mapped = {cid for t in topics for cid in t["claim_ids"]}
    unmapped = [cid for cid in valid_ids if cid not in mapped]
    coverage = round(len(mapped) / max(1, len(valid_ids)), 3)
    return {"topics": topics, "arc": arc, "unmapped": unmapped, "coverage": coverage}
