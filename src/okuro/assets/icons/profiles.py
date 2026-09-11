# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Brand-governed icon resolution — search constrained to a brand's asset_profile.
# index: imports | get_asset_profile_for_brand | resolve_icons_for_brand
# AGENT_HEADER_END -->
"""Brand → icons governance.

This is the end-to-end payoff: an agent building under a brand asks for an icon
by meaning, and gets back only icons that are allowed for that brand — scoped
to the asset_profile's ``allowed_sets``, tagged with the ``license_tier`` and
whether they are safe to ship into an EXTERNAL deliverable, and carrying the
``delivery`` method (npm-name vs inline-svg) the brand expects.

License gate (research C4 / open governance gap): permissive sets (MIT —
lucide/tabler) are ``ship_external_ok``; licensed-seat sets (e.g. Streamline)
are not, and must not be redistributed in client code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import service


def get_asset_profile_for_brand(brand_id: str) -> dict | None:
    """Return the resolved asset_profile bound to a brand's ``assets`` slot,
    or None if the brand has no assets leg."""
    from okuro.stack.registry import resolve_brand

    resolved = resolve_brand(brand_id)
    if not resolved:
        return None
    slot = resolved.get("slots", {}).get("assets")
    if not slot or not isinstance(slot, dict):
        return None
    if slot.get("missing"):
        return None
    return slot.get("data")


def resolve_icons_for_brand(
    brand_id: str,
    query: str,
    *,
    limit: int = service.DEFAULT_LIMIT,
    library_path: Path | str | None = None,
) -> dict[str, Any]:
    """Search icons for `query`, constrained + governed by the brand's asset_profile.

    Returns an envelope:
        {brand, profile_id, allowed_sets, license_tier, delivery,
         ship_external_ok, governed (bool), count, results}

    ``governed`` is False (and the search is unconstrained) when the brand has
    no assets leg — surfaced so callers don't silently treat ungoverned results
    as brand-approved.
    """
    profile = get_asset_profile_for_brand(brand_id)

    if not profile:
        out = service.search_icons(query, limit=limit, library_path=library_path)
        return {
            "brand": brand_id,
            "profile_id": None,
            "allowed_sets": None,
            "license_tier": None,
            "delivery": None,
            "ship_external_ok": None,
            "governed": False,
            "warning": f"brand '{brand_id}' has no assets leg — results are NOT brand-governed",
            "count": out["count"],
            "results": out["results"],
        }

    allowed_sets = profile.get("allowed_sets") or None
    license_tier = profile.get("license_tier")
    delivery = profile.get("delivery")
    ship_external_ok = license_tier == "permissive"

    out = service.search_icons(
        query, limit=limit, set=allowed_sets, library_path=library_path
    )
    return {
        "brand": brand_id,
        "profile_id": profile.get("id"),
        "allowed_sets": allowed_sets,
        "license_tier": license_tier,
        "delivery": delivery,
        "ship_external_ok": ship_external_ok,
        "governed": True,
        "count": out["count"],
        "results": out["results"],
    }
