import { memo, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { formatTime, humanizeSlug } from "@/lib/format";
import type { LogEntry } from "@/types/api";

interface LogStreamProps {
  logs: LogEntry[];
  wsConnected: boolean;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
}

const LOG_COLORS: Record<string, string> = {
  task_created: "text-info",
  plan_created: "text-info",
  subtask_running: "text-warning",
  subtask_done: "text-success",
  subtask_failed: "text-error",
  subtask_pending: "text-tertiary",
  status_changed: "text-fg-muted",
  subtask_waiting_approval: "text-warning",
  approval_approved: "text-success",
  approval_skipped: "text-tertiary",
  // ROCK-SOLID v5 P1.4 — silent states get calm surfaces. These were
  // allowlisted (reach the client) but rendered as a bare grey machine
  // string — evidence inventory Part D, "silent states".
  task_halted_reconciled: "text-info",
  subtask_review_unresolved: "text-warning",
  phase_retry_blocked_review: "text-info",
  phase_override_blocked_review: "text-success",
  awaiting_user_cleared: "text-success",
  engine_resumed: "text-info",
  engine_parked: "text-info",
  autopilot_decision: "text-info",
  // P2.2 — informational, not a warning. A recurring task continuing
  // without a person is the DESIGNED behaviour; colouring it amber
  // would re-teach the red-pill lesson the hard way.
  gate_auto_resolved: "text-info",
  // P3.1 — both are INFO. continuation_suggested is good news; the
  // sentinel row describes a recovery that already succeeded.
  continuation_suggested: "text-info",
  review_sentinel_lost: "text-info",
};

// Calm, human labels for event types whose raw machine name would otherwise
// render verbatim (see the fallback below). Registering a type here is the
// fix — the raw string itself was never wrong, it was just never translated.
const LOG_LABELS: Record<string, string> = {
  task_halted_reconciled: "recovered automatically",
  subtask_review_unresolved: "quality check needs your call",
  phase_retry_blocked_review: "you chose retry",
  phase_override_blocked_review: "you chose override — continuing",
  awaiting_user_cleared: "you answered — continuing",
  engine_resumed: "engine resumed",
  // Named verbatim in the plan: the user should read a hiccup that was
  // handled, not a machine string implying something was lost.
  review_sentinel_lost: "review transport hiccup — recovered automatically",
  // Plan's exact wording (P1.4 fix shape): "Paused — waiting for you".
  engine_parked: "paused — waiting for you",

  // ROCK-SOLID v5 P3.10 — the review/gate cluster. Measured before writing
  // these: 76 of 87 allowlisted event types reached the feed as their raw
  // machine name, 21 of them review- or gate-shaped. humanizeSlug (the new
  // fallback below) makes any type READABLE; these are the ones that also
  // need to be MEANINGFUL, because "Phase Blocked By Review Cap" is words
  // without being an explanation.
  review_started: "quality check started",
  critic_finding: "quality check flagged something",
  scorer_decision: "quality check scored the work",
  verdict_published: "quality check finished",
  review_complete_event: "quality check finished",
  phase_blocked_by_review_cap: "quality check ran out of retries — needs your call",
  phase_blocked_review: "waiting on your decision",
  phase_decide_blocked_review: "you decided — continuing",
  blocked_review_decided: "you decided — continuing",
  awaiting_user: "waiting on you",
  awaiting_refreshed: "wording re-checked",
  node_awaiting_user: "waiting on you",
  await_user_decision: "waiting on your decision",
  await_user_decision_resolved: "you decided — continuing",
  await_user_decision_skipped: "decision skipped — continuing",
  subtask_waiting_approval: "waiting for your go-ahead",
  approval_approved: "you approved it — running",
  approval_rejected: "you rejected it",
  approval_skipped: "you skipped it — continuing",
  approval_batch: "you answered several at once",
  subtasks_blocked_upstream: "held until the step above finishes",
};

/**
 * Row label. Most types are a static calm string (LOG_LABELS); a few carry
 * data the label should quote. autopilot_decision is the plan's own example
 * verbatim: "Autopilot chose X for you" (P1.4).
 */
function labelFor(entry: LogEntry): string {
  if (entry.type === "autopilot_decision") {
    const chosen = entry.data.chosen_option as string | undefined;
    return chosen
      ? `autopilot chose "${humanizeSlug(chosen)}" for you`
      : "autopilot answered this gate for you";
  }
  // P2.2 — worded to say WHY nobody was asked. "autopilot chose" would
  // be misleading here: this is not the resolver's judgement, it is the
  // policy that a scheduled run does not stop on an absent human.
  if (entry.type === "continuation_suggested") {
    const n = entry.data.count as number | undefined;
    return n ? `${n} next step${n === 1 ? "" : "s"} ready` : "next steps ready";
  }
  if (entry.type === "gate_auto_resolved") {
    const chosen = entry.data.chosen_option as string | undefined;
    return chosen
      ? `recurring task — continued with "${humanizeSlug(chosen)}" instead of waiting`
      : "recurring task — continued instead of waiting";
  }
  const authored = LOG_LABELS[entry.type];
  if (authored) return authored;
  // P3.10 — NEVER the raw machine name as the headline. A per-type table is
  // a lagging mechanism (it covered 11 of 87 types after two sessions of
  // adding entries one at a time), so the FALLBACK carries the guarantee:
  // an unregistered type still reads as words. The raw name is not lost —
  // it moves to the technical-detail position on the row.
  return humanizeSlug(entry.type).toLowerCase();
}

/** The machine name, demoted. Rendered de-emphasised at the end of the row
 *  so an engineer can still identify the event without it being the thing a
 *  person reads first. Omitted when the label IS the type (nothing to add). */
function technicalType(entry: LogEntry): string | null {
  const authored = LOG_LABELS[entry.type];
  return authored || entry.type in SPECIAL_LABELS ? entry.type : null;
}

/** Types whose label is computed in labelFor rather than tabled. Kept as a
 *  set so technicalType and the coverage guard agree with labelFor. */
const SPECIAL_LABELS: Record<string, true> = {
  autopilot_decision: true,
  continuation_suggested: true,
  gate_auto_resolved: true,
};

const FILTER_OPTIONS = ["ALL", "RUNNING", "DONE", "FAILED"] as const;
type LogFilter = (typeof FILTER_OPTIONS)[number];

const FILTER_TYPES: Record<LogFilter, string[] | null> = {
  ALL: null,
  RUNNING: ["subtask_running"],
  DONE: ["subtask_done"],
  FAILED: ["subtask_failed"],
};

export const LogStream = memo(function LogStream({
  logs,
  wsConnected,
  collapsed = false,
  onToggleCollapsed,
}: LogStreamProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [filter, setFilter] = useState<LogFilter>("ALL");

  const filtered = FILTER_TYPES[filter]
    ? logs.filter((l) => FILTER_TYPES[filter]!.includes(l.type))
    : logs;

  // Auto-scroll on new entries
  useEffect(() => {
    if (collapsed) return;
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [filtered.length, autoScroll, collapsed]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    setAutoScroll(scrollHeight - scrollTop - clientHeight < 40);
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      {collapsed ? (
        <button
          type="button"
          onClick={onToggleCollapsed}
          disabled={!onToggleCollapsed}
          aria-expanded={false}
          aria-label="Expand log"
          className="flex h-row-dense w-full items-center justify-between px-10 text-left hover:bg-surface-elevated/40 disabled:cursor-default disabled:hover:bg-transparent"
        >
          <span className="flex items-center gap-2">
            {onToggleCollapsed && (
              <ChevronUp className="h-3 w-3 text-tertiary" />
            )}
            <span className="text-2xs uppercase tracking-wider text-tertiary">
              Log
            </span>
            <span
              className={`h-1.5 w-1.5 rounded-full ${wsConnected ? "bg-success" : "bg-tertiary"}`}
            />
            {filtered.length > 0 && (
              <span className="text-3xs text-tertiary">{filtered.length}</span>
            )}
          </span>
        </button>
      ) : (
        <div
          role={onToggleCollapsed ? "button" : undefined}
          tabIndex={onToggleCollapsed ? 0 : undefined}
          aria-expanded={onToggleCollapsed ? true : undefined}
          aria-label={onToggleCollapsed ? "Collapse log" : undefined}
          onClick={onToggleCollapsed}
          onKeyDown={(e) => {
            if (!onToggleCollapsed) return;
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onToggleCollapsed();
            }
          }}
          className={`flex h-row-dense items-center justify-between px-10 ${
            onToggleCollapsed ? "cursor-pointer hover:bg-surface-elevated/40" : ""
          }`}
        >
          <div className="flex items-center gap-2">
            {onToggleCollapsed && (
              <ChevronDown className="h-3 w-3 text-tertiary" />
            )}
            <span className="text-2xs uppercase tracking-wider text-tertiary">
              Log
            </span>
            <span
              className={`h-1.5 w-1.5 rounded-full ${wsConnected ? "bg-success" : "bg-tertiary"}`}
            />
          </div>
          <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
            {FILTER_OPTIONS.map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`text-3xs uppercase tracking-wider px-1.5 py-0.5 rounded ${
                  filter === f
                    ? "bg-accent text-inverse"
                    : "text-tertiary hover:text-tertiary"
                }`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Log entries */}
      {!collapsed && (
        <>
          <div className="mx-10 h-px shrink-0 bg-border" aria-hidden="true" />
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto p-2 font-mono text-xs"
        >
          {filtered.length === 0 ? (
            <p className="text-center text-tertiary">No events</p>
          ) : (
            filtered.map((entry, i) => {
              const color = LOG_COLORS[entry.type] ?? "text-tertiary";
              const role = entry.data.role as string | undefined;
              const subtaskId = entry.data.subtask_id as string | undefined;
              return (
                <div key={i} className="flex gap-2 py-0.5">
                  <span className="shrink-0 text-tertiary">
                    {formatTime(entry.timestamp)}
                  </span>
                  <span className={`shrink-0 ${color}`}>
                    {labelFor(entry)}
                  </span>
                  {role && (
                    <span className="text-fg-muted">[{role}]</span>
                  )}
                  {subtaskId && (
                    <span className="text-tertiary">{subtaskId}</span>
                  )}
                  {technicalType(entry) && (
                    <span className="ml-auto shrink-0 font-mono text-3xs text-tertiary/60">
                      {technicalType(entry)}
                    </span>
                  )}
                </div>
              );
            })
          )}
        </div>
        </>
      )}
    </div>
  );
});
