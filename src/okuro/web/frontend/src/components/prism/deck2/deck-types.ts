// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism 2D deck v2 (kit-first) — the A4↔A3 slide/deck contract.
//   The kit (src/okuro/prism/kit/board) is the STYLING authority: archetype
//   templates carry `data-slot` regions; this file types the DATA that fills
//   those slots. A3's `compose` stage emits a `DeckDoc` shaped exactly like this;
//   until it lands, fixtures/demo-deck.json is the golden fixture built against
//   the same contract. Renderers (archetypes.tsx) turn a `Slide` into the kit's
//   HTML/classes verbatim — never new styling.
// AGENT_HEADER_END -->

/** The depth ladder, shallow → deep. Columns of the 2D grid are topics; rows are
 *  HERO, INDEX, then L0→L3. L0=cover glance · L1=key points · L2=detail modules ·
 *  L3=full doc-view (reflow). Distinct from the legacy L1–L4 Rung in prism-api.ts;
 *  the kit pipeline (design v3 §4) is L0–L3. */
export type DeckLevel = "L0" | "L1" | "L2" | "L3";
export const DECK_LEVELS: DeckLevel[] = ["L0", "L1", "L2", "L3"];

/** The 12 approved board archetypes (kit-spec §3b · board/archetypes/*.html).
 *  A slide names exactly one; the numeric gate enforces one archetype/slide. */
export type ArchetypeId =
  | "hero"
  | "lens-reframe"
  | "disclosure-list"
  | "scenario-beats"
  | "decision-matrix"
  | "framework-tiles"
  | "binary-choice"
  | "classification-table"
  | "flow-sequence"
  | "proportion-bars"
  | "card-set"
  | "ask-cta";

/** A title as ordered runs; at most ONE run may be accented (kit numeric gate:
 *  ≤1 accent span/title). Rendered into `.slide-title` with `<span class="accent">`
 *  around the accented run. */
export type TitleRun = { text: string; accent?: boolean };
export interface Eyebrow { num?: string; text: string }
/** Persona identity mark colour slot on hero / lens tabs (kit `.persona-mark a|b|c`). */
export type PersonaMark = "a" | "b" | "c";
export interface Persona { mark: PersonaMark; name: string; lens: string }
export type CalloutVariant = "info" | "warn" | "risk" | "decision" | "wow";
export interface Callout { variant?: CalloutVariant; label?: string; body: string }

// ── per-archetype slot payloads ────────────────────────────────────────────

export interface HeroSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  lede?: string;
  personas?: Persona[];
  meta?: { label: string; value: string }[];
}

/** A disclosure/bullet row (shared by disclosure-list + lens panes). */
export interface BulletRow {
  mark?: string;          // "1" | "2" … the numbered chip
  warn?: boolean;         // amber chip for a risk row (kit `.bullet-mark.warn`)
  title: string;
  sub?: string;
  toggle?: string;        // "Detail" | "Evidence" — the expander label
  body?: string;          // revealed detail
}

export interface LensReframeSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  lenses: { persona: Persona; rows: BulletRow[] }[];
}

export interface DisclosureListSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  rows: BulletRow[];      // 3–7
}

export interface ScenarioBeatsSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  beats: { mark: string; body: string; who?: string }[];
  callout?: Callout;
}

export interface DecisionMatrixSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  options: { label: string; title: string; recommended?: boolean; lines: { k: string; v: string }[] }[];
}

/** A table cell may bold its content (kit `<strong>`). */
export interface TableCell { text: string; strong?: boolean }
export interface FrameworkTilesSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  tiles: { label: string; value: string; desc?: string }[];  // 3–5
  table?: { headers: string[]; rows: TableCell[][] };
}

export interface BinaryChoiceSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  paths: { tone: "warn" | "accent"; eyebrow: string; title: string; body: string; footer?: string }[]; // 2
  callout?: Callout;
}

export type Verdict = "essential" | "nogo" | "redesign" | "redundant";
export type HttpMethod = "get" | "post" | "put" | "delete";
/** A column header. `role` drives cell rendering in that column: "method" → HTTP
 *  method badge, "verdict" → verdict badge (coloured by the row's `verdict`).
 *  No role → plain text. Headers are DATA (kit template default = the API-audit
 *  set) so the same archetype carries any verdict/classification table, not only
 *  endpoint audits. */
export interface ClassificationColumn { label: string; role?: "method" | "verdict" }
export interface ClassificationRow {
  /** One cell per column, by index. */
  cells: string[];
  /** Row highlight + the badge class in the verdict-role column (if any). */
  verdict?: Verdict;
}
export interface ClassificationTableSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  summary?: { text: string; stats: { color: string; text: string }[] };
  columns: ClassificationColumn[]; // 2–5; kit default = Endpoint·Method·Verdict·Reason
  rows: ClassificationRow[];
}

