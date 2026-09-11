import type { ChartSeries, ChartType, SlideElement } from "./scene";

/**
 * chart-svg — hand-rolled inline-SVG charts (Phase D). NO chart library: the
 * IR carries `{chartType, series, categories}` and this module renders it to an
 * SVG string sized to the element box. One pure function so the React deck
 * renderer (via dangerouslySetInnerHTML), the export HTML path (PDF + PNG
 * foreignObject) and the PPTX rasterizer all emit byte-identical marks.
 *
 * Method: the `dataviz` skill. Colours are its VALIDATED categorical palette
 * (fixed slot order, never cycled), stepped for a light OR dark surface; marks
 * are thin with 4px rounded data-ends on a single baseline; grid/axes are
 * recessive hairlines; a legend appears for ≥2 series; every label wears an ink
 * token, never a series colour. A single series may take the element's own
 * `color` (brand accent) as its one hue.
 */

// dataviz categorical palette — fixed order, validated for adjacent CVD in both
// modes (references/palette.md). Dark steps for a dark surface, light for light.
const PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
const PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];

interface Ink {
  primary: string; // tick + category labels, legend text
  muted: string; // de-emphasised axis text
  grid: string; // hairline gridlines
  axis: string; // baseline / left axis
  palette: string[]; // categorical series colours for this surface
}

