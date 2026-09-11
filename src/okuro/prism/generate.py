# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Generate a recipient-tailored, brand-styled facet tree from a topic;
#   NL edit pass over an existing PrismDoc; person->depth-default resolver.
# index:
#   imports
#   def _brand_design
#   def _recipient_identity
#   def resolve_depth_default
#   def _doc_system / _doc_user
#   def _extract_json_span / _lenient_json_loads / _parse_doc
#   def _flatten_node / _salvage_tree / _normalize
#   def generate_doc (4-attempt: parse-retry + mis-nest salvage + collapse-retry)
#   def run_ops_instruction / def edit_doc
# AGENT_HEADER_END -->
"""okuro·prism generation — topic → brand-styled, depth-ladder facet tree.

Given a topic (+ optional recipient person + brand), assemble:
  - brand design tokens  (resolve_brand → design_profile: colours, type)
  - recipient lens        (person_lens → cognitive profile, PII-firewalled)
  - grounded facts        (gather_context over okuro's brain)
…then ask the LLM to emit a NESTED outline (title/kind/rungs/children per
node) — safer for the model than authoring flat parent_id/children pointers
directly. ``_normalize`` flattens the outline into the strict PrismDoc facet
dict (stable ids, parent_id + children arrays, all 4 rungs present per facet)
and the doc is saved via ``okuro.prism.storage``. Two LLM calls: pass 1 authors
the tree+prose, pass 2 (``_enrich_blocks``) emits the rich visual modules pass 1
reliably refuses to (a focused call whose only product is block JSON).

Mirrors ``okuro.slides.generate`` deliberately — same brand/lens/grounding
assembly, same LLM-invoke wrapper, same NL-edit-via-ops core.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger("okuro.prism.generate")

_DEFAULT_BRAND = "okuro"
from okuro.prism.rungs import RUNGS as _RUNGS

# Model-tier selector (web/MCP `tier`) → bridge capability. resolve_provider
# infers the model from the capability name, so this stays provider-agnostic:
# on claude it lands haiku/sonnet/opus, on gemini/local its own fast/std/quality
# tiers. Unknown/absent tier → "standard" (the prior hardcoded behavior).
_TIER_CAPABILITY = {"fast": "fast", "mid": "standard", "top": "quality"}


def _tier_capability(tier: Optional[str]) -> str:
    return _TIER_CAPABILITY.get((tier or "").strip().lower(), "standard")


# Layout-diversity knob (web slider, 1-3). An authoring directive appended to
# BOTH generation passes — pass 1 governs tree/prose shape, pass 2 governs which
# visual modules get emitted. 2 = balanced = the prior default (empty clause, no
# behavior change); 1 clamps to a safe module set, 3 opens the full palette.
_DIVERSITY_CLAUSES = {
    1: (
        "LAYOUT DISCIPLINE — CONSERVATIVE. Stay within a small, safe module set: "
        "prose body, stat, table, heading, callout. At most ONE primary module "
        "per facet, uniform vertical rhythm, no full-bleed/standalone modules. "
        "Predictable and readable beats striking — do not reach for exotic "
        "modules even where they'd fit."
    ),
    2: "",  # balanced — current default behavior
    3: (
        "LAYOUT DIVERSITY — EXPRESSIVE. Maximize structural + visual variety: no "
        "two consecutive facets should share the same shape. Reach across the "
        "FULL module palette — statement, quote, graph, timeline, options, "
        "compare, lens, meter, steps, spec, simulator, matrix, cards, frequency, "
        "hops — wherever the content genuinely supports it (NEVER force a module "
        "onto content that doesn't). Use standalone/full-bleed modules for "
        "chapter-defining claims, and vary the column layout facet to facet. "
        "Same hard rule as always: never fabricate data to fill a module."
    ),
}


def _diversity_clause(diversity: Optional[int]) -> str:
    try:
        return _DIVERSITY_CLAUSES.get(int(diversity), "")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""


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


def _brand_voice(brand_id: str) -> dict[str, Any]:
    """The brand's VOICE as a `language` dict — from the voice slot, then v0.

    "language belongs to the brand not the design" (the owner, 2026-09-03). This
    read `design["language"]`, which only a v0 design profile carries, so the
    moment a brand's design slot named an engine KIT the voice went silent —
    no error, just a deck written in nobody's voice.

    ORDER IS VOICE-SLOT-FIRST with the design block as a migration fallback,
    the same shape `stack.registry._resolve_design_profile` uses for the other
    half of the same split. When the last brand carries a voice slot the
    fallback is dead code and goes with the v0 package.
    """
    try:
        from okuro.stack.registry import resolve_brand
        from okuro.stack.voice_view import language_view

        brand = resolve_brand(brand_id) or {}
        slot = brand.get("slots", {}).get("voice")
        if isinstance(slot, list):
            slot = slot[0] if slot else None
        if isinstance(slot, dict) and not slot.get("missing"):
            language = language_view(slot.get("data") or {})
            if language:
                return language
    except Exception as exc:
        logger.info("brand voice resolve failed: %s", exc)
    return {}


def _brand_health(brand_id: str) -> list[str]:
    """Lint the brand DEFINITION before a deck builds on it — the errors that make
    ``_brand_design`` silently fall back to ``{}`` (missing design slot, dangling
    profile ref, absent design YAML). Returns human-readable problems (empty =
    healthy) so an off-brand deck is never silent. Never raises — a failed lint
    must not block a build, only explain why the deck may be off-brand."""
    if not brand_id:
        return []
    try:
        from okuro.stack.validator import lint_brand

        res = lint_brand(brand_id) or {}
        return [str(e) for e in (res.get("errors") or [])]
    except Exception as exc:  # noqa: BLE001 — the gate never blocks a build
        logger.info("brand health check skipped for %s: %s", brand_id, exc)
        return []


def _recipient_identity(person_id: str) -> str:
    """Public identity for the doc (name · role · org). Deliberately scoped to
    this user-owned, user-intended prism use — separate from person_lens's PII
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


def resolve_depth_default(person_id: Optional[str]) -> str:
    """Resolve the entry rung for ``person_id`` from the density+jargon sliders
    (joint 5-row heuristic locked in the 3.1 contract §4 — reads BOTH axes,
    not density alone). Interim stand-in for the still-open person_lens
    depth-appetite axis (2.2/2.3): when that axis lands, only this function's
    body changes, not the ``prism_retailor``/``prism_generate`` contract.

    Sliders are 1-5 ints (``cognitive_profile_for_llm``): density 1=sparse..
    5=dense, jargon 1=layman..5=expert. Bucketed 1-2=low, 3=mid, 4-5=high.
    No person_id / no profile → "L2" (safe default)."""
    if not person_id:
        return "L2"
    try:
        from okuro.peer.cognitive_profile import cognitive_profile_for_llm

        profile = cognitive_profile_for_llm(person_id) or {}
    except Exception as exc:
        logger.info("cognitive_profile_for_llm failed for %s: %s", person_id, exc)
        profile = {}

    sliders = profile.get("sliders") or {}
    density, jargon = sliders.get("density"), sliders.get("jargon")
    if density is None or jargon is None:
        return "L2"

    density_bucket = "concise" if density <= 2 else ("detailed" if density >= 4 else "balanced")
    jargon_bucket = "plain" if jargon <= 2 else ("technical" if jargon >= 4 else "balanced")

    if density_bucket == "concise":
        return "L1"
    if density_bucket == "balanced":
        return "L3" if jargon_bucket == "technical" else "L2"
    return "L4" if jargon_bucket == "technical" else "L3"


