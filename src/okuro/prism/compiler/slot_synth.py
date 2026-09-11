# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compose reliability — schema-driven minimal-instance minting
#   used two ways: (1) EXEMPLAR of an archetype's REQUIRED slot structure shown in
#   the compose prompt + re-ask so a live model sees the exact nested shape (e.g.
#   ask-cta ask.items must be a non-empty array); (2) FAIL-SOFT SYNTHESIS — when the
#   LLM never produces a valid required-slot fill, build a minimal SCHEMA-VALID fill
#   from the cell's claim data so the deck always completes (flagged in the IR).
# index: mint_instance | exemplar | synthesize | _short_title
# AGENT_HEADER_END -->
"""Schema-driven slot minting for compose reliability (DP10).

One generator serves every archetype: given a JSON slot schema it produces the
minimal instance that satisfies it. Seeded with placeholder text it is a prompt
EXEMPLAR (teach the shape); seeded with the cell's claim statements it is the
FAIL-SOFT fill (grounded, valid, never empty). No per-archetype hand-authoring,
so a new archetype needs zero new synth code — the class of "required nested slot
came back wrong/absent" is closed structurally, not slot by slot.
"""

from __future__ import annotations

import itertools
from typing import Any, Iterator


def _next_text(pool: Iterator[str], fallback: str) -> str:
    try:
        val = next(pool)
        return val.strip() or fallback
    except StopIteration:
        return fallback


def mint_instance(schema: dict[str, Any], text_pool: Iterator[str],
                  placeholder: str = "…") -> Any:
    """The minimal instance that satisfies ``schema``.

    Objects fill their ``required`` keys (all keys if none declared); arrays emit
    ``minItems`` (>=1) items; strings draw from ``text_pool`` then fall back to
    ``placeholder``; scalars get a neutral default. Enums take their first member.
    """
    schema = schema or {}
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        # prefer object/array structure over scalars when a union is offered
        for pref in ("object", "array", "string", "number", "integer", "boolean"):
            if pref in t:
                t = pref
                break
        else:
            t = t[0] if t else "string"
    if t == "object":
        props = schema.get("properties", {})
        req = schema.get("required") or list(props.keys())
        return {k: mint_instance(props.get(k, {"type": "string"}), text_pool, placeholder)
                for k in req}
    if t == "array":
        n = max(1, int(schema.get("minItems", 1) or 1))
        items = schema.get("items", {"type": "string"})
        return [mint_instance(items, text_pool, placeholder) for _ in range(n)]
    if t in ("number", "integer"):
        return 0
    if t == "boolean":
        return False
    if t == "null":
        return None
    return _next_text(text_pool, placeholder)


def _short_title(prose: str, claims: list[dict[str, Any]]) -> str:
    """A <=22-ish char title from the narrative spine or the lead claim."""
    src = (prose or "").strip() or (claims[0]["statement"] if claims else "Overview")
    words = src.split()
    return " ".join(words[:6]) if words else "Overview"


def exemplar(slot_schema: dict[str, Any]) -> dict[str, Any]:
    """A placeholder-filled instance of an archetype's required-slot schema, for
    the compose prompt / re-ask (teaches the exact nested shape)."""
    pool: Iterator[str] = iter(("Short label", "A concise line of real content",
                                "Another concrete item"))
    inst = mint_instance(slot_schema, pool, placeholder="text")
    if isinstance(inst, dict) and "title" in inst:
        inst["title"] = "A clear <accent>point</accent>"
    return inst


def synthesize(slot_schema: dict[str, Any], claims: list[dict[str, Any]],
               prose: str) -> dict[str, Any]:
    """Fail-soft: a minimal SCHEMA-VALID slot fill grounded in the cell's claim
    data (statements → text slots), used when the LLM can't produce a valid fill.
    """
    statements = [c.get("statement", "") for c in (claims or []) if c.get("statement")]
    statements = statements or [(prose or "").strip() or "See details"]
    # cycle so every text slot draws a real claim line rather than exhausting the
    # pool to a placeholder (mint pulls a bounded count, so cycle can't run away).
    pool: Iterator[str] = itertools.cycle(statements)
    inst = mint_instance(slot_schema, pool, placeholder="—")
    if isinstance(inst, dict):
        inst["title"] = _short_title(prose, claims)
        if "eyebrow" in (slot_schema.get("properties") or {}):
            inst.setdefault("eyebrow", "")
    return inst
