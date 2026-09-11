# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides SVG animation emitters for various animation types
# index:
#   def emit_animation
#   def _attach
#   def _pulse
#   def _drift
#   def _phase
#   def _advance
#   def _orbit
#   def _shuffle
# AGENT_HEADER_END -->
"""Animation emitters. SMIL-based so the SVG is fully self-contained."""
from __future__ import annotations
from .scene import (
    Animation, PulseAnim, DriftAnim, PhaseAnim, AdvanceAnim, OrbitAnim,
    ShuffleAnim,
)


def emit_animation(a: Animation) -> str:
    if isinstance(a, PulseAnim):
        return _pulse(a)
    if isinstance(a, DriftAnim):
        return _drift(a)
    if isinstance(a, PhaseAnim):
        return _phase(a)
    if isinstance(a, AdvanceAnim):
        return _advance(a)
    if isinstance(a, OrbitAnim):
        return _orbit(a)
    if isinstance(a, ShuffleAnim):
        return _shuffle(a)
    return ""


def _attach(target: str, child_xml: str) -> str:
    # Inject the animation into the target group via a CSS class hook OR
    # use animateTransform with xlink. Since we wrap each primitive in <g id=...>,
    # we use a separate <g> with transform pivot — simplest is animateTransform
    # nested via a script-free pattern: prefix each with target lookup.
    # We emit a <use> reference + the animateTransform inside a wrapping <g>.
    return (
        f'<g><animateTransform xlink:href="#{target}" '
        f'attributeName="transform" attributeType="XML" '
        f'type="{child_xml}" additive="sum"/></g>'
    )


def _pulse(a: PulseAnim) -> str:
    return (
        f'<g>'
        f'<animateTransform xlink:href="#{a.target}" attributeName="transform" '
        f'type="scale" values="{a.min_scale};{a.max_scale};{a.min_scale}" '
        f'dur="{a.period_s}s" repeatCount="indefinite" additive="sum"/>'
        f'</g>'
    )


def _drift(a: DriftAnim) -> str:
    return (
        f'<g>'
        f'<animateTransform xlink:href="#{a.target}" attributeName="transform" '
        f'type="translate" values="0,0;{a.dx},{a.dy};{-a.dx},{-a.dy*0.6};0,0" '
        f'dur="{a.period_s}s" repeatCount="indefinite" additive="sum"/>'
        f'</g>'
    )


def _phase(a: PhaseAnim) -> str:
    # Visual phase shift via horizontal translate on the wave group.
    # We use the wavelength implicitly via a 2*pi cycle = period_s.
    return (
        f'<g>'
        f'<animateTransform xlink:href="#{a.target}" attributeName="transform" '
        f'type="translate" values="0,0;-280,0" '
        f'dur="{a.period_s}s" repeatCount="indefinite" additive="sum"/>'
        f'</g>'
    )


def _advance(a: AdvanceAnim) -> str:
    return (
        f'<g>'
        f'<animateTransform xlink:href="#{a.target}" attributeName="transform" '
        f'type="translate" values="0,0;{a.distance_px},0;0,0" '
        f'dur="{a.period_s}s" repeatCount="indefinite" additive="sum"/>'
        f'</g>'
    )


def _orbit(a: OrbitAnim) -> str:
    return (
        f'<g>'
        f'<animateTransform xlink:href="#{a.target}" attributeName="transform" '
        f'type="rotate" from="0" to="360" '
        f'dur="{a.period_s}s" repeatCount="indefinite" additive="sum"/>'
        f'</g>'
    )


def _shuffle(a: ShuffleAnim) -> str:
    """Quick chaotic micro-displacements -- the visual signature of scrambling."""
    j = a.jitter
    # 8 keyframes of pseudo-random offsets, no easing
    keys = [(0,0), (j*0.6,-j*0.4), (-j*0.5,j*0.7), (j*0.3,j*0.5),
            (-j*0.7,-j*0.2), (j*0.2,-j*0.6), (-j*0.3,j*0.3), (0,0)]
    values = ";".join(f"{x:.1f},{y:.1f}" for x,y in keys)
    return (
        f'<g>'
        f'<animateTransform xlink:href="#{a.target}" attributeName="transform" '
        f'type="translate" values="{values}" '
        f'dur="{a.period_s}s" repeatCount="indefinite" additive="sum" '
        f'calcMode="discrete"/>'
        f'</g>'
    )
