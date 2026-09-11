/**
 * okuro-slides — scene IR (Phase 1, engine proof).
 *
 * A Deck is an ordered list of Slides; each Slide is a STATE. The transition
 * between two consecutive slides is a Figma-style smart-animate: elements that
 * share an `id` across slides MORPH (position/size/style tween), and elements
 * absent in the target slide EXIT in the direction the deck is arranged
 * (horizontal → left/right, vertical → up/down) rather than a blind fade.
 *
 * This same IR is intended to drive okuro-video later (render the scene to
 * MP4), so it is deliberately declarative and renderer-agnostic.
 */

export type Arrangement = "horizontal" | "vertical";

// Either a named easing or a cubic-bezier [x1,y1,x2,y2] (the "curve").
export type Easing =
  | "linear"
  | "easeIn"
  | "easeOut"
  | "easeInOut"
  | [number, number, number, number];

export type ElementKind =
  | "text" | "box" | "image" | "frame" | "video" | "flow"
  | "list" | "kpi" | "quote" | "divider"
  | "chart" | "table";

/** A chart's plot family (Phase D). */
export type ChartType = "bar" | "line" | "area";

/** One named data series for a chart element. */
export interface ChartSeries {
  name: string;
  values: number[];
}

/** Direction of a KPI's delta — colours + arrow the change (up=good/green,
 *  down=bad/red, flat=neutral). */
export type DeltaDir = "up" | "down" | "flat";

/** Drop-shadow presets for a box (element-agnostic). */
export type ShadowPreset = "none" | "sm" | "md" | "lg";
export interface BoxBorder { width: number; color: string }
export interface BoxGradient { from: string; to: string; angle?: number }

/** Shared default line-height for text. One source of truth — every renderer
 *  reads `el.lineHeight ?? DEFAULT_LINE_HEIGHT` so authored decks without the
 *  field render exactly as before. */
export const DEFAULT_LINE_HEIGHT = 1.25;

/** Baseline canvas every element coord is authored against — the ONE frontend
 *  source of truth, mirroring the backend default (`slides/storage.py`
 *  DEFAULT_CANVAS). Every renderer reads `deckSize(deck)` so a legacy/malformed
 *  deck that reaches the UI without a valid `size` still renders at 1280×720
 *  instead of crashing the whole Slides tab on `deck.size.w`. */
export const DEFAULT_CANVAS = { w: 1280, h: 720 } as const;

/** A deck's baseline canvas, guaranteed valid — falls back to DEFAULT_CANVAS
 *  when `size` is missing or malformed (non-positive / non-numeric w|h). */
export function deckSize(deck: Pick<Deck, "size">): { w: number; h: number } {
  const s = deck.size;
  if (
    s &&
    typeof s.w === "number" && typeof s.h === "number" &&
    s.w > 0 && s.h > 0
  ) {
    return { w: s.w, h: s.h };
  }
  return { w: DEFAULT_CANVAS.w, h: DEFAULT_CANVAS.h };
}

/** Auto-layout for a frame. flow "none" = free (children keep relative x/y);
 *  "row"/"col" = stack children with gap + padding, frame hugs the content. */
export interface FrameLayout {
  flow: "none" | "row" | "col";
  gap: number;
  padX: number;
  padY: number;
  align: "start" | "center" | "end";
}

