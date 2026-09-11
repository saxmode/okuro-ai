# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM P4.2 — the AeSlides-style geometry linter. Four verifiable
#   layout checks over a rendered composed slide: aspect ratio, element
#   collision, visual imbalance (DOM boxes) and excessive whitespace (PIXELS).
#   Every constant is either taken from the AeSlides reference implementation and
#   cited at its definition, or declared LOCAL in the LOCAL_ block. Nothing is
#   invented; nothing is silently tuned.
# index:
#   PUBLISHED constants (aspect / smoothstep / whitespace / imbalance)
#   LOCAL constants (the two pass-cuts the paper does not publish)
#   smoothstep_reward / aspect_reward   (verbatim ports)
#   content_ratio (pixel leg) / find_collisions / visual_imbalance
#   Collision / LintReport / lint_geometry / assert_geometry_clean
# AGENT_HEADER_END -->
"""Verifiable geometry checks for a rendered composed slide.

Sourcing, stated plainly because it bounds what these checks can claim:

* AeSlides, *Incentivizing Aesthetic Layout in LLM-Based Slide Generation via
  Verifiable Rewards*, arXiv:2604.22840. The PAPER specifies the four metrics
  and gives tau=0.05, H=201, W=151, x_tol=0.05, y_tol=0.15 — but NOT alpha,
  beta, m, Tclip, or the smoothstep bounds.
* The authors' released reference snippets, github.com/ympan0508/aeslides
  (``src/reward.py``, ``src/whitespace.py``, ``src/centroid.py``). These DO
  carry every missing constant, and they are the values used here. Each is cited
  at its definition with file and symbol.

Two divergences between paper and code are recorded rather than reconciled:
the centroid tolerances (paper 0.05/0.15, code 0.0075/0.145 — code wins here,
it is the implementation) and the smoothstep direction (the released code
returns 0.0 below ``lower`` and 1.0 above ``upper``, i.e. the input is a
GOODNESS, not a defect rate).

What is deliberately NOT imported: the binding between a measured quantity and
the smoothstep — the repo withholds every call site ("we do not release the full
system implementation"). So aspect and whitespace are computed as continuous
REWARDS on published constants, and any pass/fail cut on them is declared in the
LOCAL block below and marked local in the report. Collision and imbalance need
no such cut: collision is exact geometry, and imbalance's threshold IS the
published tolerance pair.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

# ── PUBLISHED — aeslides src/reward.py::calculate_asymmetric_quadratic_reward ──
#: 16:9, expressed exactly as the reference does (1280/720).
ASPECT_TARGET_AR = 1280 / 720
ASPECT_ALPHA = 16.0                  # reward.py arg `alpha`
ASPECT_BETA = 64.0                   # reward.py arg `beta`  (tall-slide asymmetry)
ASPECT_MARGIN = 0.04                 # reward.py arg `margin` — the paper's `m`

# ── PUBLISHED — aeslides src/reward.py::calculate_smoothstep_reward ────────────
SMOOTHSTEP_LOWER = 0.8
SMOOTHSTEP_UPPER = 0.995
SMOOTHSTEP_GAMMA = 1.0

# ── PUBLISHED — aeslides src/whitespace.py (local-variance content detection) ──
WS_BOX_KSIZE_H = 201                 # horizontal box extent (kernel WIDTH)
WS_BOX_KSIZE_V = 151                 # vertical box extent  (kernel HEIGHT)
WS_GAUSSIAN_KSIZE = (21, 21)
WS_STD_CLIP = 50.0                   # the paper's `Tclip`
WS_BINARY_THRESHOLD = 0.05           # the paper's `tau`
WS_CROP_RATIOS = (0.15, 0.1, 0.1, 0.1)   # (top, bottom, left, right)
#: cv2.GaussianBlur(sigma=0) derives sigma from ksize: 0.3*((k-1)*0.5-1)+0.8.
#: For k=21 that is exactly 3.5; pinned here so the scipy port is not a guess.
WS_GAUSSIAN_SIGMA = 0.3 * ((WS_GAUSSIAN_KSIZE[0] - 1) * 0.5 - 1) + 0.8
WS_GAUSSIAN_RADIUS = (WS_GAUSSIAN_KSIZE[0] - 1) // 2      # 21 taps -> radius 10

# ── PUBLISHED — aeslides src/centroid.py (visual imbalance) ───────────────────
IMBALANCE_X_TOL = 0.0075             # centroid.py `x_tolerance_norm`
IMBALANCE_Y_TOL = 0.145              # centroid.py `y_tolerance_norm`
IMBALANCE_MIN_OPACITY = 0.05         # centroid.py `min_opacity`
#: centroid.py treats score > 1.0 as imbalanced — the tolerances ARE the cut, so
#: this is a published threshold, not a local one.
IMBALANCE_MAX_D = 1.0

# ── LOCAL — declared here because the reference does not publish them ─────────
#: Sub-pixel tolerance before two boxes count as overlapping. Browsers report
#: fractional rects; without this every adjacent cell reads as a collision.
LOCAL_COLLISION_EPS_PX = 1.0
#: Hard-fail cut for the whitespace leg. The reference publishes the DETECTOR
#: (every constant above) but not which quantity is fed to the smoothstep nor at
#: what scale, so no published cut exists. This one is LOCAL, and every report it
#: fires in marks the finding `local=True`. Calibrate on the deck corpus before
#: treating it as anything but a smoke alarm.
LOCAL_MIN_CONTENT_RATIO = 0.02
#: Aspect has NO hard-fail cut at all: the reward is continuous and the paper
#: publishes no pass line. It is reported as a score and feeds the rerank.
LOCAL_ASPECT_HARD_FAIL = False
#: Visual imbalance is BLOCKED as a hard gate, on measurement, not on taste.
#:
#: scripts/prism_geometry_lint_proof.py rendered all 24 buildable
#: (family x arrangement) pairs the P4.1 bridge admits and applied the published
#: tolerances: only 7 of 24 legitimate compositions came in at d <= 1. The
#: failures are not defects — they are arrangements whose DESIGN INTENT is
#: off-centre mass (arr-asymmetric-lead 9|3 measured d~15.5, arr-notan-void,
#: arr-hero-rail 8|4), and the grid families deliberately encode that as
#: `asymmetry_bias` up to 0.9 for bauhaus. AeSlides' x_tol of 0.0075 (0.75% of
#: width) assumes an optically centred composition; prism's families do not.
#:
#: A gate that no correct layout can pass is not a gate. The published constants
#: are therefore kept EXACTLY as the reference sets them and the metric ships as
#: a reported score — loosening x_tol to make it pass would be inventing the very
#: threshold the brief forbids. Re-open this only with a corpus measurement that
#: shows a cut separating good compositions from bad ones on THIS corpus.
LOCAL_IMBALANCE_HARD_FAIL = False


# ── verbatim ports ────────────────────────────────────────────────────────────

def aspect_reward(
    w: float, h: float,
    target: float = ASPECT_TARGET_AR,
    alpha: float = ASPECT_ALPHA,
    beta: float = ASPECT_BETA,
    margin: float = ASPECT_MARGIN,
    flat_low_ar: float = ASPECT_TARGET_AR,
    flat_high_ar: float = ASPECT_TARGET_AR,
) -> float:
    """Asymmetric quadratic aspect reward in (0, 1].

    Port of aeslides ``src/reward.py::calculate_asymmetric_quadratic_reward``.
    ``beta`` punishes OVERLONG (tall) slides harder than wide ones — which is the
    failure mode a stacking composer actually has.
    """
    if target <= 0:
        raise ValueError(f"target must be > 0, got {target!r}")
    if w <= 0 or h <= 0:
        return 0.0
    if flat_low_ar <= 0 or flat_high_ar <= 0:
        raise ValueError("flat_low_ar and flat_high_ar must be > 0")
    if flat_low_ar > flat_high_ar:
        raise ValueError("flat_low_ar must be <= flat_high_ar")

    ar = w / h
    if flat_low_ar <= ar <= flat_high_ar:
        return 1.0
    e = math.log(ar / flat_high_ar) if ar > flat_high_ar else math.log(ar / flat_low_ar)
    base = alpha * (e ** 2)
    tall_excess = max(-e - margin, 0.0)
    return math.exp(-base - beta * (tall_excess ** 2))


def smoothstep_reward(
    x: float,
    scale: float = 1.0,
    lower: float = SMOOTHSTEP_LOWER,
    upper: float = SMOOTHSTEP_UPPER,
    gamma: float = SMOOTHSTEP_GAMMA,
) -> float:
    """Port of aeslides ``src/reward.py::calculate_smoothstep_reward``.

    NOTE the direction, which is the opposite of a naive reading of the paper's
    Eq. 8: ``x <= lower`` returns **0.0** and ``x >= upper`` returns **1.0**, so
    ``x`` is a goodness measure. Kept faithful rather than "corrected".
    """
    x = x * scale
    if upper <= lower:
        raise ValueError("upper must be greater than lower")
    if gamma <= 0:
        raise ValueError("gamma must be positive")
    if x <= lower:
        return 0.0
    if x >= upper:
        return 1.0
    t = (x - lower) / (upper - lower)
    a = t ** gamma
    b = (1.0 - t) ** gamma
    u = a / (a + b)
    return 3.0 * u * u - 2.0 * u * u * u


# ── the pixel leg ─────────────────────────────────────────────────────────────

def content_ratio(
    png_bytes: bytes,
    box_ksize_h: int = WS_BOX_KSIZE_H,
    box_ksize_v: int = WS_BOX_KSIZE_V,
    std_clip: float = WS_STD_CLIP,
    binary_threshold: float = WS_BINARY_THRESHOLD,
    crop_ratios: tuple[float, float, float, float] = WS_CROP_RATIOS,
) -> dict[str, float]:
    """Local-variance content ratio of a rendered slide — the excessive-whitespace
    metric. Port of aeslides ``src/whitespace.py``.

    The reference uses OpenCV; okuro does not ship cv2, so this uses
    numpy/PIL/scipy. The substitutions are exact-for-exact, not approximations:
    PIL ``convert("L")`` and ``cv2.COLOR_BGR2GRAY`` are both ITU-R 601-2
    (0.299/0.587/0.114); ``scipy.ndimage.uniform_filter`` is a normalised box
    filter like ``cv2.boxFilter``; scipy ``mode="reflect"`` is
    ``cv2.BORDER_REFLECT``; and the Gaussian sigma/radius are pinned to what
    ``cv2.GaussianBlur(..., (21,21), 0)`` derives. Remaining difference is uint8
    rounding, which is sub-threshold at tau=0.05.

    Returns ``content_ratio_crop`` (the metric), ``content_ratio_full``, and the
    resolution — enough for a caller to see WHY a slide scored as it did.
    """
    import numpy as np
    from PIL import Image
    from scipy.ndimage import gaussian_filter, uniform_filter

    t, b, l, r = crop_ratios
    if t + b >= 1 or l + r >= 1:
        raise ValueError("crop_ratios remove the entire image")

    gray = np.asarray(
        Image.open(io.BytesIO(png_bytes)).convert("L"), dtype=np.float32
    )
    if gray.ndim != 2 or gray.size == 0:
        raise ValueError("failed to decode png_bytes into a grayscale image")

    blurred = gaussian_filter(
        gray, sigma=WS_GAUSSIAN_SIGMA, radius=WS_GAUSSIAN_RADIUS, mode="reflect"
    )
    # scipy size is (axis0=rows=HEIGHT, axis1=cols=WIDTH); the reference passes
    # cv2 ksize=(kh, kv) which is (WIDTH, HEIGHT). Transposed deliberately.
    size = (box_ksize_v, box_ksize_h)
    local_mean = uniform_filter(blurred, size=size, mode="reflect")
    local_mean_sq = uniform_filter(blurred ** 2, size=size, mode="reflect")
    variance = np.maximum(local_mean_sq - local_mean ** 2, 0.0)
    std_dev = np.sqrt(variance)

    if std_clip <= 1e-8:
        norm_freq = np.zeros_like(std_dev, dtype=np.float32)
    else:
        norm_freq = (np.clip(std_dev, 0.0, std_clip) / std_clip).astype(np.float32)

    mask = norm_freq > binary_threshold
    h, w = mask.shape
    crop = mask[int(h * t): int(h * (1 - b)), int(w * l): int(w * (1 - r))]
    return {
        "content_ratio_crop": float(np.mean(crop)) if crop.size else 0.0,
        "content_ratio_full": float(np.mean(mask)),
        "width": float(w),
        "height": float(h),
    }


# ── the DOM legs ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Collision:
    """One geometry violation. ``kind`` is one of the three the paper names."""

    kind: str            # overlap | container_escape | boundary_overflow
    a: str
    b: str
    overlap_px: float
    detail: str = ""


def _rect(d: dict[str, Any]) -> tuple[float, float, float, float]:
    return float(d["x"]), float(d["y"]), float(d["w"]), float(d["h"])


def _intersection(p: dict, q: dict) -> float:
    ax, ay, aw, ah = _rect(p)
    bx, by, bw, bh = _rect(q)
    ox = min(ax + aw, bx + bw) - max(ax, bx)
    oy = min(ay + ah, by + bh) - max(ay, by)
    return min(ox, oy) if ox > 0 and oy > 0 else 0.0


def _escape(child: dict, parent: dict) -> float:
    """How far ``child`` sticks out of ``parent``, in px (0 = contained)."""
    cx, cy, cw, ch = _rect(child)
    px, py, pw, ph = _rect(parent)
    return max(
        px - cx, py - cy, (cx + cw) - (px + pw), (cy + ch) - (py + ph), 0.0
    )


def find_collisions(
    measure: dict[str, Any], eps_px: float = LOCAL_COLLISION_EPS_PX
) -> list[Collision]:
    """The three collision classes AeSlides names, over the measured boxes.

    Exact geometry — no published constant is needed and none is invented. The
    only tolerance is ``eps_px``, declared LOCAL, which exists because browsers
    report fractional rects and adjacent grid cells would otherwise all read as
    overlapping.
    """
    out: list[Collision] = []
    cells: Sequence[dict] = measure.get("cells", ())
    leaves: Sequence[dict] = measure.get("leaves", ())
    slide = measure.get("slide")

    # 1. overlapping bounding boxes — sibling cells must tile, never stack.
    for i in range(len(cells)):
        for j in range(i + 1, len(cells)):
            ov = _intersection(cells[i], cells[j])
            if ov > eps_px:
                out.append(Collision(
                    "overlap",
                    f"cell:{cells[i].get('component')}#{cells[i].get('unit')}",
                    f"cell:{cells[j].get('component')}#{cells[j].get('unit')}",
                    ov, "sibling cells overlap",
                ))

    # 2. container escape — painted content outside the cell that owns it.
    by_unit = {c.get("unit"): c for c in cells}
    for lf in leaves:
        host = by_unit.get(lf.get("unit"))
        if host is None:
            continue
        esc = _escape(lf, host)
        if esc > eps_px:
            out.append(Collision(
                "container_escape", f"leaf@unit{lf.get('unit')}",
                f"cell:{host.get('component')}#{host.get('unit')}",
                esc, "painted leaf escapes its cell",
            ))

    # 3. slide boundary overflow.
    if slide:
        for c in cells:
            esc = _escape(c, slide)
            if esc > eps_px:
                out.append(Collision(
                    "boundary_overflow",
                    f"cell:{c.get('component')}#{c.get('unit')}", "slide",
                    esc, "cell escapes the slide box",
                ))
    return out


def visual_imbalance(
    measure: dict[str, Any],
    x_tol: float = IMBALANCE_X_TOL,
    y_tol: float = IMBALANCE_Y_TOL,
) -> dict[str, float]:
    """Normalised centroid offset ``d``; ``d > 1`` is imbalanced.

    Port of aeslides ``src/centroid.py``: an area-weighted centroid of the
    painted leaves, offset from the canvas centre, each axis scaled by its own
    tolerance. LOCAL simplification, declared: the reference weights nodes by
    type (``icon_weight=0.5``, background-area heuristics) and okuro's kit DOM
    carries no such taxonomy, so every painted leaf is weighted by its AREA
    alone. The opacity floor is kept (applied in the measure JS).
    """
    slide = measure.get("slide") or {}
    leaves: Sequence[dict] = measure.get("leaves", ())
    vw, vh = float(slide.get("w", 0.0)), float(slide.get("h", 0.0))
    if vw <= 0 or vh <= 0 or not leaves:
        return {"d": 0.0, "center_x_norm": 0.5, "center_y_norm": 0.5,
                "offset_x_norm": 0.0, "offset_y_norm": 0.0, "weight": 0.0}

    total = cx = cy = 0.0
    for lf in leaves:
        x, y, w, h = _rect(lf)
        wt = w * h
        if wt <= 0:
            continue
        total += wt
        cx += (x + w / 2.0) * wt
        cy += (y + h / 2.0) * wt
    if total <= 0:
        return {"d": 0.0, "center_x_norm": 0.5, "center_y_norm": 0.5,
                "offset_x_norm": 0.0, "offset_y_norm": 0.0, "weight": 0.0}

    cx /= total
    cy /= total
    ox = (cx - vw / 2.0) / vw
    oy = (cy - vh / 2.0) / vh
    d = math.sqrt((abs(ox) / x_tol) ** 2 + (abs(oy) / y_tol) ** 2)
    return {"d": d, "center_x_norm": cx / vw, "center_y_norm": cy / vh,
            "offset_x_norm": ox, "offset_y_norm": oy, "weight": total}


# ── the report ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Finding:
    check: str
    message: str
    value: float
    local: bool = False          # True => the cut is LOCAL, not published


@dataclass(frozen=True)
class LintReport:
    aspect_reward: float
    imbalance_d: float
    collisions: tuple[Collision, ...]
    content_ratio_crop: float | None      # None = pixel leg not supplied
    whitespace_reward: float | None
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        """No HARD failure. Aspect never contributes (no published cut)."""
        return not self.findings

    @property
    def published_failures(self) -> tuple[Finding, ...]:
        """Failures resting only on PUBLISHED constants — the defensible set."""
        return tuple(f for f in self.findings if not f.local)

    def composition_score(self) -> float:
        """A single 0-1 geometry score for the rerank, built ONLY from parts whose
        formula and constants are both published: the aspect reward, times a hard
        0/1 collision factor. A slide that collides cannot buy its way back.

        Whitespace is deliberately NOT a factor. Its DETECTOR is published but the
        binding of content-ratio to the smoothstep is not, and folding
        ``smoothstep_reward(content_ratio)`` in here would silently adopt exactly
        the unpublished binding this module refuses to guess — it zeroes every
        slide under 0.8 content ratio, which on the real corpus is every
        single-unit manuscript slide. ``content_ratio_crop`` and
        ``whitespace_reward`` stay on the report for a caller that has calibrated
        a binding; the score does not assume one. Same reasoning for imbalance:
        reported, not scored (see LOCAL_IMBALANCE_HARD_FAIL).
        """
        return self.aspect_reward * (0.0 if self.collisions else 1.0)


def lint_geometry(
    measure: dict[str, Any],
    png_bytes: bytes | None = None,
    min_content_ratio: float = LOCAL_MIN_CONTENT_RATIO,
) -> LintReport:
    """Run all four checks over one rendered slide.

    ``png_bytes=None`` runs the three DOM checks and reports the whitespace leg
    as ``None`` — explicitly absent, never silently passed. That distinction is
    the point: a check that reports "fine" when it did not run is the vacuous
    gate this campaign keeps finding.
    """
    slide = measure.get("slide") or {}
    w, h = float(slide.get("w", 0.0)), float(slide.get("h", 0.0))
    ar = aspect_reward(w, h)
    collisions = tuple(find_collisions(measure))
    imb = visual_imbalance(measure)

    ratio: float | None = None
    ws_reward: float | None = None
    if png_bytes is not None:
        ratio = content_ratio(png_bytes)["content_ratio_crop"]
        ws_reward = smoothstep_reward(ratio)

    findings: list[Finding] = []
    for c in collisions:
        findings.append(Finding(
            "element_collision",
            f"{c.kind}: {c.a} vs {c.b} ({c.overlap_px:.1f}px) — {c.detail}",
            c.overlap_px,
        ))
    if LOCAL_IMBALANCE_HARD_FAIL and imb["d"] > IMBALANCE_MAX_D:
        # Off by measurement — see LOCAL_IMBALANCE_HARD_FAIL. `imbalance_d` is
        # still reported on every LintReport, so the signal is available to a
        # reranker and to the calibration script; it just does not refuse a build.
        findings.append(Finding(
            "visual_imbalance",
            f"centroid offset d={imb['d']:.2f} > {IMBALANCE_MAX_D} "
            f"(x {imb['offset_x_norm']:+.4f}/{IMBALANCE_X_TOL}, "
            f"y {imb['offset_y_norm']:+.4f}/{IMBALANCE_Y_TOL})",
            imb["d"],
        ))
    if ratio is not None and ratio < min_content_ratio:
        findings.append(Finding(
            "excessive_whitespace",
            f"content ratio {ratio:.4f} < {min_content_ratio} (LOCAL cut — the "
            f"reference publishes the detector, not this pass line)",
            ratio, local=True,
        ))
    if LOCAL_ASPECT_HARD_FAIL:                     # off: no published pass cut
        findings.append(Finding("aspect_ratio", f"reward {ar:.3f}", ar, local=True))

    return LintReport(
        aspect_reward=ar,
        imbalance_d=imb["d"],
        collisions=collisions,
        content_ratio_crop=ratio,
        whitespace_reward=ws_reward,
        findings=tuple(findings),
    )


def assert_geometry_clean(report: LintReport, where: str = "slide") -> None:
    """Route a lint failure through ``prism.strict`` — refuse under strict, log
    loudly otherwise. Every new hard-fail in this overhaul has exactly one
    rollback and this is no exception."""
    from okuro.prism.config import warn_or_fail

    if report.clean:
        return
    warn_or_fail(
        "geometry_lint_failed",
        f"{where}: " + "; ".join(f.message for f in report.findings),
        where=where,
        checks=[f.check for f in report.findings],
        local_only=all(f.local for f in report.findings),
    )


__all__ = [
    "ASPECT_TARGET_AR", "ASPECT_ALPHA", "ASPECT_BETA", "ASPECT_MARGIN",
    "SMOOTHSTEP_LOWER", "SMOOTHSTEP_UPPER", "SMOOTHSTEP_GAMMA",
    "WS_BOX_KSIZE_H", "WS_BOX_KSIZE_V", "WS_STD_CLIP", "WS_BINARY_THRESHOLD",
    "WS_CROP_RATIOS", "IMBALANCE_X_TOL", "IMBALANCE_Y_TOL", "IMBALANCE_MAX_D",
    "LOCAL_COLLISION_EPS_PX", "LOCAL_MIN_CONTENT_RATIO", "LOCAL_ASPECT_HARD_FAIL",
    "LOCAL_IMBALANCE_HARD_FAIL",
    "Collision", "Finding", "LintReport",
    "aspect_reward", "smoothstep_reward", "content_ratio",
    "find_collisions", "visual_imbalance", "lint_geometry",
    "assert_geometry_clean",
]
