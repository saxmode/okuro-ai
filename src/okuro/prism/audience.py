# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Audience-driven MODULE SELECTION — map a recipient's cognitive
#   slider vector to a preferred/avoided rich-block palette (leapfrog #1).
# index:
#   MODULE_AFFINITY
#   def _signal
#   def score_modules
#   def module_policy
#   def policy_prompt_clause
# AGENT_HEADER_END -->
"""okuro·prism audience module policy — the deepest recipient differentiator.

``resolve_depth_default`` (generate.py) already tunes DEPTH per recipient, and
``retailor`` tunes WORDING. This module tunes the third axis: *which MODULE
TYPES appear at all*. Two viewers of the same doc should not merely read the
same blocks reworded — an engineer should meet code/spec/graph where a CEO
meets stat/timeline/cta for the very same content.

Design (systematic, not per-person — DP10): every audience-discriminating block
type carries an AFFINITY vector over the canonical cognitive sliders
(``peer.cognitive_profile.SLIDER_NAMES``). A recipient's 1-5 slider vector is
projected to signed deviations (3 → 0, pole → ±1); each module's score is the
weighted dot-product, normalised to [-1, 1]. Modules above/below a threshold
become the PREFER / AVOID palette injected into the generation + enrichment
prompts. A neutral (all-3) or absent profile scores every module 0 → empty
policy → generation is unchanged (the feature is strictly additive).

Structural blocks (heading/image) and the always-content-driven prose spine
(text/diagram/callout) are never gated — only the rich data/narrative modules
the enrichment pass chooses between.

Slider pole convention (see cognitive_profile.SLIDER_LABELS): value 5 = the
RIGHT label, value 1 = the LEFT label. A POSITIVE weight means the RIGHT pole
favours the module; NEGATIVE means the LEFT pole favours it. E.g. ``code`` has
``information_depth: -1.0`` because information_depth 1 = "detailed" (code's
audience) and 5 = "high-level concept" (not code's audience).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Audience-KIND projection policy — the SINGLE source (DP10) shared by prism's
# story projection (prism.projection) and resonance's PCO projection
# (resonance.projection.project_pco). Both SELECT the claim SET for a reader and
# reframe it; holding the policy in one place stops the two paths diverging.
#   * CONCISE_KINDS  — readers who get a trimmed set (weight="nice" dropped).
#   * KIND_CONSTRUAL — the altitude a kind reads at (reframe the opposite one).
#   * KIND_FRAME     — regulatory-focus framing (promotion→gain, guard→loss).
# Values MUST stay aligned with resonance.pco (WEIGHTS/CONSTRUALS) so a future
# convergence of the two projection engines is a pure relocation.
# ---------------------------------------------------------------------------
CONCISE_KINDS = frozenset({"board", "exec"})
KIND_CONSTRUAL = {"board": "why", "exec": "why", "technical": "how"}
KIND_FRAME = {"board": "gain", "exec": "gain", "technical": "loss"}

# The audience KINDS audience_kind() classifies into — the closed vocabulary a
# claim's ``audiences`` tag draws from (mirrors resonance.pco audience values).
AUDIENCE_KINDS = frozenset({"board", "exec", "technical", "general"})

# Per-module affinity over cognitive sliders. Weight sign follows the pole
# convention above; magnitude is the axis's relative pull on this module.
# Only modules whose fit genuinely varies by cognition are listed — the rest
# stay purely content-driven.
MODULE_AFFINITY: dict[str, dict[str, float]] = {
    # headline numbers — concept-level skimmers, parallel scanners, readers who
    # want the conclusion not the derivation.
    "stat": {"information_depth": 0.9, "pace": 0.5, "need_for_cognition": -0.5},
    # quantitative shape — analytical readers who reason from the data AND can
    # read a chart (a low-graph-literacy reader gets an icon-array/gist instead).
    "chart": {"rational": 0.9, "graph_literacy": 0.6, "format": 0.4, "need_for_cognition": 0.3},
    # count-based icon array ("12 of 100") — the low-numeracy / low-graph-literacy
    # reader parses a discrete count far better than a bare % or a chart (Reyna,
    # Peters, Garcia-Retamero; icon arrays are Pareto-safe for high-numeracy too).
    "frequency": {"numeracy": -0.9, "graph_literacy": -0.3, "need_for_cognition": -0.3},
    # attribute/data grid — structure-lovers, analytical, dense-tolerant.
    "table": {"format": 1.0, "rational": 0.5, "density": 0.4},
    # ✓/✗/~ comparison grid — analytical + structured + expert; intuition-led
    # readers get less from a grid.
    "matrix": {"rational": 0.9, "format": 0.5, "jargon": 0.3, "experiential": -0.4},
    # budget/limit/proportion bars — concrete (how much), guard/limit framing,
    # structured.
    "meter": {"format": 0.6, "construal": -0.6, "regulatory_focus": -0.5},
    # same point reframed per audience — valued by intuitive, abstract,
    # multi-perspective readers.
    "lens": {"experiential": 0.7, "construal": 0.5, "need_for_cognition": 0.2},
    # roadmap / ordered beats — strategic, long-horizon readers.
    "timeline": {"time_horizon": 0.9, "construal": 0.3},
    # decision matrix — options-first deciders who want the full trade-space.
    "options": {"decision_framing": -1.0, "need_for_cognition": 0.5, "rational": 0.4},
    # before→after — fast scanners, concrete, structured.
    "compare": {"pace": 0.6, "format": 0.4, "construal": -0.3},
    # peer-item grid — parallel scanners; dense readers prefer a packed table.
    "cards": {"pace": 0.9, "format": 0.4, "density": -0.4},
    # tier/model spec cards — expert, detailed, analytical.
    "spec": {"jargon": 0.9, "information_depth": -0.7, "rational": 0.4},
    # fenced code / prompt — expert AND detail-seeking only.
    "code": {"jargon": 1.0, "information_depth": -1.0},
    # relationship/dependency map — analytical, expert, abstract (why/how it
    # connects), high reasoning appetite; concept-skimmers dislike it.
    "graph": {"rational": 0.7, "jargon": 0.5, "construal": 0.5, "graph_literacy": 0.4,
              "need_for_cognition": 0.5, "information_depth": -0.3},
    # numbered how-to pipeline — concrete, detailed, tactical.
    "steps": {"construal": -1.0, "information_depth": -0.5, "time_horizon": -0.3},
    # board-ask / next-step — recommendation-first, opportunity-framed readers.
    "cta": {"decision_framing": 0.8, "regulatory_focus": 0.4},
    # coverage grid — concrete, structured, guard-oriented (did we cover it).
    "checklist": {"format": 0.5, "construal": -0.4, "regulatory_focus": -0.4},
}

# Above this normalised score → PREFER; below its negation → AVOID.
_THRESHOLD = 0.30
# Keep the injected palette tight (prompt economy + a real signal, not a
# ranked list of everything).
_MAX_PREFER = 7
_MAX_AVOID = 6


def _signal(value: Any) -> float:
    """Project a 1-5 slider to a signed deviation in [-1, 1] (3 → 0, 5 → +1)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    v = max(1.0, min(5.0, v))
    return (v - 3.0) / 2.0


