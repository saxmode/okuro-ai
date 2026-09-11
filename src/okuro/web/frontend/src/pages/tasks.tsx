import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Pause, Pencil, Play, Plus, X, Search } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTasks, useRecurringDefs } from "@/hooks/use-tasks";
import { taskApi, recurringApi } from "@/lib/api";
import { formatDuration, shortId, formatAge, humanizeCron } from "@/lib/format";
import { TONE_TEXT } from "@/lib/color-class";
import type { TaskSummary, RecurringDef } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { toast } from "@/components/ui/toast";
import { ScheduleEditor } from "@/components/schedule/schedule-editor";
import { CreateDialog } from "@/components/task/create-dialog";
import { Segmented } from "@/components/ui/segmented";
import { PageHeader } from "@/components/shell/page-header";

const FILTERS: { label: string; value: string | undefined }[] = [
  { label: "ALL", value: undefined },
  { label: "ACTIVE", value: "active" },
  { label: "DONE", value: "done" },
  { label: "FAILED", value: "failed" },
  { label: "BLOCKED", value: "blocked" },
];

export function TasksPage({
  taskSummariesOverride,
}: {
  /**
   * Test-only injection. When supplied, replaces the live useTasks
   * query with the provided rows so the C10 list-row tests can assert
   * against `task.color_class + task.label` without an HTTP mock.
   */
  taskSummariesOverride?: TaskSummary[];
} = {}) {
  const [filter, setFilter] = useState<string | undefined>(undefined);
  const [search, setSearch] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const { data, isLoading: queryLoading } = useTasks({
    status: filter,
    limit: 200,
  });
  const { data: recurringData } = useRecurringDefs();

  const rows = taskSummariesOverride ?? data?.tasks ?? [];
  const isLoading = !taskSummariesOverride && queryLoading;
  const tasks = rows.filter(
    (t) =>
      !search ||
      t.description?.toLowerCase().includes(search.toLowerCase()) ||
      t.id.includes(search),
  );

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Tasks"
        right={
          <div className="flex items-center gap-2">
            <Button asChild size="sm" variant="outline">
              <Link to="/work/gantt">Gantt</Link>
            </Button>
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              Create
            </Button>
          </div>
        }
      />

      {/* Search + filters */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search tasks..."
            className="bg-surface pl-8 text-sm text-fg placeholder:text-tertiary"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              aria-label="Clear search"
              className="absolute right-2 top-2 text-tertiary hover:text-fg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:rounded-sm"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
        </div>
        <Segmented
          ariaLabel="Filter tasks by status"
          value={filter ?? "all"}
          onChange={(v) => setFilter(v === "all" ? undefined : v)}
          options={FILTERS.map((f) => ({
            label: f.label.charAt(0) + f.label.slice(1).toLowerCase(),
            value: f.value ?? "all",
          }))}
        />
      </div>

      {/* Task instances split into two segments per user model: self-
          invoked (top) vs. recurring cron-spawned (bottom). task_type
          === "recurring" is the orchestrator's own label for instances
          spawned by a RecurringDef on schedule; everything else the user
          or another agent kicked off directly. */}
      {isLoading ? (
        <p className="text-sm text-tertiary">Loading...</p>
      ) : tasks.length === 0 ? (
        <p className="text-sm text-tertiary">No tasks found</p>
      ) : (
        <TaskSegments tasks={tasks} />
      )}

      {/* Recurring definitions (the schedules themselves, not individual
          runs). Kept below the instance lists so the page reads
          top-to-bottom: 'what ran just now' → 'what will run later'. */}
      {recurringData?.definitions && recurringData.definitions.length > 0 && (
        <RecurringSection defs={recurringData.definitions} />
      )}

      <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  );
}

function TaskSegments({ tasks }: { tasks: TaskSummary[] }) {
  const selfInvoked = tasks.filter((t) => t.task_type !== "recurring");
  const recurring = tasks.filter((t) => t.task_type === "recurring");

  return (
    <div className="space-y-8">
      <TaskSegment
        title="Self-invoked"
        subtitle="Tasks you or an agent kicked off directly"
        rows={selfInvoked}
      />
      <TaskSegment
        title="Recurring runs"
        subtitle="Instances spawned by a schedule below"
        rows={recurring}
      />
    </div>
  );
}

function TaskSegment({
  title,
  subtitle,
  rows,
}: {
  title: string;
  subtitle: string;
  rows: TaskSummary[];
}) {
  return (
    <section>
      <div className="mb-2 flex items-baseline gap-3">
        <h2 className="text-xs font-medium uppercase tracking-wider text-tertiary">
          {title}
        </h2>
        <span className="text-2xs text-fg-subtle">{subtitle}</span>
        <span className="ml-auto text-2xs tabular-nums text-fg-subtle">
          {rows.length}
        </span>
      </div>
      {rows.length === 0 ? (
        <p className="text-xs italic text-fg-subtle">None in this view.</p>
      ) : (
        <div className="space-y-1">
          {rows.map((t) => (
            <TaskRow key={t.id} task={t} />
          ))}
        </div>
      )}
    </section>
  );
}

