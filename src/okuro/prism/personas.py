# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resolve a target-group id into a compact persona set (label + PII-
#   firewalled cognitive lens) that the Component Arranger uses to emit per-seat
#   lens tabs — the same point retold for each board seat. okuro·prism moat:
#   layout+content tailored to the audience's actual members.
# index: resolve_personas
# AGENT_HEADER_END -->
"""Persona resolution for prism decks.

A deck built for a concrete target group (e.g. a specific company's board) can
retell each topic per member — the audience-tailoring edge no generic deck tool
has. This turns a ``target_group_id`` into a small list of ``{label, lens}``:

* ``label`` — the member's display name (the lens-tab title; the deck is the
  user's own internal artifact about their own audience, so the name is wanted).
* ``lens``  — the member's ANONYMIZED cognitive projection from ``person_lens``
  (how to adapt: formality, detail appetite, what lands) — never re-identifying.

Returns ``[]`` (no lens tabs) for a template/generic group, an unknown group, or
fewer than two profiled members — so lens tabs only appear when they are real.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("okuro.prism.personas")


def resolve_personas(group_id: str, *, context: str = "", limit: int = 5) -> list[dict[str, Any]]:
    """Resolve ``group_id`` to up to ``limit`` personas. Returns [] unless the
    group is concrete with >= 2 profiled members."""
    if not (group_id or "").strip():
        return []
    try:
        from okuro.peer.persons import person_lens
        from okuro.peer.target_groups import resolve_audience
    except Exception as exc:  # noqa: BLE001 — peer module optional at import time
        logger.warning("resolve_personas: import failed: %s", exc)
        return []

    aud = resolve_audience(group_id)
    if not isinstance(aud, dict) or aud.get("error") or aud.get("generic"):
        return []

    from okuro.db import get_db
    db = get_db()

    personas: list[dict[str, Any]] = []
    for m in (aud.get("members") or [])[:limit]:
        pid = m.get("person_id")
        label = (m.get("display_name") or pid or "").strip()
        if not pid or not label:
            continue
        # Substantive concern = the person's role/lens description (name-free);
        # the tab is already labelled with the name, so this adds no new PII.
        row = db.fetchone("SELECT role FROM persons WHERE id = ?", (pid,))
        concern = " ".join(((row["role"] if row else "") or "").split())[:280]
        # Fall back to the anonymized cognitive sliders when no concern is set.
        lens = concern
        if not lens:
            try:
                cog = person_lens(pid, context=context) or ""
            except Exception as exc:  # noqa: BLE001 — a missing lens never blocks the build
                logger.warning("resolve_personas: person_lens failed for %s: %s", pid, exc)
                cog = ""
            for line in cog.splitlines():
                if "Sliders:" in line:
                    lens = " ".join(line.replace("**", "").split())[:200]
                    break
        personas.append({"label": label, "lens": lens or "no profile"})

    return personas if len(personas) >= 2 else []


def resolve_member_sliders(group_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Resolve a target group into its members' PII-firewalled cognitive slider
    vectors — the input to ``audience.group_policy`` (group-resolution, not lens
    tabs). Returns ``[{"sliders": {...}}, ...]``; ``[]`` for a generic/unknown
    group or fewer than 2 profiled members."""
    if not (group_id or "").strip():
        return []
    try:
        from okuro.peer.cognitive_profile import cognitive_profile_for_llm
        from okuro.peer.target_groups import resolve_audience
    except Exception as exc:  # noqa: BLE001 — peer module optional at import time
        logger.warning("resolve_member_sliders: import failed: %s", exc)
        return []

    aud = resolve_audience(group_id)
    if not isinstance(aud, dict) or aud.get("error") or aud.get("generic"):
        return []

    out: list[dict[str, Any]] = []
    for m in (aud.get("members") or [])[:limit]:
        pid = m.get("person_id")
        if not pid:
            continue
        try:
            prof = cognitive_profile_for_llm(pid) or {}
        except Exception as exc:  # noqa: BLE001 — a missing profile never blocks the build
            logger.warning("resolve_member_sliders: profile failed for %s: %s", pid, exc)
            continue
        if prof.get("sliders"):
            out.append({"sliders": prof["sliders"]})
    return out
