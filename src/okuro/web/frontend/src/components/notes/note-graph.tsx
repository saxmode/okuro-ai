import { useMemo } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
} from "@xyflow/react";
import * as dagre from "@dagrejs/dagre";
import "@xyflow/react/dist/style.css";

interface GraphNode {
  id: string;
  title: string;
}
interface GraphEdge {
  source: string;
  target: string;
}

interface NoteGraphProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  activeId: string | null;
  onSelect: (id: string) => void;
}

const NODE_W = 168;
const NODE_H = 36;

/** Obsidian-style link graph. Dagre lays nodes out left→right; clicking a node
 *  opens that note. Note→note edges only (ghost links have no target). */
function GraphInner({ nodes, edges, activeId, onSelect }: NoteGraphProps) {
  const { rfNodes, rfEdges } = useMemo(() => {
    const g = new dagre.graphlib.Graph();
    g.setDefaultEdgeLabel(() => ({}));
    g.setGraph({ rankdir: "LR", nodesep: 24, ranksep: 64 });
    nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
    edges.forEach((e) => g.setEdge(e.source, e.target));
    dagre.layout(g);

    const rfNodes: Node[] = nodes.map((n) => {
      const pos = g.node(n.id);
      const active = n.id === activeId;
      return {
        id: n.id,
        position: { x: (pos?.x ?? 0) - NODE_W / 2, y: (pos?.y ?? 0) - NODE_H / 2 },
        data: { label: n.title || "Untitled" },
        style: {
          width: NODE_W,
          fontSize: 12,
          padding: "6px 10px",
          borderRadius: 8,
          border: `1px solid ${active ? "var(--color-accent)" : "var(--color-border)"}`,
          background: active ? "var(--color-accent-subtle)" : "var(--color-surface-elevated)",
          color: "var(--color-fg-primary)",
          textAlign: "center" as const,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap" as const,
        },
      };
    });
    const rfEdges: Edge[] = edges.map((e, i) => ({
      id: `${e.source}->${e.target}-${i}`,
      source: e.source,
      target: e.target,
      style: { stroke: "var(--color-border-hover)" },
      animated: false,
    }));
    return { rfNodes, rfEdges };
  }, [nodes, edges, activeId]);

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      onNodeClick={(_, node) => onSelect(node.id)}
      fitView
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      proOptions={{ hideAttribution: true }}
      minZoom={0.2}
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--color-border-subtle)" />
      <Controls position="bottom-right" showInteractive={false} />
    </ReactFlow>
  );
}

export function NoteGraph(props: NoteGraphProps) {
  if (props.nodes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-tertiary">
        No notes to graph yet.
      </div>
    );
  }
  return (
    <ReactFlowProvider>
      <GraphInner {...props} />
    </ReactFlowProvider>
  );
}
