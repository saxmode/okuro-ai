// <!-- AGENT_HEADER
// role: code
// purpose: The per-view configuration ONE node component takes, so /flow,
//   /workflows and the orchestrator's run view share it instead of each
//   re-implementing the same card. Business logic as data, not as branches.
// AGENT_HEADER_END -->
import React from "react";

/** Which slots a view lets an author edit by double-clicking ON the node.
 *  Named for the SLOT, not the data key — /workflows' "sub" is its prompt. */
export type NodeField = "title" | "tag" | "sub";

/** Where the ports are and what they can do.
 *
 *  "data"     — /flow's own: derived from data.ports, any of four sides,
 *               typed, tap-to-add, long-press-to-move
 *  "stack-tb" — one fixed input on top, one output below. A workflow reads as
 *               a vertical stack, and the shape then states where work begins
 *               and ends without anyone reading a label.
 *  "stack-lr" — the same pair, sideways: a wide fan-out is easier to follow
 *  "none"     — no ports at all
 */
export type PortPolicy = "data" | "stack-tb" | "stack-lr" | "none";

/** A slot is either a KEY into node.data, or a function of the whole node.
 *
 *  Returning null from a function HIDES that slot. Returning undefined renders
 *  it empty — which is what /flow does today for a node with no tag, and that
 *  behaviour has to survive. */
export type SlotSource = string | ((data: NodeDataLike) => unknown);

export type NodeDataLike = Record<string, unknown>;

export interface NodeBadge {
  key: string;
  text: React.ReactNode;
  title?: string;
  /** "bad" (error tone) · "accent" · omit for the default outline */
  className?: string;
}

/**
 * ONE node component, three views.
 *
 * What separates /flow, /workflows and the orchestrator's run view is BUSINESS
 * LOGIC, not visual ability. So it is handed to the component as data; the
 * component never asks which view it is in, it asks its config.
 *
 * | axis       | /flow                  | /workflows              | orchestrator   |
 * |------------|------------------------|-------------------------|----------------|
 * | editable   | title, tag             | title, sub (= prompt)   | nothing        |
 * | colorOf    | data.color, 16 swatches| derived: risk + validity| server status  |
 * | fields     | tag / title / sub      | role / title / prompt   | same, read-only|
 * | ports      | "data"                 | "stack-tb"              | "stack-tb"     |
 * | badges     | none                   | risk, tier, criteria, … | run state      |
 *
 * Colour is the axis a review said forced three separate renderers. It does
 * not: it is one function whose OUTPUT is a colour. Three inputs, one slot.
 */
export interface NodeViewConfig {
  id: string;
  /** Empty list = read-only. The same markup renders; the double-click handler
   *  is simply absent. That is what lets a run view reuse this component. */
  editable: readonly NodeField[];
  colorOf: (data: NodeDataLike) => string | undefined;
  fields: { chip: SlotSource; title: SlotSource; sub: SlotSource };
  ports: PortPolicy;
  /** Content under the body. The view returns a list; the node renders it. */
  badges: ((data: NodeDataLike) => readonly NodeBadge[]) | null;
  /** Right-hand slot in the header, beside the chip (/workflows: the phase). */
  headerRight: ((data: NodeDataLike) => React.ReactNode) | null;
  minW: number;
  minH: number;
  /** Cap the width of a node the author has NOT resized, so one long line
   *  cannot stretch it across the canvas. null = size by content (/flow). */
  maxW: number | null;
  /** Clamp the body to N lines until the author resizes the node.
   *
   *  A /workflows prompt is a full subtask brief — hundreds of words. Drawn
   *  unclamped it turns every node into a wall of text and the GRAPH stops
   *  being readable, which is the one thing a canvas is for. Resizing lifts the
   *  clamp, so dragging a node taller actually reveals more. null = no clamp
   *  (/flow's subtitle is one short line by construction). */
  subClamp: number | null;
  /** What to draw in a slot that is empty.
   *
   *  `onlyWhenSelected` keeps an unnamed node clean on a busy canvas while
   *  still giving the author working on one something to double-click. /flow
   *  shows its em dash unconditionally and must keep doing so. */
  emptyTitle: { text: string; onlyWhenSelected?: boolean };
  emptySub: { text: string; onlyWhenSelected?: boolean } | null;
}

/** /flow's config — and the default, so anything rendering these nodes without
 *  a provider behaves exactly as it did before configuration existed. */
export const NODE_VIEW_FLOW: NodeViewConfig = {
  id: "flow",
  editable: ["title", "tag"],
  colorOf: (data) => data.color as string | undefined,
  fields: { chip: "tag", title: "title", sub: "sub" },
  ports: "data",
  badges: null,
  headerRight: null,
  minW: 230,
  minH: 96,
  maxW: null,
  subClamp: null,
  emptyTitle: { text: "—" },
  emptySub: null,
};

export const NodeViewContext = React.createContext<NodeViewConfig>(NODE_VIEW_FLOW);
export const useNodeView = () => React.useContext(NodeViewContext);

/** Read a slot through the view's field mapping. */
export function slot(cfg: NodeViewConfig, data: NodeDataLike, name: keyof NodeViewConfig["fields"]) {
  const f = cfg.fields[name];
  return typeof f === "function" ? f(data) : data[f];
}

/** May this slot be edited inline in this view? */
export function canEdit(cfg: NodeViewConfig, name: NodeField): boolean {
  return cfg.editable.indexOf(name) >= 0;
}
