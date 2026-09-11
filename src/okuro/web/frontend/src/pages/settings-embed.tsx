import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { ArrowLeft, Loader2, RefreshCw, AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  embedApi,
  type EmbedConfigPayload,
  type EmbedDetection,
  type EmbedHealth,
  type EmbedSwitchEstimate,
  type EmbedTierOptions,
} from "@/lib/api";

/**
 * /settings/embed — change embedding tier + device after onboarding.
 *
 * Reads the persisted ~/.okuro/embed-config.yaml, exposes a "Change tier"
 * modal that surfaces the dim delta + file count + ETA + the
 * "search disabled until done" warning when re-embed is required.
 *
 * Confirming PUTs /api/embed/config which:
 *   1. atomically writes the new config file
 *   2. drops vec_cortex + clears cortex_docs if dim differs
 *   3. enqueues a background reindex (job id surfaced for polling)
 *   4. regenerates okuro-embed + okuro-daemon unit files
 */
export function SettingsEmbedPage() {
  const qc = useQueryClient();
  const cfg = useQuery({
    queryKey: ["embed", "config"],
    queryFn: embedApi.getConfig,
  });
  const tiersResp = useQuery({
    queryKey: ["embed", "tiers"],
    queryFn: embedApi.tiers,
  });
  const detectionResp = useQuery({
    queryKey: ["embed", "detect"],
    queryFn: embedApi.detect,
  });
  const health = useQuery({
    queryKey: ["embed", "health"],
    queryFn: embedApi.health,
    refetchInterval: 5000,
  });

  const [modalOpen, setModalOpen] = useState(false);
  const [pendingTier, setPendingTier] = useState<"low" | "high">("low");
  const [pendingDevice, setPendingDevice] = useState<string>("auto");
  const [reindexJob, setReindexJob] = useState<string | null>(null);

  const switchMutation = useMutation({
    mutationFn: embedApi.putConfig,
    onSuccess: (res) => {
      setReindexJob(res.reindex_job_id ?? null);
      setModalOpen(false);
      void qc.invalidateQueries({ queryKey: ["embed"] });
    },
  });

  const estimate = useQuery<EmbedSwitchEstimate>({
    queryKey: ["embed", "estimate", pendingTier],
    queryFn: () => embedApi.estimate(pendingTier),
    enabled: modalOpen,
  });

  const openSwitchFor = (tier: "low" | "high", device: string) => {
    setPendingTier(tier);
    setPendingDevice(device);
    setModalOpen(true);
  };

  if (cfg.isLoading || tiersResp.isLoading || detectionResp.isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-fg-tertiary text-sm">
        <Loader2 size={14} className="animate-spin mr-2" />
        Loading embed configuration…
      </div>
    );
  }
  if (cfg.error || tiersResp.error || detectionResp.error) {
    return (
      <div className="p-6 text-sm text-error">
        Failed to load embed configuration.
      </div>
    );
  }
  if (!cfg.data || !tiersResp.data || !detectionResp.data) return null;

  return (
    <div className="flex flex-col gap-6 p-6">
      <PageHeader
        title="Embedding"
        subtitle="Pick the model okuro uses to embed your codebase + memories. Switching tiers requires a re-embed."
        right={
          <Link
            to="/settings"
            className="flex items-center gap-1 text-xs text-fg-tertiary hover:text-fg-primary"
          >
            <ArrowLeft size={12} />
            Back to Settings
          </Link>
        }
      />

      <CurrentChip cfg={cfg.data} health={health.data} />

      <HardwareCard
        detection={detectionResp.data}
        onRedetect={() => detectionResp.refetch()}
        refreshing={detectionResp.isFetching}
      />

      <TierList
        cfg={cfg.data}
        tiers={tiersResp.data}
        detection={detectionResp.data}
        onChange={openSwitchFor}
      />

      {reindexJob && <ReindexProgress jobId={reindexJob} />}

      <SwitchModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        pendingTier={pendingTier}
        pendingDevice={pendingDevice}
        estimate={estimate.data}
        loading={estimate.isLoading || switchMutation.isPending}
        onConfirm={() =>
          switchMutation.mutate({
            tier: pendingTier,
            device: pendingDevice,
            chosen_by: "web",
          })
        }
      />
    </div>
  );
}