_SCHEMA_PREFIX = (
    'Outline JSON shape (emit EXACTLY this, no prose, no code fences):\n'
    '{"title": str, "root": Node}\n'
    'Node = {"title": str, "headline"?: str, '
    '"kind": "topic"|"diagram"|"decision"|"metric", '
    '"rungs": {"L1": Rung, "L2": Rung, "L3": Rung, "L4": Rung}, '
    '"children"?: [Node ...]}\n'
    'ASSERTION HEADLINE (Pyramid Principle "action title"): "title" is a SHORT '
    'nav label (2-4 words); "headline" is the section\'s conclusion as a COMPLETE '
    'DECLARATIVE SENTENCE WITH A VERB — the so-what, stated as a claim ("ARR '
    'cleared the CHF 4M target, up 35%" — NOT a topic label like "Revenue" and '
    'NOT a noun phrase like "Strong revenue growth"). THE TEST: a director who '
    'reads ONLY the headlines, top to bottom, in order, must get the COMPLETE '
    'argument — so each headline advances the case and, read as a sequence, they '
    'form the storyline. Every SECTION gets one; never a heading, always a '
    'sentence that could stand alone as the point.\n'
    'Rung = {"body": str, "media"?: [{"kind":"mermaid","spec":str}], '
    '"callouts"?: [{"type":"risk"|"decision"|"wow","text":str}], '
    '"blocks"?: [RichBlock ...]}\n'
    'body = the prose (markdown). RichBlock adds a VISUAL module ALONGSIDE the '
    'prose — use it instead of cramming structured data into prose:\n'
)


