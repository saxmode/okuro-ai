import { useState } from "react";
import type { Deck, SlideElement } from "./scene";
import { reorderZ } from "./scene-ops";

/**
 * LayerPanel — a compact, collapsible list of the current slide's top-level
 * elements. Click selects (shift-click multi-selects); ↑/↓ reorder z. Selection
 * stays in sync with the canvas via the shared `selectedIds`.
 *
 * Rows render TOP-of-stack first (highest effective z) to match how layers read.
 */
const ICONS: Record<SlideElement["kind"], string> = {
  text: "T",
  box: "▭",
  image: "▣",
  video: "▶",
  frame: "⬚",
  flow: "⚙",
  list: "☰",
  kpi: "#",
  quote: "❝",
  divider: "─",
  chart: "📊",
  table: "▦",
};

function labelFor(el: SlideElement): string {
  if (el.kind === "text") {
    const t = (el.text ?? "").replace(/\s+/g, " ").trim();
    return t ? (t.length > 22 ? `${t.slice(0, 22)}…` : t) : "text";
  }
  if (el.kind === "flow") return el.flowId ? `flow · ${el.flowId}` : "flow";
  if (el.kind === "list") {
    const n = el.items?.length ?? 0;
    return `list · ${n} item${n === 1 ? "" : "s"}`;
  }
  if (el.kind === "kpi") return el.value ? `kpi · ${el.value}` : "kpi";
  if (el.kind === "quote") {
    const t = (el.text ?? "").replace(/\s+/g, " ").trim();
    return t ? `quote · ${t.length > 16 ? `${t.slice(0, 16)}…` : t}` : "quote";
  }
  if (el.kind === "chart") {
    const n = el.series?.length ?? 0;
    return `chart · ${el.chartType ?? "bar"}${n ? ` · ${n} series` : ""}`;
  }
  if (el.kind === "table") {
    const r = el.rows?.length ?? 0;
    const c = el.columns?.length ?? el.rows?.[0]?.length ?? 0;
    return `table · ${c}×${r}`;
  }
  return el.kind;
}

export function LayerPanel({
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
  const [open, setOpen] = useState(true);
  const slide = deck.slides[slideIndex];
  const slideId = slide?.id ?? "";
  const els = slide?.elements ?? [];
  // Top-of-stack first.
  const ordered = [...els]
    .map((e, i) => ({ e, i }))
    .sort((a, b) => ((b.e.z ?? 0) - (a.e.z ?? 0)) || (b.i - a.i))
    .map((o) => o.e);

  const sel = new Set(selectedIds);
  const click = (id: string, shift: boolean) => {
    if (shift) onSelect(sel.has(id) ? selectedIds.filter((i) => i !== id) : [...selectedIds, id]);
    else onSelect([id]);
  };

  const ico = "rounded border border-border px-1 text-[10px] leading-4 text-fg hover:border-accent hover:text-accent disabled:opacity-30";

  // A row for one element + (recursively) its frame children, indented. Frame
  // children hold all generated text, so surfacing them makes that content
  // selectable from the layer tree (their canvas selection is by id too). z
  // reorder is a top-level concern (children are ordered by the frame's
  // auto-layout), so the ↑/↓ controls show only at depth 0.
  const renderRow = (el: SlideElement, depth: number): React.ReactNode => {
    const selected = sel.has(el.id);
    const kids = el.kind === "frame" ? el.children ?? [] : [];
    return (
      <div key={el.id}>
        <div
          onClick={(e) => click(el.id, e.shiftKey)}
          className={`group flex cursor-pointer items-center gap-2 rounded py-1 pr-2 text-xs ${selected ? "bg-accent/15 text-accent" : "text-fg hover:bg-accent/5"}`}
          style={{ paddingLeft: 8 + depth * 14 }}
        >
          <span className="w-4 shrink-0 text-center text-tertiary">{ICONS[el.kind]}</span>
          <span className="min-w-0 flex-1 truncate">{labelFor(el)}</span>
          {depth === 0 && (
            <span className="flex shrink-0 gap-0.5 opacity-0 group-hover:opacity-100">
              <button
                className={ico}
                title="bring forward"
                onClick={(e) => { e.stopPropagation(); onChange(reorderZ(deck, slideId, el.id, "up")); }}
              >
                ↑
              </button>
              <button
                className={ico}
                title="send backward"
                onClick={(e) => { e.stopPropagation(); onChange(reorderZ(deck, slideId, el.id, "down")); }}
              >
                ↓
              </button>
            </span>
          )}
        </div>
        {kids.map((c) => renderRow(c, depth + 1))}
      </div>
    );
  };

  return (
    <div className="border-b border-border">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2 text-xs uppercase tracking-wide text-tertiary hover:text-accent"
      >
        <span>layers</span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="max-h-48 overflow-y-auto px-2 pb-2">
          {ordered.length === 0 ? (
            <div className="px-1 py-1 text-[11px] text-tertiary">empty slide</div>
          ) : (
            ordered.map((el) => renderRow(el, 0))
          )}
        </div>
      )}
    </div>
  );
}
