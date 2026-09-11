import type { ChartSeries, ChartType, Deck, DeltaDir, ElementProvenance, FrameLayout, SlideElement } from "./scene";
import { DEFAULT_LINE_HEIGHT, hugs } from "./scene";
import {
  alignElements,
  bringToFront,
  distributeElements,
  groupElements,
  removeElement,
  sendToBack,
  setFrameLayout,
  THEMES,
  ungroupFrame,
  updateElement,
  type AlignMode,
} from "./scene-ops";

// Font choices for the family select: deck font + theme fonts + common stacks.
const SYSTEM_FONTS = [
  "Inter, system-ui, sans-serif",
  "Georgia, 'Times New Roman', serif",
  "'JetBrains Mono', ui-monospace, monospace",
];
function fontOptions(deckFont?: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const f of [deckFont, ...THEMES.map((t) => t.font), ...SYSTEM_FONTS]) {
    if (f && !seen.has(f)) { seen.add(f); out.push(f); }
  }
  return out;
}
function fontLabel(f: string): string {
  return f.split(",")[0]!.replace(/['"]/g, "").trim();
}

// ── chart data ⇄ text (minimal editing surface — no data-grid) ────────────────
// One series per line: "Name: 1, 2, 3". Line-based so every intermediate keypress
// still parses (unlike live JSON), keeping the textarea editable.
function seriesToText(series?: ChartSeries[]): string {
  return (series ?? []).map((s) => `${s.name}: ${s.values.join(", ")}`).join("\n");
}
function parseSeries(text: string): ChartSeries[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, i) => {
      const ci = line.indexOf(":");
      const name = ci >= 0 ? line.slice(0, ci).trim() || `Series ${i + 1}` : `Series ${i + 1}`;
      const rest = ci >= 0 ? line.slice(ci + 1) : line;
      const values = rest.split(/[,\s]+/).map(Number).filter((n) => Number.isFinite(n));
      return { name, values };
    })
    .filter((s) => s.values.length > 0);
}
function csvToArr(text: string): string[] {
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

// ── table data ⇄ text ─────────────────────────────────────────────────────────
// Columns = one comma-separated line; rows = one row per line, cells separated by
// " | " (a pipe reads cleaner than commas for prose cells).
function rowsToText(rows?: string[][]): string {
  return (rows ?? []).map((r) => r.join(" | ")).join("\n");
}
function parseRows(text: string): string[][] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.split("|").map((c) => c.trim()));
}

// "Can't-lie" gate verdict pill. Hover shows the reason ("why this is true").
const PROV_STYLE: Record<ElementProvenance["status"], { label: string; cls: string; tip: string }> = {
  grounded: { label: "grounded", cls: "border-[var(--color-status-success,#11d425)] text-[var(--color-status-success,#11d425)]", tip: "Supported by the deck's grounding source." },
  inferred: { label: "inferred", cls: "border-[var(--color-status-warning,#c52998)] text-[var(--color-status-warning,#c52998)]", tip: "Plausible, but not explicitly stated in the source." },
  fabricated: { label: "unsupported", cls: "border-[var(--color-status-error,#f92f77)] text-[var(--color-status-error,#f92f77)]", tip: "The grounding source does NOT support this claim." },
  unverified: { label: "unverified", cls: "border-border text-tertiary", tip: "No grounding source was available to check against." },
};
function ProvenanceBadge({ p }: { p: ElementProvenance }) {
  const s = PROV_STYLE[p.status] ?? PROV_STYLE.unverified;
  return (
    <div className={`flex items-center gap-1 self-start rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide ${s.cls}`} title={p.why?.trim() || s.tip}>
      <span aria-hidden>◆</span>
      {s.label}
    </div>
  );
}

/**
 * ElementInspector — right-pane editor. Adapts to the selection:
 *  - 0 selected → hint
 *  - 1 selected → element properties + z-order
 *  - 2+ selected → align / distribute / z-order on the group
 */
