// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism interactive-graph block — a pan/zoom analytical graph over
//   ReactFlow + dagre (the flow-designer stack, reused; no cytoscape). Three
//   altitudes (SPINE → CLUSTERS → FULL) + an analytical layer (reference-parity,
//   Phase C.1): click a node for a detail panel (kind/summary/tags/edges),
//   neighborhood highlight, live search, and group filters — non-matches fade.
//   Lazy-loaded by BlockView.
// index: types | atAltitude | layout | GraphBlock
// AGENT_HEADER_END -->
import * as dagre from "@dagrejs/dagre";
import { Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider, type Edge, type Node } from "@xyflow/react";
import { useMemo, useState } from "react";
import "@xyflow/react/dist/style.css";

import type { Block } from "@/lib/prism-api";

type GraphData = Extract<Block, { type: "graph" }>;
type GNode = GraphData["nodes"][number];
type GEdge = GraphData["edges"][number];

type Altitude = "spine" | "clusters" | "full";
const ALTITUDES: { key: Altitude; label: string }[] = [
  { key: "spine", label: "Spine" },
  { key: "clusters", label: "Clusters" },
  { key: "full", label: "Full" },
];

const NODE_W = 150;
const NODE_H = 44;

/** Reduce the raw graph to the nodes+edges shown at a given altitude. */
function atAltitude(data: GraphData, alt: Altitude): { nodes: GNode[]; edges: GEdge[] } {
  const nodes = data.nodes ?? [];
  const edges = data.edges ?? [];
  if (alt === "full") return { nodes, edges };

  if (alt === "spine") {
    let keep = new Set(nodes.filter((n) => n.spine).map((n) => n.id));
    if (keep.size === 0) {
      const deg = new Map<string, number>();
      edges.forEach((e) => {
        deg.set(e.from, (deg.get(e.from) ?? 0) + 1);
        deg.set(e.to, (deg.get(e.to) ?? 0) + 1);
      });
      const ranked = [...nodes].sort((a, b) => (deg.get(b.id) ?? 0) - (deg.get(a.id) ?? 0));
      keep = new Set(ranked.slice(0, Math.max(3, Math.ceil(nodes.length / 2))).map((n) => n.id));
    }
    return {
      nodes: nodes.filter((n) => keep.has(n.id)),
      edges: edges.filter((e) => keep.has(e.from) && keep.has(e.to)),
    };
  }

  // clusters: collapse grouped nodes into one super-node per group.
  const groupOf = new Map<string, string>();
  nodes.forEach((n) => groupOf.set(n.id, n.group ? `group:${n.group}` : n.id));
  const seen = new Map<string, GNode>();
  nodes.forEach((n) => {
    const id = groupOf.get(n.id)!;
    if (!seen.has(id)) seen.set(id, n.group ? { id, label: n.group, group: n.group } : n);
  });
  const eseen = new Set<string>();
  const cedges: GEdge[] = [];
  edges.forEach((e) => {
    const from = groupOf.get(e.from) ?? e.from;
    const to = groupOf.get(e.to) ?? e.to;
    if (from === to) return;
    const key = `${from}→${to}`;
    if (eseen.has(key)) return;
    eseen.add(key);
    cedges.push({ from, to });
  });
  return { nodes: [...seen.values()], edges: cedges };
}

/** Left→right dagre layout → absolute node positions (positions only). */
function layout(gnodes: GNode[], gedges: GEdge[]): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 72 });
  g.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(gnodes.map((n) => n.id));
  gnodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  gedges.forEach((e) => { if (ids.has(e.from) && ids.has(e.to)) g.setEdge(e.from, e.to); });
  dagre.layout(g);
  const pos = new Map<string, { x: number; y: number }>();
  gnodes.forEach((n) => {
    const gn = g.node(n.id);
    pos.set(n.id, { x: (gn?.x ?? 0) - NODE_W / 2, y: (gn?.y ?? 0) - NODE_H / 2 });
  });
  return pos;
}

