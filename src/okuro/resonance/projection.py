# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Content-IR PROJECTION — select & reframe a PCO's claims for one
#   audience kind BEFORE it collapses to a content brief. The missing SELECTION
#   layer: board and technical readers get different claim SETS, not just words.
# index:
#   def _serves
#   def _directive_for
#   def project_pco
# AGENT_HEADER_END -->
"""Phase E — the projection layer.

``to_content_brief`` dumps every claim regardless of reader. Rewording ("make
it executive") changes STYLE, not SELECTION/FRAMING/ALTITUDE — the actual
relevance levers. Projection sits between the media-neutral PCO and the brief:
given the resolved audience KIND it SELECTS the claims that serve that reader
(dropping the rest), and attaches a phrasing DIRECTIVE (gain/loss frame,
why/how altitude) so the generator reframes rather than reinvents.

Pure + total: ``kind=None`` (no profiled audience) returns the PCO unchanged, so
every pre-E path and every neutral-audience render is byte-for-byte identical.
Untagged claims are universal — a PCO with no Phase-E tags projects to itself.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .pco import PCO, Claim, NarrativeBeat

# Concise readers get a trimmed claim set (fewer, higher-signal). Expansive
# readers keep everything. Derived from audience KIND (prism.audience.audience_kind).
_CONCISE_KINDS = frozenset({"board", "exec"})
# The altitude each kind reads at — used to reframe a claim tagged with the
# opposite construal.
_KIND_CONSTRUAL = {"board": "why", "exec": "why", "technical": "how"}
# Regulatory-focus framing per kind: promotion-oriented readers hear the gain,
# guard-oriented readers hear the loss. Neutral kinds get no frame spin.
_KIND_FRAME = {"board": "gain", "exec": "gain", "technical": "loss"}


def _serves(claim: Claim, kind: str) -> bool:
    """A claim serves a kind when it is universal (no audience tags) or lists it."""
    return not claim.audiences or kind in claim.audiences


def _directive_for(claim: Claim, kind: str) -> Optional[str]:
    """Build the phrasing directive for this claim under this audience kind.

    Only emits when the claim carries the relevant reframe material AND it
    differs from how the claim is already stated — so untagged claims stay
    quiet (additive) and no redundant instruction is spent.
    """
    parts: list[str] = []
    frame_key = _KIND_FRAME.get(kind)
    if frame_key and claim.frame and claim.frame.get(frame_key):
        parts.append(f"frame as {frame_key}: {claim.frame[frame_key]}")
    target = _KIND_CONSTRUAL.get(kind)
    if target and claim.construal and claim.construal != target:
        altitude = "outcome / strategic" if target == "why" else "mechanism / how"
        parts.append(f"phrase at the {target} altitude ({altitude}), not {claim.construal}")
    return "; ".join(parts) or None


def project_pco(pco: PCO, kind: Optional[str], *, concise: Optional[bool] = None) -> PCO:
    """Project ``pco`` onto one audience ``kind`` → a new, audience-fit PCO.

    * ``kind=None`` → unchanged (no profiled audience; neutral path preserved).
    * SELECT: keep claims that serve ``kind``; for concise readers also drop
      ``weight="nice"`` claims so the SET shrinks (fewer, load-bearing points).
    * REFRAME: attach a per-claim phrasing directive (gain/loss, why/how).
    * Beats that lose all their claims are dropped; order/priority preserved.

    Returns a fresh PCO — the input and its claims are never mutated (the same
    PCO can be projected for many audiences, e.g. ``render_matrix``).
    """
    if not kind:
        return pco
    if concise is None:
        concise = kind in _CONCISE_KINDS

    new_beats: list[NarrativeBeat] = []
    for beat in pco.beats:
        kept: list[Claim] = []
        for claim in beat.claims:
            if not _serves(claim, kind):
                continue
            if concise and claim.weight == "nice":
                continue
            kept.append(replace(claim, directive=_directive_for(claim, kind)))
        if kept:
            new_beats.append(replace(beat, claims=kept))

    return replace(pco, beats=new_beats)


__all__ = ["project_pco"]