def score_modules(sliders: dict[str, Any]) -> dict[str, float]:
    """Score every affinity-listed module for a slider vector, normalised to
    [-1, 1] (dot-product ÷ total affinity magnitude). A neutral vector (all 3,
    or empty) scores every module exactly 0."""
    signals = {axis: _signal(sliders.get(axis)) for axis in
               {a for aff in MODULE_AFFINITY.values() for a in aff}}
    scores: dict[str, float] = {}
    for module, affinity in MODULE_AFFINITY.items():
        total = sum(abs(w) for w in affinity.values()) or 1.0
        scores[module] = sum(w * signals[axis] for axis, w in affinity.items()) / total
    return scores


def module_policy(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Map a PII-firewalled cognitive profile (``cognitive_profile_for_llm``
    output) to a module palette policy.

    Returns ``{"prefer": [...], "avoid": [...], "lead": str|None,
    "scores": {module: float}}``. An empty/neutral profile yields empty
    prefer/avoid lists (``lead`` None) — callers treat that as "no policy".
    """
    sliders = (profile or {}).get("sliders") or {}
    scores = score_modules(sliders)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    prefer = [m for m, s in ranked if s >= _THRESHOLD][:_MAX_PREFER]
    avoid = [m for m, s in reversed(ranked) if s <= -_THRESHOLD][:_MAX_AVOID]
    return {
        "prefer": prefer,
        "avoid": avoid,
        "lead": prefer[0] if prefer else None,
        "scores": scores,
    }


def policy_prompt_clause(policy: dict[str, Any] | None) -> str:
    """Render a policy into a prompt clause for the generation / enrichment
    passes. Returns "" when there's nothing to steer (no person, neutral
    profile) so the prompt is untouched in the default path."""
    if not policy:
        return ""
    prefer, avoid = policy.get("prefer") or [], policy.get("avoid") or []
    if not prefer and not avoid:
        return ""
    lines = [
        "AUDIENCE MODULE POLICY — this recipient's cognition favours specific "
        "MODULE TYPES for the same content. This chooses WHICH module renders a "
        "point (an engineer meets code/spec/graph where an executive meets "
        "stat/timeline/cta); it never changes WHICH points are covered.",
    ]
    if prefer:
        lines.append(
            "PREFER these module types wherever a section's content fits one: "
            + ", ".join(prefer) + "."
        )
    if avoid:
        lines.append(
            "AVOID these unless the content strongly demands them: "
            + ", ".join(avoid) + "."
        )
    lines.append(
        "Content shape still rules — never fabricate data or force a module "
        "the content doesn't support just to satisfy the palette."
    )
    return "\n".join(lines)


def audience_kind(profile: dict[str, Any] | None) -> Optional[str]:
    """Classify the recipient into a narrative archetype from their cognition:
    ``"technical" | "board" | "exec" | "general"`` (None if no profile).

    Primary split is jargon (an expert reader wants a technical structure
    regardless of altitude — a CTO reads problem→design→benchmarks, not an
    SCQA board arc). Among non-expert readers, concept-level + strategic →
    board; concept-level → exec; else general."""
    sliders = (profile or {}).get("sliders") or {}
    if not sliders:
        return None
    jargon = _signal(sliders.get("jargon"))
    depth = _signal(sliders.get("information_depth"))   # +1 = high-level concept
    horizon = _signal(sliders.get("time_horizon"))      # +1 = strategic
    if jargon >= 0.3:
        return "technical"
    if depth >= 0.3 and horizon >= 0.3:
        return "board"
    if depth >= 0.3:
        return "exec"
    return "general"


_NARRATIVE_CLAUSE: dict[str, str] = {
    "board": (
        "NARRATIVE STRUCTURE — GOVERNANCE: open with SCQA (the situation, the "
        "complication that changed, the decision it forces); lead with the "
        "single headline ASK; 2-4 supporting sections, each one decision/risk "
        "at oversight altitude; push methodology, raw data, and process detail "
        "down to the L4 level (the appendix). End on the ask, not a summary."
    ),
    "exec": (
        "NARRATIVE STRUCTURE — EXECUTIVE: BLUF — the answer first, 'so what' "
        "before 'what'. One insight + one recommendation per section, framed as "
        "an outcome (not a process). Keep it strategic; detail lives one level "
        "deeper for those who want it."
    ),
    "technical": (
        "NARRATIVE STRUCTURE — TECHNICAL: problem → constraints → design & "
        "tradeoffs → evidence/benchmarks. Assume domain fluency: skip "
        "motivational framing and business justification, use precise "
        "terminology, and go deep in the L3/L4 levels. EXPERTISE-REVERSAL "
        "— for a domain expert, redundant explanation ADDS cognitive load: OMIT "
        "the fundamentals they already know, STRIP restated context and "
        "step-by-step scaffolding, and spend the reclaimed space on depth, edge "
        "cases, failure modes, and the non-obvious tradeoffs."
    ),
    "general": (
        "NARRATIVE STRUCTURE — MIXED AUDIENCE: spell out the full SCQA (don't "
        "assume shared context), plain language, acronyms defined on first use. "
        "Progressive disclosure — headline the point first, put the specialist "
        "detail in deeper levels behind clear labels. SCAFFOLD for the non-expert "
        "reader: introduce each new term before using it, build from the familiar "
        "to the new, and KEEP the worked-through explanation (a novice learns from "
        "guidance, not from compression) — reserve compression for the deeper levels (L3/L4)."
    ),
}


def narrative_clause(profile: dict[str, Any] | None) -> str:
    """The per-audience narrative-structure directive for the generation prompt,
    or "" when there's no profile (neutral → the default assertion+BLUF arc)."""
    kind = audience_kind(profile)
    return _NARRATIVE_CLAUSE.get(kind or "", "")


