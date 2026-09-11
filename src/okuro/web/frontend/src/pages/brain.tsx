import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { Search, X } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/empty-state";
import { SectionLabel } from "@/components/ui/section-label";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { FacetBar } from "@/components/ui/facet-bar";
import { useBrain } from "@/hooks/use-dashboard";
import { useUrlTab } from "@/hooks/use-url-tab";
import { SessionsPanel } from "@/components/dashboard/sessions-panel";
import { ThoughtsPanel } from "@/components/dashboard/thoughts-panel";
import { MemoryPanel } from "@/components/dashboard/memory-panel";
import { ProgressPanel } from "@/components/dashboard/progress-panel";
import { PageHeader } from "@/components/shell/page-header";

/**
 * /brain — sessions + thoughts + memory + progress + facet filters.
 * Wave 2 W2.5 partial: facet chips on all tabs. URL-routable entity
 * detail pages deferred to Wave 3.
 */
export function BrainPage() {
  const { data, isLoading } = useBrain();
  const tab = useUrlTab("sessions");
  const [searchParams] = useSearchParams();
  const autoOpenId = searchParams.get("id");
  const [search, setSearch] = useState("");

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Brain"
        subtitle="What agents learned, thought, decided, and delivered"
      />

      <Tabs value={tab.value} onValueChange={tab.onValueChange}>
        {/* Sticky bar: tabs (left) + search (right). Sticks to the top of
            <main> so both remain visible while the page content scrolls. */}
        <div className="sticky top-0 z-10 -mx-10 flex items-end justify-between gap-4 bg-surface/95 px-10 pt-1 pb-px backdrop-blur-sm">
          <TabsList>
            <TabsTrigger value="sessions">Sessions</TabsTrigger>
            <TabsTrigger value="thoughts">Thoughts</TabsTrigger>
            <TabsTrigger value="memory">Memory</TabsTrigger>
            <TabsTrigger value="progress">Progress</TabsTrigger>
          </TabsList>
          <div className="relative w-64 shrink-0 pb-1">
            <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={`Search ${tab.value}…`}
              className="bg-surface pl-8 pr-8 text-sm"
              aria-label={`Search ${tab.value}`}
            />
            {search && (
              <button
                type="button"
                onClick={() => setSearch("")}
                aria-label="Clear search"
                className="absolute right-2 top-2 text-tertiary hover:text-fg-muted"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>

        <TabsContent value="sessions">
          <SessionsTab
            sessions={data?.sessions ?? []}
            loading={isLoading}
            search={search}
          />
        </TabsContent>

        <TabsContent value="thoughts">
          <ThoughtsTab
            thoughts={data?.thoughts ?? []}
            loading={isLoading}
            search={search}
            autoOpenId={tab.value === "thoughts" ? autoOpenId : null}
          />
        </TabsContent>

        <TabsContent value="memory">
          <MemoryTab
            memory={data?.memory ?? []}
            loading={isLoading}
            search={search}
          />
        </TabsContent>

        <TabsContent value="progress">
          <ProgressTab
            progress={data?.progress ?? []}
            loading={isLoading}
            search={search}
          />
        </TabsContent>
      </Tabs>
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
        {filtered.length}
        {filtered.length !== sessions.length ? ` / ${sessions.length}` : ""}{" "}
        sessions
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
        {displayed.length}
        {displayed.length !== thoughts.length ? ` / ${thoughts.length}` : ""}{" "}
        thoughts
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
        {filtered.length}
        {filtered.length !== memory.length ? ` / ${memory.length}` : ""}{" "}
        memories
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
        {filtered.length}
        {filtered.length !== progress.length ? ` / ${progress.length}` : ""}{" "}
        progress entries
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
