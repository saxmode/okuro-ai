# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism workflow NODE 6 (assemble) — the SAVE stage the drawn
#   workflow was missing. Takes the run's authored topics + the AGENT's component
#   choices (reasoned over prism_library, Phase D), builds solver SlidePlans,
#   validates them against the typed boundary (schema.validate — a wrong choice is
#   REFUSED with defects, never repaired silently), runs the ladder gate + accuracy
#   accounting, composes the v4 kit via build_deck_doc, and persists to the deck2
#   store. The ONLY way a workflow deck is saved; legacy facet tools are sealed.
# index: convert_topic | assemble_deck
# AGENT_HEADER_END -->
"""Assemble the workflow's authored topics into a saved deck2 deck.

Levels exist BY JOB (Phase C) — this module never collapses or re-authors them.
The agent owns component choice; code owns validation, geometry and persistence.
Hard refusals: schema boundary violations (wrong component for a shape, no focal,
bad reading order). Reported, not refused: ladder-gate findings and accuracy
deficits — the same fail-soft rule ``compile_authored_deck_doc`` ships with.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from okuro.prism.authoring.accuracy import AccuracyReport, account
from okuro.prism.authoring.gate import check_ladder
from okuro.prism.authoring.ladder import L4Doc, LadderDoc, RenderedUnit
from okuro.prism.authoring.topic import ClaimGrounding, TopicSource
from okuro.prism.depth import display_map
from okuro.prism.solver.density import slide_density
from okuro.prism.solver.schema import Claim, InformationUnit, SlidePlan, validate
from okuro.prism.workflow.gather import GROUNDING_FLOOR
from okuro.prism.workflow.kit_budget import shape_budget, unrenderable
from okuro.prism.workflow.layout import derive_family, reading_order
from okuro.prism.workflow.types import AuthoredTopic

logger = logging.getLogger(__name__)

LEVELS = ("L1", "L2", "L3")


class AssembleDefects(ValueError):
    """The save was refused. ``defects`` name every violation, agent-fixable."""

    def __init__(self, defects: list[dict[str, str]]):
        self.defects = defects
        super().__init__(f"{len(defects)} defect(s) — save refused")


def _claims_for(topic: AuthoredTopic) -> tuple[dict[str, Claim], dict[str, ClaimGrounding]]:
    """One Claim per gathered fact (fact ids are the cross-level identity that
    makes superset checks meaningful). Tier: the most important block that cites
    the fact wins; a fact no block cites stays tier 1 (L4-only material)."""
    tier_by_fact: dict[str, int] = {}
    for lvl in topic.levels.values():
        for b in lvl.blocks:
            for fid in b.fact_ids:
                tier_by_fact[fid] = min(tier_by_fact.get(fid, b.tier), b.tier)
    claims: dict[str, Claim] = {}
    grounding: dict[str, ClaimGrounding] = {}
    for f in topic.facts:
        claims[f.id] = Claim(
            id=f.id, text=f.text, shape=f.shape,
            tier=tier_by_fact.get(f.id, 1), items=f.items, chars=len(f.text),
            items_content=tuple(tuple(t)[:3] for t in f.items_content),
        )
        grounding[f.id] = ClaimGrounding(source_quote=f.evidence)
    return claims, grounding


def _block_claim(block: Any, claims: dict[str, Claim],
                 grounding: dict[str, ClaimGrounding]) -> list[str]:
    """A block's claim ids. Blocks citing no fact get a synthesized claim from the
    block's own text — flagged in grounding so accuracy reports it, never hides it."""
    ids = [fid for fid in block.fact_ids if fid in claims]
    if ids:
        return ids
    cid = f"__block_{block.id or block.job or 'anon'}__"
    if cid not in claims:
        claims[cid] = Claim(
            id=cid, text=block.text, shape=block.shape, tier=block.tier,
            items=max(1, len(block.items_content)), chars=len(block.text),
            items_content=tuple(tuple(t)[:3] for t in block.items_content),
        )
        grounding[cid] = ClaimGrounding(synthesized=True)
    return [cid]


