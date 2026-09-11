// <!-- AGENT_HEADER
// role: code
// purpose: Read-only, offline render of a Prism deck for the single-file HTML
//   export — at FULL PARITY with the live viewer: scroll deck, 2D grid nav, and
//   the altitude-3 detail deep-dive (open + peek). Reuses the live renderers
//   (BlockGrid/BlockView), GridDeck, and the shared DetailFacet verbatim so
//   nothing drifts; only the nav shell is reimplemented WITHOUT react-router or
//   any API call. Theme + logo are pre-baked into the payload (logo urls are
//   already data: URIs), so nothing is fetched.
// index: types | helpers | PrismExportDeck
// AGENT_HEADER_END -->
import { Maximize2, Minimize2 } from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import { BlockGrid, BlockView } from "@/components/prism/blocks";
import { DeckIndex } from "@/components/prism/deck-index";
import { DetailFacet } from "@/components/prism/detail-facet";
import { GridDeck } from "@/components/prism/grid-deck";
import {
  brandToTheme,
  pickLogoForBackground,
  rungToBlocks,
  type BrandAsset,
  type DocDetail,
  type Facet,
  type LogoCorner,
  type Rung,
} from "@/lib/prism-api";

/** Deck payload the backend injects into window.__PRISM_EXPORT__. `brand` is the
 *  raw resolve_brand() dict (or null); `logos` carry data: URIs so the file is
 *  self-contained; `corner` is the resolved logo corner. */
export interface ExportPayload {
  doc: DocDetail;
  brand: unknown | null;
  logos: BrandAsset[];
  corner: LogoCorner;
}

// 80px inset from the pinned corner (Tailwind 20 = 5rem), mirrors the viewer.
const CORNER_POS: Record<LogoCorner, string> = {
  tl: "top-20 left-20",
  tr: "top-20 right-20",
  bl: "bottom-20 left-20",
  br: "bottom-20 right-20",
};

/** Perceived-luminance test — defaults to dark (the prism canvas default) so an
 *  unknown background picks a light logo. Copied from prism-viewer (local there). */
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
  if (!m) return true;
  const p = (m[1] ?? "").split(",").map((x) => parseFloat(x) || 0);
  return luma(p[0] ?? 0, p[1] ?? 0, p[2] ?? 0);
}

/** Depth-first reading order from the entry facet — mirrors the viewer. */
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

function firstText(blocks: ReturnType<typeof rungToBlocks>): string | null {
  const t = blocks.find((b) => b.type === "text") as { md?: string } | undefined;
  return t?.md?.trim() || null;
}

