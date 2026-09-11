/**
 * okuro-slides — pure, immutable deck mutators used by the editor.
 *
 * Element identity is the load-bearing concept: an element keeps its `id` when
 * a slide is duplicated, so "duplicate slide → move things" is how a user
 * authors a smart-animate transition (same id on two slides ⇒ it morphs).
 */

import type { Deck, FrameLayout, Slide, SlideElement } from "./scene";
import { deckSize } from "./scene";
import { layoutChildren } from "./flatten";

let _seq = 0;
export function newId(prefix = "el"): string {
  _seq += 1;
  const r = Math.random().toString(36).slice(2, 7);
  return `${prefix}_${r}${_seq.toString(36)}`;
}

/** Apply `patch` to the element with id `elId` anywhere in the tree — a
 *  top-level element OR a (possibly deeply nested) frame child. Returns the same
 *  reference when nothing under this node matched, so unaffected subtrees keep
 *  their identity (no needless React re-renders / morph churn). */
function patchTree(el: SlideElement, elId: string, patch: Partial<SlideElement>): SlideElement {
  if (el.id === elId) return { ...el, ...patch };
  if (el.children && el.children.length) {
    let changed = false;
    const children = el.children.map((c) => {
      const next = patchTree(c, elId, patch);
      if (next !== c) changed = true;
      return next;
    });
    if (changed) return { ...el, children };
  }
  return el;
}

export function updateElement(
  deck: Deck,
  slideId: string,
  elId: string,
  patch: Partial<SlideElement>,
): Deck {
  return {
    ...deck,
    slides: deck.slides.map((s) =>
      s.id !== slideId
        ? s
        : {
            ...s,
            elements: s.elements.map((e) => patchTree(e, elId, patch)),
          },
    ),
  };
}

export function addElement(deck: Deck, slideId: string, el: SlideElement): Deck {
  return {
    ...deck,
    slides: deck.slides.map((s) =>
      s.id !== slideId ? s : { ...s, elements: [...s.elements, el] },
    ),
  };
}

export function removeElement(deck: Deck, slideId: string, elId: string): Deck {
  return {
    ...deck,
    slides: deck.slides.map((s) =>
      s.id !== slideId ? s : { ...s, elements: s.elements.filter((e) => e.id !== elId) },
    ),
  };
}

export function removeElements(deck: Deck, slideId: string, ids: string[]): Deck {
  const set = new Set(ids);
  return {
    ...deck,
    slides: deck.slides.map((s) =>
      s.id !== slideId ? s : { ...s, elements: s.elements.filter((e) => !set.has(e.id)) },
    ),
  };
}

/** Deep-clone an element with fresh ids (recursing into frame children). */
function reid(el: SlideElement): SlideElement {
  return { ...el, id: newId(el.kind), children: el.children?.map(reid) };
}

/** Clone elements with new ids, offset so the paste is visible. */
export function cloneElements(els: SlideElement[], dx = 24, dy = 24): SlideElement[] {
  return els.map((e) => ({ ...reid(e), x: e.x + dx, y: e.y + dy }));
}

export function addElements(deck: Deck, slideId: string, els: SlideElement[]): Deck {
  return {
    ...deck,
    slides: deck.slides.map((s) => (s.id !== slideId ? s : { ...s, elements: [...s.elements, ...els] })),
  };
}

function uniqueSlideId(deck: Deck, base: string): string {
  let id = base;
  let n = 1;
  const ids = new Set(deck.slides.map((s) => s.id));
  while (ids.has(id)) {
    n += 1;
    id = `${base}-${n}`;
  }
  return id;
}

/**
 * Insert a copy of slide `index` right after it. Elements keep their ids
 * (so they're shared → they morph between the two slides). Only the slide id
 * is freshened. This is the primary way to author a transition.
 */
export function duplicateSlide(deck: Deck, index: number): { deck: Deck; newIndex: number } {
  const src = deck.slides[index];
  if (!src) return { deck, newIndex: index };
  // Deep-clone each element (recursing frame children) but KEEP ids — shared ids
  // across the two slides are what makes the elements morph. A shallow `{...e}`
  // would alias the same `children` array in both slides, so a later edit to a
  // child on one slide would leak into the other.
  const cloneEl = (e: SlideElement): SlideElement => ({ ...e, children: e.children?.map(cloneEl) });
  const copy: Slide = {
    id: uniqueSlideId(deck, `${src.id}-copy`),
    elements: src.elements.map(cloneEl),
  };
  const slides = [...deck.slides];
  slides.splice(index + 1, 0, copy);
  return { deck: { ...deck, slides }, newIndex: index + 1 };
}

