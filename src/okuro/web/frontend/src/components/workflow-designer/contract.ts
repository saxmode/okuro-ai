// <!-- AGENT_HEADER
// role: code
// purpose: The DRAWN-WORKFLOW node contract, client side — the same shape
//   okuro.orchestrator.flow_compiler interprets, plus the pure helpers the
//   inspector needs (defaults, normalization, per-node authoring issues).
// AGENT_HEADER_END -->

// The compiler is the authority on this contract; this file mirrors it so the
// author sees a mistake while drawing instead of after pressing Compile. It is
// deliberately a MIRROR, not a second source of truth: everything here also
// fails server-side, and the compile endpoint always has the last word.
//
// Kept free of React so it can be unit-tested without a renderer.

import { stepLabel } from "@/lib/nouns";

// The node-kind wire value the compiler expects — a MACHINE name, unaffected
// by the D-A copy rename (the user-facing noun is `PART`, see @/lib/nouns).
export const SUBTASK_KIND = "subtask";

export const RISKS = ["LOW", "MED", "HIGH"] as const;
export const COMPLEXITIES = ["fast", "standard", "strategic"] as const;

export type Risk = (typeof RISKS)[number];
export type Complexity = (typeof COMPLEXITIES)[number];

/** ``fan_out`` makes ONE SUBTASK PER ITEM at runtime; ``over`` names the list
 *  the orchestrator supplies the items from. */
export interface FanOut {
  over: string;
  /** Artifact file in the task's artifacts/ holding the item list, and an
   *  optional key inside it. The ENGINE reads these to expand the fan-out
   *  automatically when the upstream phase closes
   *  (`workflow_run.collect_fanout_items`). The inspector does not edit them
   *  today — they are authored in the seeded graph — but they MUST survive a
   *  round-trip through this editor. See `normalizeSubtaskData`. */
  items_from?: string;
  items_key?: string;
}

/** ``node.data`` for a node that RUNS. A node without ``kind: "subtask"`` is
 *  decoration — the compiler ignores it, and so does everything here.
 *
 *  A type alias rather than an interface on purpose: only aliases get TypeScript's
 *  implicit index signature, which is what makes this assignable to ReactFlow's
 *  ``Record<string, unknown>`` node data without a cast at every call site. */
export type SubtaskData = {
  kind: typeof SUBTASK_KIND;
  /** explicit, NOT derived from edge depth */
  phase: number;
  /** must exist in the role catalogue — the inspector picks, never types */
  role: string;
  prompt?: string;
  acceptance_criteria?: string[];
  risk?: Risk;
  complexity?: Complexity;
  artifact_name?: string;
  outputs?: string[];
  /** names the phase; any node in the phase may set it */
  phase_name?: string;
  /** phase-level; any node in the phase sets it for the whole phase */
  serialize?: boolean;
  fan_out?: FanOut;
  /** canvas-only: the short label drawn on the node. Never read by the compiler. */
  title?: string;
};

/** ``node.data`` for ANY node on the canvas, subtask or decoration. Every
 *  subtask field is optional and ``kind`` is free-form, because a decoration
 *  node legitimately has none of them — narrowing to a subtask is what
 *  ``isSubtask`` is for.
 *
 *  The index signature is not laxity: a drawn graph is a ReactFlow payload whose
 *  ``data`` is an open bag, and the compiler itself spreads ``**data`` rather
 *  than picking known keys. Anything extra an author or a future field puts
 *  there has to survive a load/save round trip. */
export type NodeData = Partial<Omit<SubtaskData, "kind">> & {
  kind?: string;
  [key: string]: unknown;
};

export const DEFAULT_RISK: Risk = "MED";
export const DEFAULT_COMPLEXITY: Complexity = "standard";

export function isSubtask(data: NodeData | undefined | null): boolean {
  return !!data && data.kind === SUBTASK_KIND;
}

export function newSubtaskData(phase = 1, role = ""): SubtaskData {
  return {
    kind: SUBTASK_KIND,
    phase,
    role,
    prompt: "",
    acceptance_criteria: [],
    risk: DEFAULT_RISK,
    complexity: DEFAULT_COMPLEXITY,
  };
}

