# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Output rendering profiles — two-axis structure × paint resolver.
# index:
#   imports
#   _STRUCTURE_DEFAULTS
#   _ACCESSIBLE_DEFAULTS
#   def _load_user_profile
#   def _structure_from_user_profile
#   def _structure_from_person
#   def _resolve_paint
#   def resolve_output_profile
# AGENT_HEADER_END -->
"""Output Rendering Model — two-axis system for okuro visual output.

Every output has a RECIPIENT and a CONTEXT:

    STRUCTURE  = how information is organized  → derived from RECIPIENT's cognitive profile
    PAINT      = how it looks visually         → derived from CONTEXT's design profile

    resolve_output_profile(recipient, context) → {"structure": {...}, "paint": "profile-id"}

Recipient types:
    "self"    → user_profile.ui_adaptations
    "public"  → accessible defaults
    <slug>    → persons table lookup

Context types:
    "personal"  → user's preferred design profile
    <kit>       → a design_engine kit id
"""
import copy
import json

from okuro.db import get_db


_STRUCTURE_DEFAULTS = {
    "focuser": {
        "preset": "focuser",
        "visual": {
            "font_scale": 1.0,
            "contrast": "standard",
            "animation": "reduced",
            "size_contrast": "extreme",
            "whitespace": "spacious",
        },
        "layout": {
            "content_pattern": "drill-down",
            "hierarchy": "progressive-disclosure",
            "levels": ["topics", "items", "details"],
            "items_per_level": 7,
            "one_focus_per_screen": True,
        },
        "information": {
            "context_treatment": "on-demand",
            "truncation": "title-only",
            "show_ids": False,
            "section_numbering": False,
            "sort_rationale": False,
        },
        "interaction": {
            "action_language": "soft",
            "max_options": 3,
            "consistency": "strict",
            "edit_before_act": True,
            "navigation": "drill-in-out",
        },
    },
    "browser": {
        "preset": "browser",
        "visual": {
            "font_scale": 1.15,
            "contrast": "high",
            "animation": "reduced",
            "size_contrast": "moderate",
            "whitespace": "balanced",
        },
        "layout": {
            "content_pattern": "cards",
            "hierarchy": "progressive-disclosure",
            "levels": ["groups", "items"],
            "items_per_level": 5,
            "one_focus_per_screen": False,
        },
        "information": {
            "context_treatment": "expandable",
            "truncation": "expandable",
            "show_ids": False,
            "section_numbering": False,
            "sort_rationale": False,
        },
        "interaction": {
            "action_language": "direct",
            "max_options": 3,
            "consistency": "flexible",
            "edit_before_act": False,
            "navigation": "scroll",
        },
    },
    "analyst": {
        "preset": "analyst",
        "visual": {
            "font_scale": 1.0,
            "contrast": "standard",
            "animation": "none",
            "size_contrast": "moderate",
            "whitespace": "tight",
        },
        "layout": {
            "content_pattern": "table",
            "hierarchy": "flat",
            "levels": ["all"],
            "items_per_level": 25,
            "one_focus_per_screen": False,
        },
        "information": {
            "context_treatment": "subtitle",
            "truncation": "hard",
            "show_ids": True,
            "section_numbering": True,
            "sort_rationale": True,
        },
        "interaction": {
            "action_language": "direct",
            "max_options": 5,
            "consistency": "strict",
            "edit_before_act": False,
            "navigation": "scroll",
        },
    },
}

_ACCESSIBLE_DEFAULTS = copy.deepcopy(_STRUCTURE_DEFAULTS["browser"])
_ACCESSIBLE_DEFAULTS["preset"] = "accessible"
_ACCESSIBLE_DEFAULTS["visual"].update({
    "font_scale": 1.2,
    "contrast": "high",
    "animation": "none",
})


def _load_user_profile() -> dict:
    """Load and parse the user profile JSON. Returns empty dict on failure."""
    try:
        row = get_db().fetchone("SELECT profile FROM user_profile WHERE id = 1")
        if not row:
            return {}
        raw = row["profile"]
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}


def _structure_from_user_profile() -> dict:
    """Read structure from user_profile.ui_adaptations."""
    profile = _load_user_profile()
    ui = profile.get("ui_adaptations")
    if ui:
        return ui
    return _STRUCTURE_DEFAULTS["focuser"]


