# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler → viewer ADAPTER. Serializes A3's flat
#   ComposedDeck (compiler output) into A4's nested DeckDoc JSON (the viewer's
#   deck2/deck-types.ts contract). The two seams were built to slightly different
#   snapshots of the kit-spec contract: A3 emits generic slot field names + a flat
#   slide list with no hero; A4 renders kit-faithful slot names + a nested
#   hero/topics/levels/l3 tree. Both are frozen + independently verified, so this
#   ONE serializer is the single reconciliation layer (DP10): it renames every
#   slot field to what the A4 renderer reads, groups A/B variants into cells,
#   synthesizes the hero from the audience profile + outline, and reflows each L3
#   master_doc into a DocView. `validate_deck_doc` mirrors deck-types.ts so tests
#   can assert BOTH goldens (A3 golden run + A4 demo-deck.json) against one oracle.
# index: to_deck_doc | validate_deck_doc | A4 BrandId | classification-table note
# AGENT_HEADER_END -->
"""ComposedDeck (A3) → DeckDoc (A4) serializer + a structural DeckDoc validator.

The kit (``src/okuro/prism/kit/board/archetypes/*.html``) is the styling
authority; the A4 renderer (``deck2/archetypes.tsx``) emits it byte-faithfully
from typed slot data. This module maps A3's compose output onto exactly those
typed slots. Where A3's model is genuinely more generic than the kit template —
``classification-table`` (A3: generic headers+cells+rowclass; kit/A4:
endpoint·method·verdict·reason) — we map positionally and keep the kit-faithful
column labels; see ``_classification_table``.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from okuro.prism.compiler.archetypes import ARCHETYPE_IDS
from okuro.prism.compiler.ir import AudienceProfile, ComposedDeck, Outline, Slide
from okuro.prism.brands import a4_brands, normalise_brand
from okuro.prism.composer import BUDGET as _PROSE_BUDGET, extract_lede

# Character budget for the synthesized hero lede. The hero is the SHALLOWEST rung,
# so it takes the tightest prose budget the composer defines (~1 sentence) — see
# _synth_hero for the geometry this protects.
HERO_LEDE_BUDGET: int = _PROSE_BUDGET["L1"]

# A4 accepts the brand ids the board kit is calibrated for (deck-types.ts
# BrandId), plus anything OKURO_PRISM_BRANDS adds; anything else → okuro.
# eyebrow kick number = the archetype's kit ordinal (01..12), matching the specimen.
_ARCH_NUM = {a: f"{i + 1:02d}" for i, a in enumerate(ARCHETYPE_IDS)}
_PERSONA_MARKS = ("a", "b", "c")

_ACCENT_RE = re.compile(r"<accent>(.*?)</accent>", re.IGNORECASE | re.DOTALL)


# ── shared field converters ─────────────────────────────────────────────────


def _title_runs(title: Any) -> list[dict[str, Any]]:
    """A3 title string (with at most one ``<accent>…</accent>`` span) → A4
    TitleRun[]. Splits the string into plain + accented runs, order preserved."""
    if isinstance(title, list):  # already runs (defensive)
        return title
    s = "" if title is None else str(title)
    runs: list[dict[str, Any]] = []
    pos = 0
    for m in _ACCENT_RE.finditer(s):
        if m.start() > pos:
            runs.append({"text": s[pos:m.start()]})
        runs.append({"text": m.group(1), "accent": True})
        pos = m.end()
    if pos < len(s):
        runs.append({"text": s[pos:]})
    return runs or [{"text": s}]


def _eyebrow(text: Any, num: Optional[str]) -> dict[str, Any]:
    eb: dict[str, Any] = {"text": "" if text is None else str(text)}
    if num:
        eb["num"] = num
    return eb


_SEVERITY_WARN = {"critical", "major"}


def _bullet_rows(rows: Any, default_toggle: str) -> list[dict[str, Any]]:
    """A3 _ROW {mark,title,sub,body,severity} → A4 BulletRow {mark,warn,title,
    sub,toggle,body}. severity∈{critical,major} → warn; a body implies a toggle."""
    out: list[dict[str, Any]] = []
    for r in rows or []:
        row: dict[str, Any] = {"title": r.get("title", "")}
        if r.get("mark"):
            row["mark"] = str(r["mark"])
        if r.get("sub"):
            row["sub"] = r["sub"]
        if str(r.get("severity", "")).lower() in _SEVERITY_WARN or r.get("warn"):
            row["warn"] = True
        if r.get("body"):
            row["body"] = r["body"]
            row["toggle"] = r.get("toggle") or default_toggle
        out.append(row)
    return out


def _persona(p: dict[str, Any], idx: int) -> dict[str, Any]:
    mark = p.get("mark") if p.get("mark") in _PERSONA_MARKS else _PERSONA_MARKS[idx % 3]
    return {"mark": mark, "name": p.get("name", ""), "lens": p.get("lens", "")}


_CALLOUT_TONE = {"info": "info", "warn": "warn", "risk": "risk",
                 "decision": "decision", "wow": "wow"}


def _callout(c: Any) -> Optional[dict[str, Any]]:
    if not c:
        return None
    out: dict[str, Any] = {"body": c.get("text") or c.get("body") or ""}
    if c.get("label"):
        out["label"] = c["label"]
    variant = _CALLOUT_TONE.get(str(c.get("tone") or c.get("variant") or "").lower())
    if variant:
        out["variant"] = variant
    return out


# ── per-archetype slot converters (A3 slots → A4 slots) ──────────────────────


def _base(slots: dict[str, Any], archetype: str) -> dict[str, Any]:
    """eyebrow+title shared by every archetype."""
    return {
        "eyebrow": _eyebrow(slots.get("eyebrow"), _ARCH_NUM.get(archetype)),
        "title": _title_runs(slots.get("title")),
    }


def _hero(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "hero")
    if s.get("lede"):
        out["lede"] = s["lede"]
    if s.get("personas"):
        out["personas"] = [_persona(p, i) for i, p in enumerate(s["personas"])]
    if s.get("meta"):
        out["meta"] = [{"label": m.get("label", ""), "value": m.get("value", "")}
                       for m in s["meta"]]
    return out


def _lens_reframe(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "lens-reframe")
    tabs = s.get("lens-tabs") or []
    panes = s.get("lens-pane") or []
    lenses = []
    for i, tab in enumerate(tabs):
        rows = panes[i] if i < len(panes) else []
        lenses.append({"persona": _persona(tab, i),
                       "rows": _bullet_rows(rows, "Detail")})
    out["lenses"] = lenses
    return out


def _disclosure_list(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "disclosure-list")
    out["rows"] = _bullet_rows(s.get("rows"), "Evidence")
    return out


def _scenario_beats(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "scenario-beats")
    out["beats"] = [{"mark": b.get("time", ""), "body": b.get("body", ""),
                     **({"who": b["actor"]} if b.get("actor") else {})}
                    for b in s.get("beats") or []]
    co = _callout(s.get("callout"))
    if co:
        out["callout"] = co
    return out


def _decision_matrix(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "decision-matrix")
    opts = []
    for o in s.get("options") or []:
        opt: dict[str, Any] = {"label": o.get("label", ""), "title": o.get("name", ""),
                               "lines": [{"k": ln.get("label", ""), "v": ln.get("value", "")}
                                         for ln in o.get("lines") or []]}
        if o.get("recommended"):
            opt["recommended"] = True
        opts.append(opt)
    out["options"] = opts
    return out


def _framework_tiles(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "framework-tiles")
    out["tiles"] = [{"label": t.get("label", ""), "value": t.get("value", ""),
                     **({"desc": t["desc"]} if t.get("desc") else {})}
                    for t in s.get("tiles") or []]
    tbl = s.get("table")
    if tbl and tbl.get("headers"):
        out["table"] = {
            "headers": list(tbl["headers"]),
            "rows": [[{"text": str(cell)} for cell in row] for row in tbl.get("rows") or []],
        }
    return out


def _binary_choice(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "binary-choice")
    paths = []
    for p in s.get("paths") or []:
        tone = "accent" if str(p.get("tone")).lower() in ("recommend", "accent") else "warn"
        paths.append({"tone": tone, "eyebrow": p.get("eyebrow", ""), "title": p.get("title", ""),
                      "body": p.get("body", ""),
                      **({"footer": p["footer"]} if p.get("footer") else {})})
    out["paths"] = paths
    co = _callout(s.get("callout"))
    if co:
        out["callout"] = co
    return out


# rowclass / verdict tone → the kit's semantic dot colour (deck theme vars).
_VERDICT_COLOR = {"essential": "var(--ok)", "ok": "var(--ok)",
                  "nogo": "var(--warn)", "danger": "var(--warn)", "warn": "var(--warn)",
                  "redesign": "var(--gold)", "gold": "var(--gold)",
                  "redundant": "var(--fg-tertiary)"}
_VERDICTS = {"essential", "nogo", "redesign", "redundant"}


def _classification_table(s: dict[str, Any]) -> dict[str, Any]:
    """A3 verdict/classification table → A4 slot-driven table. A3's compose already
    emits real ``table.headers`` + ``rows[].cells`` (+ optional ``rowclass``); we
    pass those straight through as ``columns`` + ``rows.cells`` so the archetype
    carries ANY classification (not only API audits). Headers are tagged with a
    ``method``/``verdict`` role only when they match the kit's API-audit set, so
    the specimen/demo still render method+verdict badges; a generic table (e.g.
    'Altitude level' × 'Prism coverage status') renders its own labels with no
    empty columns. rowclass → the row's verdict highlight."""
    out = _base(s, "classification-table")
    callout = s.get("callout")
    if callout and callout.get("total"):
        stats = []
        for st in callout.get("stats") or []:
            color = _VERDICT_COLOR.get(str(st.get("tone", "")).lower(), "var(--fg-tertiary)")
            stats.append({"color": color, "text": f"{st.get('count','')} {st.get('label','')}".strip()})
        out["summary"] = {"text": callout["total"], "stats": stats}

    tbl = s.get("table") or {}
    headers = [str(h) for h in (tbl.get("headers") or [])] or ["Item", "Status"]

    def _role(label: str) -> Optional[str]:
        low = label.strip().lower()
        if low == "method":
            return "method"
        if low == "verdict":
            return "verdict"
        return None

    out["columns"] = [{"label": h, **({"role": r} if (r := _role(h)) else {}) } for h in headers]
    rows = []
    for r in tbl.get("rows") or []:
        cells = [str(c) for c in (r.get("cells") or [])]
        row: dict[str, Any] = {"cells": cells}
        rc = str(r.get("rowclass", "")).lower()
        if rc in _VERDICTS:
            row["verdict"] = rc
        rows.append(row)
    out["rows"] = rows
    return out


