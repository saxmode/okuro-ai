# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: prism — recipient-derived DECK SLOTS (hero persona chips, hero meta
#   pills) with D-P1's attribution split enforced at the source. Shared by the
#   compiler and the workflow so neither engine imports the other.
# index: personas_from_audience | hero_meta | recipient_names | find_attribution
# AGENT_HEADER_END -->
"""Turn a resolved audience into the deck's recipient-shaped slots.

D-P1 (the owner, 2026-08-02) is the rule these functions exist to make
structural rather than remembered:

    Tailoring steers SILENTLY — topic ordering, emphasis, entry depth,
    component choice. Content NEVER names an audience member. A per-deck
    toggle `mention_audience_members` (default NO) exists for relationships
    where explicit mention is safe; the user judges that, not the pipeline.

"Robin would like this" on a board slide is a reputational hazard when it is
wrong, so the default has to be the safe one and the unsafe one has to be
asked for. Naming lives behind one boolean, in one place, and
:func:`find_attribution` is the lint that proves the boolean is what suppresses
it — a gate that lands a phase after the thing it gates is not a gate.

Previously these builders lived in ``compiler/pipeline.py`` and named the
reader unconditionally. They moved here rather than being cross-imported: the
workflow deliberately replaced the compiler's staged engine, and reaching into
it for two helpers would re-couple exactly what was separated.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_PERSONA_MARKS = ("a", "b", "c")
#: How many persona chips the hero has room for.
_MAX_CHIPS = 3


def _anon_label(index: int) -> str:
    """A stable, non-identifying chip label. Deliberately NOT the role or the
    brand: on a three-person board "CTO" names someone as surely as their name
    does, and D-P1 is about who the reader can be identified as, not about the
    literal string."""
    return f"Reader {chr(ord('A') + index)}"


def personas_from_audience(
    audience: Any,
    *,
    mention_names: bool = False,
) -> list[dict[str, str]]:
    """Hero persona chips ``{mark, name, lens}`` from the resolved recipients.

    ``mention_names=False`` (the default, D-P1) yields chips carrying only the
    mark and the ANONYMIZED cognitive lens — the projection that already passed
    the PII firewall — under a generic "Reader A/B/C" label. Role and brand are
    withheld too, for the reason in :func:`_anon_label`.

    ``mention_names=True`` is the opt-in the deck owner sets when they have
    judged the relationship safe, and reproduces the compiler's original chip.
    """
    out: list[dict[str, str]] = []
    for i, r in enumerate(getattr(audience, "recipients", [])[:_MAX_CHIPS]):
        lens_text = (getattr(r, "lens", "") or "")[:40]
        if mention_names:
            lens = " · ".join(x for x in (r.role, r.brand) if x) or lens_text
            name = r.name or f"Reader {i + 1}"
        else:
            lens = lens_text or "audience"
            name = _anon_label(i)
        out.append({"mark": _PERSONA_MARKS[i], "name": name, "lens": lens or "audience"})
    return out


def hero_meta(family: str, audience: Any, brand: str) -> list[dict[str, str]]:
    """Hero meta pills: grid family, audience shape, brand.

    Name-free by construction — every value is a property of the DECK, not of a
    person — so this wires unconditionally, toggle or no toggle.
    """
    meta: list[dict[str, str]] = []
    if family:
        meta.append({"label": "family", "value": family})
    meta.append({"label": "audience",
                 "value": "composite" if getattr(audience, "composite", False) else "single"})
    if brand:
        meta.append({"label": "brand", "value": brand})
    return meta


def recipient_names(audience: Any) -> list[str]:
    """Every name the deck must not say when the toggle is off — including the
    first-name and last-name parts, because "Robin" alone is an attribution."""
    names: set[str] = set()
    for r in getattr(audience, "recipients", []) or []:
        full = (getattr(r, "name", "") or "").strip()
        if not full:
            continue
        names.add(full)
        names.update(p for p in full.split() if len(p) > 2)
    return sorted(names)


def find_attribution(doc: Any, names: list[str]) -> list[dict[str, str]]:
    """THE LINT. Every place a recipient's name reaches rendered deck content.

    Walks the whole DeckDoc rather than the slots this module produced, because
    the hazard is a name ANYWHERE a reader sees it — a hero chip, a slide title,
    a composed fragment, a lens tab label. Returns ``{where, name, excerpt}``
    per hit so a failure names the surface to fix.
    """
    if not names:
        return []
    # Longest first: regex alternation is first-match-wins, so with "Alex"
    # ahead of "Alex Reiter" a full-name hit reports only the first name and
    # the operator fixing it looks for the wrong string.
    ordered = sorted(names, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in ordered) + r")\b")
    hits: list[dict[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            m = pattern.search(node)
            if m:
                i = max(0, m.start() - 40)
                hits.append({"where": path, "name": m.group(1),
                             "excerpt": node[i:m.end() + 40].strip()})
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(doc, "")
    return hits


def audience_from_brief(brief: Optional[Any]) -> Optional[Any]:
    """Rehydrate the ``AudienceProfile`` a workflow ``RecipientBrief`` carries.

    The brief stores it as the verbatim dict the compiler's profile stage
    produced, so the people graph, the PII firewall and person x brand
    disambiguation are resolved exactly once (DP10). Returns None when there is
    no brief or no audience in it — a deck for no resolved reader is a legal
    deck, it just gets no recipient slots.
    """
    from okuro.prism.compiler.profile import AudienceProfile

    payload = getattr(brief, "audience", None) if brief is not None else None
    if not payload:
        return None
    try:
        return AudienceProfile.from_dict(payload)
    except Exception:  # noqa: BLE001 — a malformed profile costs chips, not the deck
        return None


__all__ = [
    "audience_from_brief",
    "find_attribution",
    "hero_meta",
    "personas_from_audience",
    "recipient_names",
]
