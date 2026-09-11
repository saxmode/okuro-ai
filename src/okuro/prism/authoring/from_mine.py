# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W5 — the MINE -> AUTHORING bridge. Converts a mine-stage
#   compiler.ir.Claim dict (typed shape + structured per-shape ``fields`` + an
#   evidence quote) into the authoring engine's solver.schema.Claim (+ its
#   ClaimGrounding). The load-bearing job is extracting REAL per-item content
#   triples (label, detail, meta) from each shape's structured fields so
#   multi-item components render actual mined content and the accuracy accounting
#   can state "N of M" honestly — never an invented cell (grounding forbids it).
#   Before W5 items_content was set ONLY in authoring/fixtures.py; mine and the
#   authoring engine were disconnected. This module is that connection.
# index: solver_claim_from_mined | items_content_for | _triples_for_shape
# AGENT_HEADER_END -->
"""Mine claim dict -> authoring (solver.schema.Claim, ClaimGrounding).

The compiler's ``mine`` stage emits ``compiler.ir.Claim`` dicts with a typed
``shape`` (one of the 11 kit shapes) and a per-shape structured ``fields`` payload
(``SHAPE_FIELD_SCHEMAS``). The authoring engine consumes ``solver.schema.Claim``,
whose ``items_content`` is a tuple of ``(label, detail, meta)`` triples bound into
component slots. This module maps the former to the latter, shape by shape, reading
ONLY what mine grounded — every triple traces to a real field value, so a claim the
source gives no per-item detail for yields no triple (the accuracy accounting then
flags the content-gap rather than inventing content).

Fields values are LENIENT (a live model emits numbers-as-strings, and comparison
cell ``values`` as an object OR a checklist array OR a scalar — see
``ir.lenient_fields``); every extractor is written to tolerate those shapes.
"""
from __future__ import annotations

from typing import Any, Optional

from okuro.prism.authoring.topic import ClaimGrounding, TopicSource
from okuro.prism.solver.schema import Claim

Triple = tuple[str, str, str]


