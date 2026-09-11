# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-slides recipient re-tailoring — re-cast an existing deck to a
#   recipient + density/jargon/language axes, in place or as a linked variant.
# index:
#   imports
#   _DENSITY / _JARGON
#   def _axis_directives
#   def build_retailor_instruction
#   def retailor_deck
#   def list_variants
# AGENT_HEADER_END -->
"""okuro-slides recipient-tailoring — the differentiator.

Re-casts an *existing* deck (its narrative + layout already decided) to a new
recipient and/or explicit communication axes (density / jargon / language),
WITHOUT regenerating it. The re-tailor is expressed as a natural-language
instruction and run through the SAME scoped-ops edit path ``edit_deck`` uses
(``update_element`` patches only — geometry and element ids stay stable), so an
open canvas live-updates and the deck's structure is preserved.

Two modes:
  * ``as_variant=True``  → save a NEW deck linked to the source via
    ``deck["variantOf"] = <source_id>`` + ``deck["variantMeta"]``. The source is
    untouched; the user can switch between audience variants.
  * ``as_variant=False`` → edit the deck in place (same id).

Variants are modelled entirely inside the deck JSON blob (``variantOf`` /
``variantMeta``) — no new columns. ``list_variants`` walks decks to collect a
source + its variants for the UI's variant tabs.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("okuro.slides.retailor")

# Explicit communication axes → directive fragments fed to the edit agent.
_DENSITY = {
    "concise": "DENSITY: concise — tighten every line to its essential claim; "
    "cut filler, merge redundant bullets, prefer short phrases over sentences. "
    "Reduce word count without dropping a distinct point.",
    "balanced": "DENSITY: balanced — keep the current level of detail; refine "
    "wording only where it sharpens clarity.",
    "detailed": "DENSITY: detailed — expand terse points into fuller, "
    "self-contained statements; add the implied context a reader would need. "
    "Do NOT add new slides or elements — only enrich existing text.",
}

_JARGON = {
    "plain": "JARGON: plain — replace domain/technical terms with plain-language "
    "equivalents; spell out acronyms on first use; assume a non-expert reader.",
    "balanced": "JARGON: balanced — keep common domain terms, gloss the rarer "
    "ones; assume a literate but non-specialist reader.",
    "technical": "JARGON: technical — use precise domain terminology and "
    "accepted acronyms; assume an expert reader who prefers signal over "
    "hand-holding.",
}

_DENSITY_VALUES = tuple(_DENSITY)
_JARGON_VALUES = tuple(_JARGON)

# Density also sets the variant's default reveal depth (deck.maxDepth): a terser
# audience starts with fewer progressive-disclosure levels shown, a detailed one
# with all of them. Purely a starting hint for present mode — the wording pass
# above still runs; this just picks where the depth stepper opens.
_DENSITY_MAXDEPTH = {"concise": 2, "balanced": 3, "detailed": 4}


def density_maxdepth(density: Optional[str]) -> Optional[int]:
    """Default `maxDepth` for a density axis value, or None if unset/unknown."""
    if not density:
        return None
    return _DENSITY_MAXDEPTH.get(density.strip().lower())


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
                f"LANGUAGE: translate ALL human-readable slide text into '{lang}' "
                "(BCP-47). Translate every text element's wording — titles, body, "
                "bullets, captions, speaker notes — naturally and idiomatically. "
                "Do NOT translate element ids, urls/src, flow ids, colors, or any "
                "structural field — only the visible text."
            )
    return out


def build_retailor_instruction(
    *,
    recipient_ident: str = "",
    lens: str = "",
    density: Optional[str] = None,
    jargon: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """Compose the natural-language re-tailor instruction handed to the edit
    agent. Always pins the edit to text-only ``update_element`` ops with stable
    geometry + ids, then layers the recipient lens and explicit axes."""
    parts: list[str] = [
        "RE-TAILOR this deck for a different audience. This is a WORDING pass, "
        "NOT a redesign: adjust ONLY the text of existing elements via "
        "update_element ops (patch the 'text' field). Do NOT add, remove, "
        "reorder, or move slides or elements; do NOT change geometry "
        "(x/y/w/h/layout) or element ids; do NOT restyle colors. Keep every "
        "element id byte-for-byte stable so the variant stays diff-able against "
        "the source.",
    ]

    if recipient_ident or lens:
        recip = "RECIPIENT (tune wording, framing, and flight-level to them):"
        if recipient_ident:
            recip += f"\n  {recipient_ident}"
        if lens:
            recip += f"\n{lens}"
        parts.append(recip.strip())

    parts.extend(_axis_directives(density, jargon, language))

    parts.append(
        "Emit the SMALLEST set of update_element ops that re-casts the text. "
        "If a text element does not need to change, omit it."
    )
    return "\n\n".join(parts)


def retailor_deck(
    deck_id: str,
    *,
    person_id: Optional[str] = None,
    density: Optional[str] = None,
    jargon: Optional[str] = None,
    language: Optional[str] = None,
    as_variant: bool = True,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Re-tailor an existing deck to a recipient + density/jargon/language axes.

    Builds a re-tailor instruction (recipient lens + explicit axes) and runs it
    through the shared scoped-ops edit path (``update_element`` only — geometry
    and ids stay stable). With ``as_variant`` the result is saved as a NEW deck
    linked to the source (``variantOf`` + ``variantMeta``); otherwise the deck is
    edited in place.

    Returns the saved deck detail plus an ``applied`` summary of the axes used.
    """
    from okuro.slides import get_deck

    doc = get_deck(deck_id)
    if doc is None:
        raise ValueError(f"deck '{deck_id}' not found")

    if not (person_id or density or jargon or language):
        raise ValueError("nothing to re-tailor — pass person_id and/or density/jargon/language")

    # Recipient lens (reuse the generator's identity + person_lens projection).
    ident, lens = "", ""
    if person_id:
        from okuro.slides.generate import _recipient_identity

        ident = _recipient_identity(person_id)
        try:
            from okuro.peer.persons import person_lens

            lens = person_lens(person_id, context=doc.title) or ""
        except Exception as exc:  # noqa: BLE001
            logger.info("person_lens failed for %s: %s", person_id, exc)

    instruction = build_retailor_instruction(
        recipient_ident=ident,
        lens=lens,
        density=density,
        jargon=jargon,
        language=language,
    )

    from okuro.slides.generate import run_ops_instruction

    # Run the instruction → ops → apply, but DON'T let the helper persist when we
    # need a fresh variant id (we save below with the variant metadata attached).
    new_deck, op_errors, ops_applied = run_ops_instruction(
        doc.deck, instruction, provider=provider
    )

    max_depth = density_maxdepth(density)
    applied = {
        "person_id": person_id or None,
        "density": (density or None),
        "jargon": (jargon or None),
        "language": (language or None),
        "maxDepth": max_depth,
    }

    from okuro.slides import save_deck

    if as_variant:
        meta = {k: v for k, v in applied.items() if v}
        new_deck = dict(new_deck)
        new_deck["variantOf"] = doc.id
        new_deck["variantMeta"] = meta
        if max_depth is not None:
            new_deck["maxDepth"] = max_depth
        if person_id:
            new_deck["recipient"] = person_id
        suffix = ident or person_id or language or density or jargon or "variant"
        title = f"{doc.title} — {suffix}"
        # New id: drop the source id so save_deck mints a fresh slug from title.
        new_deck.pop("id", None)
        saved = save_deck(id=None, title=title, deck=new_deck, origin="retailor", allow_empty=True)
    else:
        if person_id or max_depth is not None:
            new_deck = dict(new_deck)
        if person_id:
            new_deck["recipient"] = person_id
        if max_depth is not None:
            new_deck["maxDepth"] = max_depth
        saved = save_deck(
            id=doc.id,
            title=new_deck.get("title") or doc.title,
            deck=new_deck,
            origin="retailor",
            allow_empty=True,
        )

    detail = saved.to_detail()
    detail["op_errors"] = op_errors
    detail["ops_applied"] = ops_applied
    detail["applied"] = applied
    detail["as_variant"] = as_variant
    return detail


