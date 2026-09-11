/**
 * LivenessBanner — engine liveness as a first-class rendered fact (Phase 1).
 *
 * Before this, a wedged / dead / respawning engine rendered IDENTICALLY to a
 * working one: the pipeline kept showing "running" with no indication the
 * process behind it had stopped reporting. snapshot.engine_state (derived
 * from the engine lease at read time) makes liveness visible:
 *
 *   - live / idle  → no banner (normal; engine present or not expected).
 *   - recovering   → engine gone but within the reconciler respawn window;
 *                    transient, auto-heals. Amber, no action.
 *   - wedged       → should be running but isn't past the grace window.
 *                    Red. The daemon keeps trying to resume. The banner
 *                    points ONLY to actions that exist in THIS state: the
 *                    engine is in an active (non-terminal) lifecycle here,
 *                    so the ContinueBar (Retry/Resume) is NOT mounted — it
 *                    appears only once the task is terminal. The real lever
 *                    is Stop (header, visible while working); after Stop the
 *                    task halts and Retry/Resume become available. Pointing
 *                    at "Resume / Retry below" was a dead-end (they don't
 *                    exist in a non-terminal state).
 */
import { AlertTriangle, Loader2 } from "lucide-react";
import type { TaskSnapshot } from "@/types/api";

export function LivenessBanner({
  engineState,
}: {
  /** snapshot.engine_state */
  engineState: TaskSnapshot["engine_state"];
}) {
  if (engineState !== "recovering" && engineState !== "wedged") return null;

  if (engineState === "recovering") {
    return (
      <div
        data-testid="liveness-banner"
        data-engine-state="recovering"
        className="flex items-start gap-3 border-b-2 border-warning/50 bg-warning-subtle/40 px-10 py-3"
      >
        <Loader2
          className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-warning"
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="text-2xs font-bold uppercase tracking-wider text-warning">
              Engine recovering
            </span>
            <span className="text-2xs text-fg-muted">
              The engine stopped reporting and is being resumed automatically.
              Live updates resume in a moment — no action needed.
            </span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      data-testid="liveness-banner"
      data-engine-state="wedged"
      className="flex items-start gap-3 border-b-2 border-error/50 bg-error-subtle/40 px-10 py-3"
    >
      <AlertTriangle
        className="mt-0.5 h-4 w-4 shrink-0 text-error"
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="text-2xs font-bold uppercase tracking-wider text-error">
            Engine not responding
          </span>
          <span className="text-2xs text-fg-muted">
            This task shows as running, but the engine has stopped responding
            and automatic recovery hasn't caught up. If it stays stuck, use
            Stop (top-right) to halt it — Retry and Resume become available
            once it's stopped.
          </span>
        </div>
      </div>
    </div>
  );
}

export default LivenessBanner;