def _s(v: Any) -> str:
    """Scalar/opaque field value -> a clean display string (never 'None')."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return ", ".join(x for x in (_s(i) for i in v) if x)
    if isinstance(v, dict):
        # object cell (comparison values keyed by criterion): "k: v · k: v"
        return " · ".join(f"{k}: {_s(x)}" for k, x in v.items() if _s(x))
    return str(v)


def _with_unit(value: Any, unit: Any) -> str:
    val, u = _s(value), _s(unit)
    if val and u and not val.endswith(u):
        return f"{val} {u}"
    return val or u


def _triples_for_shape(shape: str, fields: dict[str, Any]) -> list[Triple]:
    """Real per-item content for a mined claim, one triple per rendered item.

    Empty for shapes with no per-item structure (narrative). A triple is
    ``(label, detail, meta)`` — components map these generic slots to their own
    (card=name/desc, list=title/sub, tile=label/value, table row=cells, step=
    what/actor/tool). Only values MINED from the source appear; missing detail
    stays empty so the accuracy accounting surfaces a content-gap, never fills it.
    """
    f = fields or {}
    if shape == "metric":
        return [(_s(f.get("label")), _with_unit(f.get("value"), f.get("unit")),
                 _s((f.get("baseline") or {}).get("label")
                    if isinstance(f.get("baseline"), dict) else ""))]
    if shape == "delta":
        arrow = f"{_s(f.get('before'))} → {_s(f.get('after'))}".strip(" →")
        return [(_s(f.get("label")), _with_unit(arrow, f.get("unit")), _s(f.get("direction")))]
    if shape == "comparison":
        out: list[Triple] = []
        for opt in f.get("options") or []:
            if not isinstance(opt, dict):
                out.append((_s(opt), "", ""))
                continue
            meta = "recommended" if opt.get("recommended") else ""
            out.append((_s(opt.get("name")), _s(opt.get("values")), meta))
        return out
    if shape == "sequence":
        out = []
        for st in f.get("steps") or []:
            if not isinstance(st, dict):
                out.append((_s(st), "", ""))
                continue
            out.append((_s(st.get("what")), _s(st.get("actor")), _s(st.get("tool"))))
        return out
    if shape == "relationship":
        return [(_s(n.get("label") if isinstance(n, dict) else n),
                 _s(n.get("kind") if isinstance(n, dict) else ""), "")
                for n in (f.get("nodes") or [])]
    if shape == "proportion":
        out = []
        for p in f.get("parts") or []:
            if not isinstance(p, dict):
                out.append((_s(p), "", ""))
                continue
            out.append((_s(p.get("label")), _with_unit(p.get("value"), f.get("unit")),
                        _s(p.get("level"))))
        return out
    if shape == "quote":
        return [(_s(f.get("attribution")), _s(f.get("text")), "")]
    if shape == "set":
        out = []
        for it in f.get("items") or []:
            if not isinstance(it, dict):
                out.append((_s(it), "", ""))
                continue
            out.append((_s(it.get("name")), _s(it.get("desc")), _s(it.get("icon"))))
        return out
    if shape == "trend":
        return [(_s(pt.get("x") if isinstance(pt, dict) else pt),
                 _with_unit(pt.get("y") if isinstance(pt, dict) else "", f.get("unit")), "")
                for pt in (f.get("series") or [])]
    if shape == "verdict":
        return [(_s(f.get("subject")), _s(f.get("ruling")), _s(f.get("rationale")))]
    # narrative (and any unknown shape): no per-item structure — prose only.
    return []


def items_content_for(shape: str, fields: dict[str, Any]) -> tuple[Triple, ...]:
    """Public: the per-item content triples for a mined (shape, fields), non-empty
    labels dropped-nothing (empties are meaningful — they drive content-gap flags).
    Returned as a hashable tuple for the frozen Claim."""
    return tuple(_triples_for_shape(shape, fields))


def solver_claim_from_mined(mined: dict[str, Any], *, tier: int = 1) -> tuple[Claim, ClaimGrounding]:
    """Convert one mine-stage claim dict -> (solver.schema.Claim, ClaimGrounding).

    ``tier`` is assigned by the caller from the outline/projection (importance);
    the mine stage does not tier. ``items`` (cardinality) = the number of real
    content triples, min 1 (a single-value claim still contributes one item). The
    evidence quote becomes the grounding ``source_quote``; a claim mine could not
    ground verbatim (grounded=False) is NOT synthesized — it is a real mined claim
    whose quote simply did not match; the L4-coverage gate handles it.
    """
    cid = str(mined["id"])
    statement = str(mined.get("statement") or "")
    shape = str(mined.get("shape") or "narrative")
    fields = mined.get("fields") or {}
    triples = items_content_for(shape, fields)
    items = max(1, len(triples))
    claim = Claim(
        id=cid, text=statement, shape=shape, tier=int(tier),
        source_id=str((mined.get("evidence") or {}).get("artifact_id") or "") or None,
        items=items, chars=len(statement), items_content=triples,
    )
    ev = (mined.get("evidence") or {}).get("quote") or ""
    grounding = ClaimGrounding(source_quote=str(ev), synthesized=False, fallback=False)
    return claim, grounding


def build_topic_sources(
    mine_out: dict[str, Any],
    outline: dict[str, Any],
    authored: dict[str, Any],
    *,
    content_character: str = "analytical",
    audience_mood: str = "technical",
) -> list[TopicSource]:
    """Assemble the authoring inputs from the compiler's mine + outline + author.

    Reuses the compiler stages 1-5 (mine claims, the outline topic grouping, and
    the author stage's per-topic master_doc + claim tiers) and converts each topic
    into a ``TopicSource`` the authoring ladder engine consumes. The tier (0 = most
    important) comes from the author stage's ``TieredClaim`` list — the same tiers
    that drove the compiler's own depth slicing — so the resolution ladder's L1/L2
    inclusion is grounded in the pipeline's importance ranking, not re-guessed.

    ``mine_out``  = ``compiler.mine.mine`` output (``{"claims": [claim-dict…], …}``).
    ``outline``   = ``compiler.ir.Outline.to_dict()`` (topics with ordered claim_ids).
    ``authored``  = ``compiler.ir.Authored.to_dict()`` (per-topic master_doc + tiers).
    """
    claims_by_id: dict[str, dict[str, Any]] = {
        str(c["id"]): c for c in (mine_out.get("claims") or [])
    }
    authored_by_topic: dict[str, dict[str, Any]] = {
        str(t["topic_id"]): t for t in (authored.get("topics") or [])
    }
    topics_out: list[TopicSource] = []
    ordered = sorted(outline.get("topics") or [], key=lambda t: t.get("order", 0))
    for topic in ordered:
        tid = str(topic["id"])
        at = authored_by_topic.get(tid, {})
        tier_map: dict[str, int] = {
            str(tc["claim_id"]): int(tc["tier"]) for tc in (at.get("tiers") or [])
        }
        claims: dict[str, Claim] = {}
        grounding: dict[str, ClaimGrounding] = {}
        for cid in topic.get("claim_ids") or []:
            md = claims_by_id.get(str(cid))
            if md is None:
                continue                       # a claim dropped upstream — skip, never fabricate
            claim, g = solver_claim_from_mined(md, tier=tier_map.get(str(cid), 3))
            claims[claim.id] = claim
            grounding[claim.id] = g
        if not claims:
            continue                           # an empty topic never reaches the ladder
        # L4-coverage (gate.py) checks each claim's grounded ``source_quote`` is a
        # VERBATIM substring of master_doc. A live author stage paraphrases the
        # master_doc and drops those spans, so a faithful deck fails coverage (0/N)
        # purely on wording (W5 Phase D: the documented paraphrase class). Guarantee
        # coverage by keeping the author's readable master as the lede, then
        # APPENDING every grounded span it does not already contain verbatim — the
        # deepest level then provably carries every mined fact, never truncated.
        author_master = str(at.get("master_doc") or "").strip()
        spans = [(grounding[c].source_quote or "").strip() for c in claims]
        spans = [s for s in spans if s]
        if author_master:
            missing = [s for s in spans if s not in author_master]
            master_doc = author_master + (("\n\n" + "\n\n".join(missing)) if missing else "")
        else:
            # no author master — build from grounded spans (then statements).
            master_doc = "\n\n".join(spans) or "\n\n".join(claims[c].text for c in claims)
        topics_out.append(TopicSource(
            topic_id=tid, title=str(topic.get("title") or tid),
            claims=claims, master_doc=master_doc, grounding=grounding,
            content_character=content_character, audience_mood=audience_mood,
        ))
    return topics_out


__all__ = ["solver_claim_from_mined", "items_content_for", "build_topic_sources"]
