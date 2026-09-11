# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism L1 HOOK — per top-level topic, select the single
#   highest-value claim for THIS recipient and frame it as the L1 impact line.
#   The faithfulness gate is first-class: a hook MUST trace to a real claim id in
#   its own topic, or it is dropped — never shipped citing nothing (the one
#   weakness the validation judges flagged).
# index: select_hook_claim | gate_hook | build_hooks | frame_hooks
# AGENT_HEADER_END -->
"""The L1 hook — Stage 2's per-recipient impact line.

Neither shipped path (build_deck, resonance render) has a hook. The redesign's L1
is NOT a teaser and NOT a slogan: it is the single highest-value TRUE point for
this reader, per top-level topic, chosen AFTER projection (so the hook differs by
audience over the same underlying truth).

Two layers:
  * a PURE, deterministic selector + assembler (``select_hook_claim`` /
    ``build_hooks``) that ranks a topic's claims and picks one, and
  * a FAITHFULNESS GATE (``gate_hook``) that is first-class, not advisory: a hook
    is admitted only if its ``claim_id`` is a real claim id IN that topic. A hook
    that cites nothing, or a claim from another topic, is dropped and recorded —
    never shipped. This gate also fences the optional LLM framing (``frame_hooks``):
    if the model sharpens the line but cites an out-of-topic (or no) claim, its
    line is discarded and the deterministic claim stands.

The selector never leads with an UNPROVEN claim (low confidence or ungrounded)
when a proven one exists — the loudest line on the deck must be the most
defensible, not merely the punchiest.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.hook")

# Lower rank = stronger hook candidate. Ordered by VALUE for an impact line:
# proven-first (faithfulness of the loudest line), then load-bearing weight, then
# a top-line granularity, then confidence; ties break on the topic's claim order.
_WEIGHT_RANK = {"must": 0, "should": 1, "nice": 2}
_GRAN_RANK = {"headline": 0, "supporting": 1, "detail": 2, "edge": 3}
_CONF_RANK = {"high": 0, "med": 1, "low": 2}


def _unproven(claim: dict[str, Any]) -> bool:
    """Mirror story.partition_evidence: a claim is unproven when its confidence is
    'low' OR it did not trace verbatim to source (grounded explicitly False)."""
    return claim.get("confidence") == "low" or claim.get("grounded") is False


def _hook_key(claim: dict[str, Any], idx: int) -> tuple:
    return (
        1 if _unproven(claim) else 0,
        _WEIGHT_RANK.get(claim.get("weight", "must"), 0),
        _GRAN_RANK.get(claim.get("granularity"), 1),
        _CONF_RANK.get(claim.get("confidence"), 1),
        idx,
    )


def select_hook_claim(topic: dict[str, Any], claims: dict[str, Any]) -> Optional[str]:
    """The single highest-value claim id for ``topic`` — the L1 hook candidate.

    Ranks the topic's OWN claims (``topic['claim_ids']`` resolved against the
    story's ``claims`` dict) by ``_hook_key`` and returns the best id, or None when
    the topic has no resolvable claim. Pure + deterministic (stable on claim
    order). Run over a PROJECTED story so the candidates are already the reader's."""
    cands = [
        (claims[cid], cid, i)
        for i, cid in enumerate(topic.get("claim_ids") or [])
        if isinstance(claims.get(cid), dict)
    ]
    if not cands:
        return None
    best = min(cands, key=lambda t: _hook_key(t[0], t[2]))
    return best[1]


def gate_hook(claim_id: Optional[str], topic: dict[str, Any]) -> bool:
    """FAITHFULNESS GATE (first-class): a hook is valid only if it cites a real
    claim id that belongs to THIS topic. A hook citing nothing or a foreign claim
    fails — the caller drops it rather than ship an ungrounded headline."""
    return bool(claim_id) and claim_id in set(topic.get("claim_ids") or [])


def _top_level(story: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in (story.get("topics") or []) if isinstance(t, dict) and not t.get("parent")]


