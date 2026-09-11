import { useState, useEffect } from "react";
import { formatDuration, elapsedSince } from "@/lib/format";
import type { SubtaskSummary, SubtaskStatus } from "@/types/api";
import { ReviewTimeline } from "@/components/task/review-timeline";
import { SUBTASK_STATUS_CONFIG } from "@/types/api";

interface AgentPanelProps {
  subtask: SubtaskSummary | undefined;
  /** P3.9 — needed to fetch this subtask's review rounds. */
  taskId?: string;
}

const TIMEOUT_SECONDS = 1200; // 20min default subtask timeout

export function AgentPanel({ subtask, taskId }: AgentPanelProps) {
  if (!subtask) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-tertiary">
        Select a part to view details
      </div>
    );
  }

  const retries = subtask.retries ?? 0;

  return (
    <div className="space-y-3 p-3">
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-2xs uppercase tracking-wider text-tertiary">
          Agent
        </span>
        <div className="flex items-center gap-2">
          {retries > 0 && (
            <span
              className="rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-3xs font-bold uppercase tracking-wider text-warning"
              title={`Succeeded after ${retries} retr${retries === 1 ? "y" : "ies"}`}
            >
              {retries}× retry
            </span>
          )}
          <StatusChip status={subtask.status} />
        </div>
      </div>

      {/* Fields */}
      <div className="space-y-2 text-sm">
        <Field label="ID" value={subtask.id} />
        <Field label="Role" value={subtask.role} />
        <Field
          label="Description"
          value={subtask.description}
          wrap
        />
        <Field label="Risk" value={subtask.risk} />
        <Field label="Complexity" value={subtask.complexity} />
        {subtask.model_used && (
          <Field label="Model" value={subtask.model_used} />
        )}
        {subtask.cli_used && (
          <Field label="CLI" value={subtask.cli_used} />
        )}
        {subtask.duration > 0 && (
          <Field label="Duration" value={formatDuration(subtask.duration)} />
        )}
      </div>

      {/* Running timer + timeout bar */}
      {subtask.status === "running" && subtask.started_at && (
        <RunningTimer startedAt={subtask.started_at} />
      )}

      {/* P3.9 — what the reviewer actually did, round by round. Placed
          after the run facts and before the error, because a FAIL here is
          usually the explanation for the error below it. Self-hides when the
          subtask was never reviewed. */}
      <ReviewTimeline taskId={taskId} subtaskId={subtask.id} />

      {/* Error display */}
      {subtask.error && (
        <div className="rounded border border-error/30 bg-error/5 p-2 text-xs text-error">
          {subtask.error}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  wrap,
}: {
  label: string;
  value: string;
  wrap?: boolean;
}) {
  return (
    <div className="flex gap-2">
      <span className="w-20 shrink-0 text-2xs uppercase tracking-wider text-tertiary">
        {label}
      </span>
      <span
        className={`text-xs text-fg-muted ${wrap ? "break-words" : "truncate"}`}
      >
        {value}
      </span>
    </div>
  );
}

function StatusChip({ status }: { status: SubtaskStatus | string }) {
  // C10 — SUBTASK_STATUS_CONFIG carries icon glyphs only. Color comes
  // from `subtask.color_class` on the snapshot, which this chip's caller
  // surfaces independently. Until every caller threads color_class
  // through, the chip uses the neutral tertiary tone — drift-safe
  // because the closed 5-token enum no longer lives here.
  const cfg = SUBTASK_STATUS_CONFIG[status as SubtaskStatus] ?? { icon: "?" };
  return (
    <span className="text-3xs font-bold uppercase tracking-wider text-tertiary">
      {cfg.icon} {status}
    </span>
  );
}

function RunningTimer({ startedAt }: { startedAt: string }) {
  const [elapsed, setElapsed] = useState(() => elapsedSince(startedAt));

  useEffect(() => {
    const interval = setInterval(() => {
      setElapsed(elapsedSince(startedAt));
    }, 1000);
    return () => clearInterval(interval);
  }, [startedAt]);

  const pct = Math.min(100, (elapsed / TIMEOUT_SECONDS) * 100);
  const color =
    pct < 50 ? "bg-success" : pct < 80 ? "bg-warning" : "bg-error";

  return (
    <div>
      <div className="flex justify-between text-2xs text-tertiary">
        <span>{formatDuration(elapsed)}</span>
        <span>{formatDuration(TIMEOUT_SECONDS)}</span>
      </div>
      <div className="mt-1 h-1 rounded-full bg-border">
        <div
          className={`h-full rounded-full transition-[width] duration-1000 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