/** Strip what the compiler would ignore anyway, so a saved graph carries intent
 *  rather than the inspector's empty form fields. Optional-and-empty is dropped;
 *  required fields are kept verbatim EVEN WHEN INVALID so the author does not
 *  lose a half-finished node on save — validity is reported, not enforced. */
export function normalizeSubtaskData(data: NodeData): SubtaskData {
  const out: SubtaskData = {
    kind: SUBTASK_KIND,
    phase: Number(data.phase ?? 1),
    role: String(data.role ?? ""),
  };
  const prompt = String(data.prompt ?? "").trim();
  if (prompt) out.prompt = prompt;

  const criteria = cleanList(data.acceptance_criteria);
  if (criteria.length) out.acceptance_criteria = criteria;

  const outputs = cleanList(data.outputs);
  if (outputs.length) out.outputs = outputs;

  const risk = String(data.risk ?? DEFAULT_RISK) as Risk;
  if (risk !== DEFAULT_RISK) out.risk = risk;
  const complexity = String(data.complexity ?? DEFAULT_COMPLEXITY) as Complexity;
  if (complexity !== DEFAULT_COMPLEXITY) out.complexity = complexity;

  const artifact = String(data.artifact_name ?? "").trim();
  if (artifact) out.artifact_name = artifact;
  const phaseName = String(data.phase_name ?? "").trim();
  if (phaseName) out.phase_name = phaseName;
  const title = String(data.title ?? "").trim();
  if (title) out.title = title;

  if (data.serialize) out.serialize = true;

  // fan_out is a toggle in the inspector: ON with a blank `over` still has to
  // round-trip, otherwise switching it on and typing later silently loses it.
  //
  // PRESERVE EVERY KEY, do not rebuild from `over` alone. This normalizer runs on
  // every autosave, so rebuilding dropped `items_from`/`items_key` — the fields
  // the ENGINE reads to expand a fan-out by itself. Opening the seeded prism
  // workflow and moving a node was enough to silently disable its automation:
  // the graph still said "one subtask per topic", and the run would wait forever
  // for a list nothing was told to look for. A normalizer must not be the thing
  // that decides which parts of the contract survive.
  const fan = data.fan_out as FanOut | undefined;
  if (fan && typeof fan === "object") {
    out.fan_out = { ...fan, over: String(fan.over ?? "") };
  }

  return out;
}

function cleanList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((v) => String(v ?? "").trim()).filter(Boolean);
}

/** Authoring problems with ONE node, in the compiler's own terms.
 *
 *  ``knownRoles`` is the live role catalogue. Pass it empty (or omit it) to skip
 *  the catalogue check — an unreachable /api/roles must not paint every node red.
 */
export function nodeIssues(data: NodeData, knownRoles?: readonly string[]): string[] {
  const issues: string[] = [];
  const role = String(data.role ?? "").trim();
  if (!role) {
    issues.push("no role — every part needs one");
  } else if (knownRoles && knownRoles.length && !knownRoles.includes(role)) {
    issues.push(`role '${role}' is not in the role catalogue`);
  }

  // A cleared number input hands back "", not undefined — both mean "no phase".
  const phase: unknown = data.phase;
  if (phase === undefined || phase === null || phase === "") {
    issues.push("no step — set a step (1, 2, 3, …)");
  } else if (!Number.isInteger(Number(phase))) {
    issues.push(`non-numeric step '${String(phase)}'`);
  } else if (Number(phase) < 1) {
    issues.push(`step ${Number(phase)} — steps start at 1`);
  }

  const risk = String(data.risk ?? DEFAULT_RISK).toUpperCase();
  if (!(RISKS as readonly string[]).includes(risk)) issues.push(`invalid risk '${risk}'`);
  const cx = String(data.complexity ?? DEFAULT_COMPLEXITY).toLowerCase();
  if (!(COMPLEXITIES as readonly string[]).includes(cx)) issues.push(`invalid complexity '${cx}'`);

  if (data.fan_out && !String((data.fan_out as FanOut).over ?? "").trim()) {
    issues.push("fan-out is on but has no list to fan out over");
  }
  return issues;
}

/** True when this node could compile — the gate the editor uses to mark a node
 *  (and the workflow) as valid. */
