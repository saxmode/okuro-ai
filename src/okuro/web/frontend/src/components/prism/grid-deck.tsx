// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism 2D deck — the gated "grid" navigation mode (R3 spec).
//   Topics run VERTICALLY (facet order, depth-first); each topic's rungs become
//   HORIZONTAL detail slides (col0 = glance+brief landing, col1 = working, col2
//   = expert). Down = next topic, Right = deeper rung, and linear advance WRAPS
//   from a topic's last rung to the next topic. Keyboard + debounced wheel +
//   a 2D minimap; Enter opens the focused DetailFacet. The scroll-reveal deck
//   stays the default/fallback — this renders only when prism-nav=grid.
//   Transition is a full PUSH: the outgoing slide slides entirely off-screen
//   along the travel axis while the incoming slide slides in from the opposite
//   edge (vertical for topic moves ↓/↑, horizontal for rung moves →/←).
// index: buildTopics | GridDeck
// AGENT_HEADER_END -->
import { ChevronDown, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, type Variants } from "motion/react";

import { BlockGrid } from "@/components/prism/blocks";
import { DeckIndex, type DeckMeta } from "@/components/prism/deck-index";
import { RUNG_LABEL } from "@/components/prism/detail-facet";
import { TemplateSlide, brandStyle, hasTemplate, type BoundTemplate } from "@/components/prism/templates/slide-templates";
import { MarkdownContent } from "@/components/ui/markdown-content";
import {
  RUNGS, coverLine, docviewMarkdown, rungToBlocks,
  type Block, type Facet, type Layout, type Rung, type RungContent, type SlideType,
} from "@/lib/prism-api";
import { parseApiDate } from "@/lib/format";

// A slide is one level (L1 cover → L4 doc-view) of a topic. It may carry an
// alternative (B) arrangement — the flattened alt blocks + its layout + which
// rung's alts to commit when "Keep" is pressed.
type Slide = {
  level: Rung; label: string; slideType: SlideType; blocks: Block[]; layout?: Layout;
  template?: BoundTemplate;
  altBlocks?: Block[]; altLayout?: Layout; altRung?: Rung;
};
// A content topic wraps a facet; the synthetic HERO (row 0, the deck cover) and
// INDEX (row 1, the agenda matrix) topics have no facet.
type Topic = { facet?: Facet; slides: Slide[]; isIndex?: boolean; isHero?: boolean };
/** Travel direction feeding the push variants: which axis moved and its sign. */
type Dir = { axis: "v" | "h"; sign: number };

// The flattened B arrangement for a rung (same body/media/callouts, alt modules).
function _altFor(rc: RungContent | undefined): { blocks: Block[]; layout?: Layout } | null {
  const alt = rc?.alts?.[0];
  if (!alt) return null;
  return { blocks: rungToBlocks({ ...rc, blocks: alt.blocks }), layout: alt.layout };
}

/** The slide type a level renders as when the backend didn't stamp one (mirrors
 *  okuro.prism.slides.slide_type_for): L1→cover (section for a parent), L4→
 *  doc-view, else content. */
function _slideType(level: Rung, facet: Facet): SlideType {
  if (level === "L1") return (facet.children ?? []).length ? "section" : "cover";
  if (level === "L4") return "doc-view";
  return "content";
}

/** Fold each facet into its level slides L1→L4: L1 is ALWAYS present (the cover,
 *  derived from the headline even when it has no blocks); L2/L3/L4 appear only
 *  when they carry content. The inverse-density slide model — biggest-type cover
 *  first, full document last. */
function buildTopics(facets: Facet[]): Topic[] {
  return facets.map((f) => {
    const slides: Slide[] = [];
    for (const level of RUNGS) {
      const rc = f.rungs?.[level];
      const blocks = rungToBlocks(rc);
      if (level !== "L1" && blocks.length === 0) continue; // L1 cover always shows
      const a = _altFor(rc);
      slides.push({
        level, label: RUNG_LABEL[level], slideType: rc?.slide_type ?? _slideType(level, f),
        blocks, layout: rc?.layout,
        template: hasTemplate(rc?.template) ? rc!.template : undefined,
        altBlocks: a?.blocks, altLayout: a?.layout, altRung: a ? level : undefined,
      });
    }
    // A facet with no rungs at all still gets its cover slide.
    if (!slides.length) slides.push({ level: "L1", label: RUNG_LABEL.L1, slideType: "cover", blocks: [] });
    return { facet: f, slides };
  });
}

