# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 — component fragment builders. Emit each component as an
#   HTML fragment whose TAG+CLASS tree matches the W1 gallery specimen (the
#   rendered==gallery contract compares tag+class trees, text/attrs elided). The
#   ext components (in the contract corpus) reproduce the gallery structure
#   exactly at any fill; base components reuse the shipped kit CSS classes. NO
#   styling is authored here beyond kit classes — geometry is the grid composer's.
# index:
#   build(component, content) dispatch + per-component builders
#   EXT_MAX_FILL (contract corpus content) / gallery_max_html
# AGENT_HEADER_END -->
"""Component fragment builders (the pipeline's render vocabulary).

Skeleton parity is the contract (contract_test.check_contract): two fragments
are equal iff their tag+sorted-class trees match, with text and attributes
elided. So a builder only has to reproduce the gallery's element/class nesting —
which it does by construction here, copied from the W1 gallery source. The ext
builders are parameterised by item count so the same code renders both the
contract corpus (gallery max fill) and arbitrary fixture content.
"""
from __future__ import annotations

import re
from html import escape
from typing import Any

Content = dict[str, Any]

# ── non-degenerate synthetic content (W2 test infrastructure) ─────────────────
# The proof fixtures are synthetic, but item-grid builders used to emit N IDENTICAL
# cells ("Item"/"value"/"Do the thing"/"-92%"). That degeneracy contaminates the
# layout-only vision judge (5 identical cards, uniform metrics read as a placeholder
# wall, not a composition). These deterministic pools give DISTINCT items with
# varied text lengths (short/typical/long per the W1 char-bands) so the judge grades
# geometry, not repetition. No real-world content — just non-degenerate synthetic.
# Indexed by cell position; deterministic (determinism gate stays green).
_CARD_EYEBROWS = ["MODULE", "SURFACE", "LAYER", "PLANE", "STAGE"]
_CARD_TITLES = ["Retrieval", "Indexing", "Ranking", "Caching", "Routing", "Embedding"]
_CARD_BODIES = [                                     # short · typical · long, cycled
    "Finds the span fast.",
    "Shapes the context window before the model ever sees a token.",
    "Walks every root, ranks by semantic distance, and returns the 2 KB answer.",
]
_CARD_FOOTERS = ["okuro", "cortex", "arXiv:2401 · 2026-07", "internal", "kg"]  # [2] = provenance
_NODE_LABELS = ["CONTROL", "DATA", "AGENT", "BUS", "STORE", "EDGE"]
_NODE_VALUES = ["control plane", "data plane", "agent plane", "message bus", "vector store", "edge cache"]
_FLOW_ACTIONS = ["Parse the request", "Embed the query", "Fan out to roots",
                 "Rank the candidates", "Compose the answer", "Stream to the client",
                 "Log the trace", "Cache the result"]
_FLOW_TOOLS = ["router", "encoder", "cortex", "ranker", "composer", "sse", "kg", "redis"]
_METRIC_LABELS = ["Recall@5", "Context", "Latency", "Cost", "Hit rate"]
_METRIC_VALUES = ["0.92", "−92%", "38ms", "$0.004", "×3.1"]
_METRIC_DESCS = ["vs grep 0.28", "2 KB not 25 KB", "p95 end-to-end", "per query", "warm cache"]
_MATRIX_FEATURES = ["Semantic recall", "Latency p95", "Multi-repo", "Streaming",
                    "Offline", "Rerank", "Index size", "Cost / 1k queries",
                    "Incremental", "Symbol-aware", "Fuzzy match", "Cross-lang",
                    "Provenance", "Auth-scoped", "Cache", "Telemetry"]
_MATRIX_COLS = ["cortex", "grep", "ripgrep", "sourcegraph"]


def _pick(pool: list[str], i: int) -> str:
    return pool[i % len(pool)]


_CONTENT_W_PX = 1472                                  # 1600 canvas - 2*64 padding


