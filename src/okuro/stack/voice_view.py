# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: project a voice profile into the `language` dict shape its consumers read, so the voice slot can replace the design profile's language block.
# index:
#   language_view
#   brand_view
# AGENT_HEADER_END -->
"""A brand's voice, in the vocabulary its consumers already speak.

THE OTHER HALF OF `design_engine/profile_view.py`, and it exists for the same
reason. v0's design profile fused two things: a palette and a VOICE. The
palette moved to the engine and `profile_view` projects it back into the shape
five subsystems read. The voice moved to `voice_profiles` (migration 146) and
until now nothing projected it anywhere -- so `language` had left the design
slot and arrived nowhere its readers looked.

MEASURED, 2026-09-05, before this was written:

    prism/generate.py:882   voice_clause(design.get("language"))   -> {} for a kit
    sections.py::build_brand                                       -> renders no voice
    sections.py::_voice_lines                                      -> DOES render it,
        but only from build_design_profile, which returns EMPTY for a
        brand-bound project -- so okuro's own packet carried the WORD "voice"
        in a slot list and none of its content.

That is why repointing a design slot at a kit could not be a one-line change:
the value was still being read out of the layer being retired, and the failure
would have been silent in both consumers.

THE SHAPE IS v0's `language`, FIELD FOR FIELD, because that is what
`prism.voice.voice_clause` parses and there is no reason to make it learn a
second dialect:

    tone                     voice_profiles.tone
    casing                   voice_profiles.casing
    locale                   voice_profiles.locale
    formatting {k: v}        rules kind='formatting'   (key -> value)
    terminology.prefer {k:v} rules kind='prefer'       (key -> value)
    terminology.avoid [..]   rules kind='avoid'        (key)
    voice.sentences          voice_profiles.sentences
    voice.reference          voice_profiles.reference
    voice.banned_phrases     rules kind='banned_phrase' (key)

`constraint` rules are NOT part of `language` and are not folded in. In v0 they
sat under `visual.constraints` -- a statement about the interface, not about
prose -- and voice_clause would render them as writing instructions. They reach
agents through the bootstrap voice lines, which show every kind.

A ROW THIS DOES NOT RECOGNISE IS STILL PROJECTED. The rules table's `kind` is
open-ended on purpose (a sixth kind is a row, not a migration), so an unknown
kind is skipped here rather than raising -- the caller gets the language it can
use and the bootstrap lines still show the rest.
"""

from __future__ import annotations

from typing import Any


def _pairs(rules: dict[str, list[dict]], kind: str) -> dict[str, str]:
    """A `key -> value` rule kind as a dict. Rows with no value are dropped:
    a substitution with nothing to substitute to is not a substitution."""
    out: dict[str, str] = {}
    for row in rules.get(kind) or []:
        key, value = row.get("key"), row.get("value")
        if key and value:
            out[str(key)] = str(value)
    return out


def _keys(rules: dict[str, list[dict]], kind: str) -> list[str]:
    """A rule kind that is a LIST -- the key is the whole statement."""
    return [str(r["key"]) for r in (rules.get(kind) or []) if r.get("key")]


def language_view(voice: dict[str, Any] | None) -> dict[str, Any]:
    """A resolved voice profile -> v0's `language` dict. `{}` for None.

    Empty sub-blocks are omitted rather than emitted empty, because
    `voice_clause` tests each for truthiness and an empty dict is one more
    thing for every caller to guard.
    """
    if not isinstance(voice, dict) or not voice:
        return {}

    rules = voice.get("rules") if isinstance(voice.get("rules"), dict) else {}
    out: dict[str, Any] = {}

    for field in ("tone", "casing", "locale"):
        if voice.get(field):
            out[field] = voice[field]

    formatting = _pairs(rules, "formatting")
    if formatting:
        out["formatting"] = formatting

    terminology: dict[str, Any] = {}
    prefer = _pairs(rules, "prefer")
    if prefer:
        terminology["prefer"] = prefer
    avoid = _keys(rules, "avoid")
    if avoid:
        terminology["avoid"] = avoid
    if terminology:
        out["terminology"] = terminology

    inner: dict[str, Any] = {}
    for field in ("sentences", "reference"):
        if voice.get(field):
            inner[field] = voice[field]
    banned = _keys(rules, "banned_phrase")
    if banned:
        inner["banned_phrases"] = banned
    if inner:
        out["voice"] = inner

    return out


def brand_view(voice: dict[str, Any] | None) -> dict[str, Any]:
    """The `brand` block v0's design profile carried: owner and domain.

    Same story as `language`: who the brand belongs to and what it covers are
    facts about the BRAND, and they sat in the design YAML because that was the
    file that existed. Projected separately from `language` because they are
    identity rather than voice, and no consumer wants them mixed in.
    """
    if not isinstance(voice, dict) or not voice:
        return {}
    return {k: voice[k] for k in ("owner", "domain") if voice.get(k)}


__all__ = ["language_view", "brand_view"]
