import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useActivity } from "@/hooks/use-tasks";
import { useOrchestratorState } from "@/hooks/use-orchestrator-state";
import type { ActivityEvent } from "@/types/api";
import { parseApiDate } from "@/lib/format";

/**
 * DecomposerActivityPanel — exposes the decomposer's live activity
 * while the orchestrator is synthesizing execution phases from a
 * resolved council. Today this period is opaque: a single "thinking…"
 * label and an empty activity feed for 1-4 minutes. This panel reads
 * the events the streaming decomposer already writes to
 * `.activity.jsonl` (subtask_id starting with "decomposer:") and
 * renders them in a self-collapsing expander.
 *
 * Layout is laid out with room for future phases of the surfacing
 * roadmap:
 *   - header band (state + completion mirror)              ← P2 final mirror lives here
 *   - chip row (phase emergence)                           ← P2 fills this from plan_created
 *   - chip row (consumed positions + ADR locks)            ← P3 fills these
 *   - chip row (conflicts + risks)                         ← P4
 *   - raw stream (token + tool_use + thinking)             ← P1, this commit
 *
 * Each row only renders when it has content, so today only the raw
 * stream + header are visible — P2-P4 add data without re-mounting.
 */

const TOOL_VERB: Record<string, string> = {
  WebSearch: "searching the web",
  WebFetch: "fetching url",
  Read: "reading",
  Grep: "grepping",
  Glob: "globbing",
  Write: "writing",
  Edit: "editing",
  Bash: "shell",
  artifact_write: "writing artifact",
  log_progress: "logging progress",
  write_memory: "saving memory",
  session_report: "wrapping up",
};

function verbFor(name?: string): string {
  if (!name) return "tool call";
  return TOOL_VERB[name] ?? `calling ${name}`;
}

function fmtTs(ts: string): string {
  // Activity rows ship ISO timestamps. Show seconds-since since they
  // arrive in quick succession during a decompose call.
  const d = parseApiDate(ts);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, { hour12: false });
}

