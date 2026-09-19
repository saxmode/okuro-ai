import { useMemo, useRef, useState } from "react";
import { Network, Grid3X3, Table as TableIcon, Clock, Loader2, Boxes, MapPin, SlidersHorizontal } from "lucide-react";
import { SidePanel, MobilePanelTrigger } from "@/components/ui/side-panel";
import { Segmented } from "@/components/ui/segmented";
import { EmptyState } from "@/components/ui/empty-state";
import { useKnowledgeGraph } from "@/hooks/use-knowledge";
import { UnifiedGraph } from "@/components/knowledge/unified-graph";
import { DetailDrawer } from "@/components/knowledge/detail-drawer";
import { FacetSidebar, type FilterState } from "@/components/knowledge/facet-sidebar";
import { GalleryView } from "@/components/knowledge/gallery-view";
import { TableView } from "@/components/knowledge/table-view";
import { TimelineView } from "@/components/knowledge/timeline-view";
import { SavedQueryChips } from "@/components/knowledge/saved-query-chips";
import { TourPanel } from "@/components/knowledge/tour-panel";
import type { KnowledgeNode, KnowledgeNodeType } from "@/types/knowledge";
import { cn } from "@/lib/utils";
import { UNKNOWN_TOTAL } from "@/lib/capped-count";
import { sectionSlugs } from "@/shell/views/sections";
import type { LeafViewProps } from "@/shell/views/registry";
import { useSectionTitle } from "@/shell/components/PageTitle";

/**
 * /knowledge — multi-lens view over the unified memory graph.
 *
 * The graph is one lens, not the homepage. Tabs let the user pick the
 * geometry that fits the question:
 *   - Gallery: skimmable cards (default — fastest re-orientation)
 *   - Table: dense rows for sorting/scanning
 *   - Graph: local force-directed (depth-bounded) — opt-in
 *   - Timeline: when-it-happened axis
 *
 * Backed by GET /api/knowledge/graph which returns memory + thoughts +
 * artifacts + progress + kg_triples as one filterable node-set.
 *
 * Scaffold note: Gallery + Graph + Detail-drawer + Faceted sidebar are
 * follow-up todos. This file ships the page shell, top stats strip, and
 * an EmptyState per tab so the route is real but UI iteration can
 * happen panel-by-panel.
 */
/** The four lenses, in the section list's own order. */
type Lens = "gallery" | "table" | "graph" | "timeline";
const LENSES: readonly Lens[] = ["gallery", "table", "graph", "timeline"];

/**
 * THE FOUR LENSES ARE SECTIONS — ruled (KNOW summary Q7), and the same shape
 * BRAIN and MODELS use: resolved FROM the declared list rather than pinned to
 * 0..3, so inserting a section cannot silently render another lens.
 *
 * Before this, all four lived in `useState` and NONE could be linked, and the
 * shell declared the single section `["GRAPH"]` — the lens that is not the
 * default — so `?view=graph` resolved to index 0 and showed GALLERY.
 *
 * `?tab=` works here too without this file doing anything: `routes.ts:605`
 * reads `params.get("view") ?? params.get("tab")`.
 */
const LENS_SLUGS = sectionSlugs("know", "knowledge");
const SECTION_OF = (lens: Lens): number => Math.max(0, LENS_SLUGS.indexOf(lens));
const LENS_OF_SECTION = (index: number): Lens => {
  const slug = LENS_SLUGS[index];
  return (LENSES.find((l) => l === slug) ?? "gallery") as Lens;
};

/** The `per_type_cap` this page asks `GET /api/knowledge/graph` for. */
const PER_TYPE_CAP = 200;

