# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides layout geometry for a 12-column x 6-row grid system with typography snapping
# index: class Grid
# AGENT_HEADER_END -->
"""Swiss grid system. The single source of truth for layout geometry.

A canvas (1200x600) is divided into a 12 column x 6 row module grid with
a 1-module outer margin. All primitive coordinates live in MODULE units
(col, row) and are snapped to the grid by the renderer. Typography snaps
to an 8px sub-baseline.

      0      1     2     3     4     5     6     7     8     9    10    11    12
      |------|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|
   0  |  margin (always 1 module)                                            |
   1  |     +-----+-----+-----+-----+-----+-----+-----+-----+-----+-----+    |
   2  |     |                                                           |    |
   3  |     |              content area                                 |    |
   4  |     |                                                           |    |
   5  |     +-----+-----+-----+-----+-----+-----+-----+-----+-----+-----+    |
   6  |  margin                                                              |
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Grid:
    width: int = 1200
    height: int = 600
    cols: int = 12
    rows: int = 6
    margin_modules: float = 1.0       # outer margin, in modules
    baseline_px: int = 8              # typography sub-grid

    @property
    def module_w(self) -> float:
        return self.width / self.cols

    @property
    def module_h(self) -> float:
        return self.height / self.rows

    # Content area (inside outer margin) ------------------------------
    @property
    def inner_left(self) -> float:
        return self.margin_modules * self.module_w

    @property
    def inner_top(self) -> float:
        return self.margin_modules * self.module_h

    @property
    def inner_right(self) -> float:
        return self.width - self.inner_left

    @property
    def inner_bottom(self) -> float:
        return self.height - self.inner_top

    @property
    def inner_w(self) -> float:
        return self.inner_right - self.inner_left

    @property
    def inner_h(self) -> float:
        return self.inner_bottom - self.inner_top

    # Coordinate helpers ---------------------------------------------
    def x(self, col: float) -> float:
        """col in [0, cols] -> px (col=0 is left edge of canvas)."""
        return col * self.module_w

    def y(self, row: float) -> float:
        return row * self.module_h

    def cx(self, col: float) -> float:
        """col is the CENTER of a module: 1.5 -> middle of module 1."""
        return col * self.module_w

    def cy(self, row: float) -> float:
        return row * self.module_h

    def snap_baseline(self, y_px: float) -> float:
        """Snap y to the typography 8px baseline."""
        return round(y_px / self.baseline_px) * self.baseline_px

    def bbox(self, c0: float, r0: float, c1: float, r1: float
             ) -> tuple[float, float, float, float]:
        """Module bbox -> px bbox (x0, y0, x1, y1)."""
        return (self.x(c0), self.y(r0), self.x(c1), self.y(r1))

    # Anchor zones for compositions ----------------------------------
    @property
    def title_zone(self) -> tuple[float, float, float, float]:
        """Bottom-left strip for title, by Swiss convention (col 1..6, row 5..6)."""
        return (1.0, 5.0, 6.0, 5.7)

    @property
    def eyebrow_zone(self) -> tuple[float, float, float, float]:
        return (1.0, 0.6, 6.0, 1.0)

    @property
    def meta_zone(self) -> tuple[float, float, float, float]:
        """Bottom-right small metadata."""
        return (8.0, 5.4, 11.0, 5.7)


DEFAULT = Grid()


# Type scale: 4 steps at ~1.32 ratio, baseline-snapped
TYPE_SCALE = {
    "eyebrow":    {"size": 10, "tracking": 4, "weight": 400, "case": "upper"},
    "annotation": {"size": 11, "tracking": 2, "weight": 400, "case": "upper"},
    "value":      {"size": 13, "tracking": 1, "weight": 500, "case": "upper"},
    "caption":    {"size": 13, "tracking": 1, "weight": 400, "case": "as-is"},
    "title":      {"size": 22, "tracking": 1, "weight": 500, "case": "upper"},
    "display":    {"size": 32, "tracking": 0, "weight": 600, "case": "upper"},
}
