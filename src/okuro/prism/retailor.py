# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism recipient re-tailoring — re-cast an existing doc's rung
#   wording to a recipient + density/jargon/language/depth_override axes, in
#   place or as a linked variant.
# index:
#   imports
#   _DENSITY / _JARGON
#   def _deck_rung_text
#   def _axis_directives
#   def build_retailor_instruction
#   def retailor_facets
# AGENT_HEADER_END -->
"""okuro·prism recipient-tailoring — the differentiator.

Re-casts an *existing* doc (its facet tree already decided) to a new
recipient and/or explicit communication axes (density / jargon / language /
depth_override), WITHOUT regenerating it. The re-tailor is expressed as a
natural-language instruction and run through the SAME scoped-ops edit path
``edit_doc`` uses (``set_rung`` patches only — facet ids + tree shape stay
stable), so an open canvas live-updates and the doc's structure is preserved.

``depth_override`` (L1|L2|L3|L4) is prism's one net-new axis
vs. ``slides_retailor``: it forces the variant's entry level explicitly (e.g.
Vogt→L1, Reiter→L3) ahead of the still-open person_lens
depth-appetite axis (2.2/2.3) landing — see ``generate.resolve_depth_default``.

Two modes:
  * ``as_variant=True``  → save a NEW doc linked to the source via the
    ``variant_of`` column (migration 073). The source is untouched.
  * ``as_variant=False`` → edit the doc in place (same id).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.retailor")

_DENSITY = {
    "concise": "DENSITY: concise — tighten every rung body to its essential "
    "claim; cut filler, merge redundant points, prefer short phrases over "
    "sentences. Reduce word count without dropping a distinct point.",
    "balanced": "DENSITY: balanced — keep the current level of detail; refine "
    "wording only where it sharpens clarity.",
    "detailed": "DENSITY: detailed — expand terse rung bodies into fuller, "
    "self-contained statements; add the implied context a reader would need. "
    "Do NOT add new facets — only enrich existing rung text.",
}

_JARGON = {
    "plain": "JARGON: plain — replace domain/technical terms with "
    "plain-language equivalents; spell out acronyms on first use; assume a "
    "non-expert reader.",
    "balanced": "JARGON: balanced — keep common domain terms, gloss the rarer "
    "ones; assume a literate but non-specialist reader.",
    "technical": "JARGON: technical — use precise domain terminology and "
    "accepted acronyms; assume an expert reader who prefers signal over "
    "hand-holding.",
}

from okuro.prism.rungs import RUNGS as _DEPTHS


def _deck_rung_text(doc: dict[str, Any]) -> str:
    """Concatenate every facet's rung bodies (+ callouts) into one string — the
    text an entailment gate compares before vs after adaptation."""
    parts: list[str] = []
    for facet in (doc.get("facets") or {}).values():
        for rc in (facet.get("rungs") or {}).values():
            if not isinstance(rc, dict):
                continue
            body = (rc.get("body") or "").strip()
            if body:
                parts.append(body)
            for c in (rc.get("callouts") or []):
                t = (c.get("text") if isinstance(c, dict) else "") or ""
                if t.strip():
                    parts.append(t.strip())
    return "\n\n".join(parts)


def _axis_directives(
    density: Optional[str], jargon: Optional[str], language: Optional[str]
) -> list[str]:
    """Validated directive fragments for the explicit axes (skip unset/unknown)."""
    out: list[str] = []
    if density:
        d = density.strip().lower()
        if d in _DENSITY:
            out.append(_DENSITY[d])
    if jargon:
        j = jargon.strip().lower()
        if j in _JARGON:
            out.append(_JARGON[j])
    if language:
        lang = language.strip()
        if lang:
            out.append(
                f"LANGUAGE: translate ALL human-readable rung text into '{lang}' "
                "(BCP-47). Translate every rung's body/callouts naturally and "
                "idiomatically. Do NOT translate facet ids, mermaid specs, or "
                "any structural field — only the visible text."
            )
    return out


_BLOCK_TRANSLATE_SYSTEM = (
    "You translate okuro·prism visual-module JSON into a target language. You are given a JSON "
    "ARRAY of blocks. Translate ONLY the human-readable string VALUES a reader sees: title, "
    "label, text, md, body, headline, kicker, caption, note, sub, detail; table headers and row "
    "cells; tabs[].md; nodes[].label and nodes[].summary; edges[].label; steps[].title/detail; "
    "options[].title/pros/cons/note; cards[].title/body/tag; spec cards[].title and each "
    "rows[].v; matrix columns and rows[].label and text cells; risk items[].title/mitigation; "
    "decisionrecord question/decision/options[].label/rejected_because; evidence claims[].text; "
    "statement and quote text.\n"
    "PRESERVE EXACTLY — never translate, never drop: every JSON KEY; the 'type' value; enum "
    "tokens (chart, severity, likelihood, state, variant, trend, display, align, confidence, "
    "grade, actor); all booleans (recommended, done, spine, chosen); all NUMBERS and units; any "
    "id / from / to / key / source_ref. In a mermaid 'spec' (diagram block) translate ONLY the "
    "quoted node labels (A[\"label\"]) and edge labels — NEVER the arrows, node ids, or mermaid "
    "keywords (flowchart, graph, LR, -->). Keep the array the SAME length and each block the SAME "
    "shape. Return ONLY the translated JSON array."
)


def _translate_doc_blocks(
    doc: dict[str, Any], language: str, provider: Optional[str] = None
) -> tuple[dict[str, Any], int]:
    """Translate the VISIBLE text inside every rung's rich blocks into ``language``,
    preserving structure/ids/numbers/enums. Returns (doc, blocks_translated). The
    body/callouts wording pass handles prose; this completes the bilingual variant
    so tables, stats, graphs, and lens tabs aren't left in the source language."""
    import json
    import os

    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    lang = (language or "").strip()
    if not lang:
        return doc, 0

    count = 0
    for facet in (doc.get("facets") or {}).values():
        for rc in (facet.get("rungs") or {}).values():
            blocks = rc.get("blocks")
            if not isinstance(blocks, list) or not blocks:
                continue
            user = (
                f"TARGET LANGUAGE: {lang} (BCP-47)\n\n"
                f"BLOCKS (translate visible text only, keep structure):\n"
                f"{json.dumps(blocks, ensure_ascii=False)}\n\n"
                "Return ONLY the translated JSON array."
            )
            _prev = os.environ.get("MAX_THINKING_TOKENS")
            os.environ["MAX_THINKING_TOKENS"] = "0"
            try:
                res = invoke(
                    prompt=user, system_prompt=_BLOCK_TRANSLATE_SYSTEM,
                    provider=provider, capability="standard", timeout=240,
                )
            finally:
                if _prev is None:
                    os.environ.pop("MAX_THINKING_TOKENS", None)
                else:
                    os.environ["MAX_THINKING_TOKENS"] = _prev
            if not (isinstance(res, dict) and res.get("success")):
                continue
            try:
                data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "[", "]"))
            except (ValueError, KeyError) as exc:
                logger.warning("_translate_doc_blocks: parse failed: %s", exc)
                continue
            # Length-match guard: only replace when the shape is preserved, so a
            # bad translation never drops or reorders a rung's modules.
            if isinstance(data, list) and len(data) == len(blocks):
                rc["blocks"] = data
                count += len(data)
    return doc, count


