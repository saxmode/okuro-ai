import { useEffect, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Eye,
  Play,
  RefreshCw,
  CalendarClock,
} from "lucide-react";
import { roleApi } from "@/lib/api";
import { formatAge } from "@/lib/format";
import type { MaintenanceJob, RoleDetail } from "@/types/api";
import { Button } from "@/components/ui/button";

interface MaintenancePanelProps {
  roleId: string;
  role: RoleDetail | undefined;
}

/**
 * Mandate preview shape returned by the lightweight
 * ``GET /api/roles/{id}/maintenance/mandate`` endpoint.
 * Mirrors the server ``get_maintenance_mandate`` payload loosely — unknown
 * keys are tolerated.
 */
interface MandatePreview {
  instructions?: string;
  research_mandate?: {
    searches?: string[];
    sources_to_check?: string[];
    current_knowledge_summary?: string;
  };
}

/**
 * Maintenance tab: schedule, stats, Preview mandate (read-only) + Run
 * research now (spawns real role-researcher orchestrator task). Polls
 * ``/api/roles/maintenance/status`` while a job is running.
 */
export function MaintenancePanel({ roleId, role }: MaintenancePanelProps) {
  const qc = useQueryClient();
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [previewMandate, setPreviewMandate] = useState<MandatePreview | null>(
    null,
  );

  const maintenance = role?.maintenance;
  const stats = maintenance?.stats;
  // Mandate shipped inline with role detail (only when stale) — used as the
  // preview fallback so there's always *something* to show.
  const inlineMandate = maintenance?.mandate as MandatePreview | undefined;

  const runMutation = useMutation({
    mutationFn: () => roleApi.runMaintenance(roleId),
    onSuccess: (res) => {
      setActiveJobId(res.job_id);
    },
  });

  const previewMutation = useMutation({
    mutationFn: () => roleApi.mandatePreview(roleId),
    onSuccess: (data) => {
      setPreviewMandate(data as MandatePreview);
    },
  });

  const { data: job } = useQuery<MaintenanceJob>({
    queryKey: ["role-maintenance-job", activeJobId],
    queryFn: () => roleApi.maintenanceStatus(activeJobId!) as Promise<MaintenanceJob>,
    enabled: !!activeJobId,
    refetchInterval: (q) => {
      const j = q.state.data as MaintenanceJob | undefined;
      if (!j) return 2000;
      return j.status === "running" ? 2000 : false;
    },
  });

  // When job finishes, refresh role + roles listing so last_maintained / stale update.
  useEffect(() => {
    if (job?.status === "done" || job?.status === "failed") {
      qc.invalidateQueries({ queryKey: ["role", roleId] });
      qc.invalidateQueries({ queryKey: ["roles"] });
      qc.invalidateQueries({ queryKey: ["roles-maintenance"] });
    }
  }, [job?.status, qc, roleId]);

  const schedule = role?.maintenance_schedule ?? "monthly";
  const lastMaintained = role?.last_maintained;
  const stale = maintenance?.stale ?? role?.stale ?? false;

  // Effective mandate to render: explicit preview click > inline (stale only).
  const mandate = previewMandate ?? inlineMandate ?? null;

  return (
    <div className="space-y-4 p-1">
      {/* Status row */}
      <div className="flex flex-wrap items-center gap-3">
        <StatusPill stale={stale} />
        <StatCell label="Schedule" value={schedule} />
        <StatCell
          label="Last maintained"
          value={lastMaintained ? formatAge(lastMaintained) : "never"}
        />
        <StatCell
          label="Entries"
          value={String(stats?.total_entries ?? 0)}
        />
        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => previewMutation.mutate()}
            disabled={previewMutation.isPending}
            className="h-8 text-xs"
          >
            {previewMutation.isPending ? (
              <>
                <RefreshCw className="mr-1 h-3 w-3 animate-spin" /> Loading…
              </>
            ) : (
              <>
                <Eye className="mr-1 h-3 w-3" /> Preview mandate
              </>
            )}
          </Button>
          <Button
            size="sm"
            onClick={() => runMutation.mutate()}
            disabled={runMutation.isPending || job?.status === "running"}
            className="h-8 text-xs"
          >
            {runMutation.isPending || job?.status === "running" ? (
              <>
                <RefreshCw className="mr-1 h-3 w-3 animate-spin" /> Running…
              </>
            ) : (
              <>
                <Play className="mr-1 h-3 w-3" /> Run research now
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Cross-nav to the global recurring schedule view */}
      <div className="flex items-center gap-2 text-2xs">
        <CalendarClock className="h-3 w-3 text-tertiary" />
        <Link
          to="/work?focus=role-refresh"
          className="text-info hover:underline"
        >
          See schedule + history →
        </Link>
        <span className="text-tertiary">
          Daily sweep runs via role-refresh recurring task.
        </span>
      </div>

      {/* Run feedback */}
      {job && (
        <div
          className={`rounded border p-2 text-xs ${
            job.status === "done"
              ? "border-success/40 bg-success/10 text-success"
              : job.status === "failed"
                ? "border-error/40 bg-error/10 text-error"
                : "border-warning/40 bg-warning/10 text-warning"
          }`}
        >
          <div className="flex items-center gap-2">
            {job.status === "done" ? (
              <CheckCircle2 className="h-3 w-3" />
            ) : job.status === "failed" ? (
              <AlertTriangle className="h-3 w-3" />
            ) : (
              <Clock className="h-3 w-3" />
            )}
            <span className="font-medium uppercase tracking-wider">
              {job.status}
            </span>
            <span className="text-tertiary">
              job {job.job_id.slice(0, 8)} · started {formatAge(job.started_at)}
            </span>
            {job.task_id && (
              <Link
                to={`/work/${job.task_id}`}
                className="ml-auto text-info hover:underline"
              >
                open task →
              </Link>
            )}
          </div>
          {job.status === "done" && typeof job.learned === "number" && (
            <p className="mt-1 text-xs">
              Research task finished — {job.learned} knowledge{" "}
              {job.learned === 1 ? "entry" : "entries"} written.
            </p>
          )}
          {job.orchestrator_phase && job.status === "running" && (
            <p className="mt-1 text-xs text-tertiary">
              phase: {job.orchestrator_phase}
            </p>
          )}
          {job.error && <p className="mt-1 text-xs">{job.error}</p>}
        </div>
      )}

      {/* Mandate */}
      <section className="space-y-2">
        <h3 className="text-2xs font-medium uppercase tracking-wider text-tertiary">
          Research mandate
        </h3>
        {mandate ? (
          <div className="space-y-2 rounded border border-border bg-surface-elevated p-3 text-xs">
            {mandate.instructions && (
              <p className="text-fg-muted">{mandate.instructions}</p>
            )}
            {mandate.research_mandate?.searches?.length ? (
              <div>
                <span className="text-2xs uppercase tracking-wider text-tertiary">
                  Searches
                </span>
                <ul className="mt-1 space-y-0.5">
                  {mandate.research_mandate.searches.map((s, i) => (
                    <li key={i} className="text-fg">
                      · {s}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {mandate.research_mandate?.sources_to_check?.length ? (
              <div>
                <span className="text-2xs uppercase tracking-wider text-tertiary">
                  Sources to check
                </span>
                <ul className="mt-1 space-y-0.5">
                  {mandate.research_mandate.sources_to_check.map((s, i) => (
                    <li key={i}>
                      <a
                        href={s}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-info hover:underline break-all"
                      >
                        {s}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {mandate.research_mandate?.current_knowledge_summary && (
              <div>
                <span className="text-2xs uppercase tracking-wider text-tertiary">
                  Current knowledge
                </span>
                <p className="mt-1 text-fg">
                  {mandate.research_mandate.current_knowledge_summary}
                </p>
              </div>
            )}
          </div>
        ) : (
          <p className="text-xs text-tertiary">
            {stale
              ? "No mandate yet — click Preview mandate to load."
              : `Role is fresh — click Preview mandate to see what a refresh would cover. Next auto refresh due when ${schedule} interval lapses since ${
                  lastMaintained ? formatAge(lastMaintained) : "the first maintenance run"
                }.`}
          </p>
        )}
      </section>

      {/* Stats */}
      <section className="space-y-2">
        <h3 className="text-2xs font-medium uppercase tracking-wider text-tertiary">
          Knowledge stats
        </h3>
        <div className="grid grid-cols-3 gap-2 text-xs">
          <StatBox label="Total" value={String(stats?.total_entries ?? 0)} />
          <StatBox label="Maturity" value={stats?.maturity ?? "—"} />
          <StatBox label="Sessions" value={String(stats?.sessions ?? 0)} />
        </div>
        {stats?.by_type && Object.keys(stats.by_type).length > 0 && (
          <div className="flex flex-wrap gap-1.5 pt-1">
            {Object.entries(stats.by_type).map(([t, c]) => (
              <span
                key={t}
                className="rounded bg-surface-elevated px-2 py-0.5 text-3xs text-fg-muted"
              >
                {t} · {c}
              </span>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function StatusPill({ stale }: { stale: boolean }) {
  return stale ? (
    <span className="inline-flex items-center gap-1 rounded-full bg-error/10 px-2 py-0.5 text-2xs font-medium uppercase tracking-wider text-error">
      <span className="h-1.5 w-1.5 rounded-full bg-error" />
      Stale
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 rounded-full bg-success/10 px-2 py-0.5 text-2xs font-medium uppercase tracking-wider text-success">
      <span className="h-1.5 w-1.5 rounded-full bg-success" />
      Fresh
    </span>
  );
}

function StatCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-3xs uppercase tracking-wider text-tertiary">
        {label}
      </span>
      <span className="text-xs text-fg">{value}</span>
    </div>
  );
}

function StatBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-border bg-surface-elevated p-2">
      <div className="text-3xs uppercase tracking-wider text-tertiary">
        {label}
      </div>
      <div className="mt-0.5 text-sm font-medium text-fg">{value}</div>
    </div>
  );
}
