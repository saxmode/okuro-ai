// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism viewer — scroll-to-reveal deck over the block model. The
//   facet tree renders as full-height sections; each facet's rungs (glance→
//   expert) become blocks: glance = lede, brief = the visible modules, working+
//   expert = "Go deeper" modules. Each block is a typed, provenance-tagged
//   module (text/heading/stat/table/callout/image/diagram) rendered by BlockView.
//   Reveal = one-shot IntersectionObserver; brand theme applied as canvas.
//   M3 nav: dot-rail (doubles as TOC) + keyboard (↑/↓·j/k·digits·Home/End) +
//   active-section tracking; ?facet=<id> routes to DetailFacet (altitude 3 —
//   one facet, all rungs, full page, drill into children).
// index: imports | Reveal | flattenFacets | firstText | PersonLite | DetailFacet | PrismViewer
// AGENT_HEADER_END -->
import { ArrowLeft, Download, Layers, Maximize2, Minimize2, Sparkles } from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import { api } from "@/lib/api";
import { BlockGrid, BlockView } from "@/components/prism/blocks";
import { DeckIndex } from "@/components/prism/deck-index";
import { DetailFacet, RUNG_LABEL } from "@/components/prism/detail-facet";
import { GridDeck } from "@/components/prism/grid-deck";
import {
  brandAssetsApi,
  brandsApi,
  brandToTheme,
  pickLogoForBackground,
  prismApi,
  rungToBlocks,
  tokenizeApiSrc,
  type BrandAsset,
  type BrandSummary,
  type BrandTheme,
  type Block,
  type Diversity,
  type DocDetail,
  type Facet,
  type LogoCorner,
  type ModelTier,
  type Rung,
  type VariantSummary,
} from "@/lib/prism-api";

/** Rough perceived-luminance test for a CSS colour (hex or rgb()). Defaults to
 *  "dark" for unknown input — the prism canvas is dark by default — so the
 *  renderer picks a light/white logo mark unless the background is clearly light. */
function isDarkBg(color?: string): boolean {
  if (!color) return true;
  const luma = (r: number, g: number, b: number) => 0.299 * r + 0.587 * g + 0.114 * b < 128;
  let hex = color.trim().replace(/^#/, "");
  if (/^[0-9a-f]{3}$/i.test(hex)) hex = hex.split("").map((c) => c + c).join("");
  if (/^[0-9a-f]{6}$/i.test(hex)) {
    const n = parseInt(hex, 16);
    return luma((n >> 16) & 255, (n >> 8) & 255, n & 255);
  }
  const m = /rgba?\(([^)]+)\)/.exec(color);
  if (!m) return true; // unknown format → treat as dark (canvas default)
  const p = (m[1] ?? "").split(",").map((x) => parseFloat(x) || 0);
  return luma(p[0] ?? 0, p[1] ?? 0, p[2] ?? 0);
}

// 80px inset from both edges of the corner the logo is pinned to (Tailwind 20 = 5rem = 80px).
const CORNER_POS: Record<LogoCorner, string> = {
  tl: "top-20 left-20",
  tr: "top-20 right-20",
  bl: "bottom-20 left-20",
  br: "bottom-20 right-20",
};

/** Whether the viewer asked the OS/browser to minimise motion. Live-updates if
 *  the setting changes. The gist must never be gated behind an animation, so a
 *  reduced-motion reader gets every reveal rendered statically at full opacity. */
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true,
  );
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!mq) return;
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

/** One-shot scroll reveal (reference-deck mechanic): fades + lifts in when it enters
 *  the reading zone, never re-hides. Staggered via delay. Reduced-motion shows
 *  it immediately (static-readable baseline — the gist is never hidden behind
 *  motion), and a bare content div is the fallback if the observer never fires. */
