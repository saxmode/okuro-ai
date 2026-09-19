import { useDashboardGpu, useHost } from "@/hooks/use-dashboard";
import { Progress } from "@/components/ui/progress";

export function SystemPanel() {
  const { data: host } = useHost();
  const { data: gpu } = useDashboardGpu();
  const gpus = gpu?.gpus ?? [];

  if (!host && gpus.length === 0) return null;

  return (
    <div className="space-y-3">
      {host && (
        <>
          <MetricRow
            label="CPU"
            sub={
              host.cpu.count_physical
                ? `${host.cpu.count_physical}c / ${host.cpu.count_logical}t`
                : `${host.cpu.count_logical}t`
            }
            pct={host.cpu.utilization_percent}
          />
          <MetricRow
            label="RAM"
            sub={`${(host.memory.used_mb / 1024).toFixed(1)} / ${(host.memory.total_mb / 1024).toFixed(1)} GB`}
            pct={host.memory.utilization_percent}
          />
          {host.swap.total_mb > 0 && host.swap.utilization_percent > 5 && (
            <MetricRow
              label="SWAP"
              sub={`${(host.swap.used_mb / 1024).toFixed(1)} / ${(host.swap.total_mb / 1024).toFixed(1)} GB`}
              pct={host.swap.utilization_percent}
            />
          )}
        </>
      )}

      {gpus.map((g) => {
        const totalMb = g.vram?.total_mb ?? 0;
        const usedMb = g.vram?.used_mb ?? 0;
        const pct = totalMb > 0 ? Math.round((usedMb / totalMb) * 100) : 0;
        return (
          <div key={g.id} className="space-y-1">
            {/* THE SLOT AND THE HARDWARE ARE TWO DIFFERENT FIELDS, AND ONLY
                ONE OF THEM WAS ON SCREEN. `/api/gpu` carries `name` — the slot,
                "GPU0" — and `model`, the card. This panel rendered `name`
                alone, so HEALTH's Machine tab read "GPU0" and "GPU1" and never
                said what is in the machine. Memory 245d4942 flagged exactly
                this field pair; it is the one half of its third fix that was
                still open.

                SLOT FIRST, because the slot is how every other okuro surface
                addresses a GPU — the broker, the lease, the allocator — so leading
                with the model would cost a cross-reference. The model
                truncates with the native tooltip, which is the rule
                `schedule/cells.tsx:8-10` already states for identifiers, and
                it needs to: the live strings are "NVIDIA RTX 6000 Ada
                Generation" and "NVIDIA RTX PRO 4000 Blackwell" in a row that
                is already tight at a 751.63px pane.

                This is WORK/AGENTS' panel too (`pages/dashboard.tsx`), so the
                change is a two-leaf change by construction. */}
            <div className="flex items-baseline justify-between gap-2">
              <span className="min-w-0 text-2xs font-medium text-fg-muted">
                {g.name}
                {g.model ? (
                  <span
                    className="ml-1.5 truncate font-normal text-tertiary"
                    title={g.model}
                  >
                    · {g.model}
                  </span>
                ) : null}
              </span>
              <span className="shrink-0 text-3xs text-tertiary">
                {g.temperature_c != null ? `${g.temperature_c}°C` : "—"}
              </span>
            </div>
            <Progress value={pct} className="h-1.5" />
            <div className="flex justify-between text-3xs text-tertiary">
              <span>
                {(usedMb / 1024).toFixed(1)}/{(totalMb / 1024).toFixed(1)} GB
              </span>
              <span>{pct}%</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function MetricRow({
  label,
  sub,
  pct,
}: {
  label: string;
  sub: string;
  pct: number;
}) {
  const rounded = Math.round(pct);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-2xs font-medium text-fg-muted">{label}</span>
        <span className="text-3xs text-tertiary">{sub}</span>
      </div>
      <Progress value={rounded} className="h-1.5" />
      <div className="flex justify-end text-3xs text-tertiary">
        <span>{rounded}%</span>
      </div>
    </div>
  );
}
