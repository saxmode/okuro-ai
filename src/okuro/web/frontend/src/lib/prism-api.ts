// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism API client — facet-tree doc CRUD + generate/edit/retailor,
//   mirrors lib/slides-api.ts. Reuses brandsApi/brandToTheme (same brand-resolve
//   contract both tools consume).
// AGENT_HEADER_END -->
import { api } from "./api";

export { brandsApi, brandAssetsApi, brandToTheme, tokenizeApiSrc, pickLogoForBackground } from "./slides-api";
export type { BrandAsset, BrandSummary, BrandTheme, BgTarget } from "./slides-api";

// The four per-topic depth levels (the slide-model ladder), shallow → deep:
//   L1 = cover · L2 = key points · L3 = detail · L4 = full-doc (doc-view).
// Renamed from the legacy glance/brief/working/expert; the backend storage layer
// normalises any legacy-keyed deck to these on read, so the client only sees L1–L4.
export type Rung = "L1" | "L2" | "L3" | "L4";
/** Model tier for generation → haiku/sonnet/opus on claude (provider-agnostic). */
export type ModelTier = "fast" | "mid" | "top";
/** Layout-diversity knob: 1 conservative · 2 balanced · 3 expressive. */
export type Diversity = 1 | 2 | 3;
/** Corner the sticky logo module is pinned to on a deck. */
export type LogoCorner = "tl" | "tr" | "bl" | "br";
export type FacetKind = "topic" | "diagram" | "decision" | "metric";
export type Density = "concise" | "balanced" | "detailed";
export type Jargon = "plain" | "balanced" | "technical";

export const RUNGS: Rung[] = ["L1", "L2", "L3", "L4"];

/** Slide type a level renders as (mirrors okuro.prism.slides). Backend-stamped on
 *  each RungContent; the 2D grid keys off it to render covers + doc-views. */
export type SlideType = "hero" | "index" | "cover" | "section" | "content" | "doc-view";

export interface Callout { type: "risk" | "decision" | "wow"; text: string }
export interface Media { kind: "mermaid"; spec: string }

/** Typed, provenance-tagged content module. A rung's content is an ordered
 *  list of these — the block model. `source_ref` is the claim's origin (a URL,
 *  doc id, kg ref) so provenance survives into any module. */
