import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  RefreshCw,
  Play,
  Square,
  RotateCw,
  Loader2,
  Download,
  X,
  Power,
  PowerOff,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/components/ui/toast";
import { PageHeader } from "@/components/shell/page-header";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * /services — control okuro background services.
 *
 * Surfaces the service_manager module to the browser:
 * start/stop/restart/install/enable/disable + live state + journal logs.
 *
 * Backend: /api/services/* (see src/okuro/orchestrator/api/services.py).
 */

// ── Types ────────────────────────────────────────────────────────────

type ServiceRow = {
  name: string;
  description: string;
  state: string;
  active: boolean;
  running: boolean;
  backend: string;
  installed: boolean;
  uptime_seconds: number | null;
  memory_bytes: number | null;
  restart_count: number | null;
  enabled: boolean | null;
  pid: number | null;
  started_at: number | null;
};

type ServiceListResponse = {
  services: ServiceRow[];
  backend: string;
};

type ServiceDetail = {
  service: ServiceRow;
  exec_start: string[];
  working_directory: string | null;
  environment: Record<string, string>;
  logs: string[];
};

// ── API helpers ──────────────────────────────────────────────────────

const servicesApi = {
  list: () => api<ServiceListResponse>("/api/services"),
  get: (name: string) =>
    api<ServiceDetail>(`/api/services/${encodeURIComponent(name)}`),
  start: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/start`, {
      method: "POST",
    }),
  stop: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/stop`, {
      method: "POST",
    }),
  restart: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/restart`, {
      method: "POST",
    }),
  install: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/install`, {
      method: "POST",
    }),
  enable: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/enable`, {
      method: "POST",
    }),
  disable: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/disable`, {
      method: "POST",
    }),
};

// ── Formatters ───────────────────────────────────────────────────────

