import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Segmented } from "@/components/ui/segmented";
import { SectionLabel } from "@/components/ui/section-label";
import { StatusBadge } from "@/components/ui/status-badge";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { MetricCard } from "@/components/ui/metric-card";
import { DaemonJobsPanel } from "@/components/schedule/daemon-jobs-panel";
import { SystemPanel } from "@/components/dashboard/system-panel";
import { useSystemStatus } from "@/hooks/use-tasks";
import { formatDuration } from "@/lib/format";
import { parseApiDate } from "@/lib/format";
import { usePaneInterval } from "@/lib/pane-active";
import { sectionSlugs } from "@/shell/views/sections";
import type { LeafViewProps } from "@/shell/views/registry";
import { useSectionTitle } from "@/shell/components/PageTitle";

interface DoctorCheck {
  name: string;
  status: "ok" | "warn" | "fail";
  message: string;
  fix: string | null;
}

interface DoctorResult {
  checks: DoctorCheck[];
  passed: number;
  warned: number;
  failed: number;
}

interface StorageMount {
  path: string;
  device?: string;
  size_gb: number;
  used_gb: number;
  free_gb: number;
  usage_percent: number;
  alert?: boolean;
}

interface StorageResult {
  mounts: StorageMount[];
  alerts?: Array<{ mount: string; severity: string; message: string }>;
  raid?: {
    device: string;
    status: string;
    type: string;
    active_drives: number;
    total_drives: number;
  };
}

const CORE_CHECKS = new Set(["Python", "Database", "Embeddings"]);
const AGENT_CHECKS = new Set(["Providers", "GPU"]);

type CheckGroup = "CORE" | "AGENTS" | "SCHEDULES" | "OTHER";

/**
 * The five tabs, named as the shell names its sections, and the index each one
 * is. Derived from `sectionSlugs` rather than retyped: a rename in
 * `sections.ts` then moves both halves or fails the build, instead of leaving
 * the page pointing at an index that means something else.
 */
type HealthTab = "checks" | "machine" | "storage" | "schedules" | "cortex";

const HEALTH_SLUGS = sectionSlugs("system", "health");
const SECTION_OF: Record<HealthTab, number> = {
  checks: Math.max(0, HEALTH_SLUGS.indexOf("checks")),
  machine: Math.max(0, HEALTH_SLUGS.indexOf("machine")),
  storage: Math.max(0, HEALTH_SLUGS.indexOf("storage")),
  schedules: Math.max(0, HEALTH_SLUGS.indexOf("schedules")),
  cortex: Math.max(0, HEALTH_SLUGS.indexOf("cortex")),
};
const TAB_OF_SECTION = (index: number): HealthTab =>
  (HEALTH_SLUGS[index] as HealthTab | undefined) ?? "checks";

function groupCheck(name: string): CheckGroup {
  if (CORE_CHECKS.has(name)) return "CORE";
  if (AGENT_CHECKS.has(name)) return "AGENTS";
  if (name.toLowerCase().includes("schedul")) return "SCHEDULES";
  return "OTHER";
}

/**
 * THE FIVE TABS ARE FIVE ADDRESSES — Q-L1, the cheapest real conversion in the
 * topic after MODELS.
 *
 * `sections.ts` already declared five sections in the page's own rendered
 * order, and the counts already matched; only the mechanism was missing. What
 * it cost before, measured: `/system/health?view=storage` left the URL
 * untouched with the Services tab still active, and so did `?view=machine`.
 *
 * `ui/segmented`, not `ui/tabs`, and that is not taste. Radix `Tabs` owns its
 * own active value and UNMOUNTS inactive content, so it cannot be driven from
 * the shell's section index without fighting it (inventory §3). `Segmented` is
 * controlled by construction and MODELS already runs on it.
 *
 * TWO SPELLINGS CHANGED AND BOTH OLD ONES STILL RESOLVE. `services` became
 * CHECKS because SYSTEM has a SERVICES *leaf* two labels along the same rail
 * and the tab is full of doctor checks; `system` became MACHINE because the
 * topic is already called SYSTEM. `SECTION_ALIASES` in `shell/views/sections`
 * maps the two old `?tab=` values, so a live bookmark lands where it meant to
 * rather than silently on the default.
 */
