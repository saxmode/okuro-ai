/**
 * FailureBanner — top-of-detail surface for terminal FAILURE states
 * (lifecycle `failed` / `halted`).
 *
 * Why this exists: a failed/halted task carries NO `snapshot.blocker`
 * (state_reader only sets blocker for awaiting / blocked_review), so the
 * BlockerCard never fires — the only failure signal was the red header
 * label + a per-subtask error string that pipeline-view used to render
 * ONLY when `retries > 0`. The common halt-on-exit case sets retries=0,
 * so the reason rendered nowhere. This banner pins the reason to the top
 * of the page where the user looks first, and explains what happened.
 *
 * It does NOT own the recovery actions — those live in ContinueBar
 * (Retry / Resume / Continue) below the pipeline. The banner points at
 * them so there's one action surface, not two.
 */
import { useState } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";

export interface SubtaskFailure {
  id: string;
  role: string;
  error: string;
}

// The generic string the engine's atexit reconciler stamps onto subtasks
// that were still running when the process died (engine.py
// `_reconcile_on_exit`). When every failure is this string the cause is
// "engine interrupted", not "a subtask errored" — we explain accordingly.
const HALT_ON_EXIT_ERROR = "task halted before subtask completed";

function isHaltOnExit(failures: SubtaskFailure[]): boolean {
  return (
    failures.length > 0 &&
    failures.every((f) => f.error.trim() === HALT_ON_EXIT_ERROR)
  );
}

export function FailureBanner({
  lifecycleState,
  label,
  failures,
  haltReason,
  onRetry,
}: {
  /** snapshot.lifecycle.state — "failed" | "halted". */
  lifecycleState: string;
  /** snapshot.lifecycle.label — e.g. "Halted" / "Failed". */
  label: string;
  /** Failed subtasks carrying an error string, de-duplicated by caller. */
  failures: SubtaskFailure[];
  /** snapshot.lifecycle.halt_reason — why this task was halted, in the
   *  halter's own words. Rendered in place of the generic halt explanation,
   *  because a specific reason always beats a plausible one. */
  haltReason?: string;
  /** Re-run the failed work. Surfaced as a Retry button in the banner so
   *  the only recovery action is reachable without scrolling to the
   *  ContinueBar. */
  onRetry?: () => void | Promise<void>;
}) {
  const isHalted = lifecycleState === "halted";
  const [retrying, setRetrying] = useState(false);
  if (lifecycleState !== "failed" && lifecycleState !== "halted") return null;

  async function handleRetry() {
    if (!onRetry || retrying) return;
    setRetrying(true);
    try {
      await onRetry();
    } finally {
      setRetrying(false);
    }
  }

  // Plain-language "what happened" — the part the user actually asked for.
  // Halt-on-exit is the interrupted-engine case; a real subtask error is
  // a different story. Both end with where the recovery actions live.
  const haltOnExit = isHaltOnExit(failures);

  // An explicit reason from whoever halted the task beats every generic
  // sentence below it. This is the whole point of surfacing halt_reason: the
  // 2026-08-03 sweep wrote a specific explanation into 43 tasks and this
  // banner went on guessing "the engine was probably interrupted" over the
  // top of it, because nothing read the field.
  const statedReason = (haltReason || "").trim();

  const explanation = statedReason
    ? "The orchestrator handed control back. The stated reason is below. Your completed work is saved."
    : haltOnExit
      ? "The orchestrator stopped before it finished — the engine process was interrupted (a crash, a restart, or a manual stop). Any part that was mid-run got marked failed because it had nowhere to land. Your completed work is saved."
      : isHalted
        ? "The orchestrator handed control back before completing the plan. The reason is below. Your completed work is saved."
        : "A part failed and the orchestrator could not continue. The reason is below.";

  const recovery = isHalted
    ? "Use Resume below to pick up where it left off, or Retry to re-run the failed step."
    : "Use Retry below to re-run, or Continue with a new instruction.";

  // ROCK-SOLID v5 P1.3 — halted is a user-initiated Stop, not a failure
  // (state_reader.py's _LIFECYCLE_COLOR already says "warning" for it; this
  // banner painted it identically to a real "failed" regardless — direct
  // BE/FE contradiction, evidence inventory mismatch #15). A user pressing
  // Stop and seeing a full-width red alert reads as "I broke something."
  const tone = isHalted
    ? {
        container: "border-warning/50 bg-warning-subtle/40",
        icon: "text-warning",
        label: "text-warning",
        itemBorder: "border-warning/30",
      }
    : {
        container: "border-error/50 bg-error-subtle/40",
        icon: "text-error",
        label: "text-error",
        itemBorder: "border-error/30",
      };

  return (
    <div
      data-testid="failure-banner"
      className={`flex items-start gap-3 border-b-2 px-10 py-3 ${tone.container}`}
    >
      <AlertTriangle
        className={`mt-0.5 h-4 w-4 shrink-0 ${tone.icon}`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex items-baseline gap-2">
          <span className={`text-2xs font-bold uppercase tracking-wider ${tone.label}`}>
            {label || (isHalted ? "Halted" : "Failed")}
          </span>
          <span className="text-2xs text-fg-muted">{explanation}</span>
        </div>

        {/* The stated halt reason, verbatim. Its own block rather than the
            header line because it is a sentence or three, not a label. */}
        {statedReason && (
          <p
            data-testid="halt-reason"
            className={`rounded border bg-surface/60 px-2 py-1.5 text-2xs whitespace-pre-wrap break-words leading-snug text-fg ${tone.itemBorder}`}
          >
            {statedReason}
          </p>
        )}

        {/* Reason(s) — the inline detail behind the message. For the
            generic halt-on-exit string we skip the per-subtask list (the
            explanation already covers it); a real error gets surfaced
            verbatim so the user sees exactly what broke. */}
        {!haltOnExit && failures.length > 0 && (
          <ul className="space-y-1">
            {failures.map((f) => (
              <li
                key={f.id}
                className={`rounded border bg-surface/60 px-2 py-1.5 text-2xs text-fg-muted ${tone.itemBorder}`}
              >
                <span className="font-mono text-3xs text-tertiary">
                  {f.role || f.id}
                </span>
                <p className="mt-0.5 whitespace-pre-wrap break-words leading-snug text-fg">
                  {f.error}
                </p>
              </li>
            ))}
          </ul>
        )}

        <div className="flex items-center gap-3">
          {onRetry && (
            <Button
              size="sm"
              variant="outline"
              onClick={handleRetry}
              disabled={retrying}
              className="text-2xs"
            >
              <RotateCcw className="mr-1 h-3 w-3" aria-hidden="true" />
              {retrying ? "Retrying…" : "Retry"}
            </Button>
          )}
          <p className="text-3xs text-tertiary">{recovery}</p>
        </div>
      </div>
    </div>
  );
}

export default FailureBanner;
