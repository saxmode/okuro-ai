// <!-- AGENT_HEADER
// role: code
// purpose: ENTRY POINT · node and edge components, the per-product node-view
//   context, port geometry and dagreLayout. For the two EDITOR leaves (FLOW
//   and WORKFLOWS) — the gantt must not be able to reach this.
// AGENT_HEADER_END -->
export {
  BridgeContext,
  FlowNode,
  GroupNode,
  LabeledEdge,
  NOOP_BRIDGE,
  PortSelContext,
  alignStyle,
  inkFor,
  vDefault,
} from "./nodes/nodes";
// The node-view contract in full — it IS the per-product seam (which slots a
// product fills, which fields it may edit, which port policy it wants), so a
// consumer that can read half of it cannot implement the other half.
export * from "./nodes/node-view";
// WORKFLOWS takes exactly ONE function from graph.ts — dagreLayout — out of a
// 332-line module. It shares the port geometry that sits beside it, so the two
// stay in one entry rather than splitting a single function into a fourth.
// WORKFLOWS takes exactly ONE function from graph.ts — dagreLayout — out of a
// 332-line module. It shares the port geometry that sits beside it, so the two
// stay in one entry rather than splitting a single function into a fourth.
//
// `export *` rather than a hand-picked list: the first version of this file
// enumerated 30 names and still missed `mh` and `mw`, which is what a boundary
// that lies looks like. graph.ts is pure geometry and serialisation helpers —
// there is nothing in it an editor leaf should be kept away from.
export * from "./nodes/graph";
