# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.sender — P5 sender symmetry. Derives the user's own axis vector
#   from their profile, on the SAME registry the recipient uses. Read-only.
# index: UNMEASURABLE_AXES | sender_format_preferences | sender_axes |
#   sender_block
# AGENT_HEADER_END -->
"""Sender symmetry — give the origin the same coordinates as the destination.

``person_translate`` adapts FROM the user TO a recipient. The recipient was a
17-axis vector; the user was five free-text strings. Half of a translation
had coordinates and the other half had adjectives.

THE RULE THAT GOVERNS EVERY MAPPING BELOW: derive only what a field actually
evidences. An axis with no source stays UNMEASURED and surfaces as a prior —
inventing a sender value would reproduce, on the sender side, the exact
defect P1.2 fixed on the recipient side: a manufactured neutral carrying the
authority of an answer.

COMPUTED AT READ, NEVER PERSISTED. ``yu.profile.update_profile`` accepts any
path and any value with no validation, so a stored sender vector would have
no guard at all and would drift from the profile that justifies it. The
derivation is cheap and the profile is the source of truth.

THE NEUROTYPE LABEL NEVER REACHES THE MODEL. Naming a neurotype in a prompt
makes the model write a persona rather than apply a constraint, and measurably
degrades rewrite fidelity (Q1 schema research :95). It is a SOURCE for the
derivation here — "monotropic attention" is why ``pace`` is 1 — and it stops
at this module.
"""

from __future__ import annotations

from typing import Any

# Axes no field in the user profile evidences. Named rather than silently
# absent, so "why is the sender neutral on numeracy" has an answer that is not
# "somebody forgot".
UNMEASURABLE_AXES: frozenset[str] = frozenset({
    # Nothing in the profile speaks to comfort with figures or charts. The
    # honest move is to leave them unmeasured and let them read as priors.
    "numeracy",
    "graph_literacy",
    # The profile evidences the RATIONAL pole (evidence_over_assumptions) and
    # says nothing about intuition reliance. REI's two poles are orthogonal —
    # high rational does NOT imply low experiential — so deriving one from
    # the other would be an invention.
    "experiential",
    # Per-area, never one global number: expertise reversal is per-topic, and
    # a single sender jargon level would flatten "strong in architecture,
    # working on frontend" into a meaningless average.
    "jargon",
    # usage='prohibited' in the registry. Deriving them would revive axes the
    # registry deliberately retired.
    "certainty",
    "risk_framing",
})


def _rules(items: Any) -> list[str]:
    """Flatten a profile list that mixes bare strings and {rule, rationale}."""
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("rule"), str):
            out.append(item["rule"])
    return out


def _dict(profile: Any, key: str) -> dict:
    val = profile.get(key) if isinstance(profile, dict) else None
    return val if isinstance(val, dict) else {}


def sender_format_preferences(profile: dict) -> list[str]:
    """The user's PREFERRED formats, as strings.

    ``communication.format_preferences`` is a dict of
    ``{preferred: [...], avoid: [...]}`` on the live profile, and was a plain
    list on older ones. The shipped prompt builder did
    ``', '.join(prefs or [])`` over it — which iterates a dict's KEYS, so
    every translate prompt carried the literal string ``"avoid, preferred"``
    as the sender's format preference. Measured, not inferred.

    The ``avoid`` list is deliberately NOT included: an avoid rendered as a
    preference is worse than no preference at all.
    """
    prefs = _dict(profile, "communication").get("format_preferences")
    if isinstance(prefs, list):
        return _rules(prefs) or [p for p in prefs if isinstance(p, str)]
    if isinstance(prefs, dict):
        return _rules(prefs.get("preferred"))
    return []


