# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Generate a recipient-tailored, brand-styled deck IR from a topic.
# index:
#   imports
#   def _brand_design
#   def _deck_system / _deck_user
#   def _parse_deck / _normalize
#   def generate_deck
# AGENT_HEADER_END -->
"""Recipient-tailored deck generation.

Given a topic (+ optional recipient person + brand), assemble:
  - brand design tokens  (resolve_brand → design_profile: colours, type)
  - recipient lens        (person_lens → flight level + cognitive profile)
  - grounded facts        (gather_context over okuro's brain)
…then ask the LLM to emit a scene-IR Deck (the same JSON the frontend renders +
animates). The deck is normalised and saved via okuro.slides. One LLM call,
non-streaming — decks are small.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Optional

logger = logging.getLogger("okuro.slides.generate")

_DEFAULT_BRAND = "okuro"
# Single source of truth for the baseline canvas — defined in the storage layer
# (which now also defaults it on every read/write), reused here so a generated
# deck and a defaulted one always agree.
from okuro.slides.storage import DEFAULT_CANVAS as _CANVAS
from okuro.slides.grid import COMPOSITION_NAMES as _COMPOSITION_NAMES


def _brand_design(brand_id: str) -> dict[str, Any]:
    """The design-profile token blob for a brand (colours/type), or {}."""
    try:
        from okuro.stack.registry import resolve_brand

        brand = resolve_brand(brand_id) or {}
        slot = brand.get("slots", {}).get("design")
        if isinstance(slot, list):
            slot = slot[0] if slot else None
        if isinstance(slot, dict):
            return slot.get("data") or {}
    except Exception as exc:
        logger.info("brand design resolve failed: %s", exc)
    return {}


_SCHEMA = (
    'Deck JSON shape (emit EXACTLY this, no prose, no code fences):\n'
    '{"title": str, "arrangement": "horizontal"|"vertical", '
    '"background": "#rrggbb", "transition": {"duration": 0.6, "easing": "easeInOut"}, '
    '"slides": [ {"id": str, "composition": '
    '"split-7-5"|"rail-8-4"|"full-bleed"|"centered", "elements": [ Element ... ]} ]}\n'
    'Element = {"id": str, "kind": "text"|"box"|"frame"|"image"|"list"|"kpi"|"quote"|"divider"|"chart"|"table", "x": int, '
    '"y": int, "w": int, "h": int, "text"?: str, "src"?: str (image url), '
    '"items"?: [str] (for kind "list"), "ordered"?: bool (numbered list), '
    '"gap"?: int (list row spacing px), '
    '"value"?: str + "label"?: str + "delta"?: str + '
    '"deltaDir"?: "up"|"down"|"flat" (for kind "kpi"), '
    '"attribution"?: str (source, for kind "quote" — uses "text" for the quotation), '
    '"orientation"?: "h"|"v" + "thickness"?: int + "accent"?: bool (for kind "divider"), '
    '"chartType"?: "bar"|"line"|"area" + "series"?: [{"name": str, "values": [number]}] + '
    '"categories"?: [str] + "showLegend"?: bool + "showAxes"?: bool (for kind "chart"), '
    '"columns"?: [str] + "rows"?: [[str]] + "header"?: bool (for kind "table"), '
    '"border"?: {"width": int, "color": "#rrggbb"} + "shadow"?: "sm"|"md"|"lg" + '
    '"gradient"?: {"from": "#rrggbb", "to": "#rrggbb", "angle"?: int} (box enrichment), '
    '"bg"?: "#rrggbb", "color"?: "#rrggbb", "fontSize"?: int, '
    '"fontWeight"?: 400|600|700, "radius"?: int, '
    '"align"?: "left"|"center"|"right", "z"?: int, '
    '"depth"?: 1|2|3|4 (progressive-disclosure level; 1=always shown, higher '
    'reveals later), '
    '"slot"?: str (name of a slot in the slide\'s composition), '
    '"children"?: [ Element ... ], '
    '"layout"?: {"flow": "none"|"row"|"col", "gap": int, "padX": int, '
    '"padY": int, "align": "start"|"center"|"end"}}\n'
    'A "frame" is an AUTO-LAYOUT container: with layout.flow "col" (or "row") '
    'it stacks its children with gap+padding and HUGS the content, reflowing '
    'when any child text wraps — so siblings never overlap. Children use coords '
    'RELATIVE to the frame (frame x/y position it on the canvas; child x/y are '
    'offsets inside it, overridden by auto-layout when flow != "none").\n'
    f'Canvas is {_CANVAS["w"]}x{_CANVAS["h"]} px; place elements within it.\n'
    'CRITICAL — smart-animate: give an element the SAME id on consecutive '
    'slides when it should persist/morph (e.g. a title that moves + shrinks, a '
    'logo that stays). A new id enters; a dropped id exits in the arrangement '
    'direction. Reuse ids deliberately to author motion.'
)


def _grid_hint() -> str:
    """Concrete 12-column grid coordinates + composition archetypes for the
    author, so it places elements ON the grid and VARIES composition."""
    try:
        from okuro.slides.grid import column_lines, grid_metrics

        lefts, _rights = column_lines(float(_CANVAS["w"]))
        m = grid_metrics(float(_CANVAS["w"]))
        cols = ", ".join(str(int(round(x))) for x in lefts)
        return (
            f"\n\nGRID — compose on a 12-column grid (canvas {_CANVAS['w']}px wide, "
            f"{int(round(m['margin']))}px outer margin). Column left-edge x "
            f"positions: {cols}. Start frames/text blocks at one of these x and "
            "align right edges to the grid.\n"
            "COMPOSITION — set each slide's \"composition\" to one of, and tag "
            "each top-level element with the \"slot\" it fills:\n"
            "  split-7-5 (slots: body, aside) — text left, visual right;\n"
            "  rail-8-4 (slots: hero, rail) — dominant block + supporting column;\n"
            "  full-bleed (slots: fill, caption) — one idea edge-to-edge "
            "(image / quote / oversized numeral);\n"
            "  centered (slot: block) — covers & section dividers ONLY.\n"
            "VARY composition across slides — do NOT center every slide."
        )
    except Exception:
        return ""


def _deck_system(design: dict[str, Any]) -> str:
    design_txt = json.dumps(design, separators=(",", ":"))[:1500] if design else "(no brand tokens — use a clean dark theme)"
    return (
        "You are okuro·slides' deck-authoring agent. Produce a polished, "
        "on-brand slide deck as a scene-IR JSON document.\n\n"
        + _SCHEMA
        + "\n\nBRAND DESIGN TOKENS (use these colours + type hierarchy; pick a "
        "background and accents from them):\n"
        + design_txt
        + "\n\nRules: 4-6 slides; one clear idea per slide; real layout (titles "
        "~64-84px, body ~28-36px, generous spacing); reuse element ids across "
        "slides for shared/morphing elements.\n"
        "LAYOUT — CRITICAL: ANY group of stacked text (title+subtitle, an "
        "eyebrow+title, a bullet list, a labelled card) MUST be wrapped in a "
        "\"frame\" with layout.flow \"col\" (or \"row\" for side-by-side) plus "
        "gap/padX/padY — so the frame HUGS its text and REFLOWS when a line "
        "wraps. NEVER hardcode a per-line y for stacked text: if a title wraps "
        "to two lines, fixed-y siblings overlap. Child x/y are relative to the "
        "frame; the frame's own x/y place the group on the canvas. Use absolute "
        "positioning (bare text/box, no frame) ONLY for standalone or "
        "background elements."
        + "\nLISTS — for any bullet or numbered list use a \"list\" element "
        "(kind:\"list\", items:[...]) rather than newline-joined \"•\" text: it "
        "renders real markers with a hanging indent and hugs its height. Set "
        "\"ordered\":true for a numbered list."
        + "\nKPIs — when a slide's point IS a number (a metric, %, count, money), "
        "use a \"kpi\" element (value + optional label + delta/deltaDir) so the "
        "figure reads as an oversized headline with a coloured change chip, not "
        "buried in a sentence."
        + "\nQUOTES — for a pull-quote or testimonial use a \"quote\" element "
        "(text = the quotation, attribution = the source) — it renders oversized "
        "with an accent bar, not as ordinary body text."
        + "\nCHARTS — when the point is a comparison or a trend across categories, "
        "use a \"chart\" element (chartType bar|line|area, series:[{name,values}], "
        "categories:[...]) — it renders as a clean native chart with axes + legend. "
        "Pick by job: a SINGLE headline number is a \"kpi\", NOT a one-bar chart; a "
        "few enumerated points are a \"list\"; multi-category or multi-series numeric "
        "data (comparison → bar, change-over-time → line/area) is a \"chart\". Keep "
        "series ≤ 4 for legibility. Give the chart a generous box (≥ 560px wide)."
        + "\nTABLES — for dense, multi-dimensional data that is text (not a single "
        "trend or comparison) use a \"table\" element (columns:[...], rows:[[...]], "
        "header:true) — it renders a clean brand-themed grid that hugs its height. "
        "Use a table only when a chart or KPI can't carry the data; keep it to a "
        "few columns."
        + "\nDEPTH — progressive disclosure: tag each element with a \"depth\" "
        "1-4 so the deck reveals in reading order. depth 1 = the GLANCE takeaway "
        "the audience must read in <3s (the title + ONE headline point / key "
        "number); depth 2 = the core supporting points; depth 3-4 = evidence, "
        "detail, caveats, sources. Most slides need only depth 1-2; add 3-4 only "
        "when a slide genuinely carries deeper backup. A slide's depth-1 elements "
        "alone must stand as a coherent glance. Leave depth off (⇒1) for anything "
        "always-visible (backgrounds, logos, the title). Depth is orthogonal to "
        "layout — deeper elements still sit in their authored place; they just "
        "appear as the presenter steps down."
        + _grid_hint()
        + "\nOutput ONLY the JSON object."
    )


def _deck_user(topic: str, lens: str, context: str) -> str:
    parts = [f"TOPIC for the deck:\n{topic}\n"]
    if lens.strip():
        parts.append(
            "RECIPIENT LENS — write at THIS person's flight level and in THEIR "
            "cognitive style (wording, abstraction, what they care about):\n"
            + lens.strip()[:2500]
            + "\n"
        )
    if context.strip():
        parts.append(
            "GROUNDED FACTS from okuro's brain (use these; do not invent okuro "
            "specifics you weren't given):\n" + context.strip()[:5000] + "\n"
        )
    parts.append("Author the deck now as a single JSON object. Nothing else.")
    return "\n".join(parts)


def _parse_deck(output: str) -> dict[str, Any]:
    """Extract the JSON object from the LLM output (tolerates fences/prose)."""
    text = (output or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in deck output")
    return json.loads(text[start : end + 1])


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _coerce_float(v: Any) -> Optional[float]:
    """Finite float or None (NaN/inf/garbage → None) — for chart series values."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


