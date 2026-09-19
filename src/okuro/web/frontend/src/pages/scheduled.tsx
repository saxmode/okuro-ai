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
  Dialog,
  DialogContent,
  DialogDescription,
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
import { usePaneInterval } from "@/lib/pane-active";
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
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">Scheduled</h1>` above this pane, so the page's
          own PageHeader h1 is gone.

          THE SUBTITLE SURVIVES AS CONTENT, and here that is not a courtesy:
          it names the three layers the page stacks, and a reader who does not
          know that "recurring runs", "daemon jobs" and "system timers" are
          three different schedulers cannot tell the panels apart. The START
          pass kept its two subtitles for the same reason.

          `text-fg-muted` rather than `PageHeader`'s `text-tertiary`: that
          token is the standing AA failure (kit todo c581c9b2), and re-homing
          the line was the chance to stop feeding it. */}
      <p className="type-small text-fg-muted">
        Everything that runs on a clock — recurring runs, daemon jobs, system
        timers.
      </p>
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
  /**
   * THE DELETE CONFIRM LIVES HERE, NOT IN THE ROW, AND THE CONSOLE IS WHY.
   *
   * `DialogContent` renders an IN-TREE `<span {...probe} />` marker before it
   * portals (`ui/dialog.tsx:116`) — it carries the ground the dialog was
   * declared on, so the scrim gets the right alpha. A row renders inside
   * `<tbody>`, and a `<span>` there is invalid HTML: React logged
   * "<tbody> cannot contain a nested <span>" and "In HTML, <span> cannot be a
   * child of <tbody>. This will cause a hydration error." twice per page in
   * all 12 contract states, while the modal itself looked and behaved
   * perfectly. So the panel owns one dialog and the rows ask for it, which is
   * also the shape `components/models/vram-reclaim-modal.tsx` already uses.
   */
  const [pendingDelete, setPendingDelete] = useState<RecurringDef | null>(null);
  const qcPanel = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: (id: string) => recurringApi.remove(id),
    onSuccess: (_r, id) => {
      toast.success("Deleted", { description: pendingDelete?.title ?? id });
      setPendingDelete(null);
      qcPanel.invalidateQueries({ queryKey: ["recurring"] });
    },
    onError: (err: unknown) =>
      toast.error("Failed to delete", {
        description: err instanceof Error ? err.message : String(err),
      }),
  });
  const { data, isLoading, error } = useQuery({
    queryKey: ["recurring"],
    queryFn: () => recurringApi.list(),
    // L4 / T9 — POLL ONLY WHILE THE PANE IS ON SCREEN. Law 3 keeps all five
    // topic panes mounted, so an unguarded interval keeps asking while the
    // user is four topics away. MEASURED before this line, real topic-bar
    // clicks (not `goto`, which remounts the document), 70-second windows:
    // on /system/scheduled 3 calls, at /know/repos with all 5 panes still
    // mounted 3 calls, back on the route 3 calls.
    refetchInterval: usePaneInterval(60_000),
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
                  <RecurringRow
                    key={d.id}
                    def_={d}
                    onRequestDelete={() => setPendingDelete(d)}
                    deleting={
                      deleteMutation.isPending && pendingDelete?.id === d.id
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* R5 (372ccdb2), PERMANENT BRANCH — A MODAL, NOT AN ARM.
          The p3 spec (542bd096 §2) records "no confirm found — 0 `confirm(`,
          no Dialog, no two-step arm". That is wrong and was already wrong at
          its own measurement HEAD 276fc2a80: a `confirmDelete` arm had been in
          this file the whole time. So this was not a missing confirm, it was
          the WRONG KIND of one. `DELETE /api/recurring/{id}` runs
          `def_path.unlink()` (orchestrator/api/main.py:3516) — no soft delete,
          no archive, nothing to undo — and R5 reserves the arm for reversible
          acts.

          `ui/dialog` steers `open` through `usePaneModalOpen`, so this cannot
          survive a topic change and leave the app unclickable (the class fix
          from commit 164647757). */}
      <Dialog
        open={!!pendingDelete}
        onOpenChange={(o) => !o && setPendingDelete(null)}
      >
        <DialogContent className="max-w-md">
          {pendingDelete && (
            <>
              <DialogHeader>
                <DialogTitle>Delete “{pendingDelete.title}”?</DialogTitle>
                <DialogDescription>
                  {/* THE PATH WAS MEASURED, NOT GUESSED. `OKURO_ROOT` defaults
                      to `okuro_home() / "orchestrator"` (main.py:122), so the
                      file is under `~/.okuro/orchestrator/recurring/`, not
                      `~/.okuro/recurring/` — which is what this sentence said
                      until `ls` disagreed with it. */}
                  This removes the definition file{" "}
                  <span className="font-mono">
                    ~/.okuro/orchestrator/recurring/{pendingDelete.id}.yaml
                  </span>{" "}
                  and the run stops firing. Its past outcomes stay in the run
                  history, but the schedule, prompt and role are gone and okuro
                  cannot put them back — you would write the definition again.
                  <br />
                  To stop it without losing it, switch it{" "}
                  <span className="text-fg">off</span> instead.
                </DialogDescription>
              </DialogHeader>
              <div className="flex justify-end gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPendingDelete(null)}
                >
                  Cancel
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={deleteMutation.isPending}
                  onClick={() => deleteMutation.mutate(pendingDelete.id)}
                >
                  Delete definition
                </Button>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}

function RecurringRow({
  def_,
  onRequestDelete,
  deleting,
}: {
  def_: RecurringDef;
  onRequestDelete: () => void;
  deleting: boolean;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
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
            {/* R5's permanent branch is a MODAL, and the panel owns it —
                see `RecurringPanel`'s `pendingDelete` for why it cannot live
                in this row. */}
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 text-2xs text-error"
              onClick={onRequestDelete}
              disabled={deleting}
              title="Delete this scheduled run"
              aria-label={`Delete ${def_.title}`}
            >
              <Trash2 className="h-3 w-3" />
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
    // Same measurement, same rule — see RecurringPanel above.
    refetchInterval: usePaneInterval(60_000),
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
