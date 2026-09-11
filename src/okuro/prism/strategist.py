# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism Audience Strategist — decide, for a target person/group,
#   which claims to include or cut, how deep, in what jargon, at what angle, and
#   the single takeaway. Stage 2 of the root-doc→deck pipeline (between Distiller
#   and Architect). Implements the prism-audience-strategist role charter.
# index: _STRAT_SYSTEM | strategize | select_claims
# AGENT_HEADER_END -->
"""Prism Audience Strategist.

Turns the full claim set into an AUDIENCE BRIEF: the recipient-fit decision of
what to keep, what to cut, how deep to go, the framing, and the one takeaway.
Writes no prose and picks no modules — it decides scope, so the architect that
follows structures only what matters to THIS recipient.

Mirrors the ``prism-audience-strategist`` charter (roles/catalog).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.strategist")

from okuro.prism.rungs import RUNGS as _RUNGS
_JARGON = ("plain", "balanced", "technical")

_STRAT_SYSTEM = (
    "You are okuro·prism's AUDIENCE STRATEGIST. Given a CITED CLAIM SET (id: statement) "
    "and a TARGET recipient, decide the recipient-fit STRATEGY. You write no prose and "
    "pick no visual modules — you decide SCOPE. Output ONLY a JSON object.\n\n"
    "Decide:\n"
    "  - include_ids / exclude_ids: which claims THIS recipient needs vs. doesn't. Bias "
    "HARD toward cutting — a board deck is ~20% of the research; keep only what changes "
    "THEIR decision or understanding. Every id appears in exactly one list.\n"
    "  - depth_ceiling: the deepest LEVEL this recipient reads — L1|L2|L3|L4. "
    "A board making a DECISION reads to L3 (they need the key comparison, options and "
    "mechanism to decide); reserve L2 for a pure at-a-glance exec summary. A TECHNICAL / "
    "expert reader (jargon=technical) reaches L4 — never stop an expert reader at L3; they "
    "want the edge cases and specifics. Default a non-expert decision deck to L3.\n"
    "  - jargon: plain|balanced|technical for this recipient.\n"
    "  - angle: the framing that makes the material land for them (one phrase).\n"
    "  - takeaway: the ONE thing they must leave with (exactly one sentence).\n"
    "  - tone: how it should read to them (one phrase).\n"
    "  - cut_rationale: 2-3 lines on what you cut and why.\n\n"
    "HARD RULES: include/exclude are drawn ONLY from the given claim ids — invent none. "
    "Exactly one takeaway. Justify cuts by the AUDIENCE, not by what's interesting. Do NOT "
    "rewrite claims or add facts.\n\n"
    'Return exactly: {"include_ids":[...],"exclude_ids":[...],"depth_ceiling":str,'
    '"jargon":str,"angle":str,"takeaway":str,"tone":str,"cut_rationale":str}.'
)


def _format_claims(claims: list[dict[str, Any]], max_chars: int = 40000) -> str:
    lines: list[str] = []
    total = 0
    for c in claims:
        line = f"{c['id']}: {(c.get('statement') or '').strip()}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def strategize(
    claims: list[dict[str, Any]],
    target: str,
    *,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Decide the audience brief for ``target`` over ``claims``.

    Returns ``{include_ids, exclude_ids, depth_ceiling, jargon, angle, takeaway,
    tone, cut_rationale, kept, cut}``. Claim ids are validated against the input;
    any id the model omitted from both lists defaults to INCLUDE (fail-open — a
    dropped claim is worse than an extra one)."""
    claims = [c for c in (claims or []) if isinstance(c, dict) and c.get("id") and (c.get("statement") or "").strip()]
    if not claims:
        raise ValueError("claims required")
    if not (target or "").strip():
        raise ValueError("target required")

    user = (
        f"TARGET RECIPIENT:\n{target.strip()}\n\n"
        f"CLAIMS (id: statement):\n{_format_claims(claims)}\n\n"
        "Produce the audience brief. Return ONLY the JSON object."
    )

    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "0"
    try:
        res = invoke(
            prompt=user, system_prompt=_STRAT_SYSTEM, provider=provider,
            capability="standard", timeout=240,
        )
    finally:
        if _prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = _prev

    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")

    valid = {c["id"] for c in claims}
    data: dict[str, Any] = {}
    try:
        parsed = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
        if isinstance(parsed, dict):
            data = parsed
    except (ValueError, KeyError) as exc:
        logger.warning("strategize: parse failed: %s", exc)

    exclude = [cid for cid in (data.get("exclude_ids") or []) if cid in valid]
    exclude_set = set(exclude)
    # Fail-open: everything not explicitly excluded is included (a dropped claim
    # is worse than an extra one; the architect can still nest a marginal claim).
    include = [cid for cid in valid if cid not in exclude_set]

    from okuro.prism.rungs import normalize_rung
    depth_raw = normalize_rung(data.get("depth_ceiling"))  # tolerate a legacy ceiling value
    depth = depth_raw if depth_raw in _RUNGS else "L3"
    jargon = data.get("jargon") if data.get("jargon") in _JARGON else "balanced"
    # Couple ceiling to expertise (DP10): a TECHNICAL/expert reader reads to the
    # bottom — never stop them at L3 (which starves the L4 level and the
    # deep-detail ladder). The LLM tends to default even an expert reader to L3.
    if jargon == "technical" and _RUNGS.index(depth) < _RUNGS.index("L4"):
        depth = "L4"

    return {
        "include_ids": include,
        "exclude_ids": exclude,
        "depth_ceiling": depth,
        "jargon": jargon,
        "angle": (data.get("angle") or "").strip(),
        "takeaway": (data.get("takeaway") or "").strip(),
        "tone": (data.get("tone") or "").strip(),
        "cut_rationale": (data.get("cut_rationale") or "").strip(),
        "kept": len(include),
        "cut": len(exclude),
    }


def select_claims(claims: list[dict[str, Any]], brief: dict[str, Any]) -> list[dict[str, Any]]:
    """The included subset of ``claims`` per a strategist ``brief`` — the input to
    the Structure Architect."""
    inc = set(brief.get("include_ids") or [])
    return [c for c in claims if c.get("id") in inc]