# The canonical RichBlock catalog — the single source of truth for every visual
# module's shape, shared by the outline generator (_SCHEMA) and the pipeline
# Component Arranger (arranger._ARRANGE_SYSTEM). Widen the palette HERE and both
# consumers gain the type. Keep every documented type in _RICH_BLOCK_TYPES and
# rendered by the viewer's block switch (components/prism/blocks.tsx).
_RICHBLOCK_CATALOG = (
    '  {"type":"stat", "items":[{"value":str,"label":str,"delta"?:str,'
    '"trend"?:"up"|"down"|"flat","state"?:"good"|"warn"|"bad","sub"?:str} ...]} '
    'board-grade metric tiles — a bare number forces guessing, so add context '
    'where you have it: delta="+12%" with trend for direction, sub="vs 8.5 '
    'target" for a baseline, state to color it good/warn/bad (trend is the '
    'arrow, state is the meaning — "up" is not always good)\n'
    '  {"type":"table", "headers":[str ...], "rows":[[str ...] ...], '
    '"rowStates"?:["good"|"warn"|"bad"|"highlight" ...]} comparison/data; wrap a '
    'cell in `backticks` for inline code, rowStates[i] tints row i\n'
    '  {"type":"heading", "eyebrow"?:str, "title":str, "lede"?:str} sub-section header\n'
    '  {"type":"chart", "chart":"bar"|"line"|"area"|"pie", '
    '"series":[{"label":str,"value":number} ...], "unit"?:str, '
    '"annotations"?:[{"at":series-index,"text":str} ...]}     '
    'quantitative: bar=magnitude across categories, line/area=trend over an '
    'ordered sequence, pie=composition/share of a whole. annotations call out a '
    'specific datapoint (the peak, the inflection, the outlier) — use sparingly '
    'for bar/line/area, keep text under ~30 chars\n'
    '  {"type":"smallmultiples", "chart":"bar"|"line"|"area", '
    '"panels":[{"label":str,"series":[{"label":str,"value":number} ...]} ...], '
    '"unit"?:str}                                                    '
    'a GRID of same-shape mini-charts sharing ONE scale — compare the SAME '
    'metric across many segments/regions/periods at a glance (use instead of one '
    'crowded multi-series chart when there are 4+ comparable groups)\n'
    '  {"type":"graph", "nodes":[{"id":str,"label":str,"group"?:str,"spine"?:bool,'
    '"kind"?:str,"summary"?:str,"tags"?:[str]} ...], '
    '"edges":[{"from":str,"to":str,"label"?:str} ...]}             '
    'an analytical relationship/concept map (deps, data-flow, org) — clickable '
    'node detail panel, search, group filters. Mark load-bearing nodes spine:true, '
    'cluster with group, add kind/summary/tags for the detail panel; leave chart '
    'for numbers and mermaid for linear flows\n'
    '  {"type":"lens", "tabs":[{"label":str,"md":str} ...]}          '
    'SAME point reframed per audience/persona (each tab = one lens)\n'
    '  {"type":"timeline", "events":[{"time"?:str,"title":str,"body"?:str,"actor"?:str} ...]} '
    'ordered beats / roadmap / sequence of events\n'
    '  {"type":"options", "options":[{"title":str,"recommended"?:bool,'
    '"pros"?:[str],"cons"?:[str],"note"?:str} ...]}                  '
    'a decision matrix — competing choices, mark one recommended:true\n'
    '  {"type":"compare", "before":{"label"?:str,"items":[str]}, '
    '"after":{"label"?:str,"items":[str]}}                          before→after / v1→v2\n'
    '  {"type":"cards", "columns"?:int, "cards":[{"title":str,"body"?:str,"tag"?:str} ...]} '
    'a grid of peer items (capabilities, tiers, features)\n'
    '  {"type":"matrix", "columns":[str ...], "rows":[{"label":str,"cells":[bool|str ...]} ...], "highlightCol"?:int} '
    'a comparison grid — cells true/false/"partial" render ✓/✗/~, else text; '
    'highlightCol tints the winning column (us-vs-competitors, option×criteria)\n'
    '  {"type":"meter", "items":[{"label":str,"value":number,"max"?:number,"note"?:str,"state"?:"good"|"warn"|"bad"} ...], "unit"?:str} '
    'horizontal proportion/budget/latency bars (token budget, % of a whole, edge-vs-cloud ms)\n'
    '  {"type":"steps", "steps":[{"n"?:str,"title":str,"detail"?:str,"badge"?:str,"actor"?:"edge"|"cloud"|"user"|"process","meta"?:str} ...]} '
    'a numbered pipeline/sequence — actor colors the step, badge=tool, meta=timing\n'
    '  {"type":"spec", "cards":[{"title":str,"variant"?:"active"|"research"|"ambient"|"default","badge"?:str,"rows":[{"k":str,"v":str} ...]} ...]} '
    'tier/model spec cards — colored border by variant, k-v spec rows\n'
    '  {"type":"code", "code":str, "lang"?:str, "filename"?:str}          a fenced code / prompt block (copy button)\n'
    '  {"type":"cta", "title"?:str, "items"?:[str], "action"?:{"label":str,"href":str}} '
    'a board-ask / open-questions box (heavier than callout)\n'
    '  {"type":"checklist", "items":[{"text":str,"done"?:bool} ...], "columns"?:int} a ✓ coverage grid\n'
    '  {"type":"decisionrecord", "question"?:str, "decision":str, '
    '"options"?:[{"label":str,"chosen"?:bool,"rejected_because"?:str} ...], '
    '"reversal_trigger"?:str, "confidence"?:str} an auditable ADR: the call, what '
    'was rejected + why, and the trip-wire that reverses it (use for a decision/ask facet)\n'
    '  {"type":"assumptionledger", "assumptions":[{"text":str,"confidence"?:"high"|"med"|"low",'
    '"falsifier"?:str,"load_bearing"?:bool} ...]} the load-bearing beliefs the case '
    'rests on, each with a confidence and a FALSIFIER (what would change your mind) — '
    'surface these explicitly for a decision/strategy facet so they can be challenged\n'
    '  {"type":"tripwires", "wires":[{"condition":str,"metric"?:str,"status"?:"clear"|"watch"|"tripped"} ...]} '
    'pre-committed reversal conditions ("reverse if X crosses Y") with live status — decision hygiene for an ask/decision facet\n'
    '  {"type":"risk", "items":[{"title":str,"severity"?:"high"|"med"|"low","likelihood"?:"high"|"med"|"low",'
    '"mitigation"?:str,"owner"?:str} ...], "caption"?:str} a RISK REGISTER: the named risks of a plan, each '
    'graded by severity (impact) × likelihood, with its mitigation and owner — use for a risk/decision facet\n'
    '  {"type":"simulator", "inputs":[{"key":str,"label":str,"min":num,"max":num,"step"?:num,"value":num,"unit"?:str} ...], '
    '"outputs":[{"label":str,"expr":str,"unit"?:str,"decimals"?:int} ...]} an INTERACTIVE model: '
    'assumption sliders the reader moves to recompute outcomes live. expr is arithmetic over input keys '
    '(+ - * / % ^ and parens), e.g. "seats*price*12*margin". Use when the case turns on 2-4 uncertain '
    'numbers the reader should stress-test themselves\n'
    '  {"type":"frequency", "items":[{"label":str,"count":num,"of":num,"state"?:"good"|"warn"|"bad"} ...]} '
    'a probability/risk as a DISCRETE COUNT icon array ("12 of 100"), not a '
    'percentage — a mixed-numeracy board reads counts far more accurately. Use '
    'for base rates, failure/success odds, hit rates. count out of of; state '
    'colours it\n'
    '  {"type":"hops", "draws"?:[num ...], "quantiles"?:{"p10":num,"p50":num,"p90":num}, '
    '"unit"?:str, "label"?:str, "reference"?:num, "referenceLabel"?:str, "decimals"?:int} '
    'a Hypothetical Outcome Plot — an UNCERTAIN forecast shown as its spread, not '
    'a single number. Give EITHER an explicit "draws" array of equally-likely '
    'sampled outcomes OR a "quantiles" {p10,p50,p90} summary. reference marks a '
    'target/breakeven. Use for revenue/cost/timeline forecasts, downside ranges — '
    'anywhere a point estimate would over-promise certainty. NEVER invent draws; '
    'derive from stated ranges only\n'
    '  {"type":"evidence", "claims":[{"text":str,"source"?:str,"confidence"?:"high"|"med"|"low",'
    '"as_of"?:str,"grade"?:"primary"|"secondary"|"model"|"assumption"} ...]} '
    'an evidence "nutrition label" — the load-bearing claims with their SOURCE, a '
    'confidence level, an as-of date, and a source GRADE (primary=data/filing, '
    'secondary=report, model=AI/estimate, assumption=unverified). Use to make an '
    'AI-authored recommendation auditable for a skeptical board. Only fill source/'
    'as_of from the INPUT — never fabricate a citation\n'
    '  {"type":"biascheck", "biases":[{"bias":str,"risk":str,"counter":str,"severity"?:"high"|"med"|"low"} ...]} '
    'names the specific COGNITIVE BIASES threatening THIS decision (authority, '
    'confirmation, sunk-cost, groupthink…), each with how it distorts the call '
    '(risk) and the counter-evidence/dissent that pre-empts it. Use on a '
    'decision/ask facet to show the recommendation survived its own scrutiny\n'
    '  {"type":"messageproof", "audiences"?:[str ...], '
    '"messages":[{"message":str,"proofs":[{"text":str,"audiences"?:[str],"weight"?:"high"|"med"|"low",'
    '"source_ref"?:str} ...]} ...]} the equity story — each core MESSAGE backed by '
    'distinct PROOF points, each proof tagged with the audience roles it lands for '
    'and a weight. audiences lists the roles (e.g. "board","investor","regulator") '
    'so the reader can filter proofs per audience. Use to carry ONE deck that '
    'proves the case differently to different high-stakes readers\n'
    '  {"type":"statement", "text":str, "kicker"?:str, "footnote"?:str, "align"?:"left"|"center", "display"?:"inline"|"standalone"} '
    'ONE assertion as the whole slide — huge type, no other module. Use for a '
    'chapter-defining claim or the single takeaway of a facet; keep text under '
    '~14 words. display:"standalone" renders it full-bleed centered\n'
    '  {"type":"quote", "quote":str, "attribution"?:str, "role"?:str, "display"?:"inline"|"standalone"} '
    'a stakeholder pull-quote with attribution — use a REAL quote from the input, '
    'never invent one\n'
    '  {"type":"gallery", "images":[{"src":str,"caption"?:str} ...], "columns"?:int} '
    'a tiled grid of images (src = a URL or data URI from the input) — visual '
    'proof by volume; do not fabricate image srcs\n'
    'MERMAID: in a media mermaid spec, ALWAYS quote node labels — '
    'A["text"] not A[text] — and use <br/> for line breaks, never \\n. Labels '
    'with parentheses, commas, slashes or arrows break the parser unless quoted.\n'
    'PROVENANCE: any RichBlock MAY carry "source_ref":str — when the INPUT '
    'annotates a claim with "(source: X)", copy X VERBATIM onto the block that '
    'renders that claim, so the deck stays auditable. Omit source_ref when the '
    'input gives no source for that content; NEVER invent one.\n'
    '  {"type":"callout", "variant":"risk"|"decision"|"wow"|"info"|"warn", "text":str} '
    'a short boxed aside — a risk flag, a decision, a wow-moment, or an info/warn '
    'note; one or two sentences, NOT a data module\n'
    '  {"type":"diagram", "spec":str} a MERMAID diagram for a LINEAR flow / sequence '
    '/ state (spec = mermaid source). Quote every node label — A["text"] not '
    'A[text] — and use <br/> not \\n; leave graph for non-linear relationship maps\n'
)