export function KnowledgePage({ view: section, onSelectView }: Partial<LeafViewProps> = {}) {
  /* Local fallback for every context without the shell — `?embed=1`, a unit
     test — exactly as `pane-active`'s default is `true`. */
  const [localSection, setLocalSection] = useState(0);
  const current = section ?? localSection;
  const activeTab = LENS_OF_SECTION(current);
  /* `Tabs` calls `onValueChange` twice per mouse click, both inside one tick,
     so a guard against the RENDERED value passes twice and the address gets
     two history entries. Traced on BRAIN with a patched `pushState`; the ref
     holds what was last ASKED for. */
  const asked = useRef(current);
  if (asked.current !== current) asked.current = current;
  const setActiveTab = (next: string) => {
    const index = SECTION_OF(next as Lens);
    if (index === asked.current) return;
    asked.current = index;
    if (onSelectView) onSelectView(index);
    else setLocalSection(index);
  };
  const [hideOrphans, setHideOrphans] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filters, setFilters] = useState<FilterState>({});
  const [tourOpen, setTourOpen] = useState(false);
  const [facetsOpen, setFacetsOpen] = useState(false);

  const types = filters.entityType ? [filters.entityType] : undefined;
  const { data, isLoading, isError, error } = useKnowledgeGraph({
    per_type_cap: PER_TYPE_CAP,
    hide_orphans: hideOrphans,
    types,
    topic: filters.topic,
    project: filters.project,
    status: filters.status,
    confidence_min: filters.confidence_min,
    confidence_max: filters.confidence_max,
  });

  const stats = useMemo(() => buildStats(data?.nodes ?? [], data?.edges?.length ?? 0), [data]);

  /* ===================================================================
     THE LENS ROW AND THE GRAPH CONTROLS ARE ON THE PLATE NOW.
     ===================================================================
     They were a `sticky top-0` bar inside `.c-panes`, which audit C4-3
     names: `.c-panes{z-index:0}` is a containment boundary, so a leaf's
     sticky header pins BEHIND the plate. The plate is the sticky surface
     now. BRAIN carried the identical bar and is fixed the same way, one
     cause, one pattern (DP11).

     RADIX TABS COULD NOT MAKE THE TRIP. A published node is created here
     and rendered by `PlateTitle`, inside `TopicBar`'s React tree; context
     flows through the tree, so `TabsList` and `TabsTrigger` lose their
     provider. `ui/segmented` takes plain props and carries the four icons
     just as well. `TabsContent` also unmounted the inactive lens, throwing
     away a graph layout on every switch; `hidden` keeps all four mounted
     against the one `useKnowledgeGraph()` query they share. */
  const header = useMemo(
    () => ({
      actions: (
        <>
          <Segmented
            ariaLabel="Knowledge lens"
            value={activeTab}
            onChange={(v) => setActiveTab(v as string)}
            options={[
              { label: "Gallery", value: "gallery", icon: <Grid3X3 /> },
              { label: "Table", value: "table", icon: <TableIcon /> },
              { label: "Graph", value: "graph", icon: <Network /> },
              { label: "Timeline", value: "timeline", icon: <Clock /> },
            ]}
          />
          {activeTab === "graph" && (
            <button
              type="button"
              onClick={() => setTourOpen((v) => !v)}
              className={cn(
                "flex items-center gap-1.5 rounded-sm border px-2 transition-colors",
                tourOpen
                  ? "border-accent text-accent"
                  : "border-border-subtle hover:border-fg-muted hover:text-fg",
              )}
              title={tourOpen ? "Hide code tour" : "Show code tour"}
            >
              <MapPin />
              tour
            </button>
          )}
          <label className="flex cursor-pointer items-center gap-1.5 text-tertiary select-none">
            <input
              type="checkbox"
              checked={hideOrphans}
              onChange={(e) => setHideOrphans(e.target.checked)}
              className="h-3 w-3 accent-accent"
            />
            hide orphans
          </label>
          {isLoading && <Loader2 className="animate-spin" />}
        </>
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeTab, tourOpen, hideOrphans, isLoading, onSelectView],
  );
  useSectionTitle(header);

  return (
    <div className="page-shell space-y-6">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">Knowledge</h1>` above this pane. The subtitle
          stays as the lead: it says what the node set IS, which the one-word
          leaf label cannot. No controls to relocate — the `right` slot was
          empty and the leaf's controls live in the sticky bar below.
          `text-fg-muted`, not `text-tertiary`: that tier is the open AA
          failure (kit todo c581c9b2) and does not flip with the appearance. */}
      <p className="type-small text-fg-muted">
        Everything agents learned, thought, decided, delivered — as one
        filterable graph
      </p>

      <StatsStrip
        stats={stats}
        loading={isLoading}
        truncated={data?.truncated ?? false}
        cap={PER_TYPE_CAP}
      />

      <SavedQueryChips
        currentFilters={filters}
        onApply={setFilters}
        hasActive={Object.values(filters).some((v) => v !== undefined && v !== null && v !== "")}
      />

      <div className="flex gap-6">
        <SidePanel
          side="left"
          desktopClassName="w-56 shrink-0 overflow-y-auto border-r border-border-subtle pr-4"
          open={facetsOpen}
          onOpenChange={setFacetsOpen}
          title="Filters"
        >
          <FacetSidebar
            facets={data?.facets ?? null}
            filters={filters}
            onChange={setFilters}
            totalCount={data?.nodes?.length ?? 0}
          />
        </SidePanel>

        <div className="min-w-0 flex-1">
      <MobilePanelTrigger
        icon={<SlidersHorizontal className="h-3 w-3" />}
        label="Filters"
        onClick={() => setFacetsOpen(true)}
        className="mb-3"
      />
        <div hidden={activeTab !== "gallery"} className="pt-4">
          {isError ? (
            <EmptyState
              title="Couldn't load knowledge"
              description={(error as Error | undefined)?.message ?? "Unknown error"}
            />
          ) : isLoading ? (
            <EmptyState title="Loading…" description="Querying the unified graph" />
          ) : (data?.nodes ?? []).length === 0 ? (
            <EmptyState
              icon={<Boxes className="h-10 w-10" />}
              title="No knowledge yet"
              description="Memories, thoughts, artifacts, and progress will appear here as agents work."
            />
          ) : (
            <GalleryView
              nodes={data!.nodes}
              onSelect={setSelectedId}
              selectedId={selectedId}
            />
          )}
        </div>

        <div hidden={activeTab !== "table"} className="pt-4">
          {isError ? (
            <EmptyState
              title="Couldn't load knowledge"
              description={(error as Error | undefined)?.message ?? "Unknown error"}
            />
          ) : isLoading ? (
            <EmptyState title="Loading…" description="Querying the unified graph" />
          ) : (data?.nodes ?? []).length === 0 ? (
            <EmptyState
              icon={<Boxes className="h-10 w-10" />}
              title="No knowledge yet"
            />
          ) : (
            <TableView
              nodes={data!.nodes}
              onSelect={setSelectedId}
              selectedId={selectedId}
            />
          )}
        </div>

        {/* ===================================================================
            THE GRAPH IS THE ONE LENS THAT MAY NOT BE MOUNTED WHILE HIDDEN.
            ===================================================================
            The other three keep their state across a switch because `hidden`
            keeps them mounted. React Flow cannot: it measures its container on
            mount to compute the viewport, and a `hidden` container measures
            0x0. MEASURED by the step-3b critic — a COLD open of this lens
            framed the graph at transform 227,280, against -8,-3 after leaving
            and coming back, i.e. the only correct framing was the one that
            happened to remount after a real measurement.

            So this lens renders its content only while it is the active one.
            The cost is the layout on every entry, which is the behaviour that
            shipped before step 3b; the alternative is a graph that opens
            wrongly framed every first time, which is worse and is not what
            keeping state was for. */}
        <div hidden={activeTab !== "graph"} className="pt-4">
          {activeTab !== "graph" ? null : isError ? (
            <EmptyState
              title="Couldn't load knowledge"
              description={(error as Error | undefined)?.message ?? "Unknown error"}
            />
          ) : isLoading ? (
            <EmptyState title="Loading…" description="Querying the unified graph" />
          ) : (data?.nodes ?? []).length === 0 ? (
            <EmptyState title="No knowledge yet" description="Empty graph." />
          ) : (
            <div className="flex gap-3">
              {tourOpen && (
                <TourPanel
                  project={filters.project}
                  onFocus={setSelectedId}
                  onClose={() => setTourOpen(false)}
                />
              )}
              <div className="min-w-0 flex-1">
                <UnifiedGraph
                  nodes={data!.nodes}
                  edges={data!.edges}
                  onSelect={setSelectedId}
                  selectedId={selectedId}
                />
              </div>
            </div>
          )}
        </div>

        <div hidden={activeTab !== "timeline"} className="pt-4">
          {isError ? (
            <EmptyState
              title="Couldn't load knowledge"
              description={(error as Error | undefined)?.message ?? "Unknown error"}
            />
          ) : isLoading ? (
            <EmptyState title="Loading…" description="Querying the unified graph" />
          ) : (data?.nodes ?? []).length === 0 ? (
            <EmptyState
              icon={<Boxes className="h-10 w-10" />}
              title="No knowledge yet"
              description="Memories, thoughts, artifacts, and progress will appear here as agents work."
            />
          ) : (
            <TimelineView
              nodes={data!.nodes}
              onSelect={setSelectedId}
              selectedId={selectedId}
            />
          )}
        </div>
        </div>
      </div>

      <DetailDrawer
        nodeId={selectedId}
        onClose={() => setSelectedId(null)}
        onSelect={(id) => setSelectedId(id)}
      />
    </div>
  );
}