def build_retailor_instruction(
    *,
    recipient_ident: str = "",
    lens: str = "",
    density: Optional[str] = None,
    jargon: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """Compose the natural-language re-tailor instruction handed to the edit
    agent. Always pins the edit to text-only ``set_rung`` ops with stable
    facet ids/tree shape, then layers the recipient lens and explicit axes."""
    parts: list[str] = [
        "RE-TAILOR this facet tree for a different audience. This is a "
        "WORDING pass, NOT a redesign: adjust ONLY rung bodies (and their "
        "media/callouts text) via set_rung ops. Do NOT add, remove, or "
        "reparent facets; do NOT change facet ids, kinds, or tree shape.",
    ]

    if recipient_ident or lens:
        recip = "RECIPIENT (tune wording, framing, and depth emphasis to them):"
        if recipient_ident:
            recip += f"\n  {recipient_ident}"
        if lens:
            recip += f"\n{lens}"
        parts.append(recip.strip())

    parts.extend(_axis_directives(density, jargon, language))

    parts.append(
        "Emit the SMALLEST set of set_rung ops that re-casts the text. If a "
        "rung does not need to change, omit it."
    )
    return "\n\n".join(parts)


def retailor_facets(
    doc_id: str,
    *,
    person_id: Optional[str] = None,
    depth_override: Optional[str] = None,
    density: Optional[str] = None,
    jargon: Optional[str] = None,
    language: Optional[str] = None,
    reselect_modules: bool = False,
    as_variant: bool = True,
    verify: bool = True,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Re-tailor an existing doc to a recipient + density/jargon/language/
    depth_override axes.

    Builds a re-tailor instruction (recipient lens + explicit axes) and runs
    it through the shared scoped-ops edit path (``set_rung`` only — facet ids
    and tree shape stay stable). With ``as_variant`` the result is saved as a
    NEW doc linked to the source via ``variant_of``; otherwise the doc is
    edited in place.

    Returns the saved doc detail plus an ``applied`` summary of the axes used.
    """
    from okuro.prism import get_doc

    existing = get_doc(doc_id)
    if existing is None:
        raise ValueError(f"doc '{doc_id}' not found")

    if depth_override is not None and depth_override not in _DEPTHS:
        raise ValueError(f"depth_override must be one of {_DEPTHS}")

    if not (person_id or density or jargon or language or depth_override):
        raise ValueError(
            "nothing to re-tailor — pass person_id and/or density/jargon/language/depth_override"
        )

    resolved_rung = depth_override
    if resolved_rung is None and person_id:
        from okuro.prism.generate import resolve_depth_default

        resolved_rung = resolve_depth_default(person_id)

    ident, lens = "", ""
    if person_id:
        from okuro.prism.generate import _recipient_identity

        ident = _recipient_identity(person_id)
        try:
            from okuro.peer.persons import person_lens

            lens = person_lens(person_id, context=existing.title) or ""
        except Exception as exc:  # noqa: BLE001
            logger.info("person_lens failed for %s: %s", person_id, exc)

    new_doc, op_errors, ops_applied = (existing.doc, [], 0)
    entailment_redo = False
    wants_wording_pass = bool(person_id or density or jargon or language)
    if wants_wording_pass:
        instruction = build_retailor_instruction(
            recipient_ident=ident,
            lens=lens,
            density=density,
            jargon=jargon,
            language=language,
        )

        from okuro.prism.generate import run_ops_instruction

        new_doc, op_errors, ops_applied = run_ops_instruction(
            existing.doc, instruction, provider=provider
        )

        # Orthogonality invariant — RE-VERIFY after adaptation. The re-worded deck
        # must stay a non-destructive overlay on the immutable source: it may
        # reframe / compress / translate, but must add no fact the original didn't
        # support. The pre-adaptation doc already passed the build faithfulness gate,
        # so it IS the trusted ground truth. On a drift, redo the wording pass once
        # with the added facts named; never hard-fail a retailor over the gate.
        if verify:
            try:
                from okuro.prism.critic import critique_entailment

                source_text = _deck_rung_text(existing.doc)
                chk = critique_entailment(source_text, _deck_rung_text(new_doc), provider=provider)
                if not chk.get("passed") and chk.get("added"):
                    added = "; ".join((a.get("fact") or "") for a in chk["added"][:5])
                    logger.warning(
                        "retailor entailment redo (%d added fact(s)): %s",
                        len(chk["added"]), added,
                    )
                    redo = build_retailor_instruction(
                        recipient_ident=ident, lens=lens,
                        density=density, jargon=jargon, language=language,
                    ) + (
                        "\n\nFIDELITY — your previous re-tailor INVENTED facts the source "
                        "never stated: " + added + ". Re-tailor again from the SAME source, "
                        "re-wording ONLY; state no number, name, or claim the source lacks."
                    )
                    new_doc, op_errors, ops_applied = run_ops_instruction(
                        existing.doc, redo, provider=provider
                    )
                    entailment_redo = True
            except Exception as exc:  # noqa: BLE001 — the gate never blocks a retailor
                logger.warning("retailor entailment gate skipped: %s", exc)

    new_doc = dict(new_doc)

    # Module/layout RE-SELECTION: beyond re-wording, re-pick WHICH visual modules
    # each rung uses for the new recipient's cognition (an engineer meets code/
    # graph where a CEO meets stat/timeline). Re-runs generation's pass-2 block
    # enrichment with the recipient's audience policy; layout recomposes on save.
    reselected = False
    if reselect_modules and person_id:
        try:
            from okuro.peer.cognitive_profile import cognitive_profile_for_llm
            from okuro.prism.audience import module_policy, policy_prompt_clause
            from okuro.prism.generate import _enrich_blocks

            policy_clause = policy_prompt_clause(module_policy(cognitive_profile_for_llm(person_id)))
            n = _enrich_blocks(
                new_doc, existing.title, provider=provider,
                policy_clause=policy_clause, capability="standard",
            )
            reselected = n > 0
            logger.info("retailor re-selected modules for %s: %d blocks", person_id, n)
        except Exception as exc:  # noqa: BLE001 — never fail a retailor over re-selection
            logger.warning("retailor module re-selection failed (keeping modules): %s", exc)

    # Block translation: the wording pass above only re-casts rung BODY/callouts.
    # For a real bilingual variant the modules (tables, stats, graph labels, lens
    # tabs) must translate too — otherwise a 'German' deck keeps English blocks.
    blocks_translated = 0
    if language:
        try:
            new_doc, blocks_translated = _translate_doc_blocks(new_doc, language, provider=provider)
            logger.info("retailor translated %d block(s) to %s", blocks_translated, language)
        except Exception as exc:  # noqa: BLE001 — never fail a retailor over block translation
            logger.warning("retailor block translation failed (blocks left in source language): %s", exc)

    if resolved_rung:
        new_doc["entry_rung"] = resolved_rung

    applied = {
        "person_id": person_id or None,
        "depth_override": depth_override or None,
        "density": (density or None),
        "jargon": (jargon or None),
        "language": (language or None),
        "reselect_modules": reselected,
        "blocks_translated": blocks_translated,
        "entailment_redo": entailment_redo,
    }

    from okuro.prism import save_doc

    if as_variant:
        suffix = ident or person_id or language or density or jargon or depth_override or "variant"
        title = f"{existing.title} — {suffix}"
        new_doc.pop("id", None)
        saved = save_doc(
            id=None,
            title=title,
            brand_id=existing.brand_id,
            variant_of=existing.id,
            doc=new_doc,
            origin="retailor",
            allow_empty=True,
        )
    else:
        saved = save_doc(
            id=existing.id,
            title=new_doc.get("title") or existing.title,
            brand_id=existing.brand_id,
            variant_of=existing.variant_of,
            doc=new_doc,
            origin="retailor",
            allow_empty=True,
        )

    detail = saved.to_detail()
    detail["op_errors"] = op_errors
    detail["ops_applied"] = ops_applied
    detail["applied"] = applied
    detail["as_variant"] = as_variant
    from okuro.prism.generate import _brand_health

    brand_warnings = _brand_health(existing.brand_id)
    if brand_warnings:
        detail["brand_warnings"] = brand_warnings
    return detail