export type FlowActor = "user" | "edge" | "cloud" | "process";
export interface FlowSequenceSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  lede?: string;
  steps: { num: string; actor: FlowActor; what: string; tool?: string; ms?: string }[]; // 5–8
}

export type BarState = "safe" | "caution" | "danger";
export interface ProportionBarsSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  lede?: string;
  rows: { stage: string; pct: number; state: BarState; value: string }[];
}

export type StatState = "ok" | "info" | "gold" | "warn" | "";
export interface CardSetSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  stats?: { num: string; numState?: StatState; lbl: string }[];
  cards: { icon?: string; name: string; desc?: string }[];  // 3–5
}

export interface AskCtaSlots {
  eyebrow: Eyebrow;
  title: TitleRun[];
  ask: { title: string; items: string[] };
  arch?: { label: string; value: string; ok?: boolean }[];
}

/** Discriminated slide union — the archetype tag selects the slot payload type. */
export type Slide =
  | { archetype: "hero"; slots: HeroSlots }
  | { archetype: "lens-reframe"; slots: LensReframeSlots }
  | { archetype: "disclosure-list"; slots: DisclosureListSlots }
  | { archetype: "scenario-beats"; slots: ScenarioBeatsSlots }
  | { archetype: "decision-matrix"; slots: DecisionMatrixSlots }
  | { archetype: "framework-tiles"; slots: FrameworkTilesSlots }
  | { archetype: "binary-choice"; slots: BinaryChoiceSlots }
  | { archetype: "classification-table"; slots: ClassificationTableSlots }
  | { archetype: "flow-sequence"; slots: FlowSequenceSlots }
  | { archetype: "proportion-bars"; slots: ProportionBarsSlots }
  | { archetype: "card-set"; slots: CardSetSlots }
  | { archetype: "ask-cta"; slots: AskCtaSlots };

// ── provenance + L1 visual + multi-perspective lenses (PRISM v4 W5 Phase D) ───

/** A source ref surfaced on a ref-carrying component (§3a provenance affordance).
 *  `quote` is the verbatim evidence span mined from the source; `artifactId` links
 *  back to the okuro artifact it was grounded in. Rendered as a clickable "source"
 *  affordance that opens a popover — visible on any cell whose claims carry refs. */
export interface Provenance {
  claim: string;            // claim id the quote grounds
  quote: string;            // verbatim evidence span
  artifactId: string;       // source okuro artifact id
  artifactTitle?: string;   // human label for the link
}

export type CellVisualKind = "bar" | "stat" | "proportion" | "illustration";
/** A REAL visual element carried on a topic's L1 hook (§3c / answer 8 orphan
 *  gate). `bar`/`proportion` = a data chart derived from the claim's own mined
 *  magnitudes; `stat` = the number-is-the-point single figure; `illustration` =
 *  a generated inline SVG (self-contained, export-safe). Never invented — every
 *  chart datum traces to `source`. */
export interface CellVisual {
  kind: CellVisualKind;
  title?: string;
  /** bar / proportion: one entry per mined item. `value` is 0–100 (normalised for
   *  bar width); `display` is the honest label ("8 items", "sub-10 ms"). */
  bars?: { label: string; value: number; display: string; state?: BarState }[];
  /** stat: the single dominant figure (the number is the point). */
  stat?: { value: string; label: string; sub?: string };
  /** illustration: a self-contained inline SVG string (no external refs). */
  svg?: string;
  /** The claim this visual is derived from (drives the provenance affordance). */
  source?: Provenance;
}

/** A cell in the grid: the primary slide plus optional A/B alternates (design v3
 *  §5). `alternates` are additional whole-slide arrangements of the SAME claim;
 *  the reader keeps one via the edit-mode swap control, persisted to the deck. */
export interface DeckCell {
  slide: Slide;
  alternates?: Slide[];
  /** Chosen alternate index (0 = primary `slide`, 1..N = `alternates[i-1]`). */
  pick?: number;
  /** A real visual element for this cell (L1 hooks carry one — the §3c gate). */
  visual?: CellVisual;
  /** Source refs for the claims rendered in this cell (§3a provenance gate). */
  provenance?: Provenance[];
  /** Per-lens rewritten slide overlays, keyed by lens id. The audience-translation
   *  engine (Phase B) re-words the SAME structure per persona; the viewer's active
   *  lens tab picks `lensSlides[lensId] ?? slide`. Structure is identical across
   *  lenses (clean-insertion), so accuracy/visual/provenance are lens-invariant. */
  lensSlides?: Record<string, Slide>;
  /** The v4 free-composition SOLVER's real kit HTML for this cell (from
   *  compose_ladder). When present the viewer renders THIS (ComposedCell) instead
   *  of downcasting to the 12-archetype `slide` — which stays only as a fallback.
   *  `html`/`altHtml` are wrapperless `.composed-slide` fragments, styled by the
   *  kit CSS already injected into the deck's shadow root. */
  composed?: ComposedRender;
}