export function HealthPage({
  view: section,
  onSelectView,
}: Partial<LeafViewProps> = {}) {
  /**
   * `onSelectView` IS ABSENT OUTSIDE THE SHELL and for a pane on its way out
   * (`TopicBar.tsx:429`), so the section keeps a local fallback — the same
   * reason MODELS has one and the same reason `pane-active` defaults to true.
   */
  const [localSection, setLocalSection] = useState(0);
  const mode = TAB_OF_SECTION(section ?? localSection);
  const setMode = (next: HealthTab) => {
    const index = SECTION_OF[next];
    if (onSelectView) onSelectView(index);
    else setLocalSection(index);
  };

  /* THE VIEW SWITCHER IS ON THE PLATE. It is this leaf's only control, and
     it was the first interactive row of the pane — under the blurred plate. */
  const header = useMemo(
    () => ({
      actions: (
        <Segmented<HealthTab>
          ariaLabel="Health view"
          value={mode}
          onChange={setMode}
          options={[
            { label: "Checks", value: "checks" },
            { label: "Machine", value: "machine" },
            { label: "Storage", value: "storage" },
            { label: "Schedules", value: "schedules" },
            { label: "Cortex", value: "cortex" },
          ]}
        />
      ),
    }),
    [mode],
  );
  useSectionTitle(header);

  return (
    <div className="page-shell space-y-8">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">Health</h1>` above this pane, so the page's own
          PageHeader h1 is gone. The subtitle survives as content because it
          states the question the page answers, which is what tells a reader
          why five unrelated tabs are one leaf. `text-fg-muted` rather than
          `PageHeader`'s `text-tertiary` — the standing AA failure, kit todo
          c581c9b2. */}
      <p className="type-small text-fg-muted">
        Services, hardware, storage, schedules — is anything broken?
      </p>

      {mode === "checks" && <ServicesTab />}
      {mode === "machine" && <SystemTab />}
      {mode === "storage" && <StorageTab />}
      {mode === "schedules" && <SchedulesTab />}
      {mode === "cortex" && <CortexHealthTab />}
    </div>
  );
}

function ServicesTab() {
  const { data: status } = useSystemStatus();
  const { data: doctor, isLoading } = useQuery({
    queryKey: ["doctor"],
    queryFn: () => api<DoctorResult>("/api/doctor"),
    // L4 / T9 — Law 3 keeps all five topic panes mounted, so an unguarded
    // interval polls from four topics away. HEALTH owns FOUR of them and is
    // the heaviest background cost in SYSTEM: doctor 30s, storage 60s, cortex
    // coverage 120s, cortex enrichment 30s. Measured before the guards.
    refetchInterval: usePaneInterval(30_000),
    staleTime: 10_000,
  });

  if (isLoading || !doctor) return <LoadingSkeleton lines={5} />;

  const grouped = doctor.checks.reduce<Record<CheckGroup, DoctorCheck[]>>(
    (acc, c) => {
      const g = groupCheck(c.name);
      (acc[g] ??= []).push(c);
      return acc;
    },
    { CORE: [], AGENTS: [], SCHEDULES: [], OTHER: [] },
  );

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-3 gap-3">
        <MetricCard label="OK" value={doctor.passed} />
        <MetricCard label="Warn" value={doctor.warned} accent={doctor.warned > 0} />
        <MetricCard label="Fail" value={doctor.failed} accent={doctor.failed > 0} />
      </div>

      {(["CORE", "AGENTS", "OTHER"] as const)
        .filter((g) => grouped[g] && grouped[g].length > 0)
        .map((g) => (
          <section key={g}>
            <SectionLabel className="mb-2">{g}</SectionLabel>
            <div className="rounded-md border border-border divide-y divide-border">
              {grouped[g].map((c) => (
                <CheckRow key={c.name} check={c} />
              ))}
            </div>
          </section>
        ))}

      {status && (
        <section>
          <SectionLabel className="mb-2">Orchestrator</SectionLabel>
          <div className="grid grid-cols-4 gap-3">
            <MetricCard label="Tasks" value={status.tasks_count} />
            <MetricCard label="Active" value={status.active_tasks} accent={status.active_tasks > 0} />
            <MetricCard label="Tools" value={status.tools_count} />
            <MetricCard label="Uptime" value={formatDuration(status.uptime_seconds)} />
          </div>
        </section>
      )}
    </div>
  );
}

