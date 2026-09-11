/**
 * Knowledge-graph API types — parallel to the deliberation GraphNode/Edge
 * (which live in api.ts under different semantics). These mirror
 * okuro.orchestrator.api.knowledge_graph Pydantic models 1:1.
 */

export type KnowledgeNodeType =
  | "memory"
  | "thought"
  | "artifact"
  | "progress"
  | "kg_entity";

export type KnowledgeEdgeType =
  | "parent"
  | "supersedes"
  | "memory_ref"
  | "tunnel"
  | "kg_triple"
  | "progress_ref";

export interface KnowledgeNode {
  id: string; // composite: mem:uuid | thg:uuid | art:uuid | prg:uuid | kge:name
  type: KnowledgeNodeType;
  label: string;
  topic?: string | null;
  project?: string | null;
  confidence?: number | null;
  status?: string | null;
  kind?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  is_supersedes_head?: boolean;
  body_preview?: string | null;
  layer?: string | null; // architectural layer for kg_entity code_refs
}

// ── Tour types (kind=tour artifacts emitted by tour-builder role) ────

export interface TourStep {
  index: number;
  file: string;
  layer: string;
  symbols: string[];
  why: string;
  prerequisites: number[];
}

export interface TourBody {
  project: string;
  generated_at: string;
  focus_layer?: string | null;
  max_steps: number;
  steps: TourStep[];
}

export interface KnowledgeEdge {
  id: string;
  source: string;
  target: string;
  type: KnowledgeEdgeType;
  label?: string | null;
  valid_from?: string | null;
  valid_to?: string | null;
}

export interface FacetCount {
  value: string;
  count: number;
}

export interface KnowledgeFacets {
  entity_types: FacetCount[];
  topics: FacetCount[];
  projects: FacetCount[];
  statuses: FacetCount[];
  confidence_bins: FacetCount[];
}

export interface KnowledgeGraphResponse {
  nodes: KnowledgeNode[];
  edges: KnowledgeEdge[];
  facets: KnowledgeFacets;
  generated_at: string;
  truncated: boolean;
  mode: "global" | "local";
  focus?: string | null;
}

// ── Detail-drawer types ──────────────────────────────────────────────

export interface RelationItem {
  node: KnowledgeNode;
  edge_type: KnowledgeEdgeType;
  edge_label?: string | null;
  direction: "outgoing" | "incoming";
}

export interface NodeRelations {
  supersedes: RelationItem[];
  superseded_by: RelationItem[];
  parents: RelationItem[];
  children: RelationItem[];
  memory_refs_out: RelationItem[];
  memory_refs_in: RelationItem[];
  tunnels: RelationItem[];
  kg_triples_out: RelationItem[];
  kg_triples_in: RelationItem[];
  progress_refs_out: RelationItem[];
  progress_refs_in: RelationItem[];
}

export interface NodeDetail {
  node: KnowledgeNode;
  body?: string | null;
  extras: Record<string, unknown>;
  relations: NodeRelations;
  generated_at: string;
}

export interface SavedQuery {
  id: string;
  name: string;
  filter_dsl: Record<string, unknown>;
  color_group?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  last_used_at?: string | null;
}

export interface KnowledgeGraphParams {
  focus?: string;
  depth?: number;
  types?: KnowledgeNodeType[]; // serialized as comma list
  topic?: string;
  project?: string;
  status?: string;
  confidence_min?: number;
  confidence_max?: number;
  date_from?: string;
  date_to?: string;
  hide_orphans?: boolean;
  per_type_cap?: number;
}