_SCHEMA = _SCHEMA_PREFIX + _RICHBLOCK_CATALOG + (
    'Every rung is REQUIRED on every node and the four form a PROGRESSIVE-DEPTH '
    'ladder — the SAME section explained at four increasing depths, each a '
    'SUPERSET of the one before (what → how → how-exactly → implementation), '
    'never a restatement and never losing detail as you go right:\n'
    '  L1 = WHAT it solves — the outcome/point in ONE BLUF sentence, the '
    'highest level, the least detail. This is the COVER line (body only, no module);\n'
    '  L2 = HOW it solves it — the shape of the solution. LEAD with ONE '
    'STRUCTURAL module that CARRIES the shape (compare for a before→after or '
    'contrast, cards for a set of peer items, options for competing choices, '
    'matrix for us-vs-them, stat for headline numbers, statement for the single '
    'claim); prose is connective tissue only, never the whole level. A set of 2+ '
    'peer items is a cards/options/compare block — NEVER a markdown bullet list '
    'in the body. Keep it to ONE module (visual weight still rises into '
    'L3/L4);\n'
    '  L3 = HOW EXACTLY — the mechanism: the moving parts, interfaces and '
    'steps that make it work (body + the substantive modules: table/chart/steps/'
    'diagram);\n'
    '  L4 = FULL DETAIL — the deepest layer: specifics, parameters, edge '
    'cases and trade-offs WITH the rationale (body + the densest modules + risk/'
    'decision/wow callouts where they matter).\n'
    'Detail AND visual weight MUST increase L1→L2→L3→L4. USE THE '
    'RIGHT MODULE: stat for a few headline numbers, chart for a series/'
    'distribution/trend, table for comparisons/attribute lists, graph for '
    'relationships, diagram for linear flows, callout for warnings/decisions, '
    'timeline for a sequence, options for a decision, compare for before→after, '
    'cards for peer items, lens for per-audience reframings, matrix for '
    'us-vs-competitors/option×criteria grids, meter for budget/proportion/latency '
    'bars, steps for a numbered pipeline, spec for tier/model spec cards — do '
    'not force structured data into prose. A rung MAY equal the one below it when nothing more to '
    'add — never pad.'
)


# The intake contract (Phase G): generation only gets topic+person+brand, so
# the decision it serves, likely objections, and hard constraints are often
# unstated. Surface the assumption rather than inventing silently — an honest
# gap the user can correct beats a confident guess they can't see.
_PUNCHY_CLAUSE = (
    "WRITE PUNCHY — decks fail when the text reads like paragraphs. Every "
    "headline and L1 line is a TAGLINE: assertion-first, one idea, target "
    "6-10 words (≤14 if a full sentence). Cut hedges (\"we believe\", \"it "
    "seems\", \"in order to\"), prefer verbs over nominalizations (\"decide\" not "
    "\"make a decision\"), concrete nouns over abstractions, front-load the "
    "payoff. Vary the shape screen to screen — not every facet is a paragraph: "
    "use a statement block for a single decisive claim, a quote for a "
    "stakeholder voice, bullets for a set, prose only when the argument needs "
    "connective tissue. Bodies stay short; the module carries the structure."
)


_INTAKE_CLAUSE = (
    "INTAKE — if the TOPIC leaves the primary decision/ask, the likely "
    "objections, or hard constraints (format, compliance, locale) UNSTATED, do "
    "NOT silently invent them. Make the single load-bearing assumption you had "
    "to make EXPLICIT — one short 'Assumes: …' line in the overview's L4 "
    "level — so the reader can correct it. Never fabricate specifics as fact."
)


def _doc_system(design: dict[str, Any], voice: str = "", diversity: str = "") -> str:
    design_txt = json.dumps(design, separators=(",", ":"))[:1500] if design else "(no brand tokens — use a clean dark theme)"
    voice_block = f"\n\n{voice}" if voice else ""
    diversity_block = f"\n\n{diversity}" if diversity else ""
    return (
        "You are okuro·prism's facet-tree authoring agent. Produce a single "
        "topic as a progressive-disclosure OUTLINE: an overview root that "
        "drills into sections.\n"
        "CRITICAL TREE RULE: the root is an OVERVIEW and MUST decompose into "
        "3-6 CHILD facets — each a distinct SECTION the reader drills into. A "
        "single-facet tree is INVALID; never put the whole topic in one node. "
        "`children` is a TOP-LEVEL key of each Node (a SIBLING of `rungs`), holding "
        "an ARRAY of child Nodes — NEVER place children inside a rung, `media`, or "
        "`callouts`. "
        "Go 2 levels deep by default (children may have their own 2-4 children "
        "where the section has real substructure), 3 only if the topic truly "
        "has that much structure.\n\n"
        + _SCHEMA
        + "\n\n" + _PUNCHY_CLAUSE
        + "\n\n" + _INTAKE_CLAUSE
        + diversity_block
        + voice_block
        + "\n\nBRAND DESIGN TOKENS (visual paint — colours/type; do not invent "
        "facts from them):\n"
        + design_txt
        + "\n\nOutput ONLY the JSON object."
    )


def _doc_user(topic: str, lens: str, context: str) -> str:
    parts = [f"TOPIC for the doc:\n{topic}\n"]
    if lens.strip():
        parts.append(
            "RECIPIENT LENS — bias which facets exist and how deep they go "
            "toward what THIS person cares about (their cognitive style, "
            "flight level, risk vs. opportunity framing):\n"
            + lens.strip()[:2500]
            + "\n"
        )
    if context.strip():
        parts.append(
            "GROUNDED FACTS from okuro's brain (use these; do not invent "
            "specifics you weren't given):\n" + context.strip()[:5000] + "\n"
        )
    parts.append("Author the outline now as a single JSON object. Nothing else.")
    return "\n".join(parts)


def _extract_json_span(output: str, open_ch: str, close_ch: str) -> str:
    """Strip fences/prose and return the outermost ``open_ch``...``close_ch`` span."""
    text = (output or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start = text.find(open_ch)
    end = text.rfind(close_ch)
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no {open_ch}...{close_ch} span in output")
    return text[start : end + 1]


def _balance_brackets(candidate: str) -> str:
    """Append whatever closing ``}``/``]`` chars are needed to balance any
    unclosed ``{``/``[`` (string-literal-aware scan, nesting order preserved).
    Live-repro'd root cause (2026-07-04): the model occasionally drops exactly
    one closing brace near the end of an otherwise well-formed outline — NOT
    output truncation (duration well under timeout, prose reads complete).

    Returns ``candidate`` UNCHANGED on a mismatched closer (e.g. a ``]`` where
    a ``}`` was expected) rather than guessing — that malformation isn't a
    simple missing-bracket case and appending a naive closer would only mask
    the real defect. ``_lenient_json_loads`` falls through to ``json_repair``
    for it instead."""
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in candidate:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return candidate  # mismatched closer — bail, let json_repair handle it
            stack.pop()
    return candidate + "".join(reversed(stack))


def _lenient_json_loads(candidate: str) -> Any:
    """``json.loads`` with three fallbacks, cheapest first: strip trailing
    commas before a closing bracket, balance unclosed brackets, then
    ``json_repair`` (handles the remaining malformation classes live-repro'd
    against ``prism_generate`` on 2026-07-04 that aren't just a dropped
    bracket — missing commas mid-object, mismatched quotes). The first two
    tiers are cheap and string-literal-safe; ``json_repair`` is the general
    fallback so ``generate_doc``'s regenerate-on-failure retry is the last
    resort, not the first line of defense."""
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    stripped = re.sub(r",(\s*[}\]])", r"\1", candidate)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_balance_brackets(stripped))
    except json.JSONDecodeError:
        pass

    import json_repair

    return json_repair.loads(stripped)


def _parse_doc(output: str) -> dict[str, Any]:
    """Extract the JSON object from the LLM output (tolerates fences/prose/trailing commas)."""
    return _lenient_json_loads(_extract_json_span(output, "{", "}"))


