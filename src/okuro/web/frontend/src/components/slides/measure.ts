import type { SlideElement } from "./scene";
import { DEFAULT_LINE_HEIGHT } from "./scene";

/**
 * Measure the rendered height of wrapped text so containers truly hug content
 * (the authored `h` assumes one line). Uses a cached hidden div that mirrors the
 * element's render styles (width, font, line-height, wrapping). Browser-only.
 */

let _probe: HTMLDivElement | null = null;
const _cache = new Map<string, number>();

function probe(): HTMLDivElement {
  if (_probe) return _probe;
  const d = document.createElement("div");
  d.style.position = "absolute";
  d.style.visibility = "hidden";
  d.style.left = "-99999px";
  d.style.top = "0";
  d.style.whiteSpace = "pre-wrap";
  d.style.wordBreak = "break-word";
  d.style.boxSizing = "border-box";
  document.body.appendChild(d);
  _probe = d;
  return d;
}

// strip **/* emphasis markers so width estimate matches rendered glyphs closely
function plain(text: string): string {
  return text.replace(/\*\*/g, "").replace(/\*/g, "");
}

export function measureTextHeight(
  text: string,
  width: number,
  opts: { fontSize: number; fontWeight: number; fontFamily?: string; pad?: number; lineHeight?: number; letterSpacing?: number },
): number {
  if (typeof document === "undefined" || width <= 0) return 0;
  const lh = opts.lineHeight ?? DEFAULT_LINE_HEIGHT;
  const ls = opts.letterSpacing ?? 0;
  const key = `${width}|${opts.fontSize}|${opts.fontWeight}|${opts.fontFamily ?? ""}|${opts.pad ?? 0}|${lh}|${ls}|${text}`;
  const hit = _cache.get(key);
  if (hit !== undefined) return hit;
  const d = probe();
  d.style.width = `${width}px`;
  d.style.padding = `${opts.pad ?? 0}px`;
  d.style.fontSize = `${opts.fontSize}px`;
  d.style.fontWeight = String(opts.fontWeight);
  d.style.fontFamily = opts.fontFamily ?? "";
  d.style.lineHeight = String(lh);
  d.style.letterSpacing = ls ? `${ls}px` : "normal";
  d.textContent = plain(text) || " ";
  const h = d.offsetHeight;
  _cache.set(key, h);
  return h;
}

/** Row-marker gutter for a list, in px — the hanging-indent width the render
 *  reserves for "•" / "12.". Kept here so measure + render agree. */
export function listMarkerWidth(fontSize: number): number {
  return Math.round(fontSize * 1.4);
}

/** Default spacing between list rows when the element gives no explicit `gap`. */
export function listGap(el: SlideElement): number {
  return el.gap ?? Math.round((el.fontSize ?? 28) * 0.5);
}

/** Measured height of a list element: each row wraps in the width left of its
 *  marker gutter; total = Σ row heights + gaps + padding. Mirrors the render in
 *  element-body.tsx so hug height matches the drawn box. */
function measureListHeight(el: SlideElement, deckFont?: string): number {
  const items = el.items ?? [];
  const pad = el.pad ?? 0;
  if (items.length === 0) return el.h;
  const fontSize = el.fontSize ?? 28;
  const textW = Math.max(1, el.w - pad * 2 - listMarkerWidth(fontSize));
  const gap = listGap(el);
  let total = 0;
  for (const item of items) {
    const rh = measureTextHeight(item || " ", textW, {
      fontSize,
      fontWeight: el.fontWeight ?? 400,
      fontFamily: el.font ?? deckFont,
      pad: 0,
      lineHeight: el.lineHeight,
      letterSpacing: el.letterSpacing,
    });
    total += rh > 0 ? rh : 0;
  }
  const h = total + gap * Math.max(0, items.length - 1) + pad * 2;
  return h > 0 ? h : el.h;
}

// ── kpi ───────────────────────────────────────────────────────────────────
// Shared sub-part sizing so the measured height matches the drawn KPI exactly.

/** The KPI's figure size (its `fontSize`, default 72). */
export function kpiValueSize(el: SlideElement): number {
  return el.fontSize ?? 72;
}
/** The KPI's label + delta size (derived from the figure size). */
export function kpiSubSize(el: SlideElement): number {
  return Math.max(14, Math.round(kpiValueSize(el) * 0.28));
}
/** Gap between the figure and the label/delta row. */
export function kpiGap(el: SlideElement): number {
  return el.gap ?? Math.max(6, Math.round(kpiValueSize(el) * 0.12));
}
const KPI_VALUE_LH = 1.05;

/** Measured height of a KPI: figure line + (label/delta row) + padding. */
function measureKpiHeight(el: SlideElement, deckFont?: string): number {
  const pad = el.pad ?? 0;
  const valueSize = kpiValueSize(el);
  const w = Math.max(1, el.w - pad * 2);
  const valueH = measureTextHeight(el.value ?? " ", w, {
    fontSize: valueSize,
    fontWeight: el.fontWeight ?? 700,
    fontFamily: el.font ?? deckFont,
    pad: 0,
    lineHeight: KPI_VALUE_LH,
  });
  const subText = el.label || el.delta;
  let subH = 0;
  if (subText) {
    subH = measureTextHeight(subText, w, {
      fontSize: kpiSubSize(el),
      fontWeight: 400,
      fontFamily: el.font ?? deckFont,
      pad: 0,
      lineHeight: 1.2,
    });
  }
  const h = (valueH > 0 ? valueH : valueSize) + (subH > 0 ? kpiGap(el) + subH : 0) + pad * 2;
  return h > 0 ? h : el.h;
}

