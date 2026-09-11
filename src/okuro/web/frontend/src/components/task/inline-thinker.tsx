import { Loader2 } from "lucide-react";
import { useOrchestratorState } from "@/hooks/use-orchestrator-state";
import { cn } from "@/lib/utils";

/**
 * InlineThinker — activity indicator embedded in a task's pipeline view.
 *
 * Positioned by the caller. Convention:
 *   - slot="top"    → before the first phase (new tasks, planning)
 *   - slot="bottom" → after the last phase / before suggestion cards (follow-ups)
 *
 * Filters events by taskId so a background task in another tab doesn't
 * flash this widget on.
 *
 * Visual language mirrors SuggestionNode's dashed-outline "proposed"
 * treatment — this marks *intent* (something is about to happen here),
 * not a fact yet in the plan.
 */

const STATE_LABEL: Record<string, string> = {
  thinking: "Thinking…",
  planning: "Planning…",
  running: "Running…",
  generating: "Generating…",
  reviewing: "Reviewing…",
  // Healthy but paused for a human decision — never "error". Matches the
  // NEEDS_USER verdict-chip pattern (activity-feed.tsx VERDICT_TONE), the
  // one place in the app that already got this right.
  waiting: "Waiting on you",
  complete: "Done",
  error: "Error",
  idle: "",
};

const STATE_TONE: Record<string, string> = {
  thinking: "border-accent/40 text-accent",
  planning: "border-accent/40 text-accent",
  running: "border-accent/40 text-accent",
  generating: "border-accent/40 text-accent",
  reviewing: "border-warning/40 text-warning",
  waiting: "border-info/40 text-info",
  complete: "border-success/40 text-success",
  error: "border-error/40 text-error",
  idle: "border-border text-tertiary",
};

export interface InlineThinkerProps {
  taskId: string;
  slot: "top" | "bottom";
  /**
   * Authoritative terminal-ness from the task's snapshot.lifecycle. When
   * true the chip never shows a busy label — the engine has settled, so a
   * lingering WS busy delta (e.g. a missed `idle` frame) is suppressed.
   * task-detail derives this via isTerminalLifecycle(snapshot.lifecycle.state).
   */
  terminal?: boolean;
}

export function InlineThinker({ taskId, slot, terminal = false }: InlineThinkerProps) {
  const { state, label } = useOrchestratorState({ taskId, terminalLifecycle: terminal });

  if (state === "idle" && !label) return null;

  const spinning = state !== "complete" && state !== "error" && state !== "idle" && state !== "waiting";
  const tone = STATE_TONE[state] ?? STATE_TONE.idle;

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-slot={slot}
      className={cn(
        // min-w-0 lets the truncate inside actually clip on narrow viewports;
        // sm:min-w-[200px] keeps a comfortable minimum on desktop without
        // forcing overflow on phones where 200px > viewport-padding.
        "mx-auto flex w-fit min-w-0 max-w-[calc(100%-1rem)] items-center gap-2 rounded border border-dashed px-3 py-1.5 sm:min-w-[200px]",
        "bg-surface-elevated/60 text-2xs tracking-wide",
        tone,
        slot === "top" ? "mb-2" : "mt-2",
      )}
    >
      {spinning ? (
        <Loader2 className="h-3 w-3 shrink-0 animate-spin" aria-hidden="true" />
      ) : (
        <span
          aria-hidden="true"
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            state === "complete" && "bg-success",
            state === "error" && "bg-error",
            state === "waiting" && "bg-info",
          )}
        />
      )}
      <span className="truncate font-medium">
        {label || STATE_LABEL[state] || ""}
      </span>
    </div>
  );
}
