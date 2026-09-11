// @ts-nocheck
// <!-- AGENT_HEADER
// role: code
// purpose: okuro-flow — node/group/edge React components + inline editors,
//   ported from tools/flow-designer. The owning editor supplies its write-back
//   callbacks through BridgeContext; without a provider the nodes render but
//   are not editable.
// AGENT_HEADER_END -->
import { BaseEdge, EdgeLabelRenderer, Handle, NodeResizer, Position, getBezierPath, useNodeConnections, useReactFlow, useStore } from "@xyflow/react";
import React from "react";

import { MarkdownContent } from "@/components/ui/markdown-content";

import { MGIN, MGOUT, PATHFN, POS, SIDES, portDir, portSide, portsOf } from "./graph";
// Per-view configuration: what this node may edit, where its colour comes
// from, its ports and its badges. ONE component, three views — the differences
// arrive as data, never as branches in here.
import { NODE_VIEW_FLOW, NodeViewContext, canEdit, slot, useNodeView } from "./node-view";

// default highlight for every standard element; per-node data.color overrides it
const ACCENT = "var(--color-accent)";

// Pick dark or light body text for a SOLID-filled node, by relative luminance
// (WCAG-ish). Only a concrete #hex can be measured; the accent var and any
// non-hex resolve to dark text — accent is a bright colour on every theme.
function contrastText(color) {
  if (!color || color[0] !== "#") return "#06060a";
  let h = color.slice(1);
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const v = (i) => parseInt(h.slice(i, i + 2), 16) / 255;
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  const L = 0.2126 * lin(v(0)) + 0.7152 * lin(v(2)) + 0.0722 * lin(v(4));
  return L > 0.5 ? "#06060a" : "#f5f5f7";
}

// data.style: "tinted" (default — elevated bg + coloured border), "solid"
// (filled with the colour, contrast text) or "outline" (no fill). Returns the
// className suffix + inline card/chip styles; bg/border/text are dynamic per
// node colour so they live inline, CSS only handles what inline can't.
function variantStyles(variant, col, color) {
  const card = { borderColor: col };
  let chip = { background: col };
  if (variant === "solid") {
    const txt = contrastText(color);
    card.background = col;
    card.color = txt;
    chip = { background: "rgba(0,0,0,0.26)", color: txt };
  } else if (variant === "outline") {
    card.background = "transparent";
  }
  // handle circle keeps the node colour (= default connection colour) always;
  // its ring is handled per-variant in CSS (canvas-bg on solid, none on tinted).
  return { cls: " fd-v-" + variant, card, chip, port: col };
}

// ---- content alignment ----
// data.align = left|center|right (horizontal) · data.valign = top|center|bottom.
// `safe ` prefix falls back to start when content overflows, so a small node
// never clips its own text. Defaults preserve the pre-config look: box nodes
// vertically centre, mdnotes sit top.
const H_FLEX = { left: "flex-start", center: "center", right: "flex-end" };
const V_FLEX = { top: "flex-start", center: "center", bottom: "flex-end" };
const H_TEXT = { left: "left", center: "center", right: "right" };
export const vDefault = (data) => (data && data.cat === "mdnote" ? "top" : "center");
export function alignStyle(data) {
  const h = (data && data.align) || "left";
  const v = (data && data.valign) || vDefault(data);
  return {
    alignItems: "safe " + H_FLEX[h],
    justifyContent: "safe " + V_FLEX[v],
    textAlign: H_TEXT[h],
  };
}
// text-block variant (mdnote): no cross-axis shrink — the markdown stays
// full-width so wrapping holds; H is handled by text-align, V by justify.
export function alignStyleText(data) {
  const h = (data && data.align) || "left";
  const v = (data && data.valign) || vDefault(data);
  return { justifyContent: "safe " + V_FLEX[v], textAlign: H_TEXT[h] };
}

// Drag-to-resize affordance. Handles show only while the node is selected.
// Resized width/height land on node.width/height — serialized + autosaved for free.
// minW/minH mirror the CSS min-width/min-height so content never clips.
function Resizer({ color, minW, minH, visible }) {
  return (
    <NodeResizer
      color={color || ACCENT}
      isVisible={visible}
      minWidth={minW}
      minHeight={minH}
      lineClassName="fd-rz-line"
      handleClassName="fd-rz-handle"
    />
  );
}

