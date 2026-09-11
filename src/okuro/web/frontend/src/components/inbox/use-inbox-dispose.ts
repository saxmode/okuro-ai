import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "@/components/ui/toast";
import { inboxApi, type InboxAction } from "@/lib/inbox-api";

/**
 * Shared inbox disposition mutation — used by both the /inbox page and
 * the NOW "Needs you" strip so they invalidate the same ["inbox"] query
 * key and emit identical toast verbs.
 *
 * Extracted from pages/inbox.tsx (Phase 4).
 */
export function useInboxDispose() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action }: { id: string; action: InboxAction }) =>
      inboxApi.dispose(id, action),
    onSuccess: (_row, { action }) => {
      const verb =
        action === "act" ? "Dispatched" : action === "defer" ? "Deferred" : "Dismissed";
      toast.success(verb);
      queryClient.invalidateQueries({ queryKey: ["inbox"] });
    },
    onError: (err: Error) => toast.error(err.message || "Disposition failed"),
    meta: { okuroLabel: "Dispatching…" },
  });
}