def _flow_sequence(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "flow-sequence")
    if s.get("lede"):
        out["lede"] = s["lede"]
    steps = []
    for st in s.get("steps") or []:
        actor = str(st.get("actor") or "").lower()
        step: dict[str, Any] = {"num": str(st.get("n", "")),
                                "actor": actor if actor in ("user", "edge", "cloud") else "process",
                                "what": st.get("what", "")}
        if st.get("tool"):
            step["tool"] = st["tool"]
        if st.get("value"):
            step["ms"] = str(st["value"])
        steps.append(step)
    out["steps"] = steps
    return out


def _proportion_bars(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "proportion-bars")
    if s.get("lede"):
        out["lede"] = s["lede"]
    out["rows"] = [{"stage": r.get("label", ""),
                    "pct": r.get("pct", 0),
                    "state": r.get("level") or "safe",
                    "value": str(r.get("value", ""))}
                   for r in s.get("rows") or []]
    return out


def _card_set(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "card-set")
    if s.get("stats"):
        out["stats"] = [{"num": str(st.get("num", "")), "lbl": st.get("label", ""),
                         **({"numState": st["tone"]} if st.get("tone") else {})}
                        for st in s["stats"]]
    out["cards"] = [{"name": c.get("name", ""),
                     **({"icon": c["icon"]} if c.get("icon") else {}),
                     **({"desc": c["desc"]} if c.get("desc") else {})}
                    for c in s.get("cards") or []]
    return out


