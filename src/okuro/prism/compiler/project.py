# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler STAGE 3 (project) — claims × AudienceProfile ->
#   per-claim keep/cut + emphasis + a one-line takeaway, for THIS audience. A
#   pure SELECT/RANK/annotate stage: the claim's data (numbers, entities, words)
#   is immutable — project may drop a claim or weight it, never rewrite it.
# index: project | _SYSTEM | _coverage_fill
# AGENT_HEADER_END -->
"""Stage 3 — project: tailor WHICH claims survive and how hard they land.

This is the recipient's non-destructive overlay (the orthogonality invariant:
truth ⟂ audience). The LLM sees claim summaries + the anonymized audience lens and
returns a decision per claim; it may NOT touch a claim's ``fields``. Coverage is
enforced in code — any claim the model forgot defaults to keep with emphasis =
its mined salience, so a forgetful model can only under-emphasize, never silently
delete data.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.compiler.ir import (
    AudienceProfile,
    ProjectedClaim,
    Projection,
)

logger = logging.getLogger(__name__)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decisions"],
    "additionalProperties": False,
    "properties": {
        "decisions": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["claim_id", "keep"],
                "additionalProperties": False,
                "properties": {
                    "claim_id": {"type": "string"},
                    "keep": {"type": "boolean"},
                    "emphasis": {"type": "number", "minimum": 0, "maximum": 1},
                    "takeaway": {"type": "string"},
                },
            },
        },
    },
}

_SYSTEM = """You are the PROJECT stage of the prism deck compiler. Given a set of \
data claims and an anonymized description of the audience, decide for EACH claim:
- keep: true if this claim earns a place in THIS audience's deck, false to cut it.
- emphasis: 0..1 — how central it should be for this audience (drives slide order \
and which claim leads a topic).
- takeaway: one short sentence stating the SO-WHAT of this claim for this audience \
(their language, their concern). Do not restate the claim; state why they care.

You may CUT claims that don't serve this audience and RANK the rest. You must NOT \
change any claim's underlying data — you are selecting and weighting, not editing.
Keep the deck focused: prefer cutting low-salience claims that don't advance the \
audience's decision. Return ONLY {"decisions": [...]} covering every claim id."""


def _audience_descriptor(prof: AudienceProfile) -> str:
    lines = [f"Recipients: {len(prof.recipients)}"
             f"{' (composite — satisfy all)' if prof.composite else ''}",
             f"Jargon tolerance: {prof.jargon_tolerance}",
             f"Depth ceiling: L{prof.depth_ceiling}"]
    for r in prof.recipients:
        lens = r.lens or "(no cognitive profile — use role defaults)"
        lines.append(f"- {r.role or 'reader'} lens: {lens}")
    if prof.prefer_modules:
        lines.append(f"Prefers module types: {', '.join(prof.prefer_modules)}")
    if prof.avoid_modules:
        lines.append(f"Avoids module types: {', '.join(prof.avoid_modules)}")
    return "\n".join(lines)


def _coverage_fill(
    decisions: list[ProjectedClaim], claims: list[dict[str, Any]]
) -> list[ProjectedClaim]:
    """Ensure every claim has a decision; add keep-by-default for any the model
    omitted (emphasis = mined salience). Drop decisions for unknown ids."""
    by_id = {c["id"]: c for c in claims}
    seen = {d.claim_id for d in decisions if d.claim_id in by_id}
    out = [d for d in decisions if d.claim_id in by_id]
    for cid, c in by_id.items():
        if cid not in seen:
            logger.info("project: model omitted %s — keep-by-default", cid)
            out.append(ProjectedClaim(claim_id=cid, keep=True,
                                      emphasis=float(c.get("salience", 0.5)), takeaway=""))
    return out


def project(
    mine_out: dict[str, Any],
    audience: AudienceProfile,
    *,
    provider: Optional[str] = None,
    invoke_fn=None,
) -> Projection:
    """Select/rank claims for the audience. Claim data is never mutated."""
    claims = mine_out.get("claims", [])
    if not claims:
        raise ValueError("project: no claims to project")

    compact = [{"id": c["id"], "shape": c["shape"], "salience": c.get("salience", 0.5),
                "statement": c["statement"]} for c in claims]
    user = (
        f"AUDIENCE:\n{_audience_descriptor(audience)}\n\n"
        f"CLAIMS ({len(compact)}):\n" + llm.dumps(compact) +
        "\n\nDecide keep/emphasis/takeaway for every claim. Return ONLY the JSON."
    )
    data = llm.call_json(
        system_prompt=_SYSTEM, user_prompt=user, schema=_SCHEMA, stage="project",
        # 600s (match mine): this judges EVERY claim in one call, so it scales with
        # the whole source. A 46k-word doc mined 324 claims and the old 300s budget
        # timed out mid-decision. Per-topic stages stay tighter; full-set stages get
        # the generous budget.
        provider=provider, capability="standard", timeout=600,
        thinking_tokens=4000, invoke_fn=invoke_fn,
    )
    decisions = [
        ProjectedClaim(
            claim_id=d["claim_id"], keep=bool(d["keep"]),
            emphasis=float(d.get("emphasis", 0.5)), takeaway=(d.get("takeaway") or "").strip(),
        )
        for d in data.get("decisions", [])
    ]
    decisions = _coverage_fill(decisions, claims)
    if not any(d.keep for d in decisions):  # never ship an empty deck
        top = max(decisions, key=lambda d: d.emphasis)
        top.keep = True
        logger.warning("project: model cut everything — forcing keep on %s", top.claim_id)
    return Projection(decisions=decisions, depth_ceiling=audience.depth_ceiling)