export type Block =
  | { type: "text"; md: string; source_ref?: string }
  | { type: "heading"; eyebrow?: string; title: string; lede?: string; display?: "inline" | "standalone"; source_ref?: string }
  | { type: "stat"; items: { value: string; label: string; delta?: string; trend?: "up" | "down" | "flat"; state?: "good" | "warn" | "bad"; sub?: string }[]; source_ref?: string }
  | { type: "table"; headers: string[]; rows: string[][]; rowStates?: ("good" | "warn" | "bad" | "highlight")[]; source_ref?: string }
  | { type: "callout"; variant: "risk" | "decision" | "wow" | "info" | "warn"; text: string; source_ref?: string }
  | { type: "image"; src: string; caption?: string; overlay?: { title?: string; text?: string; align?: "left" | "center" }; display?: "inline" | "standalone"; source_ref?: string }
  | { type: "diagram"; kind: "mermaid"; spec: string; source_ref?: string }
  // M2 rich-viz modules — heavier renderers, lazy-loaded by BlockView.
  | { type: "chart"; chart: "bar" | "line" | "area" | "pie"; series: { label: string; value: number }[]; unit?: string; caption?: string; annotations?: { at: number; text: string }[]; source_ref?: string }
  | { type: "smallmultiples"; chart: "bar" | "line" | "area"; panels: { label: string; series: { label: string; value: number }[] }[]; unit?: string; caption?: string; source_ref?: string }
  | { type: "graph"; nodes: { id: string; label: string; group?: string; spine?: boolean; kind?: string; summary?: string; tags?: string[] }[]; edges: { from: string; to: string; label?: string }[]; caption?: string; source_ref?: string }
  | { type: "flow"; flow_id: string; caption?: string; source_ref?: string }
  // M4 storytelling modules — narrative-shaped presentational blocks.
  | { type: "lens"; tabs: { label: string; md: string }[]; source_ref?: string }
  | { type: "timeline"; events: { time?: string; title: string; body?: string; actor?: string }[]; source_ref?: string }
  | { type: "options"; options: { title: string; recommended?: boolean; pros?: string[]; cons?: string[]; note?: string }[]; source_ref?: string }
  | { type: "compare"; before: { label?: string; items: string[] }; after: { label?: string; items: string[] }; source_ref?: string }
  | { type: "cards"; columns?: number; cards: { title: string; body?: string; tag?: string }[]; source_ref?: string }
  // M5 decision-artifact modules — the reference deck's "more than slides" data-viz.
  | { type: "matrix"; columns: string[]; rows: { label: string; cells: (string | boolean)[] }[]; highlightCol?: number; caption?: string; source_ref?: string }
  | { type: "meter"; items: { label: string; value: number; max?: number; note?: string; state?: "good" | "warn" | "bad" }[]; unit?: string; caption?: string; source_ref?: string }
  | { type: "steps"; steps: { n?: string; title: string; detail?: string; badge?: string; actor?: "edge" | "cloud" | "user" | "process"; meta?: string }[]; caption?: string; source_ref?: string }
  | { type: "spec"; cards: { title: string; variant?: "active" | "research" | "ambient" | "default"; badge?: string; rows: { k: string; v: string }[] }[]; caption?: string; source_ref?: string }
  | { type: "code"; code: string; lang?: string; filename?: string; source_ref?: string }
  | { type: "cta"; title?: string; items?: string[]; action?: { label: string; href: string }; variant?: "ask" | "info"; source_ref?: string }
  | { type: "checklist"; items: { text: string; done?: boolean }[]; columns?: number; source_ref?: string }
  | { type: "redteam"; challenges: { objection: string; severity?: "high" | "med" | "low"; rebuttal?: string; who?: string }[]; panel?: string[]; caption?: string; source_ref?: string }
  | { type: "decisionrecord"; question?: string; decision: string; options?: { label: string; chosen?: boolean; rejected_because?: string }[]; reversal_trigger?: string; confidence?: string; source_ref?: string }
  | { type: "assumptionledger"; assumptions: { text: string; confidence?: "high" | "med" | "low"; falsifier?: string; load_bearing?: boolean }[]; caption?: string; source_ref?: string }
  | { type: "tripwires"; wires: { condition: string; metric?: string; status?: "clear" | "watch" | "tripped" }[]; caption?: string; source_ref?: string }
  | { type: "risk"; items: { title: string; severity?: "high" | "med" | "low"; likelihood?: "high" | "med" | "low"; mitigation?: string; owner?: string; refs?: string[] }[]; caption?: string; source_ref?: string }
  | { type: "audiobrief"; src: string; duration?: number; voice?: string; transcript?: string; caption?: string; source_ref?: string }
  | { type: "simulator"; inputs: { key: string; label: string; min: number; max: number; step?: number; value: number; unit?: string }[]; outputs: { label: string; expr: string; unit?: string; decimals?: number }[]; caption?: string; source_ref?: string }
  // Rigor modules — the board-grade moat: calibrated uncertainty, evidence-grade
  // provenance, bias pre-emption, audience-weighted proof.
  | { type: "frequency"; items: { label: string; count: number; of: number; state?: "good" | "warn" | "bad" }[]; caption?: string; source_ref?: string }
  | { type: "hops"; draws?: number[]; quantiles?: { p10: number; p50: number; p90: number }; unit?: string; label?: string; reference?: number; referenceLabel?: string; fps?: number; decimals?: number; caption?: string; source_ref?: string }
  | { type: "evidence"; claims: { text: string; source?: string; confidence?: "high" | "med" | "low"; as_of?: string; grade?: "primary" | "secondary" | "model" | "assumption" }[]; caption?: string; source_ref?: string }
  | { type: "biascheck"; biases: { bias: string; risk: string; counter: string; severity?: "high" | "med" | "low" }[]; caption?: string; source_ref?: string }
  | { type: "messageproof"; audiences?: string[]; messages: { message: string; proofs: { text: string; audiences?: string[]; weight?: "high" | "med" | "low"; source_ref?: string }[] }[]; caption?: string; source_ref?: string }
  // Typographic + visual modules — real text/layout variety (research W1).
  // `display: "standalone"` renders the module full-bleed as its own slide.
  | { type: "statement"; text: string; kicker?: string; footnote?: string; align?: "left" | "center"; display?: "inline" | "standalone"; source_ref?: string }
  | { type: "quote"; quote: string; attribution?: string; role?: string; display?: "inline" | "standalone"; source_ref?: string }
  | { type: "gallery"; images: { src: string; caption?: string }[]; columns?: number; display?: "inline" | "standalone"; caption?: string; source_ref?: string }
  | { type: "visualizer"; source: "studio" | "illustrator" | "icon" | "podcast"; src?: string; svg?: string; alt?: string; caption?: string; transcript?: string; duration?: number; display?: "inline" | "standalone"; source_ref?: string };