// How a node writes an edit back into the graph.
//
// A CONTEXT, not a module-level singleton. This used to be a mutable object in
// module scope that the owning editor assigned onto during render — which meant
// two editors mounted on one page silently overwrote each other's callbacks,
// and that is the reason this node component could not be reused by a second
// view. The shape is unchanged; only the scope moved, from the module to the
// provider. Same pattern as PortSelContext directly below.
//
// The default is a no-op set, so a node rendered WITHOUT a provider (a test, a
// read-only projection) is simply not editable rather than crashing.
export const NOOP_BRIDGE = {
  RENAME: (_id, _v) => {},
  SETTAG: (_id, _v) => {},
  // multi-line body text (/flow's `sub`, /workflows' `prompt`). Only reachable
  // when the view's config lists "sub" as editable.
  SETSUB: (_id, _v) => {},
  EDGE_EDIT: (_id) => {},
  EDGE_SET: (_id, _v) => {},
  EDGE_WAYPOINTS: (_id, _wps) => {},
  COLLAPSE: (_id) => {},
  // click the + on an empty port → spawn a connected node on that side.
  // (nodeId, portId, dir: "source"|"target", side: "left"|"right"|"top"|"bottom")
  PORT_ADD: (_nodeId, _portId, _dir, _side) => {},
  // long-press a port → select it so the toolbar can re-place it.
  // (nodeId, portId)
  PORT_SELECT: (_nodeId, _portId) => {},
};

export const BridgeContext = React.createContext(NOOP_BRIDGE);
export const useBridge = () => React.useContext(BridgeContext);


// Which port (if any) is currently selected for re-placement. Provided by the
// app around <ReactFlow>; consumed by PortHandle to render the selected ring.
export const PortSelContext = React.createContext(null);

// A port handle with tap / long-press / drag disambiguation:
//   tap (empty port)  → spawn a connected node (bridge.PORT_ADD)
//   long-press        → select the port for re-placement (bridge.PORT_SELECT)
//   drag              → start a connection (React Flow's own handling)
// A move past the threshold cancels the long-press/tap so drags stay drags.
const LONG_MS = 450;
const MOVE_PX = 8;
function PortHandle({ nodeId, portId, dir, side, style, label, open }) {
  const bridge = useBridge();
  const sel = React.useContext(PortSelContext);
  const selected = sel && sel.nodeId === nodeId && sel.portId === portId;
  const g = React.useRef(null);
  const onPointerDown = (e) => {
    const timer = setTimeout(() => {
      if (g.current) {
        g.current.long = true;
        bridge.PORT_SELECT(nodeId, portId);
      }
    }, LONG_MS);
    g.current = { x: e.clientX, y: e.clientY, moved: false, long: false, timer };
  };
  const onPointerMove = (e) => {
    const s = g.current;
    if (!s) return;
    if (Math.abs(e.clientX - s.x) > MOVE_PX || Math.abs(e.clientY - s.y) > MOVE_PX) {
      s.moved = true;
      clearTimeout(s.timer);
    }
  };
  const onPointerUp = () => {
    const s = g.current;
    if (!s) return;
    clearTimeout(s.timer);
    if (!s.moved && !s.long && open) bridge.PORT_ADD(nodeId, portId, dir, side);
    g.current = null;
  };
  const cls = ["nodrag"];
  if (open) cls.push("fd-port-open", "nopan");
  if (selected) cls.push("fd-port-sel");
  return (
    <Handle
      type={dir}
      position={POS[side]}
      id={portId}
      style={style}
      title={open ? "tap: add node · long-press: move side" : "long-press: move side"}
      className={cls.join(" ")}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    />
  );
}

// Set of this node's handle ids that already have an edge — used to show the
// "+ add connected node" affordance only on still-empty ports. Returns a
// stable string key so the selector re-runs cheaply.
function useConnectedPorts(nodeId) {
  const key = useStore((s) => {
    let k = "";
    for (const e of s.edges) {
      if (e.source === nodeId && e.sourceHandle) k += "|" + e.sourceHandle;
      if (e.target === nodeId && e.targetHandle) k += "|" + e.targetHandle;
    }
    return k;
  });
  return React.useMemo(() => new Set(key.split("|").filter(Boolean)), [key]);
}

// True once the author has actually RESIZED this node.
//
// NodeProps' width/height cannot answer this: they are the MEASURED size, so
// they are a number for every node the moment the canvas measures it. The
// explicit dimensions live on the node itself — the same width/height the
// editor serializes — and only a resize ever writes them.
function useIsResized(id) {
  return useStore((s) => {
    const n = s.nodeLookup.get(id);
    return n ? n.width != null || n.height != null : false;
  });
}

// double-click a node/group name to rename it inline.
// `editable` comes from the view config: false renders the same span with no
// double-click handler and no hint, which is how the run view stays read-only
// without a second component.
function EditableTitle({ id, value, cls, editable = true, empty = "—" }) {
  const bridge = useBridge();
  const [ed, setEd] = React.useState(false);
  const [v, setV] = React.useState(value);
  React.useEffect(() => {
    setV(value);
  }, [value]);
  if (ed)
    return (
      <input
        className="fd-ndttl-in nodrag"
        value={v}
        autoFocus
        aria-label={"title of " + id}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => {
          setEd(false);
          bridge.RENAME(id, v);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            e.currentTarget.blur();
          }
          if (e.key === "Escape") {
            setV(value);
            setEd(false);
          }
        }}
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
      />
    );
  if (!editable) return <span className={cls || "fd-ndttl"}>{value || empty}</span>;
  return (
    <span
      className={cls || "fd-ndttl"}
      title="double-click to rename"
      onDoubleClick={(e) => {
        e.stopPropagation();
        setEd(true);
      }}
    >
      {value || empty}
    </span>
  );
}

