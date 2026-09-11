// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism gallery — the /prism landing page. Tile grid (title +
//   facet count + brand + updated) with search + sort. Click opens a doc;
//   "+ New" starts the generate panel. Mirrors flow-gallery's list surface
//   (no folders — prism docs don't have that axis yet).
// AGENT_HEADER_END -->
import { AlertTriangle, ArrowDownAZ, Boxes, Clock, LayoutGrid, Layers, List, Loader2, Plus, Search, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { Button } from "@/components/ui/button";
import { prismApi, prismDeck2Api, type Deck2Summary, type DocSummary, type GenerateJob } from "@/lib/prism-api";
import { parseApiDate } from "@/lib/format";

function relTime(iso: string): string {
  if (!iso) return "";
  const t = parseApiDate(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  const m = s / 60;
  if (m < 60) return `${Math.floor(m)}m ago`;
  const h = m / 60;
  if (h < 24) return `${Math.floor(h)}h ago`;
  const d = h / 24;
  if (d < 30) return `${Math.floor(d)}d ago`;
  return `${Math.floor(d / 30)}mo ago`;
}

/** Absolute calendar date for a tile tooltip (the mtime-derived created stamp). */
function absDate(iso?: string): string {
  if (!iso) return "";
  const t = parseApiDate(iso).getTime();
  if (Number.isNaN(t)) return "";
  return new Date(t).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function PrismGallery() {
  const navigate = useNavigate();
  const [docs, setDocs] = useState<DocSummary[]>([]);
  // deck2 = the kit-first interactive decks (compiler → DeckDoc), a separate
  // store from the facet-tree docs; surfaced here so live decks are discoverable
  // instead of reachable only by direct /prism/deck?id= URL.
  const [decks, setDecks] = useState<Deck2Summary[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<"recent" | "name">("recent");
  // Deck-overview layout: tile grid (default) vs. a compact list. Scoped to the
  // interactive-decks section (the overview the owner asked to rework).
  const [deckView, setDeckView] = useState<"grid" | "list">("grid");
  // In-progress generations (spinner tiles). Terminal "done" jobs are hidden —
  // they trigger a doc-list refresh so the finished deck slides in as a real tile.
  const [jobs, setJobs] = useState<GenerateJob[]>([]);
  const refreshedFor = useRef<Set<string>>(new Set());

  const refresh = useCallback(async () => {
    try {
      const [docsRes, decksRes] = await Promise.all([
        prismApi.list(),
        prismDeck2Api.list().catch(() => ({ decks: [] as Deck2Summary[] })),
      ]);
      setDocs(docsRes.docs);
      setDecks(decksRes.decks);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll the generation-job registry so a deck being built shows here (spinner
  // + live phase) even when the request was fired from another view.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const { jobs } = await prismApi.generateJobs();
        if (!alive) return;
        const done = jobs.filter((j) => j.status === "done" && !refreshedFor.current.has(j.id));
        if (done.length) {
          done.forEach((j) => refreshedFor.current.add(j.id));
          refresh();
        }
        setJobs(jobs.filter((j) => j.status !== "done"));
      } catch {
        /* transient poll error — keep the last known jobs */
      }
    };
    tick();
    const iv = setInterval(tick, 1500);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, [refresh]);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    let list = docs.filter((d) => !d.variant_of); // variants nest under their source in the viewer, not the gallery
    if (q) list = list.filter((d) => d.title.toLowerCase().includes(q));
    const sorted = [...list];
    if (sort === "name") sorted.sort((a, b) => a.title.localeCompare(b.title));
    else sorted.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    return sorted;
  }, [docs, search, sort]);

  const visibleDecks = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = q
      ? decks.filter(
          (d) =>
            d.title.toLowerCase().includes(q) ||
            (d.brand || "").toLowerCase().includes(q) ||
            (d.tagline || "").toLowerCase().includes(q),
        )
      : decks;
    const sorted = [...list];
    // Default = newest first (by file mtime, the created proxy); Name = A→Z.
    if (sort === "name") sorted.sort((a, b) => a.title.localeCompare(b.title));
    else sorted.sort((a, b) => (b.created || "").localeCompare(a.created || ""));
    return sorted;
  }, [decks, search, sort]);

  const remove = async (e: React.MouseEvent, d: DocSummary) => {
    e.stopPropagation();
    if (!window.confirm(`Delete "${d.title}"? This cannot be undone.`)) return;
    await prismApi.remove(d.id);
    setDocs((ds) => ds.filter((x) => x.id !== d.id));
  };

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold text-fg">okuro·prism</span>
        <span className="text-xs text-tertiary">fast overview, drill in where it matters</span>
        <div className="ml-auto flex items-center gap-2">
          <div className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1">
            <Search size={13} className="text-tertiary" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search…"
              className="w-40 bg-transparent text-sm text-fg placeholder:text-tertiary focus:outline-none"
            />
          </div>
          <button
            className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-fg hover:border-accent hover:text-accent"
            onClick={() => setSort((s) => (s === "recent" ? "name" : "recent"))}
            title="toggle sort"
          >
            {sort === "recent" ? <Clock size={13} /> : <ArrowDownAZ size={13} />}
            {sort === "recent" ? "Recent" : "Name"}
          </button>
          <button
            className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-fg hover:border-accent hover:text-accent"
            onClick={() => navigate("/prism-gallery")}
            title="overview of every module & layout"
          >
            <Layers size={13} /> Modules &amp; Layouts
          </button>
          {/* The v4 solver KIT (37 components) — a SEPARATE library from the
              BlockView modules above. Static specimen gallery served outside the
              SPA at /prism-kit, so it must be a plain external link (new tab),
              not react-router navigate(). */}
          <a
            href="/prism-kit/gallery/"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-fg hover:border-accent hover:text-accent"
            title="v4 solver component kit (37) — specimen gallery"
          >
            <Boxes size={13} /> Kit (37) ↗
          </a>
          <Button size="sm" onClick={() => navigate("/prism?new=1")}>
            <Plus size={15} /> New doc
          </Button>
        </div>
      </div>

      {loading ? (
        <p className="text-sm text-tertiary">Loading…</p>
      ) : visible.length === 0 && jobs.length === 0 && visibleDecks.length === 0 ? (
        <div className="flex flex-1 items-center justify-center rounded-xl border border-border text-sm text-tertiary">
          {search ? "Nothing matches your search." : "No decks yet — generate one with New doc."}
        </div>
      ) : (
        <div className="flex flex-col gap-4 overflow-y-auto">
          {visibleDecks.length > 0 && (
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-xs font-semibold text-tertiary">
                  <LayoutGrid size={13} /> Decks · interactive
                </div>
                <button
                  className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-fg hover:border-accent hover:text-accent"
                  onClick={() => setDeckView((v) => (v === "grid" ? "list" : "grid"))}
                  title="toggle deck layout"
                >
                  {deckView === "grid" ? <List size={13} /> : <LayoutGrid size={13} />}
                  {deckView === "grid" ? "List" : "Grid"}
                </button>
              </div>
              {deckView === "grid" ? (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
                  {visibleDecks.map((d) => (
                    <div
                      key={d.id}
                      onClick={() => navigate(`/prism/deck?id=${encodeURIComponent(d.id)}`)}
                      className="group flex cursor-pointer flex-col gap-2 rounded-lg border border-border p-3 hover:border-accent/60"
                    >
                      {/* Text preview — the deck's own title + tagline (a live hero
                          thumbnail would need a full DeckDoc fetch + shadow-kit mount
                          per tile; too heavy for the index). Recognizable, not generic. */}
                      <div className="flex h-24 flex-col justify-center gap-1 rounded-md border border-accent/30 bg-accent/5 p-3">
                        <div className="line-clamp-3 text-sm font-semibold leading-snug text-accent">{d.title}</div>
                        {d.tagline ? (
                          <div className="line-clamp-2 text-[11px] leading-snug text-tertiary">{d.tagline}</div>
                        ) : null}
                      </div>
                      <div className="flex items-center justify-between text-[11px] text-tertiary">
                        <span title={absDate(d.created)}>{relTime(d.created || "") || "—"}</span>
                        <span>
                          {d.topics ?? 0} topic{d.topics === 1 ? "" : "s"} · L1–L4
                        </span>
                      </div>
                      {d.brand && (
                        <span className="w-fit rounded-full border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                          {d.brand}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="flex flex-col divide-y divide-border rounded-lg border border-border">
                  {visibleDecks.map((d) => (
                    <div
                      key={d.id}
                      onClick={() => navigate(`/prism/deck?id=${encodeURIComponent(d.id)}`)}
                      className="group flex cursor-pointer items-center gap-3 px-3 py-2 hover:bg-accent/5"
                    >
                      <LayoutGrid size={16} className="shrink-0 text-accent" />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium text-fg">{d.title}</div>
                        {d.tagline ? (
                          <div className="truncate text-[11px] text-tertiary">{d.tagline}</div>
                        ) : null}
                      </div>
                      <span className="hidden shrink-0 text-[11px] text-tertiary sm:inline">
                        {d.topics ?? 0} topic{d.topics === 1 ? "" : "s"}
                      </span>
                      {d.brand && (
                        <span className="shrink-0 rounded-full border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                          {d.brand}
                        </span>
                      )}
                      <span className="shrink-0 text-[11px] text-tertiary" title={absDate(d.created)}>
                        {relTime(d.created || "") || "—"}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {(visible.length > 0 || jobs.length > 0) && (
            <div className="flex flex-col gap-2">
              {visibleDecks.length > 0 && (
                <div className="flex items-center gap-1.5 text-xs font-semibold text-tertiary">
                  <Layers size={13} /> Docs · facet-tree
                </div>
              )}
              <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
          {jobs.map((j) => {
            const failed = j.status === "error";
            return (
              <div
                key={j.id}
                className={`flex flex-col gap-2 rounded-lg border p-3 ${failed ? "border-[var(--color-status-error,#f92f77)]/50" : "border-accent/40"}`}
                title={failed ? j.error ?? "generation failed" : `building — ${j.phase}`}
              >
                <div className="flex h-20 items-center justify-center rounded-md border border-border/60 bg-black/10">
                  {failed ? (
                    <AlertTriangle size={24} className="text-[var(--color-status-error,#f92f77)]" />
                  ) : (
                    <Loader2 size={24} className="animate-spin text-accent" />
                  )}
                </div>
                <div className="truncate text-sm font-medium text-fg">{j.title || j.topic}</div>
                <div className="flex items-center justify-between text-[11px]">
                  {failed ? (
                    <span className="text-[var(--color-status-error,#f92f77)]">failed</span>
                  ) : (
                    <span className="text-accent">{j.phase}…</span>
                  )}
                  {j.tier && <span className="text-tertiary">{j.tier}</span>}
                </div>
                {failed && <span className="truncate text-[10px] text-tertiary">{j.error}</span>}
              </div>
            );
          })}
          {visible.map((d) => (
            <div
              key={d.id}
              onClick={() => navigate(`/prism?id=${encodeURIComponent(d.id)}`)}
              className="group relative flex cursor-pointer flex-col gap-2 rounded-lg border border-border p-3 hover:border-accent/60"
            >
              <div className="flex h-20 items-center justify-center rounded-md border border-border/60 bg-black/10 text-tertiary">
                <Layers size={26} />
              </div>
              <div className="truncate text-sm font-medium text-fg">{d.title}</div>
              <div className="flex items-center justify-between text-[11px] text-tertiary">
                <span>{d.facet_count} facet{d.facet_count === 1 ? "" : "s"}</span>
                <span>{relTime(d.updated_at)}</span>
              </div>
              {d.brand_id && (
                <span className="w-fit rounded-full border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                  {d.brand_id}
                </span>
              )}
              <button
                onClick={(e) => remove(e, d)}
                aria-label="delete doc"
                className="absolute right-2 top-2 hidden h-5 w-5 items-center justify-center rounded-full bg-[var(--color-status-error,#f92f77)] text-white group-hover:flex"
              >
                <Trash2 size={11} />
              </button>
            </div>
          ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