export interface SlideElement {
  /** STABLE identity across slides — same id in two slides = one element that
   *  morphs between them. New id = enters; missing id = exits. */
  id: string;
  kind: ElementKind;
  /** Geometry in baseline-canvas px (see Deck.size). */
  x: number;
  y: number;
  w: number;
  h: number;
  text?: string;
  src?: string;
  /** Accessible alt text for image elements (a11y / export). Falls back to a
   *  kind-based label when absent. Shared with okuro-video — optional. */
  alt?: string;
  /** flow element: id of the okuro·flow doc to embed live. */
  flowId?: string;
  bg?: string;
  color?: string;
  fontSize?: number;
  fontWeight?: number;
  font?: string;
  /** Multiplier (default DEFAULT_LINE_HEIGHT). */
  lineHeight?: number;
  /** Tracking in px (default 0). */
  letterSpacing?: number;
  /** 0–1, applied to the whole element (default 1). */
  opacity?: number;
  radius?: number;
  pad?: number;
  align?: "left" | "center" | "right";
  z?: number;
  // ── component primitives (Phase C) — all optional ⇒ backward-compatible ──────
  /** list element: the bullet/numbered rows (one string per row; each wraps and
   *  hangs under its marker). */
  items?: string[];
  /** list element: numbered ("1." "2." …) when true, else a bullet marker. */
  ordered?: boolean;
  /** list/kpi element: spacing in px between rows/parts (default derives from
   *  fontSize). */
  gap?: number;
  /** kpi element: the big figure (e.g. "€2.4M", "87%"). `fontSize` sizes it. */
  value?: string;
  /** kpi element: caption under the figure. */
  label?: string;
  /** kpi element: the change chip (e.g. "+12%"), coloured by `deltaDir`. */
  delta?: string;
  deltaDir?: DeltaDir;
  /** quote element: uses `text` for the quotation; this names the source. */
  attribution?: string;
  /** divider element: rule direction (default "h"). */
  orientation?: "h" | "v";
  /** divider element: rule weight in px (default 2). */
  thickness?: number;
  /** divider element: paint the rule in the brand accent (else a muted line or
   *  the element's `color`). */
  accent?: boolean;
  // ── data primitives (Phase D) — all optional ⇒ backward-compatible ──────────
  /** chart element: plot family (default "bar"). */
  chartType?: ChartType;
  /** chart element: one or more named numeric series. */
  series?: ChartSeries[];
  /** chart element: x-axis category labels (one per value index). */
  categories?: string[];
  /** chart element: show the series legend (default: on when >1 series). */
  showLegend?: boolean;
  /** chart element: draw axes + gridlines + tick labels (default true). */
  showAxes?: boolean;
  /** table element: header row labels (also the column count). */
  columns?: string[];
  /** table element: body rows (each an array of cell strings). */
  rows?: string[][];
  /** table element: render `columns` as a styled header row (default true). */
  header?: boolean;
  // ── box enrichment (any kind, but authored on box) — all optional ───────────
  /** A stroke around the element box. */
  border?: BoxBorder;
  /** A drop-shadow preset ("none" ⇒ no shadow). */
  shadow?: ShadowPreset;
  /** A 2-stop linear gradient fill (paints over `bg`). */
  gradient?: BoxGradient;
  /** Progressive-disclosure depth (1–4; absent ⇒ 1 = always visible). An element
   *  is shown when `(depth ?? 1) <= the view's maxDepth`; deeper elements morph in
   *  as the present-mode depth stepper is raised. Backward-compatible: a deck with
   *  no depth renders identically at any maxDepth ≥ 1. */
  depth?: number;
  // frame-only: nested children (relative coords) + optional auto-layout.
  children?: SlideElement[];
  layout?: FrameLayout;
  /** "Can't-lie" gate verdict for this element's claim (see slides/entailment.py):
   *  grounded — the grounding source supports it (or it asserts no fact);
   *  inferred — plausible/consistent but not explicitly stated (soft-flagged);
   *  fabricated — source does NOT support it (should have been dropped);
   *  unverified — no grounding source was available to check against. */
  provenance?: ElementProvenance;
}

export type ProvenanceStatus =
  | "grounded"
  | "inferred"
  | "fabricated"
  | "unverified";

export interface ElementProvenance {
  status: ProvenanceStatus;
  /** Short reason, populated for inferred / fabricated. */
  why?: string;
}

export interface Slide {
  id: string;
  elements: SlideElement[];
  /** Speaker / presenter notes (shown in present mode, not on the slide). */
  notes?: string;
}

export interface Transition {
  /** seconds */
  duration: number;
  easing: Easing;
  /**
   * How consecutive slides transition (default "morph"):
   * - "morph": Figma-style smart-animate — shared ids tween in place, others
   *   slide off + fade along the arrangement axis.
   * - "push": the whole slide translates off-screen along the arrangement axis
   *   as one opaque block while the next slides in — "old screen leaves, next
   *   appears". No per-element morph, no cross-fade.
   */
  mode?: "morph" | "push";
}