// double-click a node/group chip to edit it inline. The chip is /flow's free
// tag and /workflows' ROLE — and a role is exactly the field /workflows must
// not let you type, because the compiler refuses one outside the live
// catalogue. That is a config decision ("tag" absent from `editable`), not a
// branch in here.
function EditableChip({ id, value, editable = true }) {
  const bridge = useBridge();
  const [ed, setEd] = React.useState(false);
  const [v, setV] = React.useState(value);
  React.useEffect(() => {
    setV(value);
  }, [value]);
  if (ed)
    return (
      <input
        className="fd-chip-in nodrag"
        value={v}
        autoFocus
        aria-label={"tag of " + id}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => {
          setEd(false);
          bridge.SETTAG(id, v);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") {
            setV(value);
            setEd(false);
          }
        }}
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
      />
    );
  if (!editable) return <span className="fd-chiptext">{value}</span>;
  return (
    <span
      className="fd-chiptext"
      title="double-click to edit tag"
      onDoubleClick={(e) => {
        e.stopPropagation();
        setEd(true);
      }}
    >
      {value}
    </span>
  );
}

// The body line under the title — /flow's `sub`, /workflows' `prompt`.
//
// Multi-line, because a prompt is prose: plain Enter is a newline and belongs
// to the text, ⌘/Ctrl+Enter commits, blur commits, Escape reverts. Single
// commit path (keys blur, blur writes) so a keypress and a click-away can never
// both fire a write.
function EditableSub({ id, value, cls, editable = true, rows = 4, clamp = null, empty = "" }) {
  const bridge = useBridge();
  const [ed, setEd] = React.useState(false);
  const [v, setV] = React.useState(value);
  React.useEffect(() => {
    setV(value);
  }, [value]);
  if (ed)
    return (
      <textarea
        className="fd-ndsub-in nodrag"
        rows={rows}
        value={v}
        autoFocus
        aria-label={"sub of " + id}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => {
          setEd(false);
          if (v !== value) bridge.SETSUB(id, v);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setV(value);
            setEd(false);
            return;
          }
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            e.currentTarget.blur();
          }
        }}
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
      />
    );
  // Clamp until the author resizes: -webkit-line-clamp is the only thing that
  // truncates WRAPPED text to N lines and still shows an ellipsis.
  const clampStyle = clamp
    ? { display: "-webkit-box", WebkitLineClamp: clamp, WebkitBoxOrient: "vertical", overflow: "hidden" }
    : null;
  const shown = value || empty;
  const emptyCls = value ? "" : " fd-ndsub-empty";
  if (!editable) return <div className={cls + emptyCls} style={clampStyle}>{shown}</div>;
  return (
    <div
      className={cls + emptyCls}
      style={clampStyle}
      title="double-click to edit"
      onDoubleClick={(e) => {
        e.stopPropagation();
        setEd(true);
      }}
    >
      {shown}
    </div>
  );
}

// straight-segment polyline through an ordered point list — the "manual" edge
// style routes along orthogonal segments the user can drag (FigJam elbow feel).
function manualPath(pts) {
  if (pts.length < 2) return "";
  return "M" + pts.map((p) => `${p.x},${p.y}`).join(" L");
}
// auto orthogonal route between two endpoints (used until the user edits it).
function defaultElbow(S, T) {
  const dx = T.x - S.x,
    dy = T.y - S.y;
  if (Math.abs(dx) < 1 || Math.abs(dy) < 1) return [S, T]; // already aligned → straight
  if (Math.abs(dx) >= Math.abs(dy)) {
    const mx = Math.round((S.x + T.x) / 2);
    return [S, { x: mx, y: S.y }, { x: mx, y: T.y }, T]; // H–V–H
  }
  const my = Math.round((S.y + T.y) / 2);
  return [S, { x: S.x, y: my }, { x: T.x, y: my }, T]; // V–H–V
}
// drag plan for moving segment i (keeps it orthogonal). Endpoints are fixed, so
// dragging a segment touching one inserts an extra bend ("adds a portion").
function planSegDrag(cur, i, orient) {
  const n = cur.length;
  const leftFixed = i === 0;
  const rightFixed = i + 1 === n - 1;
  const c0 = orient === "h" ? cur[i].y : cur[i].x;
  const mk = (base) => (orient === "h" ? { x: base.x, y: c0 } : { x: c0, y: base.y });
  if (!leftFixed && !rightFixed) return { working: cur.slice(), ctrl: [i, i + 1] };
  if (leftFixed && rightFixed) return { working: [cur[0], mk(cur[0]), mk(cur[1]), cur[1]], ctrl: [1, 2] };
  if (leftFixed) return { working: [cur[0], mk(cur[0]), ...cur.slice(1)], ctrl: [1, 2] };
  return { working: [...cur.slice(0, n - 1), mk(cur[n - 1]), cur[n - 1]], ctrl: [i, n - 1] };
}
// drop duplicate / collinear vertices so repeated drags don't accumulate cruft.
function simplifyOrtho(v) {
  const out = [v[0]];
  for (let k = 1; k < v.length - 1; k++) {
    const a = out[out.length - 1],
      b = v[k],
      c = v[k + 1];
    if (Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5) continue; // duplicate
    const colH = Math.abs(a.y - b.y) < 0.5 && Math.abs(b.y - c.y) < 0.5;
    const colV = Math.abs(a.x - b.x) < 0.5 && Math.abs(b.x - c.x) < 0.5;
    if (colH || colV) continue; // collinear → b is redundant
    out.push(b);
  }
  out.push(v[v.length - 1]);
  return out;
}

