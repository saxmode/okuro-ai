# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W5 — the AUTHORING -> DECKDOC adapter. Turns per-topic
#   LadderDocs (authoring L1/L2/L3 + L4 master + accuracy) into the deck2 viewer
#   contract (deck-types.ts DeckDoc: hero + topics{levels L0-L2, l3 docview} +
#   per-cell accuracy). Bridges the TWO renderer vocabularies (the authoring/solver
#   ~37-component world vs the viewer's 12 fixed archetypes) by mapping the
#   dominant claim's SHAPE -> archetype and binding the claim's mined items_content
#   triples into that archetype's slots. Level map: authoring L1(hook)->L0,
#   L2->L1, L3(densest)->L2, L4(full text)->l3 docview. Accuracy is taken from the
#   authoring accounting so the viewer surfaces the SAME N-of-M truth the gate proved.
# index: build_deck_doc | topic_to_deck | _cell_for_level | _archetype_for | _slots_*
# AGENT_HEADER_END -->
"""Authoring LadderDoc(s) -> deck2 DeckDoc (the live viewer contract).

The viewer renders 12 archetypes (deck-types.ts); the authoring engine speaks a
larger component vocabulary. This adapter maps by claim SHAPE (the 11 kit shapes
are archetype-aligned by design) and fills each archetype's content slot from the
claim's real mined ``items_content`` (label, detail, meta) — never invented; a
claim with no per-item content renders as a text row and the accuracy accounting
(carried through unchanged) flags the content-gap. ``validate_deck_doc`` is the
structural oracle every emitted deck is checked against.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from okuro.prism.authoring.accuracy import AccuracyReport
from okuro.prism.authoring.ladder import LadderDoc, RenderedUnit
from okuro.prism.solver.schema import Claim

logger = logging.getLogger("okuro.prism.to_deckdoc")

# authoring ladder level -> deck2 viewer level (L4 is the l3 doc-view).
_LADDER_TO_DECK = {"L1": "L0", "L2": "L1", "L3": "L2"}
_ASIDE_SHAPES = ("narrative", "quote")
_FLOW_ACTORS = {"user", "edge", "cloud", "process"}
_ACTOR_ALIAS = {"": "process", "system": "process", "server": "cloud", "api": "cloud",
                "human": "user", "device": "edge", "client": "user"}


# ── title / eyebrow ──────────────────────────────────────────────────────────


def _title_runs(text: str) -> list[dict[str, Any]]:
    """Title string -> TitleRun[] with the last word accented (kit: <=1 accent)."""
    s = (text or "").strip().rstrip(".")
    if not s:
        return [{"text": "Untitled"}]
    words = s.split()
    if len(words) >= 2:
        return [{"text": " ".join(words[:-1]) + " "}, {"text": words[-1], "accent": True}]
    return [{"text": s}]


def _eyebrow(num: str, text: str) -> dict[str, Any]:
    eb: dict[str, Any] = {"text": text or ""}
    if num:
        eb["num"] = num
    return eb


def _int_pct(detail: str, fallback: int) -> int:
    m = re.search(r"(\d{1,3})", detail or "")
    if m:
        return max(0, min(100, int(m.group(1))))
    return fallback


# ── shape -> archetype ───────────────────────────────────────────────────────


def _archetype_for(shape: str) -> str:
    return {
        "set": "card-set",
        "comparison": "decision-matrix",
        "metric": "framework-tiles",
        "delta": "framework-tiles",
        "trend": "framework-tiles",
        "proportion": "proportion-bars",
        "sequence": "flow-sequence",
        "relationship": "classification-table",
        "verdict": "ask-cta",
        "quote": "scenario-beats",
        "narrative": "disclosure-list",
    }.get(shape, "disclosure-list")


# ── per-archetype slot builders (fed real items_content triples) ──────────────
# Each returns the archetype's `slots` dict, filling its content slot from up to
# `n` triples. A claim with no triples still yields a valid slide (one text row
# from the claim statement) so the viewer never renders an empty archetype.


def _triples(claim: Claim, n: int) -> list[tuple[str, str, str]]:
    ts = [t for t in claim.content_items() if t and (t[0] or t[1])]
    return ts[:n] if ts else []


def _slots_card_set(claim, n, stats):
    ts = _triples(claim, n)
    cards = [{"name": lab or det or "—", **({"desc": det} if det else {})} for lab, det, _m in ts]
    if not cards:
        cards = [{"name": (claim.text or "—")[:80]}]
    slots: dict[str, Any] = {"cards": cards}
    if stats:
        slots["stats"] = stats
    return slots


def _slots_disclosure(claim, n):
    ts = _triples(claim, n)
    rows = []
    for i, (lab, det, meta) in enumerate(ts):
        row: dict[str, Any] = {"mark": str(i + 1), "title": lab or det or "—"}
        if meta:
            row["sub"] = meta
        if det and det != lab:
            row["body"] = det
            row["toggle"] = "Detail"
        rows.append(row)
    if not rows:
        rows = [{"mark": "1", "title": (claim.text or "—")[:120]}]
    return {"rows": rows}


def _slots_framework_tiles(claim, n):
    ts = _triples(claim, n)
    tiles = [{"label": lab or "—", "value": det or "—", **({"desc": meta} if meta else {})}
             for lab, det, meta in ts]
    if not tiles:
        tiles = [{"label": (claim.text or "—")[:40], "value": "—"}]
    return {"tiles": tiles}


def _slots_classification(claim, n):
    ts = _triples(claim, n)
    rows = [{"cells": [lab or "—", det or "—", meta or "—"]} for lab, det, meta in ts]
    if not rows:
        rows = [{"cells": [(claim.text or "—")[:60], "—", "—"]}]
    return {"columns": [{"label": "Item"}, {"label": "Detail"}, {"label": "Note"}], "rows": rows}


def _slots_flow(claim, n):
    ts = _triples(claim, n)
    steps = []
    for i, (lab, det, meta) in enumerate(ts):
        actor = (meta or "").strip().lower()
        actor = actor if actor in _FLOW_ACTORS else _ACTOR_ALIAS.get(actor, "process")
        steps.append({"num": str(i + 1), "actor": actor,
                      "what": (f"{lab} — {det}" if det else lab) or "—"})
    if not steps:
        steps = [{"num": "1", "actor": "process", "what": (claim.text or "—")[:80]}]
    return {"steps": steps}


def _slots_proportion(claim, n):
    ts = _triples(claim, n)
    rows = []
    for lab, det, meta in ts:
        state = meta if meta in ("safe", "caution", "danger") else "safe"
        rows.append({"stage": lab or "—", "pct": _int_pct(det, 50), "state": state,
                     "value": det or "—"})
    if not rows:
        rows = [{"stage": (claim.text or "—")[:40], "pct": 50, "state": "safe", "value": "—"}]
    return {"rows": rows}


def _slots_decision_matrix(claim, n):
    ts = _triples(claim, n)
    options = []
    for lab, det, meta in ts:
        options.append({"label": (lab or "—")[:16], "title": lab or "—",
                        **({"recommended": True} if meta == "recommended" else {}),
                        "lines": [{"k": "detail", "v": det or "—"}]})
    if len(options) < 1:
        options = [{"label": "A", "title": (claim.text or "—")[:40], "lines": [{"k": "", "v": ""}]}]
    return {"options": options}


def _slots_scenario_beats(claim, n):
    ts = _triples(claim, n)
    beats = [{"mark": str(i + 1), "body": (f"{lab}: {det}" if det else lab) or "—",
              **({"who": meta} if meta else {})} for i, (lab, det, meta) in enumerate(ts)]
    if not beats:
        beats = [{"mark": "1", "body": (claim.text or "—")[:200]}]
    return {"beats": beats}


def _slots_ask_cta(claim, n):
    ts = _triples(claim, n)
    items = [(f"{lab} — {det}" if det else lab) or "—" for lab, det, _m in ts]
    if not items:
        items = [(claim.text or "—")[:120]]
    return {"ask": {"title": "The ask", "items": items}}


_SLOT_BUILDERS = {
    "card-set": lambda c, n, extra: _slots_card_set(c, n, extra.get("stats")),
    "disclosure-list": lambda c, n, extra: _slots_disclosure(c, n),
    "framework-tiles": lambda c, n, extra: _slots_framework_tiles(c, n),
    "classification-table": lambda c, n, extra: _slots_classification(c, n),
    "flow-sequence": lambda c, n, extra: _slots_flow(c, n),
    "proportion-bars": lambda c, n, extra: _slots_proportion(c, n),
    "decision-matrix": lambda c, n, extra: _slots_decision_matrix(c, n),
    "scenario-beats": lambda c, n, extra: _slots_scenario_beats(c, n),
    "ask-cta": lambda c, n, extra: _slots_ask_cta(c, n),
}


# ── provenance + L1 visual (PRISM v4 W5 Phase D orphan gates) ─────────────────
# Every ref-carrying claim surfaces its verbatim source span; every topic's L1
# hook carries a REAL visual (a data chart from the claim's own mined magnitudes,
# or — when nothing is chartable — a generated illustration). Never invented: each
# chart datum and quote traces to the claim it was mined from.

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _num(s: str) -> Optional[float]:
    m = _NUM_RE.search((s or "").replace(",", ""))
    return float(m.group()) if m else None


def _prov_for_claim(claim: Claim, grounding: dict[str, Any], artifact_id: str,
                    artifact_title: str) -> Optional[dict[str, Any]]:
    """A Provenance dict for one claim, from its mined evidence quote. None when the
    claim carries no source ref (nothing to surface)."""
    g = grounding.get(claim.id) if grounding else None
    quote = (getattr(g, "source_quote", "") or "").strip() if g else ""
    aid = (claim.source_id or artifact_id or "").strip()
    if not quote or not aid:
        return None
    p: dict[str, Any] = {"claim": claim.id, "quote": quote, "artifactId": aid}
    if artifact_title:
        p["artifactTitle"] = artifact_title
    return p


def _cell_provenance(units: list[RenderedUnit], claims: dict[str, Claim],
                     grounding: dict[str, Any], artifact_id: str,
                     artifact_title: str) -> list[dict[str, Any]]:
    """Source refs for the distinct claims rendered in a cell (dedup by claim id)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for u in units:
        cid = u.dominant_claim
        if cid in seen:
            continue
        seen.add(cid)
        c = claims.get(cid)
        if not c:
            continue
        p = _prov_for_claim(c, grounding, artifact_id, artifact_title)
        if p:
            out.append(p)
    return out