# Rich modules that AUGMENT the prose body (text/diagram/callout already live in
# body/media/callouts, which the ops + retailor edit paths operate on).
_RICH_BLOCK_TYPES = {
    "stat", "table", "image", "heading", "chart", "graph", "smallmultiples",
    "lens", "timeline", "options", "compare", "cards",
    "matrix", "meter", "steps", "spec",
    "code", "cta", "checklist",
    "redteam", "decisionrecord", "assumptionledger", "tripwires", "risk",
    "audiobrief", "simulator",
    "frequency", "hops", "evidence", "biascheck", "messageproof",
    "statement", "quote", "gallery", "visualizer",
    # renderer parity (blocks.tsx has a case for each) — previously stripped:
    "callout", "diagram", "flow",
}


def _norm_rung(raw: Any) -> dict[str, Any]:
    """Normalize a rung: prose ``body`` (+ media/callouts) is the retailor-editable
    spine; ``blocks`` carries optional rich visual modules ALONGSIDE it."""
    r = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"body": str(r.get("body") or "")}
    if isinstance(r.get("media"), list):
        out["media"] = r["media"]
    if isinstance(r.get("callouts"), list):
        out["callouts"] = r["callouts"]
    if isinstance(r.get("blocks"), list):
        rich = [b for b in r["blocks"]
                if isinstance(b, dict) and b.get("type") in _RICH_BLOCK_TYPES]
        if rich:
            out["blocks"] = rich
    return out


def _flatten_node(
    node: Any, *, parent_id: str | None, facets: dict[str, Any], seq: list[int]
) -> str | None:
    """Recursively flatten one nested outline node into ``facets`` (id-keyed,
    parent_id + children arrays). Returns the assigned facet id, or None if
    ``node`` isn't a usable dict."""
    if not isinstance(node, dict):
        return None
    seq[0] += 1
    fid = "f_root" if parent_id is None else f"f_{seq[0]}"
    kind = node.get("kind") if node.get("kind") in ("topic", "diagram", "decision", "metric") else "topic"
    raw_rungs = node.get("rungs") if isinstance(node.get("rungs"), dict) else {}
    rungs = {r: _norm_rung(raw_rungs.get(r)) for r in _RUNGS}

    facets[fid] = {
        "id": fid,
        "title": str(node.get("title") or fid),
        "kind": kind,
        "parent_id": parent_id,
        "children": [],
        "rungs": rungs,
    }
    # Assertion-evidence headline (the section's takeaway), distinct from the
    # short nav title. Optional — omitted when the model gives only a title.
    headline = node.get("headline")
    if isinstance(headline, str) and headline.strip():
        facets[fid]["headline"] = headline.strip()
    for child in node.get("children") or []:
        cid = _flatten_node(child, parent_id=fid, facets=facets, seq=seq)
        if cid is not None:
            facets[fid]["children"].append(cid)
    return fid


def _salvage_tree(root: Any) -> Optional[dict[str, Any]]:
    """Recover a mis-nested outline. Pass-1 sometimes buries the child SECTION
    nodes inside a rung/media/callouts object instead of the Node's ``children``
    array — valid JSON, but ``_flatten_node`` only sees the root, collapsing the
    doc to 1 facet. This walks the WHOLE raw structure, collects every node-like
    dict (has ``title`` + a ``rungs`` dict) wherever it hides, and rebuilds a
    clean 2-level tree: the first as root, the rest as its direct children.
    Returns None when there's nothing to recover (< 3 nodes found)."""
    nodes: list[dict[str, Any]] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if "title" in o and isinstance(o.get("rungs"), dict):
                nodes.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(root)
    if len(nodes) < 3:
        return None
    head, rest = nodes[0], nodes[1:]
    strip = lambda n: {k: v for k, v in n.items() if k != "children"}
    return {**strip(head), "children": [strip(n) for n in rest]}


def _normalize(raw: dict[str, Any], *, topic: str) -> dict[str, Any]:
    """Force the LLM outline into a valid PrismDoc (flat facets + entry_facet_id)."""
    title = str(raw.get("title") or topic[:60] or "Untitled doc").strip()
    facets: dict[str, Any] = {}
    entry_facet_id = _flatten_node(raw.get("root"), parent_id=None, facets=facets, seq=[0])
    if entry_facet_id is None or not facets:
        raise ValueError("generated outline has no usable root facet")
    return {"title": title, "entry_facet_id": entry_facet_id, "facets": facets}


def _invoke(
    prompt: str, system: str, *, thinking: int, provider: Optional[str],
    timeout: int = 150, capability: str = "standard",
) -> str:
    """invoke() with an explicit thinking budget (env, restored after).
    ``capability`` selects the model tier (fast/standard/quality) when no
    explicit provider pins the model — see ``_tier_capability``."""
    import os as _os

    from okuro.bridge.invoke import invoke

    prev = _os.environ.get("MAX_THINKING_TOKENS")
    _os.environ["MAX_THINKING_TOKENS"] = str(thinking)
    try:
        res = invoke(prompt=prompt, system_prompt=system, provider=provider, capability=capability, timeout=timeout)
    finally:
        if prev is None:
            _os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            _os.environ["MAX_THINKING_TOKENS"] = prev
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")
    return res.get("output") or ""


_ENRICH_SYSTEM = (
    "You are okuro·prism's MODULE pass. You are given a facet tree that already "
    "has prose. Your ONLY job: where a section's content is naturally a data / "
    "visual / narrative MODULE, emit that module as a real structured BLOCK. "
    "Output ONLY a JSON array — nothing else.\n\n"
    "Block types (put the REAL values in the block's own fields — NEVER describe "
    "a block in prose, NEVER use a mermaid diagram for these):\n"
    '  stat:     {"type":"stat","items":[{"value":str,"label":str,"delta"?:str,'
    '"trend"?:"up"|"down"|"flat","state"?:"good"|"warn"|"bad","sub"?:str} ...]} '
    '(add delta/trend/sub for context where the data supports it — a KPI with a '
    'baseline reads board-grade; trend=arrow, state=good/warn/bad meaning)\n'
    '  chart:    {"type":"chart","chart":"bar"|"line"|"area"|"pie","series":[{"label":str,"value":number} ...],"unit"?:str,"annotations"?:[{"at":series-index,"text":str} ...]} (annotations call out one datapoint — the peak/outlier — bar/line/area only, text <30 chars)\n'
    '  smallmultiples: {"type":"smallmultiples","chart":"bar"|"line"|"area","panels":[{"label":str,"series":[{"label":str,"value":number} ...]} ...],"unit"?:str} (same metric across 4+ segments, shared scale)\n'
    '  table:    {"type":"table","headers":[str ...],"rows":[[str ...] ...]}\n'
    '  timeline: {"type":"timeline","events":[{"time"?:str,"title":str,"body"?:str,"actor"?:str} ...]}\n'
    '  options:  {"type":"options","options":[{"title":str,"recommended"?:bool,"pros"?:[str],"cons"?:[str],"note"?:str} ...]}\n'
    '  compare:  {"type":"compare","before":{"label"?:str,"items":[str ...]},"after":{"label"?:str,"items":[str ...]}}\n'
    '  cards:    {"type":"cards","columns"?:int,"cards":[{"title":str,"body"?:str,"tag"?:str} ...]}\n'
    '  lens:     {"type":"lens","tabs":[{"label":str,"md":str} ...]}\n'
    '  graph:    {"type":"graph","nodes":[{"id":str,"label":str,"group"?:str,"spine"?:bool} ...],"edges":[{"from":str,"to":str} ...]}\n\n'
    "Return ONLY a JSON array; each item attaches block(s) to ONE rung of ONE "
    "section:\n"
    '  {"facet_id":str,"rung":"L1"|"L2"|"L3"|"L4","blocks":[Block ...],"body"?:str}\n'
    "Module→content fit: numbers→stat or chart · a sequence/roadmap→timeline · a "
    "choice→options · before/after→compare · peer items→cards · per-audience "
    "reframing→lens · an attribute matrix→table.\n"
    "DEPTH = DENSITY. Modules get RICHER as the level deepens, so a reader moving "
    "right (L1→L2→L3→L4) ALWAYS gains detail, never loses it. "
    "Place modules by level:\n"
    "  · L1 — NO modules. A single BLUF sentence, nothing else (the cover).\n"
    "  · L2 — usually NO module; at most ONE light one (a small stat row or a "
    "2-item compare). Keep it a lean headline, not the densest slide.\n"
    "  · L3 — the substantive modules (table, chart, timeline, options, "
    "cards, steps, compare) that carry the working detail.\n"
    "  · L4 — the DENSEST modules (full/annotated charts, multi-column tables, "
    "graph, matrix, smallmultiples), in ADDITION to what L3 shows. L4 is "
    "always the richest level of a facet.\n"
    "HARD RULE: never place a heavier module on a shallower level than a lighter "
    "one — the visual weight must increase with depth, matching the prose. Pull "
    "REAL values from the section's prose + the topic; if the prose only NAMES a "
    "module ('bar chart of X'), reconstruct the actual data. Add an optional "
    "\"body\" to replace a rung body that merely describes the block with a real "
    "one-sentence lead. OMIT sections with no natural module. NEVER invent data or "
    "a module that isn't supported by the content."
)


