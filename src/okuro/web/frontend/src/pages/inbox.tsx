import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { Inbox } from "lucide-react";
import { Segmented } from "@/components/ui/segmented";
import { BandFilter, type BandFilterChip } from "@/components/ui/band-filter";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { FacetBar } from "@/components/ui/facet-bar";
import { InboxRow, KIND_LABEL, KIND_ORDER } from "@/components/inbox/InboxRow";
import { useInboxDispose } from "@/components/inbox/use-inbox-dispose";
import { inboxApi, type InboxKind, type InboxState } from "@/lib/inbox-api";
import { sectionSlugs } from "@/shell/views/sections";
import type { LeafViewProps } from "@/shell/views/registry";
import { useSectionTitle } from "@/shell/components/PageTitle";

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

/** Rows the LIST renders. A ranked queue is read from the top; 50 is plenty. */
const LIST_LIMIT = 50;

/**
 * Rows the COUNTING fetch asks for, and why it is not the same number.
 *
 * R7 (372ccdb2) — "a capped list shows the TRUE TOTAL, never the cap; unknown
 * values render as a dash". The counts used to come from a `limit=50` fetch,
 * so every chip described the first fifty rows rather than the partition.
 * Measured live on 2026-09-15, kit `standard`:
 *
 *     GET /api/inbox?state=surfaced&limit=50   50 rows, 7 kinds
 *     GET /api/inbox?state=surfaced&limit=200  60 rows, 9 kinds
 *
 * So `All` read 50 against a real 60, `Signal` read 1 against 5, `Reminder` 9
 * against 11, and `Research` and `Forgotten` read **0 while the partition held
 * 3 and 1** — those kinds were cut off below the cap, so the chips stated as
 * fact that there was nothing there.
 *
 * 200 IS THE SERVER'S OWN CEILING, not a guess: `_LIMIT_CAP` in
 * `okuro/sense/inbox/__init__.py` clamps silently, measured — `limit=201` and
 * `limit=500` both answer 200 rows. So a fetch that comes back with exactly
 * 200 rows may be complete or may be truncated, and the honest reading of an
 * ambiguous number is that it is UNKNOWN. That is the dash, and STAGING is
 * live proof it fires: 200 rows at `limit=200`.
 *
 * WHAT THIS IS NOT. It is not the real fix. The real fix is one count query on
 * the server — `SELECT kind, COUNT(*) … GROUP BY kind` — which would give true
 * totals at every size AND delete this second fetch. Two costs argue for it and
 * both are measured: the endpoint runs one `inbox_detail` lookup per row, so
 * this asks for 200 detail resolutions to render nine numbers (3 ms today, but
 * it scales with the table); and `log_impression` writes one row per GET, so
 * the counting fetch already tells `inbox_impressions` that 200 items were
 * "seen" when 50 were rendered. That is a backend change and it is the owner's
 * call — carried as a question, not taken here.
 */
const COUNT_LIMIT = 200;

const VIEW_OPTIONS: ReadonlyArray<{ value: InboxView; label: string }> = [
  { value: "surfaced", label: "Inbox" },
  { value: "staging", label: "Staging" },
];

/**
 * THE PARTITION IS A SECTION, so it lives in `?view=` and not in `useState`.
 *
 * Resolved from the declared list rather than pinned, the rule
 * `mounts/work-tasks.tsx:46` states: inserting a section above STAGING must not
 * silently start rendering the other partition.
 */
const SECTION_OF: Record<InboxView, number> = {
  surfaced: Math.max(0, sectionSlugs("start", "inbox").indexOf("surfaced")),
  staging: Math.max(0, sectionSlugs("start", "inbox").indexOf("staging")),
};
const VIEW_OF_SECTION = (index: number): InboxView =>
  index === SECTION_OF.staging ? "staging" : "surfaced";

