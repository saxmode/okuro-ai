import { useQuery } from "@tanstack/react-query";
import { taskApi } from "@/lib/api";

/**
 * Durable review history from the task_events store (GET /api/tasks/{id}/review).
 *
 * This is the single source of truth for review OUTCOMES — verdict, load-bearing
 * findings, scorer rubric — independent of the best-effort `.activity.jsonl`
 * stream, which can drop rows if the reviewer's emit helpers resolve the wrong
 * tasks_dir. The activity feed renders these as the authoritative findings,
 * falling back to streamed rows only when the durable record is not yet present.
 */
export function useTaskReview(taskId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ["review", taskId],
    queryFn: () => taskApi.getReview(taskId!),
    enabled: enabled && !!taskId,
    refetchInterval: 5_000,
  });
}
