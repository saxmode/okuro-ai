# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 two-phase sizing (phase 1) — estimate a component's
#   rendered height in px WITHOUT rendering, by replaying the W1 height-model
#   feature_recipe (calibrate_heights.design_row) against the persisted per-
#   (component,brand) coefficients. The solver plans on these estimates; ONE real
#   render + measure-guard (solve.py) corrects the residual.
# index:
#   HeightModel / estimate_px / row_units / col_px
# AGENT_HEADER_END -->
"""Height estimation from the W1 calibrated model (v4.1 B2 phase 1).

W1 shipped ``kit/board/height-model.json`` with, per (component, brand), a
linear model ``height = coeffs . design_row(...)`` over a physically-meaningful
feature (rows x wrapped-lines), and the ``feature_recipe`` needed to compute that
feature offline. This module re-implements ``design_row`` + ``predict`` in pure
Python (no playwright/numpy import) so the beam solver can size candidates
without a browser — the whole point of two-phase sizing. It is a faithful replay
of ``kit/tests/calibrate_heights.py`` (design_row/cards_per_row/predict); if that
recipe changes, this must track it (guarded by test_solver's parity check against
the model's own reported errors).
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

_HEIGHT_JSON = (
    Path(__file__).resolve().parents[1] / "kit" / "board" / "height-model.json"
)


@lru_cache(maxsize=1)
def _model() -> dict[str, Any]:
    return json.loads(_HEIGHT_JSON.read_text())


def content_w_px() -> int:
    # feature_recipe.col_px constant: 1600 canvas - 2*64 padding.
    return 1472


def col_px(col_span: int) -> float:
    """col_span (of 12) -> content pixel width, per the recipe."""
    return round(col_span / 12 * content_w_px())


def row_unit_px() -> float:
    return float(_model().get("row_unit_px", 45.0))


def _cards_per_row(lo: dict[str, Any], colpx: float) -> int:
    kind = lo.get("layout", "stack")
    if kind in ("grid", "wrap"):
        gap = lo.get("gap_px", 24)
        return max(1, int((colpx + gap) // (lo["min_card_px"] + gap)))
    if kind == "grid3":
        return 3
    if kind == "columns2":
        return 2
    return 1


def _design_row(
    lo: dict[str, Any], items: int, chars: int, colpx: float,
    text_bearing: bool, char_w: float,
) -> list[float]:
    """Faithful replay of calibrate_heights.design_row (DP12 physics feature)."""
    if lo.get("layout") == "aspect":
        return [1.0, float(colpx)]
    cpr = _cards_per_row(lo, colpx)
    rows = float(lo["fixed_rows"]) if "fixed_rows" in lo else float(math.ceil(items / cpr))
    if not text_bearing or lo.get("layout") == "wrap":
        return [1.0, rows]
    cw = char_w * lo.get("font_px", 15) / 17.0
    kind = lo.get("layout", "stack")
    inset = lo.get("text_inset", 48)
    if kind == "grid":
        wrap_w = lo["min_card_px"] - inset
    elif kind == "grid3":
        wrap_w = colpx / 3 - inset
    elif kind == "columns2":
        wrap_w = colpx / 2 - inset
    else:
        wrap_w = colpx - inset
    if "max_width_ch" in lo:
        wrap_w = min(wrap_w, lo["max_width_ch"] * cw)
    wrap_w = max(wrap_w, cw * 4)
    text_term = rows * chars * lo.get("text_mult", 1) / wrap_w
    return [1.0, rows, float(text_term)]


@lru_cache(maxsize=1)
def _char_w() -> dict[str, float]:
    return {k: float(v) for k, v in _model().get("char_w_at_17px", {}).items()}


class HeightModel:
    """Query wrapper over height-model.json."""

    def __init__(self) -> None:
        self._m = _model()
        self._comps = self._m["components"]
        self._cw = _char_w()

    def known(self, component: str) -> bool:
        return component in self._comps

    def coeffs(self, component: str, brand: str) -> list[float]:
        c = self._comps[component]
        pb = c.get("per_brand", {})
        if brand in pb and pb[brand].get("coeffs"):
            return [float(x) for x in pb[brand]["coeffs"]]
        return [float(x) for x in c.get("pooled", {}).get("coeffs", [0.0, 0.0])]

    def estimate_px(
        self, component: str, items: int, chars: int, col_span: int,
        brand: str = "okuro",
    ) -> float:
        """Estimated rendered height in px for a component at (items, chars,
        col_span) under a brand — no render. Clamps to the component's cap_px."""
        if component not in self._comps:
            # Unknown component: conservative fallback = one row unit per item.
            return max(row_unit_px(), items * row_unit_px() * 0.5)
        c = self._comps[component]
        lo = c["layout"]
        tb = bool(c.get("text_bearing", False))
        cw = self._cw.get(brand, next(iter(self._cw.values()), 8.5))
        feats = _design_row(lo, max(items, 1), max(chars, 0), col_px(col_span), tb, cw)
        coeffs = self.coeffs(component, brand)
        h = sum(f * k for f, k in zip(feats, coeffs))
        cap = lo.get("cap_px")
        if cap:
            h = min(h, float(cap))
        return max(h, 1.0)

    def row_units(
        self, component: str, items: int, chars: int, col_span: int,
        brand: str = "okuro",
    ) -> float:
        """Height expressed in baseline row-units (px / row_unit_px)."""
        return self.estimate_px(component, items, chars, col_span, brand) / row_unit_px()


@lru_cache(maxsize=1)
def get_height_model() -> HeightModel:
    return HeightModel()


__all__ = ["HeightModel", "get_height_model", "col_px", "row_unit_px", "content_w_px"]