// Full-dimension push: the incoming slide starts one screen away on the travel
// axis and slides in sharp + opaque; the outgoing slide slides the other way
// while it BLURS and FADES OUT — "the screen dissolves away as the next moves in".
const PUSH: Variants = {
  enter: (d: Dir) => ({
    x: d.axis === "h" ? (d.sign > 0 ? "100%" : "-100%") : 0,
    y: d.axis === "v" ? (d.sign > 0 ? "100%" : "-100%") : 0,
    opacity: 1,
    filter: "blur(0px)",
  }),
  center: { x: 0, y: 0, opacity: 1, filter: "blur(0px)" },
  exit: (d: Dir) => ({
    x: d.axis === "h" ? (d.sign > 0 ? "-100%" : "100%") : 0,
    y: d.axis === "v" ? (d.sign > 0 ? "-100%" : "100%") : 0,
    opacity: 0,
    filter: "blur(8px)",
  }),
};

// Transition duration = design-stack motion speed 3 (medium) = 650ms.
const PUSH_SECONDS = 0.65;

function fmtHeroDate(s?: string): string | null {
  if (!s) return null;
  const d = parseApiDate(s.trim());
  return isNaN(d.getTime()) ? null : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** HERO — the deck's title slide (deck-level cover, distinct from the INDEX). The
 *  title at the largest type, the takeaway as a subtitle, then the brand/recipient/
 *  date + topic count. This is the deck's entry: one deck, one loud first frame. */
function HeroSlide({ title, tagline, accent, meta, count }: {
  title: string; tagline?: string; accent: string; meta?: DeckMeta; count: number;
}) {
  const date = fmtHeroDate(meta?.date);
  const bits = [meta?.brand, meta?.recipient ? `for ${meta.recipient}` : null, date,
    `${count} ${count === 1 ? "topic" : "topics"}`].filter(Boolean);
  return (
    <div className="flex min-h-[70vh] flex-col justify-center gap-6">
      <p className="text-xs font-semibold uppercase tracking-[0.3em]" style={{ color: accent }}>okuro · prism</p>
      <h1 className="text-5xl font-bold leading-[1.02] tracking-tight sm:text-6xl md:text-7xl" style={{ maxWidth: "18ch" }}>{title}</h1>
      {tagline && <p className="text-xl opacity-70 sm:text-2xl" style={{ maxWidth: "44ch" }}>{tagline}</p>}
      {bits.length > 0 && (
        <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm opacity-55">
          {bits.map((b, i) => (
            <span key={i} className="flex items-center gap-2">{i > 0 && <span className="opacity-40">·</span>}{b}</span>
          ))}
        </p>
      )}
      <p className="mt-4 text-xs uppercase tracking-wide opacity-40">↓ index · → deeper · Enter to begin</p>
    </div>
  );
}

/** L1 COVER — one idea as the whole frame (the highest-density-of-type, lowest-
 *  density-of-content level). The facet's assertion (cover line) at huge type; the
 *  short nav title as a kicker. A SECTION (a parent chapter) reads the same but
 *  flags that sub-topics follow. Extractive — coverLine reuses existing text. */
function CoverSlide({ facet, accent, section }: { facet: Facet; accent: string; section?: boolean }) {
  const line = coverLine(facet);
  const title = (facet.title || "").trim();
  // The outer topic header already shows facet.kind, so the cover kicker only adds
  // signal when it's distinct: "Section" for a parent, or the short nav title when
  // it differs from the cover line. Otherwise omit it (no "TOPIC / TOPIC" echo).
  const kicker = section ? "Section" : (title && title !== line ? title : null);
  return (
    <div className="flex min-h-[62vh] flex-col justify-center gap-5">
      {kicker && <p className="text-sm font-semibold uppercase tracking-[0.25em]" style={{ color: accent }}>{kicker}</p>}
      <h1 className="text-4xl font-bold leading-[1.05] tracking-tight sm:text-5xl md:text-6xl" style={{ maxWidth: "20ch" }}>
        {line || title}
      </h1>
      {section && (facet.children ?? []).length > 0 && (
        <p className="text-sm uppercase tracking-wide opacity-45">{(facet.children ?? []).length} sub-topic{(facet.children ?? []).length === 1 ? "" : "s"} follow →</p>
      )}
    </div>
  );
}

/** L4 DOC-VIEW — the topic's FULL documentation rendered inside the deck: the whole
 *  prose ladder as one long-form column, then the level's densest modules. Not a
 *  link-out; the reader stays in the deck. The frame scrolls (its motion wrapper
 *  is overflow-y-auto). */
function DocViewSlide({ facet, blocks, layout, accent }: {
  facet: Facet; blocks: Block[]; layout?: Layout; accent: string;
}) {
  const md = docviewMarkdown(facet);
  return (
    <div className="flex flex-col gap-5 py-2">
      <div className="flex items-baseline gap-3">
        <p className="text-xs font-semibold uppercase tracking-[0.25em]" style={{ color: accent }}>Full documentation</p>
      </div>
      <h1 className="text-3xl font-semibold leading-tight sm:text-4xl" style={{ maxWidth: "26ch" }}>{facet.headline || facet.title}</h1>
      {md && (
        <div className="max-w-[68ch] opacity-90">
          <MarkdownContent variant="viewer">{md}</MarkdownContent>
        </div>
      )}
      {blocks.length > 0 && <BlockGrid blocks={blocks} layout={layout} accent={accent} />}
    </div>
  );
}

export function GridDeck({
  facets,
  title,
  tagline,
  accent,
  brandId,
  canvasStyle,
  onOpenFacet,
  widthMode = "normal",
  meta,
  onPickAlt,
}: {
  facets: Facet[];
  title: string;
  tagline?: string;
  accent: string;
  brandId?: string;
  canvasStyle: React.CSSProperties | undefined;
  onOpenFacet: (id: string) => void;
  widthMode?: "normal" | "wide";
  meta?: DeckMeta;
  /** Commit an A/B choice for a facet's rung (altIndex undefined ⇒ keep A). */
  onPickAlt?: (facetId: string, rung: Rung, altIndex?: number) => void;
}) {
  const contentTopics = useMemo(() => buildTopics(facets), [facets]);
  // Per-slide A/B view: key `${t}:${s}` → -1 (A / primary) or an alt index.
  const [altView, setAltView] = useState<Record<string, number>>({});
  // Two deck-level slides lead: row 0 = HERO (the title / takeaway cover), row 1 =
  // INDEX (the agenda matrix). Content topics follow, so facet i lives at grid
  // topic index i+2.
  const _synthSlide: Slide = { level: "L1", label: "", slideType: "hero", blocks: [] };
  const topics = useMemo<Topic[]>(
    () => [
      { isHero: true, slides: [{ ..._synthSlide, slideType: "hero" }] },
      { isIndex: true, slides: [{ ..._synthSlide, slideType: "index" }] },
      ...contentTopics,
    ],
    [contentTopics],
  );
  const FACET_OFFSET = 2; // hero + index precede the content topics
  const T = topics.length;
  const [pos, setPos] = useState({ t: 0, s: 0 });
  const wheelAt = useRef(0);

  // Clamp to valid coordinates whenever the data shrinks under us.
  const t = Math.min(pos.t, Math.max(0, T - 1));
  const topic = topics[t];
  const S = topic ? topic.slides.length : 0;
  const s = Math.min(pos.s, Math.max(0, S - 1));

  // Travel direction, derived from the delta since the last committed position.
  // Computed in render (idempotent) so AnimatePresence sees the correct `custom`
  // the moment the outgoing slide begins exiting; committed after paint.
  const prev = useRef({ t, s });
  const lastDir = useRef<Dir>({ axis: "v", sign: 1 });
  if (t !== prev.current.t) lastDir.current = { axis: "v", sign: t > prev.current.t ? 1 : -1 };
  else if (s !== prev.current.s) lastDir.current = { axis: "h", sign: s > prev.current.s ? 1 : -1 };
  const dir = lastDir.current;
  useEffect(() => { prev.current = { t, s }; }, [t, s]);

  // Desired column (rung depth) the reader wants to hold across topics. Vertical
  // moves preserve it, clamping only for display when a topic is shallower — so
  // passing through a 1-slide topic doesn't permanently reset you to the landing.
  // Explicit horizontal / jump actions update it.
  const desiredS = useRef(0);
  const slidesAt = useCallback((ti: number) => topics[ti]?.slides.length ?? 1, [topics]);

  // Jump from an index cell to (topic, slide). col 0..3 → the L1..L4 slide of that
  // topic (falls back to the cover when a level is absent).
  const jumpTo = useCallback((facetId: string, col: number) => {
    const fi = facets.findIndex((f) => f.id === facetId);
    if (fi < 0) return;
    const level = RUNGS[Math.max(0, Math.min(3, col))];
    const slides = contentTopics[fi]?.slides ?? [];
    let si = slides.findIndex((sl) => sl.level === level);
    if (si < 0) si = 0;
    desiredS.current = si;
    setPos({ t: fi + FACET_OFFSET, s: si });
  }, [facets, contentTopics]);

  const down = useCallback(() => setPos((p) => {
    const nt = Math.min(T - 1, p.t + 1);
    return { t: nt, s: Math.min(desiredS.current, slidesAt(nt) - 1) };
  }), [T, slidesAt]);
  const up = useCallback(() => setPos((p) => {
    const nt = Math.max(0, p.t - 1);
    return { t: nt, s: Math.min(desiredS.current, slidesAt(nt) - 1) };
  }), [slidesAt]);
  const right = useCallback(() => setPos((p) => {
    const ns = Math.min(slidesAt(p.t) - 1, p.s + 1);
    desiredS.current = ns;
    return { t: p.t, s: ns };
  }), [slidesAt]);
  const left = useCallback(() => setPos((p) => {
    const ns = Math.max(0, p.s - 1);
    desiredS.current = ns;
    return { t: p.t, s: ns };
  }), []);
  const advance = useCallback(() => setPos((p) => {
    const st = slidesAt(p.t);
    if (p.s < st - 1) { desiredS.current = p.s + 1; return { t: p.t, s: p.s + 1 }; }
    if (p.t < T - 1) { desiredS.current = 0; return { t: p.t + 1, s: 0 }; } // WRAP → next topic, slide 0
    return p;
  }), [slidesAt, T]);
  const retreat = useCallback(() => setPos((p) => {
    if (p.s > 0) { desiredS.current = p.s - 1; return { t: p.t, s: p.s - 1 }; }
    if (p.t > 0) { const ns = slidesAt(p.t - 1) - 1; desiredS.current = ns; return { t: p.t - 1, s: ns }; } // reverse-WRAP
    return p;
  }), [slidesAt]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      switch (e.key) {
        case "ArrowDown": case "j": e.preventDefault(); down(); break;
        case "ArrowUp": case "k": e.preventDefault(); up(); break;
        case "ArrowRight": case "l": e.preventDefault(); right(); break;
        case "ArrowLeft": case "h": e.preventDefault(); left(); break;
        case " ": case "PageDown": e.preventDefault(); (e.shiftKey ? retreat : advance)(); break;
        case "PageUp": e.preventDefault(); retreat(); break;
        case "Enter":
          e.preventDefault();
          if (topics[t]?.isHero || topics[t]?.isIndex) down(); // hero → index → first topic
          else if (topics[t]?.facet) onOpenFacet(topics[t].facet!.id);
          break;
        case "Home": e.preventDefault(); desiredS.current = 0; setPos({ t: 0, s: 0 }); break;
        case "End": e.preventDefault(); desiredS.current = 0; setPos({ t: T - 1, s: 0 }); break;
        default:
          if (/^[1-9]$/.test(e.key)) { const n = Number(e.key) - 1; if (n < T) { e.preventDefault(); desiredS.current = 0; setPos({ t: n, s: 0 }); } }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [down, up, left, right, advance, retreat, onOpenFacet, topics, t, T]);

  const onWheel = useCallback((e: React.WheelEvent) => {
    // Debounced snap — one slide per gesture, no scroll-jacking mid-momentum.
    // A tall slide scrolls internally first (its own overflow), so only fire
    // when the inner content isn't the thing that should scroll.
    const now = e.timeStamp;
    if (Math.abs(e.deltaY) < 24) return;
    if (now - wheelAt.current < 520) return;
    wheelAt.current = now;
    if (e.deltaY > 0) advance(); else retreat();
  }, [advance, retreat]);

  if (!topic) {
    return <div className="flex h-full items-center justify-center rounded-xl border border-border text-sm text-tertiary">Empty deck.</div>;
  }

  const slide = topic.slides[s]!;
  const maxW = widthMode === "wide" ? "max-w-6xl" : "max-w-3xl";
  const hasRight = s < S - 1;
  const hasDown = t < T - 1;

  // A/B view for this slide (only meaningful when it carries an alternative).
  const slideKey = `${t}:${s}`;
  const av = altView[slideKey] ?? -1;
  const showAlt = av >= 0 && !!slide.altBlocks;
  const shownBlocks = showAlt ? slide.altBlocks! : slide.blocks;
  const shownLayout = showAlt ? slide.altLayout : slide.layout;

  return (
    <div
      className="relative min-h-0 flex-1 overflow-hidden rounded-xl border border-border"
      style={canvasStyle}
      onWheel={onWheel}
    >
      {/* current slide — pushes in from the travel direction while the previous
          slide pushes out the opposite edge (both full-screen, opaque). */}
      <AnimatePresence custom={dir} initial={false}>
        <motion.div
          key={`${t}-${s}`}
          custom={dir}
          className="absolute inset-0 overflow-y-auto"
          style={canvasStyle}
          variants={PUSH}
          initial="enter"
          animate="center"
          exit="exit"
          transition={{ duration: PUSH_SECONDS, ease: [0.22, 1, 0.36, 1] }}
        >
          {(!topic.isHero && !topic.isIndex && slide.template) ? (
            /* DESIGNED TEMPLATE — full-bleed, brand-tokened, its own grid. Bypasses
               the narrow reading cage entirely (the gold-standard layer). */
            <div className="tpl min-h-full" style={brandStyle(brandId, accent)}>
              <div className="tpl-inner"><TemplateSlide template={slide.template} active={slide.label} accent={accent} /></div>
            </div>
          ) : (
          <div className={`mx-auto flex min-h-full ${maxW} flex-col justify-center gap-5 px-10 py-[8vh]`}>
            {topic.isHero ? (
              <HeroSlide title={title} tagline={tagline} accent={accent} meta={meta} count={contentTopics.length} />
            ) : topic.isIndex ? (
              <DeckIndex facets={facets} title={title} accent={accent} onSelect={jumpTo} meta={meta} />
            ) : (
              <>
                {/* topic header — kind + the per-level slide tabs (L1→L4) */}
                <div className="flex items-baseline gap-3">
                  <span className="text-xs font-medium uppercase tracking-[0.25em]" style={{ color: accent }}>{topic.facet!.kind}</span>
                  {S > 1 && (
                    <span className="flex gap-1.5 text-[10px] uppercase tracking-wide text-tertiary">
                      {topic.slides.map((sl, i) => (
                        <span key={i} className={i === s ? "font-semibold" : "opacity-40"} style={i === s ? { color: accent } : undefined}>
                          {sl.label}
                        </span>
                      ))}
                    </span>
                  )}
                  {slide.altBlocks && (
                    <span className="ml-auto flex items-center gap-1 rounded border border-current/20 px-1.5 py-0.5 text-[10px]">
                      <button onClick={() => setAltView((v) => ({ ...v, [slideKey]: -1 }))} className={av < 0 ? "font-bold" : "opacity-40 hover:opacity-80"} style={av < 0 ? { color: accent } : undefined} title="Version A">A</button>
                      <span className="opacity-30">/</span>
                      <button onClick={() => setAltView((v) => ({ ...v, [slideKey]: 0 }))} className={av >= 0 ? "font-bold" : "opacity-40 hover:opacity-80"} style={av >= 0 ? { color: accent } : undefined} title="Version B — an alternative design">B</button>
                      {onPickAlt && slide.altRung && (
                        <button onClick={() => onPickAlt(topic.facet!.id, slide.altRung!, av >= 0 ? 0 : undefined)} className="ml-1 border-l border-current/20 pl-1.5 opacity-60 hover:opacity-100" title="Keep the shown version and drop the other">Keep</button>
                      )}
                    </span>
                  )}
                </div>
                {slide.slideType === "cover" || slide.slideType === "section" ? (
                  /* L1 COVER — one idea, biggest type, lowest density (inverse-density). */
                  <CoverSlide facet={topic.facet!} accent={accent} section={slide.slideType === "section"} />
                ) : slide.slideType === "doc-view" ? (
                  /* L4 DOC-VIEW — the whole topic as a navigable document inside the deck. */
                  <DocViewSlide facet={topic.facet!} blocks={shownBlocks} layout={shownLayout} accent={accent} />
                ) : (
                  /* L2 / L3 CONTENT — the action-title headline + the arranged modules. */
                  <>
                    <h1 className="text-3xl font-semibold leading-tight sm:text-4xl">{topic.facet!.headline || topic.facet!.title}</h1>
                    {shownBlocks.length > 0 && <BlockGrid blocks={shownBlocks} layout={shownLayout} accent={accent} />}
                  </>
                )}
              </>
            )}
          </div>
          )}
        </motion.div>
      </AnimatePresence>

      {/* right chevron — "there's more depth on this topic" */}
      {hasRight && (
        <button
          onClick={right}
          title="deeper (→)"
          className="absolute right-3 top-1/2 z-10 -translate-y-1/2 rounded-full border border-current/20 bg-black/10 p-1.5 opacity-60 transition-opacity hover:opacity-100"
          style={{ color: accent }}
        >
          <ChevronRight size={18} />
        </button>
      )}
      {/* down chevron — "next topic" */}
      {hasDown && (
        <button
          onClick={down}
          title="next topic (↓)"
          className="absolute bottom-3 left-1/2 z-10 -translate-x-1/2 rounded-full border border-current/20 bg-black/10 p-1.5 opacity-60 transition-opacity hover:opacity-100"
          style={{ color: accent }}
        >
          <ChevronDown size={18} />
        </button>
      )}

      {/* 2D minimap — rows = topics, dots = detail slides; click to jump */}
      <div className="absolute left-3 top-1/2 z-10 flex -translate-y-1/2 flex-col gap-1.5">
        {topics.map((tp, ti) => {
          const tpTitle = tp.isHero ? "Title" : tp.isIndex ? "Overview" : tp.facet!.title;
          return (
          <div key={ti} className="flex items-center gap-1">
            {tp.slides.map((sl, si) => (
              <button
                key={si}
                onClick={() => { desiredS.current = si; setPos({ t: ti, s: si }); }}
                title={tpTitle}
                aria-label={`${tpTitle} · ${sl.label || tpTitle}`}
                className="rounded-full transition-all"
                style={{
                  width: ti === t && si === s ? 9 : 6,
                  height: ti === t && si === s ? 9 : 6,
                  background: ti === t ? accent : "currentColor",
                  opacity: ti === t && si === s ? 1 : ti === t ? 0.5 : 0.25,
                }}
              />
            ))}
          </div>
          );
        })}
      </div>

      {/* position readout — content topics numbered 1..(T-2); hero + index lead */}
      <div className="absolute bottom-3 right-3 z-10 text-[11px] tabular-nums text-tertiary">
        {topic.isHero ? "Title" : topic.isIndex ? "Overview"
          : `${t - (FACET_OFFSET - 1)}/${T - FACET_OFFSET}${S > 1 ? ` · ${slide.label}` : ""}`}
      </div>
    </div>
  );
}
