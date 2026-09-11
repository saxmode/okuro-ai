import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, schedulesApi, type ScheduleTask } from "@/lib/api";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { MetricCard } from "@/components/ui/metric-card";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { ScheduleEditor } from "@/components/schedule/schedule-editor";
import { KindBadge, TierBadge, EmbedsBadge } from "@/components/schedule/kind-badge";
import {
  IdentityCell,
  ScheduleCell,
  TH,
  THEAD_ROW,
} from "@/components/schedule/cells";
import { humanizeCron } from "@/lib/format";
import { parseApiDate } from "@/lib/format";

// The daemon cron jobs (Layer A). Enable/disable + edit-cron write an
// override to ~/.okuro/daemon/config.yaml and SIGHUP the daemon so the
// change applies live (no restart). Shared by Health → Schedules and the
// unified Scheduled page.
export function DaemonJobsPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["schedules"],
    queryFn: () => api<{ tasks: ScheduleTask[]; count: number }>("/api/schedules"),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  if (isLoading) return <LoadingSkeleton lines={5} />;
  if (error) {
    return (
      <EmptyState
        title="Schedules endpoint unreachable"
        description={error instanceof Error ? error.message : "Unknown error"}
      />
    );
  }
  if (!data || data.tasks.length === 0) {
    return <EmptyState title="No scheduled tasks" />;
  }

  const enabled = data.tasks.filter((t) => t.enabled).length;
  const disabled = data.tasks.length - enabled;
  // Count only ENABLED tasks per kind — a disabled LLM task costs nothing, so
  // folding it into the model-call count would overstate what actually runs.
  const live = data.tasks.filter((t) => t.enabled);
  const byKind = (k: string) => live.filter((t) => t.kind === k).length;
  const callsModel = live.filter(
    (t) => t.kind === "llm" || t.kind === "mixed",
  ).length;

  return (
    <section className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        <MetricCard label="Enabled" value={enabled} accent={enabled > 0} />
        <MetricCard label="Disabled" value={disabled} />
        <MetricCard label="Total" value={data.tasks.length} />
      </div>

      <div className="grid grid-cols-3 gap-3">
        <MetricCard label="Script" value={byKind("script")} />
        <MetricCard
          label="Calls a model"
          value={callsModel}
          accent={callsModel > 0}
        />
        <MetricCard label="Orchestrator" value={byKind("orchestrator")} />
      </div>

      <SectionLabel className="mb-2">Daemon tasks</SectionLabel>
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full text-xs">
          <thead className="border-b border-border bg-surface">
            <tr className={THEAD_ROW}>
              <th className={TH}>On</th>
              <th className={TH}>Job</th>
              <th className={TH}>Kind</th>
              <th className={TH}>Tier</th>
              <th className={TH}>Schedule</th>
              <th className={TH}>Next run</th>
              <th className={`${TH} w-px`} aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {data.tasks.map((t) => (
              <DaemonJobRow key={t.id} task={t} />
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-2xs text-tertiary">
        Changes apply live — the daemon reloads on save, no restart needed.
      </p>
    </section>
  );
}

// One daemon-task row: enable/disable Switch + expandable cron editor.
function DaemonJobRow({ task }: { task: ScheduleTask }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);

  const announce = (
    res: { applied: boolean; warning: string | null },
    msg: string,
  ) => {
    qc.invalidateQueries({ queryKey: ["schedules"] });
    if (res.applied) {
      toast.success(msg, { description: task.id });
    } else {
      toast.warning(msg, {
        description: res.warning ?? "applies on next daemon start",
      });
    }
  };

  const failed = (label: string) => (err: unknown) =>
    toast.error(label, {
      description: err instanceof Error ? err.message : String(err),
    });

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) => schedulesApi.patch(task.id, { enabled }),
    onSuccess: (res, enabled) =>
      announce(res, enabled ? "Task enabled" : "Task disabled"),
    onError: failed("Failed to toggle task"),
  });

  const cronMutation = useMutation({
    mutationFn: (cron: string) => schedulesApi.patch(task.id, { cron }),
    onSuccess: (res) => {
      announce(res, "Schedule updated");
      setEditing(false);
    },
    onError: failed("Failed to update schedule"),
  });

  return (
    <>
      <tr className="border-t border-border-subtle">
        <td className="w-px px-3 py-2 align-top">
          <Switch
            size="sm"
            checked={task.enabled}
            onCheckedChange={(v) => toggleMutation.mutate(v)}
            disabled={toggleMutation.isPending}
            aria-label={task.enabled ? "Disable task" : "Enable task"}
          />
        </td>
        {/* Description rides under the id instead of owning a greedy column
            — that column was what squeezed the id into wrapping. */}
        <IdentityCell id={task.id} sub={task.description} />
        <td className="px-3 py-2 align-top">
          <div className="flex items-center gap-1">
            <KindBadge kind={task.kind} />
            <EmbedsBadge embeds={task.embeds} />
          </div>
        </td>
        <td className="px-3 py-2 align-top">
          <TierBadge tier={task.tier} />
        </td>
        <ScheduleCell expr={task.cron} humanize={humanizeCron} />
        <td className="whitespace-nowrap px-3 py-2 align-top text-tertiary">
          {task.next_run
            ? parseApiDate(task.next_run).toLocaleString()
            : task.enabled
              ? "—"
              : "disabled"}
        </td>
        <td className="w-px px-3 py-2 text-right align-top">
          <Button
            size="sm"
            variant="outline"
            className="h-6 px-2 text-2xs"
            onClick={() => setEditing((v) => !v)}
          >
            {editing ? "Cancel" : "Edit"}
          </Button>
        </td>
      </tr>
      {editing && (
        <tr className="border-t border-border-subtle bg-surface">
          <td colSpan={7} className="px-3 py-2">
            <ScheduleEditor
              value={task.cron}
              onSave={(cron) => cronMutation.mutate(cron)}
              onClose={() => setEditing(false)}
              saving={cronMutation.isPending}
            />
          </td>
        </tr>
      )}
    </>
  );
}