export function addBlankSlide(deck: Deck): { deck: Deck; newIndex: number } {
  const slide: Slide = { id: uniqueSlideId(deck, "slide"), elements: [] };
  return { deck: { ...deck, slides: [...deck.slides, slide] }, newIndex: deck.slides.length };
}

export function removeSlide(deck: Deck, index: number): { deck: Deck; newIndex: number } {
  if (deck.slides.length <= 1) return { deck, newIndex: 0 };
  const slides = deck.slides.filter((_, i) => i !== index);
  return { deck: { ...deck, slides }, newIndex: Math.max(0, index - 1) };
}

export function moveSlide(deck: Deck, from: number, to: number): Deck {
  if (to < 0 || to >= deck.slides.length || from === to) return deck;
  const slides = [...deck.slides];
  const [s] = slides.splice(from, 1);
  if (s) slides.splice(to, 0, s);
  return { ...deck, slides };
}

/** A sensible new element near the canvas centre. */
export function makeElement(kind: SlideElement["kind"], deck: Deck): SlideElement {
  const { w: canvasW, h: canvasH } = deckSize(deck);
  const cx = canvasW / 2;
  const cy = canvasH / 2;
  if (kind === "text") {
    return {
      id: newId("txt"),
      kind: "text",
      x: cx - 300,
      y: cy - 40,
      w: 600,
      h: 80,
      text: "New text",
      fontSize: 40,
      fontWeight: 600,
      color: "#eafff0",
      align: "left",
      z: 1,
    };
  }
  if (kind === "list") {
    return {
      id: newId("list"),
      kind: "list",
      x: cx - 320,
      y: cy - 90,
      w: 640,
      h: 180,
      items: ["First point", "Second point", "Third point"],
      ordered: false,
      fontSize: 32,
      fontWeight: 400,
      color: "#d2ffdd",
      align: "left",
      z: 1,
    };
  }
  if (kind === "kpi") {
    return {
      id: newId("kpi"),
      kind: "kpi",
      x: cx - 220,
      y: cy - 80,
      w: 440,
      h: 160,
      value: "87%",
      label: "conversion rate",
      delta: "+12%",
      deltaDir: "up",
      fontSize: 96,
      fontWeight: 700,
      color: "#eafff0",
      align: "left",
      z: 1,
    };
  }
  if (kind === "quote") {
    return {
      id: newId("quote"),
      kind: "quote",
      x: cx - 380,
      y: cy - 80,
      w: 760,
      h: 160,
      text: "The best way to predict the future is to invent it.",
      attribution: "Alan Kay",
      fontSize: 44,
      fontWeight: 500,
      color: "#eafff0",
      align: "left",
      z: 1,
    };
  }
  if (kind === "divider") {
    return {
      id: newId("rule"),
      kind: "divider",
      x: cx - 300,
      y: cy - 12,
      w: 600,
      h: 24,
      orientation: "h",
      thickness: 2,
      accent: true,
      z: 1,
    };
  }
  if (kind === "chart") {
    return {
      id: newId("chart"),
      kind: "chart",
      x: cx - 340,
      y: cy - 200,
      w: 680,
      h: 400,
      chartType: "bar",
      categories: ["Q1", "Q2", "Q3", "Q4"],
      series: [{ name: "Revenue", values: [12, 19, 15, 27] }],
      showAxes: true,
      color: "#8ff0a4",
      z: 1,
    };
  }
  if (kind === "table") {
    return {
      id: newId("table"),
      kind: "table",
      x: cx - 340,
      y: cy - 110,
      w: 680,
      h: 220,
      columns: ["Metric", "Q3", "Q4"],
      rows: [
        ["Revenue", "€1.2M", "€1.8M"],
        ["Users", "8,400", "12,900"],
        ["Churn", "3.1%", "2.4%"],
      ],
      header: true,
      fontSize: 22,
      color: "#eafff0",
      z: 1,
    };
  }
  return {
    id: newId("box"),
    kind: "box",
    x: cx - 200,
    y: cy - 150,
    w: 400,
    h: 300,
    bg: "#10231a",
    radius: 16,
    z: 1,
  };
}

// ── Themes + slide-layout templates ─────────────────────────────────────────

export interface Theme { name: string; background: string; font: string; accent: string; text: string }

export const THEMES: Theme[] = [
  { name: "Okuro Dark", background: "#0b0f0c", font: "Inter, system-ui, sans-serif", accent: "#8ff0a4", text: "#eafff0" },
  { name: "Midnight", background: "#0a0e1a", font: "Inter, system-ui, sans-serif", accent: "#7aa2ff", text: "#eaf0ff" },
  { name: "Paper", background: "#faf7f0", font: "Georgia, 'Times New Roman', serif", accent: "#b5651d", text: "#1a1a1a" },
  { name: "Mono", background: "#101010", font: "'JetBrains Mono', ui-monospace, monospace", accent: "#e0e0e0", text: "#f5f5f5" },
];