export function ElementInspector({
  deck,
  slideIndex,
  selectedIds,
  onSelect,
  onChange,
}: {
  deck: Deck;
  slideIndex: number;
  selectedIds: string[];
  onSelect: (ids: string[]) => void;
  onChange: (deck: Deck) => void;
}) {
  const slide = deck.slides[slideIndex];
  const slideId = slide?.id ?? "";
  const sel = slide?.elements.find((e) => e.id === selectedIds[0]) ?? null;

  const ico = "rounded border border-border px-2 py-1 text-xs text-fg hover:border-accent hover:text-accent";

  const zRow = (
    <div className="flex flex-wrap gap-1">
      <span className="mr-1 self-center text-xs text-tertiary">order</span>
      <button className={ico} onClick={() => onChange(bringToFront(deck, slideId, selectedIds))}>Front</button>
      <button className={ico} onClick={() => onChange(sendToBack(deck, slideId, selectedIds))}>Back</button>
    </div>
  );

  if (selectedIds.length === 0) {
    return (
      <div className="flex flex-col gap-1.5 p-3 text-xs text-tertiary">
        <div>Select an element to edit. Shift-click to multi-select; drag to move, corner to resize.</div>
        <div className="flex flex-col gap-0.5 border-t border-border pt-1.5">
          <span>⌘Z / ⇧⌘Z &nbsp;undo / redo</span>
          <span>⌘D &nbsp;duplicate &nbsp;·&nbsp; ⌘C / ⌘V &nbsp;copy / paste</span>
          <span>⌫ &nbsp;delete &nbsp;·&nbsp; ←↑↓→ &nbsp;nudge (⇧ = 10px)</span>
          <span>double-click text to edit</span>
        </div>
      </div>
    );
  }

  if (selectedIds.length > 1) {
    const align = (m: AlignMode) => onChange(alignElements(deck, slideId, selectedIds, m));
    const dist = (a: "h" | "v") => onChange(distributeElements(deck, slideId, selectedIds, a));
    return (
      <div className="flex flex-col gap-3 p-3 text-sm">
        <span className="text-xs uppercase tracking-wide text-accent">{selectedIds.length} selected</span>
        <div>
          <div className="mb-1 text-xs text-tertiary">align</div>
          <div className="flex flex-wrap gap-1">
            <button className={ico} onClick={() => align("left")} title="Align left">⫷ L</button>
            <button className={ico} onClick={() => align("centerX")} title="Align center">↔ C</button>
            <button className={ico} onClick={() => align("right")} title="Align right">R ⫸</button>
            <button className={ico} onClick={() => align("top")} title="Align top">⫯ T</button>
            <button className={ico} onClick={() => align("middle")} title="Align middle">↕ M</button>
            <button className={ico} onClick={() => align("bottom")} title="Align bottom">B ⫰</button>
          </div>
        </div>
        <div>
          <div className="mb-1 text-xs text-tertiary">distribute (3+)</div>
          <div className="flex gap-1">
            <button className={ico} onClick={() => dist("h")} title="Distribute horizontally">horizontally</button>
            <button className={ico} onClick={() => dist("v")} title="Distribute vertically">vertically</button>
          </div>
        </div>
        <button
          className="rounded-md bg-accent/15 px-3 py-1 text-sm text-accent hover:bg-accent/25"
          onClick={() => { const { deck: d, frameId } = groupElements(deck, slideId, selectedIds); onChange(d); if (frameId) onSelect([frameId]); }}
        >
          Group
        </button>
        {zRow}
      </div>
    );
  }

  if (!sel) return <div className="p-3 text-xs text-tertiary">…</div>;

  if (sel.kind === "frame") {
    const lay: FrameLayout = sel.layout ?? { flow: "none", gap: 16, padX: 16, padY: 16, align: "start" };
    const setLay = (p: Partial<FrameLayout>) => onChange(setFrameLayout(deck, slideId, sel.id, p));
    const layNum = (label: string, key: "gap" | "padX" | "padY") => (
      <label className="flex items-center justify-between gap-2 text-tertiary">
        {label}
        <input type="number" value={lay[key]} onChange={(e) => setLay({ [key]: Number(e.target.value) || 0 } as Partial<FrameLayout>)} className="w-20 rounded border border-border bg-transparent px-1.5 py-0.5 text-fg" />
      </label>
    );
    return (
      <div className="flex flex-col gap-3 p-3 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wide text-accent">frame</span>
          <button onClick={() => { onChange(removeElement(deck, slideId, sel.id)); onSelect([]); }} className="rounded border border-border px-2 py-0.5 text-xs text-[var(--color-status-error,#f92f77)] hover:border-[var(--color-status-error,#f92f77)]">Delete</button>
        </div>
        <label className="flex items-center gap-2 text-xs text-tertiary">
          auto-layout
          <select value={lay.flow} onChange={(e) => setLay({ flow: e.target.value as FrameLayout["flow"] })} className="rounded border border-border bg-transparent px-1 py-0.5 text-fg">
            <option value="none" className="bg-background">free</option>
            <option value="col" className="bg-background">stack ↓</option>
            <option value="row" className="bg-background">stack →</option>
          </select>
        </label>
        {lay.flow !== "none" && (
          <div className="grid grid-cols-3 gap-2 text-xs">
            {layNum("gap", "gap")}
            {layNum("padX", "padX")}
            {layNum("padY", "padY")}
          </div>
        )}
        {lay.flow !== "none" && (
          <label className="flex items-center gap-2 text-xs text-tertiary">
            align
            <select value={lay.align} onChange={(e) => setLay({ align: e.target.value as FrameLayout["align"] })} className="rounded border border-border bg-transparent px-1 py-0.5 text-fg">
              <option value="start" className="bg-background">start</option>
              <option value="center" className="bg-background">center</option>
              <option value="end" className="bg-background">end</option>
            </select>
          </label>
        )}
        <button className={ico} onClick={() => { const { deck: d, ids } = ungroupFrame(deck, slideId, sel.id); onChange(d); onSelect(ids); }}>Ungroup</button>
        {zRow}
      </div>
    );
  }

  // Kinds that get the font / line-height / tracking controls (glyph-bearing,
  // author-set typography). kpi is excluded — its sub-sizes are derived.
  const textual = sel.kind === "text" || sel.kind === "list" || sel.kind === "quote";
  const patch = (p: Partial<SlideElement>) => onChange(updateElement(deck, slideId, sel.id, p));
  const num = (label: string, key: keyof SlideElement, fallback: number) => (
    <label className="flex items-center justify-between gap-2 text-tertiary">
      {label}
      <input
        type="number"
        value={(sel[key] as number) ?? fallback}
        onChange={(e) => patch({ [key]: Number(e.target.value) || 0 } as Partial<SlideElement>)}
        className="w-20 rounded border border-border bg-transparent px-1.5 py-0.5 text-fg"
      />
    </label>
  );
  // Float-capable number (lineHeight/letterSpacing): keeps 0 instead of falling back.
  const fnum = (label: string, key: keyof SlideElement, fallback: number, step: number) => (
    <label className="flex items-center justify-between gap-2 text-tertiary">
      {label}
      <input
        type="number"
        step={step}
        value={(sel[key] as number) ?? fallback}
        onChange={(e) => patch({ [key]: Number(e.target.value) } as Partial<SlideElement>)}
        className="w-20 rounded border border-border bg-transparent px-1.5 py-0.5 text-fg"
      />
    </label>
  );

  return (
    <div className="flex flex-col gap-3 p-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-accent">{sel.kind}</span>
        <button
          onClick={() => { onChange(removeElement(deck, slideId, sel.id)); onSelect([]); }}
          className="rounded border border-border px-2 py-0.5 text-xs text-[var(--color-status-error,#f92f77)] hover:border-[var(--color-status-error,#f92f77)]"
        >
          Delete
        </button>
      </div>

      {sel.provenance && <ProvenanceBadge p={sel.provenance} />}

      {(sel.kind === "text" || sel.kind === "quote") && (
        <textarea value={sel.text ?? ""} onChange={(e) => patch({ text: e.target.value })} rows={4} placeholder={sel.kind === "quote" ? "the quotation" : undefined} className="resize-y rounded border border-border bg-transparent px-2 py-1 font-mono text-xs text-fg" />
      )}

      {sel.kind === "quote" && (
        <label className="flex flex-col gap-1 text-xs text-tertiary">
          attribution
          <input type="text" value={sel.attribution ?? ""} placeholder="who said it" onChange={(e) => patch({ attribution: e.target.value || undefined })} className="rounded border border-border bg-transparent px-2 py-1 text-fg" />
        </label>
      )}

      {sel.kind === "list" && (
        <div className="flex flex-col gap-2">
          <textarea
            value={(sel.items ?? []).join("\n")}
            onChange={(e) => patch({ items: e.target.value.split("\n") })}
            rows={5}
            placeholder="one item per line"
            className="resize-y rounded border border-border bg-transparent px-2 py-1 font-mono text-xs text-fg"
          />
          <label className="flex items-center gap-2 text-xs text-tertiary">
            <input type="checkbox" checked={!!sel.ordered} onChange={(e) => patch({ ordered: e.target.checked })} className="accent-accent" />
            numbered (1. 2. 3.)
          </label>
        </div>
      )}

      {sel.kind === "divider" && (
        <div className="flex flex-col gap-2 text-xs text-tertiary">
          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1">
              orientation
              <select value={sel.orientation ?? "h"} onChange={(e) => patch({ orientation: e.target.value as "h" | "v" })} className="rounded border border-border bg-transparent px-2 py-1 text-fg">
                <option value="h" className="bg-background">horizontal —</option>
                <option value="v" className="bg-background">vertical |</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              thickness
              <input type="number" value={sel.thickness ?? 2} onChange={(e) => patch({ thickness: Math.max(1, Number(e.target.value) || 1) })} className="rounded border border-border bg-transparent px-2 py-1 text-fg" />
            </label>
          </div>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={!!sel.accent} onChange={(e) => patch({ accent: e.target.checked })} className="accent-accent" />
            accent colour
          </label>
        </div>
      )}

      {sel.kind === "chart" && (
        <div className="flex flex-col gap-2 text-xs text-tertiary">
          <label className="flex items-center justify-between gap-2">
            type
            <select value={sel.chartType ?? "bar"} onChange={(e) => patch({ chartType: e.target.value as ChartType })} className="rounded border border-border bg-transparent px-2 py-1 text-fg">
              <option value="bar" className="bg-background">bar</option>
              <option value="line" className="bg-background">line</option>
              <option value="area" className="bg-background">area</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            categories (comma-separated)
            <input type="text" value={(sel.categories ?? []).join(", ")} placeholder="Q1, Q2, Q3, Q4" onChange={(e) => patch({ categories: csvToArr(e.target.value) })} className="rounded border border-border bg-transparent px-2 py-1 text-fg" />
          </label>
          <label className="flex flex-col gap-1">
            series (one per line — "Name: 1, 2, 3")
            <textarea value={seriesToText(sel.series)} onChange={(e) => patch({ series: parseSeries(e.target.value) })} rows={4} placeholder={"Revenue: 12, 19, 15, 27\nCost: 8, 10, 9, 14"} className="resize-y rounded border border-border bg-transparent px-2 py-1 font-mono text-fg" />
          </label>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={sel.showAxes !== false} onChange={(e) => patch({ showAxes: e.target.checked })} className="accent-accent" />
              axes
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={sel.showLegend ?? (sel.series?.length ?? 0) > 1} onChange={(e) => patch({ showLegend: e.target.checked })} className="accent-accent" />
              legend
            </label>
          </div>
          <span className="text-[10px] leading-tight text-tertiary/80">1 series uses the element colour; 2+ use the validated data-viz palette.</span>
        </div>
      )}

      {sel.kind === "table" && (
        <div className="flex flex-col gap-2 text-xs text-tertiary">
          <label className="flex flex-col gap-1">
            columns (comma-separated)
            <input type="text" value={(sel.columns ?? []).join(", ")} placeholder="Metric, Q3, Q4" onChange={(e) => patch({ columns: csvToArr(e.target.value) })} className="rounded border border-border bg-transparent px-2 py-1 text-fg" />
          </label>
          <label className="flex flex-col gap-1">
            rows (one per line — cells split by " | ")
            <textarea value={rowsToText(sel.rows)} onChange={(e) => patch({ rows: parseRows(e.target.value) })} rows={5} placeholder={"Revenue | €1.2M | €1.8M\nUsers | 8,400 | 12,900"} className="resize-y rounded border border-border bg-transparent px-2 py-1 font-mono text-fg" />
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={sel.header !== false} onChange={(e) => patch({ header: e.target.checked })} className="accent-accent" />
            header row
          </label>
        </div>
      )}

      {sel.kind === "kpi" && (
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1 text-xs text-tertiary">
            figure
            <input type="text" value={sel.value ?? ""} placeholder="87%" onChange={(e) => patch({ value: e.target.value })} className="rounded border border-border bg-transparent px-2 py-1 text-fg" />
          </label>
          <label className="flex flex-col gap-1 text-xs text-tertiary">
            label
            <input type="text" value={sel.label ?? ""} placeholder="conversion rate" onChange={(e) => patch({ label: e.target.value || undefined })} className="rounded border border-border bg-transparent px-2 py-1 text-fg" />
          </label>
          <div className="grid grid-cols-2 gap-2 text-xs text-tertiary">
            <label className="flex flex-col gap-1">
              delta
              <input type="text" value={sel.delta ?? ""} placeholder="+12%" onChange={(e) => patch({ delta: e.target.value || undefined })} className="rounded border border-border bg-transparent px-2 py-1 text-fg" />
            </label>
            <label className="flex flex-col gap-1">
              direction
              <select value={sel.deltaDir ?? "flat"} onChange={(e) => patch({ deltaDir: e.target.value as DeltaDir })} className="rounded border border-border bg-transparent px-2 py-1 text-fg">
                <option value="up" className="bg-background">up ▲</option>
                <option value="down" className="bg-background">down ▼</option>
                <option value="flat" className="bg-background">flat ▬</option>
              </select>
            </label>
          </div>
        </div>
      )}

      {sel.kind === "image" && (
        <label className="flex flex-col gap-1 text-xs text-tertiary">
          alt text
          <input
            type="text"
            value={sel.alt ?? ""}
            placeholder="describe the image (a11y)"
            onChange={(e) => patch({ alt: e.target.value || undefined })}
            className="rounded border border-border bg-transparent px-2 py-1 text-fg"
          />
        </label>
      )}

      <div className="grid grid-cols-2 gap-2 text-xs">
        {num("X", "x", 0)}
        {num("Y", "y", 0)}
        {num("W", "w", 0)}
        {!hugs(sel.kind) && num("H", "h", 0)}
        {hugs(sel.kind) && num(sel.kind === "kpi" ? "figure" : "size", "fontSize", sel.kind === "kpi" ? 72 : 28)}
        {hugs(sel.kind) && num("weight", "fontWeight", sel.kind === "kpi" ? 700 : 400)}
        {sel.kind === "list" && num("item gap", "gap", Math.round((sel.fontSize ?? 28) * 0.5))}
        {!hugs(sel.kind) && num("radius", "radius", 0)}
      </div>

      <div className="flex flex-wrap items-center gap-3 text-xs text-tertiary">
        {hugs(sel.kind) && (
          <label className="flex items-center gap-1">
            align
            <select value={sel.align ?? "left"} onChange={(e) => patch({ align: e.target.value as SlideElement["align"] })} className="rounded border border-border bg-transparent px-1 py-0.5 text-fg">
              <option value="left" className="bg-background">left</option>
              <option value="center" className="bg-background">center</option>
              <option value="right" className="bg-background">right</option>
            </select>
          </label>
        )}
        <label className="flex items-center gap-1">text<input type="color" value={sel.color ?? "#eafff0"} onChange={(e) => patch({ color: e.target.value })} /></label>
        <label className="flex items-center gap-1">fill<input type="color" value={(sel.bg ?? "#10231a").startsWith("#") ? (sel.bg ?? "#10231a") : "#10231a"} onChange={(e) => patch({ bg: e.target.value })} /></label>
      </div>

      {sel.kind === "box" && (
        <div className="flex flex-col gap-2 border-t border-border pt-2 text-xs text-tertiary">
          <div className="flex items-center justify-between gap-2">
            <span>border</span>
            <div className="flex items-center gap-1">
              <input type="number" min={0} title="width (0 = none)" value={sel.border?.width ?? 0} onChange={(e) => { const w = Math.max(0, Number(e.target.value) || 0); patch({ border: w > 0 ? { width: w, color: sel.border?.color ?? "#8ff0a4" } : undefined }); }} className="w-16 rounded border border-border bg-transparent px-1.5 py-0.5 text-fg" />
              <input type="color" value={sel.border?.color ?? "#8ff0a4"} onChange={(e) => patch({ border: { width: sel.border?.width || 1, color: e.target.value } })} />
            </div>
          </div>
          <label className="flex items-center justify-between gap-2">
            shadow
            <select value={sel.shadow ?? "none"} onChange={(e) => patch({ shadow: e.target.value as SlideElement["shadow"] })} className="rounded border border-border bg-transparent px-1 py-0.5 text-fg">
              <option value="none" className="bg-background">none</option>
              <option value="sm" className="bg-background">small</option>
              <option value="md" className="bg-background">medium</option>
              <option value="lg" className="bg-background">large</option>
            </select>
          </label>
          <div className="flex items-center justify-between gap-2">
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={!!sel.gradient} onChange={(e) => patch({ gradient: e.target.checked ? { from: sel.gradient?.from ?? sel.bg ?? "#10231a", to: sel.gradient?.to ?? "#0b0f0c", angle: sel.gradient?.angle ?? 180 } : undefined })} className="accent-accent" />
              gradient
            </label>
            {sel.gradient && (
              <div className="flex items-center gap-1">
                <input type="color" value={sel.gradient.from} onChange={(e) => patch({ gradient: { ...sel.gradient!, from: e.target.value } })} />
                <input type="color" value={sel.gradient.to} onChange={(e) => patch({ gradient: { ...sel.gradient!, to: e.target.value } })} />
                <input type="number" title="angle°" value={sel.gradient.angle ?? 180} onChange={(e) => patch({ gradient: { ...sel.gradient!, angle: Number(e.target.value) || 0 } })} className="w-14 rounded border border-border bg-transparent px-1.5 py-0.5 text-fg" />
              </div>
            )}
          </div>
        </div>
      )}

      {textual && (
        <>
          <label className="flex items-center gap-2 text-xs text-tertiary">
            font
            <select
              value={sel.font ?? ""}
              onChange={(e) => patch({ font: e.target.value || undefined })}
              className="min-w-0 flex-1 rounded border border-border bg-transparent px-1 py-0.5 text-fg"
            >
              <option value="" className="bg-background">deck default</option>
              {fontOptions(deck.font).map((f) => (
                <option key={f} value={f} className="bg-background">{fontLabel(f)}</option>
              ))}
            </select>
          </label>
          <div className="grid grid-cols-2 gap-2 text-xs">
            {fnum("line", "lineHeight", DEFAULT_LINE_HEIGHT, 0.05)}
            {fnum("tracking", "letterSpacing", 0, 0.5)}
          </div>
        </>
      )}

      <div className="grid grid-cols-2 gap-2 text-xs">
        {num("padding", "pad", 0)}
        <label className="flex items-center justify-between gap-2 text-tertiary">
          opacity
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={sel.opacity ?? 1}
            onChange={(e) => patch({ opacity: Number(e.target.value) })}
            className="w-20 accent-accent"
          />
        </label>
      </div>

      {zRow}
    </div>
  );
}
