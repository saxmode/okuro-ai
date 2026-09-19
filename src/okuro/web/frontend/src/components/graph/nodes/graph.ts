// @ts-nocheck
// <!-- AGENT_HEADER
// role: code
// purpose: okuro-flow — pure graph constants + helpers (ported verbatim from
//   tools/flow-designer). Node templates, port colours, dagre layout, group
//   assembly. Dynamic JS port; type-checking intentionally skipped.
// AGENT_HEADER_END -->
import * as dagre from "@dagrejs/dagre";
import {
  Position,
  getBezierPath,
  getSimpleBezierPath,
  getSmoothStepPath,
  getStraightPath,
} from "@xyflow/react";

// generic port types -> colour (CSS vars defined in graph.css)
export const TC = {
  flow: "var(--fd-t-flow)",
  data: "var(--fd-t-data)",
  control: "var(--fd-t-control)",
  value: "var(--fd-t-value)",
  signal: "var(--fd-t-signal)",
  asset: "var(--fd-t-asset)",
};
export const TYPES = Object.keys(TC);
// node categories -> header colour. Values reference the canonical
// --fd-cat-* CSS vars (graph/graph.css) so the canvas, mermaid export, and
// stylesheet share one source of truth; hex kept as fallback for any context
// that renders before the stylesheet loads.
export const CAT = {
  process: "var(--fd-cat-process, #4aa3ff)",
  input: "var(--fd-cat-input, #46d17a)",
  output: "var(--fd-cat-output, #ffc24d)",
  decision: "var(--fd-cat-decision, #ff6fae)",
  note: "var(--fd-cat-note, #9aa0aa)",
  title: "var(--fd-cat-title, #e8eaf0)",
  mdnote: "var(--fd-cat-mdnote, #9aa0aa)",
  group: "var(--fd-cat-group, #b07cff)",
};

// 16 calculated swatches: evenly-spaced hues tuned for the dark canvas
function hsl2hex(h, s, l) {
  s /= 100;
  l /= 100;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => {
    const k = (n + h / 30) % 12;
    const c = l - a * Math.max(-1, Math.min(k - 3, Math.min(9 - k, 1)));
    return Math.round(255 * c).toString(16).padStart(2, "0");
  };
  return "#" + f(0) + f(8) + f(4);
}
export const PALETTE = Array.from({ length: 16 }, (_, i) => hsl2hex(i * 22.5, 70, 63));

export const POS = {
  left: Position.Left,
  right: Position.Right,
  top: Position.Top,
  bottom: Position.Bottom,
};
export const SIDES = ["left", "right", "top", "bottom"];

// ---- ports: ONE ordered list per node ----
//
// A node keeps `data.ports[]` — {id, label?, t?, dir:"in"|"out", side?}. The
// LIST ORDER is the author's placement tool: handles on a side are laid out in
// list order (left/right top→bottom, top/bottom left→right), never mirrored and
// never grouped by direction. That is what lets a node with an in and an out on
// the same side face its neighbour without crossing the edge vectors.
//
// `side` unset resolves from the port's direction and the node's `orient`:
// "h" (default) reads left→right, "v" is the same node turned 90°.
export const ORIENTS = [
  ["h", "Horizontal (in ← left · out → right)"],
  ["v", "Vertical (in ↑ top · out ↓ bottom)"],
];
const DEF_SIDE = { h: { in: "left", out: "right" }, v: { in: "top", out: "bottom" } };
export const portDir = (p) => (p && p.dir === "out" ? "out" : "in");
export const portSide = (p, orient) => {
  const s = p && p.side;
  return s && POS[s] ? s : DEF_SIDE[orient === "v" ? "v" : "h"][portDir(p)];
};
// Read a node's ports. Accepts the legacy ins[]/outs[] shape too, so a node
// arriving mid-flight (SSE draw, an agent write, the backend draw pipeline)
// renders correctly even before load-time migration has touched it.
export function portsOf(data) {
  if (!data) return [];
  if (Array.isArray(data.ports)) return data.ports;
  if (!data.ins && !data.outs) return [];
  return [
    ...(data.ins || []).map((p) => ({ ...p, dir: "in" })),
    ...(data.outs || []).map((p) => ({ ...p, dir: "out" })),
  ];
}
// Legacy data.ins/outs -> data.ports, ins first then outs (the order the old
// renderer drew them in, so a migrated flow keeps its handle positions).
export function migrateNode(n) {
  const d = n && n.data;
  if (!d) return n;
  let data = d;
  if (d.ins || d.outs) {
    const { ins, outs, ...rest } = d;
    data = { ...rest, ports: portsOf(d) };
  }
  if (Array.isArray(data.members)) {
    const mem = data.members.map(migrateNode);
    if (mem.some((m, i) => m !== data.members[i])) data = { ...data, members: mem };
  }
  return data === n.data ? n : { ...n, data };
}
export const migrateNodePorts = (nodes) => (nodes || []).map(migrateNode);

