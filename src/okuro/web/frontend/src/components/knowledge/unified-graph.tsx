/**
 * UnifiedGraph — React Flow canvas over a unified knowledge graph payload.
 *
 * Layout: d3-force (force-directed). Runs synchronously on node-set change.
 * Layout-switching (radial / hierarchical) is intentionally deferred — adding
 * a switcher button is cheap once we know force is the right default in
 * practice. Spatial memory is preserved: positions are NOT re-computed when
 * filters narrow the visible set, only when the underlying node IDs change.
 *
 * Visual encoding (locked here, not theming — these ARE the semantics):
 *   - Hue:   memory=accent, thought=warning, artifact=info,
 *            progress=tertiary, kg_entity=secondary
 *   - Opacity: maps confidence (0.5–1.0)
 *   - Border: supersedes-chain head gets accent ring
 *   - Edge:  solid=parent, dashed=supersedes, dotted=tunnel,
 *            thick=memory_ref, labeled=kg_triple, thin-tertiary=progress_ref
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  Handle,
  Position,
  useReactFlow,
  type Node,
  type Edge,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  forceSimulation,
  forceManyBody,
  forceLink,
  forceCenter,
  forceCollide,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from "d3-force";
import type {
  KnowledgeNode,
  KnowledgeEdge,
  KnowledgeNodeType,
} from "@/types/knowledge";
import { cn } from "@/lib/utils";

interface UnifiedGraphProps {
  nodes: KnowledgeNode[];
  edges: KnowledgeEdge[];
  onSelect?: (nodeId: string | null) => void;
  selectedId?: string | null;
}

const TYPE_CLASS: Record<KnowledgeNodeType, string> = {
  memory: "bg-accent-subtle text-accent",
  thought: "bg-warning/15 text-warning",
  artifact: "bg-info/15 text-info",
  progress: "bg-tertiary/15 text-tertiary",
  kg_entity: "bg-fg-muted/10 text-fg-muted",
};

// Layer palette for code_ref entities. Overrides TYPE_CLASS when n.layer is set.
// Kept in CSS-var space so dark/light themes pick up the right token.
export const LAYER_COLOR: Record<string, string> = {
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

const TYPE_LABEL_SHORT: Record<KnowledgeNodeType, string> = {
  memory: "MEM",
  thought: "THG",
  artifact: "ART",
  progress: "PRG",
  kg_entity: "KGE",
};

// ── Custom node renderer ─────────────────────────────────────────────

function KnowledgeNodeCard({ data, selected }: NodeProps) {
  const n = data as unknown as KnowledgeNode & { selected?: boolean };
  const opacity =
    n.confidence == null ? 1 : 0.5 + 0.5 * Math.max(0, Math.min(1, n.confidence));
  const layerColor =
    n.type === "kg_entity" && n.layer ? LAYER_COLOR[n.layer] ?? undefined : undefined;
  return (
    <div
      className={cn(
        "min-w-[17rem] max-w-[28rem] rounded-sm border bg-surface px-2 py-1.5 transition-shadow",
        // When a layer color is applied we drop TYPE_CLASS's bg/text so the
        // border accent does the lifting instead of competing colors.
        layerColor ? "text-fg" : TYPE_CLASS[n.type],
        selected ? "border-accent ring-1 ring-accent" : "border-border-subtle",
        n.is_supersedes_head ? "outline outline-1 outline-accent/40" : "",
      )}
      style={{
        opacity,
        ...(layerColor && !selected
          ? { borderLeft: `3px solid ${layerColor}` }
          : {}),
      }}
    >
      {/* invisible handles — needed for React Flow edges */}
      <Handle type="target" position={Position.Top} className="!opacity-0" />
      <Handle type="source" position={Position.Bottom} className="!opacity-0" />

      <div className="flex items-center justify-between gap-1 text-[10px] uppercase tracking-wider">
        <span className="font-mono">{TYPE_LABEL_SHORT[n.type]}</span>
        {n.project && (
          <span className="font-mono text-tertiary truncate max-w-[14rem]">
            {n.project}
          </span>
        )}
      </div>
      <div className="mt-0.5 truncate text-xs font-medium text-fg">
        {n.topic ? `[${n.topic}] ` : ""}
        {n.label}
      </div>
      {n.type === "kg_entity" && n.layer && (
        <div className="mt-0.5 text-[9px] uppercase tracking-wider opacity-70">
          {n.layer}
        </div>
      )}
    </div>
  );
}

const nodeTypes = { knowledge: KnowledgeNodeCard };

// ── d3-force layout ──────────────────────────────────────────────────