export interface Deck {
  id: string;
  title: string;
  /** Owns the off-state exit/enter vector. */
  arrangement: Arrangement;
  /** Baseline canvas the element coords are authored against. */
  size: { w: number; h: number };
  transition: Transition;
  background?: string;
  /** Default font family for the deck (brand/theme); per-element `font` overrides. */
  font?: string;
  /** okuro brand whose design system styles this deck + scopes its asset library. */
  brandId?: string;
  /** okuro person this deck is tailored to (set by generate / re-tailor). */
  recipient?: string;
  /** If this deck is an audience variant, the id of the source deck it derives from. */
  variantOf?: string;
  /** Axes applied when this variant was re-tailored (for badges + tab labels). */
  variantMeta?: { person_id?: string; language?: string; density?: string; jargon?: string };
  /** Provenance summary from the "can't-lie" entailment gate: the okuro brain
   *  sources that grounded the deck + a tally by claim status (or a
   *  {status:"unverified"|"skipped"} marker when the gate could not run). */
  provenance?: DeckProvenance;
  /** Default present-mode reveal depth for this deck — e.g. a re-tailored terse
   *  variant maps to a lower maxDepth (see slides/retailor.py). Absent ⇒ present
   *  mode starts at depth 1 (glance first). A hint for present mode ONLY; the
   *  editor and thumbnails always render at full depth. */
  maxDepth?: number;
  slides: Slide[];
}

export interface DeckProvenance {
  sources: string[];
  gate: Partial<Record<ProvenanceStatus, number>> | { status: string };
}

/** Rendered state of one element at a given slide index. */
export interface ResolvedElement {
  el: SlideElement; // content + geometry to render (from its nearest slide)
  present: boolean; // is it on the current slide
  dx: number; // off-screen translate when absent
  dy: number;
  opacity: number;
}

/** Meaningful alt text for an image element: explicit `alt`, else its
 *  text/caption, else a kind-based fallback. One source of truth for a11y. */
export function imageAlt(el: SlideElement): string {
  return el.alt?.trim() || el.text?.trim() || "slide image";
}

// ── caption legibility over images ───────────────────────────────────────────
//
// The backend contrast pass (`slides/brand_resolver.py`) can only check text
// against a solid box/deck background — it has no idea an IMAGE sits behind the
// text, so a white caption over a light full-bleed photo passes the gate yet
// ships unreadable (#2). We can't read the image's pixels, so we can't know its
// luminance; the safe, image-agnostic fix is a semi-opaque scrim/plate behind
// any caption that overlaps an image beneath it — dark plate under light text,
// light plate under dark text — which guarantees legibility over ANY photo.

interface Rect { x: number; y: number; w: number; h: number }

