# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Specialist routing — pick the best LOCAL model for a task by
#          specialized_for × qualified × fits-VRAM. The apply-time payoff of the
#          "more pro = more specialists" vision: route each task to the small
#          domain-expert that beats a generalist in its niche.
# index:
#   def specialization_of
#   def best_specialist
# AGENT_HEADER_END -->
"""Route a task to the best-fit local specialist.

Scoring (high → low): exact domain match beats a general model beats a mismatch
(mismatches are excluded); a probe-qualified model (serving_ready) beats an
unproven one; among equals the smaller VRAM footprint wins (leave room for other
tenants). A model that doesn't fit the usable VRAM is never returned — the fit
signal is the analytic KV-aware estimate (bundle.vram_gb), not a guess.
"""

from __future__ import annotations

from typing import Optional


def specialization_of(bundle) -> Optional[str]:
    """The domain a bundle is specialised for, from its prompting block."""
    block = getattr(bundle, "prompting", None) or {}
    return block.get("specialized_for")


def _is_qualified(bundle) -> bool:
    q = getattr(bundle, "qualification", None) or {}
    return bool(q.get("serving_ready"))


def best_specialist(domain: str, bundles, *, usable_vram_gb: Optional[float] = None):
    """Best local bundle for ``domain``, or None if nothing suitable fits.

    A ``general`` model is an acceptable fallback (score 1) when no exact
    specialist (score 2) is available; a model specialised for a DIFFERENT
    domain is excluded.
    """
    scored = []
    for i, b in enumerate(bundles):
        spec = specialization_of(b)
        if spec is None:
            continue
        match = 2 if spec == domain else (1 if spec == "general" else 0)
        if match == 0:
            continue
        if usable_vram_gb is not None and getattr(b, "vram_gb", 0) > usable_vram_gb:
            continue  # doesn't fit — never route to an OOM
        # (match, qualified, smaller-vram, stable index) — all high-is-better
        scored.append((match, 1 if _is_qualified(b) else 0, -float(getattr(b, "vram_gb", 0)), -i, b))
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][4]
