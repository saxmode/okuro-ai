# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler STAGE 7 (compose) — the ONLY generation-time LLM
#   styling touchpoint, and it does NOT style: it MAPS a cell's sliced claims +
#   narrative spine into the NAMED SLOTS of the chosen kit archetype (per the kit
#   shape matrix). No CSS, no geometry, no new markup — output is filled-slot slide
#   JSON the A4 renderer feeds into the shipped template. ×2 A/B per slide with a
#   deterministic per-slide seed (the grid index).
# index: compose | compose_schema | _compose_cell | _doc_slide
# AGENT_HEADER_END -->
"""Stage 7 — compose: claims -> archetype slots (LLM), ×2 A/B, deterministic.

The kit owns every pixel; compose only decides WHICH claim fills WHICH slot and
in what order — a discrete mapping problem, schema-validated. Two variants (A/B)
are produced per slide in a single call, seeded by the slide's grid index so a
re-run of the same cell asks the model the same way (reproducible framing pick,
not a temperature gamble). L3 doc-view cells need no model: the authored master
doc IS the body.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from jsonschema import Draft202012Validator

from okuro.prism.compiler import archetypes as A
from okuro.prism.compiler import llm
from okuro.prism.compiler import slot_synth
from okuro.prism.compiler.ir import (
    Authored,
    ComposedDeck,
    Outline,
    PagePlan,
    Slices,
    Slide,
    tolerant,
)

logger = logging.getLogger(__name__)

_STRING = {"type": "string"}


def _slot_schema(archetype: str) -> dict[str, Any]:
    """The single-variant slots object schema for an archetype.

    Content slots use the registry ``fill_schema``; narrative slots
    (eyebrow/title/lede) are plain strings; ``title`` is always required.
    Decorative/optional slots (A4 renders them guarded) are EXCLUDED — a live
    model routinely fills them with prose (e.g. ask-cta ``arch``), and validating
    them exhausts the bounded re-ask over content the deck does not need; they
    pass through unvalidated and are kept-if-valid / dropped-if-junk by
    ``sanitize_optional_slots``. Load-bearing REQUIRED slots stay strict.
    """
    slots = A.slots_of(archetype)
    fills = A.fill_schema(archetype)
    optional = A.optional_slots(archetype)
    props: dict[str, Any] = {}
    required: list[str] = []
    for s in slots:
        if s in fills:
            if s in optional:
                continue
            props[s] = fills[s]
            required.append(s)
        else:
            props[s] = _STRING
            if s == "title":
                required.append(s)
    return {"type": "object", "required": required or ["title"], "properties": props}


def compose_schema(archetype: str) -> dict[str, Any]:
    """The two-variant schema compose must satisfy for one archetype cell:
    ``{"variants": {"A": {"slots": {...}}, "B": {"slots": {...}}}}``. Tolerant of
    extra keys (A4 reads only known slots); the kit's single-accent rule permits
    at most one ``<accent>…</accent>`` span in the title string."""
    variant = {"type": "object", "required": ["slots"],
               "properties": {"slots": _slot_schema(archetype)}}
    schema = {"type": "object", "required": ["variants"],
              "properties": {"variants": {"type": "object", "required": ["A", "B"],
                                          "properties": {"A": variant, "B": variant}}}}
    return tolerant(schema)


_SYSTEM = """You are the COMPOSE stage of the prism deck compiler. You MAP data \
claims into the NAMED SLOTS of a fixed, already-styled slide template. You do NOT \
write CSS, you do NOT choose layout, you do NOT invent data — the template's design \
is locked; you only decide which claim fills which slot and in what order.

Rules:
- Fill ONLY the slots given, using their exact schema. Every number, name, and word \
must come from the provided claims or the provided narrative text.
- title: short (aim <=22 characters of visible text) and may wrap ONE phrase in \
<accent>…</accent> to highlight it (at most one accent span). eyebrow: a short kicker.
- Produce TWO variants, A and B, that fill the SAME slots with the SAME facts but a \
different framing/ordering/emphasis (so a human can pick the stronger one). Never \
add facts to one variant that the other lacks.
Return ONLY {"variants": {"A": {"slots": {...}}, "B": {"slots": {...}}}}."""


def _auto_number_ordinals(slots: dict[str, Any], archetype: str) -> dict[str, Any]:
    """Set the archetype's ordinal field to a 1..N sequence in code — ordinals are
    derivable, never trusted from the model (which fills them with labels)."""
    spec = A.ORDINAL_SLOTS.get(archetype)
    if not spec:
        return slots
    arr_slot, field = spec
    arr = slots.get(arr_slot)
    if isinstance(arr, list):
        for i, item in enumerate(arr):
            if isinstance(item, dict):
                item[field] = i + 1
    return slots


def _strip_ordinals(data: Any, archetype: str) -> Any:
    """Drop the model's ordinal values before validation (compose auto-numbers)."""
    spec = A.ORDINAL_SLOTS.get(archetype)
    if not spec or not isinstance(data, dict):
        return data
    arr_slot, field = spec
    for variant in (data.get("variants") or {}).values():
        slots = variant.get("slots") if isinstance(variant, dict) else None
        arr = slots.get(arr_slot) if isinstance(slots, dict) else None
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict):
                    item.pop(field, None)
    return data