function rectsOverlap(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

/** Relative luminance (0..1) of a #rgb / #rrggbb color, or null if unparseable. */
function colorLuminance(color?: string): number | null {
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

/** The scrim color to paint behind text of `textColor`: dark plate under light
 *  text, light plate under dark text. Unknown/absent color is treated as light
 *  (the deck default foreground is light) → dark plate. */
export function scrimColor(textColor?: string): string {
  const lum = colorLuminance(textColor);
  return lum !== null && lum < 0.5 ? "rgba(255,255,255,0.62)" : "rgba(0,0,0,0.5)";
}

/** A scrim color if `rect` (a caption at effective z `z`, painted in `color`)
 *  overlaps any image at or below it, else null. */
export function captionScrimForRect(
  rect: Rect,
  color: string | undefined,
  z: number,
  els: SlideElement[],
): string | null {
  const overlapsImage = els.some(
    (s) => s.kind === "image" && s.src && (s.z ?? 0) <= z && rectsOverlap(rect, s),
  );
  return overlapsImage ? scrimColor(color) : null;
}

/** A scrim color for text element `el` if it needs one (overlaps an image
 *  beneath it and has no opaque plate of its own), else null. `siblings` are the
 *  other elements on the same slide, in the SAME coordinate space as `el`. */
export function captionScrim(el: SlideElement, siblings: SlideElement[]): string | null {
  if (el.kind !== "text" || !el.text?.trim()) return null;
  const bg = el.bg?.trim().toLowerCase();
  if (bg && bg !== "transparent") return null; // author already gave it a plate
  return captionScrimForRect(el, el.color, el.z ?? 0, siblings.filter((s) => s.id !== el.id));
}

// ── chart surface resolution ─────────────────────────────────────────────────
//
// A chart with no `bg` of its own must pick light-vs-dark ink from the surface
// it actually sits on. The deck background is the coarse default, but a chart
// dropped onto a light PANEL inside a dark deck should read as light-surface.
// This resolves the effective surface = the topmost solid-filled element painted
// behind the chart that overlaps it, else the deck background. Mirrors the
// backend `_effective_bg` (brand_resolver). Images/gradients-without-a-stop are
// skipped (unknown luminance) → the chart falls back to the deck background.

/** The effective solid surface colour a chart sits on (for ink light/dark), or
 *  `deckBackground` when nothing solid is painted beneath it. `siblings` must be
 *  in the SAME coordinate space as `el`. */
export function chartSurface(
  el: SlideElement,
  siblings: SlideElement[],
  deckBackground?: string,
): string | undefined {
  const z = el.z ?? 0;
  let best: { z: number; bg: string } | null = null;
  for (const s of siblings) {
    if (s.id === el.id) continue;
    if ((s.z ?? 0) > z) continue; // painted above the chart — not its surface
    if (!rectsOverlap(el, s)) continue;
    const bg = s.bg ?? s.gradient?.from; // solid fill, or a gradient's first stop
    if (colorLuminance(bg) === null) continue; // transparent / image / unparseable
    const sz = s.z ?? 0;
    if (!best || sz >= best.z) best = { z: sz, bg: (bg as string).trim() }; // later/higher wins
  }
  return best ? best.bg : deckBackground;
}

// ── kind dispatch (Phase C component primitives) ─────────────────────────────
//
// One source of truth for how every renderer treats a kind, so measure, flatten,
// the editor's resize axis, the fit re-measure, and the export path never drift.

/** Kinds whose height HUGS their measured content (text + text-bearing
 *  primitives). Every other kind uses its declared `h`. */
const HUG_KINDS = new Set<ElementKind>(["text", "list", "kpi", "quote", "table"]);
export function hugs(kind: ElementKind): boolean {
  return HUG_KINDS.has(kind);
}

/** Component-primitive kinds rendered by a dedicated body (element-body.tsx) —
 *  not plain rich text, not a media embed. One source of truth for every
 *  renderer's dispatch (deck, editor, export). */
const COMPOSITE_KINDS = new Set<ElementKind>(["list", "kpi", "quote", "divider", "chart", "table"]);
export function isComposite(kind: ElementKind): boolean {
  return COMPOSITE_KINDS.has(kind);
}

// ── box decoration (Phase C) — border / shadow / gradient ────────────────────
// One source of truth so the deck renderer, the editor canvas and the export
// path all paint identical borders/shadows/gradients.

const SHADOWS: Record<Exclude<ShadowPreset, "none">, string> = {
  sm: "0 1px 3px rgba(0,0,0,0.30)",
  md: "0 6px 16px rgba(0,0,0,0.35)",
  lg: "0 16px 40px rgba(0,0,0,0.50)",
};

/** The CSS a box's border/shadow/gradient add, as a React style subset. Empty
 *  when the element declares none (so every element can spread it harmlessly). */
export function boxDecor(el: SlideElement): {
  border?: string;
  boxShadow?: string;
  backgroundImage?: string;
} {
  const out: { border?: string; boxShadow?: string; backgroundImage?: string } = {};
  if (el.border && el.border.width > 0) {
    out.border = `${el.border.width}px solid ${el.border.color}`;
  }
  if (el.shadow && el.shadow !== "none") out.boxShadow = SHADOWS[el.shadow];
  if (el.gradient) {
    out.backgroundImage = `linear-gradient(${el.gradient.angle ?? 180}deg, ${el.gradient.from}, ${el.gradient.to})`;
  }
  return out;
}

/** The same border/shadow/gradient as inline-CSS text for the export HTML path. */
export function boxDecorCssText(el: SlideElement): string {
  const d = boxDecor(el);
  let s = "";
  if (d.border) s += `border:${d.border};`;
  if (d.boxShadow) s += `box-shadow:${d.boxShadow};`;
  if (d.backgroundImage) s += `background-image:${d.backgroundImage};`;
  return s;
}

/** Every distinct element id across the whole deck, in first-seen order. */
export function allElementIds(deck: Deck): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const s of deck.slides) {
    for (const e of s.elements) {
      if (!seen.has(e.id)) {
        seen.add(e.id);
        out.push(e.id);
      }
    }
  }
  return out;
}

