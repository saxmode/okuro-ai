# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism pipeline ASSEMBLER — run the root-doc→deck stages
#   (distill → strategize → structure → write ladder) and assemble the result
#   into a real Prism facet-tree doc via the dedicated Component Arranger, gated
#   by the Critic (faithfulness + scenario + craft), then save it so it renders
#   at /prism. Nested topics become drill-down child facets (progressive
#   disclosure), each written from its own claims.
# index: build_deck
# AGENT_HEADER_END -->
"""Assemble the Prism target pipeline into a viewable deck.

Chains the built stages on a provided root document and emits a saved Prism doc.
Component selection uses the dedicated prism-component-expert Arranger (depth =
density, claims-only) over the full shared block catalog; a deck-level Craft gate
then re-arranges any facet that reads visually thin or same-shaped. Every topic
becomes a facet written from its OWN claims; a nested topic is linked to its
parent as a drill-down child (parent_id + children), giving progressive
disclosure when the architect produces depth and a flat deck when it doesn't.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.assemble")

from okuro.prism.rungs import RUNGS as _RUNGS


def _audience_kind_for(person_id: Optional[str]) -> Optional[str]:
    """The recipient's audience KIND (board|exec|technical|general) from their
    PII-firewalled cognitive profile, or None when there is no person / no
    profile — the neutral path where projection is a no-op and the deck ≈ today."""
    if not person_id:
        return None
    try:
        from okuro.peer.cognitive_profile import cognitive_profile_for_llm
        from okuro.prism.audience import audience_kind

        return audience_kind(cognitive_profile_for_llm(person_id) or {})
    except Exception as exc:  # noqa: BLE001 — a missing profile never blocks a build
        logger.warning("build_deck: audience_kind failed for %s: %s", person_id, exc)
        return None


def _project_for_recipient(
    topics: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    kind: str,
    *,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Project the master story onto ONE recipient KIND — the Stage-2 engine wired
    into the live pipeline.

    Builds a story over the (audience-shaped) topic tree + its claims, tags the
    claims (LLM overlay), projects for ``kind`` (pure SELECT + reframe), and picks
    the per-top-level L1 HOOK. Returns::

        {"surviving": set[claim_id],           # the claims that serve this reader
         "hooks": {top_topic_id: hook}}        # faithfulness-gated L1 hooks

    Pure-ish: the input topics/claims are never mutated (tagging + projection
    return fresh dicts). Raises only if the tagging LLM call fails — the caller
    guards it so a projection failure falls back to the full claim set."""
    from okuro.prism.story import enrich_topics, partition_evidence
    from okuro.prism.tags import tag_story
    from okuro.prism.projection import project_story
    from okuro.prism.hook import frame_hooks

    measured, unproven = partition_evidence(claims)
    story = {
        "throughline": "",
        "topics": enrich_topics(topics, claims),
        "claims": {c["id"]: c for c in claims if c.get("id")},
        "measured": measured,
        "unproven": unproven,
    }
    story = tag_story(story, provider=provider)      # LLM: audience overlay
    pstory = project_story(story, kind)              # pure: SELECT + reframe
    hooks = frame_hooks(pstory, provider=provider).get("hooks") or {}  # gated L1
    return {"surviving": set(pstory.get("claims") or {}), "hooks": hooks}


def _attach_hooks(
    facets: dict[str, Any], order: list[str], hooks: dict[str, Any]
) -> int:
    """Place each top-level facet's L1 hook as the single L1 impact block.

    Pure/deterministic: mutates the facet rungs in place and returns how many
    hooks were placed. A blank line or a missing facet is skipped; a prior hook
    block on L1 is replaced (idempotent), preserving any other L1 blocks after
    it. Only facets in ``order`` (the top-level chapters) are touched — a hook is
    a chapter-opening line, never a drill-down's."""
    n = 0
    for fid in order:
        facet = facets.get(fid)
        hk = hooks.get(fid) if hooks else None
        raw = (hk or {}).get("line", "")
        line = raw.strip() if isinstance(raw, str) else ""
        if not isinstance(facet, dict) or not line:
            continue
        stmt: dict[str, Any] = {"type": "statement", "text": line, "hook": True}
        rungs = facet.setdefault("rungs", {})
        l1 = rungs.get("L1")
        if isinstance(l1, dict):
            prior = [b for b in (l1.get("blocks") or [])
                     if not (isinstance(b, dict) and b.get("hook"))]
            l1["blocks"] = [stmt, *prior]
        else:
            rungs["L1"] = {"body": "", "blocks": [stmt]}
        n += 1
    return n


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "deck"