// ── Stats strip ──────────────────────────────────────────────────────

interface Stats {
  total: number;
  edges: number;
  byType: Record<KnowledgeNodeType, number>;
}

function buildStats(nodes: KnowledgeNode[], edgeCount: number): Stats {
  const byType: Record<KnowledgeNodeType, number> = {
    memory: 0,
    thought: 0,
    artifact: 0,
    progress: 0,
    kg_entity: 0,
  };
  for (const n of nodes) byType[n.type] = (byType[n.type] ?? 0) + 1;
  return { total: nodes.length, edges: edgeCount, byType };
}

const TYPE_LABEL: Record<KnowledgeNodeType, string> = {
  memory: "Memories",
  thought: "Thoughts",
  artifact: "Artifacts",
  progress: "Progress",
  kg_entity: "KG entities",
};

/**
 * R7 (372ccdb2) — THESE TILES USED TO PRINT THE PAGE SIZE AS THE STORE SIZE.
 *
 * `per_type_cap` is 200, and on this box three of the five types sit EXACTLY at
 * it with `TRUNCATED` lit, so `MEMORIES 200` was the ceiling wearing the look
 * of a census — on the one leaf whose whole claim is "everything agents
 * learned". A capped tile now reads `200 of —`: the count it is showing, and a
 * dash for the total it cannot know. `TOTAL NODES` and `EDGES` inherit it,
 * because a total over truncated types is truncated too.
 *
 * The dash and not a number, because `/api/knowledge/graph` returns no total
 * beside the capped arrays and the charter keeps the backend untouched until
 * p5 — the same sequencing the START pass settled for the inbox chips.
 */