function TaskRow({ task }: { task: TaskSummary }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<"cancel" | "delete" | null>(
    null,
  );

  const handleAction = async (action: "cancel" | "delete") => {
    if (!confirming) {
      setConfirming(action);
      return;
    }
    try {
      if (action === "cancel") await taskApi.cancel(task.id);
      else await taskApi.delete(task.id, task.status === "active");
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    } catch {
      /* ignore */
    }
    setConfirming(null);
  };

  // C10 — TaskSummary carries `color_class` + `label` from the BE.
  // The FE is a pure renderer: tone resolves via TONE_TEXT on the closed
  // 5-token enum; label prints exactly what state_reader supplied.
  const label = task.label ?? (task.status ?? "").toUpperCase();
  const color = task.color_class ? TONE_TEXT[task.color_class] : "text-tertiary";

  return (
    <div className="flex h-row-default items-center gap-4 border-b border-border-subtle px-4 py-3 transition-fast last:border-b-0 hover:bg-surface-elevated">
      <Link
        to={`/work/${task.id}`}
        className="flex flex-1 items-center gap-4 min-w-0"
      >
        <span
          className={`w-16 shrink-0 text-2xs font-semibold uppercase tracking-wider ${color}`}
        >
          {label}
        </span>
        <span className="shrink-0 font-mono text-xs text-fg-subtle">
          {shortId(task.id)}
        </span>
        <span className="flex-1 truncate text-sm text-fg">
          {task.description}
        </span>
        {task.mode === "deliberate" && (
          <Badge variant="outline" className="text-3xs text-info border-info">
            DAG
          </Badge>
        )}
        {task.task_type === "recurring" && (
          <Badge variant="outline" className="text-3xs text-accent border-accent">
            REC
          </Badge>
        )}
        <span className="shrink-0 font-mono text-xs text-fg-subtle tabular-nums">
          {task.subtasks_done}/{task.subtasks_total}
        </span>
        <span className="shrink-0 text-xs text-fg-subtle">
          {formatAge(task.created_at)}
        </span>
      </Link>

      {/* Actions */}
      <div className="flex shrink-0 gap-1">
        {task.status === "active" && (
          <button
            onClick={() => handleAction("cancel")}
            className={`text-3xs uppercase tracking-wider px-1.5 py-0.5 rounded transition-colors ${
              confirming === "cancel"
                ? "bg-error text-inverse"
                : "text-tertiary hover:text-error"
            }`}
          >
            {confirming === "cancel" ? "Confirm" : "Stop"}
          </button>
        )}
        <button
          onClick={() => handleAction("delete")}
          className={`text-3xs uppercase tracking-wider px-1.5 py-0.5 rounded transition-colors ${
            confirming === "delete"
              ? "bg-error text-inverse"
              : "text-tertiary hover:text-error"
          }`}
        >
          {confirming === "delete" ? "Confirm" : "Del"}
        </button>
      </div>
    </div>
  );
}

