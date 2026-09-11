// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism okuro-flow embed block (M2 leapfrog #3) — embeds a SAVED
//   okuro flow as a deck module. Lazy-fetches the flow graph by id and renders a
//   read-only labeled SVG (fit-to-view, header-coloured node blocks + edges) —
//   no ReactFlow boot, so it stays light inside the deck. Links out to the full
//   flow designer (/flow?id=…). This is the module NO surveyed competitor ships:
//   a deck that is a window into a live agent flow.
// index: box | FlowSVG | FlowEmbed
// AGENT_HEADER_END -->
import { Maximize2, X } from "lucide-react";
import { Suspense, lazy, useEffect, useState } from "react";
import { Link } from "react-router";

import { flowDesignerApi, type FlowDesignerGraph } from "@/lib/api";

// The full ReactFlow editor — heavy, so lazy-loaded and mounted ONLY when the
// user maximizes a flow module to edit it in-context. Its `embed` mode drops
// the page chrome and it autosaves to the backend, so edits persist live.
const FlowDesigner = lazy(() =>
  import("@/components/flow-designer/flow-designer").then((m) => ({ default: m.FlowDesigner })),
);

type NodeBox = { id: string; x: number; y: number; w: number; h: number; color: string; label: string };

/** Node header colour by category (mirrors flow-designer CAT), accent fallback. */
const CAT_COLOR: Record<string, string> = {
  process: "#4aa3ff", input: "#46d17a", output: "#ffc24d",
  decision: "#ff6fae", note: "#9aa0aa", group: "#b07cff",
};

function box(n: any, accent: string): NodeBox {
  const w = Number(n?.width ?? n?.measured?.width ?? n?.style?.width ?? 180) || 180;
  const h = Number(n?.height ?? n?.measured?.height ?? n?.style?.height ?? 80) || 80;
  const label = String(n?.data?.label ?? n?.data?.title ?? n?.data?.name ?? n?.id ?? "");
  const color = n?.data?.color || CAT_COLOR[n?.type] || accent;
  return { id: String(n?.id ?? Math.random()), x: Number(n?.position?.x ?? 0), y: Number(n?.position?.y ?? 0), w, h, color, label };
}

function FlowSVG({ graph, accent }: { graph: FlowDesignerGraph; accent: string }) {
  const nodes = (graph.nodes || []) as any[];
  const edges = (graph.edges || []) as any[];
  if (!nodes.length) return <div className="p-6 text-center text-sm opacity-50">Empty flow</div>;

  const bs = nodes.map((n) => box(n, accent));
  const minX = Math.min(...bs.map((b) => b.x));
  const minY = Math.min(...bs.map((b) => b.y));
  const maxX = Math.max(...bs.map((b) => b.x + b.w));
  const maxY = Math.max(...bs.map((b) => b.y + b.h));
  const W = Math.max(1, maxX - minX);
  const Hh = Math.max(1, maxY - minY);
  const pad = Math.max(W, Hh) * 0.04;
  const center = new Map(bs.map((b) => [b.id, { cx: b.x + b.w / 2, cy: b.y + b.h / 2 }]));
  const fs = Math.max(11, Math.min(W, Hh) * 0.03);

  return (
    <svg
      viewBox={`${minX - pad} ${minY - pad} ${W + pad * 2} ${Hh + pad * 2}`}
      preserveAspectRatio="xMidYMid meet"
      className="max-h-[420px] w-full"
      role="img"
    >
      {edges.map((e, i) => {
        const s = center.get(String(e?.source));
        const t = center.get(String(e?.target));
        if (!s || !t) return null;
        return <line key={"e" + i} x1={s.cx} y1={s.cy} x2={t.cx} y2={t.cy} stroke={accent} strokeOpacity={0.4} strokeWidth={2} />;
      })}
      {bs.map((b) => (
        <g key={b.id}>
          <rect x={b.x} y={b.y} width={b.w} height={b.h} rx={8} fill="var(--color-background-base, #0b0b0f)" stroke={b.color} strokeWidth={2} />
          <rect x={b.x} y={b.y} width={b.w} height={Math.min(8, b.h)} rx={4} fill={b.color} />
          <text x={b.x + b.w / 2} y={b.y + b.h / 2 + fs / 3} textAnchor="middle" fontSize={fs} fill="var(--color-fg, #e8eaf0)">
            {b.label.length > 22 ? b.label.slice(0, 21) + "…" : b.label}
          </text>
        </g>
      ))}
    </svg>
  );
}

/** M2 flow-embed module — fetch a saved flow by id, render read-only. */
export default function FlowEmbed({ flowId, caption, accent = "var(--color-accent)" }: { flowId: string; caption?: string; accent?: string }) {
  const [graph, setGraph] = useState<FlowDesignerGraph | null>(null);
  const [name, setName] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    setGraph(null);
    flowDesignerApi
      .get(flowId)
      .then((d) => {
        if (cancelled) return;
        setGraph(d.graph);
        setName(d.name || "");
      })
      .catch((e) => !cancelled && setErr(String((e as Error)?.message || e)));
    return () => { cancelled = true; };
    // Re-fetch when the editor modal closes so the read-only embed reflects
    // the just-made (autosaved) edits.
  }, [flowId, editing]);

  return (
    <figure className="rounded-lg border border-current/10">
      <div className="flex items-center gap-2 border-b border-current/10 px-3 py-1.5 text-xs">
        <span className="rounded-full bg-accent/10 px-2 py-0.5 text-accent">okuro flow</span>
        <span className="truncate opacity-70">{name || flowId}</span>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <button
            onClick={(e) => { e.stopPropagation(); setEditing(true); }}
            title="maximize & edit in-context"
            className="flex items-center gap-1 opacity-70 hover:opacity-100"
          >
            <Maximize2 size={12} /> Edit
          </button>
          <Link to={`/flow?id=${encodeURIComponent(flowId)}`} className="underline decoration-dotted underline-offset-2 opacity-70 hover:opacity-100">
            Open →
          </Link>
        </div>
      </div>
      <div className="p-2">
        {err ? (
          <div className="p-4 text-center text-sm opacity-50">Flow unavailable ({flowId})</div>
        ) : graph ? (
          <FlowSVG graph={graph} accent={accent} />
        ) : (
          <div className="p-6 text-center text-sm opacity-40">Loading flow…</div>
        )}
      </div>
      {caption && <figcaption className="px-3 pb-2 text-xs opacity-60">{caption}</figcaption>}

      {editing && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={(e) => { e.stopPropagation(); setEditing(false); }}
        >
          <div
            className="flex h-[90vh] w-full max-w-6xl flex-col overflow-hidden rounded-xl border border-border shadow-2xl"
            style={{ background: "var(--color-background-base, #0b0b0f)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-sm">
              <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs text-accent">okuro flow · editing</span>
              <span className="truncate opacity-70">{name || flowId}</span>
              <Link
                to={`/flow?id=${encodeURIComponent(flowId)}`}
                className="ml-auto text-xs underline decoration-dotted underline-offset-2 opacity-70 hover:opacity-100"
              >
                Open full page →
              </Link>
              <button
                onClick={() => setEditing(false)}
                title="close (edits autosave)"
                className="rounded-md border border-border px-1.5 py-1 opacity-70 hover:opacity-100"
              >
                <X size={14} />
              </button>
            </div>
            <div className="min-h-0 flex-1">
              <Suspense fallback={<div className="flex h-full items-center justify-center text-sm opacity-50">Loading editor…</div>}>
                <FlowDesigner flowId={flowId} embed />
              </Suspense>
            </div>
          </div>
        </div>
      )}
    </figure>
  );
}
