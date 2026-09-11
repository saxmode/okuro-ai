import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

/** One published review round for one subtask. */
export interface ReviewRound {
  attempt: number;
  verdict: string;
  findings_count: number;
  open_findings: number;
  resolved_findings: number;
  /** Why the round ended when the verdict alone does not say it
   *  ("converged", "phase gate", budget exhaustion). Empty for an ordinary
   *  round. */
  reason: string;
  artifact_id: string;
  ts: string;
  /** queue_wait_s / deterministic_s / critic_s / scorer_s. Absent on rounds
   *  published before the schema declared the field — see P3.9. */
  stage_timings: Record<string, number>;
}

export interface ReviewRounds {
  task_id: string;
  /** The round budget — M in "round N of M". Sourced backend-side from the
   *  same constant the tile badge reads, so the two cannot disagree. */
  max_attempts: number;
  subtasks: Record<string, ReviewRound[]>;
}

/**
 * Per-subtask review round history (ROCK-SOLID v5 P3.9).
 *
 * Distinct from `useTaskReview` (["review"]), which serves durable `verdict`
 * events — those are PHASE-scoped and filed under a synthetic
 * `reviewer-phase-N` id, so they cannot key a per-subtask timeline. This
 * reads `convergence_telemetry`, the only store keyed by (subtask, attempt).
 *
 * No refetchInterval: the WS state_change handler invalidates
 * ["reviewRounds"] like every other task-scoped key, so a live review updates
 * by push. Polling on top would be the redundancy P3.7 measured.
 */
export function useReviewRounds(taskId: string | undefined) {
  return useQuery({
    queryKey: ["reviewRounds", taskId],
    queryFn: () => api<ReviewRounds>(`/api/tasks/${taskId}/review-rounds`),
    enabled: !!taskId,
  });
}
