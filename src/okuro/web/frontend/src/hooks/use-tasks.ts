import { useQuery, useQueryClient } from "@tanstack/react-query";
import { taskApi, systemApi, recurringApi } from "@/lib/api";
import { pickFresherSnapshot } from "@/lib/snapshot-seq";

/** List tasks with optional status filter. */
export function useTasks(params?: { status?: string; limit?: number }) {
  return useQuery({
    queryKey: ["tasks", params],
    queryFn: () => taskApi.list(params),
    refetchInterval: 10_000,
  });
}

/**
 * Full task state (UI-optimized, includes phases/logs/artifacts).
 *
 * `paused` lets the caller silence the polling loop while a live channel
 * (WebSocket) is delivering state_change events — invalidations from the
 * WS handler already trigger a refetch, so the 3 s timer becomes pure
 * churn that re-allocates phases/artifacts identities and cascades
 * memo invalidation through the task-detail tree.
 */
export function useTaskState(
  taskId: string | undefined,
  opts?: { paused?: boolean },
) {
  return useQuery({
    queryKey: ["taskState", taskId],
    queryFn: () => taskApi.getState(taskId!),
    enabled: !!taskId,
    refetchInterval: opts?.paused ? false : 3_000,
  });
}

/**
 * Canonical task snapshot — single SoT for the FE. Step 5/7 of the
 * canonical-state migration. Replaces the constellation of polling hooks
 * (taskState 3s, awaiting 5s, gates 3s, positions 5s, discussion 5s,
 * graph 5s) that the UI composed with up to 5 s cross-slice skew.
 *
 * Refresh path: the WS handler in use-websocket.ts receives `snapshot`
 * push events from the BE and writes them directly into this query's
 * cache via setQueryData — no re-fetch round-trip. Polling at 8 s stays
 * as a fallback for the first paint + reconnect resync.
 */
export function useTaskSnapshot(
  taskId: string | undefined,
  /**
   * ROCK-SOLID v5 P3.7 — `intervalMs` exists because `paused` is not a
   * per-consumer control, however much it reads like one.
   *
   * MEASURED (src/hooks/use-snapshot-copoll.test.tsx): TanStack runs a
   * refetch timer per OBSERVER on a shared query. One paused observer alone
   * → 1 fetch in 500 ms. Add ONE unpaused observer of the same key → 6. So
   * any unpaused consumer keeps this query polling for every consumer, and
   * task-detail's `paused: connected` has never taken effect, because
   * okuro-thinker (shell, always mounted) subscribes unpaused.
   *
   * The two are not interchangeable either: task-detail's `connected` is the
   * per-task /ws/{taskId} socket, which is what delivers snapshot pushes;
   * okuro-thinker's is /ws/activity, which does not. Pausing the thinker on
   * ITS flag would be pausing on the wrong signal.
   */
  opts?: { paused?: boolean; intervalMs?: number },
) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["taskSnapshot", taskId],
    // Seq-guard the poll against a fresher WS push: if the cache already
    // holds a newer snapshot (higher seq) than this poll response, keep the
    // cached one. Without this the 8s poll can land a stale read on top of a
    // just-pushed transition and the UI jumps backwards (Phase 0).
    queryFn: async () => {
      const fresh = await taskApi.getSnapshot(taskId!);
      const prev = queryClient.getQueryData(["taskSnapshot", taskId]) as
        | typeof fresh
        | undefined;
      return pickFresherSnapshot(prev, fresh);
    },
    enabled: !!taskId,
    refetchInterval: opts?.paused ? false : (opts?.intervalMs ?? 8_000),
  });
}

/** Task detail (phases + subtasks, no logs). */
export function useTaskDetail(taskId: string | undefined) {
  return useQuery({
    queryKey: ["task", taskId],
    queryFn: () => taskApi.get(taskId!),
    enabled: !!taskId,
  });
}

/** Agent activity events for a task. */
export function useActivity(
  taskId: string | undefined,
  opts?: { paused?: boolean },
) {
  return useQuery({
    queryKey: ["activity", taskId],
    queryFn: () => taskApi.getActivity(taskId!),
    enabled: !!taskId,
    refetchInterval: opts?.paused ? false : 5_000,
  });
}

/** System status (task count, uptime, etc.). */
export function useSystemStatus() {
  return useQuery({
    queryKey: ["systemStatus"],
    queryFn: () => systemApi.status(),
    refetchInterval: 10_000,
  });
}

/** Recurring task definitions. */
export function useRecurringDefs() {
  return useQuery({
    queryKey: ["recurring"],
    queryFn: () => recurringApi.list(),
    staleTime: 30_000,
  });
}
