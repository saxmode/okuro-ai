import type { CSSProperties, ReactNode } from "react";
import type { DeltaDir, SlideElement } from "./scene";
import {
  kpiGap,
  kpiSubSize,
  kpiValueSize,
  listGap,
  listMarkerWidth,
  quoteAttrSize,
  quoteGap,
  quotePadLeft,
  tableColumnCount,
  tableFontSize,
  tableHasHeader,
  TABLE_CELL_PAD_X,
  TABLE_CELL_PAD_Y,
} from "./measure";
import { renderRich } from "./rich-text";
import { chartSvg } from "./chart-svg";

/**
 * element-body — the ONE renderer for Phase C component primitives (list, …),
 * shared by the deck renderer (slide-deck.tsx), the editor canvas
 * (slides-editor.tsx) and, via `elementBodyHtml`, the export path (export.ts).
 *
 * The outer element box (geometry, background, scrim, font/color) is owned by
 * whoever mounts this — here we render only the INNER content, inheriting the
 * box's font-size/color so a morph tween of those properties still animates.
 */

/** React body for a composite element (`isComposite(el.kind)`). `surface` is the
 *  background the element sits on (deck background) — only the chart needs it, to
 *  pick light-vs-dark ink when the chart itself has no `bg`. */
export function ElementInner({ el, surface }: { el: SlideElement; surface?: string }): ReactNode {
  if (el.kind === "list") return <ListBody el={el} />;
  if (el.kind === "kpi") return <KpiBody el={el} />;
  if (el.kind === "quote") return <QuoteBody el={el} />;
  if (el.kind === "divider") return <DividerBody el={el} />;
  if (el.kind === "chart") return <ChartBody el={el} surface={surface} />;
  if (el.kind === "table") return <TableBody el={el} />;
  return renderRich(el.text);
}

// ── table ────────────────────────────────────────────────────────────────────
// A CSS-grid table (repeat(n, 1fr) — equal columns, matching measure.ts). The
// header row is brand-tinted + bold; body rows are separated by a subtle hairline
// (never a heavy border — data is the loud thing). Numeric cells align on
// tabular figures. Hugs its content height.

const TABLE_HEADER_BG = "rgba(143,240,164,0.14)"; // okuro accent tint
const TABLE_SEP = "rgba(255,255,255,0.10)";

function TableBody({ el }: { el: SlideElement }): ReactNode {
  const cols = tableColumnCount(el);
  const columns = el.columns ?? [];
  const rows = el.rows ?? [];
  const header = tableHasHeader(el);
  const fs = tableFontSize(el);
  const cell: CSSProperties = {
    padding: `${TABLE_CELL_PAD_Y}px ${TABLE_CELL_PAD_X}px`,
    borderBottom: `1px solid ${TABLE_SEP}`,
    fontVariantNumeric: "tabular-nums",
    minWidth: 0,
    overflowWrap: "anywhere",
  };
  return (
    <div style={{ display: "grid", gridTemplateColumns: `repeat(${cols}, 1fr)`, width: "100%", fontSize: fs }}>
      {header &&
        columns.map((c, i) => (
          <div key={`h${i}`} style={{ ...cell, fontWeight: 700, background: TABLE_HEADER_BG }}>
            {renderRich(c)}
          </div>
        ))}
      {rows.map((row, r) =>
        Array.from({ length: cols }, (_, i) => (
          <div key={`${r}_${i}`} style={cell}>
            {renderRich(row[i] ?? "")}
          </div>
        )),
      )}
    </div>
  );
}

// ── chart ──────────────────────────────────────────────────────────────────
// Inline SVG (chart-svg.ts) injected via dangerouslySetInnerHTML — the SAME
// string the export path serializes, so the rendered chart and the exported one
// are byte-identical. The SVG scales to fill the (possibly morphing) box.

function ChartBody({ el, surface }: { el: SlideElement; surface?: string }): ReactNode {
  return <div style={{ width: "100%", height: "100%" }} dangerouslySetInnerHTML={{ __html: chartSvg(el, surface) }} />;
}

// ── divider ────────────────────────────────────────────────────────────────
// A rule (composite render, fixed geometry — NOT a hug kind). The bar is
// centred in the element box so the box can be a comfortable click target.

const ACCENT = "#8ff0a4";
export function dividerColor(el: SlideElement): string {
  return el.accent ? ACCENT : el.color ?? "#39463d";
}
function dividerThickness(el: SlideElement): number {
  return Math.max(1, el.thickness ?? 2);
}

function DividerBody({ el }: { el: SlideElement }): ReactNode {
  const t = dividerThickness(el);
  const vertical = el.orientation === "v";
  const bar: CSSProperties = vertical
    ? { width: t, height: "100%", borderRadius: t / 2 }
    : { width: "100%", height: t, borderRadius: t / 2 };
  return (
    <div style={{ display: "flex", width: "100%", height: "100%", alignItems: "center", justifyContent: "center" }}>
      <div style={{ ...bar, background: dividerColor(el) }} />
    </div>
  );
}