export function applyTheme(deck: Deck, theme: Theme): Deck {
  return { ...deck, background: theme.background, font: theme.font };
}

/** Apply an okuro brand's design system to the deck (bg + font) + record the
 *  brand so its asset library scopes the asset panel. */
export function applyBrandTheme(deck: Deck, brandId: string, theme: { background: string; font?: string }): Deck {
  return { ...deck, brandId, background: theme.background, font: theme.font };
}

export type LayoutKind = "title" | "title-bullets" | "two-col" | "section";

const LAYOUT_LABELS: Record<LayoutKind, string> = {
  title: "Title",
  "title-bullets": "Title + bullets",
  "two-col": "Two columns",
  section: "Section header",
};

export const LAYOUTS: { kind: LayoutKind; label: string }[] = (Object.keys(LAYOUT_LABELS) as LayoutKind[]).map((k) => ({ kind: k, label: LAYOUT_LABELS[k] }));

export function makeLayoutSlide(deck: Deck, kind: LayoutKind): Slide {
  const { w } = deckSize(deck);
  const accent = "#8ff0a4";
  // A text child of an auto-layout frame: x/y are 0 (the frame's flow stacks
  // them); width is the content width so wrapping is measured against it.
  const t = (id: string, ww: number, text: string, fontSize: number, extra: Partial<SlideElement> = {}): SlideElement => ({ id: newId(id), kind: "text", x: 0, y: 0, w: ww, h: Math.round(fontSize * 1.3), text, fontSize, color: "#eafff0", z: 1, ...extra });
  // A hugging column frame: stacks its text children with gap + padding and
  // reflows when any line wraps — no hardcoded per-line y, so siblings never
  // overlap. The frame's own x/y place the group on the canvas.
  const colFrame = (id: string, x: number, y: number, ww: number, children: SlideElement[], gap = 16, align: FrameLayout["align"] = "start"): SlideElement => ({
    id: newId(id), kind: "frame", x, y, w: ww, h: 100, z: 1, bg: "transparent", radius: 0,
    layout: { flow: "col", gap, padX: 0, padY: 0, align },
    children,
  });
  const id = uniqueSlideIdForDeck(deck, "slide");
  switch (kind) {
    case "title":
      return { id, elements: [
        colFrame("title-group", 160, 280, w - 320, [
          t("title", w - 320, "Title", 76, { fontWeight: 700, align: "center" }),
          t("sub", w - 320, "Subtitle", 30, { color: accent, align: "center" }),
        ], 24, "center"),
      ] };
    case "title-bullets":
      return { id, elements: [
        colFrame("body-group", 140, 90, w - 280, [
          t("title", w - 280, "Title", 48, { fontWeight: 700 }),
          t("body", w - 280, "• point one\n• point two\n• point three", 32, { color: "#d2ffdd" }),
        ], 32),
      ] };
    case "two-col": {
      const colW = (w - 320) / 2;
      return { id, elements: [
        colFrame("title-group", 140, 90, w - 280, [
          t("title", w - 280, "Title", 44, { fontWeight: 700 }),
        ], 24),
        colFrame("left", 140, 210, colW, [
          t("left-body", colW, "Left column\n\n• a\n• b", 28, { color: "#d2ffdd" }),
        ], 16),
        colFrame("right", 160 + colW, 210, colW, [
          t("right-body", colW, "Right column\n\n• c\n• d", 28, { color: "#d2ffdd" }),
        ], 16),
      ] };
    }
    case "section":
      return { id, elements: [
        colFrame("section-group", 140, 330, w - 280, [
          { id: newId("bar"), kind: "box", x: 0, y: 0, w: 80, h: 8, bg: accent, radius: 4, z: 1 },
          t("title", w - 280, "Section", 64, { fontWeight: 700 }),
        ], 20),
      ] };
  }
}

function uniqueSlideIdForDeck(deck: Deck, base: string): string {
  const ids = new Set(deck.slides.map((s) => s.id));
  let id = base;
  let n = 1;
  while (ids.has(id)) { n += 1; id = `${base}-${n}`; }
  return id;
}

export function insertLayoutSlide(deck: Deck, index: number, kind: LayoutKind): { deck: Deck; newIndex: number } {
  const slide = makeLayoutSlide(deck, kind);
  const slides = [...deck.slides];
  slides.splice(index + 1, 0, slide);
  return { deck: { ...deck, slides }, newIndex: index + 1 };
}