_FRAMING_CLAUSE: dict[str, str] = {
    "promotion": (
        "MOTIVATIONAL FRAME — PROMOTION FIT: this reader is opportunity-oriented (a "
        "promotion focus). Frame the SAME facts around what there is to GAIN — growth, "
        "the advantage unlocked, the upside realised, the aspiration met; use eager, "
        "possibility-forward language and open sections with the opportunity. This is an "
        "emphasis match to the reader's motivation (Joyal-Desmarais 2022, r≈.20) — it NEVER "
        "changes which claims are true; do not overstate a gain or invent upside."
    ),
    "prevention": (
        "MOTIVATIONAL FRAME — PREVENTION FIT: this reader is security-oriented (a prevention "
        "focus). Frame the SAME facts around what is SAFEGUARDED — the risk avoided, "
        "reliability held, downside contained, obligations met, the cost of inaction; use "
        "vigilant, careful language and open sections with the risk managed. This is an "
        "emphasis match to the reader's motivation (Joyal-Desmarais 2022, r≈.20) — it NEVER "
        "changes which claims are true; do not manufacture a threat or overstate a risk."
    ),
}


def framing_clause(profile: dict[str, Any] | None) -> str:
    """Motivational (regulatory-focus) FRAMING directive for the generation passes
    — the evidence-ranked recipient lever (WS-4-p1): matching a reader's promotion
    (gain/opportunity) vs prevention (security/risk-avoidance) focus is a real
    persuasion effect (Joyal-Desmarais 2022, r≈.20), where raw gain/loss valence
    alone is not (r<.10). A frame/emphasis overlay only — claims stay immutable.
    "" for a neutral / absent profile (the default balanced framing is untouched)."""
    sliders = (profile or {}).get("sliders") or {}
    if not sliders:
        return ""
    reg = _signal(sliders.get("regulatory_focus"))   # +1 = opportunity/promotion, -1 = guard/prevention
    if reg >= 0.3:
        return _FRAMING_CLAUSE["promotion"]
    if reg <= -0.3:
        return _FRAMING_CLAUSE["prevention"]
    return ""


