import type { SlideElement } from "./scene";
import { hugs } from "./scene";

/**
 * Measured fit — the render-side half of the fit/bounds guarantee.
 *
 * The backend fit pass (`slides/layout.py`) works on DECLARED element boxes; it
 * cannot know how tall text actually wraps. `flattenElements` DOES — it stamps
 * each text element's measured (wrapped) height. This runs on that flattened,
 * measured list: if a slide's real content exceeds the canvas or sits outside
 * it, uniformly scale it down (to a legibility floor) and re-center so nothing
 * is cropped or spills. A slide that already fits is returned unchanged (same
 * array reference — zero churn for the common case).
 *
 * Mirrors the backend algorithm so both halves behave identically; kept in sync
 * by shared tests on the same fixtures.
 */

// Matches slides/layout.py `_MIN_SCALE` — below this, scaling is the wrong fix
// (the content needs splitting) so we clamp and accept a slight overflow.
const MIN_SCALE = 0.55;

interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

function unionBounds(elements: SlideElement[]): Bounds | null {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  let seen = false;
  for (const e of elements) {
    if (typeof e.x !== "number" || typeof e.y !== "number" || typeof e.w !== "number" || typeof e.h !== "number") {
      continue;
    }
    seen = true;
    minX = Math.min(minX, e.x);
    minY = Math.min(minY, e.y);
    maxX = Math.max(maxX, e.x + e.w);
    maxY = Math.max(maxY, e.y + e.h);
  }
  return seen ? { minX, minY, maxX, maxY } : null;
}

/** One fit pass: scale + translate the elements so their union fits the canvas.
 *  Returns the SAME array reference when nothing needs to change. */
function fitOnce(elements: SlideElement[], canvasW: number, canvasH: number): SlideElement[] {
  const b = unionBounds(elements);
  if (!b) return elements;
  const contentW = b.maxX - b.minX;
  const contentH = b.maxY - b.minY;
  if (contentW <= 0 || contentH <= 0) return elements;

  const overflowing = contentW > canvasW || contentH > canvasH;
  const outOfBounds = b.minX < 0 || b.minY < 0 || b.maxX > canvasW || b.maxY > canvasH;
  if (!overflowing && !outOfBounds) return elements; // already fits — no churn

  const scale = overflowing
    ? Math.max(MIN_SCALE, Math.min(canvasW / contentW, canvasH / contentH))
    : 1;

  const newW = contentW * scale;
  const newH = contentH * scale;
  let tx: number;
  let ty: number;
  if (scale < 1) {
    tx = (canvasW - newW) / 2; // center when shrunk
    ty = (canvasH - newH) / 2;
  } else {
    tx = Math.min(Math.max(b.minX, 0), Math.max(0, canvasW - newW)); // shift inside
    ty = Math.min(Math.max(b.minY, 0), Math.max(0, canvasH - newH));
  }

  return elements.map((e) => {
    const out: SlideElement = {
      ...e,
      x: Math.round((e.x - b.minX) * scale + tx),
      y: Math.round((e.y - b.minY) * scale + ty),
    };
    if (scale < 1) {
      out.w = e.w * scale;
      out.h = e.h * scale;
      if (typeof e.fontSize === "number") out.fontSize = e.fontSize * scale;
      if (typeof e.radius === "number") out.radius = e.radius * scale;
      if (typeof e.pad === "number") out.pad = e.pad * scale;
    }
    return out;
  });
}

// A single fit pass shrinks fontSize linearly and multiplies `h` by the scale —
// but smaller text RE-WRAPS to a different line count, so the real measured
// height ≠ h*scale and the overflow the pass fixed can reappear (#12). When a
// `remeasure` fn is supplied we iterate: fit → re-measure text height at its new
// size/width → fit again, until it converges (a pass needs no change) or the
// bound is hit. Bounded so a pathological deck can't loop forever.
const MAX_FIT_PASSES = 3;

/** Fit a slide's (flattened, measured) elements inside the canvas.
 *
 * Returns the same array reference when no change is needed. Pass `remeasure` to
 * get a text element's true wrapped height at its (post-scale) size/width so the
 * fit converges against real geometry instead of a linear approximation; omit it
 * for a single, pure pass (the backend-mirroring behavior). */
export function fitToCanvas(
  elements: SlideElement[],
  canvasW: number,
  canvasH: number,
  remeasure?: (el: SlideElement) => number,
): SlideElement[] {
  let current = elements;
  for (let pass = 0; pass < MAX_FIT_PASSES; pass++) {
    const fitted = fitOnce(current, canvasW, canvasH);
    if (fitted === current) return current; // converged — fits with no change
    if (!remeasure) return fitted; // pure single pass (no measurer available)
    // Re-measure hug kinds at their new width/fontSize so the next pass reasons
    // about real wrapped height, not the linear h*scale guess.
    current = fitted.map((e) => (hugs(e.kind) ? { ...e, h: remeasure(e) } : e));
  }
  return current; // bounded out at the floor — best-effort converged geometry
}
