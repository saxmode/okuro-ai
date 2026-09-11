import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { gatesApi } from "@/lib/api";

export function useGates(taskId: string, enabled = true) {
  return useQuery({
    queryKey: ["gates", taskId],
    queryFn: () => gatesApi.list(taskId),
    enabled: enabled && !!taskId,
    refetchInterval: 3_000,
  });
}

export function useResolveGate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      gateId,
      selectedOptionId,
      rationale,
      skipped,
    }: {
      taskId: string;
      gateId: string;
      selectedOptionId?: string;
      rationale?: string;
      skipped?: boolean;
    }) =>
      gatesApi.resolve(taskId, gateId, {
        selected_option_id: selectedOptionId,
        rationale,
        skipped,
      }),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["gates", vars.taskId] });
      qc.invalidateQueries({ queryKey: ["taskState", vars.taskId] });
      // taskSnapshot is the PRIMARY surface for lifecycle / blocker / phase
      // colour (task-detail.tsx), so omitting it here left the card the user
      // just acted on unchanged until a WS push or the 8 s poll caught up.
      qc.invalidateQueries({ queryKey: ["taskSnapshot", vars.taskId] });
    },
  });
}