_ELEMENT_KINDS = ("text", "box", "image", "frame", "video", "flow", "list", "kpi", "quote", "divider", "chart", "table")


def _norm_layout(raw: Any) -> dict[str, Any]:
    """Coerce a frame's auto-layout block (sane defaults; validated enums)."""
    lay = raw if isinstance(raw, dict) else {}
    flow = lay.get("flow") if lay.get("flow") in ("none", "row", "col") else "col"
    align = lay.get("align") if lay.get("align") in ("start", "center", "end") else "start"
    return {
        "flow": flow,
        "gap": _coerce_int(lay.get("gap"), 16),
        "padX": _coerce_int(lay.get("padX"), 16),
        "padY": _coerce_int(lay.get("padY"), 16),
        "align": align,
    }


def _norm_element(e: Any, fallback_id: str) -> Optional[dict[str, Any]]:
    """Normalize one element; recurse into frame children. None if not a dict."""
    if not isinstance(e, dict):
        return None
    kind = e.get("kind") if e.get("kind") in _ELEMENT_KINDS else "text"
    el: dict[str, Any] = {
        "id": str(e.get("id") or fallback_id),
        "kind": kind,
        "x": _coerce_int(e.get("x"), 100),
        "y": _coerce_int(e.get("y"), 100),
        "w": _coerce_int(e.get("w"), 400),
        "h": _coerce_int(e.get("h"), 100),
        "z": _coerce_int(e.get("z"), 1),
    }
    for k in ("text", "src", "flowId", "bg", "color", "align", "slot", "value", "label", "delta", "attribution"):
        if e.get(k):
            el[k] = str(e[k])
    # kpi delta direction (colours the change chip); enum-validated.
    if e.get("deltaDir") in ("up", "down", "flat"):
        el["deltaDir"] = e["deltaDir"]
    for k in ("fontSize", "fontWeight", "radius", "gap", "thickness"):
        if e.get(k) is not None:
            el[k] = _coerce_int(e.get(k), 0)
    # divider: rule direction + accent flag.
    if e.get("orientation") in ("h", "v"):
        el["orientation"] = e["orientation"]
    if e.get("accent") is not None:
        el["accent"] = bool(e.get("accent"))
    # box enrichment: border / shadow / gradient (all optional, validated).
    b = e.get("border")
    if isinstance(b, dict) and _coerce_int(b.get("width"), 0) > 0:
        el["border"] = {"width": _coerce_int(b.get("width"), 1), "color": str(b.get("color") or "#8ff0a4")}
    if e.get("shadow") in ("sm", "md", "lg"):
        el["shadow"] = e["shadow"]
    g = e.get("gradient")
    if isinstance(g, dict) and g.get("from") and g.get("to"):
        el["gradient"] = {"from": str(g["from"]), "to": str(g["to"])}
        if g.get("angle") is not None:
            el["gradient"]["angle"] = _coerce_int(g.get("angle"), 180)
    # list primitive: bullet/numbered rows (each hugs + wraps under its marker).
    if isinstance(e.get("items"), list):
        el["items"] = [str(x) for x in e["items"]]
    if e.get("ordered") is not None:
        el["ordered"] = bool(e.get("ordered"))
    # chart primitive: type + numeric series (+ categories). Malformed series /
    # values are dropped so a bad chart degrades to fewer bars, never a crash.
    if e.get("chartType") in ("bar", "line", "area"):
        el["chartType"] = e["chartType"]
    if isinstance(e.get("series"), list):
        norm_series: list[dict[str, Any]] = []
        for si, s in enumerate(e["series"][:8]):  # palette caps at 8 series
            if not isinstance(s, dict):
                continue
            vals_raw = s.get("values")
            if not isinstance(vals_raw, list):
                continue
            vals = [f for f in (_coerce_float(v) for v in vals_raw) if f is not None]
            if vals:
                norm_series.append({"name": str(s.get("name") or f"Series {si + 1}"), "values": vals})
        if norm_series:
            el["series"] = norm_series
    if isinstance(e.get("categories"), list):
        cats = [str(c) for c in e["categories"]]
        if cats:
            el["categories"] = cats
    for k in ("showLegend", "showAxes"):
        if e.get(k) is not None:
            el[k] = bool(e.get(k))
    # table primitive: columns + rows (cells coerced to str; capped so a runaway
    # generation can't blow up the IR). Ragged rows are fine — the render pads.
    if isinstance(e.get("columns"), list):
        cols = [str(c) for c in e["columns"][:12]]
        if cols:
            el["columns"] = cols
    if isinstance(e.get("rows"), list):
        norm_rows: list[list[str]] = []
        for r in e["rows"][:50]:
            if isinstance(r, list):
                norm_rows.append([str(c) for c in r[:12]])
        if norm_rows:
            el["rows"] = norm_rows
    if e.get("header") is not None:
        el["header"] = bool(e.get("header"))
    # Progressive-disclosure depth (1..4). Optional and only written when the
    # author/LLM supplied it — an absent depth means "always visible" (⇒1) at
    # render, so decks with no depth stay byte-identical to the pre-depth path.
    if e.get("depth") is not None:
        el["depth"] = min(4, max(1, _coerce_int(e.get("depth"), 1)))
    if isinstance(e.get("provenance"), dict):
        el["provenance"] = e["provenance"]
    if kind == "frame":
        el["layout"] = _norm_layout(e.get("layout"))
        kids = []
        for ci, c in enumerate(e.get("children") or []):
            child = _norm_element(c, f"{el['id']}_c{ci}")
            if child is not None:
                kids.append(child)
        el["children"] = kids
    return el


