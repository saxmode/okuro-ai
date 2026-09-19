# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role-based person profile presets — seeds communication+cognitive shape from the roles table.
# index:
#   imports
#   _PRESET_CACHE
#   def _load_presets
#   def list_preset_roles
#   def resolve_preset
#   def _slugify
#   def _score_match
#   def generate_preset_llm
# AGENT_HEADER_END -->
"""Role-based person profile presets.

When a user adds a person with a role (e.g. "CEO", "Frontend Engineer"), we
want a sensible starter communication/cognitive profile — not an empty shell.
Six roles carry that knowledge in their `person_preset` column. This module
loads and resolves them.

- `resolve_preset("CEO")` → the ceo role's preset, or None
- `list_preset_roles()`   → [{role_id, label, domain}, ...] for UI pickers
- `generate_preset_llm(q)` → fallback via bridge_invoke when no role matches

This used to read the shipped role YAML directly, which made it the one
RUNTIME reader of a file layer that everything else had already stopped
consulting. Migration 155 moved the blocks into the `roles` table and deleted
those files, so the source of truth here is now the same one the registry and
the resolver use. We read once, cache by role_id, and match via (1) exact
slug, (2) substring on role_id, (3) substring on description. No embedding
lookups — the preset set is small and static.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("okuro.peer.presets")

_PRESET_CACHE: dict[str, dict[str, Any]] | None = None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _load_presets() -> dict[str, dict[str, Any]]:
    """Read every role carrying a person_preset and return {role_id: entry}.

    entry shape: {role_id, label, domain, description, person_preset}.
    Roles whose `person_preset` column is NULL or unparseable are skipped.
    """
    global _PRESET_CACHE
    if _PRESET_CACHE is not None:
        return _PRESET_CACHE

    out: dict[str, dict[str, Any]] = {}
    try:
        from okuro.db import get_db

        rows = get_db().fetchall(
            "SELECT role_id, domain, description, person_preset FROM roles "
            "WHERE person_preset IS NOT NULL ORDER BY role_id"
        )
    except Exception as exc:  # noqa: BLE001 — pre-155 store, or no DB at all
        log.warning("person presets unavailable (%s)", exc)
        return {}

    for row in rows:
        try:
            preset = json.loads(row["person_preset"])
        except (TypeError, ValueError) as exc:
            log.warning("skipping malformed person_preset for %s: %s",
                        row["role_id"], exc)
            continue
        if not isinstance(preset, dict) or not preset:
            continue
        role_id = row["role_id"]
        parts = role_id.split("-")
        label = " ".join(p.upper() if len(p) <= 3 else p.capitalize() for p in parts)
        out[role_id] = {
            "role_id": role_id,
            "label": label,
            "domain": row["domain"],
            "description": row["description"],
            "person_preset": preset,
        }

    _PRESET_CACHE = out
    return out


def list_preset_roles() -> list[dict[str, Any]]:
    """Return all roles that ship a person_preset, sorted by label.

    Used by the frontend role picker on the Add Person dialog.
    Returns only the metadata — not the preset body — to keep the list small.
    """
    presets = _load_presets()
    rows = [
        {
            "role_id": p["role_id"],
            "label": p["label"],
            "domain": p["domain"],
            "description": p["description"],
        }
        for p in presets.values()
    ]
    rows.sort(key=lambda r: r["label"])
    return rows


def _score_match(query_slug: str, preset_entry: dict[str, Any]) -> float:
    """Rank a preset against a slugified query.

    1.0  — exact role_id match
    0.8  — query is a token in role_id (e.g. "ceo" → "acting-ceo")
    0.5  — query appears in description (lowercased)
    0.0  — no signal
    """
    rid = preset_entry["role_id"]
    desc = (preset_entry.get("description") or "").lower()

    if rid == query_slug:
        return 1.0
    if query_slug in rid.split("-"):
        return 0.8
    if query_slug and query_slug in desc:
        return 0.5
    return 0.0


def resolve_preset(role_query: str) -> dict[str, Any] | None:
    """Find the best-matching preset for a free-text role query.

    Returns the full entry ({role_id, label, domain, description, person_preset})
    or None if no role clears a minimal confidence threshold (0.5).

    Caller is responsible for LLM fallback — see `generate_preset_llm`.
    """
    if not role_query or not role_query.strip():
        return None

    presets = _load_presets()
    if not presets:
        return None

    q = _slugify(role_query)
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for entry in presets.values():
        s = _score_match(q, entry)
        if s > best[0]:
            best = (s, entry)

    if best[0] < 0.5 or best[1] is None:
        return None
    return best[1]


def generate_preset_llm(role_query: str) -> dict[str, Any] | None:
    """Ask the bridge to synthesize a person_preset for an unknown role.

    Uses capability='classify' (low-latency, local-first). Returns a preset
    dict shaped like the YAML blocks (communication + cognitive +
    questionnaire_hints), or None on any failure. Never raises.
    """
    if not role_query or not role_query.strip():
        return None

    prompt = (
        "You are profiling a COMMUNICATION PARTNER by role.\n"
        f"Role: {role_query.strip()}\n\n"
        "Return ONLY valid JSON with this exact shape:\n"
        "{\n"
        '  "communication": {\n'
        '    "formality": "...",\n'
        '    "style": "...",\n'
        '    "response_length": "short|moderate|long",\n'
        '    "format_preferences": ["...","..."],\n'
        '    "decision_style": "...",\n'
        '    "avoid": ["...","..."]\n'
        "  },\n"
        '  "cognitive": {\n'
        '    "attention_span": "short|moderate|long",\n'
        '    "expertise_level": {"domain": "expert|working|variable"},\n'
        '    "learning_style": "...",\n'
        '    "pet_peeves": ["...","..."]\n'
        "  },\n"
        '  "questionnaire_hints": ["...","...","..."]\n'
        "}\n"
        "No prose. No code fences. JSON only."
    )

    try:
        from okuro.bridge.invoke import invoke
    except Exception:
        return None

    try:
        result = invoke(prompt, capability="classify")
    except Exception as exc:
        log.debug("bridge invoke failed for preset synth: %s", exc)
        return None

    if not result or not result.get("success"):
        return None
    text = (result.get("output") or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or "communication" not in parsed:
        return None
    return parsed