// Colour + arrow for a KPI delta direction. Plain hexes (mirrors the status
// palette used across the editor) so the export HTML path can reuse them.
const DELTA: Record<DeltaDir, { color: string; arrow: string }> = {
  up: { color: "#22c55e", arrow: "▲" },
  down: { color: "#ef4444", arrow: "▼" },
  flat: { color: "#9ca3af", arrow: "▬" },
};
function deltaStyle(dir?: DeltaDir): { color: string; arrow: string } {
  return DELTA[dir ?? "flat"];
}

// ── list ─────────────────────────────────────────────────────────────────────

function marker(el: SlideElement, i: number): string {
  return el.ordered ? `${i + 1}.` : "•";
}

function ListBody({ el }: { el: SlideElement }): ReactNode {
  const items = el.items ?? [];
  const fontSize = el.fontSize ?? 28;
  const gutter = listMarkerWidth(fontSize);
  const gap = listGap(el);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap, width: "100%" }}>
      {items.map((item, i) => (
        <div key={i} style={{ display: "flex", alignItems: "flex-start", width: "100%" }}>
          <span
            style={{
              width: gutter,
              flex: "0 0 auto",
              textAlign: el.ordered ? "right" : "center",
              paddingRight: el.ordered ? Math.round(fontSize * 0.3) : Math.round(fontSize * 0.35),
              opacity: el.ordered ? 1 : 0.9,
            }}
            aria-hidden
          >
            {marker(el, i)}
          </span>
          <span style={{ flex: "1 1 auto", minWidth: 0 }}>{renderRich(item)}</span>
        </div>
      ))}
    </div>
  );
}

// ── kpi ──────────────────────────────────────────────────────────────────────