def list_variants(source_id: str) -> dict[str, Any]:
    """Return the source deck + every deck whose ``variantOf == source_id``.

    If ``source_id`` is itself a variant, resolves to its own ``variantOf`` first
    so callers can pass either the source or any variant. Loads each deck's JSON
    to read ``variantOf`` — acceptable at single-user scale.

    Shape: ``{"source": <summary|None>, "variants": [<summary+variantMeta>...]}``
    where each entry includes ``variantOf`` and ``variantMeta`` for UI labels.
    """
    from okuro.slides import get_deck, list_decks

    root_doc = get_deck(source_id)
    if root_doc is not None:
        vof = root_doc.deck.get("variantOf")
        if isinstance(vof, str) and vof:
            source_id = vof
            root_doc = get_deck(source_id) or root_doc

    def _entry(doc) -> dict[str, Any]:
        s = doc.to_summary()
        s["variantOf"] = doc.deck.get("variantOf")
        s["variantMeta"] = doc.deck.get("variantMeta") or {}
        s["recipient"] = doc.deck.get("recipient")
        return s

    source = _entry(root_doc) if root_doc is not None else None

    variants: list[dict[str, Any]] = []
    for summ in list_decks():
        if summ.id == source_id:
            continue
        full = get_deck(summ.id)
        if full is None:
            continue
        if full.deck.get("variantOf") == source_id:
            variants.append(_entry(full))

    return {"source": source, "variants": variants, "source_id": source_id}
