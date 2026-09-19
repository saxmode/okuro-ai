import { useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { Search, X } from "lucide-react";
import { Segmented } from "@/components/ui/segmented";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/empty-state";
import { SectionLabel } from "@/components/ui/section-label";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { FacetBar } from "@/components/ui/facet-bar";
import { useBrain } from "@/hooks/use-dashboard";
import { countLabel, totalOrUnknown } from "@/lib/capped-count";
import { sectionSlugs } from "@/shell/views/sections";
import { useSectionTitle } from "@/shell/components/PageTitle";
import type { LeafViewProps } from "@/shell/views/registry";
import { SessionsPanel } from "@/components/dashboard/sessions-panel";
import { ThoughtsPanel } from "@/components/dashboard/thoughts-panel";
import { MemoryPanel } from "@/components/dashboard/memory-panel";
import { ProgressPanel } from "@/components/dashboard/progress-panel";

/**
 * /brain — sessions + thoughts + memory + progress + facet filters.
 * Wave 2 W2.5 partial: facet chips on all tabs. URL-routable entity
 * detail pages deferred to Wave 3.
 *
 * R7 (372ccdb2) — WHAT THE HEADER PRINTED WAS THE CAP, NOT A COUNT. All four
 * arrays are `LIMIT 200` in the ONE query behind this page (`app.py:2751`,
 * four statements, all four capped), and the endpoint returns no total beside
 * them. So `200 sessions` was the ceiling worn as a quantity, and the truthful
 * statement is that the total is unknown — `200 of — sessions`. The rule is
 * shared, because p3 found the identical fault on KNOWLEDGE and LESSONS in the
 * same sweep; see `lib/capped-count.ts`.
 */
/** The `LIMIT` every array in `GET /api/brain` is fetched under. */
const BRAIN_CAP = 200;

/** The four sub-views, in the section list's own order. */
type BrainTab = "sessions" | "thoughts" | "memory" | "progress";

/**
 * THE FOUR TABS ARE SECTIONS, SO THEY LIVE IN `?view=` — Q6, ruled, and this
 * leaf is where the pattern is set for the other 30.
 *
 * BRAIN was the only leaf in the IA where both grammars were live at once, and
 * p3 measured both directions: `?tab=memory` selected Memory, while
 * `?view=thoughts` resolved to index 1 and did nothing, because the page read
 * `?tab=` and nothing read the index. `LEAF_SECTIONS["know/brain"]` already
 * held the right four names on the wrong mechanism.
 *
 * Resolved FROM the declared list rather than pinned to 0..3, the rule
 * `mounts/work-tasks.tsx:46` states: inserting a section must not silently
 * start rendering another panel.
 *
 * `?tab=` KEEPS WORKING AND THIS FILE DOES NOTHING TO EARN THAT.
 * `routes.ts:605` reads `params.get("view") ?? params.get("tab")`, so the alias
 * is class-scope — which is what makes this conversion safe to repeat.
 *
 * `?id=` SURVIVES A SECTION CHANGE: `App.tsx:248` hands the current search to
 * `pathWithView`, and `mergeSearch` drops only the incoming `view`.
 */
const TABS: readonly BrainTab[] = ["sessions", "thoughts", "memory", "progress"];
const BRAIN_SLUGS = sectionSlugs("know", "brain");
const SECTION_OF = (tab: BrainTab): number => Math.max(0, BRAIN_SLUGS.indexOf(tab));
const TAB_OF_SECTION = (index: number): BrainTab => {
  const slug = BRAIN_SLUGS[index];
  return (TABS.find((t) => t === slug) ?? "sessions") as BrainTab;
};

export function BrainPage({ view: section, onSelectView }: Partial<LeafViewProps> = {}) {
  const { data, isLoading } = useBrain();
  /**
   * `onSelectView` IS ABSENT OUTSIDE THE SHELL and for a pane on its way out
   * (`TopicBar.tsx:429`), so the section keeps a local fallback — without it
   * `?embed=1` and every unit test would render four tabs that do nothing.
   * Same reason `pane-active`'s default is `true`.
   */
  const [localSection, setLocalSection] = useState(0);
  const current = section ?? localSection;
  const tabValue = TAB_OF_SECTION(current);
  /**
   * ONE HISTORY ENTRY PER TAB CHANGE, AND THIS REF IS WHY.
   *
   * Radix `Tabs` called `onValueChange` TWICE for one mouse click, both in the
   * same tick, so two history entries landed for one change and the first Back
   * was a no-op. That control is GONE — A-1 step 3b replaced it with
   * `ui/segmented`, which the same measurement recorded firing ONCE on MODELS,
   * because a control published onto the plate cannot read the leaf's own React
   * context and Radix compound parts therefore cannot cross the channel.
   *
   * THE GUARD STAYS ANYWAY, and not out of caution: it is the general rule that
   * asking the router for the section it is already on is a no-op, which the
   * arrow keys of a radio group can now trigger as easily as a double-fire.
   *
   * The ref records what was LAST ASKED FOR rather than what is rendered, so a
   * repeat inside the same tick is a no-op; it re-syncs whenever the address
   * changes from outside (a deep link, Back, the topic bar's section row).
   */
  const asked = useRef(current);
  if (asked.current !== current) asked.current = current;
  const onTabChange = (next: string) => {
    const index = SECTION_OF(next as BrainTab);
    if (index === asked.current) return;
    asked.current = index;
    if (onSelectView) onSelectView(index);
    else setLocalSection(index);
  };
  const [searchParams] = useSearchParams();
  const autoOpenId = searchParams.get("id");
  const [search, setSearch] = useState("");

  /* ===================================================================
     THE SECTION ROW AND THE SEARCH BOX ARE ON THE PLATE NOW.
     ===================================================================
     They used to live in a `sticky top-0` bar INSIDE `.c-panes`, and that
     was the shape audit C4-3 names: `.c-panes{z-index:0}` is a containment
     boundary, so a leaf's sticky header pins BEHIND the plate rather than
     under the window. Three leaves shipped one — BRAIN, KNOWLEDGE and
     ASSETS. The plate is the sticky surface now, so the bar is gone rather
     than re-ranked.

     RADIX TABS COULD NOT MAKE THE TRIP, AND THAT IS A FACT ABOUT THE
     CHANNEL RATHER THAN ABOUT THIS LEAF. A published node is CREATED here
     and RENDERED by `PlateTitle`, which lives in `TopicBar`'s React tree;
     `createPortal` moves a DOM node, not a tree position, and context flows
     through the tree. `TabsList` and `TabsTrigger` read a provider that is
     no longer an ancestor. `ui/segmented` takes plain props, is already the
     sanctioned control (`pages/agents.tsx`, ruling D3), and firing ONCE per
     click is the very measurement the `asked` ref above was written from.

     `TabsContent` UNMOUNTED THE INACTIVE SECTION, so leaving a tab threw
     away whatever it held. `hidden` keeps all four mounted — affordable
     here because ONE `useBrain()` query feeds all four and no panel polls
     on its own. Same change, same reason, as AGENTS.

     MEMOISED HERE BECAUSE THE DEPENDENCIES LIVE HERE. `onTabChange` closes
     over `onSelectView` and the `asked` ref; the ref is stable and the prop
     is listed. */
  const header = useMemo(
    () => ({
      actions: (
        <>
          <Segmented
            ariaLabel="Brain section"
            value={tabValue}
            onChange={(v) => onTabChange(v as string)}
            options={TABS.map((t) => ({
              label: t.charAt(0).toUpperCase() + t.slice(1),
              value: t as string,
            }))}
          />
          <div className="relative w-40">
            {/* `c-band-pin` opts this glyph into the band's centring rule.
                It used to be reached by `.c-band-actions .absolute`, which
                caught EVERY absolutely positioned descendant a leaf handed
                over — audit C5-1. */}
            <Search className="c-band-pin absolute left-2.5 h-3.5 w-3.5 text-tertiary" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={`Search ${tabValue}…`}
              className="bg-surface pl-8 pr-8 text-sm"
              aria-label={`Search ${tabValue}`}
            />
            {search && (
              <button
                type="button"
                onClick={() => setSearch("")}
                aria-label="Clear search"
                className="c-band-pin absolute right-2 text-tertiary hover:text-fg-muted"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        </>
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tabValue, search, onSelectView],
  );
  useSectionTitle(header);

  return (
    <div className="page-shell space-y-8">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">Brain</h1>` above this pane, so the page's own
          PageHeader h1 is gone. The subtitle stays as the leaf's lead: it says
          what the four sections are FOR, which the one-word leaf label cannot. */}
      <p className="type-small text-fg-muted">
        What agents learned, thought, decided, and delivered
      </p>

        <div hidden={tabValue !== "sessions"}>
          <SessionsTab
            sessions={data?.sessions ?? []}
            loading={isLoading}
            search={search}
          />
        </div>

        <div hidden={tabValue !== "thoughts"}>
          <ThoughtsTab
            thoughts={data?.thoughts ?? []}
            loading={isLoading}
            search={search}
            autoOpenId={tabValue === "thoughts" ? autoOpenId : null}
          />
        </div>

        <div hidden={tabValue !== "memory"}>
          <MemoryTab
            memory={data?.memory ?? []}
            loading={isLoading}
            search={search}
          />
        </div>

        <div hidden={tabValue !== "progress"}>
          <ProgressTab
            progress={data?.progress ?? []}
            loading={isLoading}
            search={search}
          />
        </div>
    </div>
  );
}

type SessionItem = NonNullable<ReturnType<typeof useBrain>["data"]>["sessions"][number];
type ThoughtItem = NonNullable<ReturnType<typeof useBrain>["data"]>["thoughts"][number];
type MemoryItem = NonNullable<ReturnType<typeof useBrain>["data"]>["memory"][number];
type ProgressItem = NonNullable<ReturnType<typeof useBrain>["data"]>["progress"][number];

function SessionsTab({ sessions, loading, search }: { sessions: SessionItem[]; loading: boolean; search: string }) {
  const [active, setActive] = useState<Partial<Record<keyof SessionItem, string>>>({});
  const filtered = useMemo(
    () => applyFilter(sessions, active, search),
    [sessions, active, search],
  );
  return (
    <section className="space-y-3">
      <FacetBar
        items={sessions}
        groups={[
          { label: "Provider", field: "provider" },
          { label: "Project", field: "project" },
        ]}
        active={active}
        onChange={setActive}
      />
      <SectionLabel>
        {countLabel(filtered.length, totalOrUnknown(sessions, BRAIN_CAP), "sessions")}
      </SectionLabel>
      {loading ? (
        <LoadingSkeleton lines={4} />
      ) : filtered.length === 0 ? (
        <EmptyState title="No sessions match filter" />
      ) : (
        <SessionsPanel sessions={filtered} variant="timeline" />
      )}
    </section>
  );
}

function ThoughtsTab({
  thoughts,
  loading,
  search,
  autoOpenId,
}: {
  thoughts: ThoughtItem[];
  loading: boolean;
  search: string;
  autoOpenId: string | null;
}) {
  const [active, setActive] = useState<Partial<Record<keyof ThoughtItem, string>>>({});
  const filtered = useMemo(
    () => applyFilter(thoughts, active, search),
    [thoughts, active, search],
  );
  // If a deep-link asks for a specific thought, don't let facet/search
  // filters hide it — render against the full set when autoOpenId is set
  // and the target isn't in `filtered`.
  const targetInFiltered =
    autoOpenId != null && filtered.some((t) => t.id === autoOpenId);
  const displayed = autoOpenId && !targetInFiltered ? thoughts : filtered;
  return (
    <section className="space-y-3">
      <FacetBar
        items={thoughts}
        groups={[
          { label: "Project", field: "project" },
          { label: "Status", field: "status" },
        ]}
        active={active}
        onChange={setActive}
      />
      <SectionLabel>
        {countLabel(displayed.length, totalOrUnknown(thoughts, BRAIN_CAP), "thoughts")}
      </SectionLabel>
      {loading ? (
        <LoadingSkeleton lines={4} />
      ) : displayed.length === 0 ? (
        <EmptyState title="No thoughts match filter" />
      ) : (
        <ThoughtsPanel
          thoughts={displayed}
          variant="timeline"
          autoOpenId={autoOpenId}
        />
      )}
    </section>
  );
}

function MemoryTab({ memory, loading, search }: { memory: MemoryItem[]; loading: boolean; search: string }) {
  const [active, setActive] = useState<Partial<Record<keyof MemoryItem, string>>>({});
  const filtered = useMemo(
    () => applyFilter(memory, active, search),
    [memory, active, search],
  );
  return (
    <section className="space-y-3">
      <FacetBar
        items={memory}
        groups={[
          { label: "Project", field: "project" },
          { label: "Topic", field: "topic" },
        ]}
        active={active}
        onChange={setActive}
      />
      <SectionLabel>
        {countLabel(filtered.length, totalOrUnknown(memory, BRAIN_CAP), "memories")}
      </SectionLabel>
      {loading ? (
        <LoadingSkeleton lines={4} />
      ) : filtered.length === 0 ? (
        <EmptyState title="No memories match filter" />
      ) : (
        <MemoryPanel memories={filtered} variant="timeline" />
      )}
    </section>
  );
}

function ProgressTab({ progress, loading, search }: { progress: ProgressItem[]; loading: boolean; search: string }) {
  const [active, setActive] = useState<Partial<Record<keyof ProgressItem, string>>>({});
  const filtered = useMemo(
    () => applyFilter(progress, active, search),
    [progress, active, search],
  );
  return (
    <section className="space-y-3">
      <FacetBar
        items={progress}
        groups={[
          { label: "Project", field: "project" },
          { label: "Agent", field: "agent" },
          { label: "Status", field: "status" },
        ]}
        active={active}
        onChange={setActive}
      />
      <SectionLabel>
        {countLabel(filtered.length, totalOrUnknown(progress, BRAIN_CAP), "progress entries")}
      </SectionLabel>
      {loading ? (
        <LoadingSkeleton lines={4} />
      ) : filtered.length === 0 ? (
        <EmptyState title="No progress entries match filter" />
      ) : (
        <ProgressPanel entries={filtered} variant="timeline" />
      )}
    </section>
  );
}

function applyFilter<T extends Record<string, unknown>>(
  items: T[],
  active: Partial<Record<keyof T, string>>,
  search: string = "",
): T[] {
  const entries = Object.entries(active).filter(([, v]) => v != null && v !== "");
  const q = search.trim().toLowerCase();
  if (entries.length === 0 && !q) return items;
  return items.filter((item) => {
    if (!entries.every(([key, val]) => String(item[key as keyof T] ?? "") === val)) {
      return false;
    }
    if (!q) return true;
    return Object.values(item).some((v) => {
      if (v == null) return false;
      if (typeof v === "object") {
        try {
          return JSON.stringify(v).toLowerCase().includes(q);
        } catch {
          return false;
        }
      }
      return String(v).toLowerCase().includes(q);
    });
  });
}