/**
 * THREE SUB-STATES, ALL THREE ADDRESSABLE — ruled S1/S2, 2026-09-15.
 *
 * Before this pass all three lived in `useState`: the Inbox/Staging partition,
 * the kind facet, and the project facet — and `?project=` was read ONCE on
 * mount and never written back, so a deep link worked and then the URL lay
 * about what was on screen from the first chip click onward.
 *
 *   partition -> `?view=surfaced|staging`, because it is a different SET of
 *                rows, which is what a section is. The prop comes from the
 *                router; `onSelectView` navigates.
 *   kind      -> `?kind=<kind>`, a facet over one set.
 *   project   -> `?project=<slug>`, likewise, and already inbound-only today.
 *
 * `onSelectView` IS ABSENT OUTSIDE THE SHELL and for a pane on its way out
 * (`TopicBar.tsx:429`), so the partition keeps a local fallback. Without it
 * `?embed=1`, `/onboarding` and every unit test would render a control that
 * does nothing — the same reason `pane-active`'s default is `true`.
 *
 * FACETS WRITE WITH `replace`, the section pushes. Fifty chip clicks are not
 * fifty places you meant to come back to; a partition switch is.
 */
export function InboxPage({ view: section, onSelectView }: Partial<LeafViewProps> = {}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [localSection, setLocalSection] = useState(0);
  const view = VIEW_OF_SECTION(section ?? localSection);
  const setView = (next: InboxView) => {
    const index = SECTION_OF[next];
    if (onSelectView) onSelectView(index);
    else setLocalSection(index);
  };

  /** Merge one facet into the query. Never replaces the whole string — that is
   *  the mistake `work-gantt.tsx:247` is the enumerated instance of (p2 Q2). */
  const setFacet = (key: "kind" | "project", value: string | null) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value == null) next.delete(key);
        else next.set(key, value);
        return next;
      },
      { replace: true },
    );
  };

  // A `?kind=` the vocabulary does not contain is IGNORED rather than sent to
  // the server. An unknown kind would refetch, match nothing, and render an
  // empty queue that looks like an empty inbox — a typo reading as news.
  const rawKind = searchParams.get("kind");
  const kindFilter: InboxKind | null =
    rawKind && (KIND_ORDER as string[]).includes(rawKind)
      ? (rawKind as InboxKind)
      : null;
  const setKindFilter = (next: InboxKind | null) => setFacet("kind", next);

  const projectFilter = searchParams.get("project");
  const setProjectFilter = (next: string | null) => setFacet("project", next);

  const { data, isLoading } = useQuery({
    queryKey: ["inbox", view, kindFilter],
    queryFn: () =>
      inboxApi.list({
        kind: kindFilter ?? undefined,
        state: view,
        limit: LIST_LIMIT,
      }),
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
    queryKey: ["inbox", view, null, COUNT_LIMIT],
    queryFn: () => inboxApi.list({ state: view, limit: COUNT_LIMIT }),
  });
  const counts = useMemo(() => {
    const acc: Record<string, number> = {};
    for (const it of allData?.inbox ?? []) {
      acc[it.kind] = (acc[it.kind] ?? 0) + 1;
    }
    return acc;
  }, [allData]);

  // R7. `null` = the number is not knowable from this fetch, which is true in
  // two cases: nothing has answered yet, and the answer came back at exactly
  // the server's ceiling so it may be truncated. Both render a dash rather
  // than a zero — a chip reading 0 is a claim, and it was a false one.
  const countsAreTotals =
    allData !== undefined && allData.inbox.length < COUNT_LIMIT;
  const totalCount = countsAreTotals ? allData.inbox.length : null;
  const countFor = (kind: InboxKind): number | null =>
    countsAreTotals ? (counts[kind] ?? 0) : null;

  const dispose = useInboxDispose();

  const kindOptions = useMemo(
    () => [
      { value: null as InboxKind | null, label: "All", count: totalCount },
      ...KIND_ORDER.map((k) => ({
        value: k as InboxKind | null,
        label: KIND_LABEL[k],
        count: countFor(k),
      })),
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- countFor closes
    // over `counts` and `countsAreTotals`, both listed.
    [counts, countsAreTotals, totalCount],
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

  /**
   * What the list is showing, which is NOT the same as what the partition
   * holds. The list asks for 50 and the surfaced partition has 60, so "50
   * ranked items" was the cap printed as a quantity — the same R7 fault as
   * the chips, one line lower. The total is only quoted when it IS the scope
   * on screen: a project facet filters client-side over the fetched rows, so
   * no server total describes it.
   */
  const listCapped = rawItems.length >= LIST_LIMIT;
  const scopeTotal =
    projectFilter != null ? null : kindFilter != null ? countFor(kindFilter) : totalCount;
  const listLabel = isLoading
    ? "Loading…"
    : listCapped && typeof scopeTotal === "number"
      ? `${items.length} of ${scopeTotal} ranked items`
      : `${items.length} ranked item${items.length === 1 ? "" : "s"}`;

  const subtitle =
    view === "staging"
      ? "below the bar — staged items not yet surfaced"
      : "One ranked queue — the high-value items that cleared the surfacing bar";

  /**
   * THE TWO FILTER GROUPS ARE ONE ITEM NOW — D7, the owner 2026-09-17.
   *
   * *"the filter groups COLLAPSE into ONE item labelled 'Filter'; on click it
   * takes the full width and shows all filter elements; on collapse only the
   * ACTIVE filters stay visible."*
   *
   * WHAT IT COST BEFORE, measured at 1366: these two `Segmented` groups laid
   * out 1085.2px of controls into an 840px band, and `.c-band{overflow:clip}`
   * simply removed 245.2px of filters with nothing on screen saying so. R7's
   * shrink stopped the clip by making them wrap, which then spent the TITLE —
   * the heading was offered 60.53px for a 95px word. Neither is a filter
   * problem; both are ten facets asking for a row that does not exist.
   *
   * A CHIP IS SHOWN ONLY WHERE THE FACET IS NOT ITS DEFAULT, which is the
   * difference between "what is filtered" and "what is selected". `surfaced`
   * and `All` are the unfiltered queue — printing them as active filters would
   * make the collapsed item claim two filters on a page with none, and the
   * whole point of collapsing is that the item says what is on.
   */
  const activeChips = useMemo<BandFilterChip[]>(() => {
    const out: BandFilterChip[] = [];
    if (view !== "surfaced") {
      out.push({
        key: "view",
        label: VIEW_OPTIONS.find((o) => o.value === view)?.label ?? view,
      });
    }
    if (kindFilter) out.push({ key: "kind", label: KIND_LABEL[kindFilter] });
    return out;
  }, [view, kindFilter]);

  /* MEMOISED HERE BECAUSE THE DEPENDENCIES LIVE HERE — `useSectionTitle` keys
     on the slot's identity and deliberately does not guess at a dependency list
     for state it cannot see (`PageTitle.tsx`). The disclosure's OPEN state is
     deliberately NOT in this list: it lives inside `BandFilter`, so opening the
     panel is a local re-render rather than a republished slot. */
  const header = useMemo(
    () => ({
      actions: (
        <BandFilter chips={activeChips} ariaLabel="Inbox filters">
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
        </BandFilter>
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [view, kindFilter, kindOptions, activeChips],
  );
  useSectionTitle(header);

  return (
    <div className="page-shell space-y-8">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">Inbox</h1>` above this pane, so the page's own
          PageHeader h1 is gone. The subtitle stayed: it is not decoration, it
          names which PARTITION is on screen and changes with it, so it moves
          down here as the block's lead and keeps its job. `text-fg-muted`
          rather than the `text-tertiary` PageHeader used — that token is the
          one AA failure on this leaf (kit todo c581c9b2) and re-homing the
          line was the chance to stop feeding it. */}
      <p className="type-small text-fg-muted">{subtitle}</p>

      {/* THE VIEW SWITCH AND THE KIND FILTER ARE ON THE PLATE — see the memo
          above. The FACET BAR stays here: it carries per-project counts off
          the loaded set and is closer to a reading of the list than to a
          chrome control. Flagged for A-2. */}
      <FacetBar<ProjectFacetItem>
        items={facetItems}
        groups={[{ label: "Project", field: "project" }]}
        active={projectFilter == null ? {} : { project: projectFilter }}
        onChange={(next) => setProjectFilter(next.project ?? null)}
      />

      <SectionLabel>{listLabel}</SectionLabel>

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