function CheckRow({ check }: { check: DoctorCheck }) {
  const tone =
    check.status === "ok" ? "success" : check.status === "warn" ? "warning" : "error";
  return (
    <div className="flex items-center gap-3 px-3 py-2">
      <StatusBadge tone={tone} label={check.status} showDot />
      <span className="w-28 shrink-0 text-sm text-fg">{check.name}</span>
      <span className="flex-1 text-xs text-fg-muted">{check.message}</span>
      {check.fix && (
        <span className="text-2xs text-tertiary">{check.fix}</span>
      )}
    </div>
  );
}

function SystemTab() {
  return (
    <section className="space-y-3">
      <SectionLabel>CPU · RAM · GPU</SectionLabel>
      <SystemPanel />
    </section>
  );
}

function StorageTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["storage"],
    queryFn: () => api<StorageResult>("/api/storage"),
    refetchInterval: usePaneInterval(60_000),
    staleTime: 30_000,
  });

  if (isLoading) return <LoadingSkeleton lines={3} />;
  if (!data || !data.mounts || data.mounts.length === 0) {
    return <EmptyState title="No storage data" />;
  }

  return (
    <section className="space-y-2">
      <SectionLabel className="mb-2">Mounts</SectionLabel>
      <div className="space-y-2">
        {data.mounts.map((m) => (
          <MountRow key={m.path} mount={m} />
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Schedules tab — daemon recurring tasks
// ---------------------------------------------------------------------------

function SchedulesTab() {
  return <DaemonJobsPanel />;
}

// ---------------------------------------------------------------------------
// Cortex tab — sidecar coverage + enrichment log
// ---------------------------------------------------------------------------

interface CoverageProject {
  slug: string;
  path: string;
  eligible: number;
  covered: number;
  stale: number;
  pct: number;
  last_scan_age_s: number | null;
}

interface CoverageResult {
  projects: CoverageProject[];
  totals: { eligible: number; covered: number; stale: number; pct: number };
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

interface EnrichmentEvent {
  timestamp: string;
  file: string;
  old_purpose?: string;
  new_purpose?: string;
  provider?: string;
  model?: string;
  duration_s?: number;
}

function CortexHealthTab() {
  const coverage = useQuery({
    queryKey: ["cortex-coverage"],
    queryFn: () => api<CoverageResult>("/api/cortex/coverage"),
    refetchInterval: usePaneInterval(120_000),
    staleTime: 60_000,
  });

  const enrichment = useQuery({
    queryKey: ["cortex-enrichment"],
    queryFn: () =>
      api<{ events: EnrichmentEvent[]; count: number }>(
        "/api/cortex/enrichment?limit=50",
      ),
    refetchInterval: usePaneInterval(30_000),
    staleTime: 15_000,
  });

  return (
    <div className="space-y-6">
      {/* Sidecar coverage */}
      <section className="space-y-3">
        <SectionLabel className="mb-2">Sidecar coverage</SectionLabel>
        {coverage.isLoading && <LoadingSkeleton lines={3} />}
        {coverage.data && (
          <>
            <div className="grid grid-cols-4 gap-3">
              <MetricCard
                label="Overall"
                value={`${coverage.data.totals.pct}%`}
                accent={coverage.data.totals.pct >= 80}
              />
              <MetricCard
                label="Eligible files"
                value={coverage.data.totals.eligible}
              />
              <MetricCard
                label="With sidecar"
                value={coverage.data.totals.covered}
              />
              <MetricCard
                label="Stale entries"
                value={coverage.data.totals.stale}
              />
            </div>
            {coverage.data.projects.length > 0 ? (
              // MEASURED, NOT ASSUMED: at a 751.63px pane this table is 778px
              // wide, and `overflow-hidden` made the last 28px — the whole
              // Coverage column — unreachable with no scrollbar and no way to
              // get there. Same class as the Discover table the MODELS pass
              // fixed (1186px inside a 750px box), and the 204-state sweep
              // reads `paneScrollW == paneClientW` right past it, because a box
              // that hides its overflow reports none to its ancestor. Only the
              // per-element clip census sees it. Six columns, one of them a
              // filesystem path: a scroller is the answer the contract allows.
              <div className="overflow-x-auto rounded-md border border-border">
                <table className="w-full text-xs">
                  <thead className="border-b border-border bg-surface">
                    {/* `case-label`, not a literal `uppercase` — 0d37d05e. */}
                    <tr className="case-label text-left text-2xs tracking-wider text-tertiary">
                      <th className="px-3 py-2 font-medium">Project</th>
                      <th className="px-3 py-2 font-medium text-right">Eligible</th>
                      <th className="px-3 py-2 font-medium text-right">With sidecar</th>
                      <th className="px-3 py-2 font-medium text-right">Stale</th>
                      <th className="px-3 py-2 font-medium text-right">Last scan</th>
                      <th className="px-3 py-2 font-medium text-right">Coverage</th>
                    </tr>
                  </thead>
                  <tbody>
                    {coverage.data.projects.map((p) => (
                      <tr key={p.path} className="border-t border-border-subtle">
                        <td className="px-3 py-2">
                          <div className="font-mono text-fg">{p.slug}</div>
                          <div className="font-mono text-3xs text-tertiary truncate max-w-[360px]" title={p.path}>
                            {p.path}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-fg">{p.eligible}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-fg-muted">{p.covered}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-warning">{p.stale || ""}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-tertiary">{formatAge(p.last_scan_age_s)}</td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          <span
                            className={
                              p.pct >= 80
                                ? "text-success"
                                : p.pct >= 40
                                  ? "text-warning"
                                  : "text-tertiary"
                            }
                          >
                            {p.pct}%
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="No indexed roots" description="Register a project in Cortex to see coverage." />
            )}
          </>
        )}
      </section>

      {/* Enrichment log */}
      <section className="space-y-3">
        <SectionLabel className="mb-2">Enrichment log</SectionLabel>
        {enrichment.isLoading && <LoadingSkeleton lines={3} />}
        {enrichment.data && enrichment.data.events.length === 0 && (
          <EmptyState
            title="No enrichment events yet"
            description="Appears once the daemon regenerates AGENT_HEADER purposes via the LLM bridge."
          />
        )}
        {enrichment.data && enrichment.data.events.length > 0 && (
          <div className="overflow-hidden rounded-md border border-border">
            <ul className="divide-y divide-border-subtle">
              {enrichment.data.events.map((ev, i) => (
                <li key={`${ev.file}-${ev.timestamp}-${i}`} className="px-3 py-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="font-mono text-xs text-fg truncate" title={ev.file}>
                        {ev.file}
                      </div>
                      {ev.new_purpose && (
                        <div className="mt-0.5 line-clamp-2 text-2xs text-fg-muted">
                          {ev.new_purpose}
                        </div>
                      )}
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="text-2xs text-tertiary tabular-nums">
                        {parseApiDate(ev.timestamp).toLocaleString()}
                      </div>
                      <div className="text-3xs text-tertiary">
                        {ev.provider}
                        {ev.model ? ` · ${ev.model}` : ""}
                        {typeof ev.duration_s === "number" ? ` · ${ev.duration_s.toFixed(1)}s` : ""}
                      </div>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </div>
  );
}

function MountRow({ mount }: { mount: StorageMount }) {
  const pct = mount.usage_percent;
  const tone =
    pct >= 90 ? "error" : pct >= 75 ? "warning" : "success";
  const fill =
    pct >= 90 ? "bg-error" : pct >= 75 ? "bg-warning" : "bg-success";
  return (
    <div className="rounded-md border border-border bg-surface-elevated px-4 py-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-fg">{mount.path}</span>
        <StatusBadge tone={tone} label={`${Math.round(pct)}%`} />
      </div>
      <div className="mt-2 h-1.5 rounded-full bg-border">
        <div
          className={`h-full rounded-full ${fill}`}
          style={{ width: `${Math.min(100, pct)}%` }}
          aria-hidden="true"
        />
      </div>
      <div className="mt-2 flex justify-between text-2xs text-tertiary">
        <span>{mount.used_gb.toFixed(1)} GB used</span>
        <span>
          {mount.free_gb.toFixed(1)} GB free · {mount.size_gb.toFixed(1)} GB total
        </span>
      </div>
    </div>
  );
}
