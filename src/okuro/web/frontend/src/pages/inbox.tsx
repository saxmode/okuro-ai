import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { Inbox } from "lucide-react";
import { Segmented } from "@/components/ui/segmented";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { FacetBar } from "@/components/ui/facet-bar";
import { PageHeader } from "@/components/shell/page-header";
import { InboxRow, KIND_LABEL, KIND_ORDER } from "@/components/inbox/InboxRow";
import { useInboxDispose } from "@/components/inbox/use-inbox-dispose";
import { inboxApi, type InboxKind, type InboxState } from "@/lib/inbox-api";

// Sentinel project label for inbox rows with no project association. The
// FacetBar treats null/"" as "skip", so we coerce null → this token to keep
// unscoped items filterable as an explicit facet value.
const NO_PROJECT = "—";

// Minimal shape FacetBar derives chips from. Carries an index signature so it
// satisfies FacetBar's `Record<string, unknown>` constraint (InboxItem does
// not). Project is the only facet field; coerced from null → NO_PROJECT.
type ProjectFacetItem = { project: string; [k: string]: unknown };

/**
 * /inbox — unified, ranked, deduped attention surface.
 *
 * Consumes the live backend (okuro.sense.inbox): every actionable item —
 * continuations, tasks, signals, research, reminders, forgotten threads —
 * projected into one queue ordered by salience DESC. Per-row disposition
 * (act / defer / dismiss) writes back via POST /api/inbox/{id}/dispose.
 *
 * Additive surface — runs alongside the legacy Todos / Signals / Reminders
 * tabs until they are retired in a later step.
 */

// The two surfacing partitions the page toggles between.
type InboxView = Extract<InboxState, "surfaced" | "staging">;

const VIEW_OPTIONS: ReadonlyArray<{ value: InboxView; label: string }> = [
  { value: "surfaced", label: "Inbox" },
  { value: "staging", label: "Staging" },
];

export function InboxPage() {
  const [searchParams] = useSearchParams();
  const [view, setView] = useState<InboxView>("surfaced");
  const [kindFilter, setKindFilter] = useState<InboxKind | null>(null);
  // Project facet, derived-from-data + filtered client-side. Seeded once from
  // a ?project=<slug> query param so deep links (e.g. the task-detail
  // "View all" link) land pre-filtered.
  const [projectFilter, setProjectFilter] = useState<string | null>(
    () => searchParams.get("project"),
  );

  const { data, isLoading } = useQuery({
    queryKey: ["inbox", view, kindFilter],
    queryFn: () =>
      inboxApi.list({ kind: kindFilter ?? undefined, state: view, limit: 50 }),
  });

  // Project filter is applied client-side over the already-loaded (kind +
  // view filtered) rows so kind AND project compose without a refetch.
  const rawItems = data?.inbox ?? [];
  const facetItems = useMemo<ProjectFacetItem[]>(
    () =>
      rawItems.map((it) => ({
        ...it,
        project: it.project ?? NO_PROJECT,
      })),
    [rawItems],
  );
  const items = useMemo(
    () =>
      projectFilter == null
        ? rawItems
        : rawItems.filter((it) => (it.project ?? NO_PROJECT) === projectFilter),
    [rawItems, projectFilter],
  );

  // Counts per kind for the filter chips. Computed from the unfiltered
  // (kind="All") fetch for the active view so the badges stay stable while
  // a facet is active.
  const { data: allData } = useQuery({
    queryKey: ["inbox", view, null],
    queryFn: () => inboxApi.list({ state: view, limit: 50 }),
  });
  const counts = useMemo(() => {
    const acc: Record<string, number> = {};
    for (const it of allData?.inbox ?? []) {
      acc[it.kind] = (acc[it.kind] ?? 0) + 1;
    }
    return acc;
  }, [allData]);

  const totalCount = allData?.inbox?.length ?? 0;

  const dispose = useInboxDispose();

  const kindOptions = useMemo(
    () => [
      { value: null as InboxKind | null, label: "All", count: totalCount },
      ...KIND_ORDER.map((k) => ({
        value: k as InboxKind | null,
        label: KIND_LABEL[k],
        count: counts[k] ?? 0,
      })),
    ],
    [counts, totalCount],
  );

  // Best salience per kind among the rows actually on screen. The row's bar
  // fills against its own kind's peak, because salience is not comparable
  // across kinds (ceilings differ ~35x by TYPE_WEIGHT/GRAVITY). Recomputed
  // from the filtered list, so the bar always describes what is in view.
  const peerMaxByKind = useMemo(() => {
    const peaks: Partial<Record<InboxKind, number>> = {};
    for (const it of items) {
      const best = peaks[it.kind];
      if (best === undefined || it.salience > best) peaks[it.kind] = it.salience;
    }
    return peaks;
  }, [items]);

  const subtitle =
    view === "staging"
      ? "below the bar — staged items not yet surfaced"
      : "One ranked queue — the high-value items that cleared the surfacing bar";

  return (
    <div className="page-shell space-y-8">
      <PageHeader title="INBOX" subtitle={subtitle} />

      <Segmented<InboxView>
        ariaLabel="Switch between the surfaced Inbox and the Staging tray"
        value={view}
        onChange={setView}
        options={VIEW_OPTIONS}
      />

      <Segmented<InboxKind | null>
        ariaLabel="Filter inbox by kind"
        value={kindFilter}
        onChange={setKindFilter}
        options={kindOptions}
      />

      <FacetBar<ProjectFacetItem>
        items={facetItems}
        groups={[{ label: "Project", field: "project" }]}
        active={projectFilter == null ? {} : { project: projectFilter }}
        onChange={(next) => setProjectFilter(next.project ?? null)}
      />

      <SectionLabel>
        {isLoading ? "Loading…" : `${items.length} ranked item${items.length === 1 ? "" : "s"}`}
      </SectionLabel>

      {isLoading ? (
        <LoadingSkeleton lines={6} />
      ) : items.length === 0 ? (
        <EmptyState
          icon={<Inbox className="h-10 w-10" />}
          title={view === "staging" ? "Staging empty" : "Inbox clear"}
          description={
            view === "staging"
              ? "Nothing is waiting below the bar. New items land here before the surfacing gate promotes the high-value ones to the Inbox."
              : "Nothing needs your attention right now. High-value signals, tasks, and continuations surface here as the system finds them."
          }
        />
      ) : (
        <div className="space-y-1" role="list" aria-label="Inbox items">
          {items.map((item) => (
            <InboxRow
              key={item.id}
              item={item}
              peerMax={peerMaxByKind[item.kind]}
              onDispose={(action) => dispose.mutate({ id: item.id, action })}
              disabled={dispose.isPending}
            />
          ))}
        </div>
      )}
    </div>
  );
}