def convert_topic(
    topic: AuthoredTopic,
    choices: dict[str, list[str]],
    family: str,
) -> tuple[Optional[LadderDoc], Optional[TopicSource], list[dict[str, str]]]:
    """Authored levels + per-block component choices -> LadderDoc.

    ``choices`` maps level -> component id per block, in block order. Every
    boundary violation becomes a defect naming the level/block; any defect means
    no LadderDoc (refusal, not repair).
    """
    defects: list[dict[str, str]] = []
    claims, grounding = _claims_for(topic)
    plans: dict[str, SlidePlan] = {}
    units: dict[str, list[RenderedUnit]] = {}

    for lvl in LEVELS:
        authored = topic.levels.get(lvl)
        if authored is None or not authored.blocks:
            defects.append({"where": f"{topic.topic_id}.{lvl}",
                            "what": "level missing or empty",
                            "fix": "author the level — levels exist by job, all must ship"})
            continue
        comps = choices.get(lvl) or []
        if len(comps) != len(authored.blocks):
            defects.append({
                "where": f"{topic.topic_id}.{lvl}",
                "what": f"{len(authored.blocks)} blocks but {len(comps)} component choices",
                "fix": "pass exactly one component id per block, in block order",
            })
            continue
        ius: list[InformationUnit] = []
        rus: list[RenderedUnit] = []
        for b, comp in zip(authored.blocks, comps):
            cids = _block_claim(b, claims, grounding)
            n_items = max(1, len(b.items_content)) if b.items_content else None
            ius.append(InformationUnit(list(cids), comp, b.emphasis, items=n_items))
            dom = sorted(cids, key=lambda c: (claims[c].tier, c))[0]
            rendered = n_items or claims[dom].items
            rus.append(RenderedUnit(
                claim_ids=tuple(cids), component=comp, emphasis=b.emphasis,
                rendered_items=rendered, total_items=claims[dom].items,
                dominant_claim=dom,
            ))
        # Answer first: the focal unit is read first even when it was authored
        # second. Was `list(range(n))` — a permutation validator that only ever
        # saw the identity.
        plan = SlidePlan(lvl, family, ius, reading_order(ius))
        for err in validate(plan, claims):
            defects.append({"where": f"{topic.topic_id}.{lvl}", "what": err,
                            "fix": "re-choose via prism_library so the component "
                                   "fits the block's shape and emphasis rules hold"})
        plans[lvl] = plan
        units[lvl] = rus

    if defects:
        return None, None, defects

    body = "\n\n".join(
        (f"## {s.heading}\n\n{s.body}" if s.heading else s.body)
        for s in topic.l4.sections
    )
    # The L4-coverage gate proves each mined quote appears in the master doc; the
    # workflow's L4 is authored prose, so append the verbatim spans — the same rule
    # the product path applies in authoring/from_mine.py.
    spans = [g.source_quote for g in grounding.values()
             if g.source_quote and g.source_quote not in body]
    if spans:
        body += "\n\n## Evidence\n\n" + "\n\n".join(f"> {s}" for s in spans)

    l4 = L4Doc(topic.topic_id, topic.title, body,
               tuple(cid for cid, g in grounding.items() if not g.synthesized))
    loads = {lvl: float(sum(u.rendered_items for u in us) + len(us))
             for lvl, us in units.items()}
    densities = {
        lvl: slide_density([(u.component, u.rendered_items, 6) for u in us])
        for lvl, us in units.items()
    }
    ladder = LadderDoc(
        topic_id=topic.topic_id, family=family, claims=claims, grounding=grounding,
        plans=plans, units=units, loads=loads, densities=densities, l4=l4,
    )
    source = TopicSource(
        topic_id=topic.topic_id, title=topic.title, claims=claims,
        master_doc=body, grounding=grounding,
    )
    return ladder, source, []


def _quality_signals(authored: list[AuthoredTopic],
                     gatherings: Optional[dict[str, Any]]) -> list[str]:
    """Signals gather measured and used to log-and-forget.

    Both cross a CACHE boundary — gather runs once and its result is reused —
    so they are carried in the IR and read here rather than emitted where they
    were measured. A log line from a cached stage is gone by the time the deck
    exists.
    """
    out: list[str] = []
    for t in authored:
        # getattr throughout: a REPORTING pass must never be the reason a save
        # that would otherwise succeed explodes.
        bad = [f.id for f in (getattr(t, "facts", None) or [])
               if getattr(f, "shape_defaulted", False)]
        if bad:
            out.append(
                f"shape_defaulted: {getattr(t, 'topic_id', '?')} — {len(bad)} fact(s) "
                f"had no usable shape and render as 'narrative' "
                f"(fit-set 8/10 pure text): {bad[:5]}")
    # Blocks the kit cannot carry AS WRITTEN. The quota layer reports the
    # downstream symptom (a component out of its depth or capacity) as a kit
    # gap, correctly — the agent choosing it had no legal option. This names the
    # cause: a block was authored that the rung cannot express.
    for t in authored:
        for level, lv in sorted((getattr(t, "levels", None) or {}).items()):
            for b in getattr(lv, "blocks", None) or []:
                n = len(getattr(b, "items_content", None) or []) or 1
                if unrenderable(level, getattr(b, "shape", ""), n):
                    budget = shape_budget(level, getattr(b, "shape", ""))
                    out.append(
                        f"unrenderable_block: {getattr(t, 'topic_id', '?')}.{level} "
                        f"block {getattr(b, 'id', '?')} is shape "
                        f"{getattr(b, 'shape', '')!r} with {n} item(s); this rung "
                        + (f"holds {budget[0]}-{budget[1]}" if budget
                           else "has NO component for that shape"))

    for tid, g in (gatherings or {}).items():
        rate = getattr(g, "grounding_rate", 1.0)
        if rate < GROUNDING_FLOOR:
            out.append(
                f"low_grounding: {tid} — only {rate:.0%} of evidence spans trace "
                "verbatim to the source, yet they ship as the deck's per-cell "
                "'verbatim quote' provenance affordance")
    return out


