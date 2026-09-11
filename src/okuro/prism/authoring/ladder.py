# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W3 engine 1 — RESOLUTION-LADDER AUTHORING. Resolve a topic's
#   depth DOWNWARD from the L4 master doc: L4 (full text) -> L3 (densest composed,
#   every claim, full cardinality via dense components) -> L2 (higher-tier subset
#   at reference density) -> L1 (archetype hook: ONE idea + strong visual + 3-5 key
#   points). Superset in MEANING (claim sets nest, detail only rises going deeper);
#   NEVER re-prose flat claims upward (pre-check REC-2). Emits typed SlidePlans the
#   W2 solver lays out, plus the coverage maps the ladder gate + accuracy read.
# index:
#   RenderedUnit / L4Doc / LadderDoc / author_ladder
# AGENT_HEADER_END -->
"""Resolve a topic into an L1-L4 resolution ladder (v4 §1, answers 1-3).

The build order is DEEP -> SHALLOW so the superset property is true by
construction: L3 carries every claim at full cardinality; L2 keeps the higher-tier
claims at reference density; L1 keeps only the headline (ONE idea) plus one strong
visual (the 3-5 key points). Each claim's rendered item-count is monotone non-
decreasing with depth, so a shallow level can honestly render a SUBSET of a high-
cardinality claim (dense-table of 13 at L3, card-set of 5 at L2) and the accuracy
accounting reports "5 of 13 — all 13 in L4". Component choice is pure manifest
translation (engine 2); geometry is the solver's.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from okuro.prism.authoring.topic import ClaimGrounding, TopicSource
from okuro.prism.authoring.translate import choose_component, render_family, render_items_for
from okuro.prism.solver.density import slide_density
from okuro.prism.solver.families import get_family
from okuro.prism.solver.heights import get_height_model
from okuro.prism.solver.schema import Claim, InformationUnit, SlidePlan
from okuro.prism.solver.selection import select_family

# claims of tier <= this stay at L2 (the "higher resolution of L1", not the full
# reservoir). L3 always carries every claim. Long-form narrative/quote asides at
# L3 render as prose/pull-quote so the level reads as a densest COMPOSITION, not a
# text wall.
_L2_MAX_TIER = 1
# L2 reference-density cap: a claim renders at most this many items at L2 (R31: L2
# ≈ reference density, a handful — NOT the full reservoir). A high-cardinality claim
# therefore shows an HONEST SUBSET at L2 (card-set of 5 of 13) and its FULL count at
# L3 (dense-table of 13) — this is what makes L3 strictly denser than L2 and gives
# the accuracy accounting its "N of M" deficit to report.
_L2_REF_ITEMS = 6
# L1 key-point budget: the single strong visual renders at most this many items
# (the "3-5 key points"); deeper levels lift the count. Keeps L1 sparse (R31).
_L1_VISUAL_ITEMS = 5
# Per-level HEIGHT budgets (px) — a level is ONE READABLE composed slide, never an
# infinite stack (the solver's 1800px readability cap). A composed slide caps at
# ~1800px whatever the level; L3 gains RESOLUTION from denser components + higher
# item counts in the SAME height, not from being taller — so L2/L3 share a budget
# and L3 out-loads L2 by density, not size. Body claims are admitted greedily (most-
# informative first, high-cardinality first); claims that don't fit live in L4 only.
_BUDGET_L2 = 1600.0
_BUDGET_L3 = 1560.0
_GUTTER_PX = 28.0
# The W1 height model is calibrated on the gallery specimens; card-grid components
# (card-set/verb-grid/story-grid/…) render TALLER in the composed stack (multi-row
# wrap) than the model predicts. The solver also places width-hungry grids full-
# width (narrow = even taller), and it STACKS rather than pack them (a narrow grid
# out-heights a stacked pair). So the projection is a conservative STACK of full-
# width heights with a card-grid inflation — it must over-, never under-, estimate.
# Only components that actually render as the tall multi-row .card-grid (renderers
# _card_set / _metric_tiles / _option_grid). List/pill components (disclosure-list
# -> beat-list, persona-row, lens-reframe) are compact and must NOT be inflated.
_CARDGRID_COMPONENTS = frozenset({
    "card-set", "verb-grid", "story-grid", "transform-grid", "metric-tiles",
    "option-grid",
})
_CARDGRID_INFLATE = 1.5
_OTHER_INFLATE = 1.2
# L1 avoids the card-grid render family for its key-points strip so the hook does
# NOT lead with the same full card band L2/L3 use (R32 distinct macro-skeleton).
_CARDGRID_FAMILY_AVOID = frozenset({"card-grid"})
# The W1 height model OVER-predicts disclosure-list (its gallery specimen is a tall
# accordion; the solver renderer aliases it to the compact beat-list ~34px/row). Use
# a direct compact estimate so a distinct list component is not wrongly budget-
# rejected (which forced two card-grids onto L2 — the judge's alignment complaint).
_COMPACT_LIST_PX: dict[str, float] = {"disclosure-list": 44.0, "beat-list": 44.0}
_COMPACT_LIST_PER_ITEM = 34.0
# The title always renders as the title band regardless of the headline claim's
# shape (a verdict-shaped headline is still a TITLE, not a verdict-callout).
_TITLE_COMPONENT = "statement-title"
# Character budgets for the two CHROME text bands, taken from their declared kit
# contracts: statement-title is "the ONE claim of a slide, display type, <=22ch"
# (a per-line width at 56px; the numeric gate flags title text over 34 chars) and
# lede is "one-sentence supporting frame under a title, <=70ch". A mined claim is a
# STATEMENT and can be a whole sentence, so both bands must check the text they are
# handed — otherwise an over-long claim just moves from one band to the other.
# Over-budget claims are not dropped: they fall through to the slide BODY.
_TITLE_MAX_CHARS = 34
_LEDE_MAX_CHARS = 70
_ASIDE_SHAPES = ("narrative", "quote")


@dataclass(frozen=True)
class RenderedUnit:
    """One authored unit + its accuracy metadata. ``rendered_items`` (N) is what
    this level shows; ``total_items`` (M) is what the claim carries. N < M => a
    deficit the accuracy accounting surfaces ('N of M — all M in L4')."""

    claim_ids: tuple[str, ...]
    component: str
    emphasis: str
    rendered_items: int
    total_items: int
    dominant_claim: str


@dataclass(frozen=True)
class L4Doc:
    """The full-resolution reading level: the topic's verbatim artifact text plus
    the ordered claim ids grounded in it (the M in 'all M in L4')."""

    topic_id: str
    title: str
    body: str
    claim_ids: tuple[str, ...]


@dataclass
class LadderDoc:
    """A topic authored across the resolution ladder. ``plans`` feed the solver;
    ``units`` + ``loads`` + ``densities`` + ``l4`` feed the gate and accuracy.

    ``loads`` = INFORMATION carried per level (rendered items + unit count) — the
    resolution axis the ladder gate proves strictly rises (v4 §1: L2 is L1 "at
    higher resolution", L3 is denser still). ``densities`` = the W2 cross-model INK
    scalar per level, REPORTED as the R31 density bar; ink-ceiling enforcement is
    the solver's density_band term (it has real spans — ink is not a resolution
    proxy: a verb-grid of 6 out-inks a tag-wall of 13, so ink monotonicity would
    contradict the information ladder). See the W3 decision memory.
    """

    topic_id: str
    family: str
    claims: dict[str, Claim]
    grounding: dict[str, ClaimGrounding]
    plans: dict[str, SlidePlan]                       # "L1"|"L2"|"L3"
    units: dict[str, list[RenderedUnit]]              # per level, in reading order
    loads: dict[str, float]                           # information load (resolution axis)
    densities: dict[str, float]                       # W2 ink scalar (R31 bar, reported)
    l4: L4Doc
    synthesized_title: bool = False
    # W5 Phase D: a topic whose claim material cannot sustain three DISTINCT strictly-
    # rising levels emits a SHORTER ladder (honest level-collapse) — only the levels
    # in ``units`` exist; the rest live in the L4 doc. True when < 3 levels are kept.
    degraded: bool = False

    def plan(self, level: str) -> SlidePlan:
        return self.plans[level]


def _title_claim(topic: TopicSource) -> tuple[Claim, bool, ClaimGrounding]:
    """The headline title claim (statement-title, tier-0 narrative). Reuse a mined
    tier-0 headline when it actually READS as a headline; else synthesise from the
    topic title (flagged).

    A mined headline is a claim STATEMENT, and a statement can be a whole sentence:
    measured on a real deck, ten of them ran 129-240 chars, which the title band
    rendered as a paragraph in 56px display type. The kit manifest caps
    statement-title at <=22ch and the numeric gate flags >``_TITLE_MAX_CHARS``, so
    an over-long statement is rejected in favour of the topic title — which is
    already the designed fallback and measured 25-41 chars on the same deck.

    Nothing is lost: the rejected claim is no longer excluded from ``body_ids``, so
    it flows into the slide BODY, where a full sentence belongs.
    """
    hid = topic.tier0_headline()
    if hid is not None:
        head = topic.claims[hid]
        # Keep the mined headline when it fits the band — or when the topic title
        # is no shorter, since swapping one over-long title for a longer one only
        # loses the mined specificity.
        if len(head.text) <= _TITLE_MAX_CHARS or len(head.text) <= len(topic.title):
            return head, False, topic.ground(hid)
    tc = Claim("__title__", topic.title, "narrative", tier=0, items=1,
               chars=len(topic.title))
    return tc, True, ClaimGrounding(synthesized=True)


def _lede_claim(topic: TopicSource, exclude: set[str]) -> tuple[Claim, ClaimGrounding] | None:
    """A one-line narrative for the title band (short-title fill, W2 handover).
    Prefer a mined narrative claim that FITS the lede band; else None (no synthetic
    padding). The length guard matters because a headline rejected by the title
    budget is still a tier-0 narrative — without it the same over-long sentence
    would simply move from the title band into the lede band."""
    cands = [
        cid for cid, c in topic.claims.items()
        if c.shape == "narrative" and cid not in exclude
        and len(c.text) <= _LEDE_MAX_CHARS
    ]
    if not cands:
        return None
    cid = sorted(cands, key=lambda c: (topic.claims[c].tier, c))[0]
    return topic.claims[cid], topic.ground(cid)


def _emphasis_for(rank: int, shape: str) -> str:
    """Non-title emphasis: the first (strongest) non-title claim is the primary
    visual; long-form narrative/quote are asides; the rest support."""
    if shape in _ASIDE_SHAPES:
        return "aside"
    return "primary" if rank == 0 else "supporting"


def _mk_unit(
    claim: Claim, emphasis: str, wanted_items: int,
    avoid: frozenset[str] = frozenset(),
) -> RenderedUnit:
    comp = choose_component(claim.shape, wanted_items, emphasis, avoid=avoid)
    rendered = render_items_for(comp, wanted_items)
    return RenderedUnit(
        claim_ids=(claim.id,), component=comp, emphasis=emphasis,
        rendered_items=rendered, total_items=claim.items, dominant_claim=claim.id,
    )


def _title_unit(title: Claim) -> RenderedUnit:
    """The focal title band — always statement-title (title band, not the claim's
    shape component). statement-title fits narrative/verdict >= 0.5 so the schema
    fit-set holds for either headline shape."""
    return RenderedUnit(
        claim_ids=(title.id,), component=_TITLE_COMPONENT, emphasis="focal",
        rendered_items=1, total_items=title.items, dominant_claim=title.id,
    )


def _to_plan(level: str, family: str, units: list[RenderedUnit]) -> SlidePlan:
    ius = [
        InformationUnit(list(u.claim_ids), u.component, u.emphasis, items=u.rendered_items)
        for u in units
    ]
    return SlidePlan(level, family, ius, list(range(len(ius))))


def _density(units: list[RenderedUnit], claims: dict[str, Claim]) -> float:
    # span estimate for density is level-agnostic (the solver assigns real spans);
    # use a neutral 6-col span so the ladder scalar orders by content, not layout.
    return slide_density([(u.component, u.rendered_items, 6) for u in units])


def _info_load(units: list[RenderedUnit]) -> float:
    """Information carried by a level: total rendered items + the unit count. This
    is the resolution axis (how much of the topic is shown), monotone by
    construction across the ladder — unlike ink, which varies by component airiness."""
    return float(sum(u.rendered_items for u in units) + len(units))


def _project_height(
    chrome: list[RenderedUnit], body: list[RenderedUnit], claims: dict[str, Claim],
) -> float:
    """Render-free height projection for a candidate level, using the W1 height model
    (the same estimator the solver plans on). ``chrome`` (title + lede) and the first
    body unit (the strong/primary visual) sit full-width; remaining supporting units
    are packed ~2 per row at half width. Conservative on the tall side so the real,
    denser solver packing lands under the readability cap."""
    hm = get_height_model()

    def h(u: RenderedUnit) -> float:
        if u.component in _COMPACT_LIST_PX:          # model is wrong for these
            return _COMPACT_LIST_PX[u.component] + _COMPACT_LIST_PER_ITEM * u.rendered_items
        base = hm.estimate_px(u.component, u.rendered_items,
                              claims[u.dominant_claim].chars, col_span=12)
        inflate = (_CARDGRID_INFLATE if u.component in _CARDGRID_COMPONENTS
                   else _OTHER_INFLATE)
        return base * inflate

    # STACK of full-width heights (the solver stacks width-hungry grids) + gutters.
    total = sum(h(u) for u in chrome) + sum(h(u) for u in body)
    total += _GUTTER_PX * (len(chrome) + len(body))
    return total


# ── R32 signatures + honest level-collapse (shared with gate.py — single source) ──

LEVELS = ("L1", "L2", "L3")


def body_signature(units: list[RenderedUnit]) -> tuple[str, ...]:
    """The body macro-skeleton = the render-family sequence of the non-focal,
    non-lede units (the R32 distinct-composition signature). Shared by the ladder
    collapse and the gate so the two can never drift."""
    return tuple(render_family(u.component) for u in units
                 if u.emphasis != "focal" and u.component != "lede")


def shared_same_family(shallow: list[RenderedUnit], deep: list[RenderedUnit]) -> bool:
    """True when EVERY non-focal claim shared by two adjacent levels keeps the SAME
    render family across them (a repeated band, not a re-composition) — R32 (b). An
    empty shared set returns False (nothing is repeated)."""
    focal = {u.dominant_claim for u in shallow if u.emphasis == "focal"}
    fam_s = {u.dominant_claim: render_family(u.component) for u in shallow}
    fam_d = {u.dominant_claim: render_family(u.component) for u in deep}
    shared = [c for c in fam_s if c in fam_d and c not in focal]
    return bool(shared) and all(fam_s[c] == fam_d[c] for c in shared)


def levels_distinct(shallow: list[RenderedUnit], deep: list[RenderedUnit]) -> bool:
    """Two adjacent levels are a DISTINCT composition (pass R32) iff their body
    signatures differ AND no shared claim merely repeats its band."""
    return (body_signature(shallow) != body_signature(deep)
            and not shared_same_family(shallow, deep))


def select_kept_levels(units: dict[str, list[RenderedUnit]],
                       loads: dict[str, float]) -> list[str]:
    """Honest level-collapse (W5 Phase D). Keep L1 (the hook + one-focal entry), then
    admit a deeper level ONLY if it strictly RISES in information load over the last
    kept level AND is a DISTINCT composition from it. A topic whose claim material
    can't sustain that emits a SHORTER ladder (hook-only, or hook+dense) — the L4 doc
    carries the full detail. The ladder gate then passes BY CONSTRUCTION on the kept
    levels, never by padding fake resolution. A topic that already rises + re-composes
    keeps all three (no-op), so nothing regresses for well-formed topics."""
    kept = ["L1"]
    for lvl in ("L2", "L3"):
        if lvl not in units:
            continue
        prev = kept[-1]
        if (loads.get(lvl, 0.0) > loads.get(prev, 0.0)
                and levels_distinct(units[prev], units[lvl])):
            kept.append(lvl)
    return kept


def author_ladder(topic: TopicSource, family: str | None = None) -> LadderDoc:
    """Author one topic into an L1-L4 ladder. Deterministic given the topic."""
    fam = family or select_family(topic.content_character, topic.audience_mood)
    get_family(fam)                                    # validate family name early

    title, synth_title, title_g = _title_claim(topic)
    claims: dict[str, Claim] = dict(topic.claims)
    grounding: dict[str, ClaimGrounding] = dict(topic.grounding)
    claims[title.id] = title
    grounding.setdefault(title.id, title_g)

    lede = _lede_claim(topic, exclude={title.id})
    lede_id = lede[0].id if lede else None
    title_unit = _title_unit(title)
    lede_unit = _mk_unit(lede[0], "supporting", 1) if lede else None
    chrome = [title_unit] + ([lede_unit] if lede_unit else [])

    # body candidates: most-informative first — tier (importance); then claims with
    # COMPLETE real per-item content before thin ones (a claim the source gives no
    # per-item detail for renders as sparse cells with dead void — it belongs in the
    # L4 reading text, not a composed slide; grounding forbids inventing the missing
    # detail); then item count desc (high-cardinality first -> honest "N of M" at L2);
    # then id.
    def _thin(cid: str) -> int:
        c = claims[cid]
        if c.items <= 1:
            return 0                                    # single-value claims are fine
        full = sum(1 for t in c.content_items() if t and t[0] and t[1])
        return 0 if full >= c.items else 1              # 1 = thin -> deprioritise
    body_ids = [cid for cid in topic.claims
                if cid != title.id and cid != lede_id]
    body_ids.sort(key=lambda c: (claims[c].tier, _thin(c), -claims[c].items, c))

    def _emphasis(rank: int, shape: str) -> str:
        return _emphasis_for(rank, shape)

    # ── L3: densest composed SLIDE — greedy admit under the height budget, full
    #    cardinality + dense components. Claims that don't fit live in L4 only. ─────
    l3_units: list[RenderedUnit] = [title_unit] + ([lede_unit] if lede_unit else [])
    l3_body: list[RenderedUnit] = []
    used3: set[str] = set()
    for cid in body_ids:
        c = claims[cid]
        # a multi-item claim the source gives no per-item detail for renders as sparse
        # cells with dead void — it belongs in the L4 reading text, not a composed
        # slide (grounding forbids inventing the detail). Skip from the composed body.
        if _thin(cid):
            continue
        emph = _emphasis(len(l3_body), c.shape)
        u = _mk_unit(c, emph, c.items, avoid=frozenset(used3))
        if not l3_body or _project_height(chrome, l3_body + [u], claims) <= _BUDGET_L3:
            l3_body.append(u)
            used3.add(u.component)
            used3.add(render_family(u.component))
    l3_units += l3_body
    l3_body_ids = [u.dominant_claim for u in l3_body]

    # ── L2: a reference-density SUBSET of L3 (guarantees L2 ⊆ L3). Higher-tier
    #    claims only, re-capped to the L2 reference budget (the "N of M" subset). ───
    l2_units: list[RenderedUnit] = [title_unit] + ([lede_unit] if lede_unit else [])
    l2_body: list[RenderedUnit] = []
    used2: set[str] = set()
    for cid in l3_body_ids:
        c = claims[cid]
        if c.tier > _L2_MAX_TIER:
            continue
        emph = _emphasis(len(l2_body), c.shape)
        wanted = min(c.items, _L2_REF_ITEMS)
        comp = choose_component(c.shape, wanted, emph, avoid=frozenset(used2))
        u = RenderedUnit(
            claim_ids=(c.id,), component=comp, emphasis=emph,
            rendered_items=render_items_for(comp, wanted), total_items=c.items,
            dominant_claim=c.id,
        )
        if not l2_body or _project_height(chrome, l2_body + [u], claims) <= _BUDGET_L2:
            l2_body.append(u)
            used2.add(u.component)
            used2.add(render_family(u.component))
    l2_units += l2_body

    # ── L1: hook — ONE idea (hero title) + the 3-5 key points as a COMPACT STRIP
    #    (R32 re-composition + restraint: NOT the same full card band L2 leads with;
    #    a terse labels strip, a distinct macro-skeleton from L2/L3). L1 ⊆ L2 holds
    #    (same claim, same-or-fewer items, lighter component). ──────────────────────
    l1_units: list[RenderedUnit] = [title_unit]
    strong = next((u for u in l2_body if claims[u.dominant_claim].shape not in _ASIDE_SHAPES),
                  None)
    if strong is not None:
        c = claims[strong.dominant_claim]
        wanted = min(c.items, _L1_VISUAL_ITEMS)
        # avoid the card-grid family L2 uses for the same claim -> a compact list/
        # strip (labels, not full body cards): re-composed, terser, restraint up.
        comp = choose_component(c.shape, wanted, "primary", avoid=_CARDGRID_FAMILY_AVOID)
        l1_units.append(RenderedUnit(
            claim_ids=(c.id,), component=comp, emphasis="primary",
            rendered_items=render_items_for(comp, wanted), total_items=c.items,
            dominant_claim=c.id,
        ))

    units = {"L1": l1_units, "L2": l2_units, "L3": l3_units}
    loads = {lvl: _info_load(us) for lvl, us in units.items()}

    # Honest level-collapse: keep only the DISTINCT strictly-rising levels; a topic
    # that can't sustain three renders a shorter ladder (the L4 doc carries the rest).
    kept = select_kept_levels(units, loads)
    degraded = len(kept) < len(units)
    units = {lvl: units[lvl] for lvl in kept}
    loads = {lvl: loads[lvl] for lvl in kept}
    plans = {lvl: _to_plan(lvl, fam, us) for lvl, us in units.items()}
    densities = {lvl: _density(us, claims) for lvl, us in units.items()}

    mined = topic.mined_claim_ids()
    l4 = L4Doc(topic.topic_id, topic.title, topic.master_doc, tuple(mined))

    return LadderDoc(
        topic_id=topic.topic_id, family=fam, claims=claims, grounding=grounding,
        plans=plans, units=units, loads=loads, densities=densities, l4=l4,
        synthesized_title=synth_title, degraded=degraded,
    )


__all__ = ["RenderedUnit", "L4Doc", "LadderDoc", "author_ladder"]