// ---- load-time reconciliation: an edge must have a handle to attach to ----
//
// React Flow REFUSES an edge whose handle the node does not declare, logs a
// console WARNING (not an error — which is why an error-only console filter
// reports zero) and simply DOES NOT DRAW the connection. Nothing tells the
// user. Audited across the whole store on 2026-09-15, 106 flows / 1,306 edges:
//
//   33  the handle is absent from the node's ports[]
//    7  the handle exists but declares the wrong `dir`
//   24  the endpoint NODE does not exist at all
//
// The worst case is not the count. ONE stored flow holds 35 edges and
// rendered 0 OF 35, because every one of its nodes carries `ports: null`
// while every edge names `in`/`out` — an agent wrote the document through the
// MCP surface without declaring ports, and the whole graph came up as
// disconnected boxes. (Flow ids are the user's data; the pass report names
// them, this file does not.)
//
// `flow-designer.tsx:1302-1305` states that `applyGraph` never sees a legacy
// node. The measurement says it sees 15 flows of them, and `migrateNode` only
// covers the ins/outs -> ports[] rename — it cannot invent a handle that was
// never declared in either spelling.
//
// SO THE FIX IS HERE, ON LOAD, AT THE CLASS. What an edge REFERENCES is itself
// a declaration: `sourceHandle` on an edge means "this node has an out port
// with this id". Synthesising it needs no per-node judgement — the direction
// is fixed by which end of the edge names it — whereas a data migration would
// have to guess whether `h_stop` declaring only `out` means the edge is wrong
// or the node is. On-load also covers the next hand-written or replayed
// document, which a one-time migration would not.
//
// A DANGLING edge (its endpoint node is gone) is NOT repaired: there is
// nothing to attach to. Those are counted and returned so the editor can say
// so out loud instead of dropping them in silence.

/** Side to place a synthesized handle on, matching the renderer's defaults. */
function synthSide(dir) {
  return dir === "out" ? "right" : "left";
}

/**
 * Categories that render NO handle at all, whatever their ports[] says.
 *
 * `title` is a section label, not a step: `nodes.tsx:861` returns a bare div
 * before any port policy runs, and `mdnote` two branches down does the same.
 * So an edge naming one of these can never be drawn, and giving them handles
 * to satisfy an edge would change what a title card IS.
 *
 * It is a DATA problem, and a real one: one stored flow is SEVEN
 * title cards with 35 edges between them, so it renders as seven labels and
 * zero connections. Counted here so the editor can say so.
 */
export const PORTLESS_CATS = new Set(["title", "mdnote"]);

/**
 * Give every edge a handle to attach to.
 *
 * Returns the reconciled nodes plus the edges that still cannot be drawn
 * because an endpoint node does not exist.
 */
