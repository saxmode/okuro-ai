// <!-- AGENT_HEADER
// role: code
// purpose: The drawn-workflow editor — shared FlowCanvas + workflow node types,
//   backend autosave with the clobber guard, and the Compile affordance that
//   turns the compiler's refusals into something an author can act on.
// AGENT_HEADER_END -->
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  MarkerType,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import React from "react";
import { useNavigate } from "react-router";

import { FlowCanvas } from "@/components/flow-designer/flow-canvas";
import { dagreLayout } from "@/components/flow-designer/graph";
import { NodeViewContext } from "@/components/flow-designer/node-view";
import { useGraphEditor } from "@/components/flow-designer/use-graph-editor";
import { BridgeContext, FlowNode, NOOP_BRIDGE } from "@/components/flow-designer/nodes";
import {
  roleApi,
  workflowApi,
  type CompileResult,
  type WorkflowDetail,
} from "@/lib/api";
import type { RoleInfo } from "@/types/api";

import {
  graphIssues,
  isSubtask,
  newSubtaskData,
  normalizeSubtaskData,
  phaseNameOf,
  phasesOf,
  type NodeData,
} from "./contract";
import { WorkflowInspector } from "./workflow-inspector";
import { useWorkflowNodeView } from "./workflow-node-view";

/** Both node types render through the SAME component /flow uses. What makes a
 *  subtask look and behave like a subtask is the view config, not a second
 *  implementation — see workflow-node-view.tsx. */
const WF_NODE_TYPES = {
  subtask: (props: NodeProps) => <FlowNode {...props} mode="quad" />,
  note: (props: NodeProps) => <FlowNode {...props} mode="quad" />,
};

/** Where the ports sit. A workflow reads as a vertical stack, so "tb" is the
 *  default; "lr" exists because a wide fan-out is easier to follow sideways.
 *  Graph-level, like /flow's own portMode. */
export type PortLayout = "tb" | "lr";

const NEW_NAME = "Untitled workflow";
/** Vertical distance between stacked subtasks. Comfortably clears a node at its
 *  tallest (a fan-out node with a prompt and chips runs ~250px) so a fresh
 *  subtask never lands on top of the one above it. */
const STACK_GAP = 260;
const ORIGIN = "web-workflow-designer";
const EDGE_TYPES = {};
const ACCENT = "var(--color-accent)";

// Edges are DEPENDENCIES, so they are directed and read as such: an arrowhead,
// no label, no styling vocabulary borrowed from /flow (where an edge can mean
// anything the author wants it to).
const DEFAULT_EDGE = {
  markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
};

/** True when a dimensions change came from a NodeResizer drag rather than from
 *  ReactFlow measuring a node it just mounted. Only the former is an edit worth
 *  saving; treating both alike either loses every resize or writes a revision
 *  on every page load. Exported so the distinction is pinned by a test. */
export function isResize(change: NodeChange): boolean {
  if (change.type !== "dimensions") return false;
  return !!change.setAttributes || change.resizing === true;
}

interface Props {
  workflowId: string | null;
}