def quant_render_policy(profile: dict[str, Any] | None) -> dict[str, Any]:
    """How to render NUMBERS for this recipient's numeracy (WS-4-p2). A low-numeracy
    reader mis-reads a bare percentage; a low-graph-literacy reader mis-reads a chart.
    Both are handled by the SAME Pareto-safe move — a discrete count + a plain gist.

    Returns ``{"low_numeracy": bool, "low_graph_literacy": bool}`` (only the True
    keys), or ``{}`` for a numerate reader / neutral / absent profile (→ no clause,
    the numeric default path is untouched)."""
    sliders = (profile or {}).get("sliders") or {}
    if not sliders:
        return {}
    out: dict[str, Any] = {}
    if _signal(sliders.get("numeracy")) <= -0.3:            # 1-2 on a 1-5 axis
        out["low_numeracy"] = True
    if _signal(sliders.get("graph_literacy")) <= -0.3:
        out["low_graph_literacy"] = True
    return out


def quant_render_clause(policy: dict[str, Any] | None) -> str:
    """Render a quant_render policy into a numbers-rendering directive for the
    generation passes, or "" when the reader is numerate (default path untouched)."""
    if not policy:
        return ""
    lines = ["NUMERACY — render numbers for a reader who is not fluent with them:"]
    if policy.get("low_numeracy"):
        lines.append(
            "- state a probability / rate / risk as a DISCRETE COUNT via a `frequency` "
            "icon-array (\"12 of 100\", never a bare \"12%\"), and add a one-line plain-"
            "language GIST of what the number MEANS for the reader."
        )
    if policy.get("low_graph_literacy"):
        lines.append(
            "- this reader does not read charts fluently: prefer a `frequency` array, a "
            "single big `stat`, or a small table with a gist line over a chart; if a chart "
            "is truly the only fit, annotate the ONE datapoint that carries the point."
        )
    if len(lines) == 1:
        return ""
    lines.append("Never invent a denominator or a count the claims don't support.")
    return "\n".join(lines)