export type BlockType = Block["type"];

/** Layout (composition layer): arranges a rung's FLATTENED block list
 *  (`rungToBlocks` order) into rows of column-spanned cells over a 12-col grid.
 *  `i` indexes into that flattened list; `span` is the column width (1–12).
 *  Derived server-side (okuro.prism.layout). Absent → flat vertical stack. */
export interface LayoutCell { i: number; span?: number }
export interface LayoutRow { cells: LayoutCell[] }
export interface Layout { archetype?: string; rows: LayoutRow[] }

export interface RungContent {
  /** Legacy single-body form — normalized to a text block for rendering. */
  body?: string;
  media?: Media[];
  callouts?: Callout[];
  /** Block model: ordered typed modules. When present, takes precedence. */
  blocks?: Block[];
  /** Grid arrangement over the flattened block list (see Layout). */
  layout?: Layout;
  /** Slide type this level renders as (backend-stamped by okuro.prism.slides).
   *  L1→cover/section · L2/L3→content · L4→doc-view. Absent on legacy decks. */
  slide_type?: SlideType;
  /** Designed template binding (backend-stamped by okuro.prism.templates): an id
   *  + extracted slot data. When present the frontend renders the designed
   *  template instead of the legacy block stack. Absent → block stack. */
  template?: { id: string; slots: Record<string, unknown> };
  /** A/B alternatives — additional visual arrangements of THIS rung's content
   *  the reader can compare and keep a favorite of. Purely additive. */
  alts?: { blocks?: Block[]; layout?: Layout; label?: string }[];
}

/** Rung → block list. The prose `body` (+ media + callouts) is the retailor-
 *  editable spine, synthesized into blocks; `blocks` carries rich visual modules
 *  (stat/table/heading/image) that AUGMENT the prose, appended after it. */
export function rungToBlocks(rung: RungContent | undefined): Block[] {
  if (!rung) return [];
  const out: Block[] = [];
  if (rung.body && rung.body.trim()) out.push({ type: "text", md: rung.body });
  (rung.media ?? []).forEach((m) => out.push({ type: "diagram", kind: "mermaid", spec: m.spec }));
  (rung.callouts ?? []).forEach((c) => out.push({ type: "callout", variant: c.type, text: c.text }));
  (rung.blocks ?? []).forEach((b) => out.push(b));
  return out;
}

/** The COVER line for a facet (mirrors okuro.prism.slides.cover_line) — EXTRACTIVE:
 *  an existing L1 hook statement > the assertion headline > the title. Used to
 *  render the L1 cover slide even when the backend didn't inject a cover block
 *  (e.g. the generate path). */
export function coverLine(facet: Facet): string {
  const l1 = facet.rungs?.L1;
  const hook = (l1?.blocks ?? []).find(
    (b) => b.type === "statement" && !!(b as { text?: string }).text?.trim(),
  ) as { text?: string } | undefined;
  return (hook?.text?.trim() || facet.headline?.trim() || facet.title?.trim() || "");
}

