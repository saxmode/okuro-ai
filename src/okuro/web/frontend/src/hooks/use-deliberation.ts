import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { deliberationApi, roleApi } from "@/lib/api";

export function useProposedPanel(taskId: string) {
  return useQuery({
    queryKey: ["proposedPanel", taskId],
    queryFn: () => deliberationApi.getProposedPanel(taskId),
    enabled: !!taskId,
  });
}

export function useCapabilityGap(taskId: string) {
  return useQuery({
    queryKey: ["capabilityGap", taskId],
    queryFn: () => deliberationApi.getCapabilityGap(taskId),
    enabled: !!taskId,
    refetchInterval: 5_000,
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
    refetchInterval: 5_000,
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
    refetchInterval: 5_000,
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
    refetchInterval: 5_000,
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
    refetchInterval: 10_000,
  });
}

export function useGraph(taskId: string, enabled = true) {
  return useQuery({
    queryKey: ["graph", taskId],
    queryFn: () => deliberationApi.getGraph(taskId),
    enabled: !!taskId && enabled,
    refetchInterval: 5_000,
  });
}