export function reconcileEdgePorts(nodes, edges) {
  const list = nodes || [];
  const byId = new Map(list.map((n) => [n.id, n]));
  // id -> Map(handleId -> "in"|"out"), the ports we must end up declaring
  const need = new Map();
  const dangling = [];
  const portless = [];

  for (const e of edges || []) {
    let lost = false;
    let noPorts = false;
    for (const [nid, handle, dir] of [
      [e.source, e.sourceHandle, "out"],
      [e.target, e.targetHandle, "in"],
    ]) {
      const node = byId.get(nid);
      if (!node) {
        lost = true;
        continue;
      }
      if (PORTLESS_CATS.has(node.data && node.data.cat)) {
        noPorts = true;
        continue;
      }
      if (!handle) continue;
      const have = portsOf(node.data).find((x) => x.id === handle);
      if (have && portDir(have) === dir) continue;
      if (!need.has(nid)) need.set(nid, new Map());
      need.get(nid).set(handle, dir);
    }
    if (lost) dangling.push(e);
    else if (noPorts) portless.push(e);
  }

  if (!need.size) return { nodes: list, dangling, portless };

  const out = list.map((n) => {
    const want = need.get(n.id);
    if (!want) return n;
    const ports = [...portsOf(n.data)];
    for (const [id, dir] of want) {
      // ADD the missing direction; never TURN an existing port round.
      //
      // Flipping was the first version and it is measurably wrong: `h_stop`
      // declares port `o` as `out`, one edge TARGETS it and another one
      // SOURCES it, so turning the port round fixed one edge and broke the
      // other. bootstrap-packet-hook-wiring went 36 drawn to 35 — a fix that
      // presents as a fix and costs an edge.
      //
      // A second port with the same id and the opposite direction is safe
      // because the renderer keys a handle on side + id (nodes.tsx:661) and
      // synthSide puts the two on opposite sides, so React Flow ends up with
      // one source handle and one target handle of that id — which is exactly
      // what a port used in both directions means.
      if (ports.some((x) => x.id === id && portDir(x) === dir)) continue;
      ports.push({ id, label: "", t: "flow", side: synthSide(dir), dir });
    }
    return { ...n, data: { ...n.data, ports } };
  });
  return { nodes: out, dangling, portless };
}

export const PATHFN = {
  default: getBezierPath,
  simplebezier: getSimpleBezierPath,
  smoothstep: getSmoothStepPath,
  step: getSmoothStepPath,
  straight: getStraightPath,
  manual: getStraightPath, // base (no waypoints) = straight; waypoints add bends
};

export const EDGE_STYLES = [
  ["default", "Bezier (curved)"],
  ["smoothstep", "Smooth step"],
  ["step", "Step (right-angle)"],
  ["straight", "Straight"],
  ["simplebezier", "Simple bezier"],
  ["manual", "Manual (draggable)"],
];

// node + port constructors
export const N = (id, x, y, cat, tag, title, sub, ports) => ({
  id,
  type: "node",
  position: { x, y },
  data: { cat, tag, title, sub, ports },
});
export const P = (id, label, t, dir) => ({ id, label, t, dir });

// generic add-node templates: [cat, tag, title, sub, ports]
export const TPLN = {
  process: ["process", "PROC", "Process", "", [P("in", "in", "flow", "in"), P("out", "out", "flow", "out")]],
  input: ["input", "IN", "Input", "source", [P("out", "out", "flow", "out")]],
  output: ["output", "OUT", "Output", "sink", [P("in", "in", "flow", "in")]],
  decision: ["decision", "DEC", "Decision", "branch", [P("in", "in", "flow", "in"), P("yes", "yes", "control", "out"), P("no", "no", "control", "out")]],
  note: ["note", "NOTE", "Note", "", []],
  title: ["title", "TITLE", "Section Title", "", []],
  mdnote: ["mdnote", "MD", "Markdown Note", "**Markdown** note — _italic_, lists, `code`, [links](https://okuro).", []],
};

// measured dimension helpers (v12 stores measured dims under node.measured)
const mw = (n, d) => n?.measured?.width ?? n?.width ?? (n?.style && n.style.width) ?? d;
const mh = (n, d) => n?.measured?.height ?? n?.height ?? (n?.style && n.style.height) ?? d;