def build_hooks(story: dict[str, Any]) -> dict[str, Any]:
    """Deterministic L1 hooks over a (projected) story — one per TOP-LEVEL topic.

    Returns ``{"hooks": {topic_id: hook}, "dropped": [{topic_id, reason}]}`` where
    a hook is ``{topic_id, claim_id, line, unproven, framed}``. ``line`` defaults
    to the claim's statement (the LLM sharpens it in ``frame_hooks``). Every
    shipped hook passes ``gate_hook``; a topic with no in-topic claim is dropped
    and recorded, never shipped with an empty hook. Pure."""
    claims = story.get("claims") or {}
    hooks: dict[str, Any] = {}
    dropped: list[dict[str, Any]] = []
    for t in _top_level(story):
        tid = t.get("id")
        cid = select_hook_claim(t, claims)
        if not gate_hook(cid, t):
            dropped.append({"topic_id": tid, "reason": "no in-topic claim to hook"})
            continue
        claim = claims[cid]
        hooks[tid] = {
            "topic_id": tid,
            "claim_id": cid,
            "line": (claim.get("statement") or "").strip(),
            "unproven": _unproven(claim),
            "framed": False,
        }
    return {"hooks": hooks, "dropped": dropped}


_HOOK_SYSTEM = (
    "You are okuro·prism's L1 HOOK writer. You are given ONE topic's claims and the "
    "id of the SELECTED highest-value claim. Sharpen THAT claim into an L1 hook — the "
    "single opening line the reader meets first. Output ONLY a JSON object.\n\n"
    "The hook is optimized for IMPACT, not density: bold, concrete, few words (aim "
    "under ~14). It states the SELECTED claim's point at its sharpest — it is NOT a "
    "slogan, a question, or a teaser, and it adds NO fact not in that claim.\n\n"
    "HARD RULES: the hook must be faithful to the selected claim — no new numbers, "
    "names, or stakes. Cite the claim id you used. You MAY pick a different id ONLY "
    "from the claims given, if one makes a sharper TRUE hook.\n\n"
    'Return exactly: {"line": str, "claim_id": str}.'
)


def _frame_one(topic: dict[str, Any], claims: dict[str, Any], selected_id: str,
               *, provider: Optional[str] = None) -> Optional[dict[str, str]]:
    """LLM: sharpen the selected claim into a hook line. Returns ``{line,
    claim_id}`` or None on any failure (the caller keeps the deterministic line)."""
    lines = [f"{cid}: {(claims[cid].get('statement') or '').strip()}"
             for cid in (topic.get("claim_ids") or []) if isinstance(claims.get(cid), dict)]
    user = (
        f"TOPIC: {(topic.get('title') or '').strip()}\n\n"
        f"CLAIMS (id: statement):\n" + "\n".join(lines) + "\n\n"
        f"SELECTED claim id: {selected_id}\n\nWrite the L1 hook. Return ONLY the JSON object."
    )
    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "0"
    try:
        res = invoke(prompt=user, system_prompt=_HOOK_SYSTEM, provider=provider,
                     capability="standard", timeout=120)
    finally:
        if _prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = _prev
    if not (isinstance(res, dict) and res.get("success")):
        return None
    try:
        data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
    except (ValueError, KeyError):
        return None
    if not isinstance(data, dict) or not (data.get("line") or "").strip():
        return None
    return {"line": data["line"].strip(), "claim_id": (data.get("claim_id") or "").strip()}


def frame_hooks(story: dict[str, Any], *, provider: Optional[str] = None) -> dict[str, Any]:
    """``build_hooks`` + an LLM pass that sharpens each hook LINE — every framed
    line RE-GATED through ``gate_hook``. If the model cites an out-of-topic (or no)
    claim, its line is discarded and the deterministic claim/line stands. The
    faithfulness invariant holds whether or not the LLM is used."""
    base = build_hooks(story)
    claims = story.get("claims") or {}
    topics_by_id = {t.get("id"): t for t in _top_level(story)}
    for tid, hook in base["hooks"].items():
        topic = topics_by_id.get(tid)
        if not topic:
            continue
        try:
            framed = _frame_one(topic, claims, hook["claim_id"], provider=provider)
        except Exception as exc:  # noqa: BLE001 — a hook never blocks the build
            logger.warning("frame_hooks: framing failed for %s: %s", tid, exc)
            framed = None
        if not framed:
            continue
        # Re-gate the model's cited claim; keep the deterministic line if it strays.
        if gate_hook(framed["claim_id"], topic):
            hook["line"] = framed["line"]
            hook["claim_id"] = framed["claim_id"]
            hook["unproven"] = _unproven(claims.get(framed["claim_id"]) or {})
            hook["framed"] = True
        else:
            logger.warning("frame_hooks: %s cited out-of-topic claim %r — line dropped",
                           tid, framed["claim_id"])
    return base


__all__ = ["select_hook_claim", "gate_hook", "build_hooks", "frame_hooks"]
