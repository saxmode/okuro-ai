# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.theme — tokens_to_theme(brand_id, channel) ->
#   ThemeBundle. Composes brand tokens (color/type/spacing) with
#   channel-specific configuration (Marp CSS, Tailwind config,
#   voice_preset). PPTX/DOCX/XLSX explicitly banned.
# index: imports | dataclass ThemeBundle | def _default_profile_id |
#   def _brand_profile_id | def _tokens_from_profile | def _resolve_brand |
#   def _voice_preset | def _tok | def _build_marp_css |
#   def _build_tailwind_config | def tokens_to_theme
# AGENT_HEADER_END -->
"""Brand token -> channel-specific theme bundles.

Composition rule (from gap memory `audience-adapted delivery pipeline`):
brand tokens never leak as raw hex into renderer output — every channel
gets a theme bundle keyed by its native config shape (Marp CSS string,
Tailwind config dict, voice preset for audio). Renderers consume the
bundle as-is.

Resolution chain — the okuro contract, one chain everywhere::

    brand -> slots.design -> kit id
      | (no brand, missing design slot, or unusable kit)
    user's `design.kit`  (then `design.profile`, read as a legacy key)
      | (unset)
    okuro-ds

Tokens are always DERIVED from a design system, never snapshotted into
this module. A hardcoded copy drifts, and it did twice: the original
``_DEFAULT_TOKENS`` mirrored a profile at authoring time and drifted on
foreground/muted/border, and ``_LAST_RESORT_TOKENS`` then sat on v0's green
``#22c55e`` for every day between okuro changing its colour and v0 being
deleted. That block is reached only when the shipped kit itself cannot load
(broken install) — it exists so renderers never emit invalid CSS, not as a
styling decision.

Note the chain deliberately ignores the user's ``design.overrides`` bag
(accent/pulse). Those are okuro-UI-only, applied client-side in the SPA
(web/frontend/src/lib/theme.ts), and must never tint a professional
brand's delivery. The token generators cannot see them by construction.

okuro never emits Microsoft proprietary formats — see convention
memory 50b49e1c. No pptx_master, no docx template, no xlsx workbook.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


#: The design system okuro ships. Was `architecture-noir`, a v0 profile, until
#: p10 deleted that layer (2026-09-06).
_DEFAULT_PROFILE_ID = "okuro-ds"

# Last-resort tokens — ONLY reached when the shipped kit cannot be loaded at
# all (broken install). A SNAPSHOT of okuro-ds, refreshed 2026-09-06, so a
# degraded render still reads as okuro rather than as unstyled HTML. Not a
# fallback for brands: a brand that resolves at all gets its own tokens.
#
# IT CANNOT BE DERIVED, and that is the point — this branch exists precisely
# for when derivation is impossible. So it is a literal, and a literal drifts:
# it carried v0's `#22c55e` long after okuro's colour became `#a2ffe5`, and
# nothing noticed because the branch is unreachable in a working install.
# `test_delivery_pipeline` now reads the default FROM THE KIT, which is what
# makes a future drift here visible.
_LAST_RESORT_TOKENS = {
    "color": {
        "primary": "#a2ffe5",
        "background": "#030303",
        "foreground": "#ffffff",
        "muted": "#818181",
        "border": "#353535",
    },
    "type": {
        "heading": "JetBrains Mono, monospace",
        "body":    "JetBrains Mono, monospace",
    },
    "spacing": {
        "tight":  "8px",
        "normal": "16px",
        "loose":  "32px",
    },
}


@dataclass
class ThemeBundle:
    """Brand × channel configuration package consumed by renderers."""

    brand_id: str | None
    channel: str
    tokens: dict[str, Any] = field(default_factory=dict)
    channel_specific: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "channel": self.channel,
            "tokens": dict(self.tokens),
            "channel_specific": dict(self.channel_specific),
        }


# ----------------------------------------------------------------------
# Brand resolution
# ----------------------------------------------------------------------


def _default_profile_id() -> str:
    """The okuro default design profile — the user's choice, else the built-in.

    Mirrors the chain served by ``GET /engine.css`` (design_engine.api) so the
    SPA and delivery renderers agree on what "unbranded" looks like.
    """
    try:
        from okuro.yu.profile import get_profile_raw

        # `design.kit` IS THE ACTIVE DESIGN SYSTEM; `design.profile` is read
        # only as a legacy key, for installs made before it existed. Same
        # order and same reason as design_engine.api::_active_kit_id.
        design = get_profile_raw().get("design") or {}
        chosen = design.get("kit") or design.get("profile")
        if chosen:
            return str(chosen)
    except Exception as e:
        log.debug("could not read user design profile: %s", e)
    return _DEFAULT_PROFILE_ID


def _brand_profile_id(brand_id: str) -> str | None:
    """Design-profile id from a brand's `design` slot, or None if unset/missing.

    resolve_brand expands each slot to ``{ref_id, data}``, or ``{ref_id,
    missing: true}`` when the ref dangles (slot refs are deliberately not
    FK-constrained). We want the id, not the payload — _tokens_from_profile
    owns loading so there is a single profile-read path.
    """
    try:
        from okuro.stack.registry import resolve_brand

        resolved = resolve_brand(brand_id)
    except Exception as e:
        log.warning("brand %r failed to resolve: %s", brand_id, e)
        return None
    if not isinstance(resolved, dict):
        return None
    design = (resolved.get("slots") or {}).get("design")
    if not isinstance(design, dict):
        return None
    ref_id = design.get("ref_id")
    if design.get("missing"):
        log.warning(
            "brand %r design slot points at missing profile %r", brand_id, ref_id
        )
        return None
    return str(ref_id) if ref_id else None


def _tokens_from_profile(profile_id: str) -> dict[str, Any] | None:
    """Flatten a design profile into the {color, type, spacing} delivery shape.

    Design profiles nest (palette.background.base); delivery channels want a
    flat 3-key map. Returns None when the profile is absent or lacks the
    identity-carrying values (accent / background / foreground), so the
    caller can advance down the chain rather than emit a half-themed render.

    Neutral values (muted, border, spacing) fall back to _LAST_RESORT_TOKENS:
    greys and gaps don't carry brand identity, the accent does.
    """
    # THROUGH THE RESOLVER, so a kit id and a v0 profile id both work. This
    # imported okuro.design.profiles directly, which made every client
    # deliverable, microsite and Marp deck fall to _LAST_RESORT_TOKENS the
    # moment v0 went — okuro-branded output where the client's brand belonged,
    # and silently, because the chain treats a None as "try the next source".
    try:
        from okuro.stack.registry import _resolve_design_profile

        profile = _resolve_design_profile(profile_id)
    except Exception as e:
        log.warning("design profile %r failed to load: %s", profile_id, e)
        return None
    if not isinstance(profile, dict):
        return None

    visual = profile.get("visual") or {}
    palette = visual.get("palette") or {}
    typo = visual.get("typography") or {}
    spacing = (visual.get("layout") or {}).get("spacing") or {}

    def _nested(group: str, key: str) -> str:
        val = palette.get(group)
        if isinstance(val, dict):
            return str(val.get(key) or "")
        return str(val or "") if val else ""

    accent = str(palette.get("accent") or "")
    background = _nested("background", "base")
    foreground = _nested("foreground", "primary")
    if not (accent and background and foreground):
        log.warning(
            "design profile %r missing accent/background/foreground — skipping",
            profile_id,
        )
        return None

    lr = _LAST_RESORT_TOKENS
    family = str(typo.get("primary") or "")
    fallback = str(typo.get("fallback") or "monospace")
    stack = f"{family}, {fallback}" if family else lr["type"]["body"]

    def _rung(*keys: str, default: str) -> str:
        """First present rung — profiles need not define every spacing step."""
        for k in keys:
            if spacing.get(k):
                return str(spacing[k])
        return default

    return {
        "color": {
            "primary": accent,
            "background": background,
            "foreground": foreground,
            "muted": _nested("foreground", "tertiary") or lr["color"]["muted"],
            "border": _nested("borders", "default") or lr["color"]["border"],
        },
        "type": {"heading": stack, "body": stack},
        "spacing": {
            "tight": _rung("sm", default=lr["spacing"]["tight"]),
            "normal": _rung("md", default=lr["spacing"]["normal"]),
            # architecture-noir has lg-plus (32px); some brands stop at xl (40px).
            "loose": _rung("lg-plus", "xl", "lg", default=lr["spacing"]["loose"]),
        },
    }


def _resolve_brand(brand_id: str | None) -> dict[str, Any]:
    """Resolve a brand to its token map via the okuro chain.

    brand's design slot -> user's default kit -> okuro-ds.
    Every step that misses is logged; nothing is swallowed silently.
    """
    candidates: list[str] = []
    if brand_id:
        pid = _brand_profile_id(brand_id)
        if pid:
            candidates.append(pid)
        else:
            log.info("brand %r has no usable design slot — using okuro default", brand_id)
    for pid in (_default_profile_id(), _DEFAULT_PROFILE_ID):
        if pid not in candidates:
            candidates.append(pid)

    for pid in candidates:
        tokens = _tokens_from_profile(pid)
        if tokens:
            return tokens

    log.error(
        "no usable design profile (tried %s) — falling back to last-resort tokens",
        ", ".join(repr(c) for c in candidates),
    )
    return {k: dict(v) for k, v in _LAST_RESORT_TOKENS.items()}


def _voice_preset(brand_id: str | None) -> dict[str, Any]:
    """Read brands.voice_preset (column added in 036). None-safe."""
    if not brand_id:
        return {}
    try:
        from okuro.db import get_db
        db = get_db()
        row = db.fetchone(
            "SELECT voice_preset FROM brands WHERE id = ?", (brand_id,)
        )
    except Exception:
        return {}
    if not row:
        return {}
    raw = row.get("voice_preset")
    if not raw:
        return {}
    try:
        loaded = json.loads(raw) if isinstance(raw, str) else raw
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, ValueError):
        return {}


# ----------------------------------------------------------------------
# Channel-specific config builders
# ----------------------------------------------------------------------


def _tok(tokens: dict[str, Any], group: str, key: str) -> str:
    """Read a token, falling back to the last-resort value for that slot.

    Keeps the channel builders free of inline literals — the only palette
    constants in this module live in _LAST_RESORT_TOKENS.
    """
    val = (tokens.get(group) or {}).get(key)
    return str(val) if val else _LAST_RESORT_TOKENS[group][key]


def _build_marp_css(tokens: dict[str, Any]) -> str:
    """Render brand tokens as a Marp theme CSS fragment.

    Marp themes import a base ('default'/'gaia'/'uncover') and override
    with custom CSS. We override section colors + headings + body font
    to match the brand. The CSS is small (< 1 KB) and embedded inline
    in the Marp markdown frontmatter so the renderer subprocess needs
    no theme file on disk.
    """
    primary = _tok(tokens, "color", "primary")
    background = _tok(tokens, "color", "background")
    foreground = _tok(tokens, "color", "foreground")
    muted = _tok(tokens, "color", "muted")
    heading_font = _tok(tokens, "type", "heading")
    body_font = _tok(tokens, "type", "body")

    return (
        "/* @theme okuro-delivery */\n"
        "@import 'default';\n"
        "section {\n"
        f"  background: {background};\n"
        f"  color: {foreground};\n"
        f"  font-family: {body_font};\n"
        "}\n"
        "h1, h2, h3, h4 {\n"
        f"  color: {primary};\n"
        f"  font-family: {heading_font};\n"
        "}\n"
        f"strong {{ color: {primary}; }}\n"
        f"hr {{ border-color: {muted}; }}\n"
        "blockquote {\n"
        f"  border-left: 4px solid {primary};\n"
        f"  color: {foreground};\n"
        "}\n"
    )


def _build_tailwind_config(tokens: dict[str, Any]) -> dict[str, Any]:
    """Tailwind 4 inline config matching the brand tokens.

    Microsite renderer drops this into ``tailwind.config.js`` (or
    @theme inline) so the static site picks up the brand colors
    without further wiring.
    """
    return {
        "theme": {
            "extend": {
                "colors": {
                    "brand-primary":     _tok(tokens, "color", "primary"),
                    "brand-bg":          _tok(tokens, "color", "background"),
                    "brand-fg":          _tok(tokens, "color", "foreground"),
                    "brand-muted":       _tok(tokens, "color", "muted"),
                    "brand-border":      _tok(tokens, "color", "border"),
                },
                "fontFamily": {
                    "heading": [_tok(tokens, "type", "heading")],
                    "body":    [_tok(tokens, "type", "body")],
                },
            },
        },
    }


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def tokens_to_theme(brand_id: str | None, channel: str) -> ThemeBundle:
    """Build a ThemeBundle for ``channel`` from ``brand_id``'s tokens.

    Always returns a bundle, even on missing brand / unresolved tokens
    (falls back down the chain to the okuro default design profile).
    Channel-specific block depends on the channel identifier:

      markdown   -> empty (markdown channel uses tokens via outline only)
      marp       -> {"marp_css": "..."}
      microsite  -> {"tailwind_config": {...}}
      tts        -> {"voice_preset": {...}}
      podcast    -> {"voice_preset": {...}}

    Unrecognised channels still return the bundle with empty
    channel_specific so downstream code is None-safe.
    """
    tokens = _resolve_brand(brand_id)
    voice = _voice_preset(brand_id)

    channel_specific: dict[str, Any] = {}
    if channel == "marp":
        channel_specific["marp_css"] = _build_marp_css(tokens)
    elif channel == "microsite":
        channel_specific["tailwind_config"] = _build_tailwind_config(tokens)
    elif channel in ("tts", "podcast"):
        channel_specific["voice_preset"] = voice
    # markdown → no channel_specific overrides; tokens carry it.
    # pptx / docx / xlsx are intentionally absent — banned formats.

    return ThemeBundle(
        brand_id=brand_id,
        channel=channel,
        tokens=tokens,
        channel_specific=channel_specific,
    )