/** A composed slide: the solver's chosen kit components, rendered to HTML by the
 *  SAME Python kit builders as the gallery (byte-identical markup, no re-render).
 *  `altHtml` is the B variant for the A/B swap. */
export interface ComposedRender {
  html: string;
  altHtml?: string | null;
  family?: string;
  component_ids?: string[];
  overflow?: boolean | null;
  /** Share of this cell's rendered content words that come from its own claims
   *  (`to_composed.cell_fidelity`). A component that renders its gallery fixture
   *  instead of the document scores near 0 while passing every structural check,
   *  so this is the only signal that distinguishes a real surface from a
   *  convincing-looking placeholder. Under `prism.strict` a cell below the floor
   *  refuses the build; with strict off it ships and the viewer must SAY so —
   *  a degradation the reader cannot see is a degradation that ships. */
  fidelity?: number | null;
}

/** One multi-perspective lens tab (§ answer 10 — the deck carries per-lens framing
 *  like the hand-authored original). `persona` is the recipient it is pitched to. */
export interface DeckLens {
  id: string;
  persona: Persona;
}

/** A luminance-aware brand logo (§3c orphan gate). The viewer picks `dark` on a
 *  dark canvas and `light` on a light canvas. Both are self-contained inline SVG
 *  (or data-URI) so the exported deck renders offline. `corner` = placement. */
export interface BrandLogo {
  corner?: "tl" | "tr" | "bl" | "br";
  dark: string;             // logo for a DARK background (light ink)
  light: string;            // logo for a LIGHT background (dark ink)
}

/** L3 doc-view: REFLOWS (never scales) — TOC scrollspy + measure-capped prose +
 *  embedded L2 modules as figures (kit zoom-stage.css `.l3-doc`). */
export interface DocSection {
  id: string;                // anchor id, referenced by the TOC
  heading: string;
  level?: 1 | 2;             // h1 / h2 within the doc
  md?: string;               // markdown body (rendered via MarkdownLite → kit .prose)
  /** An embedded module rendered as a figure — a lower-level slide shown inline
   *  (design v3 §7 "embedded L2 modules as figures"). */
  figure?: { slide: Slide; caption?: string };
  /** Cross-topic link targets (topicId) surfaced under the section. */
  crossLinks?: { topicId: string; label: string }[];
}
export interface DocView {
  title: string;
  sections: DocSection[];
  width?: "reading" | "wide" | "full" | "ultra";  // doc width tier
}

export interface DeckTopic {
  id: string;
  title: string;             // nav / index label
  /** L0–L2 cells; L3 is the doc-view. A topic need not fill every level. */
  levels: Partial<Record<Exclude<DeckLevel, "L3">, DeckCell>>;
  l3?: DocView;
}

export type BrandId = "okuro" | "northwind" | "meridian";

// ── accuracy accounting (PRISM v4 W3 §3d → W4 renders) ───────────────────────
// Mirrors okuro.prism.authoring.accuracy.AccuracyReport.to_deckdoc(). Carried on
// the DeckDoc so the viewer surfaces every truth the pipeline computed:
// synthesized/fallback cells, deficit affordances ("N of M — all M in L4"),
// count-truth mismatches, and content-gaps — never a silent thin slide.

/** One rendered unit's accuracy (accuracy.py UnitAccuracy.to_field()). */
export interface AccuracyUnit {
  claim: string;
  component: string;
  emphasis: string;
  rendered: number;              // N shown at this level
  total: number;                 // M the claim carries
  in_l4: boolean;                // the deficit is honestly labelable (all M reach L4)
  synthesized: boolean;          // code-synthesized (fail-soft) cell
  fallback: boolean;             // fell back to a lower-fit component
  affordance: string;            // "N of M — all M in L4" when N<M, else ""
  count_mismatch: boolean;       // title number != rendered items (count-truth)
  content_available: number;     // real per-item content triples present
  content_incomplete: boolean;   // fewer real items than rendered → synthetic fill
}