/** The L4 'full documentation' body (mirrors okuro.prism.slides.docview_markdown):
 *  the facet's entire prose ladder (L1→L4 bodies) concatenated + dedup'd, deepest
 *  context last. Purely extractive — every line already exists in a level body. */
export function docviewMarkdown(facet: Facet): string {
  const seen = new Set<string>();
  const parts: string[] = [];
  for (const r of RUNGS) {
    const body = (facet.rungs?.[r]?.body ?? "").trim();
    if (body && !seen.has(body)) { seen.add(body); parts.push(body); }
  }
  return parts.join("\n\n");
}

export interface Facet {
  id: string;
  title: string;
  /** Assertion-evidence headline: the single-sentence TAKEAWAY of this section
   *  (the "so what"), distinct from `title` (a short nav/TOC label). Rendered as
   *  the section heading; falls back to `title` when absent. */
  headline?: string;
  kind: FacetKind;
  parent_id: string | null;
  children: string[];
  rungs: Record<Rung, RungContent>;
}

export interface PrismTree {
  id: string;
  title: string;
  brand_id?: string;
  /** Per-deck logo-corner override; absent ⇒ inherit the brand default. */
  logo_corner?: LogoCorner;
  entry_facet_id: string;
  entry_rung?: Rung;
  /** Deck-level takeaway shown as the HERO slide's subtitle (set by the build
   *  pipeline from the strategist takeaway). */
  tagline?: string;
  facets: Record<string, Facet>;
}

export interface DocSummary {
  id: string;
  title: string;
  brand_id: string | null;
  variant_of: string | null;
  facet_count: number;
  created_at: string;
  updated_at: string;
}

export interface DocDetail extends DocSummary {
  doc: PrismTree;
  entry_rung?: Rung;
  op_errors?: string[];
  ops_applied?: number;
  applied?: Record<string, unknown>;
  as_variant?: boolean;
}

/** A doc summary enriched with variant linkage (returned by /variants). */
export interface VariantSummary extends DocSummary {}

/** Options for a generation request (recipient/brand/model-tier/layout-diversity). */
export interface GenerateOpts {
  personId?: string;
  brandId?: string;
  tier?: ModelTier;
  diversity?: Diversity;
}

/** A live generation job — the spinner + phase source polled by the gallery.
 *  `phase` walks queued → analyzing → writing → designing → done. */
export interface GenerateJob {
  id: string;
  topic: string;
  brand_id: string | null;
  person_id: string | null;
  tier: string | null;
  diversity: number | null;
  phase: string;
  status: "running" | "done" | "error";
  doc_id: string | null;
  title: string | null;
  error: string | null;
  created: number;
  updated: number;
}

export interface VariantsResponse {
  source: VariantSummary | null;
  variants: VariantSummary[];
  source_id: string;
}

export interface RetailorAxes {
  personId?: string;
  depthOverride?: Rung;
  density?: Density;
  jargon?: Jargon;
  language?: string;
  /** Also re-pick WHICH visual modules each rung uses for the recipient. */
  reselectModules?: boolean;
  asVariant?: boolean;
}

