import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { modelsApi, type GpuHolder, type GpuInfo } from "@/lib/models-api";

/**
 * VRAM contention modal. Shows what holds each GPU (okuro's own leases +
 * external servers) and lets the user reclaim VRAM — a graceful unload where
 * the holder supports it, or a confirm-gated process stop where it doesn't.
 * Inform-first: nothing is unloaded without an explicit click.
 */
export function VramReclaimModal({
  open,
  onClose,
  neededGb,
}: {
  open: boolean;
  onClose: () => void;
  neededGb?: number;
}) {
  const qc = useQueryClient();
  const [confirmPid, setConfirmPid] = useState<number | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["models", "gpu"],
    queryFn: () => modelsApi.gpu(),
    enabled: open,
    refetchInterval: open ? 5000 : false,
  });

  const reclaim = useMutation({
    mutationFn: ({ holder, confirm }: { holder: GpuHolder; confirm: boolean }) =>
      modelsApi.reclaim(holder, confirm),
    onSuccess: (r, { holder }) => {
      if (r.ok) {
        toast.success(`Reclaimed ${holder.tenant}`);
        qc.invalidateQueries({ queryKey: ["models", "gpu"] });
      } else {
        toast.error(`Could not reclaim ${holder.tenant}`, { description: r.reason });
      }
      setConfirmPid(null);
    },
    onError: (e) => toast.error("Reclaim failed", { description: String(e) }),
  });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>GPU memory</DialogTitle>
          <DialogDescription>
            {neededGb
              ? `okuro needs ${neededGb} GB. Free VRAM or unload a holder below.`
              : "What holds each GPU. Unload a holder to free VRAM for okuro."}
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <p className="text-sm text-tertiary">Reading GPUs…</p>
        ) : (
          <div className="space-y-4">
            {(data?.gpus ?? []).map((g) => (
              <GpuRow
                key={g.gpu_index}
                g={g}
                confirmPid={confirmPid}
                onStopArm={setConfirmPid}
                onReclaim={(holder, confirm) => reclaim.mutate({ holder, confirm })}
                pending={reclaim.isPending}
              />
            ))}
            {(data?.gpus ?? []).length === 0 && (
              <p className="text-sm text-tertiary">No GPUs detected.</p>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function GpuRow({
  g,
  confirmPid,
  onStopArm,
  onReclaim,
  pending,
}: {
  g: GpuInfo;
  confirmPid: number | null;
  onStopArm: (pid: number | null) => void;
  onReclaim: (holder: GpuHolder, confirm: boolean) => void;
  pending: boolean;
}) {
  const idle = g.okuro_leases.length === 0 && g.external.length === 0;
  return (
    <div className="rounded-md border border-border-subtle p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="text-sm font-medium text-fg">GPU {g.gpu_index}</span>
        <span className="text-xs text-fg-subtle">
          {g.total_gb.toFixed(0)}GB total
          {g.smi_free_gb != null && ` · ${g.smi_free_gb.toFixed(1)}GB free`}
        </span>
      </div>

      {g.okuro_leases.map((l) => (
        <div key={l.model_id} className="flex items-center gap-2 py-0.5 text-xs">
          <span className="w-16 text-success">okuro</span>
          <span className="flex-1 truncate text-fg-muted">{l.model_id}</span>
          <span className="text-fg-subtle">
            {l.tier} · {l.vram_gb.toFixed(1)}GB
          </span>
        </div>
      ))}

      {g.external.map((h) => (
        <div key={h.pid} className="flex items-center gap-2 py-0.5 text-xs">
          <span className="w-16 truncate text-warning">{h.tenant}</span>
          <span className="flex-1 truncate text-fg-muted">
            pid {h.pid}
            {h.models.length > 0 && ` · ${h.models.slice(0, 2).join(", ")}`}
          </span>
          <span className="text-fg-subtle">{(h.vram_mb / 1024).toFixed(1)}GB</span>
          {h.reclaim === "graceful_api" && (
            <Button size="xs" onClick={() => onReclaim(h, false)} disabled={pending}>
              Unload
            </Button>
          )}
          {h.reclaim === "process_stop" &&
            (confirmPid === h.pid ? (
              <Button
                size="xs"
                variant="destructive"
                onClick={() => onReclaim(h, true)}
                disabled={pending}
              >
                Confirm stop
              </Button>
            ) : (
              <Button size="xs" variant="outline" onClick={() => onStopArm(h.pid)}>
                Stop
              </Button>
            ))}
          {h.reclaim === "none" && <IdentifyCell holder={h} />}
        </div>
      ))}

      {idle && <p className="text-xs italic text-fg-subtle">idle</p>}
    </div>
  );
}

/** Unknown holder: an "Identify" affordance (extended patterns → fast model),
 * read-only. Shows the resolved label inline once fetched. */
function IdentifyCell({ holder }: { holder: GpuHolder }) {
  const identify = useMutation({
    mutationFn: () => modelsApi.identify(holder.pid, holder.tenant),
  });
  if (identify.data) {
    return <span className="text-2xs text-fg-subtle">{identify.data.label}</span>;
  }
  return (
    <Button
      size="xs"
      variant="ghost"
      onClick={() => identify.mutate()}
      disabled={identify.isPending}
    >
      {identify.isPending ? "…" : "Identify"}
    </Button>
  );
}