interface SimNode extends SimulationNodeDatum {
  id: string;
}

interface SimLink extends SimulationLinkDatum<SimNode> {
  source: string | SimNode;
  target: string | SimNode;
}

function runForceLayout(
  nodeIds: string[],
  edges: KnowledgeEdge[],
): Map<string, { x: number; y: number }> {
  if (nodeIds.length === 0) return new Map();

  const sim: SimNode[] = nodeIds.map((id) => ({ id }));
  const idSet = new Set(nodeIds);
  const links: SimLink[] = edges
    .filter((e) => idSet.has(e.source) && idSet.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  const radius = Math.sqrt(nodeIds.length) * 28;

  const sim2 = forceSimulation(sim)
    .force("charge", forceManyBody().strength(-180))
    .force(
      "link",
      forceLink<SimNode, SimLink>(links).id((d) => d.id).distance(85).strength(0.6),
    )
    .force("center", forceCenter(0, 0).strength(0.05))
    .force("collide", forceCollide().radius(45))
    .stop();

  // Run synchronously — tick count tuned for 200-500 nodes
  const ticks = Math.min(300, Math.max(120, Math.round(nodeIds.length * 0.6)));
  for (let i = 0; i < ticks; i++) sim2.tick();

  const out = new Map<string, { x: number; y: number }>();
  for (const n of sim) {
    out.set(n.id, { x: n.x ?? 0, y: n.y ?? 0 });
  }
  // Use radius for fitView heuristic later if needed
  void radius;
  return out;
}

// ── Edge styling ─────────────────────────────────────────────────────

const EDGE_COLOR = {
  parent: "var(--color-info)",
  supersedes: "var(--color-warning)",
  memory_ref: "var(--color-accent)",
  tunnel: "var(--color-fg-tertiary)",
  kg_triple: "var(--color-fg-muted)",
  progress_ref: "var(--color-fg-tertiary)",
} as const;

const EDGE_DASH = {
  parent: undefined,
  supersedes: "6 4",
  memory_ref: undefined,
  tunnel: "2 4",
  kg_triple: undefined,
  progress_ref: "3 3",
} as const;

const EDGE_WIDTH = {
  parent: 1.5,
  supersedes: 1.2,
  memory_ref: 2,
  tunnel: 1,
  kg_triple: 1,
  progress_ref: 1,
} as const;

// ── Main component ───────────────────────────────────────────────────

export function UnifiedGraph({
  nodes,
  edges,
  onSelect,
  selectedId,
}: UnifiedGraphProps) {
  // Stable signature of node ids so we only re-layout when the set changes.
  const idSig = useMemo(
    () => nodes.map((n) => n.id).sort().join("|"),
    [nodes],
  );

  const positions = useMemo(() => {
    const ids = idSig ? idSig.split("|") : [];
    return runForceLayout(ids, edges);
    // edges intentionally excluded from dep: layout settles on first run;
    // re-layout only when id-set changes. Edge churn within a fixed node set
    // (e.g. filter toggle that hides edges) does NOT move nodes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idSig]);

  const rfNodes: Node[] = useMemo(
    () =>
      nodes.map((n) => {
        const p = positions.get(n.id) ?? { x: 0, y: 0 };
        return {
          id: n.id,
          type: "knowledge",
          position: p,
          data: n as unknown as Record<string, unknown>,
          selected: n.id === selectedId,
        };
      }),
    [nodes, positions, selectedId],
  );

  const rfEdges: Edge[] = useMemo(
    () =>
      edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        type: "default",
        label: e.type === "kg_triple" || e.type === "tunnel" ? e.label ?? undefined : undefined,
        labelStyle: {
          fill: "var(--color-fg-tertiary)",
          fontSize: 9,
          fontFamily: "var(--font-mono, 'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Code', 'Roboto Mono', Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace)",
        },
        labelBgStyle: { fill: "var(--color-surface)" },
        labelBgPadding: [3, 1] as [number, number],
        style: {
          stroke: EDGE_COLOR[e.type] ?? "var(--color-border)",
          strokeWidth: EDGE_WIDTH[e.type] ?? 1,
          strokeDasharray: EDGE_DASH[e.type],
          opacity: 0.7,
        },
      })),
    [edges],
  );

  return (
    <div className="relative h-[68vh] w-full overflow-hidden rounded-sm border border-border-subtle bg-surface-subtle">
      <ReactFlowProvider>
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.15, maxZoom: 1.25 }}
          minZoom={0.1}
          maxZoom={2.5}
          proOptions={{ hideAttribution: true }}
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable
          onNodeClick={(_, node) => onSelect?.(node.id)}
          onPaneClick={() => onSelect?.(null)}
          colorMode="dark"
        >
          <Background gap={32} size={1} color="var(--color-border-subtle)" />
          <Controls
            showInteractive={false}
            className="!bg-surface !border !border-border-subtle"
          />
          <Legend />
          <LayerLegend nodes={nodes} />
          <FocusController selectedId={selectedId ?? null} positions={positions} />
        </ReactFlow>
      </ReactFlowProvider>
    </div>
  );
}