export function DecomposerActivityPanel({ taskId }: { taskId: string }) {
  const { state, label } = useOrchestratorState({ taskId });
  const { data: activity } = useActivity(taskId);
  const [collapsed, setCollapsed] = useState(true);

  const events = useMemo<ActivityEvent[]>(() => {
    const raw = Array.isArray(activity)
      ? activity
      : (activity?.events ?? []);
    return raw.filter(
      (e: ActivityEvent) =>
        typeof e.subtask_id === "string" &&
        e.subtask_id.startsWith("decomposer:"),
    );
  }, [activity]);

  // Visibility rule — panel mounts when:
  //  - the orchestrator is mid-synthesis (state in generating/planning), OR
  //  - the activity feed contains decomposer rows (so the panel stays
  //    visible briefly after completion so the user can read what
  //    happened, including the final result row's totals)
  const stateIsActive = state === "generating" || state === "planning";
  const hasEvents = events.length > 0;
  if (!stateIsActive && !hasEvents) return null;

  // Result rows carry the final accounting. P2 will render this as
  // the closing mirror; here we surface tokens + cost inline.
  const resultRow = [...events]
    .reverse()
    .find((e) => e.type === "result");

  const lastEvent = events[events.length - 1];

  return (
    <div
      className="mx-10 mb-2 mt-3 rounded-lg border-2 border-accent/50 bg-accent-subtle/30"
      data-testid="decomposer-activity-panel"
    >
      <button
        onClick={() => setCollapsed((c) => !c)}
        className="flex w-full items-center gap-3 px-4 py-2.5 text-left"
        aria-expanded={!collapsed}
      >
        {stateIsActive ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
        ) : (
          <span
            className="inline-block h-2.5 w-2.5 shrink-0 rounded-full bg-success"
            aria-hidden="true"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="text-2xs font-semibold uppercase tracking-wider text-accent">
            Decomposer · {stateIsActive ? "synthesizing execution steps" : "complete"}
          </div>
          <div className="mt-0.5 truncate text-xs text-fg-muted">
            {stateIsActive
              ? (label || "Consuming positions and locking decisions…")
              : (resultRow
                  ? `${events.length} events · ${resultRow.output_tokens ?? 0} tokens · $${(resultRow.cost_usd ?? 0).toFixed(3)}`
                  : `${events.length} events`)}
          </div>
        </div>
        <span className="text-3xs text-tertiary">
          {collapsed ? "show stream" : "hide stream"}
        </span>
        {collapsed ? (
          <ChevronRight className="h-4 w-4 text-tertiary" aria-hidden="true" />
        ) : (
          <ChevronDown className="h-4 w-4 text-tertiary" aria-hidden="true" />
        )}
      </button>

      {!collapsed && (
        <div className="border-t border-accent/30 px-4 py-3">
          {events.length === 0 ? (
            <p className="text-xs italic text-tertiary">
              Streaming will appear here as the decomposer reasons.
            </p>
          ) : (
            <ol className="space-y-1.5 max-h-[360px] overflow-y-auto pr-2 [scrollbar-gutter:stable]">
              {events.map((e, i) => (
                <EventRow key={`${e.ts}-${i}`} event={e} />
              ))}
            </ol>
          )}

          {/* P2 sentinel — when the result row lands, show a compact
              "wrote N phases / M subtasks" hint here. The full final
              mirror comes when P2 reads plan_created.phases/subtasks. */}
          {!stateIsActive && lastEvent && (
            <div className="mt-3 rounded border border-success/30 bg-success-subtle/30 px-3 py-2 text-3xs text-fg-muted">
              Decomposition completed at {fmtTs(lastEvent.ts)} — phase plan
              now visible below.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function EventRow({ event }: { event: ActivityEvent }) {
  const ts = fmtTs(event.ts);

  if (event.type === "thinking") {
    return (
      <li className="flex gap-3">
        <span className="w-16 shrink-0 font-mono text-3xs text-tertiary">{ts}</span>
        <span className="shrink-0 text-3xs uppercase tracking-wider text-accent">
          thinking
        </span>
        <span className="flex-1 whitespace-pre-wrap text-3xs italic text-fg-muted">
          {event.preview || ""}
        </span>
      </li>
    );
  }

  if (event.type === "tool_use") {
    return (
      <li className="flex gap-3">
        <span className="w-16 shrink-0 font-mono text-3xs text-tertiary">{ts}</span>
        <span className="shrink-0 text-3xs uppercase tracking-wider text-warning">
          {verbFor(event.name)}
        </span>
        <span className="flex-1 truncate font-mono text-3xs text-fg-muted">
          {event.preview || ""}
        </span>
      </li>
    );
  }

  if (event.type === "text") {
    return (
      <li className="flex gap-3">
        <span className="w-16 shrink-0 font-mono text-3xs text-tertiary">{ts}</span>
        <span className="shrink-0 text-3xs uppercase tracking-wider text-success">
          text
        </span>
        <span className="flex-1 whitespace-pre-wrap text-3xs text-fg">
          {event.text || ""}
        </span>
      </li>
    );
  }

  if (event.type === "result") {
    return (
      <li className="flex gap-3">
        <span className="w-16 shrink-0 font-mono text-3xs text-tertiary">{ts}</span>
        <span className="shrink-0 text-3xs uppercase tracking-wider text-success">
          result
        </span>
        <span className="flex-1 text-3xs text-fg-muted">
          {event.success ? "success" : "failed"} ·
          {" "}
          {event.output_tokens ?? 0} tok ·
          {" "}
          ${(event.cost_usd ?? 0).toFixed(3)} ·
          {" "}
          {((event.duration_ms ?? 0) / 1000).toFixed(1)}s
        </span>
      </li>
    );
  }

  if (event.type === "subtask_start" || event.type === "session_start") {
    return (
      <li className="flex gap-3">
        <span className="w-16 shrink-0 font-mono text-3xs text-tertiary">{ts}</span>
        <span className="shrink-0 text-3xs uppercase tracking-wider text-tertiary">
          {event.type === "subtask_start" ? "started" : "session"}
        </span>
        <span className="flex-1 text-3xs text-fg-muted">
          {event.cmd || ""}
          {event.tools_count !== undefined ? ` · ${event.tools_count} tools` : ""}
        </span>
      </li>
    );
  }

  return null;
}