def _structure_from_person(person_id: str) -> dict:
    """Derive structure from a person's cognitive profile.

    Maps cognitive fields to a structure preset, then applies overrides
    from accessibility and learning_style.
    """
    try:
        db = get_db()
        row = db.fetchone(
            "SELECT cognitive, communication FROM persons WHERE id = ? AND active = 1",
            (person_id,),
        )
        if not row:
            # Try display name match (case-insensitive substring)
            row = db.fetchone(
                "SELECT cognitive, communication FROM persons "
                "WHERE LOWER(display_name) LIKE ? AND active = 1 LIMIT 1",
                (f"%{person_id.lower()}%",),
            )
        if not row:
            return _ACCESSIBLE_DEFAULTS

        def _parse(val):
            if not val:
                return {}
            return json.loads(val) if isinstance(val, str) else val

        cog = _parse(row["cognitive"])
        comm = _parse(row["communication"])

        learning = cog.get("learning_style", "")
        attention = cog.get("attention_span", "")

        if learning == "visual" or attention == "short":
            base = copy.deepcopy(_STRUCTURE_DEFAULTS["browser"])
        elif learning == "analytical":
            base = copy.deepcopy(_STRUCTURE_DEFAULTS["analyst"])
        else:
            base = copy.deepcopy(_STRUCTURE_DEFAULTS["browser"])

        acc = cog.get("accessibility", []) or []
        if "larger text" in acc:
            base["visual"]["font_scale"] = max(base["visual"].get("font_scale", 1.0), 1.15)
        if "high contrast" in acc:
            base["visual"]["contrast"] = "high"
        if "no small typography" in acc:
            base["visual"]["font_scale"] = max(base["visual"].get("font_scale", 1.0), 1.1)

        fmt = comm.get("format_preferences", [])
        if isinstance(fmt, list):
            if "tables" in fmt:
                base["information"]["prefer_tables"] = True
            if "charts" in fmt:
                base["information"]["prefer_charts"] = True

        lang = comm.get("language")
        if lang:
            base["language"] = lang

        return base

    except Exception:
        return _ACCESSIBLE_DEFAULTS


DEFAULT_KIT = "okuro-ds"
"""The design system every install ships with, and the fallback for a name that
does not resolve. Was `architecture-noir`, a v0 profile."""


def _kit_exists(kit_id: str) -> bool:
    """Whether the engine can open this design system.

    Replaces a check for a YAML PATH. A kit has no path to hand back — the
    shipped one is a Python literal and a user's is JSON in their own store —
    so the question became "can it be loaded", which is what the caller
    actually wanted to know.
    """
    if not kit_id:
        return False
    try:
        from okuro.design_engine import store

        store.load(kit_id)
        return True
    except Exception:
        return False


def _resolve_paint(context: str) -> str:
    """Resolve context to a design_engine kit id.

    "personal" → user's work_style.design_preference (fallback: okuro-ds)
    <kit>      → the kit if the engine can open it, else okuro-ds
    """
    if not context or context == "personal":
        profile = _load_user_profile()
        pref = profile.get("work_style", {}).get("design_preference")
        if _kit_exists(pref):
            return pref
        return DEFAULT_KIT

    return context if _kit_exists(context) else DEFAULT_KIT


def resolve_output_profile(recipient: str = "self", context: str = "personal") -> dict:
    """Resolve the full rendering profile for an output.

    Args:
        recipient: "self" (user), "public", or a person slug
        context: "personal" (user's design pref), or an okuro.design profile slug

    Returns:
        {
            "structure": { ... ui_adaptations dict ... },
            "paint": "architecture-noir",
            "recipient": "self",
            "context": "personal",
        }
    """
    if recipient == "self" or not recipient:
        structure = _structure_from_user_profile()
    elif recipient == "public":
        structure = _ACCESSIBLE_DEFAULTS
    else:
        structure = _structure_from_person(recipient)

    # `paint_path` IS GONE. It handed back a YAML path, a kit has none, and
    # measured 2026-09-06: nothing read the field — it was produced here,
    # echoed in one fallback constant, and consumed nowhere.
    return {
        "structure": structure,
        "paint": _resolve_paint(context),
        "recipient": recipient,
        "context": context,
    }
