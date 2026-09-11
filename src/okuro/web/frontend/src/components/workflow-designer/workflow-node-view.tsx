// <!-- AGENT_HEADER
// role: code
// purpose: /workflows' NodeViewConfig — the data that makes the ONE shared node
//   component (flow-designer/nodes.tsx FlowNode) behave as a workflow subtask.
//   No node component of its own: business logic only.
// AGENT_HEADER_END -->
import React from "react";

import {
  NODE_VIEW_FLOW,
  type NodeBadge,
  type NodeDataLike,
  type NodeViewConfig,
} from "@/components/flow-designer/node-view";

import { stepLabel } from "@/lib/nouns";
import {
  DEFAULT_COMPLEXITY,
  DEFAULT_RISK,
  isSubtask,
  nodeIssues,
} from "./contract";

/** Where a workflow node's colour comes from.
 *
 *  RISK, because risk is the only thing on a DRAWN node that the orchestrator
 *  later acts on by itself — HIGH risk is what makes a subtask wait for
 *  approval. An invalid node overrides it: a workflow that cannot compile is a
 *  harder problem than a risky one.
 *
 *  This is the axis the critic said forced three separate node renderers. It
 *  does not: it is one function, and the component that consumes it never
 *  learns which view it is in. /flow's author picks a colour from 16 swatches,
 *  /workflows derives one from risk, the run view will read one from the
 *  server's status token — three inputs, one slot.
 */
function subtaskColor(data: NodeDataLike, roles: readonly string[]): string {
  if (nodeIssues(data, roles).length) return "var(--color-error)";
  const risk = String(data.risk ?? DEFAULT_RISK).toUpperCase();
  if (risk === "HIGH") return "var(--color-warning)";
  if (risk === "LOW") return "var(--color-fg-muted)";
  return "var(--color-info)";
}

/** Decoration — a node WITHOUT `kind: "subtask"`. The compiler ignores it and
 *  an edge to it carries no dependency, so it must never look like work. */
const NOTE_COLOR = "var(--color-fg-subtle)";

function subtaskBadges(data: NodeDataLike, roles: readonly string[]): NodeBadge[] {
  const out: NodeBadge[] = [];
  const risk = String(data.risk ?? DEFAULT_RISK).toUpperCase();
  const complexity = String(data.complexity ?? DEFAULT_COMPLEXITY);
  const criteria = ((data.acceptance_criteria as unknown[]) ?? []).length;
  const fan = data.fan_out as { over?: string } | undefined;

  if (fan) {
    // Say what actually HAPPENS at runtime, not what the field is called: one
    // drawn node becomes N subtasks, and an author who does not know that will
    // draw N nodes by hand.
    out.push({
      key: "fan",
      text: `⁂ one part per item${fan.over ? ` · ${fan.over}` : ""}`,
      title: "this node expands into one part per item at runtime",
      className: "accent",
    });
  }
  out.push({ key: "risk", text: risk, title: `risk ${risk}` });
  out.push({
    key: "cx",
    text: complexity,
    title: `tier ${complexity} — picks the model this node runs on`,
  });
  if (criteria) out.push({ key: "ac", text: `✓${criteria}`, title: `${criteria} acceptance criteria` });
  if (data.serialize) out.push({ key: "ser", text: "serial", title: "this step runs one part at a time" });

  const issues = nodeIssues(data, roles);
  if (issues.length) {
    out.push({ key: "bad", text: `! ${issues.length}`, title: issues.join("\n"), className: "bad" });
  }
  return out;
}

/** The phase, in the header's right-hand slot.
 *
 *  Role left, phase right: the two things that decide WHO runs this and WHEN.
 *  Everything else on the card is supporting detail. */
function phaseSlot(data: NodeDataLike): React.ReactNode {
  if (!isSubtask(data)) return null;
  const n = Number(data.phase);
  const ok = Number.isInteger(n) && n >= 1;
  return (
    <span
      className={"fd-badge" + (ok ? "" : " bad")}
      title={ok ? stepLabel(n) : "no step — this cannot compile"}
    >
      {ok ? `ST${n}` : "no step"}
    </span>
  );
}

/** Build /workflows' view config.
 *
 *  A hook because the role catalogue is LIVE editor state: the compiler refuses
 *  a role that is not in it, so validity — and therefore colour and the issue
 *  badge — depends on it. It is deliberately not node data: a role list must
 *  never end up serialized into a saved graph.
 *
 *  `portLayout` is graph-level, the same way /flow's own portMode is: it is a
 *  property of how this diagram is READ, not of any single node.
 */
export function useWorkflowNodeView(
  roles: readonly string[],
  portLayout: "tb" | "lr",
): NodeViewConfig {
  return React.useMemo<NodeViewConfig>(
    () => ({
      ...NODE_VIEW_FLOW,
      id: "workflow",
      // Title and prompt are free text, so inline is safe. `role` is NOT here:
      // the compiler REFUSES a role outside the catalogue, so the inspector's
      // picker stays the only way to set one — an inline box would be a
      // backdoor to a workflow that can only fail at Compile. `phase` is out
      // for the same reason in a weaker form: a constrained integer, not prose.
      editable: ["title", "sub"],
      colorOf: (data: NodeDataLike) => (isSubtask(data) ? subtaskColor(data, roles) : NOTE_COLOR),
      fields: {
        // null HIDES the chip — a decoration node has no role to show.
        // A SUBTASK without one says so: the compiler refuses it, so an empty
        // pill would be the canvas quietly hiding the reason the workflow
        // cannot run.
        chip: (data: NodeDataLike) =>
          isSubtask(data) ? String(data.role ?? "").trim() || "no role" : null,
        title: "title",
        sub: "prompt",
      },
      ports: portLayout === "lr" ? "stack-lr" : "stack-tb",
      badges: (data: NodeDataLike) => (isSubtask(data) ? subtaskBadges(data, roles) : []),
      headerRight: phaseSlot,
      // Below this a subtask stops being readable: role, phase, title, two
      // lines of prompt and a badge row all have to fit.
      minW: 200,
      minH: 110,
      // Shapes a FRESH node so a long prompt cannot stretch one across the
      // canvas. Dropped once the author resizes, or capping would silently undo
      // the drag they just made.
      maxW: 264,
      // A prompt is a full subtask brief. Unclamped it turns every node into a
      // wall of text and the GRAPH stops being readable — which is the one
      // thing a canvas is for. The inspector is where you READ a prompt; the
      // node only has to say which subtask this is. Resizing lifts the clamp.
      subClamp: 2,
      // Keep an unnamed node clean on a busy canvas, but always give the author
      // working on one something to double-click.
      emptyTitle: { text: "untitled", onlyWhenSelected: true },
      emptySub: { text: "no prompt", onlyWhenSelected: true },
    }),
    [roles, portLayout],
  );
}