export function isNodeValid(data: NodeData, knownRoles?: readonly string[]): boolean {
  return nodeIssues(data, knownRoles).length === 0;
}

interface GraphNodeLike {
  id: string;
  data?: NodeData;
}
interface GraphEdgeLike {
  source: string;
  target: string;
}

/** Sorted phase numbers present on the canvas — what the phase rail renders. */
export function phasesOf(nodes: readonly GraphNodeLike[]): number[] {
  const seen = new Set<number>();
  for (const n of nodes) {
    if (!isSubtask(n.data)) continue;
    const p = Number(n.data?.phase);
    if (Number.isInteger(p)) seen.add(p);
  }
  return [...seen].sort((a, b) => a - b);
}

/** The phase's name, taken from any node in it that sets one — same rule the
 *  compiler uses, so the rail cannot disagree with the compiled plan. */
export function phaseNameOf(nodes: readonly GraphNodeLike[], phase: number): string {
  for (const n of nodes) {
    if (!isSubtask(n.data)) continue;
    if (Number(n.data?.phase) !== phase) continue;
    const name = String(n.data?.phase_name ?? "").trim();
    if (name) return name;
  }
  return stepLabel(phase);
}

/** True when ANY node in the phase asks for it — serialize is phase-level. */
export function phaseIsSerialized(nodes: readonly GraphNodeLike[], phase: number): boolean {
  return nodes.some(
    (n) => isSubtask(n.data) && Number(n.data?.phase) === phase && !!n.data?.serialize,
  );
}

export interface GraphIssue {
  /** the node this is about, or null for a whole-graph problem */
  nodeId: string | null;
  message: string;
}

/** Everything wrong with the drawing, checked locally. Mirrors the compiler's
 *  order so the first message an author sees here is the one Compile would give.
 *  Server-side compilation stays authoritative — this only shortens the loop. */
export function graphIssues(
  nodes: readonly GraphNodeLike[],
  edges: readonly GraphEdgeLike[],
  knownRoles?: readonly string[],
): GraphIssue[] {
  const issues: GraphIssue[] = [];
  const subtasks = nodes.filter((n) => isSubtask(n.data));
  if (!subtasks.length) {
    return [{ nodeId: null, message: "no part nodes — add one to make this runnable" }];
  }
  for (const n of subtasks) {
    for (const m of nodeIssues(n.data ?? {}, knownRoles)) {
      issues.push({ nodeId: n.id, message: m });
    }
  }

  // Edges between subtask nodes are dependencies; an edge touching a node that
  // is not on the canvas at all is a broken graph (the compiler raises).
  const known = new Set(nodes.map((n) => n.id));
  const subtaskIds = new Set(subtasks.map((n) => n.id));
  const deps = new Map<string, string[]>();
  for (const id of subtaskIds) deps.set(id, []);
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) {
      issues.push({
        nodeId: null,
        message: `edge '${e.source}'->'${e.target}' references a node that is not in the graph`,
      });
      continue;
    }
    if (subtaskIds.has(e.source) && subtaskIds.has(e.target)) {
      deps.get(e.target)!.push(e.source);
    }
  }

  const cycle = findCycle(deps);
  if (cycle) issues.push({ nodeId: null, message: `dependency cycle: ${cycle.join(" -> ")}` });

  return issues;
}

/** 3-colour DFS, matching flow_compiler._detect_cycle. Returns the cycle path. */
function findCycle(deps: Map<string, string[]>): string[] | null {
  const WHITE = 0,
    GRAY = 1,
    BLACK = 2;
  const colour = new Map<string, number>();
  for (const id of deps.keys()) colour.set(id, WHITE);
  const stack: string[] = [];

  const visit = (n: string): string[] | null => {
    colour.set(n, GRAY);
    stack.push(n);
    for (const m of deps.get(n) ?? []) {
      if (colour.get(m) === GRAY) return [...stack.slice(stack.indexOf(m)), m];
      if ((colour.get(m) ?? BLACK) === WHITE) {
        const found = visit(m);
        if (found) return found;
      }
    }
    stack.pop();
    colour.set(n, BLACK);
    return null;
  };

  for (const id of deps.keys()) {
    if (colour.get(id) === WHITE) {
      const found = visit(id);
      if (found) return found;
    }
  }
  return null;
}
