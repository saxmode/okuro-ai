# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role-based person profile presets — seeds communication+cognitive shape from the roles/catalog YAMLs.
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
The roles/catalog/*.yaml files carry that knowledge under a `person_preset:`
block. This module loads and resolves them.

- `resolve_preset("CEO")` → dict from ceo.yaml, or None
- `list_preset_roles()`   → [{role_id, label, domain}, ...] for UI pickers
- `generate_preset_llm(q)` → fallback via bridge_invoke when no catalog match

The YAML catalog is the source of truth. We read once, cache by role_id,
and match via (1) exact slug, (2) substring on role_id, (3) substring on
description. No embedding lookups — the preset set is small and static.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import yaml

log = logging.getLogger("okuro.peer.presets")

_PRESET_CACHE: dict[str, dict[str, Any]] | None = None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _load_presets() -> dict[str, dict[str, Any]]:
    """Scan catalog YAMLs and return {role_id: preset_entry}.

    preset_entry shape: {role_id, label, domain, description, person_preset}.
    Roles without a `person_preset:` block are skipped.
    """
    global _PRESET_CACHE
    if _PRESET_CACHE is not None:
        return _PRESET_CACHE

    out: dict[str, dict[str, Any]] = {}
    # Both catalog layers via the shared resolver (okuro.roles.layers) —
    # a personal role in ~/.okuro/roles/catalog contributes its preset
    # exactly like a shipped one. Shipped comes first; a colliding user
    # role_id is the seeder's refusal case, so here it is just skipped.
    from okuro.roles.layers import catalog_dirs

    paths = [
        p
        for cdir, _origin in catalog_dirs()
        if cdir.exists()
        for p in sorted(cdir.glob("*.yaml"))
    ]

    for path in paths:
        try:
            with path.open() as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            log.warning("skipping malformed %s: %s", path.name, exc)
            continue
        preset = data.get("person_preset")
        if not preset or not isinstance(preset, dict):
            continue
        role_id = data.get("role_id")
        if not role_id:
            continue
        if role_id in out:
            log.warning(
                "preset for role_id %s already loaded from an earlier "
                "catalog layer — skipping %s", role_id, path,
            )
            continue
        parts = role_id.split("-")
        label = " ".join(p.upper() if len(p) <= 3 else p.capitalize() for p in parts)
        out[role_id] = {
            "role_id": role_id,
            "label": label,
            "domain": data.get("domain"),
            "description": data.get("description"),
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
    or None if no catalog role clears a minimal confidence threshold (0.5).

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
