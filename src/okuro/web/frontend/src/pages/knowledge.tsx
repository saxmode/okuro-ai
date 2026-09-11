import { useMemo, useState } from "react";
import { Network, Grid3X3, Table as TableIcon, Clock, Loader2, Boxes, MapPin, SlidersHorizontal } from "lucide-react";
import { SidePanel, MobilePanelTrigger } from "@/components/ui/side-panel";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/shell/page-header";
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
export function KnowledgePage() {
  const [activeTab, setActiveTab] = useState<"gallery" | "table" | "graph" | "timeline">(
    "gallery",
  );
  const [hideOrphans, setHideOrphans] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filters, setFilters] = useState<FilterState>({});
  const [tourOpen, setTourOpen] = useState(false);
  const [facetsOpen, setFacetsOpen] = useState(false);

  const types = filters.entityType ? [filters.entityType] : undefined;
  const { data, isLoading, isError, error } = useKnowledgeGraph({
    per_type_cap: 200,
    hide_orphans: hideOrphans,
    types,
    topic: filters.topic,
    project: filters.project,
    status: filters.status,
    confidence_min: filters.confidence_min,
    confidence_max: filters.confidence_max,
  });

  const stats = useMemo(() => buildStats(data?.nodes ?? [], data?.edges?.length ?? 0), [data]);

  return (
    <div className="page-shell space-y-6">
      <PageHeader
        title="Knowledge"
        subtitle="Everything agents learned, thought, decided, delivered — as one filterable graph"
      />

      <StatsStrip stats={stats} loading={isLoading} truncated={data?.truncated ?? false} />

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
      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as typeof activeTab)}>
        <div className="sticky top-0 z-10 -mx-10 flex items-end justify-between gap-4 bg-surface/95 px-10 pt-1 pb-px backdrop-blur-sm">
          <TabsList>
            <TabsTrigger value="gallery" className="gap-1.5">
              <Grid3X3 className="h-3.5 w-3.5" />
              Gallery
            </TabsTrigger>
            <TabsTrigger value="table" className="gap-1.5">
              <TableIcon className="h-3.5 w-3.5" />
              Table
            </TabsTrigger>
            <TabsTrigger value="graph" className="gap-1.5">
              <Network className="h-3.5 w-3.5" />
              Graph
            </TabsTrigger>
            <TabsTrigger value="timeline" className="gap-1.5">
              <Clock className="h-3.5 w-3.5" />
              Timeline
            </TabsTrigger>
          </TabsList>

          <div className="flex items-center gap-3 pb-1 text-xs text-tertiary">
            {activeTab === "graph" && (
              <button
                type="button"
                onClick={() => setTourOpen((v) => !v)}
                className={cn(
                  "flex items-center gap-1.5 rounded-sm border px-2 py-0.5 transition-colors",
                  tourOpen
                    ? "border-accent text-accent"
                    : "border-border-subtle hover:border-fg-muted hover:text-fg",
                )}
                title={tourOpen ? "Hide code tour" : "Show code tour"}
              >
                <MapPin className="h-3 w-3" />
                tour
              </button>
            )}
            <label className="flex cursor-pointer items-center gap-1.5 select-none">
              <input
                type="checkbox"
                checked={hideOrphans}
                onChange={(e) => setHideOrphans(e.target.checked)}
                className="h-3 w-3 accent-accent"
              />
              hide orphans
            </label>
            {isLoading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          </div>
        </div>

        <TabsContent value="gallery" className="pt-4">
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
        </TabsContent>

        <TabsContent value="table" className="pt-4">
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
        </TabsContent>

        <TabsContent value="graph" className="pt-4">
          {isError ? (
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
        </TabsContent>

        <TabsContent value="timeline" className="pt-4">
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
        </TabsContent>
      </Tabs>
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

function StatsStrip({
  stats,
  loading,
  truncated,
}: {
  stats: Stats;
  loading: boolean;
  truncated: boolean;
}) {
  const items: Array<{ label: string; value: number }> = [
    { label: "Total nodes", value: stats.total },
    { label: "Edges", value: stats.edges },
    ...(Object.keys(stats.byType) as KnowledgeNodeType[]).map((t) => ({
      label: TYPE_LABEL[t],
      value: stats.byType[t],
    })),
  ];

  return (
    <div className="flex flex-wrap items-stretch gap-3">
      {items.map((it) => (
        <div
          key={it.label}
          className="min-w-[14rem] rounded-sm border border-border-subtle bg-surface px-3 py-2"
        >
          <div className="text-2xs uppercase tracking-wider text-tertiary">
            {it.label}
          </div>
          <div className="mt-0.5 font-mono text-lg text-fg">
            {loading ? "—" : it.value.toLocaleString()}
          </div>
        </div>
      ))}
      {truncated && !loading && (
        <div className="flex items-center px-3 text-2xs uppercase tracking-wider text-warning">
          truncated
        </div>
      )}
    </div>
  );
}