function CurrentChip({
  cfg,
  health,
}: {
  cfg: EmbedConfigPayload;
  health?: EmbedHealth;
}) {
  return (
    <div className="rounded border border-border bg-surface-elevated p-4 flex items-center gap-4 text-sm">
      <div>
        <div className="text-xs uppercase tracking-wider text-fg-tertiary">
          Current
        </div>
        <div className="font-mono text-fg-primary">
          {cfg.tier} · {cfg.device}
        </div>
      </div>
      <div className="border-l border-border h-10" />
      <div>
        <div className="text-xs uppercase tracking-wider text-fg-tertiary">
          Service
        </div>
        <div
          className={
            health?.status === "ok"
              ? "text-success font-mono"
              : "text-warning font-mono"
          }
        >
          {health?.status ?? "checking…"}
        </div>
      </div>
      <div className="border-l border-border h-10" />
      <div>
        <div className="text-xs uppercase tracking-wider text-fg-tertiary">
          Set
        </div>
        <div className="font-mono text-fg-secondary">
          {cfg.chosen_at || "—"} · {cfg.chosen_by}
        </div>
      </div>
    </div>
  );
}

function HardwareCard({
  detection,
  onRedetect,
  refreshing,
}: {
  detection: EmbedDetection;
  onRedetect: () => void;
  refreshing: boolean;
}) {
  return (
    <div className="rounded border border-border bg-surface-elevated p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="text-xs uppercase tracking-wider text-fg-tertiary">
          Hardware
        </div>
        <button
          type="button"
          onClick={onRedetect}
          disabled={refreshing}
          className="text-xs text-fg-tertiary hover:text-fg-primary flex items-center gap-1"
        >
          {refreshing ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <RefreshCw size={12} />
          )}
          Re-detect
        </button>
      </div>
      <div className="space-y-1.5 text-sm">
        {detection.gpus.length === 0 ? (
          <div className="text-fg-secondary">
            No NVIDIA GPU detected — embedding runs on CPU.
          </div>
        ) : (
          detection.gpus.map((g) => (
            <div key={g.index} className="flex items-center gap-3">
              <span className="font-mono text-xs text-fg-tertiary">
                cuda:{g.index}
              </span>
              <span className="text-fg-primary">{g.name}</span>
              <span className="text-fg-tertiary">{g.vram_gb} GB</span>
              {g.is_display && (
                <span className="rounded bg-warning-subtle/15 text-warning text-[11px] px-1.5 py-0.5">
                  display GPU
                </span>
              )}
            </div>
          ))
        )}
        <div className="text-xs text-fg-tertiary pt-1">
          {detection.cpu_cores} cores · {detection.ram_gb} GB RAM
        </div>
      </div>
    </div>
  );
}