// ---- shared graph helpers (grouping + drill navigation) ----
export const cleanNode = (n) => {
  const { selected, ...rest } = n;
  return { ...rest, style: { ...(n.style || {}), opacity: undefined } };
};
export const decode = (h) => {
  const r = h.slice(3);
  const i = r.indexOf("__");
  return [r.slice(0, i), r.slice(i + 2)];
};

// A collapsed group is a black box: ONE merged input + ONE merged output handle.
// Every external edge funnels into these; the per-member proxy id (gi_*/go_*) is
// stashed on the edge so expand/ungroup can restore the real member wiring.
export const MGIN = "__gin";
export const MGOUT = "__gout";
// fold an external edge onto a group's merged handles, stashing member proxies.
// inSet = the member ids being absorbed (inner edges are filtered out upstream).
export function foldEdge(e, gid, inSet) {
  let ne = e;
  if (inSet.has(e.source))
    ne = { ...ne, source: gid, sourceHandle: MGOUT, data: { ...(ne.data || {}), _oout: "go_" + e.source + "__" + e.sourceHandle } };
  if (inSet.has(e.target))
    ne = { ...ne, target: gid, targetHandle: MGIN, data: { ...(ne.data || {}), _oin: "gi_" + e.target + "__" + e.targetHandle } };
  return ne;
}
// reverse foldEdge: restore member endpoints from the stashed proxies (or legacy
// gi_/go_ handles). Direct-to-box edges (no proxy) stay anchored on the group.
export function unfoldEdge(e, gid) {
  let ne = e;
  if (e.source === gid) {
    const proxy = (ne.data && ne.data._oout) || (typeof e.sourceHandle === "string" && e.sourceHandle.indexOf("go_") === 0 ? e.sourceHandle : null);
    if (proxy) {
      const [m, h] = decode(proxy);
      const { _oout, ...rest } = ne.data || {};
      ne = { ...ne, source: m, sourceHandle: h, data: rest };
    }
  }
  if (e.target === gid) {
    const proxy = (ne.data && ne.data._oin) || (typeof e.targetHandle === "string" && e.targetHandle.indexOf("gi_") === 0 ? e.targetHandle : null);
    if (proxy) {
      const [m, h] = decode(proxy);
      const { _oin, ...rest } = ne.data || {};
      ne = { ...ne, target: m, targetHandle: h, data: rest };
    }
  }
  return ne;
}
// Back-compat: older flows / agent writes wire external group edges straight to
// per-member proxy handles. Fold them onto the merged handles on load so the
// black-box rendering is consistent and the originals survive for restore.
export function migrateGroupEdges(edges) {
  return (edges || []).map((e) => {
    const so = typeof e.sourceHandle === "string" && e.sourceHandle.indexOf("go_") === 0;
    const to = typeof e.targetHandle === "string" && e.targetHandle.indexOf("gi_") === 0;
    if (!so && !to) return e;
    const data = { ...(e.data || {}) };
    const ne = { ...e };
    if (so) {
      data._oout = e.sourceHandle;
      ne.sourceHandle = MGOUT;
    }
    if (to) {
      data._oin = e.targetHandle;
      ne.targetHandle = MGIN;
    }
    ne.data = data;
    return ne;
  });
}
export function withRel(members) {
  if (!members.length) return members;
  const ax = members.reduce((s, m) => s + m.position.x, 0) / members.length;
  const ay = members.reduce((s, m) => s + m.position.y, 0) / members.length;
  return members.map((m) => ({ ...m, _rel: { x: m.position.x - ax, y: m.position.y - ay } }));
}
// assemble a group's external ports from members + inner edges.
// Returns ONE ordered list, same model as a plain node: the gi_/go_ proxy ids
// and their in/out direction are unchanged, only the container is.
export function assemble(members, inner, refIn, refOut) {
  const consIn = new Set(),
    consOut = new Set();
  inner.forEach((e) => {
    consIn.add(e.target + "__" + e.targetHandle);
    consOut.add(e.source + "__" + e.sourceHandle);
  });
  const ports = [];
  members.forEach((m) => {
    portsOf(m.data).forEach((p) => {
      const isIn = portDir(p) === "in";
      const k = m.id + "__" + p.id;
      const consumed = isIn ? consIn.has(k) : consOut.has(k);
      const referenced = isIn ? refIn.has(k) : refOut.has(k);
      if (!consumed || referenced)
        ports.push({
          id: (isIn ? "gi_" : "go_") + k,
          label: (m.data.title || m.id) + "·" + p.label,
          t: p.t,
          dir: isIn ? "in" : "out",
          m: m.id,
          mh: p.id,
        });
    });
  });
  return ports;
}
// keep the user's manual order + chosen side for group ports on re-assembly
export function mergePorts(fresh, prior) {
  if (!prior || !prior.length) return fresh;
  const pm = new Map(prior.map((p) => [p.id, p]));
  const merged = fresh.map((p) => {
    const o = pm.get(p.id);
    return o ? { ...p, side: o.side } : p;
  });
  const idx = new Map(prior.map((p, i) => [p.id, i]));
  return merged.sort((a, b) => (idx.has(a.id) ? idx.get(a.id) : 1e9) - (idx.has(b.id) ? idx.get(b.id) : 1e9));
}

