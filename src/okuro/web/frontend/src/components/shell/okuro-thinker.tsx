import { useEffect, useState } from "react";
import { Link } from "react-router";
import { Loader2, X } from "lucide-react";
import { useOrchestratorState } from "@/hooks/use-orchestrator-state";
import { useTaskSnapshot } from "@/hooks/use-tasks";
import { isTerminalLifecycle } from "@/lib/lifecycle";
import { cn } from "@/lib/utils";

/**
 * OkuroThinker — sidebar status indicator.
 *
 * Renders inline in the sidebar (between PulseCanvas and the collapsible
 * panels) on every authenticated route. No background, no shadow — reads
 * as a quiet status line, not a floating callout.
 *
 * Answers "is anything happening right now?" in <300 ms.
 *   - Click a button that fires a React Query mutation → instant "Working…"
 *     (augmented with the mutation's `meta.okuroLabel` if set).
 *   - Orchestrator starts planning / running / generating → widget reflects
 *     the backend-reported state via /ws/activity.
 *
 * Closable: × dismisses the current label. The next state/label change
 * re-shows it, so dismissing "Running 2.2…" mutes it until subtask 2.3
 * (or any other distinct label) takes over.
 *
 * Hidden (returns null) when state is idle, when there's nothing to say,
 * or when the user has dismissed the current label.
 */

const STATE_TONE: Record<string, string> = {
  thinking: "text-accent",
  planning: "text-accent",
  running: "text-accent",
  generating: "text-accent",
  reviewing: "text-warning",
  waiting: "text-info",
  complete: "text-success",
  error: "text-error",
  idle: "text-tertiary",
};

const STATE_LABEL: Record<string, string> = {
  thinking: "Thinking…",
  planning: "Planning…",
  running: "Running…",
  generating: "Generating…",
  reviewing: "Reviewing…",
  // Healthy but paused for a human decision — never "error". Mirrors
  // InlineThinker (task/inline-thinker.tsx).
  waiting: "Waiting on you",
  complete: "Done",
  error: "Error",
  idle: "",
};

export function OkuroThinker() {
  const { state, label, taskId, connected } = useOrchestratorState();

  // Lifecycle authority for the sidebar chip. The chip is scoped to whichever
  // task the orchestrator's WS deltas name (taskId). Read that task's
  // authoritative snapshot (shared TanStack cache — no extra fetch when the
  // task view already mounted it) and suppress any busy state once the
  // lifecycle has settled. Mirrors the InlineThinker fix so a missed `idle`
  // WS frame can't leave the sidebar stuck on "Generating…".
  // P3.7 — this observer is the reason the shared snapshot query never
  // actually pauses (measured; see useTaskSnapshot). It is kept, because it
  // is the ONLY thing refreshing the chip when the task page is not open —
  // and it cannot pause on `connected` above, which tracks /ws/activity while
  // snapshot pushes arrive on /ws/{taskId}.
  //
  // What it does not need is 8 s. The chip's whole job is to not show a stale
  // "Generating…" after a task settles; 30 s is imperceptible for that and
  // cuts the always-on poll it imposes on every consumer by ~4x. When the
  // task page IS open its WS pushes keep the same cache entry fresh anyway.
  const { data: snapshot } = useTaskSnapshot(taskId ?? undefined, {
    intervalMs: 30_000,
  });
  const isTerminal = isTerminalLifecycle(snapshot?.lifecycle.state);

  // A settled task never shows a busy chip. `complete` is the terminal "Done"
  // pulse (allowed through; the hook decays it on its own timer); every other
  // non-idle state is stale once the engine has stopped — collapse to idle.
  const busy = state !== "idle" && state !== "complete" && state !== "error" && state !== "waiting";
  const effectiveState = isTerminal && busy ? "idle" : state;
  const effectiveLabel = isTerminal && busy ? "" : label;

  const display = effectiveLabel || STATE_LABEL[effectiveState] || "";

  // Dismiss is keyed off the current display string. When the orchestrator
  // moves to a new label the dismissedFor value goes stale and the chip
  // reappears — so the user can mute the current activity without losing
  // signal on the next one.
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  useEffect(() => {
    if (display && display !== dismissedFor) setDismissedFor(null);
  }, [display, dismissedFor]);

  if (effectiveState === "idle" && !effectiveLabel) return null;
  if (!display) return null;
  if (display === dismissedFor) return null;

  const spinning =
    effectiveState !== "complete" &&
    effectiveState !== "error" &&
    effectiveState !== "idle" &&
    effectiveState !== "waiting";
  const tone = STATE_TONE[effectiveState] ?? STATE_TONE.idle;

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        "flex min-w-0 items-center gap-2 px-3 py-1.5 text-2xs",
        tone,
      )}
    >
      {spinning ? (
        <Loader2
          className="h-3 w-3 shrink-0 animate-spin"
          aria-hidden="true"
        />
      ) : (
        <span
          aria-hidden="true"
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            effectiveState === "complete" && "bg-success",
            effectiveState === "error" && "bg-error",
            effectiveState === "idle" && "bg-tertiary",
            effectiveState === "waiting" && "bg-info",
          )}
        />
      )}
      <span
        className="min-w-0 flex-1 truncate font-medium tracking-wide"
        title={display}
      >
        {display}
      </span>
      {taskId && (
        <Link
          to={`/work/${taskId}`}
          className="shrink-0 text-3xs uppercase tracking-wider text-tertiary hover:text-fg-muted"
        >
          open
        </Link>
      )}
      {!connected && effectiveState !== "idle" && (
        <span
          className="shrink-0 text-3xs text-tertiary"
          title="Disconnected from orchestrator"
        >
          ·
        </span>
      )}
      <button
        type="button"
        onClick={() => setDismissedFor(display)}
        aria-label="Dismiss status"
        title="Dismiss"
        className="shrink-0 rounded p-0.5 text-tertiary hover:text-fg-muted"
      >
        <X className="h-3 w-3" aria-hidden="true" />
      </button>
    </div>
  );
}
