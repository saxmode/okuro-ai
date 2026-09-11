// <!-- AGENT_HEADER
// role: code
// purpose: Pure viz helpers for prism rigor modules — frequency icon-array math
//   and HOPs (Hypothetical Outcome Plot) draw resolution/stats. No DOM, no React,
//   deterministic (seeded) so the render is stable and unit-testable.
// AGENT_HEADER_END -->

/** Scale a count-of-N to a fixed icon grid (default 100 dots), so "37 of 940"
 *  and "4 of 10" both render as a readable, comparable array. Returns the grid
 *  size and how many cells are filled (rounded, but never 0 when count>0 and
 *  never full when count<of — the honest floor/ceiling). */
export function iconCells(count: number, of: number, cells = 100): { filled: number; total: number } {
  const c = Math.max(0, count);
  const n = Math.max(1, of);
  const total = Math.min(cells, n);
  if (c <= 0) return { filled: 0, total };
  if (c >= n) return { filled: total, total };
  const raw = (c / n) * total;
  let filled = Math.round(raw);
  if (filled <= 0) filled = 1; // a nonzero risk must show at least one cell
  if (filled >= total) filled = total - 1; // a sub-certain risk must not read as full
  return { filled, total };
}

/** A tiny seeded LCG (Numerical Recipes constants) → deterministic draws with no
 *  Math.random, so HOPs sampling is reproducible across renders and in tests. */
function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (1664525 * s + 1013904223) >>> 0;
    return s / 0x100000000;
  };
}

/** Resolve the draws a HOPs animation cycles through. Prefers an explicit
 *  pre-sampled `draws` array; otherwise samples `n` values from a piecewise-
 *  linear quantile spec {p10,p50,p90} (deterministic via a fixed seed). Returns
 *  [] when neither is usable — the caller then renders nothing. */
export function resolveDraws(
  draws: number[] | undefined,
  quantiles: { p10: number; p50: number; p90: number } | undefined,
  n = 60,
): number[] {
  if (Array.isArray(draws) && draws.length) return draws.filter((d) => Number.isFinite(d));
  if (!quantiles) return [];
  const { p10, p50, p90 } = quantiles;
  if (![p10, p50, p90].every(Number.isFinite)) return [];
  const rand = lcg(1013904223);
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const u = rand();
    // Map a uniform draw through the quantile anchors (10th/50th/90th),
    // linearly interpolating within each half. Tails extrapolate off the
    // p10→p50 / p50→p90 slope so the spread stays honest.
    let v: number;
    if (u <= 0.1) v = p10 - (0.1 - u) * ((p50 - p10) / 0.4);
    else if (u <= 0.5) v = p10 + ((u - 0.1) / 0.4) * (p50 - p10);
    else if (u <= 0.9) v = p50 + ((u - 0.5) / 0.4) * (p90 - p50);
    else v = p90 + (u - 0.9) * ((p90 - p50) / 0.4);
    out.push(v);
  }
  return out;
}

/** Axis + summary stats for a draw set: domain (with a little padding) and the
 *  10th/50th/90th percentiles for the static quantile-dotplot fallback. */
export function hopsStats(draws: number[]): {
  min: number;
  max: number;
  mean: number;
  p10: number;
  p50: number;
  p90: number;
} {
  const xs = draws.filter((d) => Number.isFinite(d)).slice().sort((a, b) => a - b);
  if (!xs.length) return { min: 0, max: 1, mean: 0, p10: 0, p50: 0, p90: 0 };
  const q = (p: number) => {
    const idx = (xs.length - 1) * p;
    const lo = Math.floor(idx);
    const hi = Math.ceil(idx);
    const w = idx - lo;
    return xs[lo]! * (1 - w) + xs[hi]! * w;
  };
  const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
  return { min: xs[0]!, max: xs[xs.length - 1]!, mean, p10: q(0.1), p50: q(0.5), p90: q(0.9) };
}