def _bars_from_triples(triples: list[tuple[str, str, str]]) -> Optional[list[dict[str, Any]]]:
    """Normalised bar rows from item triples whose detail carries a magnitude. None
    when fewer than 2 items have a parseable number (not a chart)."""
    parsed = [(lab or "—", _num(det), det or "") for lab, det, _m in triples]
    nums = [n for _lab, n, _d in parsed if n is not None]
    if len(nums) < 2:
        return None
    mx = max(abs(n) for n in nums) or 1.0
    bars: list[dict[str, Any]] = []
    for lab, n, disp in parsed:
        pct = int(round(abs(n) / mx * 100)) if n is not None else 0
        bars.append({"label": lab, "value": pct, "display": disp or (str(n) if n is not None else "—")})
    return bars


def _visual_from_claim(claim: Claim, prov: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A data visual derived from ONE claim's real mined content, or None if the
    claim carries no chartable magnitude or countable set."""
    shape = claim.shape
    triples = [t for t in claim.content_items() if t and (t[0] or t[1])]
    title = (claim.text or "").strip().rstrip(".")[:60]
    src = {"source": prov} if prov else {}

    if shape == "proportion" and triples:
        bars = [{"label": lab or "—", "value": _int_pct(det, 0),
                 "display": det or "—",
                 "state": (meta if meta in ("safe", "caution", "danger") else "safe")}
                for lab, det, meta in triples]
        return {"kind": "proportion", "title": title, "bars": bars, **src}

    if shape in ("metric", "delta") and triples:
        lab, det, meta = triples[0]
        value = det or lab or "—"
        return {"kind": "stat", "title": title,
                "stat": {"value": value, "label": lab or title,
                         **({"sub": meta} if meta else {})}, **src}

    # comparison / trend / set / relationship with numeric detail -> bar chart.
    bars = _bars_from_triples(triples) if shape in ("comparison", "trend", "set", "relationship") else None
    if bars:
        return {"kind": "bar", "title": title, "bars": bars, **src}

    # a countable set with no magnitudes -> the count is the point (a stat).
    if shape in ("set", "relationship", "comparison") and len(triples) >= 2:
        noun = {"set": "items", "relationship": "nodes", "comparison": "options"}[shape]
        return {"kind": "stat", "title": title,
                "stat": {"value": str(len(triples)), "label": noun,
                         "sub": (triples[0][0] or "")[:40]}, **src}
    return None


def _pick_visual(ladder: LadderDoc, grounding: dict[str, Any], artifact_id: str,
                 artifact_title: str,
                 illustrate_fn: Optional[Any] = None) -> Optional[dict[str, Any]]:
    """The L1 hook's REAL visual. Prefers a data chart from the topic's most
    chartable claim; falls back to a generated illustration (``illustrate_fn``)
    so EVERY topic's L1 carries a visual (the §3c orphan gate). Returns None only
    when there is neither chartable data nor an illustrator — the caller decides
    whether that is a gate failure."""
    # priority: the L1 claims first (the hook), then any claim in the topic.
    l1_units = ladder.units.get("L1", [])
    ordered_ids = [u.dominant_claim for u in l1_units] + list(ladder.claims.keys())
    seen: set[str] = set()
    for cid in ordered_ids:
        if cid in seen:
            continue
        seen.add(cid)
        c = ladder.claims.get(cid)
        if not c:
            continue
        prov = _prov_for_claim(c, grounding, artifact_id, artifact_title)
        vis = _visual_from_claim(c, prov)
        if vis:
            return vis
    # nothing chartable -> a generated illustration keeps the gate satisfied.
    if illustrate_fn is not None:
        try:
            svg = illustrate_fn(ladder.l4.title, ladder.l4.body or ladder.l4.title)
        except Exception as exc:  # never let an illustrator outage break the deck
            logger.warning("to_deckdoc: illustrate_fn failed for %r: %s", ladder.topic_id, exc)
            svg = None
        if svg and str(svg).lstrip().startswith("<svg"):
            return {"kind": "illustration", "title": (ladder.l4.title or "")[:60], "svg": str(svg)}
    return None


# ── level -> deck cell ───────────────────────────────────────────────────────


def _primary_body(units: list[RenderedUnit], claims: dict[str, Claim]) -> Optional[RenderedUnit]:
    """The unit that determines the slide archetype: the first non-title body unit
    that carries real per-item content; else the first non-title unit; else None."""
    body = [u for u in units if u.component != "statement-title"]
    with_content = [u for u in body
                    if claims.get(u.dominant_claim) and _triples(claims[u.dominant_claim], u.rendered_items)]
    if with_content:
        return with_content[0]
    return body[0] if body else None


def _cell_for_level(ladder: LadderDoc, level: str, eyebrow_num: str) -> dict[str, Any]:
    """Build a DeckCell for one authoring level from its RenderedUnits."""
    units = ladder.units[level]
    claims = ladder.claims
    title_u = next((u for u in units if u.component == "statement-title"), None)
    title_claim = claims.get(title_u.dominant_claim) if title_u else None
    title_txt = title_claim.text if title_claim else ladder.l4.title
    title_runs = _title_runs(title_txt)
    eyebrow = _eyebrow(eyebrow_num, ladder.l4.title)

    primary = _primary_body(units, claims)
    if primary is None:
        # title-only level -> a single-row disclosure of the headline (valid slide).
        slide = {"archetype": "disclosure-list",
                 "slots": {"eyebrow": eyebrow, "title": title_runs,
                           "rows": [{"mark": "1", "title": (title_txt or "—")[:120]}]}}
        return {"slide": slide}

    pclaim = claims[primary.dominant_claim]
    archetype = _archetype_for(pclaim.shape)
    # supporting metric/set units become card-set stats when the primary is a card-set.
    extra: dict[str, Any] = {}
    if archetype == "card-set":
        stats = []
        for u in units:
            if u is primary or u.component == "statement-title":
                continue
            c = claims.get(u.dominant_claim)
            if c and c.shape in ("metric", "delta") and c.items_content:
                lab, det, _m = c.items_content[0]
                stats.append({"num": det or lab, "lbl": lab})
        if stats:
            extra["stats"] = stats[:3]
    slots = _SLOT_BUILDERS[archetype](pclaim, primary.rendered_items, extra)
    slots["eyebrow"] = eyebrow
    slots["title"] = title_runs
    return {"slide": {"archetype": archetype, "slots": slots}}


def _docview(ladder: LadderDoc) -> dict[str, Any]:
    """L4 reading level -> a doc-view: the topic's verbatim master text + a claims
    digest, as reflow sections (never empty; validate requires >=1 section w/ id)."""
    body = (ladder.l4.body or "").strip()
    sections: list[dict[str, Any]] = [{
        "id": f"{ladder.topic_id}-full", "heading": ladder.l4.title, "level": 1,
        "md": body or ladder.l4.title,
    }]
    # a compact digest of the mined claims with their real per-item content.
    digest_lines: list[str] = []
    for cid in ladder.l4.claim_ids:
        c = ladder.claims.get(cid)
        if not c:
            continue
        if c.items_content:
            for lab, det, meta in c.content_items():
                if lab or det:
                    tail = f" _({meta})_" if meta else ""
                    digest_lines.append(f"- **{lab or '—'}** — {det}{tail}" if det
                                        else f"- **{lab}**{tail}")
        else:
            digest_lines.append(f"- {c.text}")
    if digest_lines:
        sections.append({"id": f"{ladder.topic_id}-claims", "heading": "Details", "level": 2,
                         "md": "\n".join(digest_lines)})
    return {"title": ladder.l4.title, "width": "wide", "sections": sections}


def topic_to_deck(ladder: LadderDoc, topic_index: int, *,
                  grounding: Optional[dict[str, Any]] = None,
                  artifact_id: str = "", artifact_title: str = "",
                  illustrate_fn: Optional[Any] = None,
                  composed: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One LadderDoc -> a DeckTopic dict (levels L0-L2 + l3 doc-view). Attaches the
    §3c L1 visual (deck L0) and the §3a per-cell provenance from the topic's
    grounded evidence spans (Phase D orphan gates).

    ``composed`` (from compose_ladder, keyed by AUTHORING level L1/L2/L3) attaches
    the v4 solver's real kit HTML to each cell. When present the viewer renders the
    composed fragment instead of the 12-archetype downcast; the archetype/slots stay
    on the cell as a fallback for a level the solver couldn't compose."""
    num = f"{topic_index + 1:02d}"
    g = grounding or {}
    levels: dict[str, Any] = {}
    for lvl, deck_lvl in _LADDER_TO_DECK.items():
        if lvl not in ladder.units:
            continue                       # honest level-collapse: a shorter ladder
        cell = _cell_for_level(ladder, lvl, num)
        prov = _cell_provenance(ladder.units.get(lvl, []), ladder.claims, g,
                                artifact_id, artifact_title)
        if prov:
            cell["provenance"] = prov
        if composed and lvl in composed:
            cell["composed"] = composed[lvl]   # real kit render; viewer prefers this
        if deck_lvl == "L0":
            vis = _pick_visual(ladder, g, artifact_id, artifact_title, illustrate_fn)
            if vis:
                cell["visual"] = vis
        levels[deck_lvl] = cell
    return {"id": ladder.topic_id, "title": ladder.l4.title,
            "levels": levels, "l3": _docview(ladder)}


def _compose_all(ladders: list[LadderDoc], brand: str) -> list[Optional[dict[str, Any]]]:
    """Solve every topic's ladder into composed kit fragments, one per topic (topic
    order), with the render-measure guard when a kit browser is available.

    Composition itself never sinks the deck — a browser-less host composes
    un-measured, and a single topic's failed solve falls back to archetype cells.
    But both are DEGRADATIONS, and both are now reported through ``warn_or_fail``
    rather than a log line: under ``prism.strict`` they refuse the build, and with
    strict off they ride out as structured warnings. The same applies to a cell
    whose rendered text does not come from its own claims.
    """
    _fallbacks: list[dict[str, Any]] = []
    from okuro.prism.authoring.to_composed import FIDELITY_FLOOR, compose_ladder
    from okuro.prism.config import warn_or_fail

    def _run(measure_fn: Optional[Any]) -> list[Optional[dict[str, Any]]]:
        res: list[Optional[dict[str, Any]]] = []
        for lad in ladders:
            tid = getattr(lad, "topic_id", "?")
            try:
                res.append(compose_ladder(lad, brand, measure_fn=measure_fn))
            except Exception as exc:  # noqa: BLE001 — one topic never sinks the deck
                logger.warning("to_deckdoc: compose failed for topic %r — archetype fallback: %s",
                               tid, exc)
                # The downcast is a real degradation, not a neutral fallback: the
                # reader gets a 12-archetype slide where a composed kit surface was
                # promised, and until now the only trace was this log line.
                _fallbacks.append({"topic_id": tid, "reason": str(exc)})
                res.append(None)
        return res

    try:
        from okuro.prism.solver.render import make_measure_fn, serving_kit
        with serving_kit() as r:
            composed = _run(make_measure_fn(r))
    except Exception as exc:  # noqa: BLE001 — no browser → compose un-measured, don't crash
        logger.warning("to_deckdoc: kit measure browser unavailable — composing un-measured: %s", exc)
        composed = _run(None)

    # Fidelity is checked HERE, outside the per-topic try/except above — a strict
    # refusal must propagate, not get caught and turned into the very archetype
    # fallback it exists to prevent.
    for lad, cells in zip(ladders, composed):
        for level, cell in (cells or {}).items():
            f = cell.get("fidelity")
            if f is not None and f < FIDELITY_FLOOR:
                warn_or_fail(
                    "cell_content_fidelity",
                    f"composed cell {getattr(lad, 'topic_id', '?')}.{level} renders "
                    f"{1 - f:.0%} words that are not in its own claims "
                    f"(fidelity {f:.2f} < {FIDELITY_FLOOR}) — components "
                    f"{cell.get('component_ids')} are showing fixture text, not the source",
                    topic_id=getattr(lad, "topic_id", "?"), level=level,
                    fidelity=f, components=cell.get("component_ids"),
                )
    for fb in _fallbacks:
        warn_or_fail(
            "compose_fallback",
            f"topic {fb['topic_id']} fell back to the 12-archetype downcast — "
            f"the composed kit surface was not produced: {fb['reason']}",
            **fb,
        )
    return composed


# ── accuracy: authoring per-level report -> deck per-cell map ─────────────────


def _accuracy_cells(reports: list[tuple[AccuracyReport, int]]) -> dict[str, Any]:
    """Merge per-topic AccuracyReports into a DeckAccuracy dict (per-cell keyed
    '<deckLevel>-<topicIndex>', plus the deck-wide flag unions)."""
    cells: dict[str, list[dict[str, Any]]] = {}
    silent: list[str] = []
    mism: list[str] = []
    synth: list[str] = []
    fallb: list[str] = []
    gaps: list[str] = []
    for report, idx in reports:
        for lvl, slide_acc in report.slides.items():
            deck_lvl = _LADDER_TO_DECK.get(lvl)
            if not deck_lvl:
                continue
            cells[f"{deck_lvl}-{idx}"] = slide_acc.to_fields()
        silent += report.silent_deficits
        mism += report.count_mismatches
        synth += report.synthesized
        fallb += report.fallback
        gaps += report.content_gaps
    return {"levels": {}, "cells": cells, "silent_deficits": silent,
            "count_mismatches": mism, "synthesized": synth, "fallback": fallb,
            "content_gaps": gaps}


# ── hero ─────────────────────────────────────────────────────────────────────


def _hero(title: str, tagline: str, personas: list[dict[str, Any]],
          meta: list[dict[str, Any]]) -> dict[str, Any]:
    slots: dict[str, Any] = {
        "eyebrow": _eyebrow("01", "briefing"),
        "title": _title_runs(title),
    }
    if tagline:
        slots["lede"] = tagline
    if personas:
        slots["personas"] = personas[:3]
    if meta:
        slots["meta"] = meta
    return {"slide": {"archetype": "hero", "slots": slots}}


def build_deck_doc(
    ladders: list[LadderDoc],
    accuracies: list[AccuracyReport],
    *,
    deck_id: str,
    title: str,
    brand: str = "okuro",
    tagline: str = "",
    personas: Optional[list[dict[str, Any]]] = None,
    meta: Optional[list[dict[str, Any]]] = None,
    sources: Optional[list[Any]] = None,
    artifact_id: str = "",
    artifact_title: str = "",
    illustrate_fn: Optional[Any] = None,
    lenses: Optional[list[dict[str, Any]]] = None,
    brand_logo: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble a full deck2 DeckDoc from per-topic authoring output.

    ``ladders`` and ``accuracies`` are parallel (one per topic, topic order).
    ``sources`` (optional, parallel) supplies each topic's grounded evidence so the
    §3a provenance affordance and §3c L1 visual can be attached. ``lenses`` +
    ``brand_logo`` carry the Phase D multi-perspective tabs + luminance-aware logo.
    Brand falls back to 'okuro' if it is not one of the accepted viewer brands.
    """
    from okuro.prism.brands import normalise_brand  # single source of truth

    b = normalise_brand(brand)
    groundings = [getattr(s, "grounding", {}) or {} for s in (sources or [])]
    # Solve every ladder into real v4 kit HTML (the crafted 37-component render),
    # so the viewer ships composed components instead of the 12-archetype downcast.
    composed_by_topic = _compose_all(ladders, b)
    topics = [
        topic_to_deck(
            l, i,
            grounding=(groundings[i] if i < len(groundings) else {}),
            artifact_id=artifact_id, artifact_title=artifact_title,
            illustrate_fn=illustrate_fn,
            composed=(composed_by_topic[i] if i < len(composed_by_topic) else None),
        )
        for i, l in enumerate(ladders)
    ]
    acc = _accuracy_cells(list(zip(accuracies, range(len(accuracies)))))
    doc: dict[str, Any] = {
        "id": deck_id, "title": title, "brand": b,
        "hero": _hero(title, tagline, personas or [], meta or []),
        "topics": topics, "accuracy": acc,
    }
    if tagline:
        doc["tagline"] = tagline
    if lenses:
        doc["lenses"] = lenses
    if brand_logo:
        doc["brandLogo"] = brand_logo
    return doc


__all__ = ["build_deck_doc", "topic_to_deck", "_accuracy_cells"]
