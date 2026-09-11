# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler STAGE 2 (profile) — resolve 1-n named recipients to
#   a single AudienceProfile. Source order: okuro people graph (person + cognitive
#   lens + affiliations) -> optional web hook -> role archetype. PERSON×BRAND
#   DISAMBIGUATION (canonical brand model): a recipient with roles at >1 brand and
#   no brand pin returns needs_disambiguation — the stage never silently picks.
# index: profile | NeedsDisambiguation | _resolve_recipient | _active_brands
#   | _blend
# AGENT_HEADER_END -->
"""Stage 2 — profile: recipients -> AudienceProfile.

The deck's brand is NOT necessarily a recipient's lens brand (canonical model
§5): a deck styled for one brand may be read by someone whose primary hat is another.
So brand resolution is per-recipient and, when a recipient wears several hats,
AMBIGUOUS — and ambiguity is surfaced, never guessed. ``brand_pins`` lets a
re-run supply the human's choice (``{recipient_name: brand_slug}``).

Cognition is read through the PII firewall (``cognitive_profile_for_llm`` returns
the anonymized projection only); module preferences reuse ``prism.audience``
(DP10 — one systematic policy, not per-person special cases). A composite of
several recipients takes the SAFEST envelope: min depth ceiling, min jargon
tolerance, union of avoided modules, prefer = modules no recipient avoids.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from okuro.prism.compiler.ir import AudienceProfile, Recipient

logger = logging.getLogger(__name__)

_JARGON_RANK = {"layman": 0, "low": 0, "business": 1, "medium": 1, "expert": 2, "high": 2}

# Depth ceiling per audience kind, on the L1..L4 rung scale (prism.rungs).
# The legacy map capped at 3 on an implicit 1..3 scale, so a technical
# audience could never reach L4 (full doc) through this path — the deepest
# level the deck can render was unreachable for the readers most likely to
# want it. Relative order is preserved; only the ceiling moves.
_ARCHETYPE_DEPTH = {"technical": 4, "board": 2, "exec": 2, "general": 3}
_ARCHETYPE_DEPTH_LEGACY = {"technical": 3, "board": 1, "exec": 1, "general": 2}


class NeedsDisambiguation(Exception):
    """Raised when >=1 recipient's brand context is ambiguous and unpinned."""

    def __init__(self, options: list[dict[str, Any]]):
        self.options = options
        super().__init__(
            "recipient brand context is ambiguous; pin a brand per recipient: "
            + "; ".join(f"{o['recipient']} -> one of {[b['brand'] for b in o['brands']]}"
                        for o in options)
        )


def _slug(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in (name or "")).strip("-")


