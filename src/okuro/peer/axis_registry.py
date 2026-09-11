# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.axis_registry — THE canonical description of every cognitive
#   axis. One typed entry per axis (id, poles, polarity, scope, default,
#   usage, consumer operation). Every other surface — the Pydantic gate, the
#   MCP contract, the typed accessor, the labels — is GENERATED from here.
# index: Usage | Axis | AXES | AXIS_BY_ID | axis_ids | axis_labels |
#   axis_defaults | axis | deprecated_axes
# AGENT_HEADER_END -->
"""The cognitive axis registry.

A Python module, not a DB table — deliberately. The axis set changes at the
speed of research, not at the speed of user input, and every consumer of it
is Python. As a module it is versioned in git, needs no migration, can be
imported by the Pydantic generator at class-creation time, and rolls back
with `git revert`. A table would buy runtime mutability nobody has asked
for and cost a migration, a cache and a startup dependency.

WHY A REGISTRY AT ALL. The axis vocabulary was previously restated by hand
in at least five places — SLIDER_NAMES, SLIDER_LABELS, the Pydantic
`Sliders` model, the MCP input schema, and every role-default dict. Each
mirror drifted independently: the MCP schema declared 8 of 17, two
docstrings claimed 11 and 8, and a consumer read an axis name that has
never existed. Restatement IS the defect. Everything here has exactly one
definition and is derived everywhere else.

FIELD NAMING IS A FIREWALL CONSTRAINT, not a style choice. The PII denylist
in ``cognitive_profile`` strips any dict key with a segment in
{name, email, phone, address, handle, organization, org, company, employer,
contact} — so an entry field called ``name`` would be silently deleted from
every LLM projection. Hence ``axis_id`` / ``label_low`` / ``label_high``.
``test_axis_registry.py`` asserts that property rather than trusting it.

ORDER IS LOAD-BEARING. Axes are APPENDED, never inserted: stored Mode-C
posterior vectors are positional, so re-ordering silently reinterprets
saved data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# What a consumer is expected to DO with an axis it does not understand.
#   required       — a renderer that cannot honour it must refuse (fail-closed)
#   preferred      — honour when able; degrade quietly otherwise
#   optionally_use — a hint, safe to ignore entirely
#   prohibited     — present for back-compat; must NOT drive output
#
# Modelled on ISO/IEC 24751 AccessForAll, where the same four-way distinction
# separates "the user cannot use this without it" from "nice to have".
Usage = Literal["required", "preferred", "optionally_use", "prohibited"]


@dataclass(frozen=True)
class Axis:
    """One cognitive axis, completely described."""

    axis_id: str
    label_low: str          # what pole 1 means
    label_high: str         # what pole 5 means
    default: int            # neutral fill when unmeasured
    usage: Usage
    operation: str          # what a consumer does with it — prose, for humans
    scope: str = "global"   # "global" | "per_area" (jargon has area overrides)
    deprecated_by: str | None = None   # axis that absorbed this one


# Appended, never inserted. See the module docstring.
AXES: tuple[Axis, ...] = (
    Axis("information_depth", "detailed", "high-level concept", 3, "required",
         "sizes the structure — section/slide count, level ceiling"),
    Axis("format", "prose", "tables / bullets", 3, "preferred",
         "block type for body content"),
    Axis("decision_framing", "options first", "recommendation first", 3, "preferred",
         "order of the ask relative to its alternatives"),
    Axis("time_horizon", "tactical", "strategic", 3, "optionally_use",
         "framing altitude; derived from construal"),
    Axis("risk_framing", "upside first", "downside first", 3, "prohibited",
         "superseded — regulatory_focus carries this",
         deprecated_by="regulatory_focus"),
    Axis("jargon", "layman", "expert", 3, "required",
         "vocabulary floor; per-area overrides win where present",
         scope="per_area"),
    Axis("lead_with", "context", "answer", 4, "preferred",
         "whether a TLDR precedes the body; derived from NfC + closure"),
    Axis("pace", "one deep thread", "parallel scan", 3, "preferred",
         "bullet density per section"),
    Axis("certainty", "caveated / probabilistic", "definitive / assertive", 3,
         "prohibited", "superseded — decision_framing carries this",
         deprecated_by="decision_framing"),
    Axis("rational", "low analysis reliance", "high analysis reliance", 3,
         "preferred", "weight on explicit reasoning and quantitative support"),
    Axis("experiential", "low intuition reliance", "high intuition reliance", 3,
         "optionally_use", "weight on narrative and concrete example"),
    Axis("need_for_cognition", "just the conclusion", "the full reasoning", 3,
         "required", "how much justification survives compression"),
    Axis("density", "one idea at a time", "dense / packed", 3, "required",
         "chunking — ideas per slide or paragraph"),
    Axis("construal", "concrete / how", "abstract / why", 3, "preferred",
         "steps-vs-strategy framing; also derives time_horizon"),
    Axis("regulatory_focus", "guard against risk", "chase the opportunity", 3,
         "preferred", "gain-frame vs loss-frame of the same fact"),
    Axis("numeracy", "low numeracy (needs gist)", "high numeracy (fluent with numbers)",
         3, "required", "raw numbers vs gist; drives quant_render_policy"),
    Axis("graph_literacy", "low graph literacy", "fluent chart reader", 3,
         "required", "chart vs table vs sentence; drives quant_render_policy"),
)

AXIS_BY_ID: dict[str, Axis] = {a.axis_id: a for a in AXES}


def axis_ids() -> tuple[str, ...]:
    """Canonical axis names, in canonical order."""
    return tuple(a.axis_id for a in AXES)


def axis_labels() -> dict[str, tuple[str, str]]:
    """``{axis_id: (label_low, label_high)}``."""
    return {a.axis_id: (a.label_low, a.label_high) for a in AXES}


def axis_defaults() -> dict[str, int]:
    """``{axis_id: neutral default}``.

    Defaults are ints, never None — a decision, recorded here so it is not
    silently re-opened. Optional[int] = None would be the more honest
    representation of "never measured", but the firewall dumps the profile
    with ``exclude_none=True``, so an unmeasured axis would VANISH from
    every consumer's dict rather than arriving as a neutral. The honesty
    signal lives in ``slider_confidence`` (an unmeasured axis is rated 0.4
    and rendered "(prior)"), which is the right place for it: present but
    labelled beats absent and unexplained.
    """
    return {a.axis_id: a.default for a in AXES}


def axis(axis_id: str) -> Axis:
    """One axis entry. Raises on an unknown id — never returns a stand-in."""
    try:
        return AXIS_BY_ID[axis_id]
    except KeyError:
        raise ValueError(
            f"Unknown slider axis: {axis_id!r}. Known: {', '.join(axis_ids())}."
        ) from None


def deprecated_axes() -> dict[str, str]:
    """``{deprecated_axis: the axis that absorbed it}``.

    Retained for back-compat with stored rows; ``usage='prohibited'`` says
    they must not drive output.
    """
    return {a.axis_id: a.deprecated_by for a in AXES if a.deprecated_by}


__all__ = [
    "Axis",
    "Usage",
    "AXES",
    "AXIS_BY_ID",
    "axis",
    "axis_defaults",
    "axis_ids",
    "axis_labels",
    "deprecated_axes",
]