function RecurringSection({ defs }: { defs: RecurringDef[] }) {
  const [searchParams] = useSearchParams();
  const focusId = searchParams.get("focus");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Auto-expand a focused def when navigated to via /work?focus=<id>.
  // Runs once per focus value change so the user's manual collapse still wins
  // until they re-navigate.
  useEffect(() => {
    if (focusId && defs.some((d) => d.id === focusId)) {
      setExpandedId(focusId);
    }
  }, [focusId, defs]);

  return (
    <section id="recurring">
      <h2 className="mb-2 text-xs font-medium uppercase tracking-wider text-tertiary">
        Recurring Tasks
      </h2>
      <div className="rounded border border-border">
        {defs.map((d, i) => (
          <div
            key={d.id}
            id={`recurring-${d.id}`}
            className={i > 0 ? "border-t border-border" : ""}
          >
            <button
              onClick={() => setExpandedId(expandedId === d.id ? null : d.id)}
              className={`flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition-colors hover:bg-surface-elevated ${
                focusId === d.id ? "ring-1 ring-inset ring-accent/40" : ""
              }`}
            >
              <span className="flex-1 truncate text-fg" title={d.title}>
                {d.title}
              </span>
              {d.status === "paused" && (
                <Badge variant="outline" className="text-3xs border-warning text-warning">
                  PAUSED
                </Badge>
              )}
              {d.adaptive && (
                <Badge variant="outline" className="text-3xs border-accent text-accent">
                  ADAPTIVE
                </Badge>
              )}
              {d.roles.length > 0 ? (
                <span className="text-2xs text-tertiary">
                  {d.roles.length} roles
                </span>
              ) : (
                <span className="text-2xs text-tertiary">{d.role}</span>
              )}
              <span
                className="text-2xs text-tertiary"
                title={d.schedule}
              >
                {humanizeCron(d.schedule)}
              </span>
              {d.status === "paused" ? (
                <span className="text-2xs text-warning">paused</span>
              ) : (
                <span className="text-2xs text-fg-muted">
                  {d.next_run_seconds != null
                    ? formatDuration(d.next_run_seconds)
                    : "—"}
                </span>
              )}
              <span className="text-2xs text-tertiary">
                {d.run_count} runs
              </span>
            </button>

            {expandedId === d.id && (
              <RecurringDetail def_={d} />
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function RecurringDetail({ def_ }: { def_: RecurringDef }) {
  const qc = useQueryClient();
  const [editingSchedule, setEditingSchedule] = useState(false);

  const { data: history } = useQuery({
    queryKey: ["recurringHistory", def_.id],
    queryFn: () => recurringApi.history(def_.id),
  });

  const pauseMutation = useMutation({
    mutationFn: (paused: boolean) =>
      recurringApi.patch(def_.id, { paused }),
    onSuccess: (_res, paused) => {
      toast.success(paused ? "Paused" : "Resumed", {
        description: def_.title,
      });
      qc.invalidateQueries({ queryKey: ["recurring"] });
    },
    onError: (err: unknown) => {
      toast.error("Failed to update", {
        description: err instanceof Error ? err.message : String(err),
      });
    },
  });

  const scheduleMutation = useMutation({
    mutationFn: (cron: string) => recurringApi.patch(def_.id, { schedule: cron }),
    onSuccess: () => {
      toast.success("Schedule updated", { description: def_.title });
      qc.invalidateQueries({ queryKey: ["recurring"] });
      setEditingSchedule(false);
    },
    onError: (err: unknown) => {
      toast.error("Failed to update schedule", {
        description: err instanceof Error ? err.message : String(err),
      });
    },
  });

  const outcomes = history?.outcomes ?? [];
  const usefulCount = outcomes.filter((o) => o.outcome === "useful").length;
  const emptyCount = outcomes.filter((o) => o.outcome === "empty").length;
  const failedCount = outcomes.filter((o) => o.outcome === "failed").length;

  const isPaused = def_.status === "paused";

  return (
    <div className="border-t border-border bg-surface px-3 py-2 space-y-3">
      {/* Schedule row: cron + edit/pause controls */}
      <div className="flex flex-wrap items-center gap-2 text-2xs">
        <span className="text-tertiary">Schedule:</span>
        <span className="text-fg">{humanizeCron(def_.schedule)}</span>
        <code
          className="rounded bg-surface-elevated px-1.5 py-0.5 font-mono text-tertiary"
          title="Raw cron expression"
        >
          {def_.schedule}
        </code>
        {!isPaused && def_.next_run_seconds != null && (
          <span className="text-tertiary">
            next in {formatDuration(def_.next_run_seconds)}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1">
          <Button
            size="sm"
            variant="outline"
            className="h-7 px-2 text-2xs"
            onClick={() => setEditingSchedule((v) => !v)}
          >
            <Pencil className="mr-1 h-3 w-3" />
            {editingSchedule ? "Cancel" : "Edit"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 px-2 text-2xs"
            onClick={() => pauseMutation.mutate(!isPaused)}
            disabled={pauseMutation.isPending}
          >
            {isPaused ? (
              <>
                <Play className="mr-1 h-3 w-3" /> Resume
              </>
            ) : (
              <>
                <Pause className="mr-1 h-3 w-3" /> Pause
              </>
            )}
          </Button>
        </div>
      </div>

      {editingSchedule && (
        <ScheduleEditor
          value={def_.schedule}
          onSave={(cron) => scheduleMutation.mutate(cron)}
          onClose={() => setEditingSchedule(false)}
          saving={scheduleMutation.isPending}
        />
      )}

      {/* Info */}
      <div className="flex gap-4 text-2xs">
        {def_.roles.length > 0 && (
          <div>
            <span className="text-tertiary">Roles: </span>
            <span className="text-fg-muted">{def_.roles.join(", ")}</span>
          </div>
        )}
        {def_.description_template && (
          <div>
            <span className="text-tertiary">Template: </span>
            <span className="text-tertiary">{def_.description_template.slice(0, 60)}...</span>
          </div>
        )}
      </div>

      {/* Outcome summary */}
      {outcomes.length > 0 && (
        <div className="flex gap-3 text-2xs">
          <span className="text-success">{usefulCount} useful</span>
          <span className="text-tertiary">{emptyCount} empty</span>
          <span className="text-error">{failedCount} failed</span>
        </div>
      )}

      {/* Recent run history */}
      {outcomes.length > 0 && (
        <div className="space-y-0.5">
          <div className="text-3xs uppercase tracking-wider text-tertiary">
            Recent runs
          </div>
          {outcomes.slice(-8).reverse().map((o) => (
            <div key={o.run_id} className="flex gap-2 text-2xs">
              <span
                className={
                  o.outcome === "useful"
                    ? "text-success"
                    : o.outcome === "failed"
                      ? "text-error"
                      : "text-tertiary"
                }
              >
                {o.outcome === "useful" ? "●" : o.outcome === "failed" ? "✗" : "○"}
              </span>
              <span className="flex-1 truncate text-fg-muted">
                {o.summary || o.run_id}
              </span>
              {o.completed_at && (
                <span className="text-tertiary">
                  {formatAge(o.completed_at)}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {outcomes.length === 0 && (
        <p className="text-2xs text-tertiary">No run history yet</p>
      )}
    </div>
  );
}

export default TasksPage;
