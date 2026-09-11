# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance render seam — map ONE PCO + an audience + a brand onto any
#   media backend. prism/slides adapters today; website/hubspot/flow later.
# index:
#   def _slider_brief
#   def resolve_audience_ref
#   def _prism_adapter
#   def _slides_adapter
#   MEDIA_ADAPTERS
#   def render
#   def render_matrix
# AGENT_HEADER_END -->
"""The render seam: ``render(pco, audience, brand, media)``.

CONTENT (PCO) + STRUCTURE (audience lens) + PAINT (brand) → deliverable.
Adapters are thin: they map the three axes onto a backend's native call.
prism and slides already accept ``(topic, person_id, brand_id, provider)``,
so the mapping is ``topic = pco.to_content_brief(audience_brief)`` — the
provenance-annotated content — plus the resolved audience + brand.

``dry_run=True`` returns the planned backend call WITHOUT invoking the LLM —
so the seam can be verified cheaply. ``render_matrix`` fans one PCO out across
[audiences × media].
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .pco import PCO
from .projection import project_pco

_DEFAULT_BRAND = "okuro"


def _slider_brief(profile: dict[str, Any], *, generic: bool, role_class: str | None) -> str:
    """Synthesize an audience STRUCTURE brief from merged cognitive sliders.

    Takes the whole profile, not a bare slider dict, so per-axis confidence
    travels with the values — an unmeasured axis renders as ``(prior)``
    rather than as a stated preference (cognitive_profile, DP10).
    """
    from okuro.peer.cognitive_profile import format_sliders_for_prompt

    axes = format_sliders_for_prompt(profile)
    if not axes:
        return ""
    head = ("AUDIENCE (fit depth, framing and jargon to this cognitive profile; "
            "do NOT change the facts, only how they are presented): ")
    tag = ""
    if generic:
        tag = (f"  [GENERIC archetype '{role_class or 'default'}' — no profiled "
               "members; produce a general deck for the group and say so.]")
    return head + axes + tag


def resolve_audience_ref(audience_ref: Optional[str]) -> dict[str, Any]:
    """Resolve an audience id → {person_id, brief, generic, kind}.

    ``audience_ref`` may be a person id (STRUCTURE from person_lens) or a
    target_group id (STRUCTURE from the merged group lens). None → no lens.
    """
    if not audience_ref:
        return {"person_id": None, "brief": "", "generic": True, "kind": "none"}

    from okuro.db import get_db
    db = get_db()

    if db.fetchone("SELECT id FROM persons WHERE id = ?", (audience_ref,)):
        brief = ""
        try:
            from okuro.peer.persons import person_lens
            brief = person_lens(audience_ref) or ""
        except Exception:
            pass
        return {"person_id": audience_ref, "brief": brief,
                "generic": False, "kind": "person"}

    if db.fetchone("SELECT id FROM target_groups WHERE id = ?", (audience_ref,)):
        from okuro.peer.target_groups import resolve_audience
        aud = resolve_audience(audience_ref)
        brief = _slider_brief(aud,
                              generic=aud.get("generic", True),
                              role_class=aud.get("role_class"))
        return {"person_id": None, "brief": brief,
                "generic": aud.get("generic", True), "kind": "group",
                "members": aud.get("members", []),
                "sliders": aud.get("sliders") or {}}

    return {"person_id": None, "brief": "", "generic": True,
            "kind": "unknown", "error": f"audience not found: {audience_ref}"}


def _audience_profile(aud: dict[str, Any]) -> dict[str, Any]:
    """The PII-firewalled cognitive profile ({"sliders": {...}}) for a resolved
    audience — a person's lens or a group's merged sliders. Empty when there's
    no profiled audience, which makes ``audience_kind`` return None → no
    projection (neutral path). Never raises: the firewall must not break render.
    """
    pid = aud.get("person_id")
    if pid:
        try:
            from okuro.peer.cognitive_profile import cognitive_profile_for_llm
            return cognitive_profile_for_llm(pid) or {}
        except Exception:
            return {}
    sliders = aud.get("sliders") or {}
    return {"sliders": sliders} if sliders else {}


def _project_for_audience(pco: PCO, aud: dict[str, Any]) -> tuple[PCO, Optional[str]]:
    """Select & reframe the PCO's claims for the resolved audience KIND.

    Returns ``(projected_pco, kind)``. ``kind`` is None (→ PCO unchanged) when
    the audience has no cognitive profile. The classification reuses
    ``prism.audience.audience_kind`` so projection, module selection, narrative
    arc and layout all agree on WHO the reader is (one classifier, DP10).
    """
    from okuro.prism.audience import audience_kind
    kind = audience_kind(_audience_profile(aud))
    return project_pco(pco, kind), kind


# ── media adapters ───────────────────────────────────────────────────────────
# Each: (content_brief, person_id, brand_id, provider) -> backend result dict.


def _prism_adapter(content_brief: str, person_id: Optional[str],
                   brand_id: str, provider: Optional[str],
                   pco_sources: Optional[set[str]] = None) -> dict[str, Any]:
    from okuro.prism.generate import generate_doc
    # pco_sources = the PCO's real grounding refs → the generator validates the
    # source_ref it copies onto each block against this set (drops hallucinations).
    return generate_doc(content_brief, person_id=person_id, brand_id=brand_id,
                        provider=provider, valid_source_refs=pco_sources)


def _slides_adapter(content_brief: str, person_id: Optional[str],
                    brand_id: str, provider: Optional[str],
                    pco_sources: Optional[set[str]] = None) -> dict[str, Any]:
    from okuro.slides.generate import generate_deck
    return generate_deck(content_brief, person_id=person_id,
                        brand_id=brand_id, provider=provider)


def _website_adapter(content_brief: str, person_id: Optional[str],
                     brand_id: str, provider: Optional[str],
                     pco_sources: Optional[set[str]] = None) -> dict[str, Any]:
    from .website import render_website
    return render_website(content_brief, person_id, brand_id, provider)


MEDIA_ADAPTERS: dict[str, Callable[..., dict[str, Any]]] = {
    "prism": _prism_adapter,
    "slides": _slides_adapter,
    "website": _website_adapter,   # scroll-reveal HTML page (reference mechanic)
    # "hubspot":  _hubspot_adapter,  # net-new #6
    # "flow":     _flow_adapter,     # flow_designer_save
}


def render(pco: PCO, audience_ref: Optional[str] = None, *,
           brand_id: str = _DEFAULT_BRAND, media: str = "prism",
           provider: Optional[str] = None, dry_run: bool = False) -> dict[str, Any]:
    """Render ONE PCO for ONE audience into ONE medium.

    ``dry_run`` returns the planned backend call (no LLM) — for verifying the
    seam. Otherwise invokes the media adapter and returns its result.
    """
    if media not in MEDIA_ADAPTERS:
        return {"error": f"unknown media '{media}'. Have: {list(MEDIA_ADAPTERS)}"}

    aud = resolve_audience_ref(audience_ref)
    # Phase E — SELECT & reframe the claims for this reader BEFORE collapsing to
    # a brief (medium-agnostic: every adapter below inherits the fit). Neutral /
    # unprofiled audience → projected == pco (unchanged).
    projected, audience_kind = _project_for_audience(pco, aud)
    content_brief = projected.to_content_brief(aud.get("brief", ""))
    # Provenance refs come from the PROJECTED claims — a dropped claim's source
    # must not validate a block the generator no longer has grounds to write.
    pco_sources = {c.source_ref for b in projected.beats for c in b.claims if c.source_ref} or None
    call = {
        "media": media,
        "kwargs": {"topic": content_brief, "person_id": aud.get("person_id"),
                   "brand_id": brand_id, "provider": provider},
        "audience": {k: aud.get(k) for k in ("kind", "generic", "person_id")},
    }
    # Surface the projection for observability (which reader, how much survived).
    call["audience"]["audience_kind"] = audience_kind
    call["audience"]["claims"] = {
        "in": sum(len(b.claims) for b in pco.beats),
        "projected": sum(len(b.claims) for b in projected.beats),
    }
    if dry_run:
        return {"planned": call, "provenance_refs": sorted(pco_sources) if pco_sources else [], "dry_run": True}

    result = MEDIA_ADAPTERS[media](content_brief, aud.get("person_id"),
                                   brand_id, provider, pco_sources=pco_sources)
    return {"media": media, "audience": call["audience"], "result": result}


def render_matrix(pco: PCO, audiences: list[Optional[str]], media_list: list[str],
                  *, brand_id: str = _DEFAULT_BRAND, provider: Optional[str] = None,
                  dry_run: bool = False) -> list[dict[str, Any]]:
    """Fan ONE PCO out across [audiences × media] — the render matrix.

    One prepared context, many audience-fitted deliverables. This is the
    Resonance payoff: prepare once, communicate everywhere.
    """
    out: list[dict[str, Any]] = []
    for aud in audiences:
        for media in media_list:
            cell = render(pco, aud, brand_id=brand_id, media=media,
                          provider=provider, dry_run=dry_run)
            out.append({"audience_ref": aud, "media": media, **cell})
    return out