function formatUptime(seconds: number | null): string {
  if (seconds === null || seconds < 0) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatMemory(bytes: number | null): string {
  if (bytes === null || bytes <= 0) return "—";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

type PillVariant = "success" | "error" | "warning" | "muted";

function statePill(state: string, active: boolean): PillVariant {
  if (state === "active" || active) return "success";
  if (state === "failed") return "error";
  if (state === "activating" || state === "reloading" || state === "deactivating")
    return "warning";
  return "muted";
}

// ── Page ─────────────────────────────────────────────────────────────

export function ServicesPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ["services", "list"],
    queryFn: servicesApi.list,
    refetchInterval: 5_000,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["services", "list"] });
    if (selected) {
      queryClient.invalidateQueries({ queryKey: ["services", "detail", selected] });
    }
  };

  const installMut = useMutation({
    mutationFn: (name: string) => servicesApi.install(name),
    onSuccess: (_row, name) => {
      toast.success(`Installed ${name}`);
      invalidate();
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : "Install failed");
    },
  });

  const data = listQuery.data;
  const services = data?.services ?? [];
  const notInstalled = services.filter((s) => !s.installed);

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Services"
        subtitle="okuro background services — start, stop, restart, and run on boot."
        right={
          <Button
            size="sm"
            variant="outline"
            onClick={() => listQuery.refetch()}
            disabled={listQuery.isRefetching}
          >
            {listQuery.isRefetching ? (
              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
            )}
            Refresh
          </Button>
        }
      />

      {/* Install banner — shown when any registry service has no unit file yet */}
      {notInstalled.length > 0 && (
        <InstallBanner
          rows={notInstalled}
          onInstall={(name) => installMut.mutate(name)}
          pending={installMut.isPending}
          pendingName={installMut.variables ?? null}
        />
      )}

      {listQuery.isLoading ? (
        <div className="text-sm text-tertiary">Loading services…</div>
      ) : listQuery.isError ? (
        <EmptyState
          title="Failed to load services"
          description={
            listQuery.error instanceof Error
              ? listQuery.error.message
              : "Unknown error"
          }
        />
      ) : services.length === 0 ? (
        <EmptyState
          title="No services registered"
          description="The built-in registry is empty — this should never happen."
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {services.map((row) => (
            <ServiceCard
              key={row.name}
              row={row}
              onOpenDetail={() => setSelected(row.name)}
              onActed={invalidate}
            />
          ))}
        </div>
      )}

      {selected && (
        <DetailDialog
          name={selected}
          open={selected !== null}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

// ── Install banner ───────────────────────────────────────────────────

function InstallBanner({
  rows,
  onInstall,
  pending,
  pendingName,
}: {
  rows: ServiceRow[];
  onInstall: (name: string) => void;
  pending: boolean;
  pendingName: string | null;
}) {
  return (
    <div className="rounded border border-accent/50 bg-accent/5 p-3 space-y-2">
      <div className="flex items-start gap-2">
        <Download className="h-4 w-4 text-accent mt-0.5 shrink-0" />
        <div className="text-xs text-fg">
          <div className="font-medium">
            {rows.length === 1
              ? "1 service not installed"
              : `${rows.length} services not installed`}
          </div>
          <p className="mt-0.5 text-tertiary">
            These services have specs but no unit file yet. Install before
            start/stop will work.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2 pl-6">
        {rows.map((r) => (
          <Button
            key={r.name}
            size="sm"
            variant="outline"
            onClick={() => onInstall(r.name)}
            disabled={pending && pendingName === r.name}
          >
            {pending && pendingName === r.name ? (
              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            ) : (
              <Download className="h-3.5 w-3.5 mr-1.5" />
            )}
            Install {r.name}
          </Button>
        ))}
      </div>
    </div>
  );
}

// ── Service card ─────────────────────────────────────────────────────

function ServiceCard({
  row,
  onOpenDetail,
  onActed,
}: {
  row: ServiceRow;
  onOpenDetail: () => void;
  onActed: () => void;
}) {
  const [busy, setBusy] = useState<"start" | "stop" | "restart" | null>(null);
  const [enableBusy, setEnableBusy] = useState(false);
  const pill = statePill(row.state, row.active);

  const act = async (
    which: "start" | "stop" | "restart",
    fn: () => Promise<ServiceRow>,
  ) => {
    setBusy(which);
    try {
      await fn();
      toast.success(`${which} ${row.name}`);
      onActed();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : `${which} failed`);
    } finally {
      setBusy(null);
    }
  };

  const toggleEnable = async () => {
    const target = row.enabled ? "disable" : "enable";
    setEnableBusy(true);
    try {
      if (row.enabled) {
        await servicesApi.disable(row.name);
        toast.success(`Disabled ${row.name} on boot`);
      } else {
        await servicesApi.enable(row.name);
        toast.success(`Enabled ${row.name} on boot`);
      }
      onActed();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : `${target} failed`);
    } finally {
      setEnableBusy(false);
    }
  };

  const canStart = row.installed && !row.active && busy === null;
  const canStop = row.installed && row.active && busy === null;
  const canRestart = row.installed && busy === null;

  return (
    <div className="rounded border border-border bg-surface p-3 flex flex-col gap-3">
      {/* Header: name + state */}
      <button
        onClick={onOpenDetail}
        className="text-left space-y-1 focus:outline-none"
      >
        <div className="flex items-center justify-between gap-2">
          <div className="font-mono text-sm font-medium text-fg truncate">
            {row.name}
          </div>
          <StatePill variant={pill} label={row.state} />
        </div>
        <div className="text-2xs text-tertiary line-clamp-2">
          {row.description}
        </div>
      </button>

      {/* Metrics row */}
      <div className="grid grid-cols-3 gap-2 text-2xs">
        <Metric label="Uptime" value={formatUptime(row.uptime_seconds)} />
        <Metric label="Memory" value={formatMemory(row.memory_bytes)} />
        <Metric
          label="Restarts"
          value={row.restart_count === null ? "—" : String(row.restart_count)}
        />
      </div>

      {/* Install warning */}
      {!row.installed && (
        <div className="text-2xs text-warning rounded bg-warning/10 px-2 py-1">
          Not installed — use the install banner above.
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-1.5">
        <Button
          size="sm"
          variant="outline"
          onClick={() => act("start", () => servicesApi.start(row.name))}
          disabled={!canStart}
          className="flex-1"
        >
          {busy === "start" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Play className="h-3.5 w-3.5" />
          )}
          <span className="ml-1.5">Start</span>
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => act("stop", () => servicesApi.stop(row.name))}
          disabled={!canStop}
          className="flex-1"
        >
          {busy === "stop" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Square className="h-3.5 w-3.5" />
          )}
          <span className="ml-1.5">Stop</span>
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => act("restart", () => servicesApi.restart(row.name))}
          disabled={!canRestart}
          title="Restart"
          aria-label="Restart"
          className="shrink-0 px-2"
        >
          {busy === "restart" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RotateCw className="h-3.5 w-3.5" />
          )}
        </Button>
      </div>

      {/* Enable/Disable toggle */}
      <div className="flex items-center justify-between text-2xs">
        <div className="text-tertiary">
          Boot:{" "}
          <span
            className={cn(
              row.enabled === true
                ? "text-success"
                : row.enabled === false
                  ? "text-tertiary"
                  : "text-tertiary",
            )}
          >
            {row.enabled === null
              ? "unknown"
              : row.enabled
                ? "enabled"
                : "disabled"}
          </span>
        </div>
        <button
          onClick={toggleEnable}
          disabled={!row.installed || enableBusy || row.enabled === null}
          className={cn(
            "inline-flex items-center gap-1 text-2xs uppercase tracking-wider transition-colors",
            row.installed && !enableBusy
              ? "text-tertiary hover:text-fg-muted"
              : "text-tertiary/50 cursor-not-allowed",
          )}
        >
          {enableBusy ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : row.enabled ? (
            <PowerOff className="h-3 w-3" />
          ) : (
            <Power className="h-3 w-3" />
          )}
          {row.enabled ? "Disable" : "Enable"}
        </button>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="uppercase tracking-wider text-tertiary">{label}</div>
      <div className="text-fg font-medium tabular-nums">{value}</div>
    </div>
  );
}