def _enrich_blocks(
    doc: dict[str, Any], topic: str, *, provider: Optional[str], policy_clause: str = "",
    voice: str = "", diversity: str = "", capability: str = "standard",
) -> int:
    """Second pass — the fix for single-call generation refusing to emit rich
    blocks. Pass 1 writes tree+prose reliably; this focused call's ONLY product is
    block JSON, so the model can't hide a module behind a body string. Attaches the
    emitted blocks (+ optional cleaned body) to the named rung. Best-effort:
    returns the count applied; never raises past the caller's guard. Mutates doc.

    ``policy_clause`` (audience.policy_prompt_clause) biases WHICH module types the
    pass emits toward the recipient's cognition — leapfrog #1 (audience-driven
    module selection). Empty for plain-topic / neutral-profile generation."""
    facets = doc.get("facets") or {}
    if not facets:
        return 0
    digest: list[str] = []
    for fid, f in facets.items():
        rungs = f.get("rungs") or {}
        bodies = " | ".join(
            f"{r}: {str((rungs.get(r) or {}).get('body') or '')[:400]}" for r in _RUNGS
        )
        digest.append(f"[{fid}] {f.get('title', '')}\n  {bodies}")
    policy_block = f"\n{policy_clause}\n" if policy_clause else ""
    voice_block = f"\n{voice}\n" if voice else ""
    diversity_block = f"\n{diversity}\n" if diversity else ""
    user = (
        f"TOPIC (source of truth for real values):\n{topic[:4000]}\n"
        f"{policy_block}{voice_block}{diversity_block}\n"
        f"SECTIONS:\n" + "\n".join(digest) + "\n\n"
        "Return ONLY the JSON array of block attachments."
    )
    raw = _invoke(user, _ENRICH_SYSTEM, thinking=0, provider=provider, timeout=180, capability=capability)
    items = _lenient_json_loads(_extract_json_span(raw, "[", "]"))
    if not isinstance(items, list):
        return 0
    applied = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        fid, rung = it.get("facet_id"), it.get("rung")
        if fid not in facets or rung not in _RUNGS:
            continue
        blocks = [
            b for b in (it.get("blocks") or [])
            if isinstance(b, dict) and b.get("type") in _RICH_BLOCK_TYPES
        ]
        if not blocks:
            continue
        rung_obj = facets[fid].setdefault("rungs", {}).setdefault(rung, {"body": ""})
        rung_obj["blocks"] = blocks
        if isinstance(it.get("body"), str) and it["body"].strip():
            rung_obj["body"] = it["body"].strip()
        applied += len(blocks)
    return applied


def _validate_provenance(doc: dict[str, Any], valid: set[str]) -> None:
    """Drop any block ``source_ref`` the model emitted that isn't a real grounding
    source (kills hallucinated citations, keeps auditable ones). Mutates in place.
    Only invoked when a caller supplies the known source set (the Resonance path);
    plain-topic generation passes no set and is left untouched."""
    for facet in (doc.get("facets") or {}).values():
        for rung in (facet.get("rungs") or {}).values():
            for block in rung.get("blocks") or []:
                if isinstance(block, dict) and block.get("source_ref") and block["source_ref"] not in valid:
                    block.pop("source_ref", None)