def build_deck(
    root_text: str,
    target: str,
    *,
    kind: str = "research",
    brand_id: str = "okuro",
    title: Optional[str] = None,
    doc_id: Optional[str] = None,
    review: bool = True,
    scenario: bool = True,
    target_group_id: Optional[str] = None,
    person_id: Optional[str] = None,
    visual_hooks: bool = True,
    provider: Optional[str] = None,
    progress=None,
) -> dict[str, Any]:
    """Root document + target → a saved Prism deck. Returns the saved doc detail.

    Stages: distill → strategize → structure → PROJECT (per-recipient) → weave
    scenario → per topic (write ladder → critic redo → arrange) → save. ``review``
    runs the Critic faithfulness gate (1 redo per facet + gates the scenario);
    ``scenario`` weaves a running case across topics. ``progress(msg)`` is called
    at each stage.

    ``person_id`` gates the Stage-2 STORY PROJECTION: when it resolves to an
    audience KIND, the master story is tagged + projected for that reader — the
    claim SET is trimmed to what serves them and each top-level topic gains a
    faithfulness-gated L1 HOOK (rendered on the L1 cover). No person / no profile
    (kind=None) → projection is a no-op and the deck is exactly today's; any
    projection failure falls back to the full claim set (a build never regresses)."""
    from okuro.prism.distiller import distill
    from okuro.prism.strategist import strategize, select_claims
    from okuro.prism.architect import structure
    from okuro.prism.depth_writer import write_ladder
    from okuro.prism.arranger import arrange_topic
    from okuro.prism.critic import (
        critique_craft, critique_faithfulness, critique_ladder, critique_scenario,
        sibling_sameness,
    )
    from okuro.prism.personas import resolve_personas
    from okuro.prism.composer import compose_deck, shape_group_hints
    from okuro.prism.shapes import ladder_density_violations
    from okuro.prism.weaver import weave_scenario
    from okuro.prism import save_doc

    def _p(msg: str) -> None:
        if progress:
            progress(msg)
        logger.info("build_deck: %s", msg)

    _p("distilling root document")
    distilled = distill(root_text, kind=kind, provider=provider)
    claims = distilled["claims"]
    if not claims:
        raise RuntimeError("distiller produced no claims")
    # Surface extraction quality — the input edge every later stage cites. A low
    # grounded_ratio or dropped source chars means the deck is built on a weak or
    # partial claim base; make it visible instead of silent (WS-0).
    grounded_pct = round(distilled.get("grounded_ratio", 0) * 100)
    trunc = distilled.get("dropped_chars") or 0
    _p(f"{len(claims)} claims ({grounded_pct}% source-grounded)"
       + (f"; {trunc} source chars over the {distilled.get('source_chars', 0) - trunc} cap DROPPED" if trunc else "")
       + "; strategising for target")
    # Group-resolution (WS-4 p2): a deck for a heterogeneous GROUP is resolved to
    # the FLOOR — least-technical jargon, lowest density, progressive disclosure /
    # dual framing when members span the poles — NOT the average of their profiles.
    # The directive rides on the target so the strategist (depth/jargon) and the
    # architect (arc) both honour it. No-op for a single recipient or generic group.
    eff_target = target
    if target_group_id:
        from okuro.prism.audience import group_policy, group_policy_clause
        from okuro.prism.personas import resolve_member_sliders
        gpol = group_policy(resolve_member_sliders(target_group_id))
        gclause = group_policy_clause(gpol)
        if gclause:
            eff_target = f"{target}\n\n{gclause}"
            _p("group policy: floor=" + gpol["floor_jargon"]
               + (", progressive-disclosure" if gpol.get("progressive_disclosure") else "")
               + (", dual-construal" if gpol.get("dual_construal") else "")
               + (", split-regulatory" if gpol.get("split_regulatory") else ""))
    brief = strategize(claims, eff_target, provider=provider)
    included = select_claims(claims, brief)

    # Persona lens tabs: only when the deck targets a CONCRETE group with >=2
    # profiled members. Resolved once; applied to top-level (overview) facets.
    personas = resolve_personas(target_group_id, context=(title or target or "")) if target_group_id else []
    if personas:
        _p(f"{len(personas)} personas resolved — lens tabs enabled")

    _p(f"kept {brief['kept']}/{len(claims)} claims; structuring")
    tree = structure(included, audience=eff_target, provider=provider)
    topics = tree["topics"]
    tops = [t for t in topics if not t.get("parent")]
    if not tops:
        raise RuntimeError("architect produced no top-level topics")

    # --- Per-recipient STORY PROJECTION (Stage-2 engine, gated + reversible) ---
    # When the recipient's audience KIND is known, project the master story over
    # this (audience-shaped) tree: SELECT the claims that serve THIS reader (drop
    # the rest, so the claim SET differs by recipient — not just the wording) and
    # pick a faithfulness-gated L1 HOOK per top-level topic. kind=None (no person /
    # no profile) → the whole overlay is skipped and the deck is exactly today's.
    # Any failure or an over-aggressive projection falls back to the full claim
    # set, so a build never regresses the working path.
    hooks: dict[str, Any] = {}
    kind = _audience_kind_for(person_id)
    if kind:
        _p(f"projecting the story for a '{kind}' reader")
        try:
            proj = _project_for_recipient(topics, included, kind, provider=provider)
        except Exception as exc:  # noqa: BLE001 — projection never blocks a build
            logger.warning("build_deck: projection skipped: %s", exc)
            proj = None
        if proj:
            surviving = proj["surviving"]
            projected = [c for c in claims if c.get("id") in surviving]
            # Adopt the projection only if it leaves a workable deck: >=1 top-level
            # topic still carries a surviving claim. Otherwise keep the full set.
            top_ok = any(
                any(cid in surviving for cid in (t.get("claim_ids") or [])) for t in tops
            )
            if projected and top_ok:
                claims = projected
                hooks = proj["hooks"]
                _p(f"projected for '{kind}': {len(claims)} of {len(included)} claims "
                   f"serve this reader; {len(hooks)} L1 hook(s)")
            else:
                _p(f"projection for '{kind}' left too little — using the full claim set")

    # Child topics become DRILL-DOWN facets under their parent (progressive
    # disclosure). Each facet is written from its OWN claims — no merge — so a
    # parent reads as the overview and its children carry the depth beneath it.
    children_by_parent: dict[str, list[dict]] = {}
    for t in topics:
        if t.get("parent"):
            children_by_parent.setdefault(t["parent"], []).append(t)
    for _kids in children_by_parent.values():
        _kids.sort(key=lambda t: t.get("order", 0))

    # Scenario: one running case threaded across topics. Critic-gated — a
    # fabricated scenario (the highest hallucination risk) is dropped, not shipped.
    beats: dict[str, str] = {}
    if scenario and len(tops) >= 2:
        _p("weaving scenario")
        try:
            sc = weave_scenario(tops, claims, brief=brief, provider=provider)
            if sc.get("case") and sc.get("beats"):
                ok = True
                if review:
                    # Scenario is NARRATIVE — gate on plausibility/consistency, not
                    # literal fact-matching (which would reject every invented name/time).
                    ok = critique_scenario(
                        sc["case"], list(sc["beats"].values()), claims, provider=provider
                    ).get("passed", False)
                if ok:
                    beats = sc["beats"]
                    _p(f"scenario kept ({len(beats)} beats)")
                else:
                    _p("scenario DROPPED — failed faithfulness gate")
        except Exception as exc:  # noqa: BLE001 — never fail assembly over the scenario
            logger.warning("build_deck: scenario skipped: %s", exc)

    facets: dict[str, Any] = {}
    order: list[str] = []          # top-level facet order (entry + deck sequence)
    all_ids: list[str] = []        # every built facet id (incl. children), for the craft gate
    arrange_ctx: dict[str, dict[str, Any]] = {}  # fid -> {rung_bodies, claims} for a craft re-arrange
    facet_fit: dict[str, Optional[float]] = {}   # fid -> weakest module fit (WS-3 craft signal)
    ceiling = brief.get("depth_ceiling") or "L3"
    # Topics whose ladder STILL asserts unsupported claims after the bounded
    # grounded-redo loop — surfaced on the returned detail so an ungrounded deck is
    # never shipped silently (blocker 3: faithfulness must be verified, not assumed).
    faith_residual: list[dict[str, Any]] = []

    def _build_facet(topic: dict, parent_id: Optional[str]) -> Optional[str]:
        """Write + arrange one topic into a facet, then recurse into its children
        as drill-down facets. Returns the facet id, or None if it produced no rungs."""
        facet_claim_ids = set(topic.get("claim_ids") or [])
        facet_claims = [c for c in claims if c.get("id") in facet_claim_ids]
        # Chart-unlock hints (Composer C): clusters of same-shape claims the
        # arranger should render as ONE multi-item chart/compare rather than N stats.
        facet_hints = shape_group_hints(facet_claims)

        _p(f"writing ladder: {topic['title']}")
        ladder = write_ladder(topic, claims, brief=brief, provider=provider)

        # Critic faithfulness gate → grounded rewrites until the ladder stops
        # asserting unsupported claims (bounded), RE-VERIFYING after each rewrite.
        # The old code did one redo and shipped it unchecked; a redo that still
        # drifted sailed straight through. Now every rewrite is re-verified and any
        # residual is recorded, so ungrounded content is never shipped silently.
        if review:
            try:
                text = " ".join(v for v in ladder.values() if v)
                chk = critique_faithfulness(text, facet_claims, provider=provider)
                attempts = 0
                while (not chk.get("passed")) and chk.get("unsupported") and attempts < 2:
                    fb = "; ".join((u.get("statement") or "") for u in chk["unsupported"][:5])
                    attempts += 1
                    _p(f"critic redo {attempts}/2: {topic['title']} ({len(chk['unsupported'])} unsupported)")
                    ladder = write_ladder(
                        topic, claims, brief=brief,
                        feedback="Remove or ground these unsupported statements: " + fb,
                        provider=provider,
                    )
                    text = " ".join(v for v in ladder.values() if v)
                    chk = critique_faithfulness(text, facet_claims, provider=provider)  # re-verify the rewrite
                if (not chk.get("passed")) and chk.get("unsupported"):
                    residual = [(u.get("statement") or "") for u in chk["unsupported"][:5]]
                    faith_residual.append({"topic": topic["title"], "unsupported": residual})
                    logger.warning(
                        "build_deck: %s ships %d ungrounded statement(s) after %d redo(s): %s",
                        topic["title"], len(chk["unsupported"]), attempts, " | ".join(residual),
                    )
            except Exception as exc:  # noqa: BLE001 — a failed critic never blocks the build
                logger.warning("build_deck: critic redo skipped for %s: %s", topic["title"], exc)

            # Ladder gate: a deeper rung must add NEW substance, not pad the prose.
            # One redo when a rung restates/inverts (the deep-detail defect).
            try:
                lad = critique_ladder(ladder, provider=provider)
                if not lad.get("passed") and lad.get("issues"):
                    fb = "; ".join(
                        f"{i.get('rung')}: {i.get('issue')}" for i in lad["issues"][:4]
                    )
                    _p(f"ladder redo: {topic['title']} ({len(lad['issues'])} padding/restatement)")
                    ladder = write_ladder(
                        topic, claims, brief=brief,
                        feedback="A deeper rung padded/restated instead of adding new data. "
                                 "Fix these — each deeper rung must introduce its NEW claims, "
                                 "and stay SHORT when it has few: " + fb,
                        provider=provider,
                    )
            except Exception as exc:  # noqa: BLE001 — the ladder gate never blocks the build
                logger.warning("build_deck: ladder redo skipped for %s: %s", topic["title"], exc)

        rung_bodies = {r: ladder[r] for r in _RUNGS if ladder.get(r)}
        if not rung_bodies:
            return None

        # Thread this facet's scenario beat as the opening scene of its L1.
        beat = beats.get(topic["id"])
        if beat and rung_bodies.get("L1"):
            rung_bodies["L1"] = f"*{beat}*\n\n{rung_bodies['L1']}"

        # Arrange real, claim-grounded modules per rung (depth = density).
        _p(f"arranging modules: {topic['title']}")
        try:
            arranged = arrange_topic(
                rung_bodies, facet_claims, brief=brief,
                personas=(personas if parent_id is None else None),
                shape_hints=facet_hints,
                provider=provider,
            )
        except Exception as exc:  # noqa: BLE001 — never fail assembly over arrangement
            logger.warning("build_deck: arrange failed for %s: %s", topic["title"], exc)
            arranged = {}

        rungs: dict[str, Any] = {}
        fits: list[float] = []
        for r, body in rung_bodies.items():
            rc: dict[str, Any] = {"body": body}
            a = arranged.get(r) or {}
            if a.get("blocks"):
                rc["blocks"] = a["blocks"]
            if a.get("layout"):
                rc["layout"] = a["layout"]
            if isinstance(a.get("fit"), (int, float)):
                fits.append(a["fit"])
            rungs[r] = rc

        # Density guard (deterministic): the deepest rung must be the DENSEST. If a
        # deeper rung carries fewer datapoints than a shallower one (the deep-thin
        # defect), re-arrange once, telling the arranger to push the denser modules
        # down. The claim allocation already front-loads finer claims to deep rungs;
        # this catches a residual inversion the LLM left.
        if review:
            rblocks = {r: (rungs[r].get("blocks") or []) for r in rungs}
            viol = ladder_density_violations(rblocks)
            if viol:
                _p(f"density redo: {topic['title']} — thin deep rung(s): {', '.join(viol)}")
                try:
                    re_arr = arrange_topic(
                        rung_bodies, facet_claims, brief=brief,
                        feedback="The DEEPEST rung must carry the MOST data. These rungs are "
                                 "thinner than a shallower one: " + ", ".join(viol) + ". Put the "
                                 "denser/fuller data modules on the deeper rungs; a deep rung "
                                 "must never have fewer data modules than a shallower one.",
                        personas=(personas if parent_id is None else None),
                        shape_hints=facet_hints,
                        provider=provider,
                    )
                    for r in rungs:
                        a = re_arr.get(r) or {}
                        if a.get("blocks"):
                            rungs[r]["blocks"] = a["blocks"]
                            if a.get("layout"):
                                rungs[r]["layout"] = a["layout"]
                            elif "layout" in rungs[r]:
                                rungs[r].pop("layout")
                except Exception as exc:  # noqa: BLE001 — the guard never blocks the build
                    logger.warning("build_deck: density re-arrange failed for %s: %s", topic["title"], exc)

        fid = topic["id"]
        facets[fid] = {
            "id": fid,
            "title": topic["title"],
            "headline": topic["title"],
            "kind": "topic",
            "parent_id": parent_id,
            "children": [],
            "rungs": rungs,
        }
        all_ids.append(fid)
        arrange_ctx[fid] = {"rung_bodies": rung_bodies, "claims": facet_claims, "hints": facet_hints}
        facet_fit[fid] = min(fits) if fits else None

        # Recurse: each child becomes a drill-down facet linked to this one.
        for child in children_by_parent.get(topic["id"], []):
            cid = _build_facet(child, fid)
            if cid:
                facets[fid]["children"].append(cid)
        return fid

    for top in tops:
        tid = _build_facet(top, None)
        if tid:
            order.append(tid)

    if not facets:
        raise RuntimeError("no facets produced")

    # Craft gate: content-truth is already gated; this gates VISUAL craft so decks
    # don't converge on stat+table+steps. Flagged facets get one richer re-arrange.
    if review and len(facets) >= 2:
        _p("critiquing craft (visual variety / hero module)")
        try:
            summary = [
                {
                    "facet_id": fid,
                    "title": facets[fid]["title"],
                    "fit": facet_fit.get(fid),
                    "rungs": {
                        r: [b.get("type") for b in (rc.get("blocks") or []) if isinstance(b, dict)]
                        for r, rc in facets[fid]["rungs"].items()
                    },
                }
                for fid in all_ids
            ]
            craft = critique_craft(summary, provider=provider)
            weak = {w["facet_id"]: w for w in craft.get("weak_facets", []) if w.get("facet_id") in facets}
            # Deck-level anti-sameness (deterministic): fold facets whose deepest
            # rung repeats an over-used lead module into the same re-arrange, telling
            # the arranger to ROTATE the lead. Does not overwrite an LLM craft flag.
            for fid, overused in sibling_sameness(summary).items():
                if fid in facets and fid not in weak:
                    weak[fid] = {
                        "facet_id": fid,
                        "issue": f"deepest rung leads with '{overused}' like too many sibling facets",
                        "suggest": f"lead this facet's deepest rung with a DIFFERENT module than "
                                   f"'{overused}' (vary the deck's visual rhythm)",
                    }
            if craft.get("missing_hero"):
                _p("craft: deck has no hero module (graph/diagram/lens/simulator)")
            for fid, w in weak.items():
                ctx = arrange_ctx.get(fid)
                if not ctx or not ctx["rung_bodies"]:
                    continue
                fb = (w.get("suggest") or w.get("issue") or "").strip()
                _p(f"craft redo: {facets[fid]['title']} — {fb[:80]}")
                try:
                    re_arr = arrange_topic(
                        ctx["rung_bodies"], ctx["claims"],
                        brief=brief, feedback=fb,
                        personas=(personas if facets[fid].get("parent_id") is None else None),
                        shape_hints=ctx.get("hints"),
                        provider=provider,
                    )
                except Exception as exc:  # noqa: BLE001 — a craft redo never blocks the build
                    logger.warning("build_deck: craft re-arrange failed for %s: %s", fid, exc)
                    continue
                for r, rc in facets[fid]["rungs"].items():
                    a = re_arr.get(r) or {}
                    if a.get("blocks"):
                        rc["blocks"] = a["blocks"]
                        if a.get("layout"):
                            rc["layout"] = a["layout"]
                        elif "layout" in rc:
                            rc.pop("layout")
            if weak or craft.get("missing_hero"):
                _p(f"craft: {len(weak)} facet(s) re-arranged")
            else:
                _p("craft: passed")
        except Exception as exc:  # noqa: BLE001 — never fail assembly over the craft gate
            logger.warning("build_deck: craft gate skipped: %s", exc)

    # L1 HOOKS: place the per-recipient hook as the L1 statement on each top-level
    # facet (the loudest, most defensible point for THIS reader). Every hook already
    # passed hook.gate_hook (it cites a real in-topic claim) — this only PLACES it;
    # the cover pass below folds it into the L1 cover line. Injected after all gates
    # so it never perturbs the density / craft passes.
    if hooks:
        _p("L1 hooks: placing the per-recipient impact line on each chapter")
        n_hk = _attach_hooks(facets, order, hooks)
        _p(f"L1 hooks: {n_hk} placed")

    # L1 COVERS: open every topic on a full-page COVER — a big-type statement
    # carrying the facet's single loudest idea (an existing per-recipient hook if
    # present, else the assertion headline). This REPLACES the old decorative
    # visualizer ring (a data-free illustration injected as block[0] of every facet)
    # with the slide model's real entry: the shallow level renders as a cover, never
    # a prose paragraph (inverse-density). EXTRACTIVE — cover_line only ever reuses
    # text already on the facet (hook > headline > title), so it invents no claim.
    from okuro.prism.slides import cover_block

    _p("covers: opening each topic on its L1 cover")
    n_cov = 0
    for fid in all_ids:
        facet = facets.get(fid)
        if not isinstance(facet, dict):
            continue
        l1 = (facet.get("rungs") or {}).get("L1")
        if not isinstance(l1, dict):
            continue
        blocks = l1.get("blocks") or []
        lead = blocks[0] if blocks else None
        # Idempotent: skip if a standalone cover is already the lead block.
        if isinstance(lead, dict) and lead.get("display") == "standalone" \
                and lead.get("type") in ("statement", "quote"):
            continue
        cover = cover_block(facet)
        if not cover:
            continue
        # Drop the plain hook statement the assembler placed — cover_line already
        # folded its text into the cover, so it would otherwise render twice.
        kept = [b for b in blocks if not (isinstance(b, dict) and b.get("hook"))]
        l1["blocks"] = [cover, *kept]
        n_cov += 1
    _p(f"covers: {n_cov} placed")

    # COMPOSER (layout intelligence) — runs LAST, after every content/craft gate,
    # so it never perturbs them. (A) condenses each shallow rung's prose wall to an
    # inverse-density lede (the deepest reading floor + prose-only rungs are kept),
    # and (B) resolves the deck's audience_layout so save's derive_layouts lights
    # the board KPI-band path (dead until now on the build_deck path). Fail-soft:
    # any failure is a no-op and the deck saves exactly as assembled.
    _p("composing (condense prose walls + resolve audience layout)")
    composed = compose_deck(facets, person_id=person_id, target_group_id=target_group_id)
    audience_layout = composed.get("audience_layout") or {}
    _p(f"composer: {composed.get('condensed', 0)} rung(s) condensed, "
       f"{composed.get('chars_removed', 0)} prose chars removed"
       + (f"; audience_layout={audience_layout}" if audience_layout else ""))

    # MESSAGE LAYER — write the crisp ASSERTION HEADLINE + lede per level (the
    # 'document parts → messages' step). This is the text templates lead with: a
    # slide's one 'so what' from its own claims ("Cortex hits 0.92 recall at 1/12
    # grep's context"), not the generic topic nav-title. One LLM call per facet;
    # best-effort — a facet that yields nothing keeps its topic title.
    if review:
        _p("composing messages (crisp per-slide headlines)")
        try:
            from okuro.prism.messages import stamp_messages
            n_msg = stamp_messages({"facets": facets}, claims, provider=provider)
            _p(f"messages: {n_msg} headline(s) composed")
        except Exception as exc:  # noqa: BLE001 — message copy never blocks a build
            logger.warning("build_deck: message layer skipped: %s", exc)

    deck_title = title or (brief.get("takeaway") or "Deck")[:80]
    doc = {
        "title": deck_title,
        "brand_id": brand_id,
        "entry_facet_id": order[0],
        # Enter on the COVER, not a dense content level: the deck opens on the L1
        # cover (biggest type, one idea), never mid-content at L3. The recipient's
        # reading ceiling still governs how deep the drill pre-expands (viewer),
        # but the ENTRY of the slide model is always the cover.
        "entry_rung": "L1",
        # Hero subtitle — the deck's takeaway, shown under the title on the HERO
        # slide (deck-level cover, distinct from the INDEX/agenda). Extractive: the
        # strategist's one-line takeaway, already grounded in the claims.
        "tagline": (brief.get("takeaway") or "").strip(),
        "facets": facets,
    }
    if audience_layout:
        doc["audience_layout"] = audience_layout

    _p("saving deck")
    saved = save_doc(
        id=doc_id or _slug(deck_title),
        title=deck_title,
        brand_id=brand_id,
        doc=doc,
        origin="pipeline",
        allow_empty=True,
    )
    if faith_residual:
        n = sum(len(t["unsupported"]) for t in faith_residual)
        _p(f"⚠ faithfulness: {n} ungrounded statement(s) across {len(faith_residual)} topic(s) — see detail")

    detail = saved.to_detail()
    detail["_brief"] = brief
    detail["_claim_count"] = len(claims)
    detail["_faithfulness_residual"] = faith_residual  # [] = clean; non-empty = ungrounded content shipped
    return detail