/** M2/C1 interactive-graph module. `accent` threads brand accent. */
export default function GraphBlock({ data, accent = "var(--color-accent)" }: { data: GraphData; accent?: string }) {
  const hasClusters = useMemo(() => (data.nodes ?? []).some((n) => n.group), [data.nodes]);
  const groups = useMemo(() => [...new Set((data.nodes ?? []).map((n) => n.group).filter(Boolean))] as string[], [data.nodes]);
  const [alt, setAlt] = useState<Altitude>("full");
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [groupFilter, setGroupFilter] = useState<Set<string>>(new Set());

  const reduced = useMemo(() => atAltitude(data, alt), [data, alt]);
  const pos = useMemo(() => layout(reduced.nodes, reduced.edges), [reduced]);
  const byId = useMemo(() => new Map(reduced.nodes.map((n) => [n.id, n])), [reduced]);
  const ids = useMemo(() => new Set(reduced.nodes.map((n) => n.id)), [reduced]);

  // Search + group filter → the "matched" set (matches stay solid, rest fade).
  const q = query.trim().toLowerCase();
  const matched = useMemo(() => new Set(reduced.nodes.filter((n) => {
    const okQ = !q || `${n.label} ${n.summary ?? ""} ${(n.tags ?? []).join(" ")}`.toLowerCase().includes(q);
    const okG = !groupFilter.size || (n.group != null && groupFilter.has(n.group));
    return okQ && okG;
  }).map((n) => n.id)), [reduced, q, groupFilter]);

  // Neighborhood of the selected node (itself + directly connected).
  const neighborhood = useMemo(() => {
    if (!selected) return null;
    const s = new Set<string>([selected]);
    reduced.edges.forEach((e) => {
      if (e.from === selected) s.add(e.to);
      if (e.to === selected) s.add(e.from);
    });
    return s;
  }, [selected, reduced]);

  const litFor = (id: string) => matched.has(id) && (!neighborhood || neighborhood.has(id));

  const nodes: Node[] = useMemo(() => reduced.nodes.map((n) => {
    const lit = litFor(n.id);
    const isSel = n.id === selected;
    const isGroup = n.id.startsWith("group:");
    return {
      id: n.id,
      position: pos.get(n.id) ?? { x: 0, y: 0 },
      data: { label: n.label },
      draggable: true,
      connectable: false,
      style: {
        width: NODE_W, fontSize: 12, borderRadius: 8, padding: "6px 8px",
        border: `${isSel ? 2 : 1}px solid ${lit ? accent : "currentColor"}`,
        opacity: lit ? 1 : 0.2,
        background: isGroup ? accent : "color-mix(in srgb, var(--color-background-base, #0b0b0f) 88%, transparent)",
        color: isGroup ? "var(--color-background-base, #0b0b0f)" : "var(--color-fg, #e8eaf0)",
        fontWeight: isGroup || isSel ? 600 : 400,
        boxShadow: isSel ? `0 0 0 3px ${accent}44` : undefined,
      },
    };
  }), [reduced, pos, matched, neighborhood, selected, accent]);

  const edges: Edge[] = useMemo(() => reduced.edges
    .filter((e) => ids.has(e.from) && ids.has(e.to))
    .map((e, i) => {
      const lit = litFor(e.from) && litFor(e.to);
      return {
        id: `e${i}`, source: e.from, target: e.to, label: e.label, animated: false,
        style: { stroke: accent, strokeOpacity: lit ? 0.55 : 0.08 },
        labelStyle: { fill: "var(--color-fg, #e8eaf0)", fontSize: 10, opacity: lit ? 0.7 : 0.1 },
      };
    }), [reduced, ids, matched, neighborhood, accent]);

  if (!data.nodes?.length) return null;
  const shown = ALTITUDES.filter((a) => a.key !== "clusters" || hasClusters);
  const sel = selected ? byId.get(selected) : null;
  const selEdges = selected
    ? { out: reduced.edges.filter((e) => e.from === selected), in: reduced.edges.filter((e) => e.to === selected) }
    : { out: [], in: [] };

  const chip = (on: boolean) => `rounded px-2 py-0.5 text-[11px] transition-colors ${on ? "bg-accent/15 text-accent" : "opacity-60 hover:opacity-100"}`;

  return (
    <figure className="rounded-lg border border-current/10">
      <div className="flex flex-wrap items-center gap-1 border-b border-current/10 px-2 py-1">
        {shown.map((a) => (
          <button key={a.key} onClick={() => setAlt(a.key)} className={chip(alt === a.key)}>{a.label}</button>
        ))}
        <input
          value={query} onChange={(e) => setQuery(e.target.value)} placeholder="search…"
          className="ml-1 w-28 rounded border border-current/15 bg-transparent px-2 py-0.5 text-[11px] outline-none focus:border-accent/50"
        />
        {groups.length > 0 && groups.map((g) => (
          <button key={g} onClick={() => setGroupFilter((s) => { const n = new Set(s); n.has(g) ? n.delete(g) : n.add(g); return n; })}
            className={chip(groupFilter.has(g))}>{g}</button>
        ))}
        <span className="ml-auto text-[11px] opacity-40">{matched.size}/{reduced.nodes.length}</span>
      </div>

      <div className="flex" style={{ height: 340 }}>
        <div className="min-w-0 flex-1">
          <ReactFlowProvider>
            <ReactFlow
              nodes={nodes} edges={edges} fitView fitViewOptions={{ padding: 0.2 }}
              nodesConnectable={false} edgesFocusable={false} proOptions={{ hideAttribution: true }} minZoom={0.2}
              onNodeClick={(_, n) => setSelected((cur) => cur === n.id ? null : n.id)}
              onPaneClick={() => setSelected(null)}
            >
              <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="currentColor" style={{ opacity: 0.12 }} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </ReactFlowProvider>
        </div>

        {sel && (
          <aside className="w-56 shrink-0 overflow-y-auto border-l border-current/10 p-3 text-sm">
            <div className="flex items-start gap-2">
              <div className="min-w-0">
                {sel.kind && <p className="text-[10px] uppercase tracking-wide" style={{ color: accent }}>{sel.kind}</p>}
                <p className="font-semibold leading-tight">{sel.label}</p>
              </div>
              <button onClick={() => setSelected(null)} className="ml-auto shrink-0 text-xs opacity-50 hover:opacity-100">✕</button>
            </div>
            {sel.summary && <p className="mt-2 text-xs opacity-75">{sel.summary}</p>}
            {(sel.tags ?? []).length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {sel.tags!.map((t, i) => <span key={i} className="rounded bg-current/10 px-1.5 py-0.5 text-[10px] opacity-70">{t}</span>)}
              </div>
            )}
            {(selEdges.out.length > 0 || selEdges.in.length > 0) && (
              <div className="mt-3 space-y-2 border-t border-current/10 pt-2 text-xs">
                {selEdges.out.length > 0 && <Edges title="→ out" list={selEdges.out.map((e) => e.to)} byId={byId} onPick={setSelected} accent={accent} />}
                {selEdges.in.length > 0 && <Edges title="← in" list={selEdges.in.map((e) => e.from)} byId={byId} onPick={setSelected} accent={accent} />}
              </div>
            )}
          </aside>
        )}
      </div>
      {data.caption && <figcaption className="px-3 py-2 text-xs opacity-60">{data.caption}</figcaption>}
    </figure>
  );
}

function Edges({ title, list, byId, onPick, accent }: {
  title: string; list: string[]; byId: Map<string, GNode>; onPick: (id: string) => void; accent: string;
}) {
  return (
    <div>
      <p className="mb-1 opacity-40">{title}</p>
      <ul className="space-y-0.5">
        {list.map((id, i) => (
          <li key={i}>
            <button onClick={() => onPick(id)} className="w-full truncate text-left hover:underline" style={{ color: accent }}>
              {byId.get(id)?.label ?? id}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