def assemble_deck(
    authored: list[AuthoredTopic],
    choices: dict[str, dict[str, list[str]]],
    *,
    title: str,
    subtext: str = "",
    brand: str = "okuro",
    family: str = "",          # "" = derive from the reader; see workflow/layout.py
    deck_id: str = "",
    artifact_id: str = "",
    artifact_title: str = "",
    brief: Optional[Any] = None,
    mention_audience_members: bool = False,
    gatherings: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build + persist the deck. Refuses (AssembleDefects) on any boundary
    violation; ladder-gate findings and accuracy deficits ship WITH the deck.

    ``brief`` is the run's ``RecipientBrief``. Until it was threaded here the
    save dropped every recipient-shaped structure on the floor — the deck
    reached the right store carrying none of what made it recipient-driven, so
    tailoring survived only as the LLM's word choices in stages 2-5.

    ``mention_audience_members`` is D-P1's per-deck toggle, default OFF: chips
    carry the anonymized lens under a generic label unless the deck's owner has
    judged that naming the reader is safe. The lint that proves the toggle is
    what suppresses the name runs in this same call — a gate that lands a phase
    after the thing it gates is not a gate.

    NOT wired here, deliberately: ``lenses`` needs per-persona re-authoring
    through the compiler's deterministic engine, which the workflow replaced —
    that is P5.3 on the workflow's own authoring path. ``illustrate_fn`` is
    dropped rather than plumbed: measured, NO caller in the repo supplies a
    concrete callable, so threading it would wire None to None.
    """
    from okuro.prism.authoring.lenses import resolve_brand_logo
    from okuro.prism.authoring.recipient_slots import (
        audience_from_brief,
        find_attribution,
        hero_meta,
        personas_from_audience,
        recipient_names,
    )
    from okuro.prism.authoring.to_deckdoc import build_deck_doc
    from okuro.prism.compiler.deck_store import assert_landed, save_deck
    from okuro.prism.config import collect_warnings, warn_or_fail
    from okuro.prism.workflow.quotas import check_quotas

    if not title.strip():
        raise AssembleDefects([{"where": "deck", "what": "no title",
                                "fix": "pass the reader's-entry-point title"}])

    # FAMILY. Was the literal "swiss" for every deck ever built. An explicit
    # agent choice still wins — layout choice is the agent's job by design —
    # but an unstated one is now DERIVED from the reader rather than defaulted
    # in silence, and the deck records which of the three happened.
    fam = derive_family(brief, chosen=family)
    family = fam.family
    ladders: list[LadderDoc] = []
    sources: list[TopicSource] = []
    accuracies: list[AccuracyReport] = []
    defects: list[dict[str, str]] = []
    warnings: list[str] = []
    quota_input: list[tuple[str, dict[str, list[Any]], dict[str, Any]]] = []

    for t in authored:
        ladder, source, defs = convert_topic(t, choices.get(t.topic_id) or {}, family)
        if defs:
            defects.extend(defs)
            continue
        report = check_ladder(ladder, source)
        for v in getattr(report, "violations", []) or []:
            warnings.append(f"{t.topic_id}: {v}")
        accuracies.append(account(ladder, report))
        ladders.append(ladder)
        sources.append(source)
        # DECK ORDER, not sorted: adjacency is a property of the sequence the
        # reader walks, so re-ordering here would check a deck nobody sees.
        # getattr, because a caller may hand in a ladder double — a quota check
        # must never be the reason a save that would otherwise work explodes.
        quota_input.append((t.topic_id, getattr(ladder, "units", {}) or {},
                            getattr(ladder, "claims", {}) or {}))

    if defects:
        raise AssembleDefects(defects)
    if not ladders:
        raise AssembleDefects([{"where": "deck", "what": "no topics survived",
                                "fix": "author at least one topic before assembling"}])

    # Recipient structure, split by attribution class (D-P1). meta and brand_logo
    # are properties of the DECK — no name can leak through them, so they wire
    # unconditionally. personas carry a reader identity, so they obey the toggle.
    # The BRIEF is the setter of record. People ships
    # RecipientBrief.mention_audience_members and passes it through; prism owns
    # the toggle's meaning and the no-attribution lint. An explicit argument
    # still wins so a caller can force it, but a brief that carries the field
    # must not be ignored — that would be the declared-but-unread failure again,
    # this time across a module boundary.
    mention_audience_members = bool(
        mention_audience_members or getattr(brief, "mention_audience_members", False))
    audience = audience_from_brief(brief)
    personas = (personas_from_audience(audience, mention_names=mention_audience_members)
                if audience is not None else None)
    meta = hero_meta(family, audience, brand) if audience is not None else None

    # Every degradation the build tolerates (compose fallback, un-measured layout,
    # a cell rendering fixture text) reports through warn_or_fail. Collect them so
    # they ship WITH the deck instead of dying in a log line the reader never sees.
    # The sink stays open across the LINT too. Closing it after build_deck_doc
    # left the attribution warning with nowhere to go, so with strict off it
    # logged and vanished — the same "dies in a log line" failure this phase
    # exists to remove. Scope the collection around everything that can degrade.
    # Quality signals gather measured behind the cache. Reported, never
    # refusing: a defaulted shape becomes a hard error in P3, where a claim can
    # be typed correctly rather than merely rejected.
    warnings.extend(_quality_signals(authored, gatherings))

    with collect_warnings() as degradations:
        # ANTI-SAMENESS. The compiler's assert_quotas enforces exactly this and
        # cannot be called here (it takes a PagePlan keyed by archetype), so the
        # rules are ported to components in workflow/quotas.py. A breach the KIT
        # made unavoidable is reported as a library gap rather than refusing a
        # build the agent had no way to fix.
        for v in check_quotas(quota_input):
            if v.kit_gap:
                logger.warning("prism quota %s — kit gap: %s", v.code, v)
                warnings.append(f"{v.code} [kit gap]: {v}")
            else:
                warn_or_fail(v.code, str(v), where=v.where)

        doc = build_deck_doc(
            ladders, accuracies, deck_id=deck_id or title, title=title, brand=brand,
            tagline=subtext, sources=sources,
            artifact_id=artifact_id, artifact_title=artifact_title,
            personas=personas, meta=meta,
            brand_logo=resolve_brand_logo(brand),
        )

        # THE LINT, in the same phase as the wiring it gates. Walks the whole
        # built DeckDoc, not just the chips this module produced — a name is a
        # hazard wherever a reader meets it, including a title the author wrote
        # or a composed fragment.
        if audience is not None and not mention_audience_members:
            hits = find_attribution(doc, recipient_names(audience))
            if hits:
                warn_or_fail(
                    "audience_attribution",
                    f"deck names {len({h['name'] for h in hits})} audience member(s) in "
                    f"{len(hits)} place(s) with mention_audience_members OFF — "
                    f"first: {hits[0]['where']} → {hits[0]['excerpt']!r}",
                    hits=hits[:10],
                )
    warnings.extend(f"{d['code']}: {d['message']}" for d in degradations)
    saved_id = save_deck(doc)
    # The destination is asserted as a FACT, at the boundary the agent sees.
    # The July-27 wrong-engine run closed `done` on three LLM-judged text
    # criteria, none of which asked which store the deck reached. A returned id
    # is a claim; a file under prism-deck2 is evidence.
    landed = assert_landed(saved_id)
    # The agent reads this payload, so it speaks the reader's dialect (L1-L4),
    # not the deck2 store's L0-L2. The round trip is correct either way; what
    # was wrong is that every intermediate surface spoke a different language.
    overflow = {
        t["id"]: display_map({lvl: c["composed"].get("overflow")
                              for lvl, c in (t.get("levels") or {}).items()
                              if c.get("composed")})
        for t in doc.get("topics", [])
    }
    return {"deck_id": saved_id, "url": "/prism/deck", "topics": len(ladders),
            "store": "deck2", "path": str(landed),
            "family": fam.family, "family_source": fam.source,
            "family_rationale": fam.rationale,
            "overflow": overflow, "warnings": warnings}


__all__ = ["AssembleDefects", "convert_topic", "assemble_deck"]