// ── Focus controller ────────────────────────────────────────────────
//
// Animates the viewport to centre on the currently-selected node. Lives
// inside ReactFlowProvider so it can call useReactFlow(). Triggered by
// selectedId changes — covers both graph-click and TourPanel-click
// because both routes set the same state up in /knowledge.

function FocusController({
  selectedId,
  positions,
}: {
  selectedId: string | null;
  positions: Map<string, { x: number; y: number }>;
}) {
  const rf = useReactFlow();
  const lastFocused = useRef<string | null>(null);

  useEffect(() => {
    if (!selectedId || selectedId === lastFocused.current) return;
    const pos = positions.get(selectedId);
    if (!pos) return;
    rf.setCenter(pos.x, pos.y, { zoom: 1.4, duration: 400 });
    lastFocused.current = selectedId;
  }, [selectedId, positions, rf]);

  return null;
}

// ── Legend (corner chip) ─────────────────────────────────────────────

const LEGEND: Array<{ label: string; kind: "node" | "edge"; sample: KnowledgeNodeType | keyof typeof EDGE_COLOR }> = [
  { label: "memory", kind: "node", sample: "memory" },
  { label: "thought", kind: "node", sample: "thought" },
  { label: "artifact", kind: "node", sample: "artifact" },
  { label: "progress", kind: "node", sample: "progress" },
  { label: "kg entity", kind: "node", sample: "kg_entity" },
  { label: "parent", kind: "edge", sample: "parent" },
  { label: "supersedes", kind: "edge", sample: "supersedes" },
  { label: "ref", kind: "edge", sample: "memory_ref" },
  { label: "tunnel", kind: "edge", sample: "tunnel" },
];

function Legend() {
  const [open, setOpen] = useState(false);
  return (
    <div
      className="absolute bottom-3 right-3 z-10 max-w-[28rem] rounded-sm border border-border-subtle bg-surface/95 px-2.5 py-2 text-2xs uppercase tracking-wider text-tertiary backdrop-blur"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="font-mono hover:text-fg-muted"
      >
        {open ? "− legend" : "+ legend"}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1">
          {LEGEND.map((it) => (
            <div key={it.label} className="flex items-center gap-2">
              {it.kind === "node" ? (
                <span
                  className={cn(
                    "inline-block h-2.5 w-2.5 rounded-sm",
                    TYPE_CLASS[it.sample as KnowledgeNodeType],
                  )}
                />
              ) : (
                <span
                  className="inline-block h-px w-5"
                  style={{
                    background: EDGE_COLOR[it.sample as keyof typeof EDGE_COLOR],
                    borderTop: `1px ${
                      EDGE_DASH[it.sample as keyof typeof EDGE_DASH] ? "dashed" : "solid"
                    } ${EDGE_COLOR[it.sample as keyof typeof EDGE_COLOR]}`,
                  }}
                />
              )}
              <span>{it.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Layer legend (only renders when at least one node has a layer) ───

function LayerLegend({ nodes }: { nodes: KnowledgeNode[] }) {
  const present = useMemo(() => {
    const counts = new Map<string, number>();
    for (const n of nodes) {
      if (n.type !== "kg_entity" || !n.layer) continue;
      counts.set(n.layer, (counts.get(n.layer) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [nodes]);

  if (present.length === 0) return null;

  return (
    <div className="absolute bottom-3 left-3 z-10 max-w-[28rem] rounded-sm border border-border-subtle bg-surface/95 px-2.5 py-2 text-2xs uppercase tracking-wider text-tertiary backdrop-blur">
      <div className="font-mono">layers</div>
      <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1">
        {present.map(([layer, count]) => (
          <div key={layer} className="flex items-center gap-1.5">
            <span
              className="inline-block h-2.5 w-2.5 rounded-sm"
              style={{ background: LAYER_COLOR[layer] ?? LAYER_COLOR.unknown }}
            />
            <span className="truncate">{layer}</span>
            <span className="ml-auto font-mono text-fg-muted">{count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Suppress unused-import warning when ref isn't needed in current code path.
void useRef;
void useEffect;
