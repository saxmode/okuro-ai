// @ts-nocheck
// <!-- AGENT_HEADER
// role: code
// purpose: flowToMermaid — pure converter from an okuro-flow graph (the collapsed
//   serialize() form: nodes + edges, groups folded with data.members/innerEdges and
//   go_/gi_ proxy handles) into Mermaid `flowchart` source. okuro-flow is a visualizer,
//   not a flowchart engine — ports/positions have no Mermaid equivalent and are dropped;
//   nodes, edges, edge labels, category shapes/colours, and nested groups are preserved.
// AGENT_HEADER_END -->

// category -> Mermaid node shape. {0} is the quoted label.
const SHAPE = {
  process: (l) => `["${l}"]`, // rectangle
  input: (l) => `(["${l}"])`, // stadium — entry
  output: (l) => `[/"${l}"/]`, // parallelogram — exit
  decision: (l) => `{"${l}"}`, // rhombus — branch
  note: (l) => `>"${l}"]`, // flag — annotation
  group: (l) => `["${l}"]`, // unused (groups become subgraphs) — fallback only
};

// category -> stroke/fill for classDef fidelity. These hex are FALLBACKS only;
// the canonical values live in the --fd-cat-* CSS vars (flow-designer.css),
// resolved at generation time by resolveCat() so mermaid classDefs match the
// canvas. Mermaid classDef needs a literal color string, so a plain var()
// reference (as graph.ts uses) won't work here — hence getComputedStyle.
const CAT_COLOR = {
  process: "#4aa3ff",
  input: "#46d17a",
  output: "#ffc24d",
  decision: "#ff6fae",
  note: "#9aa0aa",
};

/** Resolve a category's canonical --fd-cat-* CSS var to a literal color,
 *  falling back to the CAT_COLOR hex (or the group hue) when unavailable. */
function resolveCat(cat: string): string {
  const fb = (CAT_COLOR as Record<string, string>)[cat] || "#b07cff";
  if (typeof document === "undefined") return fb;
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(`--fd-cat-${cat}`)
    .trim();
  return v || fb;
}

// Mermaid ids must be alnum/underscore. okuro ids already are, but coerce defensively
// and guarantee a leading letter so numeric-led ids never break the parser.
const mid = (id) => {
  const s = String(id).replace(/[^A-Za-z0-9_]/g, "_");
  return /^[A-Za-z_]/.test(s) ? s : "n_" + s;
};

// escape a label for inside Mermaid quotes: kill double-quotes, flatten newlines to <br/>
const esc = (s) =>
  String(s == null ? "" : s)
    .replace(/"/g, "&quot;")
    .replace(/\s*\n\s*/g, "<br/>")
    .trim();

// decode a go_/gi_ proxy handle back to the real member id it points at.
// handle shape: "go_<memberId>__<port>" / "gi_<memberId>__<port>" (graph.ts::decode).
const proxyMember = (handle) => {
  if (typeof handle !== "string") return null;
  if (handle.indexOf("go_") !== 0 && handle.indexOf("gi_") !== 0) return null;
  const r = handle.slice(3);
  const i = r.indexOf("__");
  return i < 0 ? r : r.slice(0, i);
};

const labelOf = (n) => {
  const d = n?.data || {};
  return esc(d.title || d.tag || n?.id || "node");
};

/**
 * flowToMermaid(graph, opts?) -> string
 *   graph: { nodes, edges } in collapsed serialize() form. Nested collapsed groups
 *          (type:"group" with data.members + data.innerEdges) recurse into subgraphs.
 *   opts.direction: "LR" (default) | "TB" | "RL" | "BT"
 *   opts.accent: CSS colour for edge strokes (resolved --color-accent) — edges
 *                inherit the user's accent, mirroring the canvas. Category node
 *                hues stay semantic and are unaffected.
 */
export function flowToMermaid(graph, opts = {}) {
  const dir = opts.direction || "LR";
  const nodes = (graph && graph.nodes) || [];
  const edges = (graph && graph.edges) || [];

  const lines = [`flowchart ${dir}`];
  const usedCats = new Set();
  const classOf = []; // [mermaidId, cat]
  const allEdges = []; // flattened real edges (real ids, proxy handles decoded)

  // emit one node (or recurse a group into a subgraph). `indent` is cosmetic.
  const emitNode = (n, indent) => {
    const pad = "  ".repeat(indent);
    const id = mid(n.id);

    if (n.type === "group") {
      // subgraph header carries the group title; members render inside, recursively.
      lines.push(`${pad}subgraph ${id}["${labelOf(n)}"]`);
      lines.push(`${pad}  direction ${dir}`);
      const members = (n.data && n.data.members) || [];
      members.forEach((m) => emitNode(m, indent + 1));
      lines.push(`${pad}end`);
      usedCats.add("group");
      classOf.push([id, "group"]);
      // inner edges live between real member ids — collect for the flat edge pass
      ((n.data && n.data.innerEdges) || []).forEach((e) => allEdges.push(e));
      return;
    }

    const cat = (n.data && n.data.cat) || "process";
    const shape = (SHAPE[cat] || SHAPE.process)(labelOf(n));
    lines.push(`${pad}${id}${shape}`);
    if (CAT_COLOR[cat]) {
      usedCats.add(cat);
      classOf.push([id, cat]);
    }
  };

  nodes.forEach((n) => emitNode(n, 0));
  edges.forEach((e) => allEdges.push(e));

  // edges: decode proxy handles back to the real member node so arrows cross
  // subgraph boundaries to the actual node rather than the group shell.
  const seen = new Set();
  allEdges.forEach((e) => {
    if (!e || e.source == null || e.target == null) return;
    // collapsed groups expose merged handles; the real member id lives in the
    // stashed proxy (data._oout/_oin). Fall back to legacy gi_/go_ handles.
    const src = proxyMember((e.data && e.data._oout) || e.sourceHandle) || e.source;
    const tgt = proxyMember((e.data && e.data._oin) || e.targetHandle) || e.target;
    const key = `${src}->${tgt}::${(e.data && e.data.label) || ""}::${e.id || ""}`;
    if (seen.has(key)) return;
    seen.add(key);
    const a = mid(src);
    const b = mid(tgt);
    const lbl = e.data && e.data.label ? `|"${esc(e.data.label)}"|` : "";
    lines.push(`  ${a} -->${lbl} ${b}`);
  });

  // classDefs for category colours (visualizer parity with the canvas)
  if (usedCats.size) {
    lines.push("");
    usedCats.forEach((cat) => {
      const c = resolveCat(cat); // canonical --fd-cat-* with hex fallback
      lines.push(`  classDef ${cat} stroke:${c},stroke-width:1.5px,color:#e8eaed,fill:${c}22`);
    });
    classOf.forEach(([id, cat]) => lines.push(`  class ${id} ${cat}`));
  }

  // Edges inherit the user's chosen accent — mirrors the canvas, where every edge
  // is stroked with the resolved --color-accent. Category node hues stay semantic.
  if (opts.accent) {
    lines.push("");
    lines.push(`  linkStyle default stroke:${opts.accent},stroke-width:1.7px;`);
  }

  return lines.join("\n");
}
