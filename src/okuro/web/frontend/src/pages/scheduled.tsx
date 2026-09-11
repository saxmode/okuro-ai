import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import {
  recurringApi,
  timersApi,
  roleApi,
  type SystemTimer,
} from "@/lib/api";
import type { RecurringDef } from "@/types/api";
import { PageHeader } from "@/components/shell/page-header";
import { SectionLabel } from "@/components/ui/section-label";
import { MetricCard } from "@/components/ui/metric-card";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FieldLabel } from "@/components/ui/form-primitives";
import { toast } from "@/components/ui/toast";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ScheduleEditor,
  CRON_PRESETS,
  validateCron,
} from "@/components/schedule/schedule-editor";
import { DaemonJobsPanel } from "@/components/schedule/daemon-jobs-panel";
import { KindBadge, TierBadge } from "@/components/schedule/kind-badge";
import {
  IdentityCell,
  ScheduleCell,
  TH,
  THEAD_ROW,
} from "@/components/schedule/cells";
import {
  formatDuration,
  humanizeCron,
  humanizeSystemdCalendar,
} from "@/lib/format";

// One place for everything that runs on a clock, across three layers:
//   Recurring runs  — orchestrator role+prompt jobs (create/edit/delete here)
//   Daemon jobs     — okuro-daemon builtin cron tasks (shared panel)
//   System timers   — systemd --user .timer units (enable/disable)
export function ScheduledPage() {
  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Scheduled"
        subtitle="Everything that runs on a clock — recurring runs, daemon jobs, system timers"
      />
      <RecurringPanel />
      <div className="space-y-3">
        <SectionLabel>Daemon jobs</SectionLabel>
        <DaemonJobsPanel />
      </div>
      <TimersPanel />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recurring orchestrator runs (Layer B) — create / pause / edit-cron / delete
// ---------------------------------------------------------------------------

function RecurringPanel() {
  const [creating, setCreating] = useState(false);
  const { data, isLoading, error } = useQuery({
    queryKey: ["recurring"],
    queryFn: () => recurringApi.list(),
    refetchInterval: 60_000,
  });

  const defs = data?.definitions ?? [];
  const active = defs.filter((d) => d.status === "scheduled").length;
  const paused = defs.length - active;

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <SectionLabel>Recurring runs</SectionLabel>
        <Button
          size="sm"
          variant="outline"
          className="h-7 px-2 text-2xs"
          onClick={() => setCreating((v) => !v)}
        >
          <Plus className="mr-1 h-3 w-3" />
          {creating ? "Close" : "New scheduled run"}
        </Button>
      </div>

      {creating && <CreateScheduledRun onDone={() => setCreating(false)} />}

      {isLoading ? (
        <LoadingSkeleton lines={3} />
      ) : error ? (
        <EmptyState
          title="Recurring endpoint unreachable"
          description={error instanceof Error ? error.message : "Unknown error"}
        />
      ) : defs.length === 0 ? (
        <EmptyState
          title="No recurring runs yet"
          description="Create one to run a role on a schedule."
        />
      ) : (
        <>
          <div className="grid grid-cols-3 gap-3">
            <MetricCard label="Scheduled" value={active} accent={active > 0} />
            <MetricCard label="Paused" value={paused} />
            <MetricCard label="Total" value={defs.length} />
          </div>
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-xs">
              <thead className="border-b border-border bg-surface">
                <tr className={THEAD_ROW}>
                  <th className={TH}>On</th>
                  <th className={TH}>Run</th>
                  <th className={TH}>Kind</th>
                  <th className={TH}>Tier</th>
                  <th className={TH}>Role(s)</th>
                  <th className={TH}>Schedule</th>
                  <th className={TH}>Next run</th>
                  <th className={`${TH} w-px`} aria-label="actions" />
                </tr>
              </thead>
              <tbody>
                {defs.map((d) => (
                  <RecurringRow key={d.id} def_={d} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function RecurringRow({ def_ }: { def_: RecurringDef }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const isActive = def_.status === "scheduled";

  const failed = (label: string) => (err: unknown) =>
    toast.error(label, {
      description: err instanceof Error ? err.message : String(err),
    });
  const refresh = () => qc.invalidateQueries({ queryKey: ["recurring"] });

  const pauseMutation = useMutation({
    mutationFn: (paused: boolean) => recurringApi.patch(def_.id, { paused }),
    onSuccess: (_r, paused) => {
      toast.success(paused ? "Paused" : "Resumed", { description: def_.title });
      refresh();
    },
    onError: failed("Failed to toggle"),
  });

  const cronMutation = useMutation({
    mutationFn: (cron: string) => recurringApi.patch(def_.id, { schedule: cron }),
    onSuccess: () => {
      toast.success("Schedule updated", { description: def_.title });
      refresh();
      setEditing(false);
    },
    onError: failed("Failed to update schedule"),
  });

  const deleteMutation = useMutation({
    mutationFn: () => recurringApi.remove(def_.id),
    onSuccess: () => {
      toast.success("Deleted", { description: def_.title });
      refresh();
    },
    onError: failed("Failed to delete"),
  });

  const roles = def_.roles?.length ? def_.roles : def_.role ? [def_.role] : [];

  return (
    <>
      <tr className="border-t border-border-subtle">
        <td className="w-px px-3 py-2 align-top">
          <Switch
            size="sm"
            checked={isActive}
            onCheckedChange={(v) => pauseMutation.mutate(!v)}
            disabled={pauseMutation.isPending}
            aria-label={isActive ? "Pause run" : "Resume run"}
          />
        </td>
        {/* Title reads as prose, the def id underneath is the identifier. */}
        <IdentityCell id={def_.title} sub={def_.id} mono={false} />
        <td className="px-3 py-2 align-top">
          <KindBadge kind={def_.kind} />
        </td>
        <td className="px-3 py-2 align-top">
          <TierBadge tier={def_.tier} />
        </td>
        <td className="max-w-[28ch] truncate whitespace-nowrap px-3 py-2 align-top font-mono text-tertiary" title={roles.join(", ")}>
          {roles.join(", ")}
        </td>
        <ScheduleCell expr={def_.schedule} humanize={humanizeCron} />
        <td className="whitespace-nowrap px-3 py-2 align-top text-tertiary">
          {!isActive
            ? "paused"
            : def_.next_run_seconds != null
              ? `in ${formatDuration(def_.next_run_seconds)}`
              : "—"}
        </td>
        <td className="w-px px-3 py-2 text-right align-top">
          <div className="flex items-center justify-end gap-1">
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 text-2xs"
              onClick={() => setEditing((v) => !v)}
            >
              {editing ? "Cancel" : "Edit"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 text-2xs text-error"
              onClick={() => {
                if (confirmDelete) deleteMutation.mutate();
                else setConfirmDelete(true);
              }}
              disabled={deleteMutation.isPending}
              title="Delete this scheduled run"
            >
              <Trash2 className="h-3 w-3" />
              {confirmDelete ? "Confirm" : ""}
            </Button>
          </div>
        </td>
      </tr>
      {editing && (
        <tr className="border-t border-border-subtle bg-surface">
          <td colSpan={8} className="px-3 py-2">
            <ScheduleEditor
              value={def_.schedule}
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

// Create form — a recurring orchestrator run = title + role + prompt + cron.
function CreateScheduledRun({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [role, setRole] = useState("");
  const [prompt, setPrompt] = useState("");
  const [cron, setCron] = useState("0 6 * * *");

  const { data: rolesData } = useQuery({
    queryKey: ["roles"],
    queryFn: () => roleApi.list(),
    staleTime: 300_000,
  });
  const roles = useMemo(
    () => [...(rolesData?.roles ?? [])].sort((a, b) => a.id.localeCompare(b.id)),
    [rolesData],
  );

  const cronError = validateCron(cron);
  const canSubmit =
    title.trim() && role && prompt.trim() && !cronError;

  const createMutation = useMutation({
    mutationFn: () =>
      recurringApi.create({
        title: title.trim(),
        description: prompt.trim(),
        role,
        schedule: cron.trim(),
      }),
    onSuccess: (res) => {
      toast.success("Scheduled run created", { description: res.title });
      qc.invalidateQueries({ queryKey: ["recurring"] });
      onDone();
    },
    onError: (err: unknown) =>
      toast.error("Failed to create", {
        description: err instanceof Error ? err.message : String(err),
      }),
  });

  return (
    <div className="space-y-3 rounded-md border border-border bg-surface-elevated p-3">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <FieldLabel>Title</FieldLabel>
          <Input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Weekly repo audit"
          />
        </div>
        <div>
          <FieldLabel>Role</FieldLabel>
          <Select value={role} onValueChange={setRole}>
            <SelectTrigger>
              <SelectValue placeholder="Pick a role…" />
            </SelectTrigger>
            <SelectContent>
              {roles.map((r) => (
                <SelectItem key={r.id} value={r.id}>
                  {r.id}
                  <span className="text-tertiary"> · {r.domain}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div>
        <FieldLabel>Prompt</FieldLabel>
        <Textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="What should the run do each time it fires?"
          rows={3}
        />
      </div>

      <div>
        <FieldLabel>Schedule (cron)</FieldLabel>
        <Input
          value={cron}
          onChange={(e) => setCron(e.target.value)}
          placeholder="0 6 * * *"
          className="font-mono text-sm"
        />
        {cronError ? (
          <p className="mt-1 text-2xs text-error">{cronError}</p>
        ) : (
          <p className="mt-1 text-2xs text-tertiary">{humanizeCron(cron)}</p>
        )}
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {CRON_PRESETS.map((p) => (
            <Button
              key={p.value}
              type="button"
              size="sm"
              variant="outline"
              className="h-6 px-2 text-2xs"
              onClick={() => setCron(p.value)}
              title={p.value}
            >
              {p.label}
            </Button>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-2 pt-1">
        <Button
          size="sm"
          className="h-7 text-2xs"
          disabled={!canSubmit || createMutation.isPending}
          onClick={() => createMutation.mutate()}
        >
          {createMutation.isPending ? "Creating…" : "Create"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-2xs"
          type="button"
          onClick={onDone}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// systemd --user timers (Layer C) — list + enable/disable (timing read-only)
// ---------------------------------------------------------------------------

function TimersPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["timers"],
    queryFn: () => timersApi.list(),
    refetchInterval: 60_000,
  });
  const timers = data?.timers ?? [];

  return (
    <section className="space-y-3">
      <SectionLabel>System timers</SectionLabel>
      {isLoading ? (
        <LoadingSkeleton lines={3} />
      ) : error ? (
        <EmptyState
          title="Timers endpoint unreachable"
          description={error instanceof Error ? error.message : "Unknown error"}
        />
      ) : timers.length === 0 ? (
        <EmptyState title="No systemd --user timers" />
      ) : (
        <>
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-xs">
              <thead className="border-b border-border bg-surface">
                <tr className={THEAD_ROW}>
                  <th className={TH}>On</th>
                  <th className={TH}>Unit</th>
                  <th className={TH}>Activates</th>
                  <th className={TH}>Schedule</th>
                  <th className={TH}>Next fire</th>
                </tr>
              </thead>
              <tbody>
                {timers.map((t) => (
                  <TimerRow key={t.unit} timer={t} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-2xs text-tertiary">
            Timing (OnCalendar) lives in the unit file — read-only here. Toggle
            enables/disables the timer live.
          </p>
        </>
      )}
    </section>
  );
}

function TimerRow({ timer }: { timer: SystemTimer }) {
  const qc = useQueryClient();
  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) => timersApi.toggle(timer.unit, enabled),
    onSuccess: (_r, enabled) => {
      toast.success(enabled ? "Timer enabled" : "Timer disabled", {
        description: timer.unit,
      });
      qc.invalidateQueries({ queryKey: ["timers"] });
    },
    onError: (err: unknown) =>
      toast.error("Failed to toggle timer", {
        description: err instanceof Error ? err.message : String(err),
      }),
  });

  return (
    <tr className="border-t border-border-subtle">
      <td className="w-px px-3 py-2 align-top">
        <Switch
          size="sm"
          checked={timer.enabled}
          onCheckedChange={(v) => toggleMutation.mutate(v)}
          disabled={toggleMutation.isPending}
          aria-label={timer.enabled ? "Disable timer" : "Enable timer"}
        />
      </td>
      <IdentityCell
        id={timer.unit}
        sub={timer.description}
        trailing={
          timer.okuro_owned ? <StatusBadge tone="info" label="okuro" /> : null
        }
      />
      <td
        className="max-w-[34ch] truncate whitespace-nowrap px-3 py-2 align-top font-mono text-tertiary"
        title={timer.activates ?? undefined}
      >
        {timer.activates ?? "—"}
      </td>
      {/* systemd speaks OnCalendar, not cron — same cell, different grammar. */}
      <ScheduleCell expr={timer.schedule} humanize={humanizeSystemdCalendar} />
      <td className="whitespace-nowrap px-3 py-2 align-top text-tertiary">
        {timer.next_run ?? "—"}
      </td>
    </tr>
  );
}

export default ScheduledPage;
