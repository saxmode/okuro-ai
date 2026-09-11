import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { SectionLabel } from "@/components/ui/section-label";
import { StatusBadge } from "@/components/ui/status-badge";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { MetricCard } from "@/components/ui/metric-card";
import { DaemonJobsPanel } from "@/components/schedule/daemon-jobs-panel";
import { SystemPanel } from "@/components/dashboard/system-panel";
import { useUrlTab } from "@/hooks/use-url-tab";
import { useSystemStatus } from "@/hooks/use-tasks";
import { formatDuration } from "@/lib/format";
import { PageHeader } from "@/components/shell/page-header";
import { parseApiDate } from "@/lib/format";

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

function groupCheck(name: string): CheckGroup {
  if (CORE_CHECKS.has(name)) return "CORE";
  if (AGENT_CHECKS.has(name)) return "AGENTS";
  if (name.toLowerCase().includes("schedul")) return "SCHEDULES";
  return "OTHER";
}

/**
 * /health — services, GPU, storage, docker, schedules, cortex.
 * Renamed from /system. Wave 2 W2.7.
 */
export function HealthPage() {
  const tab = useUrlTab("services");

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Health"
        subtitle="Services, hardware, storage, schedules — is anything broken?"
      />

      <Tabs value={tab.value} onValueChange={tab.onValueChange}>
        <TabsList>
          <TabsTrigger value="services">Services</TabsTrigger>
          <TabsTrigger value="system">System</TabsTrigger>
          <TabsTrigger value="storage">Storage</TabsTrigger>
          <TabsTrigger value="schedules">Schedules</TabsTrigger>
          <TabsTrigger value="cortex">Cortex</TabsTrigger>
        </TabsList>

        <TabsContent value="services">
          <ServicesTab />
        </TabsContent>

        <TabsContent value="system">
          <SystemTab />
        </TabsContent>

        <TabsContent value="storage">
          <StorageTab />
        </TabsContent>

        <TabsContent value="schedules">
          <SchedulesTab />
        </TabsContent>

        <TabsContent value="cortex">
          <CortexHealthTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function ServicesTab() {
  const { data: status } = useSystemStatus();
  const { data: doctor, isLoading } = useQuery({
    queryKey: ["doctor"],
    queryFn: () => api<DoctorResult>("/api/doctor"),
    refetchInterval: 30_000,
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
    refetchInterval: 60_000,
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
    refetchInterval: 120_000,
    staleTime: 60_000,
  });

  const enrichment = useQuery({
    queryKey: ["cortex-enrichment"],
    queryFn: () =>
      api<{ events: EnrichmentEvent[]; count: number }>(
        "/api/cortex/enrichment?limit=50",
      ),
    refetchInterval: 30_000,
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
              <div className="overflow-hidden rounded-md border border-border">
                <table className="w-full text-xs">
                  <thead className="border-b border-border bg-surface">
                    <tr className="text-left text-2xs uppercase tracking-wider text-tertiary">
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
