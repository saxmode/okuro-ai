import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  KnowledgeGraphParams,
  KnowledgeGraphResponse,
  KnowledgeNodeType,
  NodeDetail,
  SavedQuery,
  TourBody,
} from "@/types/knowledge";

export interface TourRecord {
  id: string;
  title: string;
  created_at?: string | null;
  project?: string | null;
  body: TourBody;
}

interface ToursResponse {
  tours: TourRecord[];
}

const PREFIX_TO_TYPE: Record<string, KnowledgeNodeType> = {
  mem: "memory",
  thg: "thought",
  art: "artifact",
  prg: "progress",
  kge: "kg_entity",
};

export function nodeTypeFromId(id: string): KnowledgeNodeType | null {
  const prefix = id.split(":", 1)[0];
  if (!prefix) return null;
  return PREFIX_TO_TYPE[prefix] ?? null;
}

function buildQuery(params: KnowledgeGraphParams): string {
  const q = new URLSearchParams();
  if (params.focus) q.set("focus", params.focus);
  if (params.depth != null) q.set("depth", String(params.depth));
  if (params.types?.length) {
    // map UI type → API short name
    const apiShort: Record<KnowledgeNodeType, string> = {
      memory: "memory",
      thought: "thought",
      artifact: "artifact",
      progress: "progress",
      kg_entity: "kg",
    };
    q.set("types", params.types.map((t) => apiShort[t]).join(","));
  }
  if (params.topic) q.set("topic", params.topic);
  if (params.project) q.set("project", params.project);
  if (params.status) q.set("status", params.status);
  if (params.confidence_min != null)
    q.set("confidence_min", String(params.confidence_min));
  if (params.confidence_max != null)
    q.set("confidence_max", String(params.confidence_max));
  if (params.date_from) q.set("date_from", params.date_from);
  if (params.date_to) q.set("date_to", params.date_to);
  if (params.hide_orphans) q.set("hide_orphans", "true");
  if (params.per_type_cap != null)
    q.set("per_type_cap", String(params.per_type_cap));
  return q.toString();
}

export function useKnowledgeGraph(params: KnowledgeGraphParams) {
  const qs = buildQuery(params);
  const path = qs ? `/api/knowledge/graph?${qs}` : "/api/knowledge/graph";
  return useQuery({
    queryKey: ["knowledge", "graph", qs],
    queryFn: () => api<KnowledgeGraphResponse>(path),
    staleTime: 15_000,
  });
}

export function useNodeDetail(nodeId: string | null) {
  return useQuery({
    queryKey: ["knowledge", "node", nodeId],
    queryFn: () => api<NodeDetail>(`/api/knowledge/node/${nodeId}`),
    enabled: !!nodeId,
    staleTime: 30_000,
  });
}

// ── Tours (kind=tour artifacts from the tour-builder role) ───────────

export function useTours(project: string | undefined, enabled = true) {
  const qs = project ? `?project=${encodeURIComponent(project)}` : "";
  return useQuery({
    queryKey: ["knowledge", "tours", project ?? null],
    queryFn: () => api<ToursResponse>(`/api/tours${qs}`),
    enabled,
    staleTime: 60_000,
  });
}

// ── Saved queries ────────────────────────────────────────────────────

export function useSavedQueries() {
  return useQuery({
    queryKey: ["knowledge", "saved-queries"],
    queryFn: () => api<SavedQuery[]>("/api/knowledge/saved-queries"),
    staleTime: 60_000,
  });
}

export function useSaveQueryMutations() {
  const qc = useQueryClient();
  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["knowledge", "saved-queries"] });

  const create = useMutation({
    mutationFn: (payload: {
      name: string;
      filter_dsl: Record<string, unknown>;
      color_group?: string | null;
    }) =>
      api<SavedQuery>("/api/knowledge/saved-queries", {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      }),
    onSuccess: invalidate,
  });

  const remove = useMutation({
    mutationFn: (id: string) =>
      api<void>(`/api/knowledge/saved-queries/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });

  const touch = useMutation({
    mutationFn: (id: string) =>
      api<SavedQuery>(`/api/knowledge/saved-queries/${id}/touch`, {
        method: "POST",
      }),
    onSuccess: invalidate,
  });

  return { create, remove, touch };
}