/** Relative luminance (0..1) of a #rgb/#rrggbb colour, or null if unparseable. */
function luminance(color?: string): number | null {
  if (!color) return null;
  let h = color.trim().toLowerCase();
  if (!h.startsWith("#")) return null;
  h = h.slice(1);
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  if (h.length !== 6 || /[^0-9a-f]/.test(h)) return null;
  const chan = (i: number) => {
    const cs = parseInt(h.slice(i, i + 2), 16) / 255;
    return cs <= 0.04045 ? cs / 12.92 : ((cs + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * chan(0) + 0.7152 * chan(2) + 0.0722 * chan(4);
}

/** Pick ink + palette for the surface the chart sits on. Resolves the effective
 *  surface as the element's OWN `bg` if set, else the `surface` the caller
 *  passes (the deck background / an underlying box), else the okuro dark deck.
 *  A light surface flips to dark ink + the light palette; dark keeps light ink +
 *  the dark palette. Text is always an ink token — never a series colour. */
function inkFor(el: SlideElement, surface?: string): Ink {
  const lum = luminance(el.bg ?? surface);
  const light = lum !== null && lum >= 0.5;
  return light
    ? { primary: "#1a1a19", muted: "#52514e", grid: "rgba(0,0,0,0.10)", axis: "rgba(0,0,0,0.22)", palette: PALETTE_LIGHT }
    : { primary: "#e8ffe8", muted: "rgba(232,255,232,0.62)", grid: "rgba(255,255,255,0.12)", axis: "rgba(255,255,255,0.24)", palette: PALETTE_DARK };
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** Compact, thousands-comma'd tick label (dataviz: clean round numbers). */
function fmt(v: number): string {
  const a = Math.abs(v);
  if (a >= 1_000_000) return `${trim(v / 1_000_000)}M`;
  if (a >= 1_000) return `${trim(v / 1_000)}k`;
  return trim(v);
}
function trim(v: number): string {
  return (Math.round(v * 100) / 100).toString();
}

/** A "nice" ceiling ≥ v (1/2/5 × 10ⁿ) so ticks land on round numbers. */
function niceCeil(v: number): number {
  if (v <= 0) return 0;
  const exp = Math.floor(Math.log10(v));
  const base = 10 ** exp;
  const f = v / base;
  const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
  return nf * base;
}

/** Clean numeric series from an element (drops non-finite, caps at 8 series). */
function cleanSeries(el: SlideElement): ChartSeries[] {
  const raw = Array.isArray(el.series) ? el.series : [];
  return raw
    .slice(0, 8)
    .map((s, i) => ({
      name: (s?.name ?? `Series ${i + 1}`).toString(),
      values: (Array.isArray(s?.values) ? s.values : []).map(Number).filter((n) => Number.isFinite(n)),
    }))
    .filter((s) => s.values.length > 0);
}

/** SVG path for a rectangle with rounded TOP corners only (dataviz: 4px rounded
 *  data-end, square at the baseline). `r` auto-clamps to short/thin bars. */
function topRoundedRect(x: number, y: number, w: number, h: number, r: number): string {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  return (
    `M${x},${y + h}` +
    `L${x},${y + rr}` +
    `Q${x},${y} ${x + rr},${y}` +
    `L${x + w - rr},${y}` +
    `Q${x + w},${y} ${x + w},${y + rr}` +
    `L${x + w},${y + h}Z`
  );
}

/**
 * Render a chart element to a self-contained `<svg>` string sized to its box.
 * viewBox is the element's declared `w×h`; width/height are 100% so it scales
 * with the (possibly morphing / fit-scaled) box.
 */
export function chartSvg(el: SlideElement, surface?: string): string {
  const W = Math.max(1, el.w);
  const H = Math.max(1, el.h);
  const type: ChartType = el.chartType ?? "bar";
  const ink = inkFor(el, surface);
  const series = cleanSeries(el);
  const showAxes = el.showAxes !== false;
  const showLegend = el.showLegend ?? series.length > 1;
  const fs = Math.max(9, Math.min(16, Math.round(H * 0.032)));

  const open = `<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" font-family="inherit">`;
  const close = `</svg>`;

  if (series.length === 0) {
    // Malformed / empty → a legible placeholder, never a crash.
    return (
      open +
      `<text x="${W / 2}" y="${H / 2}" fill="${ink.muted}" font-size="${fs + 2}" text-anchor="middle" dominant-baseline="middle">no chart data</text>` +
      close
    );
  }

  const nCat = Math.max(el.categories?.length ?? 0, ...series.map((s) => s.values.length));
  const cats = Array.from({ length: nCat }, (_, i) => el.categories?.[i] ?? `${i + 1}`);

  // Domain: bars/areas anchor at 0; a line may dip below 0.
  let dataMax = -Infinity;
  let dataMin = Infinity;
  for (const s of series) for (const v of s.values) { if (v > dataMax) dataMax = v; if (v < dataMin) dataMin = v; }
  const top = niceCeil(Math.max(dataMax, 0)) || 1;
  const bottom = dataMin < 0 ? -niceCeil(-dataMin) : 0;
  const span = top - bottom || 1;

  // Plot rect. Reserve a legend band, left tick gutter + bottom category band.
  const legendH = showLegend ? fs + 14 : 0;
  const padTop = 10 + legendH;
  const padBottom = showAxes ? fs + 18 : 8;
  const padLeft = showAxes ? Math.max(34, fs * 3) : 8;
  const padRight = 12;
  const px = padLeft;
  const py = padTop;
  const pw = Math.max(1, W - padLeft - padRight);
  const ph = Math.max(1, H - padTop - padBottom);
  const yOf = (v: number) => py + ph * (1 - (v - bottom) / span);
  const xBand = pw / nCat;

  const parts: string[] = [open];

  // ── grid + axes (recessive hairlines) ──────────────────────────────────────
  if (showAxes) {
    const ticks = 4;
    for (let t = 0; t <= ticks; t++) {
      const v = bottom + (span * t) / ticks;
      const gy = yOf(v);
      parts.push(`<line x1="${px}" y1="${gy.toFixed(1)}" x2="${px + pw}" y2="${gy.toFixed(1)}" stroke="${ink.grid}" stroke-width="1"/>`);
      parts.push(
        `<text x="${px - 6}" y="${(gy + fs * 0.34).toFixed(1)}" fill="${ink.muted}" font-size="${fs}" text-anchor="end" style="font-variant-numeric:tabular-nums">${esc(fmt(v))}</text>`,
      );
    }
    // baseline (y=0 or domain floor) + left axis
    const zeroY = yOf(Math.max(bottom, 0));
    parts.push(`<line x1="${px}" y1="${zeroY.toFixed(1)}" x2="${px + pw}" y2="${zeroY.toFixed(1)}" stroke="${ink.axis}" stroke-width="1"/>`);
    parts.push(`<line x1="${px}" y1="${py}" x2="${px}" y2="${py + ph}" stroke="${ink.axis}" stroke-width="1"/>`);
    // category labels
    for (let c = 0; c < nCat; c++) {
      const cx = px + xBand * (c + 0.5);
      parts.push(
        `<text x="${cx.toFixed(1)}" y="${(py + ph + fs + 6).toFixed(1)}" fill="${ink.muted}" font-size="${fs}" text-anchor="middle">${esc(cats[c] ?? "")}</text>`,
      );
    }
  }

  // ── marks ───────────────────────────────────────────────────────────────────
  const color = (i: number) => (series.length === 1 ? el.color ?? ink.palette[0]! : ink.palette[i % ink.palette.length]!);

  if (type === "bar") {
    const S = series.length;
    const bandPad = xBand * 0.16;
    const slot = Math.max(1, xBand - bandPad * 2);
    const gap = 2; // dataviz: 2px surface gap between adjacent bars
    const barW = Math.max(1, Math.min(24, (slot - gap * (S - 1)) / S));
    const zeroY = yOf(Math.max(bottom, 0));
    for (let c = 0; c < nCat; c++) {
      const groupW = barW * S + gap * (S - 1);
      const start = px + xBand * c + (xBand - groupW) / 2;
      series.forEach((s, si) => {
        const v = s.values[c];
        if (v === undefined || !Number.isFinite(v)) return;
        const bx = start + si * (barW + gap);
        const vy = yOf(v);
        const barTop = Math.min(vy, zeroY);
        const barH = Math.abs(vy - zeroY);
        parts.push(`<path d="${topRoundedRect(bx, barTop, barW, barH, 4)}" fill="${color(si)}"/>`);
      });
    }
  } else {
    // line / area — one polyline per series at band centres.
    const xOf = (c: number) => px + xBand * (c + 0.5);
    series.forEach((s, si) => {
      const pts = s.values
        .map((v, c) => (Number.isFinite(v) ? `${xOf(c).toFixed(1)},${yOf(v).toFixed(1)}` : null))
        .filter((p): p is string => p !== null);
      if (pts.length === 0) return;
      const col = color(si);
      if (type === "area" && si === 0) {
        const baseY = yOf(Math.max(bottom, 0)).toFixed(1);
        const areaD = `M${xOf(0).toFixed(1)},${baseY} L${pts.join(" L")} L${xOf(s.values.length - 1).toFixed(1)},${baseY} Z`;
        parts.push(`<path d="${areaD}" fill="${col}" fill-opacity="0.12"/>`);
      }
      parts.push(`<polyline points="${pts.join(" ")}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`);
      // end marker (dataviz: ≥8px marker)
      const last = pts[pts.length - 1]!.split(",");
      parts.push(`<circle cx="${last[0]}" cy="${last[1]}" r="4" fill="${col}"/>`);
    });
  }

  // ── legend (≥2 series, or forced) ────────────────────────────────────────────
  if (showLegend) {
    let lx = px;
    const ly = 10 + fs * 0.5;
    series.forEach((s, si) => {
      const label = s.name || `Series ${si + 1}`;
      parts.push(`<circle cx="${lx + 5}" cy="${ly}" r="5" fill="${color(si)}"/>`);
      parts.push(`<text x="${lx + 15}" y="${(ly + fs * 0.34).toFixed(1)}" fill="${ink.primary}" font-size="${fs}">${esc(label)}</text>`);
      lx += 15 + Math.max(28, label.length * fs * 0.56) + 16;
    });
  }

  parts.push(close);
  return parts.join("");
}