def _natural_cols(col_span: int, min_card_px: int, gap: int = 24) -> int:
    """The column count the kit's auto-fit grid naturally lands on at this width —
    the SAME formula the W1 height model uses (heights._cards_per_row). Keeping the
    tiling aligned to it means height stays what the model predicts (no inflation)."""
    colpx = col_span / 12 * _CONTENT_W_PX
    return max(1, int((colpx + gap) // (min_card_px + gap)))


def _orphan_free_cols(n: int, col_span: int, min_card_px: int) -> int:
    """Width-aware, orphan-free column count: start from the natural auto-fit count,
    and ONLY reduce by one when the last row would hold a single lone item (n %
    cols == 1) — the '5+1' / '2+1' dead-void class the judge flagged at L2. Never
    increases columns beyond natural, so cards never cram below their min width and
    the rendered height stays what the height model predicts (row count unchanged
    in the orphan case: ceil(6/5)=2 == ceil(6/4)=2)."""
    nat = _natural_cols(col_span, min_card_px)
    if n <= nat or nat <= 1:
        return max(1, min(n, nat))
    return nat - 1 if n % nat == 1 else nat


def _tiled_grid(cls: str, cells_html: str, n: int, content: "Content",
                min_card_px: int = 260) -> str:
    """An item grid with a width-aware, orphan-free column count (kills the lone-item
    hole). Class unchanged (rendered==gallery skeleton parity elides inline style
    attrs); only the column count is pinned, and only when it avoids an orphan."""
    cols = _orphan_free_cols(n, int(content.get("col_span", 12)), min_card_px)
    return (f'<div class="{cls}" style="grid-template-columns:repeat({cols},minmax(0,1fr))">'
            f'{cells_html}</div>')


def _text_only(content: Content) -> bool:
    """A claim that carries prose but NO per-item content."""
    return (not (content.get("cells") or ())
            and bool(str(content.get("text") or "").strip()))


def _items(content: Content, default: int = 3) -> int:
    """How many rows/cards/bars to emit.

    A text-only claim has ONE thing to say. Emitting N rows of it — or N rows of
    synthetic fill — invents cardinality the claim does not have, which is how a
    real deck ended up with eight identical "An ask" bullets. The contract
    corpus is unaffected: ``gallery_max_html`` passes items with no text and no
    cells, so it still renders at gallery max fill.
    """
    if _text_only(content):
        return 1
    return int(content.get("items", default))


def _txt(content: Content, key: str = "text", default: str = "") -> str:
    return escape(str(content.get(key, default)))


def _cells(content: Content) -> list:
    """The real per-item (label, detail, meta) triples bound by the pipeline.

    A text-only claim degrades to ONE cell carrying its own prose rather than to
    the synthetic pools — the claim said something, and showing the reader a
    fixture instead of it is the defect this whole phase exists to remove. Only
    a claim with neither cells NOR text (the contract corpus) reaches the
    synthetic path.
    """
    real = list(content.get("cells") or [])
    if real:
        return real
    if _text_only(content):
        return [(str(content["text"]).strip(), "", "")]
    return []


_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _bar_value(real: "tuple[str, str, str] | None", i: int) -> int:
    """A bar height from the item's own number when it carries one, else the
    deterministic synthetic ramp. Clamped to the gallery's 30-190 band so a real
    outlier cannot push a bar outside the plot."""
    if real:
        for field in (real[1], real[2], real[0]):
            m = _NUM.search(field or "")
            if m:
                try:
                    v = abs(float(m.group().replace(",", ".")))
                except ValueError:
                    continue
                return int(max(30, min(190, v if v > 1 else v * 190)))
    return 30 + ((i * 53) % 160)


def _triple(cells: list, i: int) -> tuple[str, str, str] | None:
    if i < len(cells):
        t = cells[i]
        return (str(t[0]), str(t[1]) if len(t) > 1 else "", str(t[2]) if len(t) > 2 else "")
    return None


# ── base components (reuse shipped kit CSS classes; not in contract corpus) ────

def _statement_title(c: Content) -> str:
    t = _txt(c, "text", "One claim per slide")
    return f'<div class="slide-title"><span class="accent">{t}</span></div>'


def _lede(c: Content) -> str:
    return f'<p class="slide-lede">{_txt(c, "text", "A supporting sub-headline.")}</p>'


def _prose(c: Content) -> str:
    return f'<div class="prose"><p>{_txt(c, "text", "Body prose.")}</p></div>'


def _callout(c: Content) -> str:
    return (f'<div class="callout"><div class="callout-label">Note</div>'
            f'<p>{_txt(c, "text", "Highlight box.")}</p></div>')


def _card_set(c: Content) -> str:
    n = _items(c, 3)
    cs = _cells(c)

    def card(i: int) -> str:
        real = _triple(cs, i)
        if real:
            label, detail, meta = real
            return (f'<div class="card"><div class="card-eyebrow">{escape(f"{i + 1:02d}")}</div>'
                    f'<h3>{escape(label)}</h3><p>{escape(detail)}</p>'
                    f'<div class="card-footer">{escape(meta)}</div></div>')
        return (f'<div class="card"><div class="card-eyebrow">{escape(_pick(_CARD_EYEBROWS, i))}</div>'
                f'<h3>{escape(_pick(_CARD_TITLES, i))}</h3>'
                f'<p>{escape(_pick(_CARD_BODIES, i))}</p>'
                f'<div class="card-footer">{escape(_pick(_CARD_FOOTERS, i))}</div></div>')
    cards = "".join(card(i) for i in range(n))
    return _tiled_grid("card-grid", cards, n, c, min_card_px=260)


def _metric_tiles(c: Content) -> str:
    n = _items(c, 3)
    cs = _cells(c)

    def tile(i: int) -> str:
        real = _triple(cs, i)
        label, value, desc = (real if real
                              else (_pick(_METRIC_LABELS, i), _pick(_METRIC_VALUES, i),
                                    _pick(_METRIC_DESCS, i)))
        return (f'<div class="card"><div class="card-label">{escape(label)}</div>'
                f'<div class="card-value">{escape(value)}</div>'
                f'<div class="card-desc">{escape(desc)}</div></div>')
    cards = "".join(tile(i) for i in range(n))
    return _tiled_grid("card-grid", cards, n, c, min_card_px=260)


def _stat_row(c: Content) -> str:
    n = _items(c, 4)
    cs = _cells(c)

    def cell(i: int) -> str:
        real = _triple(cs, i)
        num, lbl = (real[1] or real[0], real[0]) if real else ("48GB", "metric")
        return (f'<div class="stat-cell"><div class="stat-num">{escape(num)}</div>'
                f'<div class="stat-lbl">{escape(lbl)}</div></div>')
    return f'<div class="stat-row">{"".join(cell(i) for i in range(n))}</div>'


def _beat_list(c: Content) -> str:
    n = _items(c, 5)
    cs = _cells(c)

    def row(i: int) -> str:
        real = _triple(cs, i)
        what = (f"{real[0]} — {real[1]}".rstrip(" —") if real else "A step in the sequence")
        return (f'<li class="beat-row"><span class="beat-num">{i + 1}</span>'
                f'<span class="beat-what">{escape(what)}</span></li>')
    return f'<ul class="beat-list">{"".join(row(i) for i in range(n))}</ul>'


def _option_grid(c: Content) -> str:
    n = _items(c, 3)
    cs = _cells(c)

    def card(i: int) -> str:
        real = _triple(cs, i)
        label, title, line = (
            ("ABCDEF"[i % 6], real[0], real[1] or real[2]) if real else ("A", "Option", "Flat"))
        return ('<div class="option-card">'
                f'<div class="option-label">{escape(label)}</div><h3>{escape(title)}</h3>'
                f'<div class="option-line"><span class="k">Cost</span>'
                f'<span class="v">{escape(line)}</span></div></div>')
    return _tiled_grid("option-grid", "".join(card(i) for i in range(n)), n, c, min_card_px=260)


def _proportion_bars(c: Content) -> str:
    n = _items(c, 4)
    cs = _cells(c)

    def row(i: int) -> str:
        real = _triple(cs, i)
        label, value = (real[0], real[1] or real[2]) if real else ("Segment", "42%")
        return ('<div class="prop-row">'
                f'<span class="prop-label">{escape(label)}</span>'
                '<div class="prop-track"><div class="prop-fill"></div></div>'
                f'<span class="prop-value">{escape(value)}</span></div>')
    return f'<div class="proportion-bars">{"".join(row(i) for i in range(n))}</div>'


def _flow_steps(c: Content) -> str:
    n = _items(c, 6)
    cs = _cells(c)

    def step(i: int) -> str:
        real = _triple(cs, i)
        what, tool = (real[0], real[2] or real[1]) if real else (_pick(_FLOW_ACTIONS, i), _pick(_FLOW_TOOLS, i))
        return (f'<div class="flow-step"><span class="flow-num">{i + 1}</span>'
                f'<span class="flow-what">{escape(what)}</span>'
                f'<span class="flow-tool">{escape(tool)}</span></div>')
    return f'<div class="flow-sequence">{"".join(step(i) for i in range(n))}</div>'


def _phase_row(c: Content) -> str:
    n = _items(c, 4)
    cs = _cells(c)
    inner = []
    for i in range(n):
        real = _triple(cs, i)
        inner.append(f'<span class="phase-item">{escape(real[0] if real else "phase")}</span>')
        if i < n - 1:
            inner.append('<span class="phase-arrow">→</span>')
    return f'<div class="phase-row">{"".join(inner)}</div>'


def _arch_diagram(c: Content) -> str:
    n = _items(c, 3)
    cs = _cells(c)

    def box(i: int) -> str:
        real = _triple(cs, i)
        label, value = (real[0], real[1]) if real else (_pick(_NODE_LABELS, i), _pick(_NODE_VALUES, i))
        return (f'<div class="arch-item"><div class="arch-item-label">{escape(label)}</div>'
                f'<div class="arch-item-value">{escape(value)}</div></div>')
    boxes = "".join(box(i) for i in range(n))
    return _tiled_grid("arch-diagram", boxes, n, c, min_card_px=160)


def _pipeline_table(c: Content) -> str:
    n = _items(c, 5)
    cs = _cells(c)
    # The header is the claim's own column vocabulary when the pipeline bound one;
    # the gallery's Layer/Edge/Cloud/Cost is the no-content fallback, not a caption
    # to stamp on real data.
    hdr = [str(h) for h in (c.get("headers") or [])][:4]
    hdr += ["Layer", "Edge", "Cloud", "Cost"][len(hdr):]

    def row(i: int) -> str:
        real = _triple(cs, i)
        a, b, d = real if real else ("Layer", "edge", "cloud")
        return (f'<div class="pipeline-row"><span class="layer-name">{escape(a)}</span>'
                f'<span class="edge-cell">{escape(b)}</span><span class="cloud-cell">{escape(d)}</span>'
                f'<span class="cost-cell"></span></div>')
    rows = "".join(row(i) for i in range(n))
    head = "".join(f"<span>{escape(h)}</span>" for h in hdr)
    return (f'<div class="pipeline"><div class="pipeline-header">{head}</div>' + rows + '</div>')


def _verdict_callout(c: Content) -> str:
    cs = _cells(c)
    first = _triple(cs, 0)
    lead = _txt(c, "text", "") or (escape(first[0]) if first else "19 endpoints")
    stat = (escape(f"{first[0]} · {first[1]}".rstrip(" ·")) if first and _txt(c, "text", "")
            else (escape(first[1]) if first else "8 essential"))
    return (f'<div class="verdict-callout"><span>{lead}</span>'
            f'<span class="vc-stat"><span class="vc-dot"></span>{stat}</span></div>')


def _ask_box(c: Content) -> str:
    n = _items(c, 3)
    cs = _cells(c)
    title = _txt(c, "text", "") or "We ask the board to approve:"

    def li(i: int) -> str:
        real = _triple(cs, i)
        if not real:
            return "<li>An ask</li>"
        return f'<li>{escape(f"{real[0]} — {real[1]}".rstrip(" —"))}</li>'
    return (f'<div class="ask-box"><div class="ask-box-title">{title}</div>'
            f'<ul>{"".join(li(i) for i in range(n))}</ul></div>')


def _persona_row(c: Content) -> str:
    n = _items(c, 3)
    cs = _cells(c)

    def pill(i: int) -> str:
        real = _triple(cs, i)
        name, lens = (real[0], real[1] or real[2]) if real else ("Name", "lens")
        return ('<div class="persona-pill"><span class="persona-mark a"></span>'
                f'<span class="persona-name">{escape(name)}</span>'
                f'<span class="persona-lens">{escape(lens)}</span></div>')
    return f'<div class="persona-row">{"".join(pill(i) for i in range(n))}</div>'


# ── ext components (in the contract corpus — reproduce gallery structure exactly) ──

def _provenance(c: Content) -> str:
    n = _items(c, 5)
    cs = _cells(c)

    def ref(i: int) -> str:
        real = _triple(cs, i)
        src, as_of = (real[0], real[1] or real[2]) if real else ("ref", "2026-07")
        return (f'<span class="prov-ref"><a class="prov-src" href="#">{escape(src)}</a>'
                f'<span class="prov-as-of">{escape(as_of)}</span></span>')
    return (f'<div class="provenance"><span class="prov-label">Sources</span>'
            f'{"".join(ref(i) for i in range(n))}</div>')


def _evidence_list(c: Content) -> str:
    # per-index classes mirror components-ext.html evid(): grade = "abcd"[i%4],
    # pip j is 'on' iff j <= i%3 — the skeleton is heterogeneous across rows, so
    # the contract only holds if this exact pattern is reproduced.
    n = _items(c, 10)
    cs = _cells(c)
    rows = []
    for i in range(n):
        g = "abcd"[i % 4]
        pips = "".join(f'<span class="pip{" on" if j <= i % 3 else ""}"></span>' for j in range(3))
        real = _triple(cs, i)
        claim, as_of = (real[0], real[2] or real[1]) if real else ("claim", "2026-07")
        rows.append(
            f'<div class="evidence-row"><div class="evidence-claim">{escape(claim)}</div>'
            f'<span class="evidence-grade {g}">{g.upper()}</span>'
            f'<span class="evidence-conf">{pips}</span>'
            f'<span class="evidence-as-of">{escape(as_of)}</span></div>'
        )
    return f'<div class="evidence-list">{"".join(rows)}</div>'


# risk levels mirror components-ext.html BIAS[i][1] (i = 0..7).
_BIAS_RISK = ["high", "high", "med", "med", "low", "med", "low", "low"]


def _bias_check(c: Content) -> str:
    n = _items(c, 8)
    cs = _cells(c)

    def row(i: int) -> str:
        real = _triple(cs, i)
        name, counter, risk = ((real[0], real[1], real[2] or "risk") if real
                               else ("Bias", "counter-measure", "risk"))
        return (f'<div class="bias-row"><div class="bias-name">{escape(name)}</div>'
                f'<span class="bias-risk {_BIAS_RISK[i % len(_BIAS_RISK)]}">{escape(risk)}</span>'
                f'<div class="bias-counter">{escape(counter)}</div></div>')
    return f'<div class="bias-check">{"".join(row(i) for i in range(n))}</div>'


def _chart(c: Content) -> str:
    # Mirror the gallery chart() geometry (components-ext.html:123) so bars are laid
    # ACROSS the plot, not stacked at the origin. The rendered==gallery contract
    # compares tag+class trees (coords elided), so positioning is free — this only
    # fixes the VISUAL (a near-empty chart box was the worst intra-component void).
    n = max(1, _items(c, 16))
    cs = _cells(c)
    W, H, pad = 560, 240, 28
    bw = (W - pad * 2) / n
    bars = []
    for i in range(n):
        v = _bar_value(_triple(cs, i), i)
        x = pad + i * bw + 3
        y = H - pad - v
        bars.append(f'<rect class="bar" x="{x:.1f}" y="{y}" width="{bw - 6:.1f}" height="{v}"></rect>')
    first = _triple(cs, 0)
    title = _txt(c, "text", "") or (escape(first[0]) if first else "Chart")
    annot = escape(f"{first[0]} {first[1]}".strip()) if first else "annotation"
    return ('<div class="chart"><div class="chart-head">'
            f'<span class="chart-title">{title}</span>'
            '<span class="chart-unit">count</span></div>'
            f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet">'
            f'<line class="axis" x1="{pad}" y1="{H - pad}" x2="{W - pad}" y2="{H - pad}"></line>'
            + "".join(bars) + '</svg>'
            f'<div class="chart-annotation">{annot}</div></div>')


def _flow_embed(c: Content) -> str:
    cs = _cells(c)
    first = _triple(cs, 0)
    title = _txt(c, "text", "") or (escape(first[0]) if first else "Flow")
    nodes = [escape(t[0]) for t in (_triple(cs, i) for i in range(3)) if t] or ["a", "b", "c"]
    nodes += ["a", "b", "c"][len(nodes):]
    # The id slot names the embedded flow. With real content there IS no flow id
    # yet (flow embedding is unbuilt), so it carries the item's own meta rather
    # than the gallery's invented handle.
    flow_id = escape(first[2] or first[1]) if first and (first[2] or first[1]) else "flow_7f3a"
    return ('<div class="flow-embed"><div class="flow-embed-head">'
            f'<span class="flow-embed-icon">⌘</span><span class="flow-embed-title">{title}</span>'
            f'<span class="flow-embed-id">{flow_id}</span>'
            '<button class="flow-embed-max">Maximize</button></div>'
            '<div class="flow-embed-canvas">'
            + "".join(f'<span class="flow-node">{x}</span>' for x in nodes[:3])
            + '</div></div>')


def _statement(c: Content) -> str:
    first = _triple(_cells(c), 0)
    kicker = escape(first[0]) if first else "The wedge"
    accent = escape(first[1]) if first and first[1] else "accent"
    return (f'<div class="statement"><div class="statement-kicker">{kicker}</div>'
            f'<p class="statement-text">{_txt(c, "text", "A thesis")} '
            f'<span class="accent">{accent}</span></p></div>')


def _pull_quote(c: Content) -> str:
    first = _triple(_cells(c), 0)
    quote = _txt(c, "text", "") or (escape(first[0]) if first else "A quote.")
    who = escape(first[0]) if first and _txt(c, "text", "") else "Who"
    src = escape(first[1]) if first and first[1] else "source"
    return (f'<div class="pull-quote"><div class="pull-quote-text">{quote}</div>'
            f'<div class="pull-quote-attr"><span class="who">{who}</span>, {src}</div></div>')


def _matrix(c: Content) -> str:
    # mirror components-ext.html matrix(): cell = j==0 -> cell-yes; else
    # (i+j)%3==0 -> cell-partial; else cell-no. winner-col on col 0.
    n = _items(c, 16)
    cs = _cells(c)
    cols = 4
    # Real content fills the FEATURE column and the first data column (the claim's
    # own detail); the ✓/~/✗ marks stay synthetic because a (label, detail, meta)
    # triple carries no per-column verdict — the item text is what the reader reads.
    # Column labels: the claim's own headers when it bound any. With real content
    # and no headers the remaining columns get neutral ordinals — the gallery's
    # product names (cortex/grep/ripgrep/sourcegraph) are a comparison this
    # document never made, and stamping them on real rows invents a claim.
    hdr = [str(h) for h in (c.get("headers") or [])][:cols]
    filler = (_MATRIX_COLS if not cs
              else [f"{j + 1}" for j in range(cols)])
    hdr += [_pick(filler, j) for j in range(len(hdr), cols)]
    head = ("<thead><tr><th class=\"feature-col\">Feature</th>"
            + "".join(f"<th{' class=\"winner-col\"' if j == 0 else ''}>{escape(hdr[j])}</th>"
                      for j in range(cols))
            + "</tr></thead>")
    body_rows = []
    for i in range(n):
        real = _triple(cs, i)
        cells = ""
        for j in range(cols):
            if j == 0:
                mark = (f'<span class="cell-yes">{escape(real[1])}</span>' if real and real[1]
                        else '<span class="cell-yes">✓</span>')
            elif (i + j) % 3 == 0:
                mark = '<span class="cell-partial">~</span>'
            else:
                mark = '<span class="cell-no">✗</span>'
            cells += f"<td{' class=\"winner-col\"' if j == 0 else ''}>{mark}</td>"
        feature = escape(real[0]) if real else escape(_pick(_MATRIX_FEATURES, i))
        body_rows.append(f"<tr><td class=\"feature-cell\">{feature}</td>{cells}</tr>")
    body = "<tbody>" + "".join(body_rows) + "</tbody>"
    return f'<div class="matrix-wrap"><table class="matrix">{head}{body}</table></div>'


def _dense_table(c: Content) -> str:
    n = _items(c, 30)
    cs = _cells(c)
    hdr = [str(h) for h in (c.get("headers") or [])][:4]
    hdr += ["Endpoint", "Method", "Owner", "p95 ms"][len(hdr):]
    head = ("<thead><tr>"
            + "".join(f"<th{' class=\"num\"' if j == 3 else ''}>{escape(hdr[j])}</th>"
                      for j in range(4))
            + "</tr></thead>")

    def row(i: int) -> str:
        real = _triple(cs, i)
        a, b, d = real if real else ("/api", "GET", "platform")
        return (f'<tr><td><span class="cell-strong">{escape(a)}</span></td>'
                f'<td>{escape(b)}</td><td>{escape(d)}</td><td class="num">40</td></tr>')
    rows = "".join(row(i) for i in range(n))
    return f'<div class="dense-table-wrap"><table class="dense-table">{head}<tbody>{rows}</tbody></table></div>'


def _two_column_list(c: Content) -> str:
    n = _items(c, 24)
    cs = _cells(c)

    def item(i: int) -> str:
        real = _triple(cs, i)
        title, sub = (real[0], real[1]) if real else ("Principle", "rationale")
        return ('<div class="tcl-item"><span class="tcl-mark"></span>'
                f'<div><div class="tcl-title">{escape(title)}</div>'
                f'<div class="tcl-sub">{escape(sub)}</div></div></div>')
    return f'<div class="two-col-list">{"".join(item(i) for i in range(n))}</div>'


def _tag_wall(c: Content) -> str:
    # mirror components-ext.html tags(): class 'tw-tag' + accent (i%6==0) / ok (i%9==0).
    n = _items(c, 40)
    cs = _cells(c)
    parts = []
    for i in range(n):
        extra = " accent" if i % 6 == 0 else (" ok" if i % 9 == 0 else "")
        real = _triple(cs, i)
        label = escape(real[0]) if real else "tag"
        parts.append(f'<span class="tw-tag{extra}">{label}</span>')
    return f'<div class="tag-wall">{"".join(parts)}</div>'


_BUILDERS = {
    "statement-title": _statement_title, "lede": _lede, "prose": _prose,
    "callout": _callout, "card-set": _card_set, "metric-tiles": _metric_tiles,
    "stat-row": _stat_row, "beat-list": _beat_list, "disclosure-list": _beat_list,
    "option-grid": _option_grid, "proportion-bars": _proportion_bars,
    "flow-steps": _flow_steps, "phase-row": _phase_row, "arch-diagram": _arch_diagram,
    "pipeline-table": _pipeline_table, "cmp-table": _pipeline_table,
    "verdict-callout": _verdict_callout, "ask-box": _ask_box, "persona-row": _persona_row,
    "transform-grid": _card_set, "story-grid": _card_set, "verb-grid": _card_set,
    "lens-reframe": _persona_row,
    # ext (contract corpus)
    "provenance": _provenance, "evidence-list": _evidence_list, "bias-check": _bias_check,
    "chart": _chart, "flow-embed": _flow_embed, "statement": _statement,
    "pull-quote": _pull_quote, "matrix": _matrix, "dense-table": _dense_table,
    "two-column-list": _two_column_list, "tag-wall": _tag_wall,
    # badges
    "tier-badge": lambda c: '<span class="tier-badge tier-edge">edge</span>',
    "verdict-badge": lambda c: '<span class="verdict-badge essential">Essential</span>',
    "method-badge": lambda c: '<span class="method-badge get">GET</span>',
}

# Gallery max-fill item counts for the contract corpus (mirror components-ext.html).
EXT_MAX_FILL = {
    "provenance": 5, "evidence-list": 10, "bias-check": 8, "chart": 16,
    "flow-embed": 1, "statement": 1, "pull-quote": 1, "matrix": 16,
    "dense-table": 30, "two-column-list": 24, "tag-wall": 40,
}


def build(component: str, content: Content | None = None) -> str:
    """Render a component fragment. Unknown component -> a labelled placeholder box
    (keeps the composer total-render robust; flagged by the contract as no-ref)."""
    fn = _BUILDERS.get(component)
    if fn is None:
        return f'<div class="component-unknown" data-component="{escape(component)}"></div>'
    return fn(content or {})


def gallery_max_html() -> dict[str, str]:
    """{component: fragment} at gallery max fill — the pipeline side of the
    rendered==gallery contract (keyed 'component:<id>' by the caller)."""
    return {c: build(c, {"items": n}) for c, n in EXT_MAX_FILL.items()}


__all__ = ["build", "gallery_max_html", "EXT_MAX_FILL", "Content"]