def _dedupe_slide_ids(els: list[dict[str, Any]]) -> None:
    """Make every element id unique WITHIN one slide (top-level + frame children).

    A repeated id in a slide is fatal downstream: the morph resolver
    (``resolveElement``) matches by id and silently drops the second element, and
    React keys collide. Same id ACROSS slides is intentional (that's a morph), so
    this only de-dups within a single slide. First occurrence keeps its id."""
    seen: set[str] = set()

    def walk(el: dict[str, Any]) -> None:
        eid = str(el.get("id") or "")
        if eid in seen:
            n = 2
            while f"{eid}-{n}" in seen:
                n += 1
            eid = f"{eid}-{n}"
            el["id"] = eid
        seen.add(eid)
        for c in el.get("children") or []:
            if isinstance(c, dict):
                walk(c)

    for e in els:
        walk(e)


def _normalize(raw: dict[str, Any], *, topic: str) -> dict[str, Any]:
    """Force the LLM output into a valid Deck IR (defaults + per-element coords)."""
    title = str(raw.get("title") or topic[:60] or "Untitled deck").strip()
    arrangement = raw.get("arrangement")
    arrangement = arrangement if arrangement in ("horizontal", "vertical") else "horizontal"
    tr = raw.get("transition") if isinstance(raw.get("transition"), dict) else {}
    transition = {
        "duration": float(tr.get("duration") or 0.6),
        "easing": tr.get("easing") if tr.get("easing") else "easeInOut",
    }
    out_slides = []
    raw_slides = raw.get("slides") if isinstance(raw.get("slides"), list) else []
    for si, s in enumerate(raw_slides):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or f"s{si}")
        els = []
        for ei, e in enumerate(s.get("elements") or []):
            el = _norm_element(e, f"e{si}_{ei}")
            if el is not None:
                els.append(el)
        if els:
            _dedupe_slide_ids(els)
            slide: dict[str, Any] = {"id": sid, "elements": els}
            comp = s.get("composition")
            if comp in _COMPOSITION_NAMES:
                slide["composition"] = comp
            out_slides.append(slide)

    if not out_slides:
        raise ValueError("generated deck has no usable slides")

    return {
        "title": title,
        "arrangement": arrangement,
        "size": dict(_CANVAS),
        "transition": transition,
        "background": str(raw.get("background") or "#0b0f0c"),
        "slides": out_slides,
    }