def sender_axes(profile: dict) -> dict[str, int]:
    """Derive the sender's axis vector. Never raises, never writes.

    Every entry cites the field that justifies it. An unrecognised value
    derives NOTHING for that axis rather than falling through to a neutral —
    a default here would be indistinguishable from a measurement.
    """
    if not isinstance(profile, dict):
        return {}

    comm = _dict(profile, "communication")
    decision = _dict(profile, "decision_style")
    cognitive = _dict(profile, "cognitive_style")

    axes: dict[str, int] = {}
    patterns = " ".join(_rules(comm.get("patterns"))).lower()
    implications = " ".join(_rules(cognitive.get("implications"))).lower()

    # format ← communication.format_preferences
    prefs = " ".join(sender_format_preferences(profile)).lower()
    if prefs:
        axes["format"] = 5 if ("bullet" in prefs or "table" in prefs) else 1

    # decision_framing ← decision_style.framing
    framing = str(decision.get("framing") or "").lower()
    if "recommendation" in framing:
        axes["decision_framing"] = 5
    elif "option" in framing:
        axes["decision_framing"] = 1

    # lead_with ← the communication rules themselves
    if "lead with answer" in patterns:
        axes["lead_with"] = 5
    elif "lead with context" in patterns:
        axes["lead_with"] = 1

    # density ← "needs short, focused communication blocks"
    if "short" in implications and "block" in implications:
        axes["density"] = 2

    # pace ← monotropic attention: one deep thread, not a parallel scan
    if "monotropic" in implications or "monotropic" in patterns:
        axes["pace"] = 1

    # need_for_cognition ← research_first AND evidence_over_assumptions.
    # BOTH, deliberately: wanting evidence is not the same as wanting the
    # full chain of reasoning, and only the pair evidences the pole.
    if decision.get("research_first") and decision.get("evidence_over_assumptions"):
        axes["need_for_cognition"] = 5

    # rational ← evidence_over_assumptions
    if decision.get("evidence_over_assumptions"):
        axes["rational"] = 5

    # construal ← abstraction, or the systems-thinker implication
    abstraction = str(cognitive.get("abstraction") or "").lower()
    if abstraction == "abstract" or "systems thinker" in implications:
        axes["construal"] = 5
    elif abstraction == "concrete":
        axes["construal"] = 1

    # time_horizon ← construal. Never asked directly; the registry says as
    # much ("derived from construal"), and asking it separately would invite
    # the two to disagree.
    if "construal" in axes:
        axes["time_horizon"] = axes["construal"]

    # information_depth ← response_length, pulled back by an explicit rule.
    # Registry polarity: 1 = detailed, 5 = high-level concept. "concise"
    # alone would say 5, but "a short vague answer is worse than a slightly
    # longer precise one" is a stated constraint, so concision stops at 4.
    length = str(comm.get("response_length") or "").lower()
    if length in ("concise", "short"):
        axes["information_depth"] = 4
    elif length in ("long", "thorough", "detailed"):
        axes["information_depth"] = 1

    # regulatory_focus ← decision_style.risk_tolerance
    risk = str(decision.get("risk_tolerance") or "").lower()
    axes["regulatory_focus"] = {"low": 1, "moderate": 3, "high": 5}.get(risk, 0)
    if not axes["regulatory_focus"]:
        del axes["regulatory_focus"]

    return {k: v for k, v in axes.items() if k not in UNMEASURABLE_AXES}


def sender_block(profile: dict) -> str:
    """The SENDER section of the translate prompt, in axis coordinates.

    Empty string when the profile evidences nothing — an empty heading tells
    the model there is a sender it knows nothing about, which is worse than
    silence.
    """
    axes = sender_axes(profile)
    prefs = sender_format_preferences(profile)
    comm = _dict(profile, "communication")

    lines: list[str] = []
    if comm.get("formality"):
        lines.append(f"- formality: {comm['formality']}")
    if comm.get("style"):
        lines.append(f"- style: {comm['style']}")
    if comm.get("response_length"):
        lines.append(f"- response length: {comm['response_length']}")
    if prefs:
        lines.append(f"- format preferences: {', '.join(prefs)}")
    if axes:
        rendered = ", ".join(f"{a} {axes[a]}/5" for a in sorted(axes))
        lines.append(f"- axes: {rendered}")
        unmeasured = sorted(UNMEASURABLE_AXES - {"certainty", "risk_framing"})
        lines.append(
            f"- not measured for the sender: {', '.join(unmeasured)} — treat "
            "as unknown, not as neutral."
        )
    if not lines:
        return ""
    return "\n".join(["## SENDER (the user)", *lines])


__all__ = [
    "UNMEASURABLE_AXES",
    "sender_axes",
    "sender_block",
    "sender_format_preferences",
]
