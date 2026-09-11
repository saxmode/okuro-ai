// <!-- AGENT_HEADER
// role: code
// purpose: okuro-flow gallery thumbnail — a clean, Figma-style preview of a
//   flow: solid coloured node blocks + thin edges on a mini-canvas, fitted with
//   padding. NO labels (illegible at thumb size; recognition is by composition,
//   like Figma file thumbnails). Graph fetched lazily on scroll-in + cached, so
//   a big gallery stays cheap. No ReactFlow boot, no iframe.
// AGENT_HEADER_END -->
import React from "react";

import { flowDesignerApi, type FlowDesignerGraph } from "@/lib/api";

const GRAPH_CACHE = new Map<string, FlowDesignerGraph>();

type NodeBox = { id: string; x: number; y: number; w: number; h: number; color: string };

function box(n: any, accent: string): NodeBox {
  const x = n?.position?.x ?? 0;
  const y = n?.position?.y ?? 0;
  const w = n?.width ?? n?.measured?.width ?? n?.style?.width ?? 180;
  const h = n?.height ?? n?.measured?.height ?? n?.style?.height ?? 80;
  return { id: String(n?.id ?? Math.random()), x: +x, y: +y, w: +w || 180, h: +h || 80, color: n?.data?.color || accent };
}

function ThumbSVG({ graph, accent }: { graph: FlowDesignerGraph; accent: string }) {
  const nodes = (graph.nodes || []) as any[];
  const edges = (graph.edges || []) as any[];
  if (!nodes.length) return <span className="fd-thumb-empty">Empty flow</span>;

  const bs = nodes.map((n) => box(n, accent));
  const minX = Math.min(...bs.map((b) => b.x));
  const minY = Math.min(...bs.map((b) => b.y));
  const maxX = Math.max(...bs.map((b) => b.x + b.w));
  const maxY = Math.max(...bs.map((b) => b.y + b.h));
  const W = Math.max(1, maxX - minX);
  const H = Math.max(1, maxY - minY);
  const pad = Math.max(W, H) * 0.06;
  const r = Math.max(W, H) * 0.012;
  const stroke = Math.max(W, H) * 0.0045;
  const center = new Map(bs.map((b) => [b.id, { cx: b.x + b.w / 2, cy: b.y + b.h / 2 }]));

  return (
    <svg
      className="fd-thumb-svg"
      viewBox={`${minX - pad} ${minY - pad} ${W + pad * 2} ${H + pad * 2}`}
      preserveAspectRatio="xMidYMid meet"
    >
      {edges.map((e, i) => {
        const s = center.get(String(e?.source));
        const t = center.get(String(e?.target));
        if (!s || !t) return null;
        return <line key={"e" + i} x1={s.cx} y1={s.cy} x2={t.cx} y2={t.cy} stroke="var(--fd-line)" strokeWidth={stroke} />;
      })}
      {bs.map((b) => (
        <rect key={b.id} x={b.x} y={b.y} width={b.w} height={b.h} rx={r} ry={r} fill={b.color} opacity={0.9} />
      ))}
    </svg>
  );
}

/** Lazy flow preview — renders the mini-canvas once the card nears the viewport. */
export function FlowThumbnail({ id, accent = "var(--color-accent)" }: { id: string; accent?: string }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const [graph, setGraph] = React.useState<FlowDesignerGraph | null>(() => GRAPH_CACHE.get(id) || null);

  React.useEffect(() => {
    if (graph) return;
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          io.disconnect();
          flowDesignerApi
            .get(id)
            .then((d) => {
              GRAPH_CACHE.set(id, d.graph);
              setGraph(d.graph);
            })
            .catch(() => {});
        }
      },
      { rootMargin: "300px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [id, graph]);

  return (
    <div ref={ref} className="fd-thumb">
      {graph ? <ThumbSVG graph={graph} accent={accent} /> : <span className="fd-thumb-skel" />}
    </div>
  );
}