def _recipient_identity(person_id: str) -> str:
    """Public identity for the deck (name · role · org). Deliberately scoped to
    this user-owned, user-intended slides use — separate from person_lens's PII
    firewall (which anonymizes the cognitive projection for arbitrary prompts)."""
    try:
        from okuro.db import get_db

        row = get_db().fetchone(
            "SELECT display_name, role, organization FROM persons "
            "WHERE (id = ? OR LOWER(display_name) LIKE ?) AND active = 1 LIMIT 1",
            (person_id, f"%{person_id.lower()}%"),
        )
        if not row:
            return ""
        tail = " · ".join([b for b in (row.get("role"), row.get("organization")) if b])
        name = row.get("display_name") or person_id
        return f"{name}{f' ({tail})' if tail else ''}"
    except Exception:
        return ""


def _invoke(prompt: str, system: str, *, thinking: int, provider: Optional[str], timeout: int = 150) -> str:
    """invoke() with an explicit thinking budget (env, restored after)."""
    import os as _os

    from okuro.bridge.invoke import invoke

    prev = _os.environ.get("MAX_THINKING_TOKENS")
    _os.environ["MAX_THINKING_TOKENS"] = str(thinking)
    try:
        res = invoke(prompt=prompt, system_prompt=system, provider=provider, capability="standard", timeout=timeout)
    finally:
        if prev is None:
            _os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            _os.environ["MAX_THINKING_TOKENS"] = prev
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")
    return res.get("output") or ""


