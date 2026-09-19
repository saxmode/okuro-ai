import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { usePaneInterval } from "@/lib/pane-active";
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
 *
 * ---------------------------------------------------------------------------
 * R5 (372ccdb2) — THE TWO RECLAIMS ARE NOT THE SAME KIND OF ACT, SO THEY DO
 * NOT GET THE SAME CONFIRM.
 * ---------------------------------------------------------------------------
 * The ruling is "modal dialog for permanent deletions, two-step arm for
 * reversible actions", and these two endpoints sat on the wrong side of it in
 * both directions:
 *
 *   graceful_api  ->  POST /reclaim {confirm:false}. Asks a server to unload a
 *                     model; it reloads on its next request. REVERSIBLE, and it
 *                     had NO confirm at all — one click took VRAM out from
 *                     under somebody else's running server. Now the two-step
 *                     arm.
 *   process_stop  ->  POST /reclaim {confirm:true}. Ends a PROCESS. Whatever it
 *                     was computing is gone and okuro cannot restart it — only
 *                     the program's own owner can. That is as permanent as this
 *                     leaf gets, and it had the two-step arm, which R5 reserves
 *                     for the reversible case. Now a modal that names the
 *                     tenant, the pid and what cannot be undone.
 *
 * ARMED PER HOLDER AND PER ACTION. `armedUnload` holds a pid and the stop
 * confirm holds a whole holder, so they are separate pieces of state by
 * construction — arming Unload on one holder can never arm Stop on it, and can
 * never arm Unload on a different one. That is the cross-arm bug the TASKS
 * pattern has (`tasks.tsx:188` tests one flag for every row) and that
 * `InboxRow.tsx:95` already records rather than copies.
 *
 * WHY A NESTED DIALOG IS THE RIGHT SHAPE HERE and not an inline panel: this
 * surface is itself a modal, and the thing being confirmed is not the thing
 * the surface is for. Radix stacks dialogs (1.1.15) — the GPU list stays open
 * behind, so cancelling returns to exactly the row the user was reading.
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
  /** R5 reversible branch — which holder's Unload is armed. */
  const [armedUnload, setArmedUnload] = useState<number | null>(null);
  /** R5 permanent branch — the holder whose process stop is being confirmed. */
  const [confirmStop, setConfirmStop] = useState<GpuHolder | null>(null);

  /**
   * A CLOSED CONFIRM MUST NOT COME BACK ARMED. Radix unmounts `DialogContent`,
   * so the nested confirm disappears with the list — but the STATE behind it
   * does not, and reopening the GPU modal would have shown a stop confirm the
   * user never asked for a second time.
   */
  useEffect(() => {
    if (!open) {
      setArmedUnload(null);
      setConfirmStop(null);
    }
  }, [open]);

  /**
   * THE 5s POLL STOPS WHEN THE PANE LEAVES THE SCREEN — pane-active adoption
   * (ae294098). `enabled: open` already stopped it when the modal is closed;
   * what it could not see is that Law 3 keeps this leaf's pane mounted through
   * a TOPIC change, so an open modal kept polling from off-screen. Measured on
   * an isolated chromium with the pathname asserted: 2 calls per 12 s at
   * `/know/brain` with the modal still up.
   *
   * `usePaneInterval` takes `number | false`, so the existing condition stays
   * exactly as it was and only gains the second gate.
   */
  const { data, isLoading } = useQuery({
    queryKey: ["models", "gpu"],
    queryFn: () => modelsApi.gpu(),
    enabled: open,
    refetchInterval: usePaneInterval(open ? 5000 : false),
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
      setArmedUnload(null);
      setConfirmStop(null);
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
                armedUnload={armedUnload}
                onUnloadArm={setArmedUnload}
                onStopRequest={setConfirmStop}
                onReclaim={(holder, confirm) => reclaim.mutate({ holder, confirm })}
                pending={reclaim.isPending}
              />
            ))}
            {(data?.gpus ?? []).length === 0 && (
              <p className="text-sm text-tertiary">No GPUs detected.</p>
            )}
          </div>
        )}

        {/* R5, permanent branch. Stacked on the GPU list rather than replacing
            it, so Cancel returns to the row the user was reading. */}
        <Dialog
          open={!!confirmStop}
          onOpenChange={(o) => !o && setConfirmStop(null)}
        >
          <DialogContent className="max-w-md">
            {confirmStop && (
              <>
                <DialogHeader>
                  <DialogTitle>Stop {confirmStop.tenant}?</DialogTitle>
                  <DialogDescription>
                    This ends process {confirmStop.pid} and frees{" "}
                    {(confirmStop.vram_mb / 1024).toFixed(1)}GB. It does not
                    delete any model file — but whatever the process was
                    computing is lost, and okuro cannot start it again: only
                    whoever launched it can.
                  </DialogDescription>
                </DialogHeader>
                <div className="flex justify-end gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setConfirmStop(null)}
                  >
                    Cancel
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    disabled={reclaim.isPending}
                    onClick={() => reclaim.mutate({ holder: confirmStop, confirm: true })}
                  >
                    Stop process
                  </Button>
                </div>
              </>
            )}
          </DialogContent>
        </Dialog>
      </DialogContent>
    </Dialog>
  );
}

function GpuRow({
  g,
  armedUnload,
  onUnloadArm,
  onStopRequest,
  onReclaim,
  pending,
}: {
  g: GpuInfo;
  armedUnload: number | null;
  onUnloadArm: (pid: number | null) => void;
  onStopRequest: (holder: GpuHolder) => void;
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
          {h.reclaim === "graceful_api" &&
            (armedUnload === h.pid ? (
              <Button
                size="xs"
                variant="destructive"
                onClick={() => onReclaim(h, false)}
                disabled={pending}
                aria-label={`Confirm unload ${h.tenant}`}
              >
                Confirm unload
              </Button>
            ) : (
              <Button
                size="xs"
                onClick={() => onUnloadArm(h.pid)}
                aria-label={`Unload ${h.tenant}`}
              >
                Unload
              </Button>
            ))}
          {h.reclaim === "process_stop" && (
            <Button
              size="xs"
              variant="outline"
              onClick={() => onStopRequest(h)}
              aria-label={`Stop ${h.tenant}`}
            >
              Stop
            </Button>
          )}
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
