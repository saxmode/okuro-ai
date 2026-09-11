// <!-- AGENT_HEADER
// role: code
// purpose: /work/gantt — portfolio gantt. Reads epics from a flow_designer flow
//   (cheap persistence, decision B3a) and renders them as a time-projection over
//   the shared FlowCanvas. Double-click drills: pending -> intake (stub), running/
//   done -> the orchestrator run at /work/{task_id}.
// AGENT_HEADER_END -->
import { ReactFlowProvider, addEdge, applyNodeChanges, useEdgesState, useNodesState, useReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import React from "react";
import { useNavigate, useSearchParams } from "react-router";

import { FlowCanvas } from "@/components/flow-designer/flow-canvas";
import { GanttBar } from "@/components/flow-designer/gantt-node";
import { TimeRuler } from "@/components/flow-designer/time-axis";
import {
  type GanttZoom,
  BAR_GUTTER,
  BAR_HEIGHT,
  DEP_GAP_DAYS,
  addDays,
  dateToX,
  durationToWidth,
  laneToY,
  startOfDay,
} from "@/components/flow-designer/gantt-scale";
import "@/components/flow-designer/flow-designer.css";
import { flowDesignerApi, ganttApi, taskApi, transcribeAudio, type FlowDesignerSummary, type SuggestedEpic } from "@/lib/api";

const ZOOMS: GanttZoom[] = ["day", "week", "month", "quarter", "year"];
const nodeTypes = { ganttBar: GanttBar };

// orchestrator TaskStatus -> gantt bar status (pending|running|verified|failed)
const TASK_STATUS_TO_GANTT: Record<string, string> = {
  pending: "pending",
  planning: "running",
  active: "running",
  blocked: "running",
  deliberating: "running",
  awaiting_decision: "running",
  waiting_user: "running",
  done: "verified",
  failed: "failed",
  halted: "failed",
};

// Longest-path depth (topological level) per node — the fallback "time column".
// Roots (no incoming edge) = 0; each node = max(predecessor depth) + 1.
function computeDepth(nodes: any[], edges: any[]): Map<string, number> {
  const indeg = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  const adj = new Map<string, string[]>(nodes.map((n) => [n.id, []]));
  for (const e of edges) {
    if (adj.has(e.source) && indeg.has(e.target)) {
      adj.get(e.source)!.push(e.target);
      indeg.set(e.target, indeg.get(e.target)! + 1);
    }
  }
  const depth = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  const left = new Map(indeg);
  const q = nodes.filter((n) => indeg.get(n.id) === 0).map((n) => n.id);
  while (q.length) {
    const id = q.shift()!;
    for (const t of adj.get(id) ?? []) {
      depth.set(t, Math.max(depth.get(t)!, depth.get(id)! + 1));
      left.set(t, left.get(t)! - 1);
      if (left.get(t) === 0) q.push(t);
    }
  }
  return depth;
}

// Map an epic flow's stored nodes -> gantt bar nodes on the time axis. Real epics
// carry start/durationDays/lane; until the decompose agent fills those, fall back
// to a layered layout: x = dependency depth (time column), lane = packed row
// within that column. Reads as a proper left-to-right gantt, not a scatter.
function toGanttNodes(raw: any[], edges: any[], origin: Date, zoom: GanttZoom) {
  const depth = computeDepth(raw, edges);
  const laneAt = new Map<number, number>(); // depth -> next free lane
  return raw.map((n) => {
    const d = n.data || {};
    const durationDays = typeof d.durationDays === "number" ? d.durationDays : 5;
    const lvl = depth.get(n.id) ?? 0;
    const lane = typeof d.lane === "number" ? d.lane : (laneAt.set(lvl, (laneAt.get(lvl) ?? -1) + 1), laneAt.get(lvl)!);
    const start = d.start ? startOfDay(new Date(d.start)) : addDays(origin, lvl * (durationDays + DEP_GAP_DAYS));
    // inset the bar inside its time slot so handles/curves don't hug the boundary
    const x = dateToX(start, origin, zoom) + BAR_GUTTER;
    const width = Math.max(14, durationToWidth(durationDays, zoom) - 2 * BAR_GUTTER);
    return {
      id: n.id,
      type: "ganttBar",
      position: { x, y: laneToY(lane) },
      // node.width (NOT style.width) — style.width pins the CSS width and makes
      // the node un-resizable; node.width is what NodeResizer updates.
      width,
      height: BAR_HEIGHT,
      data: {
        title: d.title ?? d.label ?? n.id,
        color: d.color,
        status: d.status ?? "pending",
        fillPct: d.fillPct ?? 0,
        orchestrator_task_id: d.orchestrator_task_id ?? null,
      },
    };
  });
}

// LLM-suggested epics -> gantt nodes + finish-to-start edges (depth-laid-out).
function epicsToGraph(epics: SuggestedEpic[], origin: Date, zoom: GanttZoom) {
  const rawNodes = epics.map((e) => ({
    id: e.id,
    data: { title: e.title, durationDays: e.durationDays, status: "pending", fillPct: 0 },
  }));
  const edges = epics.flatMap((e) => (e.depends_on || []).map((dep) => ({ id: `${dep}->${e.id}`, source: dep, target: e.id })));
  return { nodes: toGanttNodes(rawNodes, edges, origin, zoom), edges };
}

// live width: NodeResizer writes the new size to measured/width, NOT style.width
// (style.width is only the initial value), so prefer measured/width for resize.
const widthOf = (n: any) => (n.measured?.width as number) ?? (n.width as number) ?? (n.style?.width as number) ?? 160;

// flatten captured Float32 PCM -> mono 16-bit WAV (Groq Whisper accepts WAV).
function encodeWav(chunks: Float32Array[], sampleRate: number): ArrayBuffer {
  let len = 0;
  for (const c of chunks) len += c.length;
  const pcm = new Int16Array(len);
  let o = 0;
  for (const c of chunks)
    for (let i = 0; i < c.length; i++) {
      const s = Math.max(-1, Math.min(1, c[i] ?? 0));
      pcm[o++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const dv = new DataView(buf);
  const ws = (off: number, str: string) => {
    for (let i = 0; i < str.length; i++) dv.setUint8(off + i, str.charCodeAt(i));
  };
  ws(0, "RIFF");
  dv.setUint32(4, 36 + pcm.length * 2, true);
  ws(8, "WAVE");
  ws(12, "fmt ");
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true); // PCM
  dv.setUint16(22, 1, true); // mono
  dv.setUint32(24, sampleRate, true);
  dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true);
  dv.setUint16(34, 16, true);
  ws(36, "data");
  dv.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  return buf;
}

const slugify = (s: string) =>
  s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "untitled";

// serialize gantt bars to a flow_designer graph (cheap persistence) — keep the
// gantt geometry + epic data so reload restores positions, status and task ids.
const serializeNodes = (nodes: any[]) =>
  nodes.map((n) => ({ id: n.id, type: "ganttBar", position: n.position, width: widthOf(n), height: n.height ?? BAR_HEIGHT, data: n.data }));

// Finish-to-start CONSTRAINT (the house model): for every edge A -> B (B relies
// on A), keep A.end <= B.start. The user-manipulated bar(s) are anchors and stay
// put; the OTHER side yields, only on violation, propagating outward:
//   • downstream — a dependent can't start before its predecessor ends
//     (concrete takes longer  -> paint waits, moves right)
//   • upstream   — a predecessor must finish by its dependent's start
//     (painter starts earlier -> concrete starts earlier, moves left)
// Monotonic per direction (push right / pull left) so it converges.
function enforceConstraints(nodes: any[], edges: any[], anchorIds: string[]): any[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const succ = new Map<string, string[]>();
  const pred = new Map<string, string[]>();
  for (const e of edges) {
    (succ.get(e.source) ?? succ.set(e.source, []).get(e.source)!).push(e.target);
    (pred.get(e.target) ?? pred.set(e.target, []).get(e.target)!).push(e.source);
  }
  let mutated = false;
  const queue = [...anchorIds];
  const maxIter = (nodes.length + 1) * (edges.length + 1) + 8;
  let it = 0;
  while (queue.length && it++ < maxIter) {
    const id = queue.shift()!;
    const a = byId.get(id);
    if (!a) continue;
    const aStart = a.position.x;
    const aEnd = a.position.x + widthOf(a);
    for (const tid of succ.get(id) ?? []) {
      const t = byId.get(tid);
      if (t && t.position.x < aEnd) {
        byId.set(tid, { ...t, position: { ...t.position, x: aEnd } });
        mutated = true;
        queue.push(tid);
      }
    }
    for (const pid of pred.get(id) ?? []) {
      const p = byId.get(pid);
      if (p && p.position.x + widthOf(p) > aStart) {
        byId.set(pid, { ...p, position: { ...p.position, x: aStart - widthOf(p) } });
        mutated = true;
        queue.push(pid);
      }
    }
  }
  return mutated ? nodes.map((n) => byId.get(n.id)) : nodes;
}

function Gantt() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const flowId = params.get("flow");
  const zoom = (params.get("zoom") as GanttZoom) || "week";

  const rf = useReactFlow();
  const [flows, setFlows] = React.useState<FlowDesignerSummary[]>([]);
  const [nodes, setNodes] = useNodesState<any>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<any>([]);
  const [origin] = React.useState(() => startOfDay(new Date()));
  const [accent, setAccent] = React.useState("#ffffff");
  // shared hover link between the gantt bars (top) and the task list (bottom)
  const [hoverId, setHoverId] = React.useState<string | null>(null);
  // describe -> LLM-suggested gantt (P1)
  const [desc, setDesc] = React.useState("");
  const [suggesting, setSuggesting] = React.useState(false);
  const [suggestMsg, setSuggestMsg] = React.useState("");
  // intake drawer (P2): per-epic context capture (text + RAG pre-fill + dictation)
  const [intakeNode, setIntakeNode] = React.useState<any>(null);
  const [intakeText, setIntakeText] = React.useState("");
  const [intakeCtx, setIntakeCtx] = React.useState("");
  const [recording, setRecording] = React.useState(false);
  const captureRef = React.useRef<null | { stop: () => void }>(null);
  // results drawer (P4): a bar with a task shows its run status + artifacts inline
  const [resultNode, setResultNode] = React.useState<any>(null);
  const [resultState, setResultState] = React.useState<any>(null);
  const [artifactText, setArtifactText] = React.useState<{ name: string; text: string } | null>(null);

  React.useEffect(() => {
    const a = getComputedStyle(document.documentElement).getPropertyValue("--color-accent").trim();
    if (a) setAccent(a);
  }, []);

  // load the flow list; default to the first flow when none is selected
  React.useEffect(() => {
    flowDesignerApi.list().then((r) => {
      setFlows(r.flows);
      const first = r.flows[0];
      if (!flowId && first) setParams({ flow: first.id, zoom }, { replace: true });
    });
  }, []);

  // load the selected flow's graph. Persisted gantts (gantt-* / settings.gantt)
  // already store gantt geometry -> use as-is. Architecture flows -> project onto
  // the time axis via the depth layout.
  React.useEffect(() => {
    if (!flowId) return;
    flowDesignerApi.get(flowId).then((detail) => {
      // strip editor handle ids — GanttBar has a single default source/target,
      // so the original "out"/"in" handle refs would leave edges unrendered.
      const e = ((detail.graph?.edges as any[]) || []).map((x) => ({ id: x.id, source: x.source, target: x.target }));
      const raw = (detail.graph?.nodes as any[]) || [];
      const isGantt = flowId.startsWith("gantt-") || (detail.graph?.settings as any)?.gantt;
      setEdges(e);
      setNodes(isGantt ? raw : toGanttNodes(raw, e, origin, zoom));
    });
  }, [flowId, zoom, origin, setNodes, setEdges]);

  // persist a gantt to a flow_designer flow (create-or-update at a stable id)
  const saveGantt = React.useCallback(
    (id: string, name: string, ns: any[], es: any[]) =>
      flowDesignerApi.upsert(id, { name, description: "okuro gantt", graph: { nodes: serializeNodes(ns), edges: es, settings: { gantt: true } } }),
    [],
  );

  // autosave edits to persisted gantts (drag/resize/run/add/connect) — debounced,
  // and ONLY for gantt-* flows so architecture diagrams are never clobbered.
  const saveTimer = React.useRef<number | undefined>(undefined);
  React.useEffect(() => {
    if (!flowId?.startsWith("gantt-") || !nodes.length) return;
    window.clearTimeout(saveTimer.current);
    const name = flows.find((f) => f.id === flowId)?.name || flowId;
    saveTimer.current = window.setTimeout(() => void saveGantt(flowId, name, nodes, edges).catch(() => {}), 800);
    return () => window.clearTimeout(saveTimer.current);
  }, [nodes, edges, flowId, flows, saveGantt]);

  // edges via a ref so the constraint solver always reads the LATEST edges —
  // a stable handler avoids the first-interaction-after-load reading empty edges.
  const edgesRef = React.useRef(edges);
  edgesRef.current = edges;

  // live status: poll the orchestrator tasks behind running bars and flip their
  // status + fill. Keyed on the running-task SET (not all of `nodes`) so the
  // interval is only re-created when a bar starts/finishes — not every tick.
  const nodesRef = React.useRef(nodes);
  nodesRef.current = nodes;
  const runningKey = React.useMemo(
    () => nodes.filter((n) => n.data?.orchestrator_task_id && n.data?.status === "running").map((n) => n.data.orchestrator_task_id).sort().join(","),
    [nodes],
  );
  React.useEffect(() => {
    if (!runningKey) return;
    let alive = true;
    const poll = async () => {
      const running = nodesRef.current.filter((n) => n.data?.orchestrator_task_id && n.data?.status === "running");
      const ups = await Promise.all(
        running.map(async (n) => {
          try {
            const s = await taskApi.getState(n.data.orchestrator_task_id);
            return { id: n.id, status: TASK_STATUS_TO_GANTT[s.status] || "running", fill: s.progress_percent ?? 0 };
          } catch {
            return null;
          }
        }),
      );
      if (!alive) return;
      setNodes((cur) =>
        cur.map((n) => {
          const u = ups.find((x) => x && x.id === n.id);
          if (!u) return n;
          const fill = u.status === "verified" ? 100 : u.fill;
          return u.status !== n.data?.status || fill !== n.data?.fillPct ? { ...n, data: { ...n.data, status: u.status, fillPct: fill } } : n;
        }),
      );
    };
    poll();
    const iv = window.setInterval(poll, 5000);
    return () => {
      alive = false;
      window.clearInterval(iv);
    };
  }, [runningKey, setNodes]);

  // Apply the change, then repair finish-to-start constraints from the moved
  // bar(s): dependents pushed right / predecessors pulled left, only on overlap.
  const handleNodesChange = React.useCallback(
    (changes: any[]) => {
      setNodes((prev) => {
        const result = applyNodeChanges(changes, prev);
        const anchors = [...new Set(changes.filter((c) => c.type === "position" || c.type === "dimensions").map((c) => c.id))];
        return anchors.length ? enforceConstraints(result, edgesRef.current, anchors) : result;
      });
    },
    [setNodes],
  );

  // Display-only edge styling: red+animated on a finish->start conflict (dependent
  // starts before predecessor ends); accent+animated for edges touching the
  // selected bar (its relations light up with marching ants).
  const accentHex = React.useMemo(() => getComputedStyle(document.documentElement).getPropertyValue("--color-accent").trim() || "#ffffff", []);
  const styledEdges = React.useMemo(() => {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    return edges.map((e) => {
      const s = byId.get(e.source);
      const t = byId.get(e.target);
      const conflict = s && t && t.position.x < s.position.x + widthOf(s);
      if (conflict) return { ...e, style: { stroke: "#ef4444", strokeWidth: 2 }, animated: true };
      if (s?.selected || t?.selected) return { ...e, style: { stroke: accentHex, strokeWidth: 2.5 }, animated: true };
      return e;
    });
  }, [nodes, edges, accentHex]);

  // Add a new epic bar on its own lane at the origin. Connect it to others by
  // dragging from its connector dot (hover a bar). Session-local until persisted.
  const epicSeq = React.useRef(0);
  const addEpic = React.useCallback(() => {
    setNodes((cur) => {
      const lane = cur.length;
      const id = `epic-${++epicSeq.current}`;
      return [
        ...cur,
        {
          id,
          type: "ganttBar",
          position: { x: dateToX(origin, origin, zoom) + BAR_GUTTER, y: laneToY(lane) },
          width: Math.max(14, durationToWidth(5, zoom) - 2 * BAR_GUTTER),
          height: BAR_HEIGHT,
          data: { title: "New epic", status: "pending", fillPct: 0 },
        },
      ];
    });
  }, [setNodes, origin, zoom]);

  // open the intake drawer for an epic; pull RAG context for it (Tier 2)
  const openIntake = React.useCallback(
    (n: any) => {
      setIntakeNode(n);
      setIntakeText((n.data?.context_notes || []).map((c: any) => c.text).join("\n"));
      setIntakeCtx("");
      ganttApi
        .context({ query: n.data?.title || n.id, project: flowId || undefined })
        .then((r) => setIntakeCtx(r.context || "(no related memory)"))
        .catch(() => setIntakeCtx("(memory lookup failed)"));
    },
    [flowId],
  );

  // a bar with a task -> show its run status + artifacts inline (P4)
  const openResults = React.useCallback((n: any) => {
    setResultNode(n);
    setResultState(null);
    setArtifactText(null);
    taskApi
      .getState(n.data.orchestrator_task_id)
      .then(setResultState)
      .catch(() => setResultState({ status: "unavailable", artifacts: [], recent_logs: [] }));
  }, []);

  const onNodeDoubleClick = React.useCallback(
    (_e: React.MouseEvent | null, n: any) => {
      if (n.data?.orchestrator_task_id) {
        openResults(n); // P4: inline run results
      } else {
        openIntake(n); // P2: capture context before it becomes a run
      }
    },
    [openIntake, openResults],
  );

  // dictation: capture mic via Web Audio (NOT MediaRecorder — WebKitGTK's is a
  // no-op without GStreamer muxers) -> encode WAV in JS -> backend Groq Whisper.
  const toggleRecord = React.useCallback(async () => {
    if (recording) {
      captureRef.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const AC: typeof AudioContext = (window as any).AudioContext || (window as any).webkitAudioContext;
      const ctx = new AC();
      const source = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      const chunks: Float32Array[] = [];
      processor.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      source.connect(processor);
      processor.connect(ctx.destination); // keeps the graph running (output stays silent)
      ctx.resume?.().catch(() => {}); // autoplay-policy safety (we're inside a click)

      captureRef.current = {
        stop: () => {
          captureRef.current = null;
          setRecording(false);
          try {
            processor.disconnect();
            source.disconnect();
          } catch {
            /* ignore */
          }
          stream.getTracks().forEach((t) => t.stop());
          const wav = encodeWav(chunks, ctx.sampleRate);
          ctx.close().catch(() => {});
          if (wav.byteLength <= 44) {
            window.alert("No audio captured — speak for a moment before stopping, and check the mic input.");
            return;
          }
          transcribeAudio(new Blob([wav], { type: "audio/wav" }))
            .then((text) => text && setIntakeText((t) => (t ? t + " " : "") + text))
            .catch((e) => window.alert("Dictation failed: " + (e as Error).message));
        },
      };
      setRecording(true);
    } catch (e) {
      window.alert("Mic unavailable: " + (e as Error).message);
    }
  }, [recording]);

  const saveIntake = React.useCallback(() => {
    if (!intakeNode) return;
    const text = intakeText.trim();
    setNodes((cur) => cur.map((n) => (n.id === intakeNode.id ? { ...n, data: { ...n.data, context_notes: text ? [{ text, source: "text" }] : [] } } : n)));
    setIntakeNode(null);
  }, [intakeNode, intakeText, setNodes]);

  // P3: turn the epic + its 3-tier context into a real orchestrator task.
  const [running, setRunning] = React.useState(false);
  const runEpic = React.useCallback(async () => {
    if (!intakeNode || running) return;
    setRunning(true);
    try {
      const title = intakeNode.data?.title || intakeNode.id;
      const ctx = intakeText.trim();
      const bg = intakeCtx && !intakeCtx.startsWith("(") ? intakeCtx : "";
      const description = [title, ctx && `Context:\n${ctx}`, bg && `Background:\n${bg}`].filter(Boolean).join("\n\n");
      const res = await taskApi.create({ description, skip_intake: true });
      if (!res.task_id) {
        window.alert("Task not created: " + res.status);
        return;
      }
      setNodes((cur) =>
        cur.map((n) =>
          n.id === intakeNode.id
            ? { ...n, data: { ...n.data, context_notes: ctx ? [{ text: ctx, source: "text" }] : [], orchestrator_task_id: res.task_id, status: "running" } }
            : n,
        ),
      );
      setIntakeNode(null);
    } catch (e) {
      window.alert("Run failed: " + (e as Error).message);
    } finally {
      setRunning(false);
    }
  }, [intakeNode, running, intakeText, intakeCtx, setNodes]);

  // mark the hovered bar so it highlights in the canvas (synced with the list)
  const displayNodes = React.useMemo(
    () => nodes.map((n) => ({ ...n, className: n.id === hoverId ? "fd-gbar-hl" : undefined })),
    [nodes, hoverId],
  );

  // pan the canvas to centre a bar (list row -> gantt)
  const focusBar = React.useCallback(
    (n: any) => rf.setCenter(n.position.x + widthOf(n) / 2, n.position.y + BAR_HEIGHT / 2, { zoom: rf.getZoom(), duration: 250 }),
    [rf],
  );

  // task list = the epics themselves (1 epic = 1 run), ordered by start
  const rows = React.useMemo(() => [...nodes].sort((a, b) => a.position.x - b.position.x), [nodes]);

  // describe a project -> LLM suggests a high-level gantt (replaces the canvas)
  const onSuggest = React.useCallback(async () => {
    if (!desc.trim() || suggesting) return;
    setSuggesting(true);
    setSuggestMsg("starting…");
    try {
      const name = desc.trim().slice(0, 60);
      const { epics } = await ganttApi.suggest({ description: desc.trim(), name }, setSuggestMsg);
      const g = epicsToGraph(epics, origin, zoom);
      setEdges(g.edges);
      setNodes(g.nodes);
      // persist immediately so it survives reload + appears in the selector
      const id = "gantt-" + slugify(name);
      await saveGantt(id, name, g.nodes, g.edges);
      setParams({ flow: id, zoom }, { replace: true });
      flowDesignerApi.list().then((r) => setFlows(r.flows));
      window.setTimeout(() => rf.fitView({ duration: 300, padding: 0.2 }), 120);
    } catch (e) {
      window.alert("Suggest failed: " + (e as Error).message);
    } finally {
      setSuggesting(false);
      setSuggestMsg("");
    }
  }, [desc, suggesting, origin, zoom, setEdges, setNodes, setParams, rf, saveGantt]);

  return (
    <div className="flex h-full w-full flex-col bg-[var(--color-surface)]">
      <div className="flex items-center gap-3 border-b border-[var(--color-border-subtle)] px-4 py-2 text-sm">
        <span className="font-semibold">WORK · Gantt</span>
        <select
          className="rounded border border-[var(--color-border)] bg-transparent px-2 py-1"
          value={flowId || ""}
          onChange={(e) => setParams({ flow: e.target.value, zoom }, { replace: true })}
        >
          {flows.map((f) => (
            <option key={f.id} value={f.id}>
              {f.name}
            </option>
          ))}
        </select>
        <select
          className="rounded border border-[var(--color-border)] bg-transparent px-2 py-1"
          value={zoom}
          onChange={(e) => setParams({ flow: flowId || "", zoom: e.target.value }, { replace: true })}
        >
          {ZOOMS.map((z) => (
            <option key={z} value={z}>
              {z}
            </option>
          ))}
        </select>
        <button
          className="rounded border border-[var(--color-border)] px-2 py-1 hover:border-[var(--color-accent)]"
          onClick={addEpic}
        >
          + Epic
        </button>
        <span className="ml-auto text-[var(--color-fg-muted)]">drag a bar's dot to connect · double-click → intake / run</span>
      </div>
      {/* describe a project -> LLM-suggested gantt (P1) */}
      <div className="flex items-center gap-2 border-b border-[var(--color-border-subtle)] px-4 py-2">
        <input
          className="flex-1 rounded border border-[var(--color-border)] bg-transparent px-3 py-1.5 text-sm"
          placeholder="Describe a project → okuro suggests a high-level gantt…"
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSuggest()}
          disabled={suggesting}
        />
        <button
          className="rounded bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-[var(--color-surface)] disabled:opacity-50"
          onClick={onSuggest}
          disabled={suggesting || !desc.trim()}
        >
          {suggesting ? "Planning…" : "✨ Suggest"}
        </button>
      </div>
      {/* live inference activity toast (streamed from the LLM call) */}
      {suggesting && (
        <div className="fd-suggest-toast">
          <span className="fd-suggest-spin" />
          <span className="truncate">{suggestMsg || "working…"}</span>
        </div>
      )}
      {/* top: compact gantt */}
      <div className="relative flex-[3] overflow-hidden border-b border-[var(--color-border-subtle)]">
        <FlowCanvas
          mode="gantt"
          nodes={displayNodes}
          edges={styledEdges}
          nodeTypes={nodeTypes}
          edgeTypes={{}}
          accent={accent}
          ganttZoom={zoom}
          onNodesChange={handleNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={(c) => setEdges((eds) => addEdge(c, eds))}
          onNodesDelete={() => {}}
          onNodeDragStop={() => {}}
          onNodeClick={() => {}}
          onPaneClick={() => {}}
          onEdgeDoubleClick={() => {}}
          onNodeDoubleClick={onNodeDoubleClick}
          onNodeMouseEnter={(_e, n) => setHoverId(n.id)}
          onNodeMouseLeave={() => setHoverId(null)}
        />
        <TimeRuler origin={origin} zoom={zoom} />
      </div>
      {/* bottom: tasks for this gantt (1 epic = 1 run -> these rows ARE the tasks).
          Hover a row to highlight + centre its bar; hover a bar to highlight its row. */}
      <div className="flex-[2] overflow-auto">
        <div className="px-4 py-1.5 text-2xs uppercase tracking-wider text-[var(--color-fg-subtle)]">
          Tasks · {rows.length}
        </div>
        {rows.map((n) => (
          <div
            key={n.id}
            className={"fd-tlrow" + (hoverId === n.id ? " hl" : "")}
            onMouseEnter={() => {
              setHoverId(n.id);
              focusBar(n);
            }}
            onMouseLeave={() => setHoverId(null)}
            onClick={() => onNodeDoubleClick(null as any, n)}
          >
            <span className={"fd-tldot fd-gbar-" + (n.data?.status || "pending")} />
            <span className="fd-tltitle">{n.data?.title || n.id}</span>
            <span className="fd-tlmeta">{n.data?.status || "pending"}</span>
          </div>
        ))}
      </div>

      {/* intake drawer (P2): capture context for an epic before it becomes a run */}
      {intakeNode && (
        <>
          <div className="fd-intake-scrim" onClick={() => setIntakeNode(null)} />
          <div className="fd-intake">
            <div className="fd-intake-hd">
              <span className="font-semibold">{intakeNode.data?.title || intakeNode.id}</span>
              <button className="text-[var(--color-fg-muted)]" onClick={() => setIntakeNode(null)}>
                ✕
              </button>
            </div>
            <div className="fd-intake-body">
              <div className="fd-intake-sec">From memory (auto)</div>
              <pre className="fd-intake-ctx">{intakeCtx || "loading…"}</pre>
              <div className="fd-intake-sec">Your context</div>
              <textarea
                className="fd-intake-ta"
                placeholder="What matters for this step? Constraints, links, decisions…"
                value={intakeText}
                onChange={(e) => setIntakeText(e.target.value)}
              />
              <div className="flex items-center gap-2">
                <button className={"fd-intake-mic" + (recording ? " rec" : "")} onClick={toggleRecord}>
                  {recording ? "◼ Stop" : "🎙 Dictate"}
                </button>
                <span className="text-2xs text-[var(--color-fg-subtle)]">{recording ? "recording…" : "speech → text (Groq)"}</span>
              </div>
            </div>
            <div className="fd-intake-ft">
              <button className="fd-intake-mic" onClick={saveIntake}>
                Save
              </button>
              <button className="fd-intake-save" onClick={runEpic} disabled={running}>
                {running ? "Starting…" : "▶ Run"}
              </button>
            </div>
          </div>
        </>
      )}

      {/* results drawer (P4): a bar with a task -> live status + artifacts inline */}
      {resultNode && (
        <>
          <div className="fd-intake-scrim" onClick={() => setResultNode(null)} />
          <div className="fd-intake">
            <div className="fd-intake-hd">
              <span className="font-semibold">{resultNode.data?.title || resultNode.id}</span>
              <button className="text-[var(--color-fg-muted)]" onClick={() => setResultNode(null)}>
                ✕
              </button>
            </div>
            <div className="fd-intake-body">
              {!resultState ? (
                <div className="text-sm text-[var(--color-fg-muted)]">loading…</div>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <span className={"fd-tldot fd-gbar-" + (resultNode.data?.status || "pending")} />
                    <span className="text-sm">{resultState.status}</span>
                    <span className="ml-auto text-2xs text-[var(--color-fg-subtle)]">{resultState.progress_percent ?? 0}%</span>
                  </div>
                  <div className="fd-intake-sec">Artifacts</div>
                  {(resultState.artifacts || []).length ? (
                    resultState.artifacts.map((a: any) => (
                      <button
                        key={a.name}
                        className="fd-art-row"
                        onClick={() =>
                          taskApi
                            .getArtifact(resultNode.data.orchestrator_task_id, a.name)
                            .then((text) => setArtifactText({ name: a.name, text }))
                            .catch(() => {})
                        }
                      >
                        <span className="fd-tltitle">{a.title || a.name}</span>
                        <span className="fd-tlmeta">{Math.max(1, Math.round((a.size_bytes || 0) / 1024))}k</span>
                      </button>
                    ))
                  ) : (
                    <div className="text-xs text-[var(--color-fg-subtle)]">none yet</div>
                  )}
                  {artifactText && (
                    <>
                      <div className="fd-intake-sec">{artifactText.name}</div>
                      <pre className="fd-intake-ctx">{artifactText.text.slice(0, 4000)}</pre>
                    </>
                  )}
                </>
              )}
            </div>
            <div className="fd-intake-ft">
              <button className="fd-intake-mic" onClick={() => navigate(`/work/${resultNode.data.orchestrator_task_id}`)}>
                Open full run →
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export function WorkGanttPage() {
  return (
    <ReactFlowProvider>
      <Gantt />
    </ReactFlowProvider>
  );
}