// dagre auto-layout (left -> right)
// `rankdir` is a parameter, not a constant, because the two products that share
// this helper read in different directions: /flow diagrams run left-to-right,
// while an orchestrator workflow is a vertical stack (phase 1 at the top,
// dependencies flowing down). Default stays "LR" so /flow is untouched.
export function dagreLayout(nodes, edges, rankdir = "LR") {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir, nodesep: 46, ranksep: 96 });
  g.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(nodes.map((n) => n.id));
  nodes.forEach((n) => {
    g.setNode(n.id, { width: mw(n, 230), height: mh(n, 150) });
  });
  edges.forEach((e) => {
    if (!e.hidden && ids.has(e.source) && ids.has(e.target)) g.setEdge(e.source, e.target);
  });
  dagre.layout(g);
  return nodes.map((n) => {
    const gn = g.node(n.id);
    if (!gn) return n;
    return { ...n, position: { x: gn.x - gn.width / 2, y: gn.y - gn.height / 2 } };
  });
}

// grow expanded group containers symmetrically around centre to fit children
export function fitContainers(srcNodes) {
  const pad = 20,
    head = 34,
    NW = 250,
    NH = 150;
  let nn = srcNodes;
  srcNodes
    .filter((n) => n.type === "group" && n.data.collapsed === false)
    .forEach((c) => {
      const kids = nn.filter((k) => k.parentId === c.id);
      if (!kids.length) return;
      const W = (c.style && c.style.width) || 320,
        H = (c.style && c.style.height) || 190;
      const x0 = Math.min(...kids.map((k) => k.position.x)),
        y0 = Math.min(...kids.map((k) => k.position.y));
      const x1 = Math.max(...kids.map((k) => k.position.x + mw(k, NW))),
        y1 = Math.max(...kids.map((k) => k.position.y + mh(k, NH)));
      const dV = Math.max(0, head + pad - y0, y1 + pad - H),
        dH = Math.max(0, pad - x0, x1 + pad - W);
      if (dV > 0.5 || dH > 0.5) {
        const nW = W + 2 * dH,
          nH = H + 2 * dV;
        nn = nn.map((n) => {
          if (n.id === c.id)
            return { ...n, position: { x: n.position.x - dH, y: n.position.y - dV }, style: { ...n.style, width: nW, height: nH } };
          if (n.parentId === c.id) return { ...n, position: { x: n.position.x + dH, y: n.position.y + dV } };
          return n;
        });
      }
    });
  return nn;
}

export { mw, mh };
