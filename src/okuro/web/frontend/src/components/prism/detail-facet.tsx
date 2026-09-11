// <!-- AGENT_HEADER
// role: code
// purpose: Altitude-3 detail view for a single Prism facet — every rung
//   (glance→expert) stacked full, TOC scrollspy, drill into child facets. Host-
//   agnostic: navigation is via onBack/onOpenFacet callbacks (the live viewer
//   wires react-router; the HTML export wires local state), so this one copy
//   serves both surfaces and can never drift between them.
// index: RUNG_LABEL | DetailFacet
// AGENT_HEADER_END -->
import { useEffect, useRef, useState } from "react";

import { BlockGrid } from "@/components/prism/blocks";
import { RUNGS, rungToBlocks, type DocDetail, type Facet, type Rung } from "@/lib/prism-api";

export const RUNG_LABEL: Record<Rung, string> = {
  L1: "Cover",
  L2: "Key Points",
  L3: "Detail",
  L4: "Full Doc",
};

/** Altitude 3 — a single facet rendered as its own full page: every rung
 *  (glance→expert) stacked in full, plus links to drill into child facets.
 *  Reached via ?facet=<id> (viewer) or local detail state (export). */
export function DetailFacet({
  doc, facetId, accent, canvasStyle, onBack, onOpenFacet, widthMode = "normal",
}: {
  doc: DocDetail;
  facetId: string;
  accent: string;
  canvasStyle: React.CSSProperties | undefined;
  onBack: () => void;
  onOpenFacet: (id: string) => void;
  widthMode?: "normal" | "wide";
}) {
  const facets = doc.doc.facets ?? {};
  const facet = facets[facetId];
  const backBtn = "rounded-md border border-current/20 px-2.5 py-1 text-sm opacity-70 transition-opacity hover:opacity-100";

  const scrollRef = useRef<HTMLDivElement>(null);
  const secRefs = useRef<Record<string, HTMLElement | null>>({});
  const [activeSec, setActiveSec] = useState<string>("");

  const children = facet ? ((facet.children ?? []).map((id) => facets[id]).filter(Boolean) as Facet[]) : [];
  const presentRungs = facet ? RUNGS.filter((r) => rungToBlocks(facet.rungs?.[r]).length) : [];
  const tocKeys = [...presentRungs, ...(children.length ? ["__children"] : [])];

  // Scrollspy — highlight the TOC entry for the section crossing the top band.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => {
        if (e.isIntersecting) setActiveSec((e.target as HTMLElement).dataset.sec || "");
      }),
      { root, rootMargin: "-15% 0px -70% 0px", threshold: 0 },
    );
    tocKeys.forEach((k) => { const el = secRefs.current[k]; if (el) io.observe(el); });
    return () => io.disconnect();
  }, [facetId, tocKeys.length]);

  const jump = (k: string) => secRefs.current[k]?.scrollIntoView({ behavior: "smooth", block: "start" });
  // Hash deep-link: …&facet=X#working scrolls to that rung on open.
  useEffect(() => {
    const h = window.location.hash.replace("#", "");
    if (h && secRefs.current[h]) { const t = setTimeout(() => secRefs.current[h]?.scrollIntoView(), 120); return () => clearTimeout(t); }
  }, [facetId, tocKeys.length]);
  const onLink = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href") || "";
    if (href.startsWith("#facet:")) { e.preventDefault(); onOpenFacet(href.slice(7)); }
  };
  const tocItem = (on: boolean) => `w-full rounded px-2 py-1 text-left text-xs transition-colors ${on ? "bg-accent/15 text-accent" : "opacity-55 hover:opacity-100"}`;

  if (!facet) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto rounded-xl border border-border" style={canvasStyle}>
        <div className="mx-auto flex max-w-3xl flex-col items-start gap-4 px-8 py-[8vh]">
          <p className="opacity-70">Section not found.</p>
          <button onClick={onBack} className={backBtn}>← Back to deck</button>
        </div>
      </div>
    );
  }

  return (
    <div ref={scrollRef} onClick={onLink} className="min-h-0 flex-1 overflow-y-auto rounded-xl border border-border" style={canvasStyle}>
      <div className={`mx-auto flex ${widthMode === "wide" ? "max-w-7xl" : "max-w-5xl"} gap-8 px-8 py-[6vh]`}>
        {tocKeys.length > 1 && (
          <aside className="sticky top-0 hidden h-max w-40 shrink-0 md:block">
            <button onClick={onBack} className={`${backBtn} mb-3 w-full`}>← Back to deck</button>
            <ul className="space-y-0.5">
              {presentRungs.map((r) => (
                <li key={r}><button onClick={() => jump(r)} className={tocItem(activeSec === r)}>{RUNG_LABEL[r]}</button></li>
              ))}
              {children.length > 0 && (
                <li><button onClick={() => jump("__children")} className={tocItem(activeSec === "__children")}>Drill into</button></li>
              )}
            </ul>
          </aside>
        )}

        <div className="min-w-0 flex-1 space-y-6">
          <button onClick={onBack} className={`${backBtn} self-start md:hidden`}>← Back to deck</button>
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.25em]" style={{ color: accent }}>{facet.kind}</p>
            <h1 className="mt-1 text-4xl font-semibold leading-tight">{facet.headline || facet.title}</h1>
          </div>
          {presentRungs.map((rung) => (
            <section
              key={rung}
              ref={(el) => { secRefs.current[rung] = el; }}
              data-sec={rung}
              className="scroll-mt-6 space-y-3 border-t border-current/10 pt-4"
            >
              <p className="text-[11px] uppercase tracking-wide opacity-50">{RUNG_LABEL[rung]}</p>
              <BlockGrid blocks={rungToBlocks(facet.rungs?.[rung])} layout={facet.rungs?.[rung]?.layout} accent={accent} />
            </section>
          ))}
          {children.length > 0 && (
            <div
              ref={(el) => { secRefs.current["__children"] = el; }}
              data-sec="__children"
              className="scroll-mt-6 space-y-2 border-t border-current/10 pt-4"
            >
              <p className="text-[11px] uppercase tracking-wide opacity-50">Drill into</p>
              {children.map((c) => (
                <button
                  key={c.id}
                  onClick={() => onOpenFacet(c.id)}
                  className="flex w-full items-center justify-between rounded-lg border border-current/15 px-3 py-2 text-left text-sm transition-colors hover:border-current/40"
                >
                  <span>{c.title}</span>
                  <span className="opacity-50">→</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