def _active_brands(person_id: str) -> list[dict[str, str]]:
    """Active affiliations for a person as [{brand, company, role}] (brand=slug)."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """SELECT a.role, c.name AS company, c.id AS company_id
           FROM affiliations a JOIN companies c ON c.id = a.company_id
           WHERE a.person_id = ? AND a.status = 'active'
           ORDER BY a.is_primary DESC""",
        (person_id,),
    )
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        brand = _slug(r["company"])
        if brand in seen:
            continue
        seen.add(brand)
        out.append({"brand": brand, "company": r["company"], "role": r["role"] or ""})
    return out


def _person_row(name_or_id: str) -> Optional[dict[str, Any]]:
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM persons WHERE id = ? AND active = 1", (name_or_id,))
    if not row:
        row = db.fetchone(
            "SELECT * FROM persons WHERE LOWER(display_name) LIKE ? AND active = 1",
            (f"%{name_or_id.lower()}%",),
        )
    return dict(row) if row else None


def _resolve_recipient(
    name: str,
    *,
    brand_pin: str = "",
    web_fn: Optional[Callable[[str], dict[str, Any]]] = None,
) -> tuple[Optional[Recipient], Optional[dict[str, Any]]]:
    """Resolve one recipient. Returns (Recipient, None) on success, or
    (None, disambig-option) when the person's brand context is ambiguous."""
    from okuro.peer.cognitive_profile import cognitive_profile_for_llm

    row = _person_row(name)
    if row:
        pid = row["id"]
        brands = _active_brands(pid)
        # Fall back to the person's own `organization` column if no affiliations.
        if not brands and row.get("organization"):
            brands = [{"brand": _slug(row["organization"]), "company": row["organization"],
                       "role": row.get("role") or ""}]

        if len(brands) > 1 and not brand_pin:
            return None, {"recipient": row["display_name"], "person_id": pid, "brands": brands}

        chosen = None
        if brand_pin:
            chosen = next((b for b in brands if b["brand"] == _slug(brand_pin)), None)
        if chosen is None:
            chosen = brands[0] if brands else {"brand": "", "company": "", "role": row.get("role") or ""}

        cog = cognitive_profile_for_llm(pid) or {}
        lens = _lens_text(cog)
        return Recipient(
            name=row["display_name"], person_id=pid, brand=chosen["brand"],
            role=chosen["role"] or (row.get("role") or ""), lens=lens,
            sliders=cog.get("sliders") or {}, source="people-graph",
        ), None

    # Not in the people graph — optional web hook, then role archetype.
    if web_fn is not None:
        try:
            w = web_fn(name) or {}
            if w:
                return Recipient(
                    name=name, brand=_slug(w.get("brand", "")), role=w.get("role", ""),
                    sliders=w.get("sliders") or {}, source="web",
                ), None
        except Exception as exc:  # web is best-effort; never fatal
            logger.warning("profile: web_fn failed for %s: %s", name, exc)

    sliders = _archetype_sliders(name)
    return Recipient(name=name, role=name, sliders=sliders, source="archetype"), None


def _lens_text(cog: dict[str, Any]) -> str:
    bits = []
    for k in ("relation_archetype", "jargon_tolerance", "decision_style", "attention_span"):
        v = cog.get(k)
        if v and v != "unknown":
            bits.append(f"{k}={v}")
    return "; ".join(bits)


def _archetype_sliders(role_query: str) -> dict[str, int]:
    """Slider vector for a recipient who is not in the people graph.

    This read ``resolve_preset(...)["sliders"]``, and resolve_preset returns
    ``{role_id, label, domain, description, person_preset}`` — there is no
    top-level ``sliders`` key, and ``person_preset.cognitive`` carries the
    legacy free-text shape, not a vector. So the fallback returned {} for
    EVERY unknown recipient: all modules scored 0, audience_kind came back
    None, and every off-graph reader silently collapsed to "general".

    ``role_default_sliders`` is the function that actually maps a role
    string to a vector — it is what the firewall itself uses to seed an
    unprofiled person (cognitive_profile), and it fills all 17 axes.
    """
    try:
        from okuro.peer.cognitive_profile import role_default_sliders
        return role_default_sliders(role_query) or {}
    except Exception:
        return {}