/** DeckDoc.accuracy — the whole-deck accounting (accuracy.py to_deckdoc()). */
export interface DeckAccuracy {
  /** Per-level unit list (SINGLE-topic form — the W3 to_deckdoc() output). Keyed
   *  by the level the unit renders at; accepts internal (L0–L3) OR display (L1–L4)
   *  keys. Used as the fallback when `cells` is absent. */
  levels: Partial<Record<string, AccuracyUnit[]>>;
  /** Per-CELL unit list for multi-topic decks, keyed "<row>-<col>" (row = level
   *  L0–L3, col = topic index) — so the strip shows only the focused cell's units.
   *  Preferred over `levels` when present. */
  cells?: Partial<Record<string, AccuracyUnit[]>>;
  silent_deficits: string[];     // deficits that CANNOT be honestly labelled (errors)
  count_mismatches: string[];
  synthesized: string[];
  fallback: string[];
  content_gaps: string[];        // rendered cells backed by no real content
}

export interface DeckDoc {
  id: string;
  title: string;
  brand: BrandId;
  tagline?: string;
  hero: DeckCell;            // the HERO slide (archetype "hero"), grid row 0
  topics: DeckTopic[];       // columns of the grid
  /** Optional accuracy accounting (W3 pipeline output; W4 renders it). */
  accuracy?: DeckAccuracy;
  /** Multi-perspective lens tabs (Phase D). When present the viewer shows a tab
   *  bar; switching a tab re-renders every cell through its `lensSlides[lensId]`.
   *  The first lens (or the base `slide`) is the default view. */
  lenses?: DeckLens[];
  /** Luminance-aware brand logo, placed in a corner and flipped by canvas theme. */
  brandLogo?: BrandLogo;
}

/** Resolve the effective slide for a cell honoring the active lens THEN the A/B
 *  pick. A lens overlay (Phase D audience translation) wins over the base slide;
 *  A/B alternates apply to the base structure only (lens overlays are text-only
 *  rewrites of the primary). */
export function resolveLensSlide(cell: DeckCell, lensId: string | null): Slide {
  if (lensId && cell.lensSlides && cell.lensSlides[lensId]) return cell.lensSlides[lensId]!;
  return resolveSlide(cell);
}

// ── level display (canonical 1-indexed L1–L4) ────────────────────────────────
// The deck2 contract keys levels L0–L3 (L3 = the full doc-view / reflow). The
// PRISM v4 design + viewer counter speak the 1-indexed ladder L1–L4, where L4 is
// the deepest full-text level. These map the internal key to what the reader sees;
// "all M in L4" therefore points at the deepest level (DEEPEST_LEVEL).
export const DECK_LEVEL_LABELS: Record<DeckLevel, string> = {
  L0: "L1", L1: "L2", L2: "L3", L3: "L4",
};
export const DEEPEST_LEVEL: DeckLevel = "L3";
/** 1-indexed depth position (L0 → 1 … L3 → 4). */
export function levelDisplayNum(level: DeckLevel): number {
  return DECK_LEVELS.indexOf(level) + 1;
}
/** Resolve a level's accuracy units, accepting internal (L0–L3) OR display
 *  (L1–L4) keys so it survives either pipeline emission convention. */
export function accuracyForLevel(
  acc: DeckAccuracy | undefined,
  level: DeckLevel,
): AccuracyUnit[] {
  if (!acc?.levels) return [];
  return acc.levels[level] ?? acc.levels[DECK_LEVEL_LABELS[level]] ?? [];
}
/** Resolve the accuracy units for a specific cell. Prefers the per-cell map (a
 *  multi-topic deck); falls back to the per-level map (single-topic W3 form,
 *  correct when there is one topic / col 0). */
export function accuracyForCell(
  acc: DeckAccuracy | undefined,
  row: DeckLevel,
  col: number,
): AccuracyUnit[] {
  if (!acc) return [];
  const cell = acc.cells?.[`${row}-${col}`] ?? acc.cells?.[`${DECK_LEVEL_LABELS[row]}-${col}`];
  if (cell) return cell;
  return accuracyForLevel(acc, row);
}

/** Grid coordinate. row: "hero" | "index" | DeckLevel; col: topic index (0-based).
 *  Encoded in the URL hash as `#t=<col>&d=<row>` for deep links. Hero/index have
 *  no column (they span). */
export type GridRow = "hero" | "index" | DeckLevel;
export interface GridCoord { row: GridRow; col: number }

/** Does a topic have a renderable cell at this level? */
export function hasCell(topic: DeckTopic, level: DeckLevel): boolean {
  if (level === "L3") return !!topic.l3;
  return !!topic.levels[level];
}

/** Resolve the effective slide for a cell honoring the A/B pick. */
export function resolveSlide(cell: DeckCell): Slide {
  const pick = cell.pick ?? 0;
  if (pick <= 0) return cell.slide;
  const alt = cell.alternates?.[pick - 1];
  return alt ?? cell.slide;
}