def _ask_cta(s: dict[str, Any]) -> dict[str, Any]:
    out = _base(s, "ask-cta")
    ask = s.get("ask") or {}
    out["ask"] = {"title": ask.get("title", ""), "items": list(ask.get("items") or [])}
    if s.get("arch"):
        out["arch"] = [{"label": a.get("label", ""), "value": a.get("value", ""),
                        **({"ok": True} if str(a.get("state")).lower() == "ok" else {})}
                       for a in s["arch"]]
    return out


_TO_A4 = {
    "hero": _hero,
    "lens-reframe": _lens_reframe,
    "disclosure-list": _disclosure_list,
    "scenario-beats": _scenario_beats,
    "decision-matrix": _decision_matrix,
    "framework-tiles": _framework_tiles,
    "binary-choice": _binary_choice,
    "classification-table": _classification_table,
    "flow-sequence": _flow_sequence,
    "proportion-bars": _proportion_bars,
    "card-set": _card_set,
    "ask-cta": _ask_cta,
}


def _slide_to_a4(slide: Slide) -> dict[str, Any]:
    conv = _TO_A4.get(slide.archetype)
    if conv is None:
        raise ValueError(f"deck_adapter: no A4 converter for archetype {slide.archetype!r}")
    out: dict[str, Any] = {"archetype": slide.archetype, "slots": conv(slide.slots or {})}
    # Carry the fail-soft flag through so the critic (and any UI badge) can see
    # a code-synthesized cell; A4 ignores unknown keys at runtime.
    if getattr(slide, "synthesized", False):
        out["synthesized"] = True
    return out