_OUTLINE_SYSTEM = (
    "You are a presentation strategist. Plan a deck BEFORE layout: reason about the "
    "recipient's flight level + comms style and the goal, then output a tight outline. "
    "Output markdown ONLY: a numbered list of 4-6 slides, each 'N. Title — intent; key points'."
)


def _act(msg: str) -> dict:
    return {"type": "activity", "msg": msg}


def generate_deck_events(
    topic: str,
    *,
    person_id: Optional[str] = None,
    brand_id: str = _DEFAULT_BRAND,
    mode: str = "fast",
    provider: Optional[str] = None,
):
    """Stream generation as activity events, ending with {type:done, deck}.

    mode 'fast'  → one-shot, thinking off (seconds, no reasoning).
    mode 'quality' → outline (thinking ON, a visible plan) then layout (thinking off)."""
    topic = (topic or "").strip()
    if not topic:
        yield {"type": "error", "error": "topic required"}
        return

    yield _act("resolving brand design system…")
    design = _brand_design(brand_id)

    ident, lens = "", ""
    if person_id:
        yield _act("loading recipient profile…")
        ident = _recipient_identity(person_id)
        try:
            from okuro.peer.persons import person_lens

            lens = person_lens(person_id, context=topic) or ""
        except Exception as exc:
            logger.info("person_lens failed for %s: %s", person_id, exc)

    yield _act("grounding in okuro's brain…")
    context, src_labels = "", []
    try:
        from okuro.sense.grounding import gather_context

        context, src_labels = gather_context(topic, budget=6000)
    except Exception:
        context = ""

    recipient = f"RECIPIENT (address them where natural): {ident}\n{lens}".strip() if (ident or lens) else ""

    try:
        if mode == "quality":
            yield _act("drafting the outline (reasoning)…")
            outline = _invoke(
                f"TOPIC: {topic}\n\n{recipient}\n\nGrounded facts:\n{context.strip()[:4000]}\n\nPlan the deck now.",
                _OUTLINE_SYSTEM, thinking=2048, provider=provider,
            ).strip()
            first = " · ".join([ln.strip() for ln in outline.splitlines() if ln.strip()][:6])[:240]
            yield _act(f"outline → {first}")
            yield _act("laying out the slides…")
            raw = _invoke(
                f"TOPIC: {topic}\n\n{recipient}\n\nAPPROVED OUTLINE (follow it):\n{outline}\n\n"
                f"Grounded facts:\n{context.strip()[:4000]}\n\nProduce the deck JSON now.",
                _deck_system(design), thinking=0, provider=provider,
            )
        else:
            yield _act("drafting the deck…")
            raw = _invoke(_deck_user(topic, recipient, context), _deck_system(design), thinking=0, provider=provider)
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "error": str(exc)}
        return

    try:
        deck = _normalize(_parse_deck(raw), topic=topic)
        if person_id:
            deck["recipient"] = person_id
        if brand_id:
            deck["brandId"] = brand_id
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "error": f"parse failed: {exc}"}
        return

    # "Can't-lie" gate (HYBRID policy): every factual claim must be entailed by
    # the grounded facts. Hard-block fabricated facts/figures/names (redo once,
    # then drop); soft-flag inferences; pass grounded. Needs a source to verify
    # against — with no grounding we mark unverified and do NOT block (absence of
    # proof is not proof of fabrication). Never breaks generation.
    if context.strip():
        try:
            from okuro.slides import entailment as _ent

            yield _act("checking every claim against the source…")
            claims = _ent.extract_claims(deck)
            verdicts = _ent.check_claims(claims, context, provider=provider)
            fab = _ent.fabricated_refs(verdicts)
            if fab:
                bad = [next((c["text"] for c in claims if c["ref"] == r), r) for r in fab][:12]
                yield _act(f"{len(fab)} unsupported claim(s) — regenerating without them…")
                feedback = (
                    "Your previous draft asserted facts NOT supported by the grounded "
                    "facts below. Remove or correct these EXACT statements, and assert "
                    "nothing the grounded facts do not support:\n- " + "\n- ".join(bad)
                )
                try:
                    raw2 = _invoke(
                        _deck_user(topic, recipient, context) + "\n\n" + feedback,
                        _deck_system(design), thinking=0, provider=provider,
                    )
                    deck2 = _normalize(_parse_deck(raw2), topic=topic)
                    if person_id:
                        deck2["recipient"] = person_id
                    if brand_id:
                        deck2["brandId"] = brand_id
                    deck = deck2
                    claims = _ent.extract_claims(deck)
                    verdicts = _ent.check_claims(claims, context, provider=provider)
                    fab = _ent.fabricated_refs(verdicts)
                except Exception as exc:  # noqa: BLE001
                    logger.info("entailment redo failed (keeping first draft): %s", exc)
                if fab:
                    dropped = _ent.drop_refs(deck, fab)
                    yield _act(f"dropped {dropped} unsupported element(s)")
            # Count only what actually ships: drop_refs may have removed some
            # elements, and any fabrication it could NOT remove stays flagged.
            present = {c["ref"] for c in _ent.extract_claims(deck)}
            verdicts = {r: v for r, v in verdicts.items() if r in present}
            _ent.annotate_deck(deck, verdicts)
            c = _ent.counts(verdicts)
            deck["provenance"] = {"sources": src_labels, "gate": c}
            yield _act(
                f"provenance ✓ {c.get('grounded', 0)} grounded · "
                f"{c.get('inferred', 0)} inferred · {c.get('fabricated', 0)} blocked"
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("entailment gate skipped (error): %s", exc)
            deck["provenance"] = {"sources": src_labels, "gate": {"status": "skipped"}}
    else:
        deck["provenance"] = {"sources": [], "gate": {"status": "unverified"}}

    # Deterministic brand-fidelity pass: snap colors/type to brand tokens and
    # enforce WCAG contrast. Must never break generation — fall back to the
    # unresolved deck on any failure.
    try:
        from okuro.slides.brand_resolver import resolve_brand_tokens

        deck = resolve_brand_tokens(deck, design)
    except Exception as exc:  # noqa: BLE001
        logger.info("brand resolver failed (using unresolved deck): %s", exc)

    # Template binder: GUARANTEE composition (not just guide it). Bind each
    # slide's archetype + element slots onto the 12-col grid rects, and repair
    # monotone / all-centered output into varied, on-grid layouts. Places within
    # the canvas, so snap below is a no-op and fit finds nothing to shrink. Never
    # breaks generation.
    try:
        from okuro.slides.binder import bind_deck

        bound = bind_deck(deck)
        if bound:
            yield _act(f"composed {bound} slide(s) onto the grid")
    except Exception as exc:  # noqa: BLE001
        logger.info("template binder skipped (error): %s", exc)

    # Grid tidy: nudge element edges already near a 12-column grid line onto it
    # (non-destructive — off-grid elements are left alone). Runs before fit so
    # any snap-induced bounds change is caught by the fit pass.
    try:
        from okuro.slides.grid import snap_to_grid

        snapped = snap_to_grid(deck)
        if snapped:
            yield _act(f"aligned {snapped} edge(s) to the grid")
    except Exception as exc:  # noqa: BLE001
        logger.info("grid snap skipped (error): %s", exc)

    # Fit/bounds guarantee: scale + re-center any slide whose content exceeds or
    # sits outside the canvas so nothing spills or crops. No-op for slides that
    # already fit; never breaks generation.
    try:
        from okuro.slides.layout import fit_deck

        fitted = fit_deck(deck)
        if fitted:
            yield _act(f"fit {fitted} slide(s) to the canvas")
    except Exception as exc:  # noqa: BLE001
        logger.info("layout fit pass skipped (error): %s", exc)

    yield _act("saving…")
    from okuro.slides import save_deck

    saved = save_deck(id=None, title=deck["title"], deck=deck, origin="generate")
    yield {"type": "done", "deck": saved.to_detail()}


def generate_deck(
    topic: str,
    *,
    person_id: Optional[str] = None,
    brand_id: str = _DEFAULT_BRAND,
    mode: str = "fast",
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Sync wrapper (MCP / non-streaming callers): drain the event stream."""
    last_error = "generation produced no deck"
    for ev in generate_deck_events(topic, person_id=person_id, brand_id=brand_id, mode=mode, provider=provider):
        if ev.get("type") == "done":
            return ev["deck"]
        if ev.get("type") == "error":
            last_error = ev["error"]
    raise RuntimeError(last_error)


_EDIT_SYSTEM = (
    "You edit an okuro·slides deck by emitting STRUCTURED OPS — never rewrite the "
    "whole deck. Output ONLY a JSON array of op objects, nothing else.\n\n"
    "Reference slides by integer index (0-based) and elements by their id. Ops:\n"
    '  {"op":"set_meta","title?":str,"background?":"#rrggbb","font?":str,"arrangement?":"horizontal|vertical"}\n'
    '  {"op":"add_slide","after?":int,"slide?":{"elements":[Element...]}}\n'
    '  {"op":"duplicate_slide","index":int}   {"op":"remove_slide","index":int}\n'
    '  {"op":"move_slide","from":int,"to":int}   {"op":"set_notes","slide":int,"notes":str}\n'
    '  {"op":"add_element","slide":int,"element":{Element}}\n'
    '  {"op":"update_element","slide":int,"id":str,"patch":{partial Element}}\n'
    '  {"op":"remove_element","slide":int,"id":str}\n'
    "Element = {id?, kind:'text'|'box'|'image'|'frame', x,y,w,h, text?, src?, "
    "fontSize?, fontWeight?, color?, bg?, align?, radius?, z?, "
    "children?:[Element...], layout?:{flow:'none'|'row'|'col', gap, padX, padY, "
    "align:'start'|'center'|'end'}}. A 'frame' auto-lays-out its children "
    "(layout.flow 'col'/'row' + gap/pad) and HUGS the content, reflowing on "
    "wrap — wrap any stacked text in a frame so it never overlaps instead of "
    "hardcoding per-line y. Children use coords relative to the frame. "
    "update_element's patch may set layout/children to retrofit a frame. "
    "Canvas 1280x720. Reuse an element id across "
    "slides to make it persist/morph. Make the SMALLEST set of ops that satisfies the "
    "request. For a 'review', improve wording/clarity/layout via update_element ops."
)


def run_ops_instruction(
    deck: dict[str, Any], instruction: str, *, provider: Optional[str] = None
) -> tuple[dict[str, Any], list[str], int]:
    """Shared NL-edit core: translate ``instruction`` into structured ops against
    ``deck`` (via ``_EDIT_SYSTEM``, thinking off) and apply them. Returns
    ``(new_deck, op_errors, ops_applied)`` WITHOUT persisting — callers decide how
    to save (in place vs. as a fresh variant). Reused by ``edit_deck`` and
    ``okuro.slides.retailor``."""
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction required")

    user = (
        f"Current deck (JSON):\n{json.dumps(deck, separators=(',', ':'))[:9000]}\n\n"
        f"Edit request:\n{instruction}\n\n"
        "Return ONLY the JSON array of ops."
    )

    import os as _os

    from okuro.bridge.invoke import invoke

    _prev = _os.environ.get("MAX_THINKING_TOKENS")
    _os.environ["MAX_THINKING_TOKENS"] = "0"
    try:
        res = invoke(prompt=user, system_prompt=_EDIT_SYSTEM, provider=provider, capability="standard", timeout=120)
    finally:
        if _prev is None:
            _os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            _os.environ["MAX_THINKING_TOKENS"] = _prev

    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")

    text = (res.get("output") or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise RuntimeError("agent returned no ops")
    ops = json.loads(text[start : end + 1])
    ops = ops if isinstance(ops, list) else []

    from okuro.slides.ops import apply_ops

    new_deck, op_errors = apply_ops(deck, ops)
    return new_deck, op_errors, len(ops)


def edit_deck(deck_id: str, instruction: str, *, provider: Optional[str] = None) -> dict[str, Any]:
    """Apply a natural-language edit/review to an existing deck via structured ops.
    Returns the saved deck detail (+ any per-op errors under 'op_errors')."""
    from okuro.slides import get_deck, save_deck

    doc = get_deck(deck_id)
    if doc is None:
        raise ValueError(f"deck '{deck_id}' not found")

    new_deck, op_errors, ops_applied = run_ops_instruction(doc.deck, instruction, provider=provider)

    saved = save_deck(id=deck_id, title=new_deck.get("title") or doc.title, deck=new_deck, origin="chat-edit", allow_empty=True)
    detail = saved.to_detail()
    detail["op_errors"] = op_errors
    detail["ops_applied"] = ops_applied
    return detail
