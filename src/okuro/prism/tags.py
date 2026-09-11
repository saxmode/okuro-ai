# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism CLAIM AUDIENCE-TAGS — the overlay that makes projection
#   possible. Tags each master-story claim with audiences/weight/construal/frame
#   (an audience-relative JUDGMENT), field names + values ALIGNED with
#   resonance.pco.Claim so a future convergence is a pure relocation. Truth stays
#   neutral: tags are a non-destructive overlay (truth ⊥ audience ⊥ brand).
# index: normalize_tags | apply_tags | tag_claims | tag_story
# AGENT_HEADER_END -->
"""Audience-relative claim tagging — Stage 2's overlay layer.

The master story (``story.build_story``) is audience-NEUTRAL: its claims carry
truth + shape + granularity, but no judgment of WHO each claim serves. Projection
(``prism.projection.project_story``) needs that judgment. This module supplies it
as a non-destructive OVERLAY — it adds four fields to a claim and touches nothing
else:

  * ``audiences`` — the reader KINDS the claim serves (subset of
    ``prism.audience.AUDIENCE_KINDS``); ``[]`` = universal.
  * ``weight``    — ``must`` / ``should`` / ``nice`` (concise readers drop nice).
  * ``construal`` — ``why`` (outcome) / ``how`` (mechanism), or None.
  * ``frame``     — optional ``{"gain":..., "loss":...}`` reframes of the SAME fact.

These names + values MIRROR ``resonance.pco.Claim`` exactly, so converging the
two IRs later is trivial. prism must NOT import resonance (that would close the
cycle resonance.projection→prism.audience opened), so the vocab is redeclared
here and pinned to the pco values by a test.

The LLM does the irreducibly-semantic judgment (``tag_claims`` / ``tag_story``);
the validators + merger (``normalize_tags`` / ``apply_tags``) are pure and
offline-tested. An un-tagged claim stays universal, so tagging is strictly
additive — a story that is never tagged projects to itself for every reader.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from okuro.prism.audience import AUDIENCE_KINDS

logger = logging.getLogger("okuro.prism.tags")

# Mirror resonance.pco.WEIGHTS / CONSTRUALS — pinned by test_tags to stay in sync
# (prism cannot import resonance without closing the projection import cycle).
WEIGHTS = ("must", "should", "nice")
CONSTRUALS = ("why", "how")

# The overlay fields this module writes — the ONLY keys apply_tags adds/replaces.
_TAG_FIELDS = ("audiences", "weight", "construal", "frame")


def normalize_tags(raw: Any) -> dict[str, Any]:
    """Coerce a model-emitted tag object for ONE claim into a valid overlay.

    Total: any malformed input collapses to the neutral default (universal /
    must / no reframe), so a bad model response can never poison a claim. Invalid
    audience kinds are dropped, an unknown weight falls back to ``must``, an
    unknown construal to None, and a frame keeps only non-empty gain/loss strings.
    """
    if not isinstance(raw, dict):
        return {"audiences": [], "weight": "must", "construal": None, "frame": None}

    seen: set[str] = set()
    audiences = [a for a in (raw.get("audiences") or [])
                 if a in AUDIENCE_KINDS and not (a in seen or seen.add(a))]

    weight = raw.get("weight") if raw.get("weight") in WEIGHTS else "must"
    construal = raw.get("construal") if raw.get("construal") in CONSTRUALS else None

    frame_raw = raw.get("frame")
    frame: Optional[dict[str, str]] = None
    if isinstance(frame_raw, dict):
        f = {k: frame_raw[k].strip() for k in ("gain", "loss")
             if isinstance(frame_raw.get(k), str) and frame_raw[k].strip()}
        frame = f or None

    return {"audiences": audiences, "weight": weight, "construal": construal, "frame": frame}


def apply_tags(claims: Any, tags_by_id: dict[str, Any] | None) -> Any:
    """Overlay normalized audience tags onto claims BY ID — non-destructively.

    Truth fields (``statement``/``shape``/``granularity``/``evidence``/
    ``confidence``) are never touched; only the four overlay fields are written,
    and only on a claim the model actually tagged (an untagged claim is returned
    unchanged → stays universal). Returns NEW claim dicts; the input is not
    mutated. Accepts either the story's id-keyed dict or a plain list of claims
    and returns the same shape.
    """
    tags_by_id = tags_by_id or {}

    def _merged(claim: dict[str, Any]) -> dict[str, Any]:
        cid = claim.get("id")
        raw = tags_by_id.get(cid) if cid else None
        if raw is None:
            return {**claim}
        return {**claim, **normalize_tags(raw)}

    if isinstance(claims, dict):
        return {cid: _merged(c) for cid, c in claims.items() if isinstance(c, dict)}
    return [_merged(c) for c in (claims or []) if isinstance(c, dict)]


_TAG_SYSTEM = (
    "You are okuro·prism's AUDIENCE TAGGER. Given a CITED CLAIM SET (id: statement, "
    "with each claim's kind and granularity), tag EACH claim with WHO it serves and "
    "HOW to frame it — an audience-relative OVERLAY. You NEVER rewrite a claim or "
    "change what it says; you only judge its relevance and framing. Output ONLY a "
    "JSON object.\n\n"
    "For every claim id, decide:\n"
    '  - audiences: which reader KINDS this claim serves — a subset of ["board",'
    '"exec","technical","general"]. Use [] (empty) for a UNIVERSAL claim every '
    "reader needs; name specific kinds only when the claim genuinely serves some "
    'readers and not others (a deep mechanism → ["technical"]; a governance ask → '
    '["board","exec"]). Most claims are universal — do not over-scope.\n'
    '  - weight: how load-bearing — "must" (the argument fails without it), "should" '
    '(strengthens it), or "nice" (color/detail a concise reader can lose).\n'
    '  - construal: the altitude the claim is naturally about — "why" '
    '(outcome/strategic) or "how" (mechanism/implementation). Omit if neither fits.\n'
    "  - frame: OPTIONAL gain/loss reframes of the SAME fact — "
    '{"gain":"the upside…","loss":"the risk avoided…"}. Only when the claim carries a '
    "real stake; omit otherwise. NEVER invent a stake.\n\n"
    "HARD RULES: tag ONLY the given ids — invent none. Keep every claim TRUE — tags "
    "SELECT and FRAME, they never change the facts. A claim most readers need is "
    "universal ([]), not tagged to every kind.\n\n"
    'Return exactly: {"tags":{"<id>":{"audiences":[...],"weight":str,"construal":str,'
    '"frame":{...}}, ...}}.'
)


def _format_claims(claims: list[dict[str, Any]], max_chars: int = 40000) -> str:
    lines: list[str] = []
    total = 0
    for c in claims:
        gran = c.get("granularity") or "supporting"
        knd = c.get("kind") or "fact"
        line = f"{c['id']} [{knd}/{gran}]: {(c.get('statement') or '').strip()}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def tag_claims(
    claims: list[dict[str, Any]], *, provider: Optional[str] = None
) -> dict[str, dict[str, Any]]:
    """LLM stage — judge the audience overlay for each claim.

    Returns ``{claim_id: normalized_tags}`` for the ids the model tagged (only
    ids present in the input survive; each value is passed through
    ``normalize_tags``). A parse failure yields ``{}`` (no tags → every claim
    stays universal), never raises — tagging is additive, its absence is safe.
    """
    claims = [c for c in (claims or []) if isinstance(c, dict)
              and c.get("id") and (c.get("statement") or "").strip()]
    if not claims:
        return {}

    user = (
        f"CLAIMS (id [kind/granularity]: statement):\n{_format_claims(claims)}\n\n"
        "Tag every claim. Return ONLY the JSON object."
    )

    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "0"
    try:
        res = invoke(
            prompt=user, system_prompt=_TAG_SYSTEM, provider=provider,
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
    out: dict[str, dict[str, Any]] = {}
    try:
        data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
        tags = data.get("tags") if isinstance(data, dict) else None
        if isinstance(tags, dict):
            for cid, raw in tags.items():
                if cid in valid:
                    out[cid] = normalize_tags(raw)
    except (ValueError, KeyError) as exc:
        logger.warning("tag_claims: parse failed: %s", exc)
    return out


def tag_story(story: dict[str, Any], *, provider: Optional[str] = None) -> dict[str, Any]:
    """Return a new story whose claims carry the audience overlay — the
    projection-ready story. Non-destructive: truth is untouched, and an
    un-taggable claim stays universal. The tree/measured/unproven are preserved."""
    claims = story.get("claims") or {}
    tags = tag_claims(list(claims.values()), provider=provider)
    return {**story, "claims": apply_tags(claims, tags)}


__all__ = ["WEIGHTS", "CONSTRUALS", "normalize_tags", "apply_tags", "tag_claims", "tag_story"]