// ── Multi-element layout ops (operate on a set of ids within one slide) ──────

function mapSlide(deck: Deck, slideId: string, fn: (els: SlideElement[]) => SlideElement[]): Deck {
  return {
    ...deck,
    slides: deck.slides.map((s) => (s.id === slideId ? { ...s, elements: fn(s.elements) } : s)),
  };
}

export function moveElements(deck: Deck, slideId: string, ids: string[], dx: number, dy: number): Deck {
  const set = new Set(ids);
  return mapSlide(deck, slideId, (els) =>
    els.map((e) => (set.has(e.id) ? { ...e, x: Math.round(e.x + dx), y: Math.round(e.y + dy) } : e)),
  );
}

export function bringToFront(deck: Deck, slideId: string, ids: string[]): Deck {
  const set = new Set(ids);
  return mapSlide(deck, slideId, (els) => {
    const top = Math.max(0, ...els.map((e) => e.z ?? 0));
    return els.map((e) => (set.has(e.id) ? { ...e, z: top + 1 } : e));
  });
}

export function sendToBack(deck: Deck, slideId: string, ids: string[]): Deck {
  const set = new Set(ids);
  return mapSlide(deck, slideId, (els) => {
    const bottom = Math.min(0, ...els.map((e) => e.z ?? 0));
    return els.map((e) => (set.has(e.id) ? { ...e, z: bottom - 1 } : e));
  });
}

/** Move one element one step up ("up" = toward front) or down in the z stack.
 *  Normalizes every element's z to its rank so the order is stable + dense —
 *  used by the layer panel's ↑/↓ buttons. Ties broken by array order. */
export function reorderZ(deck: Deck, slideId: string, elId: string, dir: "up" | "down"): Deck {
  return mapSlide(deck, slideId, (els) => {
    const order = els
      .map((e, i) => ({ id: e.id, z: e.z ?? 0, i }))
      .sort((a, b) => (a.z - b.z) || (a.i - b.i))
      .map((o) => o.id);
    const at = order.indexOf(elId);
    if (at < 0) return els;
    const to = dir === "up" ? at + 1 : at - 1;
    if (to < 0 || to >= order.length) return els;
    [order[at], order[to]] = [order[to]!, order[at]!];
    const rank = new Map(order.map((id, idx) => [id, idx]));
    return els.map((e) => ({ ...e, z: rank.get(e.id) ?? e.z ?? 0 }));
  });
}

export type AlignMode = "left" | "centerX" | "right" | "top" | "middle" | "bottom";

function bbox(els: SlideElement[]) {
  const minX = Math.min(...els.map((e) => e.x));
  const minY = Math.min(...els.map((e) => e.y));
  const maxX = Math.max(...els.map((e) => e.x + e.w));
  const maxY = Math.max(...els.map((e) => e.y + e.h));
  return { minX, minY, maxX, maxY, cx: (minX + maxX) / 2, cy: (minY + maxY) / 2 };
}

export function alignElements(deck: Deck, slideId: string, ids: string[], mode: AlignMode): Deck {
  const set = new Set(ids);
  return mapSlide(deck, slideId, (els) => {
    const sel = els.filter((e) => set.has(e.id));
    if (sel.length < 2) return els;
    const b = bbox(sel);
    return els.map((e) => {
      if (!set.has(e.id)) return e;
      switch (mode) {
        case "left": return { ...e, x: Math.round(b.minX) };
        case "right": return { ...e, x: Math.round(b.maxX - e.w) };
        case "centerX": return { ...e, x: Math.round(b.cx - e.w / 2) };
        case "top": return { ...e, y: Math.round(b.minY) };
        case "bottom": return { ...e, y: Math.round(b.maxY - e.h) };
        case "middle": return { ...e, y: Math.round(b.cy - e.h / 2) };
      }
    });
  });
}

export function distributeElements(deck: Deck, slideId: string, ids: string[], axis: "h" | "v"): Deck {
  const set = new Set(ids);
  return mapSlide(deck, slideId, (els) => {
    const sel = els.filter((e) => set.has(e.id));
    if (sel.length < 3) return els;
    const pos = (e: SlideElement) => (axis === "h" ? e.x : e.y);
    const size = (e: SlideElement) => (axis === "h" ? e.w : e.h);
    const sorted = [...sel].sort((a, b) => pos(a) - pos(b));
    const first = sorted[0]!;
    const lastEl = sorted[sorted.length - 1]!;
    const span = pos(lastEl) + size(lastEl) - pos(first);
    const totalSize = sorted.reduce((acc, e) => acc + size(e), 0);
    const gap = (span - totalSize) / (sorted.length - 1);
    let cursor = pos(first);
    const newPos = new Map<string, number>();
    for (const e of sorted) {
      newPos.set(e.id, Math.round(cursor));
      cursor += size(e) + gap;
    }
    return els.map((e) =>
      newPos.has(e.id) ? { ...e, ...(axis === "h" ? { x: newPos.get(e.id)! } : { y: newPos.get(e.id)! }) } : e,
    );
  });
}