// ── progressive-disclosure depth ─────────────────────────────────────────────
//
// Each element carries an optional `depth` (1–4). A view renders at a `maxDepth`
// and shows only elements with `(depth ?? 1) <= maxDepth`. Raising the level in
// present mode reveals deeper elements — and because a hidden element is treated
// as absent (off-state, opacity 0) it MORPHS in through the existing engine, no
// second animation system. Absent depth ⇒ 1, so a deck with no depth tags is a
// single glance level and renders identically at any maxDepth ≥ 1.

/** True when `el` is visible at reveal depth `maxDepth` (absent depth ⇒ 1). One
 *  source of truth for the depth filter. */
export function visibleAtDepth(el: Pick<SlideElement, "depth">, maxDepth: number): boolean {
  return (el.depth ?? 1) <= maxDepth;
}

/** The deepest depth tag among `els`, recursing into frame children. Min 1 (a
 *  set with no depth tags is a single glance level). */
export function elementsMaxDepth(els: SlideElement[]): number {
  let m = 1;
  for (const e of els) {
    m = Math.max(m, e.depth ?? 1);
    if (e.children?.length) m = Math.max(m, elementsMaxDepth(e.children));
  }
  return m;
}

/** The deepest depth tag anywhere in the deck — the "show everything" maxDepth
 *  and the present-mode stepper ceiling. */
export function maxElementDepth(deck: Deck): number {
  let m = 1;
  for (const s of deck.slides) m = Math.max(m, elementsMaxDepth(s.elements));
  return m;
}

/**
 * Resolve how element `id` should render at `slideIndex`.
 *
 * - present on this slide → its own geometry, fully visible, no offset.
 * - present but hidden by `maxDepth` (its depth exceeds the current reveal level)
 *   → parked in the "waiting to enter" off-state (opacity 0, offset ahead) so
 *   raising the level morphs it in through the same tween the absent path uses.
 * - absent → pushed off-screen along the arrangement axis + faded. The SIGN is
 *   deterministic from slide order: an element whose home is BEFORE the current
 *   slide has already exited (negative → left/up); one whose home is AFTER is
 *   still waiting to enter (positive → right/down). So navigating forward, a
 *   dropped element slides out left/up and the next one arrives from right/down
 *   — exactly the "direction owns the off-state" behavior.
 *
 * `maxDepth` is optional: omit it (the default) for the classic show-everything
 * behavior — byte-identical to before the depth axis existed.
 */
export function resolveElement(
  deck: Deck,
  id: string,
  slideIndex: number,
  maxDepth?: number,
): ResolvedElement | null {
  const here = deck.slides[slideIndex]?.elements.find((e) => e.id === id);
  if (here) {
    if (maxDepth !== undefined && !visibleAtDepth(here, maxDepth)) {
      // On this slide but below the reveal level → off-state ahead (reading
      // direction), so stepping the depth up slides/fades it into place.
      const horizontal = deck.arrangement === "horizontal";
      const { w: sw, h: sh } = deckSize(deck);
      return { el: here, present: false, dx: horizontal ? sw : 0, dy: horizontal ? 0 : sh, opacity: 0 };
    }
    return { el: here, present: true, dx: 0, dy: 0, opacity: 1 };
  }

  let nearest = -1;
  let best = Infinity;
  deck.slides.forEach((s, j) => {
    if (s.elements.some((e) => e.id === id)) {
      const d = Math.abs(j - slideIndex);
      if (d < best) {
        best = d;
        nearest = j;
      }
    }
  });
  if (nearest < 0) return null;

  const base = deck.slides[nearest]?.elements.find((e) => e.id === id);
  if (!base) return null;
  const sign = nearest < slideIndex ? -1 : 1; // past → out back; future → wait ahead
  const horizontal = deck.arrangement === "horizontal";
  const { w: sw, h: sh } = deckSize(deck); // guarded — never touch deck.size raw
  return {
    el: base,
    present: false,
    dx: horizontal ? sign * sw : 0,
    dy: horizontal ? 0 : sign * sh,
    opacity: 0,
  };
}