# How each axis aggregates across a group. Anything unlisted takes the mean —
# a neutral default for axes where no member is harmed by the middle.
#
#   MIN  — the least-capable member sets the floor, because overshooting LOSES
#          that reader entirely and undershooting costs the others almost
#          nothing (Pareto-safe: a gist + an icon array is still readable to
#          the numerate).
#   POLE — a genuinely bimodal group gets BOTH treatments, not their average;
#          the mean of "why" and "how" is a document that serves neither.
_AGGREGATE_MIN = ("jargon", "density", "numeracy", "graph_literacy")
_AGGREGATE_POLE = ("construal", "regulatory_focus")


def aggregate_members(
    vecs: list[dict[str, Any]] | None,
    usages: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """THE group-aggregation policy. One rule set, three former call sites.

    Three incompatible policies shipped simultaneously and which one applied
    depended on the entry point:

      * target_groups.resolve_audience  — MEAN of every axis
      * prism.audience.group_policy     — MIN + pole detection, with a
                                          docstring stating the mean is wrong
      * prism.composer.resolve_audience_layout — picked the single min-jargon
                                          MEMBER and used that member's WHOLE
                                          vector, a third semantics again

    Returns ``{"sliders": {...}, "policy": {...}}``:

      ``sliders`` — a merged vector, ALWAYS a dict. The return type is load-
        bearing: four consumers read ``resolve_audience()["sliders"]`` as a
        vector (handover/registry, prism/personas, and two sites in
        target_groups), so this shape cannot become the categorical knobs.
      ``policy``  — the categorical knobs (floors, dual/split flags). ``{}``
        for fewer than two profiled members, preserving group_policy's
        "not a group" contract exactly.

    A value a person marked ``required`` NEVER FLOORS. ``usages`` is the
    parallel list of their per-value declarations
    (``cognitive.value_usage``): one person saying "expert vocabulary is not
    a preference, it is a requirement" must survive the merge instead of
    being quietly lowered to the group's weakest member.

    That rule is about the PER-VALUE annotation, NOT the axis-level ``usage``
    in the registry — the registry says what a consumer must do with an axis
    in general and nothing about how a group merges. Conflating them makes
    ``jargon`` (axis-level "required") skip the very floor that protects the
    least-technical reader.

    A required value ABOVE the group floor is a genuine conflict — one
    reader needs what another cannot follow. It is surfaced in
    ``policy["usage_conflicts"]`` rather than silently resolved, because the
    honest answers are "split the audience" or "refuse", and neither is this
    function's call to make.
    """
    vecs = [v for v in (vecs or []) if isinstance(v, dict) and v]
    if not vecs:
        return {"sliders": {}, "policy": {}}
    usages = list(usages or [])

    axes: dict[str, list[float]] = {}
    # {axis: [values its owner declared non-negotiable]}
    required: dict[str, list[float]] = {}
    for i, vec in enumerate(vecs):
        declared = usages[i] if i < len(usages) and isinstance(usages[i], dict) else {}
        for axis, value in vec.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                axes.setdefault(axis, []).append(float(value))
                if declared.get(axis) == "required":
                    required.setdefault(axis, []).append(float(value))

    merged: dict[str, int] = {}
    conflicts: list[str] = []
    for axis, values in axes.items():
        if axis in _AGGREGATE_MIN:
            floor = min(values)
            must_hold = max(required.get(axis) or [floor])
            if must_hold > floor:
                conflicts.append(axis)
            merged[axis] = int(round(max(floor, must_hold)))
        elif axis in _AGGREGATE_POLE:
            # A split group keeps the neutral midpoint; `policy` carries the
            # instruction to render BOTH poles. A mean would look identical
            # to a genuinely neutral group, which is the information loss.
            merged[axis] = 3 if (max(values) - min(values)) >= 1.6 else \
                int(round(sum(values) / len(values)))
        else:
            merged[axis] = int(round(sum(values) / len(values)))

    policy = group_policy([{"sliders": v} for v in vecs])
    if conflicts and policy:
        # Only meaningful for an actual group; group_policy is {} below two
        # profiled members and must stay that way.
        policy["usage_conflicts"] = sorted(conflicts)
    return {"sliders": merged, "policy": policy}


def group_policy(members: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Resolve a heterogeneous audience GROUP into ONE deck's knobs — the group is
    NOT the average of member sliders (each knob has its own aggregation rule):

      * jargon / expertise → MIN (floor to the LEAST-technical member so everyone
        can follow; a wrong-too-high floor loses a reader). + progressive
        disclosure when the group spans novice→expert (experts' depth goes deeper,
        never on the surface — Kalyuga expertise-reversal forbids scaffolding an
        expert on the glance).
      * density / WM load → MIN (the lowest tolerance sets the surface; overload
        is unrecoverable for that member).
      * construal → DUAL when members span the poles (a why-section for the
        strategic reader AND a how-section for the implementer).
      * regulatory focus → SPLIT/neutral when mixed (a wrong single gain/loss
        frame is worse than a neutral one).

    ``members`` = list of ``{"sliders": {...}}`` PII-firewalled profiles. Fewer
    than 2 profiled members → ``{}`` (not a group; caller treats as no policy)."""
    vecs = [m["sliders"] for m in (members or [])
            if isinstance(m, dict) and isinstance(m.get("sliders"), dict) and m["sliders"]]
    if len(vecs) < 2:
        return {}
    def sig(axis: str) -> list[float]:
        return [_signal(v.get(axis)) for v in vecs]
    jarg, dens, constr, reg = sig("jargon"), sig("density"), sig("construal"), sig("regulatory_focus")
    num, gl = sig("numeracy"), sig("graph_literacy")
    jmin = min(jarg)
    floor_jargon = "plain" if jmin <= -0.3 else "technical" if jmin >= 0.3 else "balanced"
    return {
        "n": len(vecs),
        "floor_jargon": floor_jargon,
        "floor_density": "dense" if min(dens) >= 0.3 else "airy",
        "dual_construal": max(constr) >= 0.3 and min(constr) <= -0.3,
        "split_regulatory": max(reg) >= 0.3 and min(reg) <= -0.3,
        # experts + novices in the same room → keep the surface simple, depth deeper.
        "progressive_disclosure": (max(jarg) - min(jarg)) >= 0.8,
        # numeracy / graph-literacy → MIN: the least-numerate member sets how numbers
        # render (an icon-array + gist is Pareto-safe, so it costs the numerate nothing).
        "quant_floor": bool(min(num) <= -0.3),
        "graph_floor": bool(min(gl) <= -0.3),
    }


def group_policy_clause(policy: dict[str, Any] | None) -> str:
    """Render a group_policy into a directive for the strategist/writer, or "" when
    there is no group (so the single-recipient path is untouched)."""
    if not policy or policy.get("n", 0) < 2:
        return ""
    lines = [
        f"GROUP AUDIENCE ({policy['n']} members) — resolve conflicting needs into ONE "
        "deck by the FLOOR, not the average:",
        f"- jargon: pitch to the LEAST-technical member ({policy['floor_jargon']}) so "
        "every member can follow.",
    ]
    if policy.get("progressive_disclosure"):
        lines.append(
            "- the group spans novice→expert: keep the SURFACE (L1/L2) simple and "
            "push the experts' depth into the deeper levels — progressive disclosure, never "
            "expert detail on the L1 cover."
        )
    if policy.get("dual_construal"):
        lines.append(
            "- the group spans strategic and hands-on readers: cover BOTH the why "
            "(strategic) AND the how (implementation); do not pick a single altitude."
        )
    if policy.get("split_regulatory"):
        lines.append(
            "- the group is mixed on risk appetite: frame NEUTRALLY (or both ways); never "
            "commit to a single gain- or loss-frame."
        )
    if policy.get("floor_density") == "airy":
        lines.append("- keep it airy: the lowest working-memory tolerance sets the density.")
    if policy.get("quant_floor") or policy.get("graph_floor"):
        lines.append(
            "- numbers: the least-numerate member sets how numbers render — state a "
            "probability / rate as a `frequency` icon-array (\"12 of 100\", not a bare "
            "\"12%\") with a one-line plain gist"
            + (", and avoid charts a non-fluent reader can't parse" if policy.get("graph_floor") else "")
            + " (an icon-array + gist costs the numerate reader nothing)."
        )
    return "\n".join(lines)


def layout_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Derive layout-engine knobs from a cognitive profile (Phase B — audience-
    aware arrangement). Feeds ``prism.layout.derive_layouts`` so the SAME content
    arranges differently per viewer:

      * ``band_lead`` — promote stat/meter KPI bands to the TOP of a rung (the
        board/exec "headline numbers first" hero treatment). True for
        concept-level, non-expert readers (they read for the outcome, not the
        derivation); an engineer keeps blocks in narrative order instead.
      * ``density`` — "dense" (more tiles per row, tighter) for high-density /
        fast-scan readers; "airy" otherwise. Advisory for the renderer.

    Empty/neutral profile → ``{}`` (no knobs → content-only packing, unchanged)."""
    sliders = (profile or {}).get("sliders") or {}
    if not sliders:
        return {}
    depth = _signal(sliders.get("information_depth"))   # +1 = high-level concept
    jargon = _signal(sliders.get("jargon"))             # +1 = expert
    density = _signal(sliders.get("density"))
    pace = _signal(sliders.get("pace"))
    out: dict[str, Any] = {}
    if depth >= 0.3 and jargon <= 0.0:
        out["band_lead"] = True
    out["density"] = "dense" if (density >= 0.3 or pace >= 0.3) else "airy"
    return out


__all__ = [
    "CONCISE_KINDS",
    "KIND_CONSTRUAL",
    "KIND_FRAME",
    "AUDIENCE_KINDS",
    "MODULE_AFFINITY",
    "score_modules",
    "module_policy",
    "policy_prompt_clause",
    "layout_profile",
    "audience_kind",
    "narrative_clause",
    "framing_clause",
    "quant_render_policy",
    "quant_render_clause",
    "group_policy",
    "group_policy_clause",
]