function StatePill({
  variant,
  label,
}: {
  variant: PillVariant;
  label: string;
}) {
  const cls =
    variant === "success"
      ? "bg-success/15 text-success border-success/30"
      : variant === "error"
        ? "bg-error/15 text-error border-error/30"
        : variant === "warning"
          ? "bg-warning/15 text-warning border-warning/30"
          : "bg-surface border-border text-tertiary";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-2xs font-medium uppercase tracking-wider shrink-0",
        cls,
      )}
    >
      {label}
    </span>
  );
}

// ── Detail drawer ────────────────────────────────────────────────────

function DetailDialog({
  name,
  open,
  onClose,
}: {
  name: string;
  open: boolean;
  onClose: () => void;
}) {
  const detailQuery = useQuery({
    queryKey: ["services", "detail", name],
    queryFn: () => servicesApi.get(name),
    enabled: open,
    refetchInterval: open ? 3_000 : false,
  });

  // Refetch immediately when re-opened.
  useEffect(() => {
    if (open) {
      detailQuery.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const detail = detailQuery.data;
  const row = detail?.service;

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-auto">
        <DialogHeader>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <DialogTitle className="font-mono text-base">{name}</DialogTitle>
              {row && (
                <DialogDescription className="mt-1 flex items-center gap-2">
                  <StatePill
                    variant={statePill(row.state, row.active)}
                    label={row.state}
                  />
                  <span className="text-2xs text-tertiary">
                    {row.backend} · uptime {formatUptime(row.uptime_seconds)}
                    {row.pid !== null && ` · pid ${row.pid}`}
                  </span>
                </DialogDescription>
              )}
            </div>
            <button
              onClick={onClose}
              className="text-tertiary hover:text-fg-muted"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </DialogHeader>

        {detailQuery.isLoading ? (
          <div className="text-sm text-tertiary">Loading…</div>
        ) : detailQuery.isError ? (
          <div className="text-xs text-error">
            {detailQuery.error instanceof Error
              ? detailQuery.error.message
              : "Load failed"}
          </div>
        ) : detail ? (
          <div className="space-y-4">
            {/* Spec block */}
            <div className="space-y-2">
              <div className="text-2xs uppercase tracking-wider text-tertiary">
                Spec
              </div>
              <div className="rounded border border-border bg-surface p-2 space-y-1">
                <div className="text-2xs">
                  <span className="text-tertiary">ExecStart:</span>{" "}
                  <span className="font-mono text-fg break-all">
                    {detail.exec_start.join(" ")}
                  </span>
                </div>
                {detail.working_directory && (
                  <div className="text-2xs">
                    <span className="text-tertiary">WorkingDirectory:</span>{" "}
                    <span className="font-mono text-fg break-all">
                      {detail.working_directory}
                    </span>
                  </div>
                )}
                {Object.keys(detail.environment).length > 0 && (
                  <div className="text-2xs">
                    <span className="text-tertiary">Environment:</span>
                    <ul className="mt-0.5 pl-3 space-y-0.5">
                      {Object.entries(detail.environment).map(([k, v]) => (
                        <li key={k} className="font-mono text-fg">
                          {k}={v}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {row && (
                  <div className="text-2xs text-tertiary pt-1 flex flex-wrap gap-x-4">
                    <span>Memory: {formatMemory(row.memory_bytes)}</span>
                    <span>
                      Restarts:{" "}
                      {row.restart_count === null ? "—" : row.restart_count}
                    </span>
                    <span>
                      Boot:{" "}
                      {row.enabled === null
                        ? "unknown"
                        : row.enabled
                          ? "enabled"
                          : "disabled"}
                    </span>
                  </div>
                )}
              </div>
            </div>

            {/* Logs */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="text-2xs uppercase tracking-wider text-tertiary">
                  Last {detail.logs.length} log lines
                  <span className="ml-1.5 text-tertiary/60">(auto-refresh 3s)</span>
                </div>
                <Badge variant="outline" className="text-2xs">
                  journalctl --user
                </Badge>
              </div>
              <div className="rounded border border-border bg-surface max-h-96 overflow-auto">
                {detail.logs.length === 0 ? (
                  <div className="p-3 text-2xs text-tertiary">
                    No log lines — service may not have run yet, or journalctl
                    is unavailable.
                  </div>
                ) : (
                  <pre className="p-3 text-2xs font-mono text-fg-muted whitespace-pre-wrap break-words leading-relaxed">
                    {detail.logs.join("\n")}
                  </pre>
                )}
              </div>
            </div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