// ── quote ─────────────────────────────────────────────────────────────────
// Shared geometry so the measured height matches the drawn blockquote.

/** The quote's left accent-bar inset (also the gutter the “ mark hangs in). */
export function quotePadLeft(el: SlideElement): number {
  return Math.round((el.fontSize ?? 40) * 0.5);
}
/** The attribution ("— name") size, derived from the quote size. */
export function quoteAttrSize(el: SlideElement): number {
  return Math.max(14, Math.round((el.fontSize ?? 40) * 0.5));
}
/** Gap between the quotation and its attribution. */
export function quoteGap(el: SlideElement): number {
  return el.gap ?? Math.round((el.fontSize ?? 40) * 0.3);
}

/** Measured height of a quote: quotation text + (attribution) + padding. */
function measureQuoteHeight(el: SlideElement, deckFont?: string): number {
  const pad = el.pad ?? 0;
  const fontSize = el.fontSize ?? 40;
  const w = Math.max(1, el.w - pad * 2 - quotePadLeft(el));
  const textH = measureTextHeight(el.text ?? " ", w, {
    fontSize,
    fontWeight: el.fontWeight ?? 400,
    fontFamily: el.font ?? deckFont,
    pad: 0,
    lineHeight: el.lineHeight,
  });
  let attrH = 0;
  if (el.attribution) {
    attrH = measureTextHeight(el.attribution, w, {
      fontSize: quoteAttrSize(el),
      fontWeight: 400,
      fontFamily: el.font ?? deckFont,
      pad: 0,
      lineHeight: 1.2,
    });
  }
  const h = (textH > 0 ? textH : fontSize) + (attrH > 0 ? quoteGap(el) + attrH : 0) + pad * 2;
  return h > 0 ? h : el.h;
}

// ── table ───────────────────────────────────────────────────────────────────
// Shared cell geometry so the measured (hugged) height matches the drawn table.
// Columns split the width equally (CSS `repeat(n, 1fr)`), so measure uses the
// same equal column widths the render does.

export const TABLE_CELL_PAD_X = 14;
export const TABLE_CELL_PAD_Y = 9;

/** A table's cell font size (its `fontSize`, default 22). */
export function tableFontSize(el: SlideElement): number {
  return el.fontSize ?? 22;
}
/** Column count — from `columns`, else the first row's width, min 1. */
export function tableColumnCount(el: SlideElement): number {
  return Math.max(1, el.columns?.length || el.rows?.[0]?.length || 1);
}
/** Whether the header row renders (default on when columns exist). */
export function tableHasHeader(el: SlideElement): boolean {
  return el.header !== false && (el.columns?.length ?? 0) > 0;
}

/** Measured height of a table: Σ row heights (header + body). Each row's height
 *  is its tallest wrapped cell + vertical cell padding; cells wrap in the equal
 *  column width less horizontal padding. Mirrors the render in element-body.tsx. */
function measureTableHeight(el: SlideElement, deckFont?: string): number {
  const pad = el.pad ?? 0;
  const cols = tableColumnCount(el);
  const colW = Math.max(1, (el.w - pad * 2) / cols);
  const cellW = Math.max(1, colW - TABLE_CELL_PAD_X * 2);
  const fs = tableFontSize(el);
  const rowHeight = (cells: string[], weight: number): number => {
    let m = 0;
    for (let i = 0; i < cols; i++) {
      const rh = measureTextHeight(cells[i] || " ", cellW, {
        fontSize: fs,
        fontWeight: weight,
        fontFamily: el.font ?? deckFont,
        pad: 0,
        lineHeight: el.lineHeight,
      });
      if (rh > m) m = rh;
    }
    return (m > 0 ? m : fs) + TABLE_CELL_PAD_Y * 2;
  };
  let total = pad * 2;
  if (tableHasHeader(el)) total += rowHeight(el.columns ?? [], 700);
  for (const row of el.rows ?? []) total += rowHeight(row, 400);
  return total > 0 ? total : el.h;
}

/** Effective height for an element: text + text-bearing primitives hug their
 *  wrapped content; every other kind uses its fixed declared height. */
export function elementHeight(el: SlideElement, deckFont?: string): number {
  if (el.kind === "list") return measureListHeight(el, deckFont);
  if (el.kind === "kpi") return measureKpiHeight(el, deckFont);
  if (el.kind === "quote") return measureQuoteHeight(el, deckFont);
  if (el.kind === "table") return measureTableHeight(el, deckFont);
  if (el.kind !== "text") return el.h;
  const h = measureTextHeight(el.text ?? "", el.w, {
    fontSize: el.fontSize ?? 28,
    fontWeight: el.fontWeight ?? 400,
    fontFamily: el.font ?? deckFont,
    pad: el.pad ?? 0,
    lineHeight: el.lineHeight,
    letterSpacing: el.letterSpacing,
  });
  return h > 0 ? h : el.h;
}