def generate_doc(
    topic: str,
    *,
    person_id: Optional[str] = None,
    brand_id: str = _DEFAULT_BRAND,
    provider: Optional[str] = None,
    tier: Optional[str] = None,
    diversity: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
    valid_source_refs: Optional[set[str]] = None,
    enrich_blocks: bool = True,
) -> dict[str, Any]:
    """Generate a NEW brand-styled facet tree from a topic. Entry rung resolved
    per ``resolve_depth_default``. Renders live at /prism on save.

    ``tier`` (fast/mid/top) picks the model via ``_tier_capability``;
    ``diversity`` (1-3) steers layout/module variety via ``_diversity_clause``.
    ``progress`` is an optional phase callback ("analyzing"/"writing"/
    "designing") for the job registry — never allowed to break generation."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic required")

    def _emit_phase(phase: str) -> None:
        if progress is None:
            return
        try:
            progress(phase)
        except Exception:  # noqa: BLE001 — a bad progress sink never blocks gen
            pass

    capability = _tier_capability(tier)
    diversity_clause = _diversity_clause(diversity)

    _emit_phase("analyzing")
    design = _brand_design(brand_id)
    brand_warnings = _brand_health(brand_id)
    if brand_warnings:
        logger.warning(
            "prism building on an unhealthy brand %s: %s",
            brand_id, "; ".join(brand_warnings),
        )

    ident, lens = "", ""
    policy_clause = ""
    narrative = ""
    framing = ""
    quant = ""
    audience_layout: dict[str, Any] = {}
    if person_id:
        ident = _recipient_identity(person_id)
        try:
            from okuro.peer.persons import person_lens

            lens = person_lens(person_id, context=topic) or ""
        except Exception as exc:
            logger.info("person_lens failed for %s: %s", person_id, exc)
        # Leapfrog #1 — audience-driven MODULE SELECTION: derive a prefer/avoid
        # module palette from the recipient's cognition (PII-firewalled) and
        # steer BOTH generation passes toward it. Neutral/absent profile → "".
        # Phase B — audience-aware LAYOUT: the same cognition also picks the
        # arrangement knobs (KPI-band-led for a board, narrative order for an
        # engineer), persisted on the doc for the layout engine at save.
        try:
            from okuro.peer.cognitive_profile import cognitive_profile_for_llm
            from okuro.prism.audience import (
                framing_clause,
                layout_profile,
                module_policy,
                narrative_clause,
                policy_prompt_clause,
                quant_render_clause,
                quant_render_policy,
            )

            _cog = cognitive_profile_for_llm(person_id)
            policy = module_policy(_cog)
            policy_clause = policy_prompt_clause(policy)
            narrative = narrative_clause(_cog)
            framing = framing_clause(_cog)
            quant = quant_render_clause(quant_render_policy(_cog))
            audience_layout = layout_profile(_cog)
            if policy_clause:
                logger.info(
                    "prism module policy for %s — prefer=%s avoid=%s layout=%s",
                    person_id, policy.get("prefer"), policy.get("avoid"), audience_layout,
                )
        except Exception as exc:  # noqa: BLE001 — module policy never blocks gen
            logger.info("module policy failed for %s: %s", person_id, exc)

    try:
        from okuro.sense.grounding import gather_context

        context, _src = gather_context(topic, budget=6000)
    except Exception:
        context = ""

    recipient = f"RECIPIENT (address them where natural): {ident}\n{lens}".strip() if (ident or lens) else ""
    for clause in (narrative, framing, policy_clause, quant):
        if clause:
            recipient = f"{recipient}\n\n{clause}".strip() if recipient else clause

    # A facet tree's output is structurally heavier than a slides deck (every
    # facet carries 4 rungs, not one) — a longer timeout than the 150s slides
    # default avoids truncating the JSON mid-stream on multi-facet topics.
    # Brand voice (Phase G) — the profile's `language` block as an explicit
    # authoring directive, enforced in BOTH passes. "" for a brand without one.
    from okuro.prism.voice import voice_clause
    language = _brand_voice(brand_id) or (
        design.get("language") if isinstance(design, dict) else None
    )
    voice = voice_clause(language)
    system = _doc_system(design, voice, diversity_clause)
    user = _doc_user(topic, recipient, context)
    doc: Optional[dict[str, Any]] = None
    last_err: Optional[Exception] = None
    attempts = 4
    _emit_phase("writing")
    for attempt in range(attempts):
        prompt = user if last_err is None else (
            user + f"\n\nYour previous attempt was rejected: {last_err}\n"
            "Re-emit the outline as strictly valid JSON (escape every double-quote "
            "and newline inside string values, no trailing commas) AND fix the "
            "issue above."
        )
        try:
            raw = _invoke(prompt, system, thinking=0, provider=provider, timeout=280, capability=capability)
            parsed = _parse_doc(raw)
            doc = _normalize(parsed, topic=topic)
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            logger.warning("generate_doc outline parse failed (attempt %d/%d): %s", attempt + 1, attempts, exc)
            continue
        # Collapsed tree defeats the whole navigable/drill-down model. First try
        # to SALVAGE deterministically (mis-nested children), then fall back to a
        # sharply-worded retry.
        n_facets = len(doc.get("facets", {}))
        if n_facets < 3:
            salvaged = _salvage_tree(parsed.get("root"))
            if salvaged is not None:
                doc = _normalize({"title": parsed.get("title") or topic[:60], "root": salvaged}, topic=topic)
                n_facets = len(doc.get("facets", {}))
                logger.info("generate_doc salvaged mis-nested tree into %d facets", n_facets)
        if n_facets < 3 and attempt < attempts - 1:
            last_err = ValueError(
                f"the outline had only {n_facets} facet(s). The root MUST decompose "
                "into 3-6 child SECTION facets. `children` is a TOP-LEVEL key of a "
                "Node (a sibling of `rungs`) — an ARRAY of child Nodes; NEVER put "
                "children inside a rung, `media`, or `callouts`. Never return a "
                "single-node or childless-root tree.")
            logger.warning("generate_doc tree too shallow (%d facets), retrying", n_facets)
            doc = None
            continue
        break
    if doc is None:
        raise RuntimeError(f"outline generation failed after {attempts} attempts: {last_err}")
    # Pass 2 — emit the rich blocks pass 1 reliably refuses to (see _enrich_blocks).
    if enrich_blocks:
        _emit_phase("designing")
        try:
            n = _enrich_blocks(
                doc, topic, provider=provider,
                policy_clause="\n\n".join(c for c in (policy_clause, quant) if c),
                voice=voice, diversity=diversity_clause, capability=capability,
            )
            if n:
                logger.info("prism block-enrichment attached %d blocks", n)
        except Exception as exc:  # noqa: BLE001 — never fail generation over enrichment
            logger.warning("block enrichment failed (keeping prose): %s", exc)
    if valid_source_refs is not None:
        _validate_provenance(doc, valid_source_refs)
    if brand_id:
        doc["brand_id"] = brand_id
    if audience_layout:
        doc["audience_layout"] = audience_layout

    entry_rung = resolve_depth_default(person_id)
    doc["entry_rung"] = entry_rung

    from okuro.prism import save_doc

    saved = save_doc(id=None, title=doc["title"], brand_id=brand_id, doc=doc, origin="generate")
    detail = saved.to_detail()
    detail["entry_rung"] = entry_rung
    if brand_warnings:
        detail["brand_warnings"] = brand_warnings
    return detail


_EDIT_SYSTEM = (
    "You edit an okuro·prism facet tree by emitting STRUCTURED OPS — never "
    "rewrite the whole tree. Output ONLY a JSON array of op objects, nothing "
    "else.\n\n"
    'Ops:\n'
    '  {"op":"set_meta","title?":str,"entry_facet_id?":str}\n'
    '  {"op":"add_facet","parent_id":str,"facet":{"title":str,"kind?":"topic"|"diagram"|"decision"|"metric","rungs?":{RUNG:{"body":str}}}}\n'
    '  {"op":"remove_facet","facet_id":str}\n'
    '  {"op":"move_facet","facet_id":str,"new_parent_id":str}\n'
    '  {"op":"set_rung","facet_id":str,"rung":"L1"|"L2"|"L3"|"L4","body":str,"media?":[...],"callouts?":[...],"blocks?":[RichBlock...]}\n'
    '  {"op":"set_title","facet_id":str,"title":str}\n'
    '  {"op":"reorder_children","parent_id?":str,"order":[facet_id...]}   (section order)\n'
    '  {"op":"set_block","facet_id":str,"rung":str,"index":int,"block":RichBlock}\n'
    '  {"op":"move_block","facet_id":str,"rung":str,"from":int,"to":int}\n'
    '  {"op":"remove_block","facet_id":str,"rung":str,"index":int}\n'
    "Reference facets by their existing id. A rung's visual modules are its "
    "`blocks` list (RichBlock = {type, ...}); edit ONE by INDEX with set_block/"
    "move_block/remove_block, rename with set_title, change SECTION order with "
    "reorder_children. Make the SMALLEST set of ops that satisfies the request. "
    "For a 'review', improve wording/clarity via set_rung ops."
)


def run_ops_instruction(
    doc: dict[str, Any],
    instruction: str,
    *,
    provider: Optional[str] = None,
    facet_id: Optional[str] = None,
) -> tuple[dict[str, Any], list[str], int]:
    """Shared NL-edit core: translate ``instruction`` into structured ops against
    ``doc`` (via ``_EDIT_SYSTEM``, thinking off) and apply them. Returns
    ``(new_doc, op_errors, ops_applied)`` WITHOUT persisting — callers decide how
    to save (in place vs. as a fresh variant). Reused by ``edit_doc`` and
    ``okuro.prism.retailor``.

    When ``facet_id`` is given the edit is SCOPED to that one facet: the prompt is
    told to touch only it, and the resulting ops are filtered to ``set_rung`` ops
    on that id — so a contextual "edit this slide" can never disturb the rest of
    the deck."""
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction required")

    scope = ""
    if facet_id:
        target = (doc.get("facets") or {}).get(facet_id) or {}
        title = target.get("title") or facet_id
        scope = (
            f"SCOPE — edit ONLY the facet whose id is \"{facet_id}\" (title: \"{title}\"). "
            "Emit set_rung ops referencing that id and nothing else; do NOT add, "
            "remove, move, or touch any other facet, and do NOT change the deck title.\n\n"
        )

    user = (
        f"Current facet tree (JSON):\n{json.dumps(doc, separators=(',', ':'))[:9000]}\n\n"
        f"{scope}"
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

    try:
        ops = _lenient_json_loads(_extract_json_span(res.get("output") or "", "[", "]"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"agent returned no valid ops: {exc}") from exc
    ops = ops if isinstance(ops, list) else []

    # Scoped edit: keep only FACET-LOCAL ops on the target facet — a hard
    # guarantee the contextual edit stays local even if the model over-reaches.
    # (Block/title/layout edits are allowed; cross-facet ops like reorder_children
    # or add/remove/move_facet are dropped from a single-slide scope.)
    if facet_id:
        _local = {"set_rung", "set_title", "set_block", "move_block", "remove_block", "set_layout"}
        ops = [o for o in ops if isinstance(o, dict) and o.get("op") in _local and o.get("facet_id") == facet_id]

    from okuro.prism.ops import apply_ops

    new_doc, op_errors = apply_ops(doc, ops)
    return new_doc, op_errors, len(ops)


_ALT_DIRECTIVE = (
    _DIVERSITY_CLAUSES[3] + "\n"
    "This is an ALTERNATIVE arrangement of the SAME content: deliberately choose "
    "DIFFERENT module TYPES and a different column layout than an obvious first "
    "take, so the reader can compare two distinct designs side by side. Same hard "
    "rule: never fabricate data to fill a module."
)


def generate_alternatives(doc_id: str, *, provider: Optional[str] = None) -> dict[str, Any]:
    """Author a SECOND visual arrangement of an existing deck so the reader can
    A/B each slide and keep their favorite.

    One extra pass-2 enrichment (with a "make it deliberately different" directive)
    produces alternative blocks for the whole doc; each rung that ends up with a
    genuinely different module set gets an ``alts`` entry (blocks + its own derived
    layout). The primary arrangement is untouched — ``alts`` is purely additive, so
    a deck with no alternatives renders exactly as before."""
    import copy as _copy

    from okuro.prism import get_doc, save_doc
    from okuro.prism.layout import derive_rung_layout

    existing = get_doc(doc_id)
    if existing is None:
        raise ValueError(f"doc '{doc_id}' not found")

    base = _copy.deepcopy(existing.doc)
    alt = _copy.deepcopy(existing.doc)
    _enrich_blocks(alt, existing.title, provider=provider, diversity=_ALT_DIRECTIVE, capability="standard")

    profile = base.get("audience_layout") if isinstance(base.get("audience_layout"), dict) else None
    base_facets = base.get("facets") or {}
    alt_facets = alt.get("facets") or {}
    n_alts = 0
    for fid, bf in base_facets.items():
        af = alt_facets.get(fid) or {}
        for r in _RUNGS:
            b_rung = (bf.get("rungs") or {}).get(r)
            a_rung = (af.get("rungs") or {}).get(r)
            if not isinstance(b_rung, dict) or not isinstance(a_rung, dict):
                continue
            a_blocks = a_rung.get("blocks")
            # Only an alternative worth offering: it exists and actually differs.
            if not a_blocks or a_blocks == b_rung.get("blocks"):
                continue
            a_layout = derive_rung_layout({"blocks": a_blocks}, profile)
            b_rung["alts"] = [{"blocks": a_blocks, "layout": a_layout, "label": "B"}]
            n_alts += 1

    saved = save_doc(
        id=existing.id, title=existing.title, brand_id=existing.brand_id,
        variant_of=existing.variant_of, doc=base, origin="alternatives", allow_empty=True,
    )
    detail = saved.to_detail()
    detail["alternatives_added"] = n_alts
    return detail


def pick_alternative(
    doc_id: str, facet_id: str, rung: str, *, alt_index: Optional[int] = None
) -> dict[str, Any]:
    """Commit an A/B choice for one facet's rung: ``alt_index=None`` keeps the
    primary (A) arrangement; an int swaps that alternative's blocks/layout into
    the primary. Either way the rung's ``alts`` are cleared — the favorite is
    chosen."""
    import copy as _copy

    from okuro.prism import get_doc, save_doc

    existing = get_doc(doc_id)
    if existing is None:
        raise ValueError(f"doc '{doc_id}' not found")

    doc = _copy.deepcopy(existing.doc)
    facet = (doc.get("facets") or {}).get(facet_id)
    rung_obj = (facet.get("rungs") or {}).get(rung) if isinstance(facet, dict) else None
    if not isinstance(rung_obj, dict):
        raise ValueError(f"facet '{facet_id}' rung '{rung}' not found")
    alts = rung_obj.get("alts") or []
    if alt_index is not None:
        if alt_index < 0 or alt_index >= len(alts):
            raise ValueError(f"no alternative at index {alt_index}")
        chosen = alts[alt_index]
        rung_obj["blocks"] = chosen.get("blocks") or []
        if chosen.get("layout"):
            rung_obj["layout"] = chosen["layout"]
        else:
            rung_obj.pop("layout", None)
    rung_obj.pop("alts", None)

    saved = save_doc(
        id=existing.id, title=existing.title, brand_id=existing.brand_id,
        variant_of=existing.variant_of, doc=doc, origin="pick-alt", allow_empty=True,
    )
    return saved.to_detail()


def edit_doc(
    doc_id: str, instruction: str, *, provider: Optional[str] = None, facet_id: Optional[str] = None
) -> dict[str, Any]:
    """Apply a natural-language edit/review to an existing doc via structured ops.
    Returns the saved doc detail (+ any per-op errors under 'op_errors'). Pass
    ``facet_id`` to scope the edit to a single facet (contextual per-slide edit)."""
    from okuro.prism import get_doc, save_doc

    existing = get_doc(doc_id)
    if existing is None:
        raise ValueError(f"doc '{doc_id}' not found")

    new_doc, op_errors, ops_applied = run_ops_instruction(
        existing.doc, instruction, provider=provider, facet_id=facet_id
    )

    saved = save_doc(
        id=doc_id,
        title=new_doc.get("title") or existing.title,
        brand_id=existing.brand_id,
        doc=new_doc,
        origin="chat-edit",
        allow_empty=True,
    )
    detail = saved.to_detail()
    detail["op_errors"] = op_errors
    detail["ops_applied"] = ops_applied
    return detail
