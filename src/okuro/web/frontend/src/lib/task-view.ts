import type { TaskSnapshot, TaskState } from "@/types/api";
import { isTerminalLifecycle } from "@/lib/lifecycle";

/**
 * Phase 2 — single source of truth for task-detail surfaces.
 *
 * Before this, the header read snapshot.lifecycle while the
 * continue-bar / footer / progress bar read state.status from a SEPARATE
 * query (useTaskState). When the two queries lagged each other the surfaces
 * disagreed — header "Done" while the continue-bar stayed hidden, or a
 * progress bar stuck at the old percent. This derives EVERY duplicated fact
 * from the snapshot (the canonical surface), falling back to useTaskState
 * ONLY for the pre-snapshot first paint. One derivation → surfaces can never
 * contradict; a terminal state flips atomically everywhere.
 *
 * Facts NOT duplicated in the snapshot (recent_logs, the typed PhaseSummary
 * roster the Trace tab needs) still come from useTaskState — those aren't
 * contradiction sources because no other surface renders them.
 */
export interface TaskView {
  lifecycleState: string;
  intelligence: string;
  currentPhase: number;
  progressPercent: number;
  description: string;
  mode: string;
  totalPhases: number;
  totalDuration: number;
  /** Engine has settled (done/failed/blocked/halted/waiting_user). */
  isTerminal: boolean;
  /** Engine actively working → expose Stop. */
  isWorking: boolean;
  isFailed: boolean;
}

type DurationPhase = { subtasks: Array<{ duration: number }> };

export function deriveTaskView(
  snapshot: TaskSnapshot | undefined,
  state: TaskState,
): TaskView {
  const lifecycleState = snapshot?.lifecycle.state ?? state.status;
  const durationPhases: DurationPhase[] =
    (snapshot?.phases as unknown as DurationPhase[] | undefined) ?? state.phases;
  const totalDuration = durationPhases
    .flatMap((p) => p.subtasks)
    .reduce((sum, s) => sum + (s.duration || 0), 0);
  const isTerminal = isTerminalLifecycle(lifecycleState);

  return {
    lifecycleState,
    intelligence: snapshot?.intelligence ?? state.intelligence ?? "",
    currentPhase: snapshot?.current_phase ?? state.current_phase,
    progressPercent: snapshot?.progress_percent ?? state.progress_percent,
    description: snapshot?.description ?? state.description,
    mode: snapshot?.mode ?? state.mode ?? "",
    totalPhases: snapshot?.phases_total ?? state.phases.length,
    totalDuration,
    isTerminal,
    isWorking: !isTerminal && lifecycleState !== "pending",
    isFailed: lifecycleState === "failed",
  };
}