function KpiBody({ el }: { el: SlideElement }): ReactNode {
  const valueSize = kpiValueSize(el);
  const subSize = kpiSubSize(el);
  const gap = kpiGap(el);
  const cross = el.align === "center" ? "center" : el.align === "right" ? "flex-end" : "flex-start";
  const d = deltaStyle(el.deltaDir);
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: cross, gap, width: "100%" }}>
      <div style={{ fontSize: valueSize, fontWeight: el.fontWeight ?? 700, lineHeight: 1.05 }}>
        {el.value ?? ""}
      </div>
      {(el.label || el.delta) && (
        <div style={{ display: "flex", alignItems: "baseline", gap: Math.round(subSize * 0.5), flexWrap: "wrap" }}>
          {el.label && <span style={{ fontSize: subSize, opacity: 0.72 }}>{el.label}</span>}
          {el.delta && (
            <span style={{ fontSize: subSize, fontWeight: 600, color: d.color, whiteSpace: "nowrap" }}>
              {d.arrow} {el.delta}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ── quote ─────────────────────────────────────────────────────────────────────

function QuoteBody({ el }: { el: SlideElement }): ReactNode {
  const fontSize = el.fontSize ?? 40;
  const padLeft = quotePadLeft(el);
  const attrSize = quoteAttrSize(el);
  const gap = quoteGap(el);
  return (
    <div style={{ position: "relative", width: "100%", paddingLeft: padLeft, boxSizing: "border-box" }}>
      <span
        aria-hidden
        style={{ position: "absolute", left: 0, top: `-${Math.round(fontSize * 0.25)}px`, fontSize: Math.round(fontSize * 1.7), lineHeight: 1, opacity: 0.28, fontWeight: 700 }}
      >
        “
      </span>
      <div style={{ borderLeft: `${Math.max(2, Math.round(fontSize * 0.08))}px solid currentColor`, paddingLeft: Math.round(fontSize * 0.4), fontStyle: "italic" }}>
        {renderRich(el.text)}
      </div>
      {el.attribution && (
        <div style={{ marginTop: gap, fontSize: attrSize, fontStyle: "normal", opacity: 0.7 }}>— {el.attribution}</div>
      )}
    </div>
  );
}

// ── HTML string body (export: PDF print window + PNG foreignObject) ───────────

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function inlineHtml(text: string): string {
  return esc(text)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
}

/** HTML string for a composite element's inner body — mirrors ElementInner for
 *  the export renderers (which build HTML strings, not React). */
export function elementBodyHtml(el: SlideElement, surface?: string): string {
  if (el.kind === "list") {
    const items = el.items ?? [];
    const fontSize = el.fontSize ?? 28;
    const gutter = listMarkerWidth(fontSize);
    const gap = listGap(el);
    const rows = items
      .map((item, i) => {
        const mk = el.ordered ? `${i + 1}.` : "•";
        const align = el.ordered ? "right" : "center";
        const padR = el.ordered ? Math.round(fontSize * 0.3) : Math.round(fontSize * 0.35);
        return (
          `<div style="display:flex;align-items:flex-start;width:100%;">` +
          `<span style="width:${gutter}px;flex:0 0 auto;text-align:${align};padding-right:${padR}px;">${esc(mk)}</span>` +
          `<span style="flex:1 1 auto;min-width:0;">${inlineHtml(item)}</span>` +
          `</div>`
        );
      })
      .join("");
    return `<div style="display:flex;flex-direction:column;gap:${gap}px;width:100%;">${rows}</div>`;
  }
  if (el.kind === "kpi") {
    const valueSize = kpiValueSize(el);
    const subSize = kpiSubSize(el);
    const gap = kpiGap(el);
    const cross = el.align === "center" ? "center" : el.align === "right" ? "flex-end" : "flex-start";
    const d = deltaStyle(el.deltaDir);
    const label = el.label ? `<span style="font-size:${subSize}px;opacity:0.72;">${esc(el.label)}</span>` : "";
    const delta = el.delta
      ? `<span style="font-size:${subSize}px;font-weight:600;color:${d.color};white-space:nowrap;">${d.arrow} ${esc(el.delta)}</span>`
      : "";
    const sub =
      label || delta
        ? `<div style="display:flex;align-items:baseline;gap:${Math.round(subSize * 0.5)}px;flex-wrap:wrap;">${label}${delta}</div>`
        : "";
    return (
      `<div style="display:flex;flex-direction:column;align-items:${cross};gap:${gap}px;width:100%;">` +
      `<div style="font-size:${valueSize}px;font-weight:${el.fontWeight ?? 700};line-height:1.05;">${esc(el.value ?? "")}</div>` +
      `${sub}</div>`
    );
  }
  if (el.kind === "quote") {
    const fontSize = el.fontSize ?? 40;
    const padLeft = quotePadLeft(el);
    const attrSize = quoteAttrSize(el);
    const gap = quoteGap(el);
    const bar = Math.max(2, Math.round(fontSize * 0.08));
    const mark =
      `<span aria-hidden style="position:absolute;left:0;top:-${Math.round(fontSize * 0.25)}px;` +
      `font-size:${Math.round(fontSize * 1.7)}px;line-height:1;opacity:0.28;font-weight:700;">&ldquo;</span>`;
    const attr = el.attribution
      ? `<div style="margin-top:${gap}px;font-size:${attrSize}px;font-style:normal;opacity:0.7;">&mdash; ${esc(el.attribution)}</div>`
      : "";
    return (
      `<div style="position:relative;width:100%;padding-left:${padLeft}px;box-sizing:border-box;">${mark}` +
      `<div style="border-left:${bar}px solid currentColor;padding-left:${Math.round(fontSize * 0.4)}px;font-style:italic;">${inlineHtml(el.text ?? "")}</div>` +
      `${attr}</div>`
    );
  }
  if (el.kind === "divider") {
    const t = dividerThickness(el);
    const vertical = el.orientation === "v";
    const bar = vertical
      ? `width:${t}px;height:100%;`
      : `width:100%;height:${t}px;`;
    return (
      `<div style="display:flex;width:100%;height:100%;align-items:center;justify-content:center;">` +
      `<div style="${bar}border-radius:${t / 2}px;background:${dividerColor(el)};"></div></div>`
    );
  }
  if (el.kind === "chart") {
    return `<div style="width:100%;height:100%;">${chartSvg(el, surface)}</div>`;
  }
  if (el.kind === "table") {
    const cols = tableColumnCount(el);
    const columns = el.columns ?? [];
    const rows = el.rows ?? [];
    const header = tableHasHeader(el);
    const fs = tableFontSize(el);
    const cellCss = `padding:${TABLE_CELL_PAD_Y}px ${TABLE_CELL_PAD_X}px;border-bottom:1px solid ${TABLE_SEP};font-variant-numeric:tabular-nums;min-width:0;overflow-wrap:anywhere;`;
    const cells: string[] = [];
    if (header) {
      for (let i = 0; i < cols; i++) {
        cells.push(`<div style="${cellCss}font-weight:700;background:${TABLE_HEADER_BG};">${inlineHtml(columns[i] ?? "")}</div>`);
      }
    }
    for (const row of rows) {
      for (let i = 0; i < cols; i++) cells.push(`<div style="${cellCss}">${inlineHtml(row[i] ?? "")}</div>`);
    }
    return `<div style="display:grid;grid-template-columns:repeat(${cols},1fr);width:100%;font-size:${fs}px;">${cells.join("")}</div>`;
  }
  return inlineHtml(el.text ?? "");
}

/** Style overrides the OUTER box of a composite element wants (beyond the shared
 *  typography style). Currently none, but kept as the single seam for future
 *  kinds so callers spread one object. */
export function compositeBoxStyle(_el: SlideElement): CSSProperties {
  return {};
}