export const prismApi = {
  list: () => api<{ docs: DocSummary[]; seq: number }>("/api/prism"),
  get: (id: string) => api<DocDetail>(`/api/prism/${encodeURIComponent(id)}`),
  /** Resolve a person's DEFAULT entry rung (density+jargon heuristic) for the
   *  live "view as persona" depth switch — no regeneration. */
  entryRungFor: (personId: string) =>
    api<{ person_id: string | null; rung: Rung }>(
      `/api/prism/entry-rung?person_id=${encodeURIComponent(personId)}`,
    ),
  /** Kick off a generation. Returns a job_id immediately — the two-pass LLM
   *  run continues on the server; poll `generateJobs` for phase + completion. */
  generate: (topic: string, opts: GenerateOpts = {}) =>
    api<{ job_id: string }>("/api/prism/generate", {
      method: "POST",
      body: JSON.stringify({
        topic,
        person_id: opts.personId,
        brand_id: opts.brandId,
        tier: opts.tier,
        diversity: opts.diversity,
      }),
    }),
  /** Active + recently-finished generation jobs (spinner + live phase). */
  generateJobs: () => api<{ jobs: GenerateJob[] }>("/api/prism/generate/jobs"),
  /** Run the target pipeline: a root document (source text) → a faithful,
   *  recipient-fit, critic-gated deck. Async — returns a job_id; poll generateJobs. */
  buildFromArtifact: (
    target: string,
    opts: { sourceText?: string; artifactId?: string; noteId?: string; title?: string; brandId?: string } = {},
  ) =>
    api<{ job_id: string }>("/api/prism/build-from-artifact", {
      method: "POST",
      body: JSON.stringify({
        target,
        source_text: opts.sourceText,
        artifact_id: opts.artifactId,
        note_id: opts.noteId,
        title: opts.title,
        brand_id: opts.brandId,
      }),
    }),
  /** NL edit of a doc. Pass ``facetId`` to scope the edit to a single facet
   *  (contextual "edit this slide"). */
  edit: (docId: string, instruction: string, facetId?: string) =>
    api<DocDetail>("/api/prism/edit", {
      method: "POST",
      body: JSON.stringify({ doc_id: docId, instruction, facet_id: facetId }),
    }),
  /** Author a second visual arrangement of every slide (A/B). Additive. */
  generateAlternatives: (docId: string) =>
    api<DocDetail & { alternatives_added?: number }>(
      `/api/prism/${encodeURIComponent(docId)}/alternatives`,
      { method: "POST" },
    ),
  /** Commit an A/B choice for one facet's rung. altIndex undefined ⇒ keep A. */
  pickAlt: (docId: string, facetId: string, rung: Rung, altIndex?: number) =>
    api<DocDetail>(`/api/prism/${encodeURIComponent(docId)}/pick-alt`, {
      method: "POST",
      body: JSON.stringify({ facet_id: facetId, rung, alt_index: altIndex ?? null }),
    }),
  applyOps: (docId: string, ops: Record<string, unknown>[]) =>
    api<DocDetail>("/api/prism/apply-ops", {
      method: "POST",
      body: JSON.stringify({ doc_id: docId, ops }),
    }),
  save: (title: string, doc: PrismTree, id?: string, brandId?: string) =>
    api<DocDetail>("/api/prism", {
      method: "POST",
      body: JSON.stringify({ id, title, doc, brand_id: brandId }),
    }),
  remove: (id: string) =>
    api<{ deleted: boolean }>(`/api/prism/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** Restyle an existing deck: switch its brand and/or per-deck logo corner
   *  without regenerating content. Omit a field to leave it unchanged; pass
   *  logo_corner: "" to clear the override (fall back to the brand default). */
  setBrand: (docId: string, patch: { brand_id?: string; logo_corner?: LogoCorner | "" }) =>
    api<DocDetail>(`/api/prism/${encodeURIComponent(docId)}/brand`, {
      method: "POST",
      body: JSON.stringify(patch),
    }),
  /** Re-tailor a doc to a recipient + density/jargon/language/depth_override
   *  axes. With asVariant (default true) saves a NEW linked variant. */
  retailor: (docId: string, axes: RetailorAxes) =>
    api<DocDetail>("/api/prism/retailor", {
      method: "POST",
      body: JSON.stringify({
        doc_id: docId,
        person_id: axes.personId,
        depth_override: axes.depthOverride,
        density: axes.density,
        jargon: axes.jargon,
        language: axes.language,
        reselect_modules: axes.reselectModules ?? false,
        as_variant: axes.asVariant ?? true,
      }),
    }),
  /** List a doc's audience variants (source + variants). Accepts a source or
   *  any variant id — the server resolves to the source via variant_of. */
  variants: (sourceId: string) =>
    api<VariantsResponse>(`/api/prism/variants?source=${encodeURIComponent(sourceId)}`),
  events: (since: number) =>
    api<{ events: { doc_id: string; kind: string }[]; seq: number }>(`/api/prism/events?since=${since}`),
};

// ── deck2 (kit-first DeckDoc) client — the A4 2D viewer contract ──────────────
// Distinct from the facet-tree prismApi above: deck2 is the compiler→adapter
// DeckDoc served by /api/prism/deck2 (see orchestrator/api/prism.py). The 2D
// viewer (/prism/deck?id=) fetches a DeckDoc here and POSTs A/B picks back.
import type { BrandId, DeckDoc, DeckLevel } from "@/components/prism/deck2/deck-types";
import { ApiError, getToken } from "./api";

export interface Deck2Summary {
  id: string;
  title: string;
  brand: string;
  /** File mtime (ISO) — the low-touch creation proxy; deck JSON carries no own
   *  timestamp. Drives the tile date + newest-first sort on the overview. */
  created?: string;
  /** Topic (column) count, for the tile's "N topics" meta. */
  topics?: number;
  /** Deck-level takeaway (hero subtitle), shown as the tile's preview line. */
  tagline?: string;
}

export const prismDeck2Api = {
  /** All compiled deck2 decks (id + title + brand). */
  list: () => api<{ decks: Deck2Summary[] }>("/api/prism/deck2"),
  /** Fetch a compiled DeckDoc by id. */
  get: (id: string) => api<DeckDoc>(`/api/prism/deck2/${encodeURIComponent(id)}`),
  /** Persist an A/B pick on a cell (edit-mode swap). row 'hero'|'L0'|'L1'|'L2'. */
  pick: (id: string, row: "hero" | DeckLevel, col: number, pick: number) =>
    api<DeckDoc>(`/api/prism/deck2/${encodeURIComponent(id)}/pick`, {
      method: "POST",
      body: JSON.stringify({ row, col, pick }),
    }),
  /** Compile a DeckDoc from an okuro artifact for recipients (async → job_id). */
  compile: (artifactId: string, recipients: string[], brand?: string) =>
    api<{ job_id: string }>("/api/prism/deck2/compile", {
      method: "POST",
      body: JSON.stringify({ artifact_id: artifactId, recipients, brand }),
    }),
  /** Live restyle: switch the deck's brand (VISUALS only) without regenerating.
   *  The voice follows the audience — retailor to change wording. */
  setBrand: (id: string, brand: BrandId) =>
    api<DeckDoc>(`/api/prism/deck2/${encodeURIComponent(id)}/brand`, {
      method: "POST",
      body: JSON.stringify({ brand }),
    }),
  /** Re-tailor the deck to another audience — reuses the compile pipeline with a
   *  new recipient (entry point; full audience-rewrite is W5). Async → job_id. */
  retailor: (id: string, recipient: string, brand?: string) =>
    api<{ job_id: string }>(`/api/prism/deck2/${encodeURIComponent(id)}/retailor`, {
      method: "POST",
      body: JSON.stringify({ recipient, brand }),
    }),
  /** Delete a compiled deck. */
  remove: (id: string) =>
    api<{ deleted: boolean }>(`/api/prism/deck2/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** Download the deck as a self-contained, offline HTML file (R33). The browser
   *  won't attach the bearer header to an <a download>, so fetch as a blob here
   *  (with auth) and trigger the save from the object URL. */
  exportFile: async (id: string): Promise<void> => {
    const path = `/api/prism/deck2/${encodeURIComponent(id)}/export`;
    const send = async (token: string) => fetch(path, { headers: { Authorization: `Bearer ${token}` } });
    let token = await getToken();
    let res = await send(token);
    if (res.status === 401) { token = await getToken(true); res = await send(token); }
    if (!res.ok) throw new ApiError(res.statusText, res.status);
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename="([^"]+)"/.exec(cd);
    const name = m?.[1] || `${id}.html`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click();
    a.remove(); URL.revokeObjectURL(url);
  },
};