# ── L3 master_doc → DocView ───────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")


def _slug(text: str, i: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or f"s{i}"


def _docview_from_master(master_doc: str, topic_title: str) -> dict[str, Any]:
    """Reflow a topic's markdown master_doc into A4 DocSection[]. Splits on ATX
    headings (#/##/###); a headingless body becomes one overview section."""
    lines = (master_doc or "").splitlines()
    sections: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal cur, buf
        if cur is not None:
            body = "\n".join(buf).strip()
            if body:
                cur["md"] = body
            sections.append(cur)
        buf = []

    for ln in lines:
        m = _HEADING_RE.match(ln.strip())
        if m:
            flush()
            level = 1 if len(m.group(1)) == 1 else 2
            heading = m.group(2).strip()
            cur = {"id": _slug(heading, len(sections)), "heading": heading, "level": level}
        else:
            if cur is None:
                cur = {"id": "overview", "heading": topic_title or "Overview", "level": 1}
            buf.append(ln)
    flush()
    if not sections:
        sections = [{"id": "overview", "heading": topic_title or "Overview", "level": 1,
                     "md": (master_doc or "").strip()}]
    return {"title": f"{topic_title} — full context" if topic_title else "Full context",
            "width": "wide", "sections": sections}


# ── hero synthesis ────────────────────────────────────────────────────────────


def _synth_hero(*, deck_title: str, tagline: str, profile: AudienceProfile,
                outline: Outline, brand: str) -> dict[str, Any]:
    """A3 emits no deck-level hero; synthesize one (archetype 'hero') from the
    audience profile (personas) + outline (lede) + the source title."""
    # A source artifact title can be long/descriptive ("X — gap map + Phase A/B/C
    # build plan"); the hero SLIDE wants a punchy line (kit title cap). Take the
    # segment before the first separator, and word-cap it, so the hero reads clean.
    # DeckDoc.title keeps the full title for the nav/topbar.
    head = re.split(r"\s[—–:|]\s|\s-\s", (deck_title or "").strip())[0].strip()
    hw = head.split()
    if len(hw) > 7:                       # still long → trim to a headline length
        head = " ".join(hw[:7])
        hw = head.split()
    if len(hw) >= 2:
        title_runs = [{"text": " ".join(hw[:-1]) + " "}, {"text": hw[-1], "accent": True}]
    else:
        title_runs = [{"text": head or "Briefing"}]
    slots: dict[str, Any] = {
        "eyebrow": {"num": "01", "text": (brand.title() + " · board brief") if brand else "Board brief"},
        "title": title_runs,
    }
    # The arc is a whole narrative ROADMAP (measured ~120 words on a real deck),
    # and it was dumped into the hero lede verbatim while the title right above was
    # word-capped. A 409px lede pushed the hero's content to 1018px inside the
    # canvas's 724px budget; because `.slide` is justify-content:center +
    # overflow:hidden, the excess was clipped at BOTH ends — the eyebrow vanished
    # off the top and the meta strip off the bottom. Budget it like every other
    # shallow rung: extract_lede keeps whole sentences only (never rewrites, so it
    # cannot introduce a fact) up to the tightest rung budget.
    lede = tagline or (outline.arc if outline else "")
    if lede:
        slots["lede"] = extract_lede(lede, HERO_LEDE_BUDGET)
    personas = [
        {"mark": _PERSONA_MARKS[i % 3], "name": r.name,
         "lens": (r.role or r.lens or "").strip()}
        for i, r in enumerate((profile.recipients if profile else [])[:3])
    ]
    if personas:
        slots["personas"] = personas
    return {"slide": {"archetype": "hero", "slots": slots}}


# ── top-level serializer ──────────────────────────────────────────────────────


def to_deck_doc(
    composed: ComposedDeck,
    outline: Outline,
    profile: AudienceProfile,
    *,
    deck_id: str,
    deck_title: str,
    tagline: str = "",
    brand: Optional[str] = None,
) -> dict[str, Any]:
    """Serialize a ComposedDeck (+ its outline/profile) into an A4 DeckDoc JSON dict."""
    raw_brand = (brand if brand is not None else composed.brand) or "okuro"
    a4_brand = normalise_brand(raw_brand)
    titles = {t.id: t.title for t in outline.topics} if outline else {}

    # group L0-L2 slides by (topic, level) → A/B; collect L3 doc bodies.
    by_cell: dict[tuple[str, int], dict[str, Slide]] = {}
    docs: dict[str, Slide] = {}
    for s in composed.slides:
        if s.archetype == "doc" or s.level == 3:
            docs[s.topic_id] = s
        else:
            by_cell.setdefault((s.topic_id, s.level), {})[s.variant] = s

    topics: list[dict[str, Any]] = []
    for tid in composed.topic_order:
        topic: dict[str, Any] = {"id": tid, "title": titles.get(tid, tid), "levels": {}}
        for level in (0, 1, 2):
            group = by_cell.get((tid, level))
            if not group:
                continue
            primary = group.get("A") or next(iter(group.values()))
            cell: dict[str, Any] = {"slide": _slide_to_a4(primary)}
            alts = [_slide_to_a4(v) for k, v in group.items() if v is not primary]
            if alts:
                cell["alternates"] = alts
                cell["pick"] = 0
            topic["levels"][f"L{level}"] = cell
        doc = docs.get(tid)
        if doc:
            topic["l3"] = _docview_from_master(doc.slots.get("body", ""), titles.get(tid, ""))
        topics.append(topic)

    hero = _synth_hero(deck_title=deck_title, tagline=tagline, profile=profile,
                       outline=outline, brand=a4_brand)
    doc: dict[str, Any] = {"id": deck_id, "title": deck_title, "brand": a4_brand,
                           "hero": hero, "topics": topics}
    if tagline:
        doc["tagline"] = tagline
    return doc


# ── structural DeckDoc validator (mirrors deck2/deck-types.ts) ────────────────

_A4_ARCHETYPES = set(ARCHETYPE_IDS)


def validate_deck_doc(doc: Any) -> list[str]:
    """Structural check of a DeckDoc against the A4 renderer's expectations
    (deck2/deck-types.ts). Returns a list of human-readable errors; empty = valid.
    The shared oracle both goldens are asserted against."""
    errs: list[str] = []

    def req(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(msg)

    if not isinstance(doc, dict):
        return ["deck: not an object"]
    for k in ("id", "title", "brand", "hero", "topics"):
        req(k in doc, f"deck missing required key {k!r}")
    _brands = a4_brands()
    req(doc.get("brand") in _brands, f"deck.brand {doc.get('brand')!r} not in {sorted(_brands)}")

    def check_slide(where: str, slide: Any) -> None:
        if not isinstance(slide, dict):
            errs.append(f"{where}: slide not an object")
            return
        arch = slide.get("archetype")
        req(arch in _A4_ARCHETYPES, f"{where}: archetype {arch!r} not one of the 12")
        slots = slide.get("slots")
        if not isinstance(slots, dict):
            errs.append(f"{where}: slots missing")
            return
        req(isinstance(slots.get("title"), list) and bool(slots["title"]),
            f"{where}: title must be a non-empty TitleRun[]")
        for i, run in enumerate(slots.get("title") or []):
            req(isinstance(run, dict) and isinstance(run.get("text"), str),
                f"{where}: title run {i} needs a text string")
        eb = slots.get("eyebrow")
        req(isinstance(eb, dict) and isinstance(eb.get("text"), str),
            f"{where}: eyebrow must be {{text}}")

    _VISUAL_KINDS = {"bar", "stat", "proportion", "illustration"}

    def check_cell(where: str, cell: Any) -> None:
        if not isinstance(cell, dict) or "slide" not in cell:
            errs.append(f"{where}: cell needs a slide")
            return
        check_slide(f"{where}.slide", cell["slide"])
        for j, alt in enumerate(cell.get("alternates") or []):
            check_slide(f"{where}.alternates[{j}]", alt)
        # Phase D optional fields — validated only when present (back-compat).
        for lid, ls in (cell.get("lensSlides") or {}).items():
            check_slide(f"{where}.lensSlides[{lid}]", ls)
        vis = cell.get("visual")
        if vis is not None:
            req(isinstance(vis, dict) and vis.get("kind") in _VISUAL_KINDS,
                f"{where}.visual: kind must be one of {_VISUAL_KINDS}")
            if isinstance(vis, dict) and vis.get("kind") in ("bar", "proportion"):
                req(isinstance(vis.get("bars"), list) and bool(vis["bars"]),
                    f"{where}.visual: {vis.get('kind')} needs a non-empty bars[]")
            if isinstance(vis, dict) and vis.get("kind") == "stat":
                req(isinstance(vis.get("stat"), dict) and isinstance(vis["stat"].get("value"), str),
                    f"{where}.visual: stat needs {{value,label}}")
            if isinstance(vis, dict) and vis.get("kind") == "illustration":
                req(isinstance(vis.get("svg"), str) and vis["svg"].lstrip().startswith("<svg"),
                    f"{where}.visual: illustration needs an inline <svg> string")
        for pi, pv in enumerate(cell.get("provenance") or []):
            req(isinstance(pv, dict) and isinstance(pv.get("quote"), str)
                and isinstance(pv.get("artifactId"), str),
                f"{where}.provenance[{pi}]: needs {{quote, artifactId}}")

    hero = doc.get("hero")
    check_cell("hero", hero)
    if isinstance(hero, dict) and isinstance(hero.get("slide"), dict):
        req(hero["slide"].get("archetype") == "hero", "hero.slide.archetype must be 'hero'")

    topics = doc.get("topics")
    if not isinstance(topics, list) or not topics:
        errs.append("deck.topics must be a non-empty array")
        return errs
    for ti, t in enumerate(topics):
        w = f"topics[{ti}]"
        if not isinstance(t, dict):
            errs.append(f"{w}: not an object")
            continue
        req(isinstance(t.get("id"), str) and bool(t["id"]), f"{w}: missing id")
        req(isinstance(t.get("title"), str) and bool(t["title"]), f"{w}: missing title")
        levels = t.get("levels")
        if not isinstance(levels, dict):
            errs.append(f"{w}: levels missing")
            continue
        for lk, cell in levels.items():
            req(lk in ("L0", "L1", "L2"), f"{w}: bad level key {lk!r}")
            check_cell(f"{w}.{lk}", cell)
        l3 = t.get("l3")
        if l3 is not None:
            secs = l3.get("sections") if isinstance(l3, dict) else None
            req(isinstance(secs, list) and bool(secs), f"{w}.l3: sections must be non-empty")
            for si, sec in enumerate(secs or []):
                req(isinstance(sec, dict) and isinstance(sec.get("heading"), str),
                    f"{w}.l3.sections[{si}]: heading required")
                req(isinstance(sec.get("id"), str) and bool(sec.get("id")),
                    f"{w}.l3.sections[{si}]: id required")

    # Phase D: multi-perspective lens tabs + luminance-aware brand logo.
    lenses = doc.get("lenses")
    if lenses is not None:
        req(isinstance(lenses, list) and bool(lenses), "deck.lenses must be a non-empty array when present")
        for li, ln in enumerate(lenses or []):
            req(isinstance(ln, dict) and isinstance(ln.get("id"), str) and bool(ln["id"]),
                f"deck.lenses[{li}]: id required")
            p = ln.get("persona") if isinstance(ln, dict) else None
            req(isinstance(p, dict) and isinstance(p.get("name"), str),
                f"deck.lenses[{li}]: persona {{mark,name,lens}} required")
    logo = doc.get("brandLogo")
    if logo is not None:
        req(isinstance(logo, dict)
            and isinstance(logo.get("dark"), str) and logo["dark"].lstrip().startswith("<svg")
            and isinstance(logo.get("light"), str) and logo["light"].lstrip().startswith("<svg"),
            "deck.brandLogo: dark+light must be inline <svg> strings")
    return errs
