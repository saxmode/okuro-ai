# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: prism — solve each authored ladder level with the v4 free-composition
#   SOLVER and emit the SAME kit HTML the gallery renders. These composed
#   fragments are what the deck2 viewer embeds INSTEAD of the 12-archetype
#   downcast (to_deckdoc._archetype_for) — so the crafted 37-component kit that
#   looks solid in the gallery is what actually ships in a deck.
# index: compose_ladder
# AGENT_HEADER_END -->
"""Wire the v4 solver into the live deck path.

The authoring ladder already builds the solver's ``SlidePlan``s (ladder.plans) and
carries the chosen component per unit — the pipeline just never called ``solve()``.
This module closes that gap: for each level it runs the real beam-search solve and
renders the winning placement to an embeddable kit-HTML fragment (``css_base=None``),
plus the B variant for A/B swap. No re-render, no cross-language drift — the exact
code the gallery + contract test verify.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from okuro.prism.solver.render import compose_slide_html
from okuro.prism.solver.solve import solve

_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[a-z0-9]{4,}")

#: A composed cell is FAITHFUL when at least this share of its rendered content
#: words also appear in the claims it was built from. Locally declared and tuned
#: on the first end-to-end deck (2026-08-02), not borrowed: on that run faithful
#: cells scored 0.43-1.00 and fabricated ones 0.00-0.26, so the band is wide and
#: 0.35 sits in the middle of it. Re-tune with scripts/prism_corpus_baseline.py
#: if the corpus moves, and say so when you do.
FIDELITY_FLOOR = 0.35


def _words(s: str) -> set[str]:
    return set(_WORD.findall(s.lower()))


def cell_fidelity(html: str, plan: Any, claims: dict) -> float:
    """Share of the rendered cell's content words that come from its own claims.

    This is the assertion the pipeline never had. Every other check is structural
    — shape fit, focal count, reading order, overflow — so a component that
    renders its gallery fixture instead of the document passes all of them. On
    the first real deck that was 18 of 37 cells.

    Returns 1.0 for an empty render (nothing was claimed, nothing is wrong).
    """
    rendered = _words(_TAG.sub(" ", html))
    if not rendered:
        return 1.0
    src: set[str] = set()
    for unit in getattr(plan, "units", []) or []:
        for cid in getattr(unit, "claim_ids", []) or []:
            claim = claims.get(cid)
            if claim is None:
                continue
            src |= _words(getattr(claim, "text", "") or "")
            for row in getattr(claim, "items_content", ()) or ():
                src |= _words(" ".join(str(x) for x in row))
    return len(rendered & src) / len(rendered)

# Deterministic beam tie-break seed — the pipeline forbids Date/random, and a fixed
# seed makes a rebuild of the same ladder reproduce the same composition.
_SEED = 20260724

# A measure_fn matches solve()'s: (placement, plan, claims, brand) -> MeasureReport.
MeasureFn = Callable[..., Any]


def compose_ladder(
    ladder: Any,
    brand: str,
    *,
    measure_fn: Optional[MeasureFn] = None,
    seed: int = _SEED,
) -> dict[str, dict[str, Any]]:
    """Solve every level of ``ladder`` and return composed kit fragments.

    Returns ``{level: {"html", "altHtml", "family", "component_ids", "overflow"}}``.
    ``html`` is the winning placement as a wrapperless ``.composed-slide`` fragment;
    ``altHtml`` is the B variant (or None). ``measure_fn`` (from ``serving_kit()``)
    enables the render-measure guard — without it the solver ships the estimated-best
    layout un-measured, so callers that care about overflow MUST pass one.
    """
    out: dict[str, dict[str, Any]] = {}
    for level, plan in ladder.plans.items():
        res = solve(plan, ladder.claims, ladder.family, brand=brand,
                    seed=seed, measure_fn=measure_fn)
        html = compose_slide_html(res.a.placement, plan, ladder.claims, brand, None)
        alt = (compose_slide_html(res.b.placement, plan, ladder.claims, brand, None)
               if res.b is not None else None)
        measured = getattr(res, "measured", None)
        out[level] = {
            "html": html,
            "altHtml": alt,
            "family": ladder.family,
            "component_ids": [u.component for u in plan.units],
            "overflow": bool(measured.overflow) if measured is not None else None,
            "fidelity": round(cell_fidelity(html, plan, ladder.claims), 3),
        }
    return out
