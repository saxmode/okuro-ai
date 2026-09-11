import { useMemo, useState, useEffect, useRef } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Panel,
  Handle,
  Position,
  useNodesState,
  type Node,
  type Edge,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Maximize2, Minimize2 } from "lucide-react";
import {
  forceSimulation,
  forceManyBody,
  forceLink,
  forceCenter,
  forceCollide,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from "d3-force";

/**
 * Dedicated file-level code graph for a single repo.
 *
 * Nodes = files, edges = structural links (calls/imports projected to file
 * level). Colored by architectural layer, sized by degree. This is the code
 * intelligence surface — distinct from the memory-centric /knowledge graph.
 */

export interface CodeGraphNode {
  id: string;
  label: string;
  path: string;
  layer: string | null;
  community: string | null;
  degree: number;
}

export interface CodeGraphEdge {
  source: string;
  target: string;
  weight: number;
}

// Layer palette — mirrors the /knowledge LAYER_COLOR so colors read the same
// across both surfaces (CSS-var space → theme-aware).
const LAYER_COLOR: Record<string, string> = {
  ui: "var(--color-accent)",
  api: "var(--color-info)",
  domain: "var(--color-success, #11d425)",
  data: "var(--color-warning)",
  infra: "var(--color-tertiary)",
  test: "var(--color-fg-muted)",
  docs: "var(--color-fg-tertiary)",
  config: "var(--color-fg-tertiary)",
  unknown: "var(--color-border)",
};

function layerColor(layer: string | null): string {
  return (layer && LAYER_COLOR[layer]) || "var(--color-border)";
}

function FileNode({ data, selected }: NodeProps) {
  const n = data as unknown as CodeGraphNode & { selected?: boolean };
  const color = layerColor(n.layer);
  return (
    <div
      className="rounded-sm border bg-surface px-2 py-1 text-fg"
      style={{
        borderLeft: `3px solid ${color}`,
        borderColor: selected ? "var(--color-accent)" : "var(--color-border-subtle)",
        boxShadow: selected ? "0 0 0 1px var(--color-accent)" : undefined,
      }}
      title={n.path}
    >
      <Handle type="target" position={Position.Top} className="!opacity-0" />
      <Handle type="source" position={Position.Bottom} className="!opacity-0" />
      <div className="max-w-[26rem] truncate text-xs font-medium">{n.label}</div>
      <div className="flex items-center gap-1 text-[9px] uppercase tracking-wider opacity-70">
        <span>{n.layer ?? "—"}</span>
        <span>· deg {n.degree}</span>
      </div>
    </div>
  );
}

const nodeTypes = { file: FileNode };

interface SimNode extends SimulationNodeDatum {
  id: string;
}
interface SimLink extends SimulationLinkDatum<SimNode> {
  source: string | SimNode;
  target: string | SimNode;
}

function runForceLayout(
  ids: string[],
  edges: CodeGraphEdge[],
): Map<string, { x: number; y: number }> {
  if (ids.length === 0) return new Map();
  const sim: SimNode[] = ids.map((id) => ({ id }));
  const idSet = new Set(ids);
  const links: SimLink[] = edges
    .filter((e) => idSet.has(e.source) && idSet.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  const s = forceSimulation(sim)
    .force("charge", forceManyBody().strength(-200))
    .force(
      "link",
      forceLink<SimNode, SimLink>(links).id((d) => d.id).distance(90).strength(0.55),
    )
    .force("center", forceCenter(0, 0).strength(0.05))
    .force("collide", forceCollide().radius(55))
    .stop();

  const ticks = Math.min(300, Math.max(120, Math.round(ids.length * 0.6)));
  for (let i = 0; i < ticks; i++) s.tick();

  const out = new Map<string, { x: number; y: number }>();
  for (const n of sim) out.set(n.id, { x: n.x ?? 0, y: n.y ?? 0 });
  return out;
}

export function RepoCodeGraph({
  nodes,
  edges,
  onSelect,
  selectedId,
}: {
  nodes: CodeGraphNode[];
  edges: CodeGraphEdge[];
  onSelect?: (id: string | null) => void;
  selectedId?: string | null;
}) {
  const idSig = useMemo(
    () => nodes.map((n) => n.id).sort().join("|"),
    [nodes],
  );

  const positions = useMemo(() => {
    const ids = idSig ? idSig.split("|") : [];
    return runForceLayout(ids, edges);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idSig]);

  // Layout-derived nodes. selectedId is intentionally NOT a dep — selection is
  // patched below so user-dragged positions survive re-selection; a full
  // rebuild only happens when the data or force layout changes.
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;

  const baseNodes: Node[] = useMemo(
    () =>
      nodes.map((n) => {
        const p = positions.get(n.id) ?? { x: 0, y: 0 };
        return {
          id: n.id,
          type: "file",
          position: p,
          data: n as unknown as Record<string, unknown>,
        };
      }),
    [nodes, positions],
  );

  // Nodes live in RF-managed state so onNodesChange (drag) can mutate them.
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node>([]);

  // Rebuild on data/layout change, preserving current selection.
  useEffect(() => {
    const sel = selectedIdRef.current;
    setRfNodes(baseNodes.map((n) => ({ ...n, selected: n.id === sel })));
  }, [baseNodes, setRfNodes]);

  // Patch only the selection flag — keeps dragged positions intact.
  useEffect(() => {
    setRfNodes((nds) =>
      nds.map((n) => {
        const want = n.id === selectedId;
        return n.selected === want ? n : { ...n, selected: want };
      }),
    );
  }, [selectedId, setRfNodes]);

  // Fullscreen overlay toggle.
  const [isFull, setIsFull] = useState(false);
  useEffect(() => {
    if (!isFull) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setIsFull(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isFull]);

  // Highlight edges touching the selected node.
  const neighborIds = useMemo(() => {
    if (!selectedId) return new Set<string>();
    const s = new Set<string>();
    for (const e of edges) {
      if (e.source === selectedId) s.add(e.target);
      if (e.target === selectedId) s.add(e.source);
    }
    return s;
  }, [edges, selectedId]);

  const rfEdges: Edge[] = useMemo(
    () =>
      edges.map((e, i) => {
        const active =
          !!selectedId && (e.source === selectedId || e.target === selectedId);
        return {
          id: `e${i}`,
          source: e.source,
          target: e.target,
          style: {
            stroke: active ? "var(--color-accent)" : "var(--color-fg-muted)",
            strokeWidth: active ? 2 : Math.min(3, 0.6 + e.weight * 0.25),
            opacity: selectedId ? (active ? 0.9 : 0.12) : 0.35,
          },
        };
      }),
    [edges, selectedId],
  );
  void neighborIds;

  if (nodes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        No code-graph edges — the repo may be a flat set of files.
      </div>
    );
  }

  return (
    <div className={isFull ? "fixed inset-0 z-50 bg-surface" : "h-full w-full"}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        nodesDraggable
        fitView
        minZoom={0.05}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_e, node) => onSelect?.(node.id)}
        onPaneClick={() => onSelect?.(null)}
      >
        <Background gap={20} />
        <Controls showInteractive={false} />
        <Panel position="top-right">
          <button
            type="button"
            onClick={() => setIsFull((v) => !v)}
            title={isFull ? "Exit fullscreen (Esc)" : "Fullscreen"}
            aria-label={isFull ? "Exit fullscreen" : "Fullscreen"}
            className="rounded-sm border p-1.5"
            style={{
              background: "var(--color-surface)",
              borderColor: "var(--color-border-subtle)",
              color: "var(--color-fg-muted)",
            }}
          >
            {isFull ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
        </Panel>
      </ReactFlow>
    </div>
  );
}