export function WorkflowDesigner({ workflowId }: Props) {
  const navigate = useNavigate();
  const rf = useReactFlow();

  const [nodes, setNodes] = React.useState<Node[]>([]);
  const [edges, setEdges] = React.useState<Edge[]>([]);
  const [name, setName] = React.useState(NEW_NAME);
  const [description, setDescription] = React.useState("");
  // Graph-level, exactly like okuro-flow persists its own `portMode`: how a
  // diagram is READ is a property of the diagram, not of any one node.
  const [portLayout, setPortLayout] = React.useState<PortLayout>("tb");
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [roles, setRoles] = React.useState<RoleInfo[]>([]);
  const [compiled, setCompiled] = React.useState<CompileResult | null>(null);
  const [compiling, setCompiling] = React.useState(false);
  const [msg, setMsg] = React.useState("");

  const nodesRef = React.useRef(nodes);
  nodesRef.current = nodes;
  const edgesRef = React.useRef(edges);
  edgesRef.current = edges;
  const nameRef = React.useRef(name);
  nameRef.current = name;
  const descRef = React.useRef(description);
  descRef.current = description;
  const portLayoutRef = React.useRef(portLayout);
  portLayoutRef.current = portLayout;

  const flash = (text: string) => {
    setMsg(text);
    setTimeout(() => setMsg(""), 2200);
  };

  // `serialize` and `adoptDoc` are defined BELOW the coordinator (adoptDoc needs
  // the coordinator's own bookkeeping), so they reach it late-bound through
  // refs. The alternative — hoisting them above — would just move the cycle.
  const serializeRef = React.useRef<() => { nodes?: unknown[] } & Record<string, unknown>>(() => ({
    nodes: [],
    edges: [],
  }));
  const adoptRef = React.useRef<(d: WorkflowDetail) => void>(() => {});

  // The write coordinator — the same one /flow uses. Debounce, one save in
  // flight, the empty-canvas clobber guard and 409 adoption all live there;
  // what makes THIS editor dirty stays here, in markDirty's call sites.
  const editor = useGraphEditor<WorkflowDetail>({
    api: {
      create: (body) => workflowApi.create(body as Parameters<typeof workflowApi.create>[0]),
      upsert: (id, body) => workflowApi.upsert(id, body as Parameters<typeof workflowApi.upsert>[1]),
    },
    origin: ORIGIN,
    defaultName: NEW_NAME,
    initialId: workflowId,
    initiallyLoading: true,
    serialize: () => serializeRef.current(),
    getName: () => nameRef.current,
    getDescription: () => descRef.current,
    getNodeCount: () => nodesRef.current.length,
    adopt: (d) => adoptRef.current(d),
    flash,
    conflictMessage: "reloaded — this workflow was changed elsewhere",
  });
  // 409 adoption, base_rev and the save badge are the coordinator's business
  // now — this editor only says WHEN something changed.
  const { saveState, markDirty, doSave, adoptMeta, currentIdRef, dirtyRef, loadingRef } = editor;

  const roleIds = React.useMemo(() => roles.map((r) => r.id), [roles]);

  // ---- role catalogue ----
  // Fetched once. A failure is non-fatal on purpose: the picker degrades to the
  // roles already on the canvas, and Compile still enforces the real catalogue.
  React.useEffect(() => {
    let live = true;
    roleApi
      .list()
      .then((r: { roles: RoleInfo[] }) => live && setRoles(r.roles))
      .catch(() => live && setRoles([]));
    return () => {
      live = false;
    };
  }, []);

  // ---- load / adopt ----
  const adoptDoc = React.useCallback((d: WorkflowDetail) => {
    loadingRef.current = true;
    // Identity + rev + the clobber guard's baseline, and it cancels anything the
    // previous document had in flight.
    adoptMeta(d as WorkflowDetail & { graph?: { nodes?: unknown[] } });
    const g = d.graph || { nodes: [], edges: [] };
    const ns = ((g.nodes as Node[]) || []).map((n) => ({
      ...n,
      type: n.type || (isSubtask(n.data as NodeData) ? "subtask" : "note"),
    }));
    setNodes(ns);
    setEdges(((g.edges as Edge[]) || []).map((e) => ({ ...DEFAULT_EDGE, ...e })));
    setName(d.name || NEW_NAME);
    setDescription(d.description || "");
    const st = (g as { settings?: { portLayout?: string } }).settings;
    setPortLayout(st?.portLayout === "lr" ? "lr" : "tb");
    setTimeout(() => (loadingRef.current = false), 60);
  }, [adoptMeta, loadingRef]);

  React.useEffect(() => {
    if (!workflowId) {
      loadingRef.current = false;
      return;
    }
    let live = true;
    workflowApi
      .get(workflowId)
      .then((d) => live && adoptDoc(d))
      .catch(() => {
        loadingRef.current = false;
        flash("failed to load workflow");
      });
    return () => {
      live = false;
    };
  }, [workflowId, adoptDoc]);

  // ---- serialize + autosave ----
  const serialize = React.useCallback(
    () => ({
      nodes: nodesRef.current.map((n) => ({
        id: n.id,
        type: n.type,
        position: n.position,
        width: n.width,
        height: n.height,
        data: isSubtask(n.data as NodeData)
          ? normalizeSubtaskData(n.data as NodeData)
          : (n.data as NodeData),
      })),
      edges: edgesRef.current.map((e) => ({ id: e.id, source: e.source, target: e.target })),
      settings: { portLayout: portLayoutRef.current },
    }),
    [],
  );

  // Late-bind for the coordinator, now that serialize/adoptDoc exist.
  serializeRef.current = serialize;
  adoptRef.current = adoptDoc;

  // ---- graph editing ----
  const onNodesChange = React.useCallback(
    (changes: NodeChange[]) => {
      setNodes((ns) => applyNodeChanges(changes, ns));
      // Selection and MEASUREMENT are viewport noise, not workflow edits —
      // autosaving on them would write a row every time a node is clicked or
      // the canvas re-measures on mount.
      //
      // A NodeResizer drag is NOT noise: it arrives as a dimensions change too,
      // but carries `setAttributes` (which is what writes node.width/height) or
      // `resizing`. Measurement changes carry neither, so this tells a deliberate
      // resize apart from the canvas measuring itself — without it, a resize
      // would look right until reload and then be gone.
      if (changes.some((c) => c.type !== "select" && (c.type !== "dimensions" || isResize(c))))
        markDirty();
    },
    [markDirty],
  );

  const onEdgesChange = React.useCallback(
    (changes: EdgeChange[]) => {
      setEdges((es) => applyEdgeChanges(changes, es));
      if (changes.some((c) => c.type !== "select")) markDirty();
    },
    [markDirty],
  );

  const onConnect = React.useCallback(
    (conn: Connection) => {
      setEdges((es) => addEdge({ ...conn, ...DEFAULT_EDGE }, es));
      markDirty();
    },
    [markDirty],
  );

  const addSubtask = React.useCallback(() => {
    const id = `n${Date.now().toString(36)}${Math.floor(Math.random() * 1e4).toString(36)}`;
    // New nodes land in the phase the author is currently working in, so
    // building a phase does not mean retyping the phase number every time.
    const sel = nodesRef.current.find((n) => n.id === selectedId);
    const phase = Number((sel?.data as NodeData | undefined)?.phase) || 1;
    const centre = rf.screenToFlowPosition
      ? rf.screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
      : { x: 120, y: 120 };
    setNodes((ns) => {
      // Extend the STACK rather than dropping the node wherever the viewport
      // happens to be: a workflow reads top-to-bottom, so a new subtask belongs
      // under the lowest one, in the same column. Cascading it diagonally (the
      // old behaviour) fought the direction the ports now point.
      const last = ns.reduce<{ x: number; y: number } | null>(
        (lo, n) => (!lo || n.position.y > lo.y ? { x: n.position.x, y: n.position.y } : lo),
        null,
      );
      const position = last
        ? { x: last.x, y: last.y + STACK_GAP }
        : { x: centre.x, y: centre.y };
      return [
        ...ns.map((n) => ({ ...n, selected: false })),
        {
          id,
          type: "subtask",
          position,
          data: newSubtaskData(phase) as unknown as Record<string, unknown>,
          selected: true,
        },
      ];
    });
    setSelectedId(id);
    markDirty();
  }, [markDirty, rf, selectedId]);

  const addNote = React.useCallback(() => {
    const id = `note${Date.now().toString(36)}`;
    setNodes((ns) => [
      ...ns,
      {
        id,
        type: "note",
        position: { x: 60, y: 60 + ns.length * 20 },
        data: { kind: "note", title: "note" },
      },
    ]);
    markDirty();
  }, [markDirty]);

  const autoLayout = React.useCallback(() => {
    // "TB": a workflow is a vertical stack, phase 1 at the top and dependencies
    // flowing down, which is also where the node ports are. /flow keeps the
    // default "LR" — same helper, different reading direction.
    setNodes((ns) =>
      dagreLayout(ns, edgesRef.current, portLayoutRef.current === "lr" ? "LR" : "TB"),
    );
    markDirty();
    setTimeout(() => rf.fitView({ duration: 220, padding: 0.18 }), 40);
  }, [markDirty, rf]);

  const updateSelected = React.useCallback(
    (patch: NodeData) => {
      if (!selectedId) return;
      setNodes((ns) =>
        ns.map((n) =>
          n.id === selectedId ? { ...n, data: patch as unknown as Record<string, unknown> } : n,
        ),
      );
      markDirty();
    },
    [markDirty, selectedId],
  );

  /** Inline edit from a node (double-click on its title or prompt). Addressed by
   *  id rather than by selection, because double-clicking a field is not the
   *  same gesture as selecting the node and must not depend on it. Merges into
   *  the node's existing data so an inline edit can never drop a field the
   *  inspector owns. */
  const patchNode = React.useCallback(
    (nodeId: string, patch: NodeData) => {
      setNodes((ns) =>
        ns.map((n) =>
          n.id === nodeId
            ? { ...n, data: { ...(n.data as NodeData), ...patch } as unknown as Record<string, unknown> }
            : n,
        ),
      );
      markDirty();
    },
    [markDirty],
  );

  /** /workflows' half of the shared node: what it may edit, where its colour
   *  comes from, its ports and badges. Business logic as DATA — there is no
   *  workflow node COMPONENT any more. */
  const nodeView = useWorkflowNodeView(roleIds, portLayout);

  /** The shared node writes edits back through BridgeContext. Only the two
   *  free-text slots are wired: RENAME is the title, SETSUB is the prompt.
   *  Everything else in the bridge stays a no-op, which is exactly what makes
   *  /flow's port and edge affordances inert here without a single conditional
   *  — the node offers them, this view simply declines to answer. */
  const nodeBridge = React.useMemo(
    () => ({
      ...NOOP_BRIDGE,
      RENAME: (nodeId: string, v: string) => patchNode(nodeId, { title: v }),
      SETSUB: (nodeId: string, v: string) => patchNode(nodeId, { prompt: v }),
    }),
    [patchNode],
  );

  const deleteWorkflow = React.useCallback(async () => {
    const id = currentIdRef.current;
    if (!id) return;
    if (!window.confirm(`Delete workflow "${nameRef.current}"? This cannot be undone.`)) return;
    try {
      await workflowApi.remove(id, ORIGIN);
      navigate("/workflows");
    } catch {
      flash("failed to delete");
    }
  }, [navigate]);

  // ---- compile ----
  const compile = React.useCallback(async () => {
    const id = currentIdRef.current;
    if (!id) {
      flash("nothing saved to compile yet");
      return;
    }
    setCompiling(true);
    try {
      // Flush pending edits first — compiling the previous revision and showing
      // its errors against the current drawing is worse than not compiling.
      if (dirtyRef.current) await doSave();
      setCompiled(await workflowApi.compile(id));
    } catch (e) {
      setCompiled({ ok: false, error: e instanceof Error ? e.message : String(e) });
    } finally {
      setCompiling(false);
    }
  }, [doSave]);

  // ---- derived ----
  const selected = nodes.find((n) => n.id === selectedId) || null;
  const selectedIsSubtask = !!selected && isSubtask(selected.data as NodeData);
  const knownPhases = React.useMemo(
    () => phasesOf(nodes as Array<{ id: string; data?: NodeData }>),
    [nodes],
  );
  const issues = React.useMemo(
    () => graphIssues(nodes as Array<{ id: string; data?: NodeData }>, edges, roleIds),
    [nodes, edges, roleIds],
  );

  const saveBadge =
    saveState === "saving" ? "saving…" : saveState === "saved" ? "saved" : saveState === "error" ? "save failed" : "";

  return (
    <NodeViewContext.Provider value={nodeView}>
    <BridgeContext.Provider value={nodeBridge}>
      <div className="fd-root wf-root">
        <div className="fd-topbar">
          <button className="fd-tbtn" onClick={() => navigate("/workflows")}>
            ‹ Workflows
          </button>
          <input
            className="fd-nameinput"
            value={name}
            aria-label="Workflow name"
            onChange={(e) => {
              setName(e.target.value);
              markDirty();
            }}
          />
          <span className={"fd-savebadge " + saveState}>{saveBadge}</span>
          <button className="fd-tbtn" onClick={addSubtask}>
            + Part
          </button>
          <button className="fd-tbtn" onClick={addNote}>
            + Note
          </button>
          <button className="fd-tbtn" onClick={autoLayout} disabled={nodes.length === 0}>
            Auto-layout
          </button>
          <button
            className="fd-tbtn"
            onClick={compile}
            disabled={compiling}
            data-testid="wf-compile"
          >
            {compiling ? "Compiling…" : "Compile"}
          </button>
          <span style={{ flex: 1 }} />
          {msg && <span className="fd-savebadge">{msg}</span>}
          <button className="fd-tbtn danger" onClick={deleteWorkflow}>
            Delete
          </button>
        </div>

        <div className="fd-wrap">
          <div className="fd-canvas">
            <FlowCanvas
              nodes={nodes}
              edges={edges}
              nodeTypes={WF_NODE_TYPES}
              edgeTypes={EDGE_TYPES}
              accent={ACCENT}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodesDelete={() => markDirty()}
              onNodeDragStop={() => markDirty()}
              onNodeClick={(_e, n) => setSelectedId(n.id)}
              onPaneClick={() => setSelectedId(null)}
              onEdgeDoubleClick={(_e, edge) => {
                setEdges((es) => es.filter((x) => x.id !== edge.id));
                markDirty();
              }}
              onNodeDoubleClick={(_e, n) => setSelectedId(n.id)}
            />
            {compiled && <PlanPanel result={compiled} onClose={() => setCompiled(null)} />}
          </div>

          <div className="fd-side">
            {selectedIsSubtask && selected ? (
              <>
                <div className="fd-inspector-head">
                  <h1>Part</h1>
                  <div className="fd-sub">
                    {phaseNameOf(
                      nodes as Array<{ id: string; data?: NodeData }>,
                      Number((selected.data as NodeData).phase),
                    )}
                  </div>
                </div>
                <WorkflowInspector
                  nodeId={selected.id}
                  data={selected.data as NodeData}
                  roles={roles}
                  knownPhases={knownPhases}
                  onChange={updateSelected}
                />
              </>
            ) : (
              <>
                <div className="fd-inspector-head">
                  <h1>Workflow</h1>
                  <div className="fd-sub">
                    Drawn, not decomposed — every node is a part brief, every
                    edge a dependency.
                  </div>
                </div>

                <h2>Description</h2>
                <input
                  className="fd-descinput"
                  value={description}
                  aria-label="Workflow description"
                  placeholder="what this workflow is for"
                  onChange={(e) => {
                    setDescription(e.target.value);
                    markDirty();
                  }}
                />

                <h2>Port layout</h2>
                <select
                  className="wf-select"
                  aria-label="Port layout"
                  value={portLayout}
                  onChange={(e) => {
                    setPortLayout(e.target.value === "lr" ? "lr" : "tb");
                    markDirty();
                  }}
                >
                  <option value="tb">Top / bottom — vertical stack</option>
                  <option value="lr">Left / right — horizontal</option>
                </select>
                <div className="wf-hint">
                  A workflow reads top-down by default: step 1 first,
                  dependencies flowing down. Auto-layout follows this setting.
                </div>

                <h2>Steps</h2>
                {knownPhases.length === 0 ? (
                  <div className="fd-placeholder">
                    No parts yet. Add one — it needs a step and a role before
                    it can compile.
                  </div>
                ) : (
                  <div className="wf-rail">
                    {knownPhases.map((p) => {
                      const count = nodes.filter(
                        (n) =>
                          isSubtask(n.data as NodeData) && Number((n.data as NodeData).phase) === p,
                      ).length;
                      return (
                        <span className="wf-rail-item active" key={p}>
                          P{p} · {count}
                        </span>
                      );
                    })}
                  </div>
                )}

                <h2>Validity</h2>
                {issues.length === 0 ? (
                  <div className="fd-sub">
                    No authoring mistakes found. Compile to check against the
                    live role catalogue.
                  </div>
                ) : (
                  <div className="wf-issues" data-testid="wf-graph-issues">
                    <ul>
                      {issues.slice(0, 7).map((i, idx) => (
                        <li key={idx}>{i.message}</li>
                      ))}
                    </ul>
                    {issues.length > 7 && (
                      <div>…and {issues.length - 7} more</div>
                    )}
                  </div>
                )}

                <h2>Select a node</h2>
                <div className="fd-placeholder">
                  Click a part to edit its role, step, prompt and acceptance
                  criteria.
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </BridgeContext.Provider>
    </NodeViewContext.Provider>
  );
}

/** The compile result, in the two shapes it comes in: the phase/subtask
 *  structure the orchestrator would run, or the compiler's refusal. */
function PlanPanel({ result, onClose }: { result: CompileResult; onClose: () => void }) {
  if (!result.ok) {
    return (
      <div className="wf-plan error" data-testid="wf-plan-error">
        <div className="wf-plan-hd">
          <span className="wf-plan-ttl">Will not compile</span>
          <button className="wf-plan-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="wf-plan-err">{result.error}</div>
      </div>
    );
  }
  return (
    <div className="wf-plan" data-testid="wf-plan">
      <div className="wf-plan-hd">
        <span className="wf-plan-ttl">Compiled plan</span>
        <span className="wf-plan-count">
          {result.plan.phases.length} steps · {result.subtask_count} parts
        </span>
        <button className="wf-plan-close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      {result.plan.phases.map((p) => (
        <div className="wf-plan-phase" key={p.id}>
          <div className="wf-plan-phase-hd">
            <span className="wf-plan-st-id">P{p.id}</span>
            <span className="wf-plan-phase-name">{p.name}</span>
            {p.serialize && <span className="wf-chip">serial</span>}
          </div>
          {p.subtasks.map((s) => (
            <div className="wf-plan-st" key={s.id}>
              <span className="wf-plan-st-id">{s.id}</span>
              <span className="wf-plan-st-role">{s.role}</span>
              <span className="wf-plan-st-desc">{s.description}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
