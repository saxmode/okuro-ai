# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Brand VOICE enforcement — turn a design profile's `language` block
#   (tone / casing / vocabulary / banned words) into an explicit authoring
#   directive injected into both generation passes. Phase G: voice as content
#   tokens, enforced DURING generation, not styled on afterwards.
# index:
#   def voice_clause
# AGENT_HEADER_END -->
"""okuro·prism brand voice — the `language` block made load-bearing.

Design profiles already carry a ``language`` block (tone, casing, terminology
prefer/avoid). Generation used to dump it into a truncated JSON blob with a soft
"bias toward this if evident" — so voice was advisory and often lost. This turns
it into an EXPLICIT, hard directive: prefer this vocabulary, never use these
words, write at this altitude — the same PAINT-not-STRUCTURE separation prism
already applies to design tokens, now for prose.

Pure function of the profile's ``language`` dict; empty/absent → "" so a brand
without a voice block generates exactly as before (strictly additive).
"""

from __future__ import annotations

from typing import Any


def voice_clause(language: dict[str, Any] | None) -> str:
    """Render a brand's ``language`` block into an authoring directive, or "".

    Reads ``tone``, ``casing``, ``terminology.prefer`` (a substitution map),
    ``terminology.avoid`` (hard bans) and an optional ``voice`` sub-block
    (``sentences`` style, ``banned_phrases``, ``reference`` body-of-work). Only
    emits when there's real signal — a brand with an empty ``language`` yields
    "" and generation is untouched.
    """
    if not isinstance(language, dict) or not language:
        return ""

    voice = language.get("voice") if isinstance(language.get("voice"), dict) else {}
    lines = [
        "BRAND VOICE — enforce this WHILE authoring every line (it governs HOW "
        "the content reads; it never changes WHICH facts you state):",
    ]

    tone = language.get("tone")
    if tone:
        lines.append(f"- Tone: {tone}")
    sentences = voice.get("sentences")
    if sentences:
        lines.append(f"- Sentences: {sentences}")
    casing = language.get("casing")
    if casing:
        lines.append(f"- Casing: {casing}")

    term = language.get("terminology") if isinstance(language.get("terminology"), dict) else {}
    prefer = term.get("prefer") if isinstance(term.get("prefer"), dict) else {}
    if prefer:
        subs = ", ".join(f"say '{v}' not '{k}'" for k, v in prefer.items())
        lines.append(f"- Vocabulary: {subs}.")

    banned = [w for w in (list(term.get("avoid") or []) + list(voice.get("banned_phrases") or [])) if w]
    if banned:
        lines.append(f"- NEVER use these words or phrases: {', '.join(banned)}.")

    reference = voice.get("reference")
    if reference:
        lines.append(f"- Write like: {reference}")

    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = ["voice_clause"]