function Reveal({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const reduced = usePrefersReducedMotion();
  const [seen, setSeen] = useState(reduced);

  useEffect(() => {
    if (reduced) { setSeen(true); return; }
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => {
        if (e.isIntersecting) { setSeen(true); io.unobserve(e.target); }
      }),
      { rootMargin: "-10% 0px -10% 0px", threshold: 0.05 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [reduced]);

  // Reduced motion → no transition, no transform, full opacity from first paint.
  if (reduced) return <div ref={ref}>{children}</div>;
  return (
    <div
      ref={ref}
      style={{ transitionDelay: `${delay}ms` }}
      className={`transition-all duration-500 ease-out ${
        seen ? "translate-y-0 opacity-100" : "translate-y-4 opacity-0"
      }`}
    >
      {children}
    </div>
  );
}

/** Depth-first order from the entry facet — the reading order of the deck. */
function flattenFacets(doc: DocDetail | null): Facet[] {
  if (!doc) return [];
  const facets = doc.doc.facets ?? {};
  const out: Facet[] = [];
  const seen = new Set<string>();
  const walk = (id: string | null) => {
    if (!id || seen.has(id)) return;
    const f = facets[id];
    if (!f) return;
    seen.add(id);
    out.push(f);
    (f.children ?? []).forEach(walk);
  };
  walk(doc.doc.entry_facet_id);
  Object.keys(facets).forEach((id) => walk(id));
  return out;
}

/** The lede for a facet: the glance rung's first text block, if any. */
function firstText(blocks: Block[]): string | null {
  const t = blocks.find((b) => b.type === "text") as Extract<Block, { type: "text" }> | undefined;
  return t?.md?.trim() || null;
}

/** The L0 HOOK for a facet: the glance rung's first statement block. The glance
 *  is prose-first and carries at most one impact line — the per-recipient hook
 *  (a faithfulness-gated point selected by the projection engine) — so a
 *  statement here IS the hook. Rendered as a bold impact line, not lede text. */
function firstHook(blocks: Block[]): string | null {
  const s = blocks.find((b) => b.type === "statement") as Extract<Block, { type: "statement" }> | undefined;
  return s?.text?.trim() || null;
}

interface PersonLite { id: string; display_name?: string; name?: string }

const DEFAULT_BRAND = "okuro";

/** Layout-diversity slider stops (1-3). */
const DIVERSITY_LABELS: Record<number, string> = { 1: "Conservative", 2: "Balanced", 3: "Expressive" };
/** Model-tier options — the claude mapping is shown; provider-agnostic underneath. */
const TIER_OPTIONS: { value: ModelTier; label: string }[] = [
  { value: "fast", label: "Fast · Haiku" },
  { value: "mid", label: "Mid · Sonnet" },
  { value: "top", label: "Top · Opus" },
];
const DEPTHS: Rung[] = ["L1", "L2", "L3", "L4"];

// Per-facet width archetype (legacy board.css:151-153 + mono-hierarchy research:
// reading/wide/full chosen from the facet's CONTENT, not one global toggle).
// Data / multi-column / interactive modules earn a wider stage; a full-bleed
// standalone statement goes widest; a text-only facet stays a narrow reading
// column so prose measure stays comfortable. Width follows the block truth
// (orthogonality invariant), so it needs no backend hint and works on any deck.
const WIDE_BLOCKS = new Set<string>([
  "table", "matrix", "options", "compare", "cards", "spec", "graph", "simulator",
  "chart", "smallmultiples", "timeline", "steps", "meter", "messageproof", "risk",
  "biascheck", "decisionrecord", "assumptionledger", "tripwires", "hops",
  "frequency", "gallery", "lens",
]);
const WIDTH_CLS = { reading: "max-w-3xl", wide: "max-w-6xl", full: "max-w-7xl" } as const;
function facetWidthClass(blocks: Block[]): string {
  if (blocks.some((b) => b.type === "statement" && (b as { display?: string }).display === "standalone")) return WIDTH_CLS.full;
  if (blocks.some((b) => WIDE_BLOCKS.has(b.type))) return WIDTH_CLS.wide;
  return WIDTH_CLS.reading;
}

export function PrismViewer({ docId }: { docId: string | null }) {
  const navigate = useNavigate();
  const [doc, setDoc] = useState<DocDetail | null>(null);
  const [status, setStatus] = useState(docId ? "loading…" : "");
  const [people, setPeople] = useState<PersonLite[]>([]);
  const [brands, setBrands] = useState<BrandSummary[]>([]);
  const [theme, setTheme] = useState<BrandTheme | null>(null);
  const [logos, setLogos] = useState<BrandAsset[]>([]);
  const [brandCorner, setBrandCorner] = useState<LogoCorner>("tl");
  const [panel, setPanel] = useState<"none" | "generate" | "tailor" | "build">(docId ? "none" : "generate");
  // Reading width — persisted so a recipient's preference survives navigation.
  // Normal = a comfortable reading column; wide = board-room / dashboard width.
  const [widthMode, setWidthMode] = useState<"normal" | "wide">(
    () => (typeof localStorage !== "undefined" && localStorage.getItem("prism-width") === "wide" ? "wide" : "normal"),
  );
  const toggleWidth = useCallback(() => {
    setWidthMode((m) => {
      const next = m === "wide" ? "normal" : "wide";
      try { localStorage.setItem("prism-width", next); } catch { /* private mode */ }
      return next;
    });
  }, []);
  const deckW = widthMode === "wide" ? "max-w-6xl" : "max-w-3xl";
  // Navigation mode — the default scroll-reveal deck, or the 2D grid deck
  // (topics ↓ · rungs →). Gated + persisted, same idiom as the width toggle;
  // the scroll deck stays the reduced-motion / accessibility fallback.
  const [navMode, setNavMode] = useState<"scroll" | "grid">(
    () => (typeof localStorage !== "undefined" && localStorage.getItem("prism-nav") === "grid" ? "grid" : "scroll"),
  );
  const toggleNav = useCallback(() => {
    setNavMode((m) => {
      const next = m === "grid" ? "scroll" : "grid";
      try { localStorage.setItem("prism-nav", next); } catch { /* private mode */ }
      return next;
    });
  }, []);

  // Live "view as persona" depth — overrides the doc's baked entry_rung for
  // THIS viewer only, no regeneration (one deck, any reader lands at their
  // depth). null = the author's default. Persona picks resolve to a rung via
  // the same server-side heuristic the generator bakes in (single source).
  const [viewRung, setViewRung] = useState<Rung | null>(null);
  const viewAsPerson = useCallback((personId: string) => {
    if (!personId) { setViewRung(null); return; }
    prismApi.entryRungFor(personId).then((r) => setViewRung(r.rung)).catch(() => { /* keep current depth */ });
  }, []);

  // Generate panel state
  const [genTopic, setGenTopic] = useState("");
  const [genPerson, setGenPerson] = useState("");
  const [genBrand, setGenBrand] = useState(DEFAULT_BRAND);
  const [genTier, setGenTier] = useState<ModelTier>("mid");
  const [genDiversity, setGenDiversity] = useState<Diversity>(2);
  const [genPhase, setGenPhase] = useState("");
  const [generating, setGenerating] = useState(false);

  // Build-from-source panel: run the target pipeline (root doc → gated deck).
  const [buildSource, setBuildSource] = useState("");
  const [buildTarget, setBuildTarget] = useState("");
  const [buildTitle, setBuildTitle] = useState("");
  const [buildPhase, setBuildPhase] = useState("");
  const [building, setBuilding] = useState(false);

  // Tailor panel state (recipient re-tailoring / forced depth)
  const [tPerson, setTPerson] = useState("");
  const [tDepth, setTDepth] = useState<Rung | "">("");
  const [tReselect, setTReselect] = useState(false);
  const [tailoring, setTailoring] = useState(false);
  const [variants, setVariants] = useState<VariantSummary[]>([]);
  const [variantSource, setVariantSource] = useState<VariantSummary | null>(null);

  const load = useCallback(async (id: string) => {
    setStatus("loading…");
    try {
      const d = await prismApi.get(id);
      setDoc(d);
      setStatus(`loaded ${d.title}`);
      setPanel("none");
    } catch (e) {
      setStatus(`error: ${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    if (docId) void load(docId);
    else {
      setDoc(null);
      setPanel("generate");
    }
  }, [docId, load]);

  useEffect(() => {
    // /api/people returns { people: [...] } — unwrap it (the ad-hoc fetch here
    // predates peopleApi and was reading the object as an array, so the
    // recipient picker in Generate + Tailor was always empty).
    api<{ people: PersonLite[] }>("/api/people").then((r) => setPeople(Array.isArray(r?.people) ? r.people : [])).catch(() => setPeople([]));
    brandsApi.list().then((r) => setBrands(r.brands ?? [])).catch(() => setBrands([]));
  }, []);

  // Resolve the doc's brand into a deck theme + logo (visual tokens).
  useEffect(() => {
    const brandId = doc?.brand_id || (panel === "generate" ? genBrand : "");
    if (!brandId) {
      setTheme(null);
      setLogos([]);
      setBrandCorner("tl");
      return;
    }
    let cancelled = false;
    brandsApi.resolve(brandId).then((r) => {
      if (cancelled) return;
      setTheme(brandToTheme(r));
      setBrandCorner((r?.logo_corner as LogoCorner) || "tl");
    }).catch(() => { setTheme(null); setBrandCorner("tl"); });
    brandAssetsApi.list(brandId, "logo").then((r) => { if (!cancelled) setLogos(r.assets ?? []); }).catch(() => setLogos([]));
    return () => { cancelled = true; };
  }, [doc?.brand_id, panel, genBrand]);

  // Live-reload on cross-process edits (agent/MCP generate/edit/retailor) —
  // unless a panel is open (avoid clobbering in-flight input).
  useEffect(() => {
    const id = doc?.id;
    if (!id) return;
    let lastSeq = -1;
    const poll = setInterval(async () => {
      if (panel !== "none") return;
      try {
        const r = await prismApi.events(lastSeq < 0 ? 0 : lastSeq);
        if (lastSeq < 0) { lastSeq = r.seq; return; }
        if (r.seq !== lastSeq) {
          lastSeq = r.seq;
          if (r.events.some((ev) => ev.doc_id === id && ev.kind === "saved")) void load(id);
        }
      } catch { /* ignore */ }
    }, 3000);
    return () => clearInterval(poll);
  }, [doc?.id, panel, load]);

  const variantRoot = doc ? (doc.variant_of ?? doc.id) : null;
  useEffect(() => {
    if (!variantRoot) { setVariants([]); setVariantSource(null); return; }
    prismApi.variants(variantRoot).then((r) => { setVariantSource(r.source); setVariants(r.variants); }).catch(() => { setVariants([]); setVariantSource(null); });
  }, [variantRoot]);

  const generate = useCallback(async () => {
    const topic = genTopic.trim();
    if (!topic || generating) return;
    setGenerating(true);
    setGenPhase("queued");
    setStatus("generating…");
    try {
      const { job_id } = await prismApi.generate(topic, {
        personId: genPerson || undefined,
        brandId: genBrand || undefined,
        tier: genTier,
        diversity: genDiversity,
      });
      // Poll the job registry (~1s) for the live phase, then navigate on done.
      // Guarded so a stalled server can't spin forever (backend prunes at 900s;
      // client bails a touch sooner and surfaces it).
      for (let i = 0; i < 420; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const { jobs } = await prismApi.generateJobs();
        const job = jobs.find((j) => j.id === job_id);
        if (!job) throw new Error("generation job vanished");
        setGenPhase(job.phase);
        if (job.status === "done") {
          setPanel("none");
          setStatus(`generated ${job.title ?? ""}`);
          navigate(`/prism?id=${encodeURIComponent(job.doc_id ?? "")}`, { replace: true });
          break;
        }
        if (job.status === "error") throw new Error(job.error ?? "generation failed");
      }
    } catch (e) {
      setStatus(`generate failed: ${(e as Error).message}`);
    }
    setGenerating(false);
    setGenPhase("");
  }, [genTopic, genPerson, genBrand, genTier, genDiversity, generating, navigate]);

  const buildDeck = useCallback(async () => {
    const source = buildSource.trim();
    const target = buildTarget.trim();
    if (!source || !target || building) return;
    setBuilding(true);
    setBuildPhase("queued");
    setStatus("building…");
    try {
      const { job_id } = await prismApi.buildFromArtifact(target, {
        sourceText: source,
        title: buildTitle.trim() || undefined,
        brandId: genBrand || undefined,
      });
      // The build streams phases to the same job registry the gallery polls.
      for (let i = 0; i < 900; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const { jobs } = await prismApi.generateJobs();
        const job = jobs.find((j) => j.id === job_id);
        if (!job) throw new Error("build job vanished");
        setBuildPhase(job.phase);
        if (job.status === "done") {
          setPanel("none");
          setStatus(`built ${job.title ?? ""}`);
          navigate(`/prism?id=${encodeURIComponent(job.doc_id ?? "")}`, { replace: true });
          break;
        }
        if (job.status === "error") throw new Error(job.error ?? "build failed");
      }
    } catch (e) {
      setStatus(`build failed: ${(e as Error).message}`);
    }
    setBuilding(false);
    setBuildPhase("");
  }, [buildSource, buildTarget, buildTitle, genBrand, building, navigate]);

  const retailor = useCallback(async (asVariant: boolean) => {
    if (!doc || tailoring) return;
    if (!tPerson && !tDepth) { setStatus("pick a recipient or a depth to re-tailor"); return; }
    setTailoring(true);
    setStatus(asVariant ? "creating variant…" : "re-tailoring…");
    try {
      const d = await prismApi.retailor(doc.id, {
        personId: tPerson || undefined,
        depthOverride: (tDepth || undefined) as Rung | undefined,
        reselectModules: tReselect,
        asVariant,
      });
      if (asVariant) {
        navigate(`/prism?id=${encodeURIComponent(d.id)}`, { replace: true });
        await load(d.id);
      } else {
        await load(doc.id);
      }
      setStatus(asVariant ? `variant: ${d.title}` : `re-tailored ${d.title}`);
      setPanel("none");
    } catch (e) {
      setStatus(`re-tailor failed: ${(e as Error).message}`);
    }
    setTailoring(false);
  }, [doc, tailoring, tPerson, tDepth, tReselect, navigate, load]);

  const sections = useMemo(() => flattenFacets(doc), [doc]);

  // M3 nav — dot-rail + keyboard + active-section tracking + altitude-3 route.
  const [searchParams] = useSearchParams();
  const detailFacetId = searchParams.get("facet");
  const scrollRef = useRef<HTMLDivElement>(null);
  const sectionRefs = useRef<(HTMLElement | null)[]>([]);
  const indexRef = useRef<HTMLElement | null>(null);  // synthetic index page (page 2)
  const [activeIdx, setActiveIdx] = useState(0);
  const [peekFacet, setPeekFacet] = useState<string | null>(null);  // modal drill

  // Contextual per-slide edit: an inline prompt on a facet that runs a scoped
  // NL edit (medium/Sonnet via bridge_invoke) touching only that facet. The
  // change-feed poll reloads the deck; we also reload eagerly for snappiness.
  const [editFacet, setEditFacet] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editBusy, setEditBusy] = useState(false);
  const submitEdit = useCallback(async () => {
    if (!doc || !editFacet || !editText.trim() || editBusy) return;
    setEditBusy(true);
    setStatus("editing slide…");
    try {
      await prismApi.edit(doc.id, editText.trim(), editFacet);
      await load(doc.id);
      setStatus("slide edited");
      setEditFacet(null);
      setEditText("");
    } catch (e) {
      setStatus(`edit failed: ${(e as Error).message}`);
    }
    setEditBusy(false);
  }, [doc, editFacet, editText, editBusy, load]);

  // A/B alternatives: per-facet which arrangement is shown (-1 = A / primary,
  // >=0 = that alt index). "Keep" commits the shown one and clears the rest.
  const [altView, setAltView] = useState<Record<string, number>>({});
  const [altBusy, setAltBusy] = useState(false);
  const genAlternatives = useCallback(async () => {
    if (!doc || altBusy) return;
    setAltBusy(true);
    setStatus("designing alternatives…");
    try {
      const r = await prismApi.generateAlternatives(doc.id);
      await load(doc.id);
      setStatus(`${r.alternatives_added ?? 0} slides now have an A/B option`);
    } catch (e) {
      setStatus(`alternatives failed: ${(e as Error).message}`);
    }
    setAltBusy(false);
  }, [doc, altBusy, load]);
  const keepAlt = useCallback(async (facetId: string, rung: Rung, view: number) => {
    if (!doc) return;
    try {
      await prismApi.pickAlt(doc.id, facetId, rung, view >= 0 ? view : undefined);
      await load(doc.id);
      setAltView((v) => { const n = { ...v }; delete n[facetId]; return n; });
      setStatus("kept");
    } catch (e) {
      setStatus(`keep failed: ${(e as Error).message}`);
    }
  }, [doc, load]);

  // Modal drill: Escape closes the peek overlay.
  useEffect(() => {
    if (!peekFacet) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setPeekFacet(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [peekFacet]);

  const reducedMotion = usePrefersReducedMotion();
  const scrollBehavior: ScrollBehavior = reducedMotion ? "auto" : "smooth";
  const goToSection = useCallback((i: number) => {
    sectionRefs.current[i]?.scrollIntoView({ behavior: scrollBehavior, block: "start" });
  }, [scrollBehavior]);
  const scrollToIndex = useCallback(() => {
    indexRef.current?.scrollIntoView({ behavior: scrollBehavior, block: "start" });
  }, [scrollBehavior]);
  const openFacet = useCallback((id: string) => {
    if (doc) navigate(`/prism?id=${encodeURIComponent(doc.id)}&facet=${encodeURIComponent(id)}`);
  }, [doc, navigate]);
  const backToDeck = useCallback(() => {
    if (doc) navigate(`/prism?id=${encodeURIComponent(doc.id)}`);
  }, [doc, navigate]);
  // Prose cross-facet links: a markdown link [text](#facet:<id>) navigates within
  // prism (open the target section as its own page) instead of a dead anchor.
  const onFacetLink = useCallback((e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href") || "";
    if (href.startsWith("#facet:")) { e.preventDefault(); openFacet(href.slice(7)); }
  }, [openFacet]);

  // Track the centered section (thin band at viewport center → active dot).
  useEffect(() => {
    const root = scrollRef.current;
    if (!root || detailFacetId) return;
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => {
        if (e.isIntersecting) {
          const i = Number((e.target as HTMLElement).dataset.idx);
          if (!Number.isNaN(i)) setActiveIdx(i);
        }
      }),
      { root, rootMargin: "-45% 0px -45% 0px", threshold: 0 },
    );
    sectionRefs.current.forEach((el) => el && io.observe(el));
    return () => io.disconnect();
  }, [sections, detailFacetId]);

  // Live-present keyboard grammar: ↑/↓ or j/k step, digits jump, Home/End ends.
  useEffect(() => {
    if (detailFacetId || peekFacet) return;
    const n = sections.length;
    if (!n) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); goToSection(Math.min(activeIdx + 1, n - 1)); }
      else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); goToSection(Math.max(activeIdx - 1, 0)); }
      else if (e.key === "Home") { e.preventDefault(); goToSection(0); }
      else if (e.key === "End") { e.preventDefault(); goToSection(n - 1); }
      else if (/^[1-9]$/.test(e.key)) { const i = Number(e.key) - 1; if (i < n) { e.preventDefault(); goToSection(i); } }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sections.length, activeIdx, goToSection, detailFacetId, peekFacet]);

  const canvasStyle = theme
    ? { background: theme.background, color: theme.text, fontFamily: theme.font || undefined }
    : undefined;
  const accent = theme?.accent ?? "var(--color-accent)";

  // Sticky corner-logo module: auto-pick the dark/light variant by canvas
  // luminance; corner = per-deck override ?? brand default ?? top-left.
  const shownLogo = pickLogoForBackground(logos, isDarkBg(theme?.background) ? "dark" : "light");
  const effectiveCorner: LogoCorner = (doc?.doc?.logo_corner as LogoCorner) || brandCorner || "tl";

  // Restyle an existing deck in place (brand and/or logo corner). The change
  // persists + emits a change-feed event; the doc's reactive theme effect
  // re-themes immediately.
  const applyBrandMeta = useCallback(
    async (patch: { brand_id?: string; logo_corner?: LogoCorner | "" }) => {
      if (!doc) return;
      try {
        const updated = await prismApi.setBrand(doc.id, patch);
        setDoc(updated);
        setStatus("restyled");
      } catch (e) {
        setStatus(`restyle failed: ${(e as Error).message}`);
      }
    },
    [doc],
  );

  // Export the deck as a single self-contained HTML file (attachment download).
  // Token is appended as a query param (same trick as <img> asset loads) since a
  // programmatic navigation can't set an Authorization header.
  const exportHtml = useCallback(() => {
    if (!doc) return;
    const href = tokenizeApiSrc(`/api/prism/${encodeURIComponent(doc.id)}/export`);
    if (!href) return;
    const a = document.createElement("a");
    a.href = href;
    a.download = `${doc.title || "prism"}.html`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }, [doc]);

  const btn = "flex h-8 items-center gap-1 rounded-md border border-border px-2.5 text-sm text-fg transition-colors hover:border-accent hover:text-accent disabled:opacity-40";
  const iconBtn = "flex h-8 w-8 items-center justify-center rounded-md border border-border text-tertiary transition-colors hover:border-accent hover:text-accent disabled:opacity-40";
  const tab = (on: boolean) => `flex h-8 items-center rounded-md px-2.5 text-sm transition-colors ${on ? "bg-accent/15 text-accent" : "border border-border text-fg hover:text-accent"}`;
  const sel = "h-8 rounded-md border border-border bg-transparent px-2 text-sm text-fg transition-colors hover:border-accent focus:border-accent";
  // Status is shown only while an action is in flight or on failure — success
  // states ("loaded X", "restyled") stay out of the header to keep it clean.
  const showStatus = !!status && (/…$/.test(status) || /fail|error/i.test(status));

  return (
    <div className="flex h-full flex-col gap-2 p-3">
      {/* Header — mirrors task-detail: a breadcrumb/actions row, then a
          prominent title on its own line below. */}
      <div className="shrink-0">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate("/prism")}
            className="text-2xs uppercase tracking-wider text-tertiary transition-colors hover:text-fg-muted"
          >
            <ArrowLeft className="mr-1 inline h-3 w-3" aria-hidden="true" />
            Gallery
          </button>

          <div className="ml-auto flex items-center gap-2">
          {/* Brand styling — editable directly in view mode (no Tailor detour) */}
          {doc && (
            <div className="flex items-center gap-1.5 border-r border-border pr-2">
              <select value={doc.brand_id ?? ""} onChange={(e) => applyBrandMeta({ brand_id: e.target.value })} className={sel} title="Brand style — applies instantly">
                <option value="" className="bg-background text-fg">— brand —</option>
                {brands.map((b) => <option key={b.id} value={b.id} className="bg-background text-fg">{b.name || b.id}</option>)}
              </select>
              <select value={doc.doc?.logo_corner ?? ""} onChange={(e) => applyBrandMeta({ logo_corner: e.target.value as LogoCorner | "" })} className={sel} title="Logo corner on this deck">
                <option value="" className="bg-background text-fg">corner: {brandCorner} (default)</option>
                <option value="tl" className="bg-background text-fg">top-left</option>
                <option value="tr" className="bg-background text-fg">top-right</option>
                <option value="bl" className="bg-background text-fg">bottom-left</option>
                <option value="br" className="bg-background text-fg">bottom-right</option>
              </select>
            </div>
          )}
          {/* View as — land at a reader's depth live, no regeneration (one deck,
              many audiences). Persona → resolved rung; or force a rung directly. */}
          {doc && (
            <select
              value={viewRung ? `rung:${viewRung}` : ""}
              onChange={(e) => {
                const v = e.target.value;
                if (!v) { setViewRung(null); return; }
                if (v.startsWith("rung:")) { setViewRung(v.slice(5) as Rung); return; }
                if (v.startsWith("person:")) viewAsPerson(v.slice(7));
              }}
              className={sel}
              title="View as — land at this reader's depth (no regeneration)"
            >
              <option value="" className="bg-background text-fg">view as: author default</option>
              <optgroup label="depth" className="bg-background text-fg">
                <option value="rung:glance" className="bg-background text-fg">glance</option>
                <option value="rung:brief" className="bg-background text-fg">brief</option>
                <option value="rung:working" className="bg-background text-fg">working</option>
                <option value="rung:expert" className="bg-background text-fg">expert</option>
              </optgroup>
              {people.length > 0 && (
                <optgroup label="persona" className="bg-background text-fg">
                  {people.map((p) => <option key={p.id} value={`person:${p.id}`} className="bg-background text-fg">{p.display_name || p.name || p.id}</option>)}
                </optgroup>
              )}
            </select>
          )}
          {/* View toggles */}
          <button className={tab(navMode === "grid")} onClick={toggleNav} title={navMode === "grid" ? "Switch to scroll deck" : "Switch to 2D grid (topics ↓ · rungs →)"}>2D</button>
          <button className={iconBtn} onClick={toggleWidth} title={widthMode === "wide" ? "Normal width" : "Widescreen"} aria-label={widthMode === "wide" ? "Normal width" : "Widescreen"}>
            {widthMode === "wide" ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
          <button className={iconBtn} onClick={() => navigate("/prism-gallery")} title="All modules & layouts"><Layers size={14} /></button>
          {doc && <button className={iconBtn} onClick={exportHtml} title="Export as a self-contained HTML file"><Download size={14} /></button>}
          {doc && <button className={btn} onClick={genAlternatives} disabled={altBusy} title="Design a second version of every slide to A/B"><Sparkles size={13} />{altBusy ? "designing…" : "A/B"}</button>}
          {/* Panels */}
          <button className={tab(panel === "generate")} onClick={() => setPanel(panel === "generate" ? "none" : "generate")}>Generate ▾</button>
          <button className={tab(panel === "build")} onClick={() => setPanel(panel === "build" ? "none" : "build")} title="Build a deck from a source document via the target pipeline (distill → structure → write → arrange, critic-gated)">Build ▾</button>
          {doc && <button className={tab(panel === "tailor")} onClick={() => setPanel(panel === "tailor" ? "none" : "tailor")} title="re-tailor to a recipient or force a depth">Tailor ▾</button>}
          </div>
        </div>

        {/* Title — primary page anchor on its own line (task-detail parity) */}
        <div className="mt-3 flex items-center gap-2">
          {shownLogo && <img src={tokenizeApiSrc(shownLogo.url)} alt="" className="h-7 w-7 rounded object-contain" />}
          <h1 className="truncate text-xl font-semibold tracking-tight text-fg">{doc ? doc.title : "okuro·prism"}</h1>
          {showStatus && <span className="shrink-0 text-xs text-tertiary">{status}</span>}
        </div>
      </div>

      {/* Generate panel */}
      {panel === "generate" && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-2">
          <input
            value={genTopic}
            onChange={(e) => setGenTopic(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && generate()}
            placeholder="topic for a recipient-tailored, brand-styled doc…"
            className="min-w-[260px] flex-1 rounded border border-border bg-transparent px-2 py-1 text-sm text-fg placeholder:text-tertiary"
          />
          <select value={genPerson} onChange={(e) => setGenPerson(e.target.value)} className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg">
            <option value="" className="bg-background text-fg">— recipient (optional) —</option>
            {people.map((p) => <option key={p.id} value={p.id} className="bg-background text-fg">{p.display_name || p.name || p.id}</option>)}
          </select>
          <select value={genBrand} onChange={(e) => setGenBrand(e.target.value)} className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg">
            {brands.map((b) => <option key={b.id} value={b.id} className="bg-background text-fg">{b.name || b.id}</option>)}
          </select>
          <select value={genTier} onChange={(e) => setGenTier(e.target.value as ModelTier)} className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg" title="model tier — bigger = slower + richer">
            {TIER_OPTIONS.map((t) => <option key={t.value} value={t.value} className="bg-background text-fg">{t.label}</option>)}
          </select>
          <div className="flex items-center gap-2 rounded border border-border px-2 py-1" title="layout & module diversity — conservative → expressive">
            <span className="text-xs text-tertiary">Layout</span>
            <input
              type="range" min={1} max={3} step={1} value={genDiversity}
              onChange={(e) => setGenDiversity(Number(e.target.value) as Diversity)}
              className="w-20 accent-[var(--color-accent,#22c55e)]"
            />
            <span className="w-[86px] text-xs text-fg">{DIVERSITY_LABELS[genDiversity]}</span>
          </div>
          <button onClick={generate} disabled={!genTopic.trim() || generating} className="rounded-md bg-accent/15 px-3 py-1 text-sm text-accent hover:bg-accent/25 disabled:opacity-40">
            {generating ? `${genPhase || "generating"}…` : "Generate"}
          </button>
        </div>
      )}

      {/* Build-from-source panel — the target pipeline (root doc → gated deck). */}
      {panel === "build" && (
        <div className="flex flex-col gap-2 rounded-lg border border-border p-2">
          <textarea
            value={buildSource}
            onChange={(e) => setBuildSource(e.target.value)}
            placeholder="paste the ROOT document — a research report / analysis / session output (the deck is built ONLY from this; nothing is invented)…"
            className="min-h-[90px] w-full resize-y rounded border border-border bg-transparent px-2 py-1 text-sm text-fg placeholder:text-tertiary"
          />
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={buildTarget}
              onChange={(e) => setBuildTarget(e.target.value)}
              placeholder="target recipient — e.g. 'the board deciding whether to fund a rebuild'"
              className="min-w-[280px] flex-1 rounded border border-border bg-transparent px-2 py-1 text-sm text-fg placeholder:text-tertiary"
            />
            <input
              value={buildTitle}
              onChange={(e) => setBuildTitle(e.target.value)}
              placeholder="title (optional)"
              className="w-40 rounded border border-border bg-transparent px-2 py-1 text-sm text-fg placeholder:text-tertiary"
            />
            <select value={genBrand} onChange={(e) => setGenBrand(e.target.value)} className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg">
              {brands.map((b) => <option key={b.id} value={b.id} className="bg-background text-fg">{b.name || b.id}</option>)}
            </select>
            <button onClick={buildDeck} disabled={!buildSource.trim() || !buildTarget.trim() || building} className="rounded-md bg-accent/15 px-3 py-1 text-sm text-accent hover:bg-accent/25 disabled:opacity-40">
              {building ? `${buildPhase || "building"}…` : "Build"}
            </button>
          </div>
          <p className="text-[10px] text-tertiary">Distil → strategise → structure → write ladder → arrange modules, critic-gated. Runs ~15–25 min; progress streams above.</p>
        </div>
      )}

      {/* Tailor panel */}
      {panel === "tailor" && doc && (
        <div className="flex flex-wrap items-end gap-3 rounded-lg border border-border p-2">
          <label className="flex flex-col gap-0.5 text-[10px] uppercase tracking-wide text-tertiary">
            recipient
            <select value={tPerson} onChange={(e) => setTPerson(e.target.value)} className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg">
              <option value="" className="bg-background text-fg">— none —</option>
              {people.map((p) => <option key={p.id} value={p.id} className="bg-background text-fg">{p.display_name || p.name || p.id}</option>)}
            </select>
          </label>
          <div className="flex flex-col gap-0.5 text-[10px] uppercase tracking-wide text-tertiary">
            force depth
            <div className="flex items-center gap-1">
              {DEPTHS.map((d) => (
                <button key={d} onClick={() => setTDepth(tDepth === d ? "" : d)} className={tab(tDepth === d)}>{RUNG_LABEL[d]}</button>
              ))}
            </div>
          </div>
          {/* Re-pick modules too — not just re-word. Needs a recipient (their
              cognitive profile drives which modules fit). */}
          <button
            onClick={() => setTReselect((v) => !v)}
            disabled={!tPerson}
            className={`${tab(tReselect)} disabled:opacity-40`}
            title={tPerson ? "Also re-select which visual modules each slide uses for this recipient" : "Pick a recipient first"}
          >
            {tReselect ? "✓ " : ""}re-select modules
          </button>
          <div className="ml-auto flex items-center gap-2">
            <button onClick={() => retailor(false)} disabled={tailoring} className={btn} title="overwrite this doc's wording in place">{tailoring ? "…" : "Re-tailor in place"}</button>
            <button onClick={() => retailor(true)} disabled={tailoring} className="rounded-md bg-accent/15 px-3 py-1 text-sm text-accent hover:bg-accent/25 disabled:opacity-40" title="save a new switchable audience variant">{tailoring ? "creating…" : "Create variant"}</button>
          </div>
        </div>
      )}

      {/* Variant tabs */}
      {doc && (variants.length > 0 || doc.variant_of) && (
        <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-border px-2 py-1.5">
          <span className="text-[10px] uppercase tracking-wide text-tertiary">variants</span>
          {variantSource && (
            <button onClick={() => navigate(`/prism?id=${encodeURIComponent(variantSource.id)}`)} className={tab(doc.id === variantSource.id)}>source</button>
          )}
          {variants.map((v) => (
            <button key={v.id} onClick={() => navigate(`/prism?id=${encodeURIComponent(v.id)}`)} className={tab(doc.id === v.id)} title={v.title}>
              {v.title}
            </button>
          ))}
        </div>
      )}

      {/* Deck stage — a relative wrapper so the sticky corner-logo module can
          overlay whichever surface renders (detail / grid / scroll deck). */}
      <div className="relative flex min-h-0 flex-1 flex-col">
      {/* Scroll-reveal deck (default) · single-facet detail page (?facet=) */}
      {!doc ? (
        panel !== "generate" && <div className="flex flex-1 items-center justify-center rounded-xl border border-border text-sm text-tertiary">{status || "pick a doc from the gallery, or generate one"}</div>
      ) : detailFacetId ? (
        <DetailFacet doc={doc} facetId={detailFacetId} accent={accent} canvasStyle={canvasStyle} onBack={backToDeck} onOpenFacet={openFacet} widthMode={widthMode} />
      ) : navMode === "grid" ? (
        <GridDeck facets={sections} title={doc.title} tagline={doc.doc?.tagline} accent={accent} brandId={doc.brand_id ?? undefined} canvasStyle={canvasStyle} onOpenFacet={openFacet} widthMode={widthMode} meta={{ brand: brands.find((b) => b.id === doc.brand_id)?.name || doc.brand_id || undefined, recipient: undefined, date: doc.updated_at }} onPickAlt={(fid, rung, ai) => keepAlt(fid, rung, ai ?? -1)} />
      ) : (
        <div className="relative min-h-0 flex-1">
          <div ref={scrollRef} onClick={onFacetLink} className="h-full snap-y snap-mandatory overflow-y-auto rounded-xl border border-border" style={canvasStyle}>
            {sections.map((f, idx) => {
              const glanceBlocks = rungToBlocks(f.rungs?.L1);
              const lede = firstText(glanceBlocks);
              const hook = firstHook(glanceBlocks);
              // entry_rung (set by resolve_depth_default) is the recipient's DEFAULT
              // reading depth. It controls whether the deeper drill is pre-EXPANDED
              // (deepDefault, used on the <details> below) — NOT which rung is "main".
              // Making entry_rung the sole main rung orphaned the intermediate rungs'
              // blocks (e.g. the brief rung's statement/cards when entry=working), so
              // main stays brief-else-working: nothing between glance and the ceiling
              // is dropped, and a deep reader just gets the drill open by default.
              // viewRung (live "view as persona") overrides the doc's baked
              // entry_rung for this viewer only — one deck, any reader's depth.
              const entryRung = viewRung ?? (doc.doc?.entry_rung as Rung | undefined);
              const deepDefault = entryRung === "L3" || entryRung === "L4";
              const mainRung: Rung = rungToBlocks(f.rungs?.L2).length ? "L2" : "L3";
              const mainRungContent = f.rungs?.[mainRung];
              const alts = mainRungContent?.alts;
              const altV = altView[f.id] ?? -1;
              const activeAlt = altV >= 0 && alts ? alts[altV] : undefined;
              const mainBlocks = activeAlt
                ? rungToBlocks({ ...mainRungContent, blocks: activeAlt.blocks })
                : rungToBlocks(mainRungContent);
              const mainLayout = activeAlt ? activeAlt.layout : mainRungContent?.layout;
              const mainKeys = new Set(mainBlocks.map((b) => JSON.stringify(b)));
              const deeperBlocks = [...rungToBlocks(f.rungs?.L3), ...rungToBlocks(f.rungs?.L4)]
                .filter((b) => !mainKeys.has(JSON.stringify(b)));
              // Per-facet width from content; the global "wide" toggle forces wide.
              const facetW = widthMode === "wide" ? "max-w-6xl" : facetWidthClass(mainBlocks);
              return (
                <Fragment key={f.id}>
                {/* Index page — page 2, right after the hero: the matrix map.
                    A column click (working/expert) opens that facet's detail;
                    the row (overview) scrolls to the section. */}
                {idx === 1 && sections.length > 1 && (
                  <section
                    ref={indexRef}
                    className={`relative mx-auto flex min-h-[90vh] snap-start ${deckW} flex-col justify-center px-8 py-[8vh]`}
                  >
                    <Reveal delay={0}>
                      <DeckIndex
                        facets={sections}
                        title={doc.title}
                        accent={accent}
                        meta={{ brand: brands.find((b) => b.id === doc.brand_id)?.name || doc.brand_id || undefined, date: doc.updated_at }}
                        onSelect={(fid, col) => {
                          const si = sections.findIndex((f) => f.id === fid);
                          if (col > 0) openFacet(fid);
                          else if (si >= 0) goToSection(si);
                        }}
                      />
                    </Reveal>
                  </section>
                )}
                <section
                  ref={(el) => { sectionRefs.current[idx] = el; }}
                  data-idx={idx}
                  className={`relative mx-auto flex min-h-[90vh] snap-start ${facetW} flex-col justify-center gap-4 px-8 py-[8vh]`}
                >
                  <div className="absolute right-4 top-4 z-[1] flex gap-1">
                    {sections.length > 1 && (
                      <button
                        onClick={scrollToIndex}
                        className="rounded border border-current/15 px-2 py-0.5 text-[11px] opacity-70 transition-opacity hover:opacity-100"
                        title="Jump to the index"
                      >
                        Index
                      </button>
                    )}
                    <button
                      onClick={() => setPeekFacet(f.id)}
                      className="rounded border border-current/15 px-2 py-0.5 text-[11px] opacity-70 transition-opacity hover:opacity-100"
                      title="Peek at this section's full detail (Esc to close)"
                    >
                      Peek ⤢
                    </button>
                    <button
                      onClick={() => openFacet(f.id)}
                      className="rounded border border-current/15 px-2 py-0.5 text-[11px] opacity-70 transition-opacity hover:opacity-100"
                      title="Open this section as its own page"
                    >
                      Open ↗
                    </button>
                    <button
                      onClick={() => { setEditFacet((cur) => (cur === f.id ? null : f.id)); setEditText(""); }}
                      className="flex items-center gap-1 rounded border border-current/15 px-2 py-0.5 text-[11px] opacity-70 transition-opacity hover:opacity-100"
                      style={editFacet === f.id ? { color: accent, opacity: 1 } : undefined}
                      title="Edit this slide with AI"
                    >
                      <Sparkles size={11} /> Edit
                    </button>
                    {alts && alts.length > 0 && (
                      <span className="flex items-center gap-1 rounded border border-current/15 px-1.5 py-0.5 text-[11px]">
                        <button onClick={() => setAltView((v) => ({ ...v, [f.id]: -1 }))} className={altV < 0 ? "font-bold" : "opacity-70 hover:opacity-100"} style={altV < 0 ? { color: accent } : undefined} title="Version A">A</button>
                        <span className="opacity-30">/</span>
                        <button onClick={() => setAltView((v) => ({ ...v, [f.id]: 0 }))} className={altV >= 0 ? "font-bold" : "opacity-70 hover:opacity-100"} style={altV >= 0 ? { color: accent } : undefined} title="Version B — an alternative design">B</button>
                        <button onClick={() => keepAlt(f.id, mainRung, altV)} className="ml-1 border-l border-current/15 pl-1.5 opacity-60 hover:opacity-100" title="Keep the shown version and drop the other">Keep</button>
                      </span>
                    )}
                  </div>
                  {editFacet === f.id && (
                    <div className="absolute inset-x-8 top-14 z-[2] flex items-center gap-2 rounded-lg border border-current/20 bg-black/30 p-2 backdrop-blur">
                      <Sparkles size={14} style={{ color: accent }} className="shrink-0" />
                      <input
                        autoFocus
                        value={editText}
                        onChange={(e) => setEditText(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") submitEdit();
                          if (e.key === "Escape") { setEditFacet(null); setEditText(""); }
                        }}
                        disabled={editBusy}
                        placeholder="Edit this slide — e.g. 'tighten the glance to one line', 'add a risk callout'…"
                        className="min-w-0 flex-1 rounded border border-current/15 bg-transparent px-2 py-1 text-sm outline-none placeholder:opacity-40 focus:border-current/40"
                      />
                      <button
                        onClick={submitEdit}
                        disabled={editBusy || !editText.trim()}
                        className="shrink-0 rounded-md px-3 py-1 text-sm disabled:opacity-40"
                        style={{ background: `${accent}26`, color: accent }}
                      >
                        {editBusy ? "editing…" : "Edit ▸"}
                      </button>
                    </div>
                  )}
                  <Reveal delay={0}>
                    <p className="text-xs font-medium uppercase tracking-[0.25em]" style={{ color: accent }}>
                      {String(idx + 1).padStart(2, "0")} · {f.kind}
                    </p>
                  </Reveal>
                  <Reveal delay={60}>
                    <h2 className="text-4xl font-bold uppercase leading-[1.05] tracking-[-0.03em] md:text-6xl">{f.headline || f.title}</h2>
                  </Reveal>
                  {hook && (
                    <Reveal delay={90}>
                      <p className="max-w-[24ch] text-2xl font-bold leading-[1.1] tracking-tight md:text-4xl" style={{ color: accent }}>
                        {hook}
                      </p>
                    </Reveal>
                  )}
                  {lede && (
                    <Reveal delay={120}>
                      <p className="max-w-[62ch] text-xl opacity-70 md:text-2xl">{lede}</p>
                    </Reveal>
                  )}
                  {mainBlocks.length > 0 && (
                    <Reveal delay={160}>
                      <BlockGrid blocks={mainBlocks} layout={mainLayout} accent={theme?.accent} />
                    </Reveal>
                  )}
                  {deeperBlocks.length > 0 && (
                    <Reveal delay={240}>
                      <details open={deepDefault} className="group mt-1 rounded-lg border border-current/15 open:pb-2">
                        <summary className="cursor-pointer list-none px-3 py-2 text-xs uppercase tracking-wide opacity-60 hover:opacity-100">
                          Go deeper ↓
                        </summary>
                        <div className="space-y-3 px-3">
                          {deeperBlocks.map((b, i) => <BlockView key={i} block={b} accent={theme?.accent} />)}
                        </div>
                      </details>
                    </Reveal>
                  )}
                </section>
                </Fragment>
              );
            })}
          </div>

          {/* Dot-nav rail — doubles as TOC (hover reveals the section title).
              Keyboard: ↑/↓ or j/k step · digits 1–9 jump · Home/End. */}
          {sections.length > 1 && (
            <nav className="absolute right-2 top-1/2 z-10 flex -translate-y-1/2 flex-col items-end gap-2">
              {sections.map((f, i) => (
                <button key={f.id} onClick={() => goToSection(i)} title={f.title} aria-label={f.title} className="group flex items-center justify-end gap-1.5">
                  <span className="max-w-0 overflow-hidden whitespace-nowrap rounded bg-black/70 text-[11px] text-white opacity-0 transition-all group-hover:max-w-[180px] group-hover:px-2 group-hover:py-0.5 group-hover:opacity-100">{f.title}</span>
                  <span
                    className={`h-2 w-2 rounded-full border transition-transform ${i === activeIdx ? "scale-125 border-transparent" : "border-current/40"}`}
                    style={i === activeIdx ? { background: accent } : undefined}
                  />
                </button>
              ))}
            </nav>
          )}
          {sections.length > 1 && (
            <div className="pointer-events-none absolute bottom-3 left-3 z-10 rounded-md border border-current/15 bg-black/40 px-2 py-1 text-[10px] opacity-70">
              ↑↓ / j k step · 1–9 jump · Home / End
            </div>
          )}
        </div>
      )}

      {/* Sticky corner-logo module — pinned to the chosen corner of the deck
          stage, stays put across scroll + 2D nav. Auto-picks dark/light by
          canvas luminance. pointer-events-none so it never blocks the deck. */}
      {doc && shownLogo && (
        <img
          src={tokenizeApiSrc(shownLogo.url)}
          alt=""
          className={`pointer-events-none absolute z-20 h-9 w-auto max-w-[128px] object-contain drop-shadow ${CORNER_POS[effectiveCorner]}`}
        />
      )}
      </div>

      {/* Modal drill — peek a section's full detail without leaving the deck. */}
      {peekFacet && doc && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setPeekFacet(null)}>
          <div
            className="flex h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-border shadow-2xl"
            style={canvasStyle}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 border-b border-border px-4 py-2">
              <span className="text-xs uppercase tracking-wide opacity-60">Peek</span>
              <button onClick={() => setPeekFacet(null)} className="ml-auto rounded px-2 py-0.5 text-sm opacity-60 transition-opacity hover:opacity-100">✕ close</button>
            </div>
            <DetailFacet doc={doc} facetId={peekFacet} accent={accent} canvasStyle={canvasStyle} onBack={() => setPeekFacet(null)} onOpenFacet={(id) => setPeekFacet(id)} />
          </div>
        </div>
      )}
    </div>
  );
}
