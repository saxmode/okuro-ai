# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: ONE source of truth for the brand ids the A4 board kit accepts. The set
#   used to be spelled out as a literal in six places (deck_adapter, deck_store,
#   gallery.js, gate_harness, calibrate_heights, the two proof runners) and every
#   copy named the same customers. It is derived from the height model instead —
#   a brand is accepted exactly when the kit is calibrated for it — and extended
#   by configuration for brands a user adds themselves.
# index:
#   DEFAULT_BRAND / a4_brands / is_a4_brand / normalise_brand
# AGENT_HEADER_END -->
"""Which brands the A4 board kit renders.

The calibrated set lives in ``kit/board/height-model.json`` (``brands``): those
are the ids that have per-brand height coefficients, so those are the ids the
solver can size without a render. okuro ships its own brand plus two neutral
fixture brands that exist to prove the kit themes across a dark sans and a light
sans — no customer identity is compiled in.

A user's own brand is *their* data and never ships here. To let one through, set
``OKURO_PRISM_BRANDS`` to a comma-separated list; those ids are accepted in
addition to the calibrated set and fall back to the model's pooled coefficients.
"""
from __future__ import annotations

import os
from functools import lru_cache

from okuro.resolve import coerce

#: Used whenever a caller supplies no brand, or one that is not accepted.
DEFAULT_BRAND = "okuro"

_ENV_VAR = "OKURO_PRISM_BRANDS"


def _extra() -> frozenset[str]:
    raw = os.environ.get(_ENV_VAR, "")
    return frozenset(b.strip() for b in raw.split(",") if b.strip())


@lru_cache(maxsize=1)
def _calibrated() -> frozenset[str]:
    """Brand ids the shipped height model carries coefficients for."""
    from okuro.prism.solver.heights import _model  # local: keeps import cost lazy

    brands = _model().get("brands") or []
    return frozenset(str(b) for b in brands) | {DEFAULT_BRAND}


def a4_brands() -> frozenset[str]:
    """Every brand id the A4 deck accepts — calibrated set + configured extras."""
    return _calibrated() | _extra()


def is_a4_brand(brand: str | None) -> bool:
    return bool(brand) and brand in a4_brands()


def normalise_brand(brand: str | None) -> str:
    """A brand id the deck can carry — the caller's, or ``okuro`` if unknown.

    THE SUBSTITUTION IS ANNOUNCED, and that is not decoration. Each calibrated
    brand carries its own character-width coefficient (okuro 10.285, northwind
    8.722, meridian 8.316), so falling through to ``okuro`` is an 18% error in
    every height estimate the solver makes — enough to move a page break.
    Before 2026-08-08 this returned the default with no signal anywhere, which
    is exactly how the re-keying of the height model went unnoticed.
    """
    return coerce(brand, a4_brands(), DEFAULT_BRAND, what="prism.brand")


__all__ = ["DEFAULT_BRAND", "a4_brands", "is_a4_brand", "normalise_brand"]