// custom edge: path-style switch (data.pstyle) + editable chip label. The
// "manual" style is an orthogonal elbow whose segments are draggable
// (data.waypoints), with handles shown only while that edge is selected.
export function LabeledEdge({ id, source, target, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style, markerEnd, data, selected }) {
  const bridge = useBridge();
  const ps = (data && data.pstyle) || "default";
  const manual = ps === "manual";
  const wps = (manual && data && data.waypoints) || [];
  const wpsRef = React.useRef(wps);
  wpsRef.current = wps;
  const rf = useReactFlow();
  const col = (style && style.stroke) || "var(--color-accent)";
  // gradient from source node colour → target node colour, oriented along the
  // real endpoints (userSpaceOnUse). Falls back to accent when a node has no
  // explicit colour. gid is sanitised for use in a url(#…) stroke reference.
  const sCol = (rf.getNode(source)?.data?.color) || "var(--color-accent)";
  const tCol = (rf.getNode(target)?.data?.color) || "var(--color-accent)";
  const gid = "fdg-" + String(id).replace(/[^a-zA-Z0-9_-]/g, "");
  const mid = gid + "-arrow";
  const edgeStyle = { ...style, stroke: `url(#${gid})` };
  // pull the target endpoint back along the handle's outward normal so the
  // arrowhead stops ~4px short of the target connector circle. targetX/Y is the
  // connector CENTRE, so the offset is measured from there — no radius term
  // (adding it double-counted and left a ~2× gap).
  const TNORM = { left: [-1, 0], right: [1, 0], top: [0, -1], bottom: [0, 1] };
  const tn = TNORM[targetPosition] || [0, 0];
  const ARROW_GAP = 5.5;
  const tx = targetX + tn[0] * ARROW_GAP;
  const ty = targetY + tn[1] * ARROW_GAP;
  const S = { x: sourceX, y: sourceY },
    T = { x: tx, y: ty };

  let path, lx, ly;
  let segs = [];
  if (manual) {
    const V = wps.length ? [S, ...wps, T] : defaultElbow(S, T);
    path = manualPath(V);
    const mid = V[Math.floor(V.length / 2)];
    lx = mid.x;
    ly = mid.y;
    for (let i = 0; i < V.length - 1; i++) {
      const a = V[i],
        b = V[i + 1];
      const orient = Math.abs(a.y - b.y) <= Math.abs(a.x - b.x) ? "h" : "v";
      segs.push({ i, orient, x1: a.x, y1: a.y, x2: b.x, y2: b.y, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 });
    }
  } else {
    const fn = PATHFN[ps] || getBezierPath;
    const args = { sourceX, sourceY, targetX: tx, targetY: ty, sourcePosition, targetPosition };
    if (ps === "step") args.borderRadius = 0;
    const r = fn(args);
    path = r[0];
    lx = r[1];
    ly = r[2];
  }

  const editing = data && data.__editing,
    label = data && data.label;
  // segment handles are a manual-style-only affordance
  const showHandles = manual && (selected || (data && data.__sel));

  // drag a whole segment, keeping it orthogonal; endpoints stay put.
  const dragSeg = (i, orient) => (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    const cur = wpsRef.current.length ? [S, ...wpsRef.current, T] : defaultElbow(S, T);
    const { working, ctrl } = planSegDrag(cur, i, orient);
    const move = (e) => {
      const p = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const c = orient === "h" ? p.y : p.x;
      const nv = working.map((v, idx) => (ctrl.includes(idx) ? (orient === "h" ? { x: v.x, y: c } : { x: c, y: v.y }) : v));
      const newWps = simplifyOrtho(nv)
        .slice(1, -1)
        .map((v) => ({ x: Math.round(v.x), y: Math.round(v.y) }));
      wpsRef.current = newWps;
      bridge.EDGE_WAYPOINTS(id, newWps);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  // double-click a segment → drop manual routing, back to the auto elbow
  const resetRoute = (ev) => {
    ev.stopPropagation();
    bridge.EDGE_WAYPOINTS(id, []);
  };

  return (
    <>
      <defs>
        <linearGradient id={gid} gradientUnits="userSpaceOnUse" x1={sourceX} y1={sourceY} x2={tx} y2={ty}>
          <stop offset="0%" style={{ stopColor: sCol }} />
          <stop offset="100%" style={{ stopColor: tCol }} />
        </linearGradient>
        {/* arrowhead recoloured to the TARGET node colour (the end it sits on);
            geometry mirrors RF's own ArrowClosed marker. */}
        <marker
          id={mid}
          className="react-flow__arrowhead"
          markerWidth="12.5"
          markerHeight="12.5"
          viewBox="-10 -10 20 20"
          markerUnits="strokeWidth"
          orient="auto-start-reverse"
          refX="0"
          refY="0"
        >
          <polyline style={{ stroke: tCol, fill: tCol, strokeWidth: 1 }} strokeLinecap="round" strokeLinejoin="round" points="-5,-4 0,0 -5,4 -5,-4" />
        </marker>
      </defs>
      <BaseEdge id={id} path={path} style={edgeStyle} markerEnd={`url(#${mid})`} />
      {showHandles &&
        segs.map((s) => (
          <line
            key={`seg-${s.i}`}
            x1={s.x1}
            y1={s.y1}
            x2={s.x2}
            y2={s.y2}
            stroke="transparent"
            strokeWidth={14}
            className="nopan"
            style={{ pointerEvents: "stroke", cursor: s.orient === "h" ? "ns-resize" : "ew-resize" }}
            onPointerDown={dragSeg(s.i, s.orient)}
            onDoubleClick={resetRoute}
          />
        ))}
      {(editing || label || showHandles) && (
        <EdgeLabelRenderer>
          {(editing || label) && (
            <div className="fd-edgelbl-wrap" style={{ transform: `translate(-50%,-50%) translate(${lx}px,${ly}px)` }}>
              {editing ? (
                <input
                  className="fd-edgelbl-in nodrag nopan"
                  autoFocus
                  defaultValue={label || ""}
                  onClick={(e) => e.stopPropagation()}
                  onMouseDown={(e) => e.stopPropagation()}
                  onBlur={(e) => bridge.EDGE_SET(id, e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") e.currentTarget.blur();
                    if (e.key === "Escape") bridge.EDGE_SET(id, label || "");
                  }}
                />
              ) : (
                <div
                  className="fd-edgelbl nodrag"
                  title="double-click to edit"
                  onDoubleClick={(e) => {
                    e.stopPropagation();
                    bridge.EDGE_EDIT(id);
                  }}
                >
                  {label}
                </div>
              )}
            </div>
          )}
          {showHandles &&
            segs.map((s) => (
              <div
                key={`h-${s.i}`}
                className="fd-wp nodrag nopan"
                title="drag to move segment · double-click to reset"
                style={{
                  transform: `translate(-50%,-50%) translate(${s.mx}px,${s.my}px)`,
                  borderColor: col,
                  cursor: s.orient === "h" ? "ns-resize" : "ew-resize",
                }}
                onPointerDown={dragSeg(s.i, s.orient)}
                onDoubleClick={resetRoute}
              />
            ))}
        </EdgeLabelRenderer>
      )}
    </>
  );
}

// render handles + labels distributed across the 4 sides.
// `col` = the node's effective colour (accent, or its data.color override).
//
// Handles on a side are spaced at (i+1)/(n+1) by their index in LIST ORDER —
// left/right read top→bottom, top/bottom read left→right, and direction never
// reorders them. Reordering data.ports[] is how an author aims a connector.
function renderPorts(ports, orient, hide, col, nodeId, connSet) {
  const byside = { left: [], right: [], top: [], bottom: [] };
  (ports || []).forEach((p) => byside[portSide(p, orient)].push(p));
  const out = [];
  SIDES.forEach((side) => {
    const arr = byside[side];
    arr.forEach((p, i) => {
      const f = (i + 1) / (arr.length + 1);
      const hs = { background: col || "var(--color-accent)" };
      if (side === "left" || side === "right") hs.top = f * 100 + "%";
      else hs.left = f * 100 + "%";
      const open = connSet && nodeId && !connSet.has(p.id);
      out.push(
        <PortHandle
          key={"h_" + side + "_" + p.id}
          nodeId={nodeId}
          portId={p.id}
          dir={portDir(p) === "out" ? "source" : "target"}
          side={side}
          style={hs}
          label={p.label}
          open={open}
        />,
      );
      if (!p.label) return;
      const ls = {};
      if (side === "left") {
        ls.left = 14;
        ls.top = f * 100 + "%";
        ls.transform = "translateY(-50%)";
      } else if (side === "right") {
        ls.right = 14;
        ls.top = f * 100 + "%";
        ls.transform = "translateY(-50%)";
        ls.textAlign = "right";
      } else if (side === "top") {
        ls.top = 11;
        ls.left = f * 100 + "%";
        ls.transform = "translateX(-50%)";
      } else {
        ls.bottom = 11;
        ls.left = f * 100 + "%";
        ls.transform = "translateX(-50%)";
      }
      out.push(
        <span key={"l_" + side + "_" + p.id} className={"fd-plab" + (hide ? " ghide" : "")} style={ls} title={p.label}>
          {p.label}
        </span>,
      );
    });
  });
  return out;
}

// classic left/right ports (mode 'lr') — inputs left / outputs right.
// Two columns by direction, each still in list order; `side` and `orient` do
// not apply in this mode, which is the whole point of the classic layout.
function renderLR(ports, hide, col, nodeId, connSet) {
  const lc = "fd-plabel" + (hide ? " ghide" : "");
  const bg = col || "var(--color-accent)";
  const isOpen = (p) => connSet && nodeId && !connSet.has(p.id);
  const all = ports || [];
  return (
    <div className="fd-body">
      <div className="fd-col in">
        {all
          .filter((p) => portDir(p) === "in")
          .map((p) => (
            <div className="fd-prow" key={p.id}>
              <PortHandle
                nodeId={nodeId}
                portId={p.id}
                dir="target"
                side="left"
                style={{ background: bg }}
                label={p.label}
                open={isOpen(p)}
              />
              {p.label && (
                <span className={lc} title={p.label}>
                  {p.label}
                </span>
              )}
            </div>
          ))}
      </div>
      <div className="fd-col out">
        {all
          .filter((p) => portDir(p) === "out")
          .map((p) => (
            <div className="fd-prow" key={p.id}>
              {p.label && (
                <span className={lc} title={p.label}>
                  {p.label}
                </span>
              )}
              <PortHandle
                nodeId={nodeId}
                portId={p.id}
                dir="source"
                side="right"
                style={{ background: bg }}
                label={p.label}
                open={isOpen(p)}
              />
            </div>
          ))}
      </div>
    </div>
  );
}

// Port policy "stack-tb" / "stack-lr": ONE input and ONE output, at fixed
// sides, not derived from data.ports.
//
// This is the shape a workflow reads in — a vertical stack — and it also states
// the graph's structure without a label: a node with nothing above it IS the
// start, one with nothing below it IS the end. The unused port is FADED, never
// unmounted: a handle that is not in the DOM is not a drop target, so removing
// it would leave nothing to drag onto and the start could never stop being the
// start.
function StackPorts({ vertical, col }) {
  const incoming = useNodeConnections({ handleType: "target" });
  const outgoing = useNodeConnections({ handleType: "source" });
  const bg = col || "var(--color-accent)";
  return (
    <>
      <Handle
        type="target"
        position={vertical ? Position.Top : Position.Left}
        style={{ background: bg, opacity: incoming.length ? 1 : 0.35 }}
        title={incoming.length ? undefined : "start of the workflow"}
      />
      <Handle
        type="source"
        position={vertical ? Position.Bottom : Position.Right}
        style={{ background: bg, opacity: outgoing.length ? 1 : 0.35 }}
        title={outgoing.length ? undefined : "end of the workflow"}
      />
    </>
  );
}

// Render whichever port policy the view asked for. One call site in the node.
function renderPortPolicy(cfg, data, id, portCol, connSet, mode) {
  if (cfg.ports === "none") return null;
  if (cfg.ports === "stack-tb") return <StackPorts vertical col={portCol} />;
  if (cfg.ports === "stack-lr") return <StackPorts vertical={false} col={portCol} />;
  // "data" — /flow's own: any side, typed, tap-to-add, long-press-to-move.
  const ports = portsOf(data);
  if (mode === "lr") return renderLR(ports, false, portCol, id, connSet);
  return renderPorts(ports, data.orient, false, portCol, id, connSet);
}

// The badge row under the body. Purely declarative: the view returns a list,
// the node renders it. /workflows puts risk/tier/criteria/fan-out/issues here;
// the run view will put live state here. Neither needs a code path in the node.
function BadgeRow({ badges }) {
  if (!badges || !badges.length) return null;
  return (
    <div className="fd-badges">
      {badges.map((b) => (
        <span key={b.key} className={"fd-badge " + (b.className || "")} title={b.title} style={b.style}>
          {b.text}
        </span>
      ))}
    </div>
  );
}

// collapsed group = black box: a single merged input (left) + merged output
// (right). Every external edge funnels here; expanding reveals the real wiring.
function mergedHandles(col, hasIn = true, hasOut = true) {
  const bg = col || "var(--color-accent)";
  const out = [];
  if (hasIn) out.push(<Handle key="mgin" type="target" position={Position.Left} id={MGIN} style={{ background: bg }} title="group input" />);
  if (hasOut) out.push(<Handle key="mgout" type="source" position={Position.Right} id={MGOUT} style={{ background: bg }} title="group output" />);
  return out;
}

export function FlowNode({ id, data, selected, mode }) {
  // The view's config: what this node may edit, where its colour comes from,
  // which slots map to which data keys, its ports, its badges. Defaults to
  // /flow's, so a node rendered without a provider behaves exactly as before.
  const cfg = useNodeView();
  const col = cfg.colorOf(data) || ACCENT;
  const chipVal = slot(cfg, data, "chip");
  const titleVal = slot(cfg, data, "title");
  const subVal = slot(cfg, data, "sub");
  const edTitle = canEdit(cfg, "title");
  const edChip = canEdit(cfg, "tag");
  const edSub = canEdit(cfg, "sub");
  const badges = cfg.badges ? cfg.badges(data) : null;
  const headRight = cfg.headerRight ? cfg.headerRight(data) : null;
  // A node the author has explicitly resized carries width/height; until then a
  // view may cap it so one long line cannot stretch it across the canvas.
  const sized = useIsResized(id);
  const capStyle = cfg.maxW && !sized ? { maxWidth: cfg.maxW } : null;
  // A resized node shows as much of its body as it now has room for — otherwise
  // dragging a node taller would change nothing visible and read as broken.
  const subClamp = sized ? null : cfg.subClamp;
  // An empty slot may be hidden until the node is selected, so an unnamed node
  // stays clean on a busy canvas but is never un-editable.
  const showEmpty = (spec) => !!spec && (!spec.onlyWhenSelected || selected);
  const titleEmpty = showEmpty(cfg.emptyTitle) ? cfg.emptyTitle.text : "";
  const subEmpty = showEmpty(cfg.emptySub) ? cfg.emptySub.text : "";
  const showSub = !data.hideSub && (subVal || subEmpty);
  // ports without an edge get a clickable "+" (spawn a connected node).
  const connSet = useConnectedPorts(id);
  // style variant — applies to the standard box + lr cards (title/mdnote opt out).
  const { cls: vcls, card: cardStyle, chip: chipStyle, port: portCol } = variantStyles(data.style || "tinted", col, cfg.colorOf(data));

  // Standalone heading: large bold text, no box chrome, no ports.
  // Honors data.color (defaults to foreground-bright for headings).
  if (data.cat === "title") {
    return (
      <div className={"fd-title" + (selected ? " selected" : "")} style={{ color: data.color || undefined }}>
        {!data.hideTitle && <EditableTitle id={id} value={data.title} cls="fd-title-txt" editable={edTitle} />}
        {!data.hideSub && data.sub && <div className="fd-title-sub">{data.sub}</div>}
      </div>
    );
  }

  // Prose note rendered through the shared (XSS-safe) Markdown pipeline.
  // Body comes from data.body, falling back to data.sub. No ports.
  if (data.cat === "mdnote") {
    const body = (data.body ?? data.sub) || "";
    return (
      <div className={"fd-mdnote" + (selected ? " selected" : "")} style={{ borderColor: col }}>
        <Resizer color={col} minW={240} minH={120} visible={selected} />
        <div className="fd-mdnote-hd" style={{ background: col }}>
          {!data.hideChip && (
            <span className="fd-ndtag" style={{ background: "transparent", color: "#06060a" }}>
              <EditableChip id={id} value={data.tag} editable={edChip} />
            </span>
          )}
          {!data.hideTitle && <EditableTitle id={id} value={data.title} cls="fd-ttl-hd" editable={edTitle} />}
        </div>
        {!data.hideSub && (
          <div
            className="fd-mdnote-body nodrag"
            style={{ display: "flex", flexDirection: "column", ...alignStyleText(data) }}
          >
            <MarkdownContent variant="compact">{body}</MarkdownContent>
          </div>
        )}
      </div>
    );
  }

  if (mode === "lr")
    return (
      <div className={"fd-nd lr" + vcls + (selected ? " selected" : "")} style={{ ...cardStyle, ...capStyle }} data-testid={"fd-node-" + id}>
        <Resizer color={col} minW={Math.max(cfg.minW, 244)} minH={cfg.minH} visible={selected} />
        <div className="fd-hd" style={{ background: col }}>
          {!data.hideChip && (
            <span className="fd-tag">
              <EditableChip id={id} value={chipVal} editable={edChip} />
            </span>
          )}
          {!data.hideTitle && (
            <EditableTitle id={id} value={titleVal} cls="fd-ttl-hd" editable={edTitle} empty={titleEmpty} />
          )}
          {headRight}
        </div>
        {showSub && (
          <EditableSub id={id} value={subVal} cls="fd-nsub" editable={edSub} clamp={subClamp} empty={subEmpty} />
        )}
        <BadgeRow badges={badges} />
        {renderPortPolicy(cfg, data, id, portCol, connSet, mode)}
      </div>
    );
  return (
    <div className={"fd-nd" + vcls + (selected ? " selected" : "")} style={{ ...cardStyle, ...capStyle }} data-testid={"fd-node-" + id}>
      <Resizer color={col} minW={cfg.minW} minH={cfg.minH} visible={selected} />
      <div className="fd-ndinner" style={alignStyle(data)}>
        {/* The chip alone keeps /flow's exact markup. A view that also wants a
            right-hand slot (e.g. /workflows' phase) gets a flex row around the
            pair — added only when that slot exists, so /flow's DOM is
            untouched and its CSS keeps matching. */}
        {headRight ? (
          <span className="fd-ndrow">
            {!data.hideChip && chipVal !== null && (
              <span className="fd-ndtag" style={chipStyle}>
                <EditableChip id={id} value={chipVal} editable={edChip} />
              </span>
            )}
            {headRight}
          </span>
        ) : (
          !data.hideChip &&
          chipVal !== null && (
            <span className="fd-ndtag" style={chipStyle}>
              <EditableChip id={id} value={chipVal} editable={edChip} />
            </span>
          )
        )}
        {!data.hideTitle && <EditableTitle id={id} value={titleVal} editable={edTitle} empty={titleEmpty} />}
        {showSub && (
          <EditableSub id={id} value={subVal} cls="fd-ndsub" editable={edSub} clamp={subClamp} empty={subEmpty} />
        )}
        <BadgeRow badges={badges} />
      </div>
      {renderPortPolicy(cfg, data, id, portCol, connSet, mode)}
    </div>
  );
}

export function GroupNode({ id, data, selected, mode }) {
  const bridge = useBridge();
  const cfg = useNodeView();
  const edTitle = canEdit(cfg, "title");
  const edChip = canEdit(cfg, "tag");
  const n = (data.members || []).length,
    col = cfg.colorOf(data) || ACCENT;
  // merged handles only exist when a child actually exposes that port type:
  // no child input → no group input (and symmetrically for outputs).
  const hasIn = (data.members || []).some((m) => portsOf(m.data).some((p) => portDir(p) === "in"));
  const hasOut = (data.members || []).some((m) => portsOf(m.data).some((p) => portDir(p) === "out"));
  if (data.collapsed === false)
    return (
      <div className={"fd-grpbox" + (selected ? " selected" : "")} style={{ borderColor: col }}>
        <Resizer color={col} minW={320} minH={190} visible={selected} />
        <div className="fd-grpbar" style={{ background: col }}>
          {!data.hideChip && (
            <span className="fd-tag">
              <EditableChip id={id} value={data.tag || "GRP"} editable={edChip} />
            </span>
          )}
          {!data.hideTitle && <EditableTitle id={id} value={data.title} cls="fd-ttl-hd" editable={edTitle} />}
          <span
            className="fd-grpclose"
            title="collapse group"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              bridge.COLLAPSE(id);
            }}
            onDoubleClick={(e) => e.stopPropagation()}
          >
            ✕
          </span>
        </div>
        {mergedHandles(col, hasIn, hasOut)}
      </div>
    );
  if (mode === "lr")
    return (
      <div className={"fd-nd lr grp" + (selected ? " selected" : "")} style={{ borderColor: col }}>
        <Resizer color={col} minW={244} minH={96} visible={selected} />
        <div className="fd-hd" style={{ background: col }}>
          {!data.hideChip && (
            <span className="fd-tag">
              <EditableChip id={id} value={data.tag || "GRP"} editable={edChip} />
            </span>
          )}
          {!data.hideTitle && <EditableTitle id={id} value={data.title} cls="fd-ttl-hd" editable={edTitle} />}
          <span style={{ marginLeft: "auto", color: "#06060a", fontSize: 10, fontWeight: 700 }}>{n} ⤢</span>
        </div>
        {!data.hideSub && data.sub && <div className="fd-nsub">{data.sub}</div>}
        {mergedHandles(col, hasIn, hasOut)}
      </div>
    );
  return (
    <div className={"fd-nd grp" + (selected ? " selected" : "")} style={{ borderColor: col }}>
      <Resizer color={col} minW={230} minH={96} visible={selected} />
      <div className="fd-ndinner" style={alignStyle(data)}>
        {!data.hideChip && (
          <span className="fd-ndtag" style={{ background: col }}>
            <EditableChip id={id} value={data.tag || "GRP"} editable={edChip} /> <span className="fd-cnt">{n} ⤢</span>
          </span>
        )}
        {!data.hideTitle && <EditableTitle id={id} value={data.title} editable={edTitle} />}
        {!data.hideSub && data.sub && <div className="fd-ndsub">{data.sub}</div>}
      </div>
      {mergedHandles(col, hasIn, hasOut)}
    </div>
  );
}