// ── Grouping / frames / auto-layout ─────────────────────────────────────────

const DEFAULT_LAYOUT: FrameLayout = { flow: "none", gap: 16, padX: 16, padY: 16, align: "start" };

/** Wrap the selected top-level elements in a frame. Children become relative to
 *  the frame origin (so the frame moves/resizes as one unit). */
export function groupElements(deck: Deck, slideId: string, ids: string[]): { deck: Deck; frameId: string } {
  const set = new Set(ids);
  const slide = deck.slides.find((s) => s.id === slideId);
  if (!slide) return { deck, frameId: "" };
  const sel = slide.elements.filter((e) => set.has(e.id));
  if (sel.length < 2) return { deck, frameId: "" };
  const minX = Math.min(...sel.map((e) => e.x));
  const minY = Math.min(...sel.map((e) => e.y));
  const maxX = Math.max(...sel.map((e) => e.x + e.w));
  const maxY = Math.max(...sel.map((e) => e.y + e.h));
  const z = Math.max(0, ...sel.map((e) => e.z ?? 0));
  const frameId = newId("grp");
  const frame: SlideElement = {
    id: frameId,
    kind: "frame",
    x: Math.round(minX),
    y: Math.round(minY),
    w: Math.round(maxX - minX),
    h: Math.round(maxY - minY),
    z,
    bg: "transparent",
    radius: 0,
    layout: { ...DEFAULT_LAYOUT },
    children: sel.map((e) => ({ ...e, x: Math.round(e.x - minX), y: Math.round(e.y - minY) })),
  };
  const rest = slide.elements.filter((e) => !set.has(e.id));
  return {
    deck: mapSlide(deck, slideId, () => [...rest, frame]),
    frameId,
  };
}

/** Flatten a frame back into absolute top-level elements. */
export function ungroupFrame(deck: Deck, slideId: string, frameId: string): { deck: Deck; ids: string[] } {
  const slide = deck.slides.find((s) => s.id === slideId);
  const frame = slide?.elements.find((e) => e.id === frameId);
  if (!slide || !frame || frame.kind !== "frame") return { deck, ids: [] };
  const { children } = layoutChildren(frame, deck.font);
  const absolute = children.map((c) => ({ ...c, x: frame.x + c.x, y: frame.y + c.y }));
  const ids = absolute.map((c) => c.id);
  return {
    deck: mapSlide(deck, slideId, (els) => els.flatMap((e) => (e.id === frameId ? absolute : [e]))),
    ids,
  };
}

/** Update a frame's auto-layout; re-hug its size for row/col flows. */
export function setFrameLayout(deck: Deck, slideId: string, frameId: string, patch: Partial<FrameLayout>): Deck {
  return mapSlide(deck, slideId, (els) =>
    els.map((e) => {
      if (e.id !== frameId || e.kind !== "frame") return e;
      const layout = { ...(e.layout ?? DEFAULT_LAYOUT), ...patch };
      const next = { ...e, layout };
      if (layout.flow !== "none") {
        const { w, h } = layoutChildren(next, deck.font);
        next.w = w;
        next.h = h;
      }
      return next;
    }),
  );
}

/** A media/flow element centred on the canvas. */
function centred(deck: Deck, w: number, h: number): { x: number; y: number; w: number; h: number } {
  const { w: canvasW, h: canvasH } = deckSize(deck);
  return { x: Math.round(canvasW / 2 - w / 2), y: Math.round(canvasH / 2 - h / 2), w, h };
}

export function makeImageElement(deck: Deck, src: string): SlideElement {
  return { id: newId("img"), kind: "image", ...centred(deck, 480, 320), src, radius: 8, z: 1 };
}

export function makeVideoElement(deck: Deck, src: string): SlideElement {
  return { id: newId("vid"), kind: "video", ...centred(deck, 640, 360), src, radius: 8, z: 1 };
}

export function makeFlowElement(deck: Deck, flowId: string): SlideElement {
  return { id: newId("flow"), kind: "flow", ...centred(deck, 720, 460), flowId, radius: 8, z: 1, bg: "#0e1512" };
}