function TierList({
  cfg,
  tiers,
  detection,
  onChange,
}: {
  cfg: EmbedConfigPayload;
  tiers: EmbedTierOptions;
  detection: EmbedDetection;
  onChange: (tier: "low" | "high", device: string) => void;
}) {
  const [device, setDevice] = useState<string>(cfg.device);
  const showDevicePicker = (target: "low" | "high") =>
    target === "high" && detection.gpus.length > 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="text-xs uppercase tracking-wider text-fg-tertiary">
        Available tiers
      </div>
      {tiers.tiers.map((t) => {
        const isCurrent = cfg.tier === t.tier;
        const targetDevice =
          t.tier === "low"
            ? "cpu"
            : showDevicePicker(t.tier)
              ? device.startsWith("cuda:")
                ? device
                : tiers.recommended_device
              : tiers.recommended_device;
        return (
          <div
            key={t.tier}
            className={`rounded border p-4 ${
              isCurrent ? "border-accent" : "border-border"
            } bg-surface-elevated`}
          >
            <div className="flex items-baseline justify-between">
              <div>
                <div className="text-sm uppercase font-bold text-fg-primary">
                  {t.tier}
                  {isCurrent && (
                    <span className="ml-2 text-xs normal-case text-accent">
                      current
                    </span>
                  )}
                </div>
                <div className="text-xs text-fg-tertiary">
                  {t.model_id} · {t.dim}d
                </div>
                <div className="text-xs text-fg-tertiary">{t.description}</div>
              </div>
              {!isCurrent && (
                <Button
                  size="sm"
                  onClick={() => onChange(t.tier, targetDevice)}
                >
                  Switch to {t.tier.toUpperCase()}
                </Button>
              )}
            </div>
            {showDevicePicker(t.tier) && !isCurrent && (
              <div className="mt-3 flex items-center gap-3">
                <label className="text-xs text-fg-tertiary">Device:</label>
                <Select value={device} onValueChange={setDevice}>
                  <SelectTrigger className="w-72">
                    <SelectValue placeholder="Select GPU" />
                  </SelectTrigger>
                  <SelectContent>
                    {detection.gpus.map((g) => (
                      <SelectItem key={g.index} value={`cuda:${g.index}`}>
                        cuda:{g.index} · {g.name} · {g.vram_gb} GB
                        {g.is_display ? " [display]" : ""}
                      </SelectItem>
                    ))}
                    <SelectItem value="cpu">cpu (slow)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function SwitchModal({
  open,
  onClose,
  pendingTier,
  pendingDevice,
  estimate,
  loading,
  onConfirm,
}: {
  open: boolean;
  onClose: () => void;
  pendingTier: "low" | "high";
  pendingDevice: string;
  estimate?: EmbedSwitchEstimate;
  loading: boolean;
  onConfirm: () => void;
}) {
  const eta = useMemo(() => {
    if (!estimate?.eta_seconds) return null;
    if (estimate.eta_seconds < 60) return `${estimate.eta_seconds} s`;
    const m = Math.round(estimate.eta_seconds / 60);
    return `${m} min`;
  }, [estimate]);

  return (
    <Dialog open={open} onOpenChange={(o) => (!o ? onClose() : null)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Change embedding tier?</DialogTitle>
          <DialogDescription>
            You will switch from{" "}
            <span className="font-mono">{estimate?.current_tier ?? "?"}</span>{" "}
            to{" "}
            <span className="font-mono">{pendingTier}</span> on{" "}
            <span className="font-mono">{pendingDevice}</span>.
          </DialogDescription>
        </DialogHeader>
        {!estimate ? (
          <div className="text-sm text-fg-tertiary flex items-center gap-2">
            <Loader2 size={12} className="animate-spin" />
            Checking impact…
          </div>
        ) : (
          <div className="space-y-3 text-sm">
            {estimate.dim_changed ? (
              <>
                <div>
                  Dim change{" "}
                  <span className="font-mono">
                    {estimate.current_dim}d → {estimate.new_dim}d
                  </span>{" "}
                  means okuro must drop and rebuild the index.
                </div>
                <div>
                  Affected: <strong>{estimate.files_affected}</strong> files.
                  {eta && (
                    <span>
                      {" "}
                      Estimated time:{" "}
                      <span className="font-mono">{eta}</span>.
                    </span>
                  )}
                </div>
                <div className="flex items-start gap-2 text-warning">
                  <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                  <span>Search will return no results until reindex completes.</span>
                </div>
              </>
            ) : (
              <div className="text-fg-secondary">
                Dim is unchanged ({estimate.new_dim}d) — switch is instant, no
                re-embed required.
              </div>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep current
          </Button>
          <Button onClick={onConfirm} disabled={loading || !estimate}>
            {loading && <Loader2 size={12} className="animate-spin mr-1" />}
            {estimate?.dim_changed
              ? "Switch and reindex"
              : "Switch"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReindexProgress({ jobId }: { jobId: string }) {
  const job = useQuery({
    queryKey: ["cortex", "reindex", jobId],
    queryFn: async () => {
      const r = await fetch(
        `/api/cortex/reindex/status?job=${encodeURIComponent(jobId)}`,
      );
      if (!r.ok) throw new Error(`reindex status ${r.status}`);
      return r.json() as Promise<{
        job_id: string;
        status: string;
        indexed?: number | null;
        total?: number | null;
        indexed_so_far?: number | null;
        error?: string | null;
      }>;
    },
    refetchInterval: (q) =>
      q.state.data?.status === "running" ? 2000 : false,
  });
  const data = job.data;
  if (!data) return null;
  const indexed = data.indexed ?? data.indexed_so_far ?? 0;
  return (
    <div className="rounded border border-border bg-surface-elevated p-4">
      <div className="text-xs uppercase tracking-wider text-fg-tertiary mb-2">
        Re-embedding ({data.status})
      </div>
      <div className="text-sm text-fg-primary">
        {indexed} files indexed
        {data.total ? ` / ${data.total}` : ""}
      </div>
      {data.error && (
        <div className="text-xs text-error mt-1">{data.error}</div>
      )}
    </div>
  );
}