def sanitize_optional_slots(slots: dict[str, Any], archetype: str) -> dict[str, Any]:
    """Drop any decorative/optional slot the model filled with a non-conforming
    value (e.g. ask-cta `arch` as prose). Optional slots are excluded from the
    compose re-ask schema, so they arrive unvalidated; a valid one is kept, junk
    is dropped so it never reaches the (load-bearing) A4 renderer as a wrong type.
    Required slots are already validated by call_json and are untouched here."""
    fills = A.fill_schema(archetype)
    for slot in A.optional_slots(archetype):
        if slot not in slots:
            continue
        sub = tolerant(fills.get(slot, {}))
        if sub and list(Draft202012Validator(sub).iter_errors(slots[slot])):
            del slots[slot]
    return slots


def _doc_slide(topic_id: str, master_doc: str, title: str, claim_ids: list[str]) -> Slide:
    return Slide(topic_id=topic_id, level=3, archetype="doc", variant="A",
                 slots={"title": title, "body": master_doc}, claim_ids=list(claim_ids))


def _compose_cell(
    *, topic_id: str, topic_title: str, level: int, archetype: str,
    claim_ids: list[str], prose: str, claims_by_id: dict[str, dict[str, Any]],
    seed: int, provider: Optional[str], invoke_fn,
) -> list[Slide]:
    claims = [claims_by_id[c] for c in claim_ids if c in claims_by_id]
    claim_view = [{"id": c["id"], "shape": c["shape"], "statement": c["statement"],
                   "fields": c["fields"]} for c in claims]
    slot_schema = _slot_schema(archetype)
    # (a) show the model the exact required-slot structure it must return, filled
    #     with placeholders — the strongest lever against a missing nested array
    #     (e.g. ask-cta ask.items). One filled A-variant example.
    example = {"variants": {"A": {"slots": slot_synth.exemplar(slot_schema)}}}
    user = (
        f"SLIDE seed={seed}\nTOPIC: {topic_title}\nLEVEL: L{level}\n"
        f"ARCHETYPE: {archetype}\nSLOTS: {', '.join(A.slots_of(archetype))}\n\n"
        f"NARRATIVE (use for eyebrow/title/lede/callout text): {prose!r}\n\n"
        f"CLAIMS TO PLACE ({len(claim_view)}):\n" + llm.dumps(claim_view) +
        "\n\nREQUIRED SLOT STRUCTURE — return exactly these keys; every array shown "
        "must be present and NON-EMPTY, filled with the real claim data:\n"
        + llm.dumps(example) +
        "\n\nMap the claims into the slots as two variants A and B. Return ONLY the JSON."
    )
    # Ordinals (e.g. flow-sequence step numbers) are derivable — strip them from
    # the model's reply BEFORE validation so a label-in-ordinal never fails, then
    # auto-number in code below.
    strip = (lambda d: _strip_ordinals(d, archetype)) if archetype in A.ORDINAL_SLOTS else None
    try:
        data = llm.call_json(
            system_prompt=_SYSTEM, user_prompt=user, schema=compose_schema(archetype),
            stage=f"compose[{topic_id}/L{level}]", provider=provider, capability="standard",
            timeout=300, thinking_tokens=3000, reask_example=example, postprocess=strip,
            invoke_fn=invoke_fn,
        )
        variants = {label: sanitize_optional_slots(data["variants"][label]["slots"], archetype)
                    for label in ("A", "B")}
        synthesized = False
    except llm.LLMError as exc:
        # (c) fail-soft-per-cell: the model never produced a valid required-slot
        # fill. Rather than fail the WHOLE deck on one cell, synthesize a minimal
        # schema-valid fill grounded in this cell's claims and FLAG it so the
        # critic/A5 reviews it. The deck always completes.
        logger.warning("compose: %s/L%d fail-soft synthesize (%s)", topic_id, level, exc)
        fill = slot_synth.synthesize(slot_schema, claims, prose)
        variants = {"A": dict(fill), "B": dict(fill)}
        synthesized = True

    return [Slide(topic_id=topic_id, level=level, archetype=archetype, variant=label,
                  slots=_auto_number_ordinals(variants[label], archetype),
                  claim_ids=list(claim_ids), synthesized=synthesized)
            for label in ("A", "B")]


def compose(
    page_plan: PagePlan,
    slices: Slices,
    authored: Authored,
    outline: Outline,
    mine_out: dict[str, Any],
    *,
    brand: str = "",
    provider: Optional[str] = None,
    invoke_fn=None,
) -> ComposedDeck:
    """Fill every grid cell's slots (LLM), ×2 A/B, into a ComposedDeck."""
    claims_by_id = {c["id"]: c for c in mine_out.get("claims", [])}
    titles = {t.id: t.title for t in outline.topics}
    master = {t.topic_id: t.master_doc for t in authored.topics}

    slides: list[Slide] = []
    # Deterministic seed = the cell's ordinal in a stable grid traversal.
    ordered = sorted(page_plan.cells, key=lambda c: (page_plan.topic_order.index(c.topic_id), c.level))
    for seed, cell in enumerate(ordered):
        sliced = slices.cell(cell.topic_id, cell.level)
        if cell.archetype == "doc":
            slides.append(_doc_slide(cell.topic_id, master.get(cell.topic_id, ""),
                                     titles.get(cell.topic_id, ""),
                                     sliced.claim_ids if sliced else []))
            continue
        if sliced is None or not sliced.claim_ids:
            logger.info("compose: skipping empty cell %s/L%d", cell.topic_id, cell.level)
            continue
        slides.extend(_compose_cell(
            topic_id=cell.topic_id, topic_title=titles.get(cell.topic_id, ""),
            level=cell.level, archetype=cell.archetype, claim_ids=sliced.claim_ids,
            prose=sliced.prose, claims_by_id=claims_by_id, seed=seed,
            provider=provider, invoke_fn=invoke_fn,
        ))
    return ComposedDeck(slides=slides, brand=brand, topic_order=list(page_plan.topic_order))