function StatsStrip({
  stats,
  loading,
  truncated,
  cap,
}: {
  stats: Stats;
  loading: boolean;
  truncated: boolean;
  cap: number;
}) {
  /**
   * EXACT EQUALITY, AND MEASUREMENT IS WHY — not `>=`.
   *
   * A clamped array lands EXACTLY on the cap; a type the cap does not bind
   * comes back past it. Measured live with `?type=kg_entity`: `kg_entity`
   * returns 312 against a `per_type_cap` of 200, so a `>=` test called it
   * truncated and printed `312 of —` for what is very likely the true total.
   * `=== cap` calls exactly the clamped types, and the server's own
   * `truncated` flag is the second half: without it, a store that happens to
   * hold exactly 200 of something would be reported as unknowable.
   */
  const clamped = (n: number) => truncated && n === cap;
  const items: Array<{ label: string; value: number; capped: boolean }> = [
    // A total over clamped types is itself a page size, which is what the
    // server's `truncated` flag says.
    { label: "Total nodes", value: stats.total, capped: truncated },
    { label: "Edges", value: stats.edges, capped: truncated },
    ...(Object.keys(stats.byType) as KnowledgeNodeType[]).map((t) => ({
      label: TYPE_LABEL[t],
      value: stats.byType[t],
      capped: clamped(stats.byType[t]),
    })),
  ];

  return (
    <div className="flex flex-wrap items-stretch gap-3">
      {items.map((it) => (
        <div
          key={it.label}
          className="min-w-56 rounded-sm border border-border-subtle bg-surface px-3 py-2"
        >
          <div className="case-label text-2xs tracking-wider text-tertiary">
            {it.label}
          </div>
          <div
            className="mt-0.5 font-mono text-lg text-fg"
            title={
              it.capped
                ? `Showing ${it.value.toLocaleString()} — the query is capped at ${cap} per type, so the total is unknown`
                : undefined
            }
          >
            {loading
              ? UNKNOWN_TOTAL
              : it.capped
                ? `${it.value.toLocaleString()} of ${UNKNOWN_TOTAL}`
                : it.value.toLocaleString()}
          </div>
        </div>
      ))}
      {truncated && !loading && (
        <div className="flex items-center px-3 case-label text-2xs tracking-wider text-warning">
          truncated
        </div>
      )}
    </div>
  );
}

