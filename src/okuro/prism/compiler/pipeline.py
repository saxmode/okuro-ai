# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler ORCHESTRATOR — runs the 7 stages end to end over
#   the content-hashed StageCache, with resume (--from <stage>) and retailor
#   (re-run profile-onward on the cached mine output). Entry is an okuro artifact
#   id ONLY. Surfaces NeedsDisambiguation to the caller instead of guessing a
#   recipient's brand. This is where the stages compose; no stage logic lives here.
# index: STAGES | PrismCompiler | compile_deck | run | retailor
# AGENT_HEADER_END -->
"""End-to-end prism compiler.

Each stage is cached by the CONTENT of its inputs, so ``run(from_stage=...)`` and
``retailor`` reuse everything upstream automatically. The orchestrator holds no
domain logic — it wires stage outputs into the next stage's inputs, persists each
IR to the cache, and applies the two hard code gates (slicer ``assert_invariants``
after author, plan ``assert_quotas`` after plan) so a broken IR never reaches
compose.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from okuro.prism.compiler import author as _author
from okuro.prism.compiler import compose as _compose
from okuro.prism.compiler import mine as _mine
from okuro.prism.compiler import outline as _outline
from okuro.prism.compiler import plan as _plan
from okuro.prism.compiler import profile as _profile
from okuro.prism.compiler import project as _project
from okuro.prism.compiler.cache import STAGE_ORDER, StageCache
from okuro.prism.compiler.ir import (
    Authored,
    AudienceProfile,
    ComposedDeck,
    Outline,
    PagePlan,
    Projection,
    Slices,
)
from okuro.db.engine import okuro_home

logger = logging.getLogger(__name__)

STAGES = STAGE_ORDER


class PrismCompiler:
    """Runs the compiler for one (artifact, recipients, brand) build."""

    def __init__(
        self,
        artifact_id: str,
        recipients: list[str],
        *,
        brand: str = "",
        brand_pins: Optional[dict[str, str]] = None,
        cache_dir: Optional[str | Path] = None,
        provider: Optional[str] = None,
        invoke_fn=None,
    ):
        self.artifact_id = str(artifact_id)
        self.recipients = recipients
        self.brand = brand
        self.brand_pins = brand_pins or {}
        self.provider = provider
        self.invoke_fn = invoke_fn
        root = cache_dir or (okuro_home() / "prism-cache" / self.artifact_id)
        self.cache = StageCache(root)

    # ── stage wrappers (cache-aware) ───────────────────────────────────────────
    def _cached(self, stage: str, inputs: Any, compute):
        hit = self.cache.get(stage, inputs)
        if hit is not None:
            return hit
        out = compute()
        self.cache.put(stage, inputs, out)
        return out

    def stage_mine(self) -> dict[str, Any]:
        return self._cached(
            "mine", {"artifact_id": self.artifact_id},
            lambda: _mine.mine(self.artifact_id, provider=self.provider,
                               invoke_fn=self.invoke_fn),
        )

    def stage_profile(self) -> AudienceProfile:
        inputs = {"recipients": sorted(self.recipients),
                  "brand_pins": self.brand_pins, "brand": self.brand}
        out = self._cached(
            "profile", inputs,
            lambda: _profile.profile(self.recipients, brand_pins=self.brand_pins).to_dict(),
        )
        return AudienceProfile.from_dict(out)

    def stage_project(self, mine_out: dict[str, Any], audience: AudienceProfile) -> Projection:
        inputs = {"claims": mine_out["claims"], "audience": audience.to_dict()}
        out = self._cached(
            "project", inputs,
            lambda: _project.project(mine_out, audience, provider=self.provider,
                                     invoke_fn=self.invoke_fn).to_dict(),
        )
        return Projection.from_dict(out)

    def stage_outline(self, projection: Projection, mine_out: dict[str, Any]) -> Outline:
        inputs = {"projection": projection.to_dict(), "claims": mine_out["claims"]}
        out = self._cached(
            "outline", inputs,
            lambda: _outline.outline(projection, mine_out, provider=self.provider,
                                     invoke_fn=self.invoke_fn).to_dict(),
        )
        return Outline.from_dict(out)

    def stage_author(self, outline: Outline, mine_out: dict[str, Any],
                     projection: Projection) -> Authored:
        inputs = {"outline": outline.to_dict(), "claims": mine_out["claims"],
                  "projection": projection.to_dict()}
        out = self._cached(
            "author", inputs,
            lambda: _author.author(outline, mine_out, projection, provider=self.provider,
                                   invoke_fn=self.invoke_fn).to_dict(),
        )
        authored = Authored.from_dict(out)
        _author.assert_invariants(authored, outline)   # hard code gate
        return authored

    def stage_plan(self, outline: Outline, mine_out: dict[str, Any],
                   authored: Authored) -> PagePlan:
        inputs = {"outline": outline.to_dict(), "claims": mine_out["claims"],
                  "authored": authored.to_dict()}
        out = self._cached(
            "plan", inputs,
            lambda: _plan.plan(outline, mine_out, authored).to_dict(),
        )
        pp = PagePlan.from_dict(out)
        tc = _plan.topic_cardinality(outline, mine_out, authored)
        # Safety net: re-enforce the CORRECTNESS quotas (density ladder + shape +
        # cardinality) on the cached plan. Variety is plan()'s concern (it may have
        # legitimately relaxed it for a narrow deck, logging so), so strict_variety
        # is off here to avoid false-failing such a plan.
        _plan.assert_quotas(pp, {tid: c[1] for tid, c in tc.items()},
                            {tid: c[0] for tid, c in tc.items()}, strict_variety=False)
        return pp

    def stage_compose(self, page_plan: PagePlan, slices: Slices, authored: Authored,
                      outline: Outline, mine_out: dict[str, Any]) -> ComposedDeck:
        inputs = {"plan": page_plan.to_dict(), "slices": slices.to_dict(),
                  "authored": authored.to_dict(), "brand": self.brand}
        out = self._cached(
            "compose", inputs,
            lambda: _compose.compose(page_plan, slices, authored, outline, mine_out,
                                     brand=self.brand, provider=self.provider,
                                     invoke_fn=self.invoke_fn).to_dict(),
        )
        return ComposedDeck.from_dict(out)

    # ── orchestration ──────────────────────────────────────────────────────────
    def run(self, *, from_stage: Optional[str] = None) -> ComposedDeck:
        """Compile the deck. ``from_stage`` re-runs that stage onward even on
        identical inputs (drops its cache and everything downstream)."""
        if from_stage:
            self.cache.invalidate_from(from_stage)

        mine_out = self.stage_mine()
        audience = self.stage_profile()
        projection = self.stage_project(mine_out, audience)
        outline = self.stage_outline(projection, mine_out)
        authored = self.stage_author(outline, mine_out, projection)
        slices = _author.build_slices(authored, outline)
        page_plan = self.stage_plan(outline, mine_out, authored)
        deck = self.stage_compose(page_plan, slices, authored, outline, mine_out)
        logger.info("prism compile complete: %d slides across %d topics",
                    len(deck.slides), len(page_plan.topic_order))
        return deck

    def compile_deck_doc(self, *, from_stage: Optional[str] = None) -> dict[str, Any]:
        """Compile and serialize straight to an A4 DeckDoc JSON dict (the viewer
        contract). Runs the same stages as ``run`` but retains outline + profile
        (which ``run`` discards) so the adapter can synthesize the hero + topic
        titles + L3 doc-views. This is the backend serializer the /prism/deck API
        returns."""
        from okuro.prism.compiler.deck_adapter import to_deck_doc

        if from_stage:
            self.cache.invalidate_from(from_stage)
        mine_out = self.stage_mine()
        audience = self.stage_profile()
        projection = self.stage_project(mine_out, audience)
        outline = self.stage_outline(projection, mine_out)
        authored = self.stage_author(outline, mine_out, projection)
        slices = _author.build_slices(authored, outline)
        page_plan = self.stage_plan(outline, mine_out, authored)
        deck = self.stage_compose(page_plan, slices, authored, outline, mine_out)
        return to_deck_doc(
            deck, outline, audience,
            deck_id=mine_out.get("source_artifact_id") or self.artifact_id,
            deck_title=mine_out.get("source_title") or "Briefing",
            tagline=outline.arc or "",
            brand=self.brand,
        )

    def _lens_audience(self, name: str) -> AudienceProfile:
        """A single-recipient AudienceProfile for one lens persona (reuses the
        profile stage; brand_pins disambiguate a multi-hat person). Not cached — the
        per-lens profile is cheap and keyed on ONE recipient."""
        return AudienceProfile.from_dict(
            _profile.profile([name], brand_pins=self.brand_pins).to_dict())

    def compile_authored_deck_doc(self, *, from_stage: Optional[str] = None,
                                  rewrite: bool = False,
                                  lenses: Optional[list[str]] = None,
                                  illustrate_fn=None) -> dict[str, Any]:
        """PRISM v4 W5: compile a deck2 DeckDoc through the W3 AUTHORING engine
        (resolution-ladder authoring + blocking ladder gate + accuracy accounting)
        instead of the legacy plan/compose tail. Reuses compiler stages 1-5 (mine,
        profile, project, outline, author — the author stage supplies each topic's
        L4 master doc + claim tiers), then hands each topic to ``author_ladder`` and
        serializes via the authoring->DeckDoc adapter. Fail-soft per topic: a gate
        with quality violations still emits the topic (its accuracy surfaces any
        REAL silent deficit); a topic is dropped only if it has no claims.

        ``rewrite`` (Phase B, ENGINE 1) turns on AUDIENCE TRANSLATION: after a topic
        is authored, its ladder is re-worded for the resolved audience (facts
        immutable) and passed through the BLOCKING per-statement entailment gate
        BEFORE the ladder/coverage/accuracy gates run — so those gates validate the
        rewritten ladder, and an invented fact raises ``EntailmentBlocked`` (never
        ships). Default off: brand switch is visuals-only and must never rewrite text.

        This is the ``prism_build_from_artifact`` / ``/deck2/compile`` product path.
        """
        from okuro.prism.authoring.accuracy import account
        from okuro.prism.authoring.audience import rewrite_ladder
        from okuro.prism.authoring.from_mine import build_topic_sources
        from okuro.prism.authoring.gate import check_ladder
        from okuro.prism.authoring.ladder import author_ladder
        from okuro.prism.authoring.lenses import attach_lens_overlays, resolve_brand_logo
        from okuro.prism.authoring.recipient_slots import hero_meta, personas_from_audience
        from okuro.prism.authoring.to_deckdoc import build_deck_doc

        if from_stage:
            self.cache.invalidate_from(from_stage)
        mine_out = self.stage_mine()
        audience = self.stage_profile()                 # raises NeedsDisambiguation upstream
        projection = self.stage_project(mine_out, audience)
        outline = self.stage_outline(projection, mine_out)
        authored = self.stage_author(outline, mine_out, projection)

        sources = build_topic_sources(mine_out, outline.to_dict(), authored.to_dict())
        if not sources:
            raise ValueError("authoring: no non-empty topics to compile")
        ladders = []
        accuracies = []
        for ts in sources:
            ladder = author_ladder(ts)
            if rewrite:
                # ENGINE 1: re-word for the audience; the per-statement entailment
                # gate is blocking (raises EntailmentBlocked on an invented fact).
                ladder, _ent = rewrite_ladder(
                    ladder, audience, provider=self.provider, invoke_fn=self.invoke_fn)
            gate = check_ladder(ladder, ts)             # gates validate the REWRITTEN ladder
            if not gate.passed:
                logger.warning("prism authored: topic %r ladder gate not clean (fail-soft): %s",
                               ts.topic_id,
                               "; ".join(f"{v.check}/{v.level}" for v in gate.violations))
            ladders.append(ladder)
            accuracies.append(account(ladder, gate))

        # The compiler path predates D-P1 and is not the workflow the overhaul
        # targets, so its naming behaviour is unchanged (mention_names=True).
        # Both engines now build these slots from ONE module — the workflow can
        # wire the same chips without importing the compiler.
        personas = personas_from_audience(audience, mention_names=True)
        meta = hero_meta(ladders[0].family if ladders else "", audience, self.brand)
        artifact_id = mine_out.get("source_artifact_id") or self.artifact_id
        doc = build_deck_doc(
            ladders, accuracies,
            deck_id=artifact_id,
            title=mine_out.get("source_title") or "Briefing",
            brand=self.brand, tagline=outline.arc or "",
            personas=personas, meta=meta,
            sources=sources, artifact_id=artifact_id,
            artifact_title=mine_out.get("source_title") or "",
            illustrate_fn=illustrate_fn,
            brand_logo=resolve_brand_logo(self.brand),
        )

        # MULTI-PERSPECTIVE (Phase D): one deck, three lens tabs. Each persona's
        # ladders are re-authored (deterministic engine) then rewritten for that
        # persona (Engine 1, entailment-gated) and folded in as per-cell overlays.
        if lenses:
            lens_variants: list[tuple[dict[str, Any], list[Any]]] = []
            for name in lenses:
                lens_aud = self._lens_audience(name)
                lens_ladders = []
                for ts in sources:
                    lad, _ent = rewrite_ladder(
                        author_ladder(ts), lens_aud,
                        provider=self.provider, invoke_fn=self.invoke_fn)
                    lens_ladders.append(lad)
                lens_variants.append((_lens_descriptor(name, lens_aud, len(lens_variants)),
                                      lens_ladders))
            attach_lens_overlays(doc, lens_variants)
        return doc

    def retailor(self, recipients: Optional[list[str]] = None,
                 brand_pins: Optional[dict[str, str]] = None,
                 brand: Optional[str] = None) -> ComposedDeck:
        """Re-tailor for a new audience/brand WITHOUT re-mining: mine output is
        kept; profile-onward re-runs. This is the retailor path the brief names."""
        if recipients is not None:
            self.recipients = recipients
        if brand_pins is not None:
            self.brand_pins = brand_pins
        if brand is not None:
            self.brand = brand
        self.cache.invalidate_from("profile")
        return self.run()


# Persona chips + hero meta moved to authoring/recipient_slots.py so the workflow
# can build the SAME slots without importing this engine, and so D-P1's naming
# toggle has exactly one implementation. Lens-tab descriptors stay here — they
# belong to the compiler's per-persona re-authoring, which the workflow does not
# have (the workflow's lens tabs are P5.3, on its own authoring path).
_PERSONA_MARKS = ("a", "b", "c")


def _lens_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "lens"


def _lens_descriptor(name: str, audience: AudienceProfile, idx: int) -> dict[str, Any]:
    """A DeckLens tab descriptor for one persona from its resolved profile."""
    r = audience.recipients[0] if audience.recipients else None
    disp = (r.name if r and r.name else name) or f"Reader {idx + 1}"
    lens = ""
    if r:
        lens = " · ".join(x for x in (r.role, r.brand) if x) or (r.lens[:40] if r.lens else "")
    return {"id": _lens_slug(name),
            "persona": {"mark": _PERSONA_MARKS[idx % 3], "name": disp, "lens": lens or "audience"}}


def compile_deck(
    artifact_id: str,
    recipients: list[str],
    *,
    brand: str = "",
    brand_pins: Optional[dict[str, str]] = None,
    provider: Optional[str] = None,
    cache_dir: Optional[str | Path] = None,
    invoke_fn=None,
    from_stage: Optional[str] = None,
) -> ComposedDeck:
    """Convenience one-shot: build a deck from an okuro artifact for recipients."""
    return PrismCompiler(
        artifact_id, recipients, brand=brand, brand_pins=brand_pins,
        provider=provider, cache_dir=cache_dir, invoke_fn=invoke_fn,
    ).run(from_stage=from_stage)