def _blend(recipients: list[Recipient]) -> dict[str, Any]:
    """Safest-envelope aggregation of module policy + depth + jargon."""
    from okuro.prism.audience import audience_kind, module_policy

    from okuro.peer.flags import people_strict_enabled, warn_lenient
    from okuro.prism.rungs import RUNGS, depth_to_rung

    if people_strict_enabled():
        depth_map, ceiling_start = _ARCHETYPE_DEPTH, len(RUNGS)
    else:
        warn_lenient(
            "prism.compiler.profile.depth_ceiling",
            "depth capped at 3 on the legacy scale — L4 is unreachable",
        )
        depth_map, ceiling_start = _ARCHETYPE_DEPTH_LEGACY, 3

    prefer_sets, avoid_union = [], set()
    depth_ceiling, jargon_rank = ceiling_start, 2
    for r in recipients:
        prof = {"sliders": r.sliders, "jargon_tolerance": None}
        pol = module_policy(prof)
        prefer_sets.append(set(pol.get("prefer") or []))
        avoid_union |= set(pol.get("avoid") or [])
        kind = audience_kind(prof) or "general"
        depth_ceiling = min(depth_ceiling, depth_map.get(kind, depth_map["general"]))
        jargon_rank = min(jargon_rank, _slider_jargon(r.sliders))
    prefer = set.intersection(*prefer_sets) if prefer_sets else set()
    prefer -= avoid_union
    jargon = {0: "low", 1: "medium", 2: "high"}[jargon_rank]
    return {
        "prefer_modules": sorted(prefer),
        "avoid_modules": sorted(avoid_union),
        "depth_ceiling": depth_ceiling,
        # The same ceiling in the OTHER namespace. The int and the "L1".."L4"
        # string have carried this name in disjoint halves of the pipeline
        # with no conversion between them; emitting both, from one function,
        # is what makes them one namespace instead of two.
        "depth_ceiling_rung": depth_to_rung(depth_ceiling),
        "jargon_tolerance": jargon,
    }


def _slider_jargon(sliders: dict[str, Any]) -> int:
    """Jargon tolerance rank (0=low, 1=medium, 2=high) from the jargon axis.

    Read ``information_depth`` — a different axis entirely — while a
    dedicated ``jargon`` axis exists, is one of the few axes real people
    actually carry, and drives audience_kind, group_policy and the whole
    expertise-reversal design. The compiler's jargon envelope simply
    ignored it. Behind people.strict; the OFF path keeps the old proxy.

    The polarity also differs between the two axes, which is why this is a
    rewrite and not a key swap: jargon runs 1=layman .. 5=expert, so HIGH
    means more jargon, whereas information_depth 1=detailed was being read
    as high tolerance.
    """
    from okuro.peer.flags import people_strict_enabled, warn_lenient

    if not people_strict_enabled():
        warn_lenient(
            "prism.compiler.profile.jargon",
            "jargon inferred from information_depth; the jargon axis is ignored",
        )
        v = sliders.get("information_depth")
        if v is None:
            return 1
        try:
            return 2 if float(v) <= 2 else (0 if float(v) >= 4 else 1)
        except (TypeError, ValueError):
            return 1

    v = sliders.get("jargon")
    if v is None:
        return 1
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 1
    return 2 if f >= 4 else (0 if f <= 2 else 1)


def profile(
    recipients: list[str],
    *,
    brand_pins: Optional[dict[str, str]] = None,
    web_fn: Optional[Callable[[str], dict[str, Any]]] = None,
) -> AudienceProfile:
    """Resolve recipients to one AudienceProfile.

    Raises ``NeedsDisambiguation`` if any recipient wears multiple brand hats and
    no pin is supplied. On a re-run pass ``brand_pins={name: brand_slug}``.
    """
    if not recipients:
        raise ValueError("at least one recipient required")
    brand_pins = {k.lower(): v for k, v in (brand_pins or {}).items()}

    resolved: list[Recipient] = []
    needs: list[dict[str, Any]] = []
    for name in recipients:
        pin = brand_pins.get(name.lower(), "")
        r, ambiguous = _resolve_recipient(name, brand_pin=pin, web_fn=web_fn)
        if ambiguous is not None:
            needs.append(ambiguous)
        elif r is not None:
            resolved.append(r)

    if needs:
        raise NeedsDisambiguation(needs)

    agg = _blend(resolved)
    return AudienceProfile(
        recipients=resolved,
        jargon_tolerance=agg["jargon_tolerance"],
        depth_ceiling=agg["depth_ceiling"],
        depth_ceiling_rung=agg["depth_ceiling_rung"],
        prefer_modules=agg["prefer_modules"],
        avoid_modules=agg["avoid_modules"],
        composite=len(resolved) > 1,
    )