/** One-shot scroll reveal (fades + lifts in, never re-hides). */
function Reveal({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => { if (e.isIntersecting) { setSeen(true); io.unobserve(e.target); } }),
      { rootMargin: "-10% 0px -10% 0px", threshold: 0.05 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return (
    <div
      ref={ref}
      style={{ transitionDelay: `${delay}ms` }}
      className={`transition-all duration-500 ease-out ${seen ? "translate-y-0 opacity-100" : "translate-y-4 opacity-0"}`}
    >
      {children}
    </div>
  );
}

export function PrismExportDeck({ payload }: { payload: ExportPayload }) {
  const { doc } = payload;
  const theme = useMemo(() => brandToTheme(payload.brand), [payload.brand]);
  const sections = useMemo(() => flattenFacets(doc), [doc]);
  const deckMeta = useMemo(
    () => ({ brand: (payload.brand as { name?: string } | null)?.name, date: doc.updated_at || doc.created_at }),
    [payload.brand, doc.updated_at, doc.created_at],
  );

  const canvasStyle: React.CSSProperties | undefined = theme
    ? { background: theme.background, color: theme.text, fontFamily: theme.font || undefined }
    : undefined;
  const accent = theme?.accent ?? "#8ff0a4";
  const logo = pickLogoForBackground(payload.logos, isDarkBg(theme?.background) ? "dark" : "light");

  // Nav state (all local — no router). Scroll deck ↔ 2D grid; a detail facet is
  // the altitude-3 page (open ↗); peek is the same in a modal overlay.
  const [navMode, setNavMode] = useState<"scroll" | "grid">("scroll");
  const [widthMode, setWidthMode] = useState<"normal" | "wide">("normal");
  const [detailId, setDetailId] = useState<string | null>(null);
  const [peekFacet, setPeekFacet] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const sectionRefs = useRef<(HTMLElement | null)[]>([]);
  const indexRef = useRef<HTMLElement | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);

  const goToSection = (i: number) => sectionRefs.current[i]?.scrollIntoView({ behavior: "smooth", block: "start" });
  const scrollToIndex = () => indexRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });

  // Cross-facet prose links [text](#facet:<id>) → open that section as a page.
  const idToIdx = useMemo(() => {
    const m = new Map<string, number>();
    sections.forEach((f, i) => m.set(f.id, i));
    return m;
  }, [sections]);
  const onFacetLink = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href") || "";
    if (href.startsWith("#facet:")) {
      e.preventDefault();
      if (idToIdx.has(href.slice(7))) setDetailId(href.slice(7));
    }
  };

  // Escape closes the peek overlay.
  useEffect(() => {
    if (!peekFacet) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setPeekFacet(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [peekFacet]);

  // Active-section tracking for the dot rail (scroll deck only).
  useEffect(() => {
    const root = scrollRef.current;
    if (!root || detailId || navMode !== "scroll") return;
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
  }, [sections, detailId, navMode]);

  // Live-present keyboard grammar: ↑/↓ or j/k step, digits jump, Home/End ends.
  useEffect(() => {
    if (detailId || peekFacet || navMode !== "scroll") return;
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
  }, [sections.length, activeIdx, detailId, peekFacet, navMode]);

  const deckW = widthMode === "wide" ? "max-w-6xl" : "max-w-3xl";
  const effectiveCorner: LogoCorner = (doc.doc?.logo_corner as LogoCorner) || payload.corner || "tl";

  const ctrlBtn = "flex h-7 items-center gap-1 rounded-md border border-current/20 px-2 text-[11px] opacity-60 transition-opacity hover:opacity-100";
  const multi = sections.length > 1;

  return (
    <div className="fixed inset-0 flex flex-col" style={canvasStyle}>
      {/* Minimal floating control bar — Scroll ↔ 2D grid + width. Hidden while a
          detail page is open (it has its own Back). */}
      {!detailId && (
        <div className="pointer-events-auto absolute left-3 top-3 z-30 flex items-center gap-1.5">
          <button className={ctrlBtn} onClick={() => setNavMode((m) => (m === "grid" ? "scroll" : "grid"))} title={navMode === "grid" ? "Scroll deck" : "2D grid (topics ↓ · rungs →)"}>
            {navMode === "grid" ? "Scroll" : "2D"}
          </button>
          <button className={ctrlBtn} onClick={() => setWidthMode((m) => (m === "wide" ? "normal" : "wide"))} title={widthMode === "wide" ? "Normal width" : "Widescreen"}>
            {widthMode === "wide" ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
          </button>
        </div>
      )}

      {detailId ? (
        <div className="flex min-h-0 flex-1 flex-col p-3">
          <DetailFacet
            doc={doc}
            facetId={detailId}
            accent={accent}
            canvasStyle={canvasStyle}
            onBack={() => setDetailId(null)}
            onOpenFacet={(id) => setDetailId(id)}
            widthMode={widthMode}
          />
        </div>
      ) : navMode === "grid" ? (
        <div className="flex min-h-0 flex-1 flex-col p-3">
          <GridDeck facets={sections} title={doc.title} tagline={doc.doc?.tagline} accent={accent} brandId={doc.doc?.brand_id} canvasStyle={canvasStyle} onOpenFacet={(id) => setDetailId(id)} widthMode={widthMode} meta={deckMeta} />
        </div>
      ) : (
        <div ref={scrollRef} onClick={onFacetLink} className="relative h-full overflow-y-auto">
          {sections.map((f, idx) => {
            const lede = firstText(rungToBlocks(f.rungs?.L1));
            const mainRung: Rung = rungToBlocks(f.rungs?.L2).length ? "L2" : "L3";
            const mainBlocks = rungToBlocks(f.rungs?.[mainRung]);
            const mainLayout = f.rungs?.[mainRung]?.layout;
            const mainKeys = new Set(mainBlocks.map((b) => JSON.stringify(b)));
            const deeperBlocks = [...rungToBlocks(f.rungs?.L3), ...rungToBlocks(f.rungs?.L4)]
              .filter((b) => !mainKeys.has(JSON.stringify(b)));
            return (
              <Fragment key={f.id}>
                {/* Index page — page 2, right after the hero: the matrix map.
                    Column click (working/expert) opens that facet's detail; the
                    row (overview) scrolls to the section. */}
                {idx === 1 && multi && (
                  <section ref={indexRef} className={`relative mx-auto flex min-h-[80vh] ${deckW} flex-col justify-center px-8 py-[8vh]`}>
                    <Reveal delay={0}>
                      <DeckIndex
                        facets={sections}
                        title={doc.title}
                        accent={accent}
                        meta={deckMeta}
                        onSelect={(fid, col) => {
                          const si = idToIdx.get(fid);
                          if (col > 0) setDetailId(fid);
                          else if (si != null) goToSection(si);
                        }}
                      />
                    </Reveal>
                  </section>
                )}
                <section
                  ref={(el) => { sectionRefs.current[idx] = el; }}
                  data-idx={idx}
                  className={`relative mx-auto flex min-h-[80vh] ${deckW} flex-col justify-center gap-4 px-8 py-[8vh]`}
                >
                  <div className="absolute right-4 top-4 z-[1] flex gap-1">
                    {multi && (
                      <button onClick={scrollToIndex} className="rounded border border-current/15 px-2 py-0.5 text-[11px] opacity-40 transition-opacity hover:opacity-100" title="Jump to the index">Index</button>
                    )}
                    <button onClick={() => setPeekFacet(f.id)} className="rounded border border-current/15 px-2 py-0.5 text-[11px] opacity-40 transition-opacity hover:opacity-100" title="Peek at this section's full detail (Esc to close)">Peek ⤢</button>
                    <button onClick={() => setDetailId(f.id)} className="rounded border border-current/15 px-2 py-0.5 text-[11px] opacity-40 transition-opacity hover:opacity-100" title="Open this section as its own page">Open ↗</button>
                  </div>
                  <Reveal delay={0}>
                    <p className="text-xs font-medium uppercase tracking-[0.25em]" style={{ color: accent }}>{String(idx + 1).padStart(2, "0")} · {f.kind}</p>
                  </Reveal>
                  <Reveal delay={60}><h2 className="text-3xl font-semibold leading-tight md:text-4xl">{f.headline || f.title}</h2></Reveal>
                  {lede && <Reveal delay={120}><p className="text-lg opacity-70">{lede}</p></Reveal>}
                  {mainBlocks.length > 0 && <Reveal delay={160}><BlockGrid blocks={mainBlocks} layout={mainLayout} accent={accent} /></Reveal>}
                  {deeperBlocks.length > 0 && (
                    <Reveal delay={240}>
                      <details className="group mt-1 rounded-lg border border-current/15 open:pb-2">
                        <summary className="cursor-pointer list-none px-3 py-2 text-xs uppercase tracking-wide opacity-60 hover:opacity-100">Go deeper ↓</summary>
                        <div className="space-y-3 px-3 opacity-90">
                          {deeperBlocks.map((b, i) => <BlockView key={i} block={b} accent={accent} />)}
                        </div>
                      </details>
                    </Reveal>
                  )}
                </section>
              </Fragment>
            );
          })}

          {/* Dot-nav rail — doubles as TOC (hover reveals the section title). */}
          {multi && (
            <nav className="fixed right-2 top-1/2 z-10 flex -translate-y-1/2 flex-col items-end gap-2">
              {sections.map((f, i) => (
                <button key={f.id} onClick={() => goToSection(i)} title={f.title} aria-label={f.title} className="group flex items-center justify-end gap-1.5">
                  <span className="max-w-0 overflow-hidden whitespace-nowrap rounded bg-black/70 text-[11px] text-white opacity-0 transition-all group-hover:max-w-[180px] group-hover:px-2 group-hover:py-0.5 group-hover:opacity-100">{f.title}</span>
                  <span className={`h-2 w-2 rounded-full border transition-transform ${i === activeIdx ? "scale-125 border-transparent" : "border-current/40"}`} style={i === activeIdx ? { background: accent } : undefined} />
                </button>
              ))}
            </nav>
          )}
          {multi && (
            <div className="pointer-events-none fixed bottom-3 left-3 z-10 rounded-md border border-current/15 bg-black/40 px-2 py-1 text-[10px] opacity-40">
              ↑↓ / j k step · 1–9 jump · Home / End
            </div>
          )}
        </div>
      )}

      {/* Sticky corner logo — data: URI, auto dark/light by canvas luminance. */}
      {logo && (
        <img src={logo.url} alt="" className={`pointer-events-none fixed z-20 h-9 w-auto max-w-[128px] object-contain drop-shadow ${CORNER_POS[effectiveCorner]}`} />
      )}

      {/* Peek — a section's full detail in a modal, without leaving the deck. */}
      {peekFacet && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setPeekFacet(null)}>
          <div className="flex h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-border shadow-2xl" style={canvasStyle} onClick={(e) => e.stopPropagation()}>
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
