import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, deliberationApi, roleApi } from "@/lib/api";
import { usePaneInterval } from "@/lib/pane-active";

/**
 * SIX INTERVALS IN THIS FILE POLLED EVERY 5 SECONDS, AND ONE OF THEM COULD
 * ONLY EVER 404.
 *
 * Measured over a 16 s window on a `done` task:
 *
 *   /api/tasks/{id}/capability-gap  x3  at 70781, 75786, 80791 -> 5005 ms apart
 *   /api/roles                      x3  (103 rows, riding along)
 *   /api/tasks/{id}/activity        x3
 *   /api/tasks/{id}/review          x3
 *
 * The backend answers `404 {"detail":"No capability gap for task …"}` —
 * verified by curl, so 404 IS how it says "none". The page therefore fired a
 * request that can only fail, forever, on every task without a gap, and logged
 * a console error each time: the detail page's error count went from 5 to 15
 * in 16 seconds of sitting still.
 *
 * `components/task/flow-feedback-card.tsx:118` already had the right treatment
 * two directories away — "A 404 means 'unrated' — not an error." — so this was
 * not a missing idea, only a missing application.
 *
 * Three things changed, and the order matters:
 *   1. a 404 returns null instead of throwing (the gap card already renders
 *      nothing for a null gap);
 *   2. once it IS null the interval stops — there is no gap to appear on a
 *      task the engine has finished deliberating, and the accept mutation
 *      invalidates the key when one is created;
 *   3. every interval in this file goes through `usePaneInterval`, because the
 *      shell keeps one leaf per topic mounted and these ran off-screen.
 */

export function useProposedPanel(taskId: string) {
  return useQuery({
    queryKey: ["proposedPanel", taskId],
    queryFn: () => deliberationApi.getProposedPanel(taskId),
    enabled: !!taskId,
  });
}

export function useCapabilityGap(taskId: string) {
  /* Read the pane ONCE into a const. `cond ? false : usePaneInterval(x)` is a
     conditional hook and reorders every hook after it — the p4 DELIVER pane
     adoption hit that out loud. */
  const paneEvery5s = usePaneInterval(5_000);
  return useQuery({
    queryKey: ["capabilityGap", taskId],
    queryFn: async () => {
      try {
        return await deliberationApi.getCapabilityGap(taskId);
      } catch (e) {
        // 404 is the backend saying "no gap", not a failure.
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    enabled: !!taskId,
    // Stop once the answer is "none": a gap that does not exist will not
    // appear by itself, and `useAcceptCapabilityGap` invalidates this key.
    refetchInterval: (q) => (q.state.data === null ? false : paneEvery5s),
  });
}

export function useAcceptCapabilityGap() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      creatorRoles,
    }: {
      taskId: string;
      creatorRoles?: string[];
    }) => deliberationApi.acceptCapabilityGap(taskId, creatorRoles),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["capabilityGap", vars.taskId] });
      qc.invalidateQueries({ queryKey: ["graph", vars.taskId] });
      qc.invalidateQueries({ queryKey: ["taskState", vars.taskId] });
    },
  });
}

/** All roles, polled. Used by the gap card to surface drafts created by
 *  Phase 0 (role-designer persists with maturity='draft'). */
export function useAllRoles() {
  return useQuery({
    queryKey: ["roles", "all"],
    queryFn: () => roleApi.list().then((r) => r.roles),
    refetchInterval: usePaneInterval(5_000),
  });
}

export function usePromoteRole(taskId?: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (roleId: string) => roleApi.promote(roleId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["roles", "all"] });
      if (taskId) {
        qc.invalidateQueries({ queryKey: ["proposedPanel", taskId] });
      }
    },
  });
}

export function useConfirmPanel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      roles,
      strategy,
    }: {
      taskId: string;
      roles: string[];
      strategy?: "parallel" | "sequential" | "debate";
    }) => deliberationApi.confirmPanel(taskId, roles, strategy),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["graph", vars.taskId] });
      qc.invalidateQueries({ queryKey: ["taskState", vars.taskId] });
    },
  });
}

export function usePositions(taskId: string, round?: number) {
  return useQuery({
    queryKey: ["positions", taskId, round],
    queryFn: () => deliberationApi.getPositions(taskId, round),
    enabled: !!taskId,
    refetchInterval: usePaneInterval(5_000),
  });
}

export function useDiscussion(
  taskId: string,
  nodeId: string | null | undefined,
) {
  return useQuery({
    queryKey: ["discussion", taskId, nodeId],
    queryFn: () => deliberationApi.getDiscussion(taskId, nodeId!),
    enabled: !!taskId && !!nodeId,
    refetchInterval: usePaneInterval(5_000),
  });
}

export function useSubmitAssignments() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      nodeId,
      assignments,
    }: {
      taskId: string;
      nodeId: string;
      assignments: Array<{
        role: string;
        action: string;
        leads: string;
        reasoning: string;
      }>;
    }) => deliberationApi.submitAssignments(taskId, nodeId, assignments),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["discussion", vars.taskId] });
    },
  });
}

export function useSubmitStatement() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      nodeId,
      statement,
    }: {
      taskId: string;
      nodeId: string;
      statement: string;
    }) => deliberationApi.submitStatement(taskId, nodeId, statement),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["discussion", vars.taskId] });
    },
  });
}

export function useProceed() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, nodeId }: { taskId: string; nodeId: string }) =>
      deliberationApi.proceed(taskId, nodeId),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["discussion", vars.taskId] });
      qc.invalidateQueries({ queryKey: ["graph", vars.taskId] });
      qc.invalidateQueries({ queryKey: ["taskState", vars.taskId] });
    },
  });
}

export function useRecouncil() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, nodeId }: { taskId: string; nodeId: string }) =>
      deliberationApi.recouncil(taskId, nodeId),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["discussion", vars.taskId] });
      qc.invalidateQueries({ queryKey: ["graph", vars.taskId] });
      qc.invalidateQueries({ queryKey: ["positions", vars.taskId] });
    },
  });
}

export function useAuthorityMap(taskId: string) {
  return useQuery({
    queryKey: ["authorityMap", taskId],
    queryFn: () => deliberationApi.getAuthorityMap(taskId),
    enabled: !!taskId,
    refetchInterval: usePaneInterval(10_000),
  });
}

export function useGraph(taskId: string, enabled = true) {
  return useQuery({
    queryKey: ["graph", taskId],
    queryFn: () => deliberationApi.getGraph(taskId),
    enabled: !!taskId && enabled,
    refetchInterval: usePaneInterval(5_000),
  });
}
