// <!-- AGENT_HEADER
// role: code
// purpose: ENTRY POINT · the React Flow wrapper and the gantt time scale —
//   the only part of the graph module a read-only or time-axis consumer needs.
//   Importing this must NOT make node semantics reachable.
// AGENT_HEADER_END -->
//
// WHY THIS IS ITS OWN ENTRY, and the measurement is the argument. TASKS'
// gantt imports the canvas and the time scale and NOTHING ELSE — no nodes, no
// graph geometry, no save coordinator. Its real slice of this module is 247
// lines out of 2,004, and only an entry point can hold that line. A single
// barrel would let a gantt change reach node semantics by accident, which is
// how `components/flow-designer/` came to serve three leaves in the first
// place (R6 / IA ruling 1A: ONE module, ported once, mounted many).
export { FlowCanvas } from "./canvas/flow-canvas";
// The whole time scale, not a hand-picked subset: a partial re-export is a
// boundary that lies, and every name in `gantt-scale.ts` is time-axis maths
// that `flow-canvas.tsx:25` already depends on.
export * from "./canvas/gantt-scale";
