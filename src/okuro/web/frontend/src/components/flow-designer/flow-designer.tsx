// @ts-nocheck
// <!-- AGENT_HEADER
// role: code
// purpose: okuro-flow — the canvas App. Ported from tools/flow-designer; localStorage
//   + file persistence replaced by backend autosave + SSE live-sync. parentNode -> parentId (v12).
// AGENT_HEADER_END -->
import {
  MarkerType,
  addEdge,
  useEdgesState,
  useNodesState,
  useReactFlow,
  useUpdateNodeInternals,
} from "@xyflow/react";
import React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiError, flowDesignerApi, flowDesignerEventsUrl } from "@/lib/api";
import { useChrome } from "@/lib/chrome-context";
import { registerSnapshotContext } from "@/lib/handover-context";
import { useIsMobile } from "@/hooks/use-is-mobile";
import { drawStream, takeStreamDraw } from "@/lib/flow-stream";

import {
  CAT,
  EDGE_STYLES,
  N,
  ORIENTS,
  PALETTE,
  SIDES,
  TPLN,
  TYPES,
  assemble,
  cleanNode,
  dagreLayout,
  fitContainers,
  foldEdge,
  mergePorts,
  mh,
  migrateGroupEdges,
  migrateNode,
  migrateNodePorts,
  mw,
  portDir,
  portSide,
  portsOf,
  unfoldEdge,
  withRel,
} from "./graph";
import { FlowCanvas } from "./flow-canvas";
import { useGraphEditor } from "./use-graph-editor";
import { FloatingToolbar } from "./flow-toolbar";
import { BridgeContext, FlowNode, GroupNode, LabeledEdge, NOOP_BRIDGE, PortSelContext, vDefault } from "./nodes";
import { flowToMermaid } from "./flow-to-mermaid";
import { HandoverDialog } from "@/components/handover/handover-dialog";
import { MermaidViewer } from "@/components/mermaid/mermaid-viewer";

const NEW_NAME = "Untitled flow";
// How an edge looks with NOTHING selected. Focus mode derives its dimmed and
// highlighted variants from these at render time; save and load reset to them,
// so one viewer's selection is never written into the document.
const BASE_EDGE_OPACITY = 0.92;
const BASE_EDGE_WIDTH = 1.7;
const rid = () => Math.random().toString(36).slice(2, 10);
// Figma-style WASD alignment (multi-select): align each node to the extreme edge.
// Keyed by e.code (physical key) — macOS Option+letter mutates e.key into a
// special char (å/ß/∑/∂), so e.key would never match here.
const ALIGN_KEYS = { KeyA: "left", KeyD: "right", KeyW: "top", KeyS: "bottom" };
// Clockwise port rotation: each bullet advances one quarter-turn. Unset sides
// resolve through the node's orient first (see graph.ts::portSide) then step.
const ROT_SIDE = { right: "bottom", bottom: "left", left: "top", top: "right" };

// Thin delegate — the shared MermaidViewer (zoom/pan + scratch source editor,
// still lazy-loading mermaid into its own chunk) now backs the Mermaid tab.
function MermaidView({ code, accent }: { code: string; accent?: string }) {
  return <MermaidViewer code={code} accent={accent} />;
}

// thin wrapper: render an okuro Select from a value + [value,label] options.
// value="" / undefined shows the placeholder (radix forbids empty-string items).
function FSelect({ value, onValueChange, options, placeholder, className, title }) {
  return (
    <Select value={value || undefined} onValueChange={onValueChange}>
      <SelectTrigger size="sm" className={className || "w-full"} title={title}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => {
          const [v, l] = Array.isArray(o) ? o : [o, o];
          return (
            <SelectItem key={v} value={v}>
              {l}
            </SelectItem>
          );
        })}
      </SelectContent>
    </Select>
  );
}

export function FlowDesigner({ flowId, embed = false }: { flowId: string | null; embed?: boolean }) {
  // Inline-edit callbacks handed to the nodes through BridgeContext.
  //
  // Two layers on purpose. The SLOTS are filled by plain assignment further
  // down, at each handler's own definition site, so every callback keeps
  // closing over the current render exactly as it did when these lived on a
  // module-level object. The PROVIDED value is a stable set of thunks that read
  // through the ref, so supplying it never re-renders every node. What changed
  // versus the old global is scope alone: this ref belongs to one FlowDesigner
  // instance, so a second editor on the same page can no longer clobber it.
  const bridgeRef = React.useRef({ ...NOOP_BRIDGE });
  const bridge = bridgeRef.current;
  const bridgeValue = React.useMemo(
    () =>
      Object.fromEntries(
        Object.keys(NOOP_BRIDGE).map((k) => [k, (...args) => bridgeRef.current[k](...args)]),
      ),
    [],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selId, setSelId] = React.useState(null);
  const [msg, setMsg] = React.useState("");
  const [estyle, setEstyle] = React.useState("default");
  // Tablet multi-select mode: one-finger drag draws a selection box instead of
  // panning the canvas (pinch-zoom + two-finger pan stay active either way).
  const [multiSelect, setMultiSelect] = React.useState(false);
  // Port selected (via long-press) for re-placement: {nodeId, io, portId} | null.
  const [selPort, setSelPort] = React.useState(null);
  const [portMode, setPortMode] = React.useState(() => {
    try {
      return localStorage.getItem("fd-mode") || "quad";
    } catch (e) {
      return "quad";
    }
  });
  const flash = (m) => {
    setMsg(m);
    setTimeout(() => setMsg(""), 1500);
  };
  // resolved accent hex — canvas SVG fills (edges, minimap) can't read CSS vars
  const ACCENT = React.useMemo(() => {
    try {
      return getComputedStyle(document.documentElement).getPropertyValue("--color-accent").trim() || "#8ff0a4";
    } catch (e) {
      return "#8ff0a4";
    }
  }, []);
  const idc = React.useRef(1);
  const fileRef = React.useRef(null);
  const nodesRef = React.useRef(nodes);
  nodesRef.current = nodes;
  const edgesRef = React.useRef(edges);
  edgesRef.current = edges;

  // L2 snapshot context — hand over the live graph (nodes + edges), not just
  // pixels, so a captured flow snapshot carries the editable structure. Refs are
  // stable; the builder reads the current graph at capture time.
  React.useEffect(() => {
    return registerSnapshotContext(() => ({
      entity: { type: "flow", id: flowId },
      data: { nodes: nodesRef.current, edges: edgesRef.current },
    }));
  }, [flowId]);
  const clip = React.useRef(null);
  const pastRef = React.useRef([]),
    futureRef = React.useRef([]),
    lastRef = React.useRef(""),
    applyingRef = React.useRef(false),
    htimer = React.useRef(null);
  const rf = useReactFlow ? useReactFlow() : null;
  // live handler table for the global keydown listener (empty-deps effect) — kept
  // current each render so shortcuts never fire a stale closure. See doSaveRef.
  const actionsRef = React.useRef({});
  // edge stroke is a pure function of its source node's colour (there is no
  // per-edge colour picker) — outgoing connections inherit the source node.
  const nodeColor = React.useCallback(
    (id, list) => (list || nodesRef.current).find((n) => n.id === id)?.data?.color || ACCENT,
    [ACCENT],
  );
  const selCount = React.useMemo(() => nodes.filter((n) => n.selected).length, [nodes]);
  const sel = React.useMemo(() => {
    const s = nodes.filter((n) => n.selected);
    return s.length === 1 ? s[0] : null;
  }, [nodes]);
  const multi = selCount > 1;
  // id of the currently selected edge (canvas edge-click sets data.__sel) — lets
  // the floating toolbar's delete act on a connection, not just nodes.
  const selEdgeId = React.useMemo(() => edges.find((e) => e.data && e.data.__sel)?.id || null, [edges]);
  const [sideW, setSideW] = React.useState(() => {
    try {
      const v = +localStorage.getItem("fd-sidew");
      return v >= 240 && v <= 720 ? v : 300;
    } catch (e) {
      return 300;
    }
  });
  // Side panel collapse — the inspector is auto-hidden by default and opens on
  // node select (see the selection effect below). Starts collapsed so a fresh
  // canvas is full-width; a manual toggle (top-right) can pin it open/closed.
  const [sideCollapsed, setSideCollapsed] = React.useState(true);
  // Side-panel section collapse — open/closed per section id, persisted so a
  // power user's layout survives reloads. Defaults resolved at render time.
  const [secOpen, setSecOpen] = React.useState(() => {
    try {
      return JSON.parse(localStorage.getItem("fd-sections") || "{}") || {};
    } catch (e) {
      return {};
    }
  });
  const toggleSec = React.useCallback((id, val) => {
    setSecOpen((m) => {
      const next = { ...m, [id]: val };
      try {
        localStorage.setItem("fd-sections", JSON.stringify(next));
      } catch (e) {
        /* ignore */
      }
      return next;
    });
  }, []);
  // App chrome (left pulse panel + top nav) is owned by the shell — hidden via
  // the pulse-sidepanel toggle, not from here. The flow only reads chrome state
  // (Esc restore) and restores it on unmount; the inspector is a separate control.
  const chrome = useChrome();
  // Phone-sized viewport: gate the auto-open inspector so a tap (which is also
  // the start of a drag-to-rearrange) doesn't slam a panel over the canvas.
  const isMobile = useIsMobile();
  // Restore chrome when leaving the flow page so other pages keep their chrome.
  React.useEffect(() => () => chrome.show(), [chrome.show]);
  // Esc exits full-screen — a safety hatch since the nav is hidden while in it.
  React.useEffect(() => {
    if (!chrome.hidden) return;
    const onEsc = (e) => {
      if (e.key === "Escape") chrome.show();
    };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [chrome.hidden, chrome.show]);
  // Auto-hide inspector: on desktop, open it the moment a node is selected and
  // collapse it when the selection clears. On mobile the auto-open is suppressed
  // — a selection is also the start of a drag-to-rearrange, so the panel would
  // cover the canvas; the user opens it deliberately via the toolbar Settings
  // button instead. Either way, clearing the selection always collapses it.
  React.useEffect(() => {
    if (selCount === 0) setSideCollapsed(true);
    else if (!isMobile) setSideCollapsed(false);
  }, [selCount, isMobile]);
  // Hide all port (connection-dot) labels — declutters the canvas; handles stay live.
  const [hidePortLabels, setHidePortLabels] = React.useState(() => {
    try {
      return localStorage.getItem("fd-hide-plabels") === "1";
    } catch (e) {
      return false;
    }
  });
  // View mode — "flow" (interactive canvas) | "mermaid" (read-only rendered diagram).
  const [view, setView] = React.useState("flow");
  const [mermaidCode, setMermaidCode] = React.useState("");

  // ---- chat-to-draw: prompt okuro's agent to draw/edit the flow ----
  const [chatOpen, setChatOpen] = React.useState(false);
  const [chatPrompt, setChatPrompt] = React.useState("");
  const [chatBusy, setChatBusy] = React.useState(false);
  const [chatLog, setChatLog] = React.useState([]); // {kind, text}[]
  const chatLogRef = React.useRef(null);
  const pushLog = (kind, text) => setChatLog((l) => [...l.slice(-79), { kind, text }]);
  React.useEffect(() => {
    if (chatLogRef.current) chatLogRef.current.scrollTop = chatLogRef.current.scrollHeight;
  }, [chatLog, chatOpen]);

  // ---- okuro-flow: backend state ----
  const origin = React.useRef(rid()).current;
  const [flowList, setFlowList] = React.useState([]);
  const [currentId, setCurrentId] = React.useState(flowId || null);
  const [flowName, setFlowName] = React.useState(NEW_NAME);
  const [streamStatus, setStreamStatus] = React.useState(null); // phase text, or null
  const [streamElapsed, setStreamElapsed] = React.useState(0); // seconds
  const drawCountRef = React.useRef(0);
  const streamTimerRef = React.useRef(null);
  const [flowDesc, setFlowDesc] = React.useState("");
  // Hand-over: Content IR for the shared HandoverDialog, built from the
  // current node/edge selection when "Hand over" is clicked — null closes it.
  const [handoverContent, setHandoverContent] = React.useState(null);
  // Boot gate: autosave is armed 120ms AFTER the first load settles, so nothing
  // the canvas does while mounting looks like a user edit. /flow-specific —
  // this editor marks dirty from an effect over the graph, so it needs a window
  // in which that effect is inert.
  const bootedRef = React.useRef(false);
  // Late-bound for the coordinator: serialize and adoptDoc are defined further
  // down (adoptDoc needs the coordinator's own bookkeeping).
  const serializeRef = React.useRef(() => ({ nodes: [], edges: [] }));
  const adoptRef = React.useRef(() => {});
  // The write coordinator — shared with /workflows. Debounce, one save in
  // flight, the empty-canvas clobber guard, base_rev and 409 adoption.
  const editor = useGraphEditor({
    api: {
      create: (body) => flowDesignerApi.create(body),
      upsert: (id, body) => flowDesignerApi.upsert(id, body),
    },
    origin,
    defaultName: NEW_NAME,
    initialId: flowId || null,
    serialize: () => serializeRef.current(),
    getName: () => flowNameRef.current,
    getDescription: () => flowDescRef.current,
    getNodeCount: () => nodesRef.current.length,
    adopt: (d) => adoptRef.current(d),
    flash: (m) => flash(m),
    conflictMessage: "reloaded — this flow was changed elsewhere",
    // currentId is also STATE here, because the flow picker renders from it.
    // The ref is authoritative; this keeps the picker honest.
    onSaved: (d) => {
      setCurrentId(d.id);
      refreshList();
    },
  });
  const {
    saveState,
    setSaveState,
    scheduleSave,
    doSave,
    markDirty,
    adoptMeta,
    resetMeta,
    clearTimer,
    currentIdRef,
    loadingRef,
    dirtyRef,
    loadGenRef,
  } = editor;

  React.useEffect(() => {
    setEdges((es) => es.map((e) => ({ ...e, type: "labeled", data: { ...(e.data || {}), pstyle: estyle } })));
  }, [estyle]);
  const edgeTypes = React.useMemo(() => ({ labeled: LabeledEdge }), []);
  const normEdge = (e) => ({
    ...e,
    type: "labeled",
    // opacity/strokeWidth are FORCED to base, not merged from the stored edge:
    // documents written before focus mode became derived carry a previous
    // viewer's dimming, and merging would keep rendering it forever.
    style: {
      ...(e.style || {}),
      stroke: (e.style && e.style.stroke) || ACCENT,
      opacity: BASE_EDGE_OPACITY,
      strokeWidth: BASE_EDGE_WIDTH,
    },
    animated: false,
    zIndex: 0,
    markerEnd: e.markerEnd || { type: MarkerType.ArrowClosed, color: ACCENT },
    data: { ...(e.data || {}), pstyle: (e.data && e.data.pstyle) || (e.type && e.type !== "labeled" ? e.type : estyle), __editing: undefined },
  });
  // The look of an edge with NOTHING selected. Focus mode is derived from these,
  // and both save and load reset to them, so a document never carries one
  // viewer's selection.
  const stripFocusEdge = (e) => ({
    ...e,
    animated: false,
    zIndex: 0,
    style: { ...(e.style || {}), opacity: BASE_EDGE_OPACITY, strokeWidth: BASE_EDGE_WIDTH },
  });
  const stripFocusNode = (n) => {
    if (!n.style || n.style.opacity === undefined) return n;
    const { opacity, ...rest } = n.style;
    return { ...n, style: rest };
  };
  // Drop transient edge-editor flags AND focus decoration on the way out.
  const stripEd = (t) => ({
    nodes: t.nodes.map(stripFocusNode),
    edges: t.edges
      .map((e) => (e.data ? { ...e, data: { ...e.data, __editing: undefined, __sel: undefined } } : e))
      .map(stripFocusEdge),
  });

  // ---- focus mode ----
  //
  // DERIVED, never stored. This used to be an effect that wrote the dimming
  // straight into `nodes` and `edges` state — and those fields are serialized,
  // so merely CLICKING a node changed the document: a full save, a history
  // snapshot, and whatever was selected at the time baked permanently into the
  // stored edge style. Two users clicking one flow could 409 each other without
  // either of them editing anything.
  //
  // Selection is a property of the VIEWER, not of the drawing, so it is applied
  // on the way to the canvas and never on the way to the server. Nothing else
  // changed: the numbers below are the ones the effect used.
  const focusNodes = React.useMemo(() => {
    if (!selId) return nodes;
    const conn = new Set([selId]);
    edges.forEach((e) => {
      if (e.source === selId) conn.add(e.target);
      if (e.target === selId) conn.add(e.source);
    });
    return nodes.map((n) => ({ ...n, style: { ...n.style, opacity: conn.has(n.id) ? 1 : 0.22 } }));
  }, [nodes, edges, selId]);

  const focusEdges = React.useMemo(
    () =>
      edges.map((e) => {
        const isc = !!selId && (e.source === selId || e.target === selId);
        return {
          ...e,
          animated: isc,
          zIndex: isc ? 20 : 0,
          style: {
            ...e.style,
            opacity: selId ? (isc ? 1 : 0.16) : BASE_EDGE_OPACITY,
            strokeWidth: isc ? 2.6 : BASE_EDGE_WIDTH,
          },
        };
      }),
    [edges, selId],
  );

  // keep edge stroke in sync with source-node colour — single source of truth so
  // recolour / load / paste / group-member changes all propagate to outgoing edges.
  React.useEffect(() => {
    setEdges((es) => {
      let changed = false;
      const next = es.map((e) => {
        const col = nodeColor(e.source, nodes);
        if ((e.style && e.style.stroke) === col && e.markerEnd && e.markerEnd.color === col) return e;
        changed = true;
        return {
          ...e,
          style: { ...(e.style || {}), stroke: col },
          markerEnd: { ...(e.markerEnd || { type: MarkerType.ArrowClosed }), color: col },
        };
      });
      return changed ? next : es;
    });
  }, [nodes, nodeColor]);

  // explicit edge selection (drives the waypoint handles) — does not rely on
  // ReactFlow's internal `selected`, which the okuro canvas doesn't surface.
  const selectEdge = React.useCallback((id) => {
    setSelId(null);
    setEdges((es) => es.map((e) => ({ ...e, selected: e.id === id, data: { ...(e.data || {}), __sel: e.id === id || undefined } })));
  }, []);
  const clearEdgeSel = React.useCallback(() => {
    setEdges((es) =>
      es.some((e) => e.data && e.data.__sel)
        ? es.map((e) => (e.data && e.data.__sel ? { ...e, selected: false, data: { ...e.data, __sel: undefined } } : e))
        : es,
    );
  }, []);

  const delEdge = (id) => setEdges((es) => es.filter((e) => e.id !== id));
  bridge.EDGE_EDIT = (id) => setEdges((es) => es.map((e) => ({ ...e, data: { ...(e.data || {}), __editing: e.id === id } })));
  bridge.EDGE_SET = (id, v) =>
    setEdges((es) => es.map((e) => (e.id === id ? { ...e, data: { ...(e.data || {}), label: (v || "").trim() || undefined, __editing: false } } : e)));
  // Q2 spike: draggable waypoints. Component owns add/move/remove, persists the
  // final array here. Empty/undefined clears back to the auto-routed path.
  bridge.EDGE_WAYPOINTS = (id, wps) =>
    setEdges((es) =>
      es.map((e) => (e.id === id ? { ...e, data: { ...(e.data || {}), waypoints: wps && wps.length ? wps : undefined } } : e)),
    );
  const nodeTitle = (id) => {
    const n = nodes.find((x) => x.id === id);
    return n ? n.data.title || id : id;
  };
  const updateNodeInternals = useUpdateNodeInternals();
  const refresh = (id) => setTimeout(() => updateNodeInternals(id), 0);
  const fitSoon = () => {
    if (rf)
      setTimeout(() => {
        try {
          rf.fitView({ duration: 300, padding: 0.2 });
        } catch (e) {}
      }, 40);
  };
  const nodeTypes = React.useMemo(
    () => ({
      node: (props) => <FlowNode {...props} mode={portMode} />,
      group: (props) => <GroupNode {...props} mode={portMode} />,
    }),
    [portMode],
  );
  React.useEffect(() => {
    try {
      localStorage.setItem("fd-mode", portMode);
    } catch (e) {}
    const t = setTimeout(() => {
      nodes.forEach((n) => {
        try {
          updateNodeInternals(n.id);
        } catch (e) {}
      });
    }, 60);
    return () => clearTimeout(t);
  }, [portMode]);

  const onConnect = React.useCallback(
    (c) => {
      const col = nodeColor(c.source);
      setEdges((es) =>
        addEdge(
          {
            ...c,
            type: "labeled",
            data: { pstyle: estyle },
            style: { stroke: col, strokeWidth: 1.7, opacity: 0.92 },
            markerEnd: { type: MarkerType.ArrowClosed, color: col },
          },
          es,
        ),
      );
    },
    [estyle, nodeColor],
  );
  const onNodesDelete = React.useCallback((dels) => {
    const ids = new Set(dels.map((d) => d.id));
    setEdges((es) => es.filter((e) => !ids.has(e.source) && !ids.has(e.target)));
  }, []);

  // ---- copy / paste ----
  const doCopy = () => {
    const s = nodesRef.current.filter((n) => n.selected);
    if (!s.length) return false;
    const ids = new Set(s.map((n) => n.id));
    const inner = edgesRef.current.filter((e) => ids.has(e.source) && ids.has(e.target));
    clip.current = {
      nodes: s.map((n) => JSON.parse(JSON.stringify({ ...n, selected: false }))),
      edges: inner.map((e) => JSON.parse(JSON.stringify(e))),
      shift: 0,
    };
    flash("copied " + s.length + " node" + (s.length > 1 ? "s" : ""));
    return true;
  };
  const doPaste = () => {
    const c = clip.current;
    if (!c || !c.nodes.length) return false;
    c.shift = (c.shift || 0) + 40;
    const idmap = {},
      portRemap = {};
    const newNodes = c.nodes.map((orig) => {
      const nn = JSON.parse(JSON.stringify(orig));
      const nid = nn.id + "-c" + idc.current++;
      idmap[nn.id] = nid;
      nn.id = nid;
      nn.selected = true;
      nn.position = { x: (nn.position ? nn.position.x : 0) + c.shift, y: (nn.position ? nn.position.y : 0) + c.shift };
      if (nn.style) nn.style.opacity = undefined;
      if (nn.type === "group" && nn.data) {
        const memMap = {};
        (nn.data.members || []).forEach((m) => {
          memMap[m.id] = m.id + "-c" + idc.current++;
        });
        nn.data.members = (nn.data.members || []).map((m) => ({ ...m, id: memMap[m.id] }));
        nn.data.innerEdges = (nn.data.innerEdges || []).map((e) => ({
          ...e,
          id: "e" + idc.current++,
          source: memMap[e.source] || e.source,
          target: memMap[e.target] || e.target,
        }));
        const pr = {};
        const fix = (p) => {
          const nm = memMap[p.m] || p.m;
          const np = { ...p, m: nm, id: p.id.slice(0, 3) + nm + "__" + p.mh };
          pr[p.id] = np.id;
          return np;
        };
        nn.data.ports = portsOf(nn.data).map(fix);
        delete nn.data.ins;
        delete nn.data.outs;
        portRemap[orig.id] = pr;
      }
      return nn;
    });
    const newEdges = c.edges.map((e) => {
      let sh = e.sourceHandle,
        th = e.targetHandle;
      if (portRemap[e.source] && portRemap[e.source][sh]) sh = portRemap[e.source][sh];
      if (portRemap[e.target] && portRemap[e.target][th]) th = portRemap[e.target][th];
      return { ...e, id: "e" + idc.current++, source: idmap[e.source] || e.source, target: idmap[e.target] || e.target, sourceHandle: sh, targetHandle: th };
    });
    setNodes((ns) => ns.map((n) => ({ ...n, selected: false })).concat(newNodes));
    setEdges((es) => es.concat(newEdges));
    newNodes.forEach((n) => refresh(n.id));
    setTimeout(() => setNodes((ns) => fitContainers(ns)), 80);
    flash("pasted " + newNodes.length + " node" + (newNodes.length > 1 ? "s" : ""));
    return true;
  };

  // Cmd+D-style duplicate: copy the selection then paste it (offset + reselected).
  const doDuplicate = () => {
    if (!doCopy()) {
      flash("select a node first");
      return;
    }
    doPaste();
  };

  // Trash the selected node(s) and any edges touching them — or, when only a
  // connection is selected, delete that edge. Nodes take priority when both a
  // node and an edge somehow carry a selection flag.
  const deleteSel = () => {
    const ids = new Set(nodesRef.current.filter((n) => n.selected).map((n) => n.id));
    if (ids.size) {
      setEdges((es) => es.filter((e) => !ids.has(e.source) && !ids.has(e.target)));
      setNodes((ns) => ns.filter((n) => !ids.has(n.id)));
      setSelId(null);
      flash("deleted " + ids.size + " node" + (ids.size > 1 ? "s" : ""));
      return;
    }
    const eid = edgesRef.current.find((e) => e.data && e.data.__sel)?.id;
    if (eid) {
      delEdge(eid);
      flash("deleted connection");
      return;
    }
    flash("select a node or connection first");
  };

  // Rotate every port on the selected node(s) one quarter-turn clockwise, so a
  // right-side bullet lands on the bottom, then left, then top, then right.
  // Applies to nodes and groups alike (both keep one ordered data.ports list).
  // Each port's CURRENT side is resolved through the node's orient first, so a
  // rotation of a vertical node steps from where it is actually drawn.
  const rotatePorts = () => {
    const ids = new Set(nodesRef.current.filter((n) => n.selected).map((n) => n.id));
    if (!ids.size) {
      flash("select a node first");
      return;
    }
    setNodes((ns) =>
      ns.map((n) =>
        ids.has(n.id)
          ? {
              ...n,
              data: {
                ...n.data,
                ports: portsOf(n.data).map((p) => ({ ...p, side: ROT_SIDE[portSide(p, n.data.orient)] })),
              },
            }
          : n,
      ),
    );
    ids.forEach((id) => refresh(id));
    flash("rotated ports");
  };

  // Flatten selected nodes into a readable markdown outline — mirrors the
  // backend's ContentIR._subgraph_to_md so a flow→notes/prism handover still
  // carries a body even though this path builds the IR client-side.
  const subgraphToMd = (selNodes) => {
    const lines = [];
    for (const n of selNodes) {
      const data = (n && n.data) || {};
      const title = data.title || data.tag || "";
      const sub = data.sub || "";
      const body = data.body || "";
      if ((data.cat === "title" || data.cat === "mdnote") && body) {
        lines.push(body);
        continue;
      }
      if (title) lines.push("- **" + title + "**" + (sub ? " — " + sub : ""));
    }
    return lines.join("\n").trim();
  };

  // "Hand over →": lift the selected nodes + the edges between them into a
  // Content IR and open the shared HandoverDialog (see components/handover).
  const openHandover = () => {
    const selNodes = nodesRef.current.filter((n) => n.selected);
    if (!selNodes.length) {
      flash("select a node first");
      return;
    }
    const ids = new Set(selNodes.map((n) => n.id));
    const selEdges = edgesRef.current.filter((e) => ids.has(e.source) && ids.has(e.target));
    setHandoverContent({
      kind: "subgraph",
      title: flowName || "Flow selection",
      body_md: subgraphToMd(selNodes),
      structured: {
        nodes: selNodes.map((n) => ({ ...n, selected: false })),
        edges: selEdges,
      },
      source: { tool: "flow", id: currentId || undefined, label: flowName || undefined },
    });
  };

  // ---- undo / redo ----
  const cleanState = () => ({
    nodes: nodesRef.current.map((n) => {
      const { selected, ...r } = n;
      return { ...r, style: n.style ? { ...n.style, opacity: undefined } : n.style };
    }),
    edges: edgesRef.current.map((e) => (e.data ? { ...e, data: { ...e.data, __editing: undefined } } : e)),
  });
  const commitHistory = () => {
    const key = JSON.stringify(cleanState());
    if (applyingRef.current) {
      lastRef.current = key;
      applyingRef.current = false;
      return;
    }
    if (key === lastRef.current) return;
    if (lastRef.current) {
      pastRef.current.push(lastRef.current);
      if (pastRef.current.length > 20) pastRef.current.shift();
      futureRef.current = [];
    }
    lastRef.current = key;
  };
  const applySnap = (str) => {
    const s = JSON.parse(str);
    applyingRef.current = true;
    lastRef.current = str;
    setNodes((s.nodes || []).map((n) => ({ ...n, selected: false })));
    setEdges(s.edges || []);
    setSelId(null);
  };
  const undo = () => {
    if (!pastRef.current.length) {
      flash("nothing to undo");
      return;
    }
    futureRef.current.push(JSON.stringify(cleanState()));
    if (futureRef.current.length > 20) futureRef.current.shift();
    applySnap(pastRef.current.pop());
    flash("undo · " + pastRef.current.length + " left");
  };
  const redo = () => {
    if (!futureRef.current.length) {
      flash("nothing to redo");
      return;
    }
    pastRef.current.push(JSON.stringify(cleanState()));
    if (pastRef.current.length > 20) pastRef.current.shift();
    applySnap(futureRef.current.pop());
    flash("redo");
  };
  React.useEffect(() => {
    lastRef.current = JSON.stringify(cleanState());
  }, []);
  React.useEffect(() => {
    if (htimer.current) clearTimeout(htimer.current);
    htimer.current = setTimeout(commitHistory, 300);
  }, [nodes, edges]);

  const onNodeDragStop = React.useCallback(() => setNodes((ns) => fitContainers(ns)), []);

  React.useEffect(() => {
    // Figma-style align modifier differs by OS: Option(Alt) on macOS, Ctrl on
    // Windows, Super(Meta) on Linux — so the chord feels native everywhere.
    const ua = (typeof navigator !== "undefined" && navigator.userAgent) || "";
    const isMac = /Mac/i.test(ua);
    const isWin = /Win/i.test(ua);
    const onKey = (e) => {
      const t = e.target,
        tag = (t && t.tagName) || "";
      if (t && (t.isContentEditable || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT")) return;
      // Space is the canvas pan-activation key (flow-canvas panActivationKeyCode).
      // React Flow reads it off window without preventDefault, so the browser
      // also scrolls the page and keyboard-activates whatever shell control
      // still holds focus — lighting its focus-visible underline (the stray
      // green tab line). Swallow the default, blur any focused control outside
      // the flow root, and flag .fd-panning so the Space+drag pan can't start a
      // native text selection (selection stays free the rest of the time — the
      // class is cleared on keyup below). Editable targets already returned
      // above, so node-label spaces are unaffected.
      if (e.code === "Space") {
        e.preventDefault();
        const fdRoot = document.querySelector(".fd-root");
        const ae = document.activeElement;
        if (ae && ae !== document.body && !(fdRoot && fdRoot.contains(ae)) && typeof (ae as HTMLElement).blur === "function") {
          (ae as HTMLElement).blur();
        }
        if (fdRoot) fdRoot.classList.add("fd-panning");
        return;
      }
      const a = actionsRef.current;
      const k = (e.key || "").toLowerCase();
      // align chord (multi-select). When fewer than 2 are selected it falls
      // through so the OS default (e.g. Ctrl+A select-all) still works.
      const alignMod = isMac ? e.altKey : isWin ? e.ctrlKey : e.metaKey;
      if (alignMod && ALIGN_KEYS[e.code]) {
        if (a.alignSel(ALIGN_KEYS[e.code])) e.preventDefault();
        return;
      }
      if (e.metaKey || e.ctrlKey) {
        if (k === "z") {
          e.preventDefault();
          if (e.shiftKey) a.redo();
          else a.undo();
        } else if (k === "y") {
          e.preventDefault();
          a.redo();
        } else if (k === "c") {
          a.doCopy();
        } else if (k === "v") {
          if (a.doPaste()) e.preventDefault();
        } else if (k === "g") {
          e.preventDefault();
          if (e.shiftKey) a.ungroupSel();
          else a.groupSelected();
        } else if (k === "i") {
          e.preventDefault();
          a.addPortSel("in");
        } else if (k === "o") {
          e.preventDefault();
          a.addPortSel("out");
        }
        return;
      }
      // plain keys: per-node visibility toggles on the selected node(s)
      if (e.altKey) return;
      if (k === "c") {
        if (a.togglePart("hideChip")) e.preventDefault();
      } else if (k === "t") {
        if (a.togglePart("hideTitle")) e.preventDefault();
      } else if (k === "d") {
        if (a.togglePart("hideSub")) e.preventDefault();
      }
    };
    // Clear the pan flag when Space is released (or focus is lost mid-pan, e.g.
    // alt-tab) so text selection is restored immediately.
    const onKeyUp = (e) => {
      if (e.code === "Space") document.querySelector(".fd-root")?.classList.remove("fd-panning");
    };
    const onBlur = () => document.querySelector(".fd-root")?.classList.remove("fd-panning");
    window.addEventListener("keydown", onKey);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
  }, []);

  const addNode = (k) => {
    const t = TPLN[k];
    const id = k + "_" + idc.current++;
    setNodes((ns) =>
      ns.concat(
        N(id, 120 + Math.random() * 340, 90 + Math.random() * 230, t[0], t[1], t[2], t[3], t[4].map((p) => ({ ...p }))),
      ),
    );
  };

  // Click the + on an empty port → spawn a connected "process" node on that
  // side, wired into a matching port on the OPPOSITE side of the new node.
  // dir "source" (output) → new node gets an input on the opposite side and the
  // edge runs orig→new; dir "target" (input) → new node gets an output and the
  // edge runs new→orig.
  const OPP_SIDE = { left: "right", right: "left", top: "bottom", bottom: "top" };
  const portAdd = (nodeId, portId, dir, side) => {
    const src = nodesRef.current.find((n) => n.id === nodeId);
    if (!src) return;
    const W = mw(src, 230);
    const H = mh(src, 110);
    const GAP = 80;
    const NW = 230;
    const NH = 110;
    const opp = OPP_SIDE[side] || "left";
    let x = src.position.x;
    let y = src.position.y;
    if (side === "right") x += W + GAP;
    else if (side === "left") x -= NW + GAP;
    else if (side === "bottom") y += H + GAP;
    else if (side === "top") y -= NH + GAP;
    const nid = "process_" + idc.current++;
    const pid = "p" + idc.current++;
    const newIsTarget = dir === "source";
    const port = { id: pid, label: newIsTarget ? "in" : "out", t: "flow", dir: newIsTarget ? "in" : "out", side: opp };
    const node = N(nid, x, y, "process", "PROC", "Process", "", [port]);
    const conn = newIsTarget
      ? { source: nodeId, sourceHandle: portId, target: nid, targetHandle: pid }
      : { source: nid, sourceHandle: pid, target: nodeId, targetHandle: portId };
    const col = nodeColor(conn.source);
    setNodes((ns) => ns.map((n) => ({ ...n, selected: false })).concat({ ...node, selected: true }));
    setEdges((es) =>
      addEdge(
        { ...conn, type: "labeled", data: { pstyle: estyle }, style: { stroke: col, strokeWidth: 1.7, opacity: 0.92 }, markerEnd: { type: MarkerType.ArrowClosed, color: col } },
        es,
      ),
    );
    setSelId(nid);
    refresh(nid);
    flash("added connected node");
  };
  bridge.PORT_ADD = portAdd;
  // long-press a port → select it; the toolbar then shows side-placement icons.
  bridge.PORT_SELECT = (nodeId, portId) => {
    setSelPort({ nodeId, portId });
    setSelId(nodeId);
    flash("port selected — pick a side");
  };
  // move the long-press-selected port to another side.
  const placePort = (side) => {
    if (!selPort) return;
    const node = nodesRef.current.find((n) => n.id === selPort.nodeId);
    const i = portsOf(node?.data).findIndex((p) => p.id === selPort.portId);
    if (i < 0) return;
    setSide(selPort.nodeId, i, side);
    flash("moved to " + side);
  };
  // current side of the selected port (to highlight the active placement icon).
  const selPortSide = (() => {
    if (!selPort) return null;
    const node = nodes.find((n) => n.id === selPort.nodeId);
    const p = portsOf(node?.data).find((q) => q.id === selPort.portId);
    return p ? portSide(p, node?.data?.orient) : null;
  })();

  const autoLayout = () => {
    setNodes((ns) => dagreLayout(ns, edges));
    flash("auto-laid out");
    fitSoon();
  };

  // ---- grouping ----
  const groupSelected = () => {
    const memberSet = new Set(nodes.filter((n) => n.selected && !n.parentId).map((n) => n.id));
    if (memberSet.size < 2) {
      flash("select 2+ nodes (shift-drag a box)");
      return;
    }
    const members = nodes.filter((n) => memberSet.has(n.id)).map(cleanNode);
    const inner = edges.filter((e) => memberSet.has(e.source) && memberSet.has(e.target));
    const innerKey = new Set(inner.map((e) => e.id));
    const refIn = new Set(),
      refOut = new Set();
    edges.forEach((e) => {
      const sm = memberSet.has(e.source),
        tm = memberSet.has(e.target);
      if (tm && !sm) refIn.add(e.target + "__" + e.targetHandle);
      if (sm && !tm) refOut.add(e.source + "__" + e.sourceHandle);
    });
    const ports = assemble(members, inner, refIn, refOut);
    const ax = members.reduce((s, m) => s + m.position.x, 0) / members.length;
    const ay = members.reduce((s, m) => s + m.position.y, 0) / members.length;
    const gid = "group_" + idc.current++;
    const gnode = {
      id: gid,
      type: "group",
      position: { x: ax, y: ay },
      data: { title: "Group", tag: "GRP", cat: "group", collapsed: true, ports, members: withRel(members), innerEdges: inner },
    };
    const newEdges = edges.filter((e) => !innerKey.has(e.id)).map((e) => foldEdge(e, gid, memberSet));
    setNodes((ns) => ns.filter((n) => !memberSet.has(n.id)).map((n) => ({ ...n, selected: false })).concat({ ...gnode, selected: true }));
    setEdges(newEdges);
    setSelId(gid);
    refresh(gid);
    flash("grouped " + members.length + " nodes");
  };

  const ungroup = (gid) => {
    const g = nodes.find((n) => n.id === gid);
    if (!g || g.type !== "group") return;
    if (g.data.collapsed === false) {
      collapseGroup(gid);
      return;
    }
    const members = (g.data.members || []).map((m) => {
      const { _rel, ...rest } = m;
      return { ...rest, position: { x: g.position.x + (_rel ? _rel.x : 0), y: g.position.y + (_rel ? _rel.y : 0) }, selected: false };
    });
    const newEdges = edges.map((e) => unfoldEdge(e, gid)).concat(g.data.innerEdges || []);
    setNodes((ns) => ns.filter((n) => n.id !== gid).concat(members));
    setEdges(newEdges);
    setSelId(null);
    members.forEach((m) => refresh(m.id));
    flash("ungrouped");
  };

  const expandGroup = (gid) => {
    const g = nodes.find((n) => n.id === gid);
    if (!g || g.type !== "group" || g.data.collapsed === false) return;
    const M = (g.data.members || []).map((m) => {
      const { _rel, ...r } = m;
      return r;
    });
    if (!M.length) {
      flash("empty group");
      return;
    }
    const pad = 20,
      head = 34,
      NW = 250,
      NH = 150,
      gcol = g.data.color;
    const xs = M.map((m) => (m.position ? m.position.x : 0)),
      ys = M.map((m) => (m.position ? m.position.y : 0));
    const minX = Math.min(...xs),
      minY = Math.min(...ys),
      maxX = Math.max(...xs),
      maxY = Math.max(...ys);
    const cW = maxX - minX + NW,
      cH = maxY - minY + NH;
    const W = Math.max(320, cW + pad * 2),
      H = Math.max(190, cH + pad * 2 + head);
    const offX = pad + Math.max(0, (W - pad * 2 - cW) / 2),
      offY = head + pad + Math.max(0, (H - head - pad * 2 - cH) / 2);
    const children = M.map((m) => ({
      ...m,
      parentId: gid,
      selected: false,
      data: gcol ? { ...m.data, color: gcol } : m.data,
      position: { x: (m.position ? m.position.x : 0) - minX + offX, y: (m.position ? m.position.y : 0) - minY + offY },
    }));
    const cw = g.measured?.width || g.width || 230,
      ch = g.measured?.height || g.height || 110,
      cx = g.position.x + cw / 2,
      cy = g.position.y + ch / 2;
    const container = { ...g, position: { x: cx - W / 2, y: cy - H / 2 }, style: { ...(g.style || {}), width: W, height: H }, data: { ...g.data, collapsed: false } };
    const newEdges = edges.map((e) => unfoldEdge(e, gid)).concat(g.data.innerEdges || []);
    setNodes((ns) => ns.filter((n) => n.id !== gid).map((n) => ({ ...n, selected: false })).concat([{ ...container, selected: true }, ...children]));
    setEdges(newEdges);
    setSelId(gid);
    children.forEach((m) => refresh(m.id));
    flash("expanded " + M.length + " nodes");
  };

  const foldOne = (ns, es, gid) => {
    const cont = ns.find((n) => n.id === gid);
    if (!cont) return { nodes: ns, edges: es };
    const kids = ns.filter((n) => n.parentId === gid);
    const set = new Set(kids.map((k) => k.id));
    const members = kids.map((k) => {
      const { parentId, extent, selected, ...r } = k;
      return cleanNode(r);
    });
    const inner = es.filter((e) => set.has(e.source) && set.has(e.target));
    const ik = new Set(inner.map((e) => e.id));
    const refIn = new Set(),
      refOut = new Set();
    es.forEach((e) => {
      const sm = set.has(e.source),
        tm = set.has(e.target);
      if (tm && !sm) refIn.add(e.target + "__" + e.targetHandle);
      if (sm && !tm) refOut.add(e.source + "__" + e.sourceHandle);
    });
    const ports = mergePorts(assemble(members, inner, refIn, refOut), portsOf(cont.data));
    const cW = (cont.style && cont.style.width) || 320,
      cH = (cont.style && cont.style.height) || 190;
    const cx = cont.position.x + cW / 2,
      cy = cont.position.y + cH / 2;
    const gnode = {
      ...cont,
      position: { x: cx - 115, y: cy - 55 },
      style: { ...(cont.style || {}), width: undefined, height: undefined },
      data: { ...cont.data, collapsed: true, members: withRel(members), innerEdges: inner, ports },
    };
    const nedges = es.filter((e) => !ik.has(e.id)).map((e) => foldEdge(e, gid, set));
    return { nodes: ns.filter((n) => n.parentId !== gid && n.id !== gid).concat(gnode), edges: nedges };
  };
  const collapseGroup = (gid) => {
    const r = foldOne(nodes, edges, gid);
    setNodes(r.nodes.map((n) => ({ ...n, selected: n.id === gid })));
    setEdges(r.edges);
    setSelId(gid);
    refresh(gid);
    flash("collapsed");
  };
  bridge.COLLAPSE = collapseGroup;
  const foldAll = () => {
    // Read from refs, NOT closed-over state: serialize() is called from doSave,
    // a useCallback that captures an early-render closure where `nodes` was still
    // empty. Reading state here would serialize a STALE empty graph and autosave
    // a blank canvas over the real one — the original clobber. Refs are live.
    const curNodes = nodesRef.current;
    let ns = curNodes.map(cleanNode),
      es = edgesRef.current;
    curNodes
      .filter((n) => n.type === "group" && n.data.collapsed === false)
      .forEach((c) => {
        const r = foldOne(ns, es, c.id);
        ns = r.nodes;
        es = r.edges;
      });
    return { nodes: ns, edges: es };
  };

  // ---- serialize / apply graph ----
  const serialize = () => {
    const g = stripEd(foldAll());
    let viewport;
    try {
      viewport = rf ? rf.getViewport() : undefined;
    } catch (e) {}
    return { nodes: g.nodes, edges: g.edges, viewport, settings: { portMode, estyle } };
  };
  const applyGraph = (graph) => {
    loadingRef.current = true;
    const g = graph || {};
    if (g.settings) {
      if (g.settings.portMode) setPortMode(g.settings.portMode);
      if (g.settings.estyle) setEstyle(g.settings.estyle);
    }
    // Two load-time normalisations, both for documents written before/outside
    // this client: legacy data.ins/outs become one ordered data.ports (members
    // included), and — defensively — ReactFlow throws (reading 'x' of undefined)
    // if any node lacks a valid position, so a single bad node from an agent/API
    // write would otherwise crash the whole canvas. Coerce a safe default.
    setNodes(
      migrateNodePorts(g.nodes || []).map((n) => ({
        ...n,
        position:
          n.position && typeof n.position.x === "number" && typeof n.position.y === "number"
            ? n.position
            : { x: 0, y: 0 },
        selected: false,
      })),
    );
    setEdges(migrateGroupEdges(g.edges || []).map(normEdge));
    setSelId(null);
    setTimeout(() => {
      if (g.viewport && rf) {
        try {
          rf.setViewport(g.viewport);
        } catch (e) {}
      } else fitSoon();
      loadingRef.current = false;
    }, 60);
  };

  // ---- backend: list / load / save / delete ----
  const refreshList = React.useCallback(async () => {
    try {
      const r = await flowDesignerApi.list();
      setFlowList(r.flows || []);
    } catch (e) {}
  }, []);

  // Apply an already-fetched doc to the canvas. Used by loadFlow and by the
  // 409-conflict reconciler (adopt the server's authoritative version).
  const adoptDoc = (d) => {
    // Identity, rev and the clobber-guard baseline, plus cancellation of
    // anything the previous flow had in flight.
    adoptMeta(d);
    setCurrentId(d.id);
    setFlowName(d.name || NEW_NAME);
    setFlowDesc(d.description || "");
    applyGraph(d.graph);
    try {
      const u = new URL(window.location.href);
      u.searchParams.set("id", d.id);
      window.history.replaceState({}, "", u);
    } catch (e) {}
  };

  serializeRef.current = serialize;
  adoptRef.current = adoptDoc;

  const loadFlow = React.useCallback(async (id) => {
    if (!id) return;
    try {
      const d = await flowDesignerApi.get(id);
      adoptDoc(d);
    } catch (e) {
      flash("failed to load flow");
    }
  }, []);


  const flowNameRef = React.useRef(flowName);
  flowNameRef.current = flowName;
  const flowDescRef = React.useRef(flowDesc);
  flowDescRef.current = flowDesc;

  // Send the prompt to the drawing agent. It generates/edits the graph server-side
  // (origin "agent-chat") and we adopt the saved result — covers both fresh draws
  // and edits of the open flow. Live inference activity streams into the chat log.
  const runChat = React.useCallback(async () => {
    const p = chatPrompt.trim();
    if (!p || chatBusy) return;
    setChatBusy(true);
    pushLog("me", p);
    setChatPrompt("");
    try {
      const r = await flowDesignerApi.chat(
        { prompt: p, id: currentIdRef.current, name: flowNameRef.current, graph: serialize() },
        (msg) => pushLog("dim", msg),
      );
      pushLog("ok", `drew ${r.node_count} node${r.node_count === 1 ? "" : "s"} · ${r.edge_count} edge${r.edge_count === 1 ? "" : "s"}`);
      await loadFlow(r.flow.id);
    } catch (e) {
      pushLog("err", (e && e.message) || String(e));
    } finally {
      setChatBusy(false);
    }
  }, [chatPrompt, chatBusy, loadFlow]);

  // The sidebar chat's `draw` action is registered globally (ChatCapabilities)
  // so it works from any view; the in-canvas "✨ Draw with okuro" dock below
  // still handles live-graph edits on the open canvas.

  // Mark dirty when something PERSISTABLE changed — not merely when a state
  // array got a new identity.
  //
  // /workflows reaches the same place by calling markDirty() at its real
  // mutation sites and filtering selection and measurement out. That does not
  // port to this editor: it has ~49 setNodes/setEdges call sites, and a single
  // one left un-marked is an edit that silently never saves — a worse failure
  // than the no-op revisions this replaces. So the rule is enforced ONCE, on
  // the thing that actually matters: what would be written.
  //
  // The comparison is against the previous render's signature, so it needs no
  // knowledge of save state. Selection no longer even reaches here (focus mode
  // is derived now), and ReactFlow's own select/measure changes serialize
  // identically — so neither can start the clock.
  const prevSigRef = React.useRef(null);
  React.useEffect(() => {
    const g = serialize();
    // Viewport is deliberately excluded: panning is not an edit, and it was not
    // in this effect's dependencies before either.
    const sig = JSON.stringify([g.nodes, g.edges, g.settings, flowName, flowDesc]);
    const prev = prevSigRef.current;
    prevSigRef.current = sig;
    if (!bootedRef.current || loadingRef.current) return;
    if (prev !== null && sig === prev) return; // nothing persistable changed
    markDirty();
  }, [nodes, edges, flowName, flowDesc]);

  const newFlow = () => {
    // Cancels anything the prior flow had pending and clears identity + rev, so
    // a slow response from it can never land on the blank canvas.
    resetMeta();
    setCurrentId(null);
    setFlowName(NEW_NAME);
    setFlowDesc("");
    setNodes([]);
    setEdges([]);
    setSelId(null);
    try {
      const u = new URL(window.location.href);
      u.searchParams.delete("id");
      window.history.replaceState({}, "", u);
    } catch (e) {}
    setTimeout(() => (loadingRef.current = false), 60);
    flash("new flow");
  };

  const deleteFlow = async () => {
    const id = currentIdRef.current;
    if (!id) {
      newFlow();
      return;
    }
    if (!window.confirm("Delete this flow? This cannot be undone.")) return;
    try {
      await flowDesignerApi.remove(id, origin);
      newFlow();
      refreshList();
      flash("deleted");
    } catch (e) {
      flash("delete failed");
    }
  };

  // ---- streaming draw: render the agent's flow node-by-node as it writes ----
  const runStreamDraw = React.useCallback(
    async (drawPrompt) => {
      loadingRef.current = true;
      setCurrentId(null);
      setFlowName(NEW_NAME);
      setNodes([]);
      setEdges([]);
      drawCountRef.current = 0;
      setStreamStatus("waking okuro…");
      setStreamElapsed(0);
      const startedAt = Date.now();
      if (streamTimerRef.current) clearInterval(streamTimerRef.current);
      streamTimerRef.current = setInterval(
        () => setStreamElapsed(Math.round((Date.now() - startedAt) / 1000)),
        500,
      );
      try {
        await drawStream(drawPrompt, {
          onActivity: (msg) => {
            // map the backend phase strings to short labels
            const m = /memory/.test(msg)
              ? "reading okuro memory"
              : /agent/.test(msg)
                ? "okuro is thinking"
                : /saving/.test(msg)
                  ? "saving"
                  : msg;
            setStreamStatus(m);
          },
          onMeta: (m) => {
            if (m.name) setFlowName(m.name);
            setStreamStatus(m.name ? `drawing “${m.name}”` : "drawing the graph");
          },
          onNode: (n) => {
            setStreamStatus("drawing the graph");
            // The backend draw pipeline emits ports[] now, but migrateNode is
            // kept on the streamed node: it is a no-op on a canonical node and
            // the safety net for a hand-written or replayed legacy one, which
            // applyGraph never sees on this path.
            setNodes((ns) => ns.concat([migrateNode({ type: "node", selected: false, ...n })]));
            if (rf) setTimeout(() => rf.fitView({ duration: 200, padding: 0.25 }), 30);
          },
          onEdge: (e) => setEdges((es) => es.concat([normEdge(e)])),
          onDone: (r) => {
            setCurrentId(r.flow.id);
            if (r.flow.name) setFlowName(r.flow.name);
            try {
              window.history.replaceState(null, "", `/flow?id=${encodeURIComponent(r.flow.id)}`);
            } catch {
              /* ignore */
            }
            if (rf) setTimeout(() => rf.fitView({ duration: 300, padding: 0.2 }), 80);
          },
          onError: (msg) => {
            flash(msg);
            setStreamStatus(null);
          },
        });
      } finally {
        dirtyRef.current = false;
        loadingRef.current = false;
        if (streamTimerRef.current) clearInterval(streamTimerRef.current);
        setStreamStatus(null);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rf],
  );

  // ---- boot: list + (stream-draw | load initial flow), then enable autosave ----
  React.useEffect(() => {
    (async () => {
      await refreshList();
      const pending = takeStreamDraw();
      if (pending) await runStreamDraw(pending);
      else if (flowId) await loadFlow(flowId);
      setTimeout(() => (bootedRef.current = true), 120);
    })();
    // Already on /flow when a new draw is requested: no remount → consume here.
    const onDraw = (e) => {
      const p = takeStreamDraw() || e?.detail?.prompt;
      if (p) runStreamDraw(p);
    };
    window.addEventListener("okuro:flow-draw-stream", onDraw);
    return () => window.removeEventListener("okuro:flow-draw-stream", onDraw);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- SSE live-sync: reload current flow when ANOTHER origin saves it ----
  React.useEffect(() => {
    let es;
    let closed = false;
    (async () => {
      const url = await flowDesignerEventsUrl();
      if (closed || !url) return;
      es = new EventSource(url);
      const onEvt = (ev) => {
        let d;
        try {
          d = JSON.parse(ev.data);
        } catch (e) {
          return;
        }
        refreshList();
        if (d.origin !== origin && d.flow_id === currentIdRef.current && !dirtyRef.current) {
          if (d.kind === "deleted") {
            flash("this flow was deleted elsewhere");
            newFlow();
          } else {
            loadFlow(currentIdRef.current);
            flash("updated by " + (d.origin === "agent" ? "an agent" : "another client"));
          }
        }
      };
      es.addEventListener("saved", onEvt);
      es.addEventListener("deleted", onEvt);
    })();
    return () => {
      closed = true;
      if (es) es.close();
    };
  }, []);

  // ---- inline node editing ----
  const patch = (id, fn) => setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: fn({ ...n.data }) } : n)));
  const setTitle = (id, v) => patch(id, (d) => ({ ...d, title: v }));
  const setTag = (id, v) => patch(id, (d) => ({ ...d, tag: v }));
  const setColor = (id, v) =>
    setNodes((ns) =>
      ns.map((n) => {
        if (n.id === id) {
          const data = { ...n.data, color: v };
          if (n.type === "group" && data.members) data.members = data.members.map((m) => ({ ...m, data: { ...m.data, color: v } }));
          return { ...n, data };
        }
        if (n.parentId === id) return { ...n, data: { ...n.data, color: v } };
        return n;
      }),
    );
  bridge.RENAME = setTitle;
  bridge.SETTAG = setTag;
  const setAlign = (id, h, v) => patch(id, (d) => ({ ...d, align: h, valign: v }));
  const setSub = (id, v) => patch(id, (d) => ({ ...d, sub: v }));
  const setHide = (id, key, v) => patch(id, (d) => ({ ...d, [key]: v }));
  const setBody = (id, v) => patch(id, (d) => ({ ...d, body: v }));
  const setCat = (id, v) => patch(id, (d) => ({ ...d, cat: v }));
  // Node orientation: which sides an UNSET port side resolves to. "v" is the
  // same node turned 90° — ports without an explicit side move with it.
  const setOrient = (id, v) => {
    patch(id, (d) => ({ ...d, orient: v }));
    refresh(id);
  };
  // node style variant: undefined = tinted (default), "solid", "outline".
  const setStyle = (id, v) => patch(id, (d) => ({ ...d, style: v }));
  // ---- ports: ONE ordered list, so every mutation is an edit of data.ports.
  // Index is the position in that list, which is also the handle's position on
  // its side — reordering IS the placement tool, hence movePortTo/movePortBy.
  const setPortAt = (id, i, fn) =>
    patch(id, (d) => {
      const a = portsOf(d).slice();
      if (i < 0 || i >= a.length) return d;
      a[i] = fn(a[i]);
      return { ...d, ports: a };
    });
  const setPort = (id, i, k, v) => setPortAt(id, i, (p) => ({ ...p, [k]: v }));
  const setSide = (id, i, v) => {
    setPort(id, i, "side", v);
    refresh(id);
  };
  // Flipping direction keeps the port where it is in the list. An explicitly
  // chosen side survives; an unset one now resolves to the NEW direction's
  // default for the node's orient, which is the point of the single list.
  const setDir = (id, i, dir) => {
    setPortAt(id, i, (p) => ({ ...p, dir: dir === "in" ? "in" : "out" }));
    refresh(id);
  };
  const newPort = (dir) => ({ id: "p" + idc.current++, label: dir === "in" ? "in" : "out", t: "flow", dir });
  const addPort = (id, dir) => {
    patch(id, (d) => ({ ...d, ports: [...portsOf(d), newPort(dir)] }));
    refresh(id);
  };
  const delPort = (id, i) => {
    patch(id, (d) => {
      const a = portsOf(d).slice();
      a.splice(i, 1);
      return { ...d, ports: a };
    });
    refresh(id);
  };
  const movePortTo = (id, from, to) => {
    if (from === to) return;
    patch(id, (d) => {
      const a = portsOf(d).slice();
      if (from < 0 || from >= a.length) return d;
      const [m] = a.splice(from, 1);
      a.splice(Math.max(0, Math.min(a.length, to)), 0, m);
      return { ...d, ports: a };
    });
    refresh(id);
  };
  // Inspector ↑/↓ — the keyboard/click path to the same reorder as the drag.
  const movePortBy = (id, i, delta) => movePortTo(id, i, i + delta);
  const dragRef = React.useRef(null);

  // side-panel resize
  React.useEffect(() => {
    try {
      localStorage.setItem("fd-sidew", String(sideW));
    } catch (e) {}
  }, [sideW]);
  React.useEffect(() => {
    try {
      localStorage.setItem("fd-side-collapsed", sideCollapsed ? "1" : "0");
    } catch (e) {}
  }, [sideCollapsed]);
  React.useEffect(() => {
    try {
      localStorage.setItem("fd-hide-plabels", hidePortLabels ? "1" : "0");
    } catch (e) {}
  }, [hidePortLabels]);
  const startResize = (e) => {
    e.preventDefault();
    const move = (ev) => {
      let w = window.innerWidth - ev.clientX;
      w = Math.max(240, Math.min(720, w));
      setSideW(w);
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  // ---- multi-select editing ----
  const refreshSel = () => setTimeout(() => nodesRef.current.filter((n) => n.selected).forEach((n) => updateNodeInternals(n.id)), 0);
  const patchSel = (fn) => setNodes((ns) => ns.map((n) => (n.selected ? { ...n, data: fn({ ...n.data }, n) } : n)));
  const mSetColor = (c) =>
    setNodes((ns) =>
      ns.map((n) => {
        if (n.selected) {
          const data = { ...n.data, color: c };
          if (n.type === "group" && data.members) data.members = data.members.map((m) => ({ ...m, data: { ...m.data, color: c } }));
          return { ...n, data };
        }
        return n;
      }),
    );
  const mSetCat = (v) => patchSel((d, n) => (n.type === "group" ? d : { ...d, cat: v }));
  // Multi-select port editing matches ports across nodes by LABEL, so it still
  // groups its two inspector lists by direction — list order is per node and
  // means nothing across a selection.
  const mAddPort = (dir) => {
    patchSel((d) => ({ ...d, ports: [...portsOf(d), newPort(dir)] }));
    refreshSel();
  };
  const mPatchPorts = (dir, label, fn) =>
    patchSel((d) => ({
      ...d,
      ports: portsOf(d).map((p) => (portDir(p) === dir && p.label === label ? fn(p) : p)),
    }));
  const mRenamePort = (dir, oldL, newL) => mPatchPorts(dir, oldL, (p) => ({ ...p, label: newL }));
  const mSetPortField = (dir, label, k, v) => {
    mPatchPorts(dir, label, (p) => ({ ...p, [k]: v }));
    if (k === "side") refreshSel();
  };
  const mDelPort = (dir, label) => {
    patchSel((d) => ({ ...d, ports: portsOf(d).filter((p) => !(portDir(p) === dir && p.label === label)) }));
    refreshSel();
  };
  // Flip direction in place — same as the single-node path, the port keeps its
  // slot in each node's list.
  const mSetDir = (dir, label, to) => {
    if (dir === to) return;
    mPatchPorts(dir, label, (p) => ({ ...p, dir: to === "in" ? "in" : "out" }));
    refreshSel();
  };
  const aggPorts = (dir) => {
    const map = new Map();
    nodesRef.current
      .filter((n) => n.selected)
      .forEach((n) =>
        portsOf(n.data)
          .filter((p) => portDir(p) === dir)
          .forEach((p) => {
            const sd = portSide(p, n.data.orient);
            const e = map.get(p.label) || { label: p.label, t: p.t, side: sd, count: 0, tMixed: false, sideMixed: false };
            if (e.t !== p.t) e.tMixed = true;
            if (e.side !== sd) e.sideMixed = true;
            e.count++;
            map.set(p.label, e);
          }),
      );
    return [...map.values()];
  };

  // c/t/d visibility toggles — operate on every selected node so single + multi
  // behave identically. Returns true when something was selected (to swallow the key).
  const togglePart = (part) => {
    const selNodes = nodesRef.current.filter((n) => n.selected);
    if (!selNodes.length) return false;
    const next = !selNodes[0].data[part];
    setNodes((ns) => ns.map((n) => (n.selected ? { ...n, data: { ...n.data, [part]: next } } : n)));
    return true;
  };
  // ⌘I / ⌘O — add a port to each selected (non-group) node.
  const addPortSel = (dir) => {
    const ids = new Set(nodesRef.current.filter((n) => n.selected && n.type !== "group").map((n) => n.id));
    if (!ids.size) {
      flash("select a node first");
      return;
    }
    setNodes((ns) =>
      ns.map((n) => (ids.has(n.id) ? { ...n, data: { ...n.data, ports: [...portsOf(n.data), newPort(dir)] } } : n)),
    );
    refreshSel();
    flash("+1 " + (dir === "in" ? "input" : "output"));
  };
  // ⌘⇧G — ungroup the selected group.
  const ungroupSel = () => {
    const g = nodesRef.current.find((n) => n.selected && n.type === "group");
    if (g) ungroup(g.id);
    else flash("select a group to ungroup");
  };
  // Figma WASD align — snap every selected node's edge to the selection extreme.
  // Requires 2+ selected; returns true when it acted (to swallow the key).
  const alignSel = (dir) => {
    const sn = nodesRef.current.filter((n) => n.selected);
    if (sn.length < 2) return false;
    let pos;
    if (dir === "left") {
      const m = Math.min(...sn.map((n) => n.position.x));
      pos = (n) => ({ x: m, y: n.position.y });
    } else if (dir === "right") {
      const m = Math.max(...sn.map((n) => n.position.x + mw(n, 230)));
      pos = (n) => ({ x: m - mw(n, 230), y: n.position.y });
    } else if (dir === "top") {
      const m = Math.min(...sn.map((n) => n.position.y));
      pos = (n) => ({ x: n.position.x, y: m });
    } else {
      const m = Math.max(...sn.map((n) => n.position.y + mh(n, 150)));
      pos = (n) => ({ x: n.position.x, y: m - mh(n, 150) });
    }
    const ids = new Set(sn.map((n) => n.id));
    setNodes((ns) => ns.map((n) => (ids.has(n.id) ? { ...n, position: pos(n) } : n)));
    setTimeout(() => setNodes((ns) => fitContainers(ns)), 60);
    flash("aligned " + dir);
    return true;
  };
  // refresh the keydown listener's handler table with this render's live closures.
  actionsRef.current = { undo, redo, doCopy, doPaste, groupSelected, ungroupSel, addPortSel, togglePart, alignSel };

  // One row of the single-node port list. `i` is the index in data.ports, which
  // is also the handle's position on its side — hence the ↑/↓ beside the drag
  // grip: ordering the list is how the author aims a connector.
  const portRow = (p, i, n, full) => (
    <div
      className="fd-pcard"
      key={p.id}
      onDragOver={(e) => {
        e.preventDefault();
      }}
      onDrop={() => {
        const d = dragRef.current;
        if (d != null) movePortTo(sel.id, d, i);
        dragRef.current = null;
      }}
    >
      <div className="r1">
        <span className="drag" draggable="true" onDragStart={() => (dragRef.current = i)} title="drag to reorder">
          ⠿
        </span>
        <span className="dot" style={{ background: sel.data.color || "var(--color-accent)" }}></span>
        {full ? (
          <Input
            className="h-7 flex-1 px-2 text-xs"
            value={p.label}
            onChange={(e) => setPort(sel.id, i, "label", e.target.value)}
          />
        ) : (
          <span className="lblro">{p.label}</span>
        )}
        {full && (
          <>
            <Button
              variant="ghost"
              size="icon-xs"
              title="move earlier"
              disabled={i === 0}
              onClick={() => movePortBy(sel.id, i, -1)}
            >
              ↑
            </Button>
            <Button
              variant="ghost"
              size="icon-xs"
              title="move later"
              disabled={i === n - 1}
              onClick={() => movePortBy(sel.id, i, 1)}
            >
              ↓
            </Button>
            <Button variant="ghost" size="icon-xs" className="text-destructive" onClick={() => delPort(sel.id, i)}>
              ✕
            </Button>
          </>
        )}
      </div>
      <div className="r2">
        {full && (
          <FSelect
            className="h-7 flex-1 text-xs"
            title="direction"
            value={portDir(p)}
            onValueChange={(v) => setDir(sel.id, i, v)}
            options={["in", "out"]}
          />
        )}
        {full && (
          <FSelect
            className="h-7 flex-1 text-xs"
            value={p.t}
            onValueChange={(v) => setPort(sel.id, i, "t", v)}
            options={TYPES}
          />
        )}
        {portMode === "quad" && (
          <FSelect
            className="h-7 flex-1 text-xs"
            title="side"
            value={portSide(p, sel.data.orient)}
            onValueChange={(v) => setSide(sel.id, i, v)}
            options={SIDES}
          />
        )}
      </div>
    </div>
  );
  const mPortRow = (dir, a) => (
    <div className="fd-pcard" key={dir + "_" + a.label}>
      <div className="r1">
        <span className="dot" style={{ background: "var(--color-accent)" }}></span>
        <Input
          className="h-7 flex-1 px-2 text-xs"
          defaultValue={a.label}
          onBlur={(e) => {
            const v = e.target.value;
            if (v !== a.label) mRenamePort(dir, a.label, v);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
          }}
        />
        <span className="cntb" title="ports across selected nodes">
          {a.count}
        </span>
        <Button variant="ghost" size="icon-xs" className="text-destructive" onClick={() => mDelPort(dir, a.label)}>
          ✕
        </Button>
      </div>
      <div className="r2">
        <FSelect
          className="h-7 flex-1 text-xs"
          title="direction"
          value={dir}
          onValueChange={(v) => mSetDir(dir, a.label, v)}
          options={["in", "out"]}
        />
        <FSelect
          className="h-7 flex-1 text-xs"
          placeholder="mixed"
          value={a.tMixed ? undefined : a.t}
          onValueChange={(v) => mSetPortField(dir, a.label, "t", v)}
          options={TYPES}
        />
        {portMode === "quad" && (
          <FSelect
            className="h-7 flex-1 text-xs"
            title="side"
            placeholder="mixed"
            value={a.sideMixed ? undefined : a.side}
            onValueChange={(v) => mSetPortField(dir, a.label, "side", v)}
            options={SIDES}
          />
        )}
      </div>
    </div>
  );

  const saveBadge =
    saveState === "saving" ? "saving…" : saveState === "saved" ? "saved" : saveState === "error" ? "save failed" : "";

  // Collapsible side-panel section. Open state persists per id (defaultOpen
  // applies until the user toggles). Used to fold the lower-priority Flow /
  // Actions / Shortcuts zones so the inspector stays the panel's focus.
  const Sec = ({ id, title, hint, defaultOpen = false, children }) => {
    const open = secOpen[id] ?? defaultOpen;
    return (
      <div className={"fd-sec" + (open ? " open" : "")}>
        <button type="button" className="fd-sechead" onClick={() => toggleSec(id, !open)} aria-expanded={open}>
          <span className="fd-secarrow">▸</span>
          <span className="fd-sectitle">{title}</span>
          {hint != null && <span className="fd-sechint">{hint}</span>}
        </button>
        {open && <div className="fd-secbody">{children}</div>}
      </div>
    );
  };

  // Embedded (e.g. inside an okuro·slides deck): the fitted graph ONLY — no
  // topbar, side panel, footer, controls or interaction.
  if (embed) {
    return (
      <div className="fd-embed h-full w-full">
        {view === "mermaid" ? (
          <MermaidView code={mermaidCode} accent={ACCENT} />
        ) : (
          <BridgeContext.Provider value={bridgeValue}>
          <FlowCanvas
            mode="flow"
            // The focused PROJECTION, not the stored graph — see focusNodes.
            nodes={focusNodes}
            edges={focusEdges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            accent={ACCENT}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodesDelete={onNodesDelete}
            onNodeDragStop={onNodeDragStop}
            onNodeClick={() => {}}
            onPaneClick={() => {}}
            onEdgeDoubleClick={() => {}}
            onNodeDoubleClick={() => {}}
            // A slide/deck embed is a PICTURE: neither authorable nor
            // navigable. Both off reproduces exactly what the old single
            // `readOnly` flag did here — this is the one call site that used it.
            editable={false}
            interactive={false}
          />
          </BridgeContext.Provider>
        )}
      </div>
    );
  }

  return (
    <div className={"fd-root" + (hidePortLabels ? " fd-hide-plabels" : "")}>
      {streamStatus && (
        <div
          style={{
            position: "absolute",
            top: "50%",
            left: "50%",
            transform: "translate(-50%, -50%)",
            zIndex: 40,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 10,
            padding: "22px 30px",
            borderRadius: 16,
            background: "var(--color-surface-elevated, rgba(16,16,16,0.92))",
            border: "1px solid var(--color-accent, #8ff0a4)",
            boxShadow: "0 0 40px var(--color-accent, #8ff0a4)33",
            fontFamily: "var(--font-mono, 'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Code', 'Roboto Mono', Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace)",
            color: "var(--color-accent, #8ff0a4)",
            pointerEvents: "none",
            textAlign: "center",
            minWidth: 220,
          }}
        >
          <div
            style={{
              width: 14,
              height: 14,
              borderRadius: "50%",
              background: "var(--color-accent, #8ff0a4)",
              animation: "okuroPulse 1.1s ease-in-out infinite",
            }}
          />
          <div style={{ fontSize: 15, fontWeight: 600 }}>okuro·flow</div>
          <div style={{ fontSize: 13, opacity: 0.95 }}>{streamStatus}…</div>
          <div style={{ fontSize: 12, opacity: 0.6 }}>
            {nodes.length > 0 ? `${nodes.length} node${nodes.length === 1 ? "" : "s"} · ` : ""}
            {streamElapsed}s
          </div>
          <style>{"@keyframes okuroPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}"}</style>
        </div>
      )}
      <div className="fd-topbar">
        <a className="fd-gallery-back" href="/flow" title="back to flow gallery">
          ← Flows
        </a>
        <Select value={currentId || "__new__"} onValueChange={(v) => (v === "__new__" ? newFlow() : loadFlow(v))}>
          <SelectTrigger size="sm" className="w-[200px]" title="quick switch flow">
            <SelectValue placeholder="select flow" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__new__">— new flow —</SelectItem>
            {flowList.map((f) => (
              <SelectItem key={f.id} value={f.id}>
                {f.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          className="h-8 max-w-[360px] flex-1 font-semibold"
          value={flowName}
          placeholder="Flow name"
          onChange={(e) => setFlowName(e.target.value)}
        />
        <Button variant="outline" size="sm" onClick={newFlow}>
          + New
        </Button>
        <Button variant="outline" size="sm" onClick={() => doSave()} title="save now">
          Save
        </Button>
        <Button variant="ghost" size="sm" className="text-destructive hover:text-destructive" onClick={deleteFlow}>
          Delete
        </Button>
        <span className={"fd-savebadge " + saveState}>{saveBadge}</span>
        <div className="fd-viewtoggle ml-auto" role="group" aria-label="view mode">
          <Button
            variant={view === "flow" ? "default" : "outline"}
            size="sm"
            onClick={() => setView("flow")}
            title="interactive node canvas"
          >
            Flow
          </Button>
          <Button
            variant={view === "mermaid" ? "default" : "outline"}
            size="sm"
            onClick={() => {
              setMermaidCode(flowToMermaid(serialize(), { accent: ACCENT }));
              setView("mermaid");
            }}
            title="rendered Mermaid diagram (read-only)"
          >
            Mermaid
          </Button>
        </div>
        <Button
          variant={sideCollapsed ? "outline" : "default"}
          size="sm"
          onClick={() => setSideCollapsed((v) => !v)}
          title={sideCollapsed ? "Show inspector" : "Hide inspector"}
        >
          {sideCollapsed ? "Inspector ‹" : "Inspector ›"}
        </Button>
      </div>
      <div className="fd-wrap">
        <input type="file" accept="application/json,.json" ref={fileRef} style={{ display: "none" }} onChange={onFile} />
        <div className="fd-canvas">
          {msg && <div className="fd-toast">{msg}</div>}
          {view === "mermaid" ? (
            <MermaidView code={mermaidCode} accent={ACCENT} />
          ) : (
          <BridgeContext.Provider value={bridgeValue}>
          <PortSelContext.Provider value={selPort}>
          <FlowCanvas
            mode="flow"
            // The focused PROJECTION, not the stored graph — see focusNodes.
            nodes={focusNodes}
            edges={focusEdges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            accent={ACCENT}
            multiSelect={multiSelect}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodesDelete={onNodesDelete}
            onNodeDragStop={onNodeDragStop}
            onNodeClick={(e, n) => {
              setSelId(n.id);
              setSelPort(null);
              clearEdgeSel();
            }}
            onPaneClick={() => {
              setSelId(null);
              setSelPort(null);
              clearEdgeSel();
            }}
            onEdgeClick={(e, edge) => selectEdge(edge.id)}
            onEdgeDoubleClick={(e, edge) => bridge.EDGE_EDIT(edge.id)}
            onNodeDoubleClick={(e, n) => {
              if (n.type === "group") {
                n.data.collapsed === false ? collapseGroup(n.id) : expandGroup(n.id);
              }
            }}
          />
          </PortSelContext.Provider>
          </BridgeContext.Provider>
          )}
          {view !== "mermaid" && (
            <FloatingToolbar
              hasSel={selCount > 0 || !!selEdgeId}
              settingsOpen={!sideCollapsed}
              onOpenSettings={() => setSideCollapsed((v) => !v)}
              multiSelect={multiSelect}
              onToggleMultiSelect={() => {
                setMultiSelect((v) => !v);
                flash(!multiSelect ? "multi-select on · drag to box-select" : "multi-select off · drag to pan");
              }}
              portSelected={!!selPort}
              portSide={selPortSide}
              onPlacePort={placePort}
              align={sel && sel.type !== "group" ? { h: sel.data.align || "left", v: sel.data.valign || vDefault(sel.data) } : null}
              onAlign={(h, v) => sel && setAlign(sel.id, h, v)}
              actions={{
                newNode: () => addNode("process"),
                duplicate: doDuplicate,
                remove: deleteSel,
                addInput: () => addPortSel("in"),
                addOutput: () => addPortSel("out"),
                rotate: rotatePorts,
                handover: openHandover,
                undo,
                redo,
              }}
            />
          )}
        </div>
        <HandoverDialog
          open={!!handoverContent}
          onOpenChange={(o) => !o && setHandoverContent(null)}
          content={handoverContent}
        />
        <div className={"fd-side" + (sideCollapsed ? " fd-side-collapsed" : "")} style={{ width: sideCollapsed ? 0 : sideW }}>
          <div className="fd-resizer" onMouseDown={startResize} title="drag to resize"></div>
          <div className="fd-zonehead fd-inspector-head">
            <span className="fd-zone-label">Inspector</span>
            {sel && !multi && <span className="fd-zone-meta">{sel.data.tag || sel.type}</span>}
            {multi && <span className="fd-zone-meta">{selCount} selected</span>}
            <button
              type="button"
              className="fd-inspector-close"
              title="Close settings"
              aria-label="Close settings"
              onClick={() => setSideCollapsed(true)}
            >
              ✕
            </button>
          </div>
          {selCount === 0 && (
            <p className="fd-placeholder">Select a node to edit. Drag = box-select · ⌘-click = multi · Space+drag = pan.</p>
          )}
          {multi && (
            <div className="fd-det">
              <div className="k">{selCount} nodes selected · edits apply to all (matching parts only)</div>
              <div className="k" style={{ marginTop: 8 }}>
                color (all)
              </div>
              <div className="fd-swatches">
                <span className="sw def" title="reset to accent" style={{ background: "var(--color-accent)" }} onClick={() => mSetColor(undefined)}>
                  ∅
                </span>
                {PALETTE.map((c) => (
                  <span key={c} className="sw" title={c} style={{ background: c }} onClick={() => mSetColor(c)}></span>
                ))}
              </div>
              <div className="k" style={{ marginTop: 6 }}>
                category (all)
              </div>
              <FSelect
                placeholder="— set —"
                value={undefined}
                onValueChange={(v) => mSetCat(v)}
                options={Object.keys(CAT).filter((c) => c !== "group")}
              />
              <div className="k" style={{ marginTop: 8 }}>
                common inputs
              </div>
              {aggPorts("in").map((a) => mPortRow("in", a))}
              {!aggPorts("in").length && <div className="fd-none">none in common</div>}
              <Button variant="link" size="xs" className="h-auto px-0" onClick={() => mAddPort("in")}>
                + add input to all
              </Button>
              <div className="k">common outputs</div>
              {aggPorts("out").map((a) => mPortRow("out", a))}
              {!aggPorts("out").length && <div className="fd-none">none in common</div>}
              <Button variant="link" size="xs" className="h-auto px-0" onClick={() => mAddPort("out")}>
                + add output to all
              </Button>
            </div>
          )}
          {sel && (
            <div className="fd-det">
              <div>
                <span className="dtag" style={{ background: sel.data.color || "var(--color-accent)" }}>
                  {sel.data.tag || sel.type}
                </span>
                {sel.type === "group" && (
                  <span
                    style={{ float: "right", cursor: "pointer", color: "#b79cff", fontSize: 11 }}
                    onClick={() => (sel.data.collapsed === false ? collapseGroup(sel.id) : expandGroup(sel.id))}
                  >
                    {sel.data.collapsed === false ? "✕ collapse" : "⤢ expand"}
                  </span>
                )}
              </div>
              {(() => {
                const ins = edges.filter((e) => e.target === sel.id);
                const outs = edges.filter((e) => e.source === sel.id);
                return (
                  <div style={{ margin: "8px 0", borderBottom: "1px solid #2c2c3a", paddingBottom: 8 }}>
                    <div className="k">
                      connections ({ins.length} in · {outs.length} out) — ✕ to delete
                    </div>
                    {ins.map((e) => (
                      <div className="fd-pedit" key={e.id}>
                        <span style={{ color: "var(--color-accent)", fontSize: 10 }}>▸in</span>
                        <span style={{ flex: 1, fontSize: 10.5 }} className="text-fg-muted">
                          {nodeTitle(e.source)} → {e.targetHandle}
                        </span>
                        <Button variant="ghost" size="icon-xs" className="text-destructive" onClick={() => delEdge(e.id)}>
                          ✕
                        </Button>
                      </div>
                    ))}
                    {outs.map((e) => (
                      <div className="fd-pedit" key={e.id}>
                        <span style={{ color: "var(--color-accent)", fontSize: 10 }}>out▸</span>
                        <span style={{ flex: 1, fontSize: 10.5 }} className="text-fg-muted">
                          {e.sourceHandle} → {nodeTitle(e.target)}
                        </span>
                        <Button variant="ghost" size="icon-xs" className="text-destructive" onClick={() => delEdge(e.id)}>
                          ✕
                        </Button>
                      </div>
                    ))}
                    {ins.length + outs.length === 0 && <div className="fd-none">no connections</div>}
                  </div>
                );
              })()}
              <div className="k" style={{ marginTop: 8 }}>
                title
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Input className="h-8 flex-1" value={sel.data.title || ""} onChange={(e) => setTitle(sel.id, e.target.value)} />
                <Switch checked={!sel.data.hideTitle} onCheckedChange={(v) => setHide(sel.id, "hideTitle", !v)} title="show title (t)" />
              </div>
              <div className="k" style={{ marginTop: 6 }}>
                subtitle
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Input className="h-8 flex-1" value={sel.data.sub || ""} onChange={(e) => setSub(sel.id, e.target.value)} />
                <Switch checked={!sel.data.hideSub} onCheckedChange={(v) => setHide(sel.id, "hideSub", !v)} title="show description (d)" />
              </div>
              {sel.data.cat === "mdnote" && (
                <>
                  <div className="k" style={{ marginTop: 6 }}>
                    body (Markdown)
                  </div>
                  <Textarea
                    className="min-h-24 font-mono text-xs"
                    placeholder="**bold**, _italic_, lists, `code`, [links](…)"
                    value={(sel.data.body ?? sel.data.sub) || ""}
                    onChange={(e) => setBody(sel.id, e.target.value)}
                  />
                </>
              )}
              <div className="k" style={{ marginTop: 6 }}>
                tag (chip)
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Input className="h-8 flex-1" value={sel.data.tag || ""} onChange={(e) => setTag(sel.id, e.target.value)} />
                <Switch checked={!sel.data.hideChip} onCheckedChange={(v) => setHide(sel.id, "hideChip", !v)} title="show chip (c)" />
              </div>
              <div className="k" style={{ marginTop: 6 }}>
                color
              </div>
              <div className="fd-swatches">
                <span
                  className={"sw def" + (!sel.data.color ? " sel" : "")}
                  title="accent (default)"
                  style={{ background: "var(--color-accent)" }}
                  onClick={() => setColor(sel.id, undefined)}
                >
                  {!sel.data.color ? "✓" : "∅"}
                </span>
                {PALETTE.map((c) => (
                  <span
                    key={c}
                    className={"sw" + (sel.data.color === c ? " sel" : "")}
                    title={c}
                    style={{ background: c }}
                    onClick={() => setColor(sel.id, c)}
                  >
                    {sel.data.color === c ? "✓" : ""}
                  </span>
                ))}
              </div>
              {sel.type !== "group" && sel.data.cat !== "title" && sel.data.cat !== "mdnote" && (
                <>
                  <div className="k" style={{ marginTop: 6 }}>
                    style
                  </div>
                  <div className="fd-stylerow">
                    {[
                      ["tinted", "Tinted"],
                      ["solid", "Solid"],
                      ["outline", "Outline"],
                    ].map(([v, label]) => (
                      <button
                        key={v}
                        type="button"
                        className={"fd-stybtn" + ((sel.data.style || "tinted") === v ? " sel" : "")}
                        title={label + " node style"}
                        onClick={() => setStyle(sel.id, v === "tinted" ? undefined : v)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </>
              )}
              <div className="k" style={{ marginTop: 6 }}>
                content alignment
              </div>
              <div className="fd-aligngrid" title="align node content">
                {["top", "center", "bottom"].map((v) =>
                  ["left", "center", "right"].map((h) => {
                    const on = (sel.data.align || "left") === h && (sel.data.valign || vDefault(sel.data)) === v;
                    return (
                      <span
                        key={v + "-" + h}
                        className={"ag" + (on ? " sel" : "")}
                        data-h={h}
                        data-v={v}
                        title={v + " " + h}
                        onClick={() => setAlign(sel.id, h, v)}
                      />
                    );
                  }),
                )}
              </div>
              {sel.type === "node" && (
                <>
                  <div className="k" style={{ marginTop: 6 }}>
                    category (colour)
                  </div>
                  <FSelect
                    value={sel.data.cat || "process"}
                    onValueChange={(v) => setCat(sel.id, v)}
                    options={Object.keys(CAT).filter((c) => c !== "group")}
                  />
                  {portMode === "quad" && (
                    <>
                      <div className="k" style={{ marginTop: 6 }}>
                        orientation (where unset sides land)
                      </div>
                      <FSelect
                        value={sel.data.orient || "h"}
                        onValueChange={(v) => setOrient(sel.id, v)}
                        options={ORIENTS}
                      />
                    </>
                  )}
                  <div className="k" style={{ marginTop: 8 }}>
                    ports · label · direction · type · side
                  </div>
                  <div className="fd-none">Order sets the position on a side — drag ⠿ or use ↑↓.</div>
                  {portsOf(sel.data).map((p, i, a) => portRow(p, i, a.length, true))}
                  <div className="fd-bar" style={{ marginTop: 2 }}>
                    <Button variant="link" size="xs" className="h-auto px-0" onClick={() => addPort(sel.id, "in")}>
                      + add input
                    </Button>
                    <Button variant="link" size="xs" className="h-auto px-0" onClick={() => addPort(sel.id, "out")}>
                      + add output
                    </Button>
                  </div>
                </>
              )}
              {sel.type === "group" && (
                <>
                  <div className="k" style={{ marginTop: 8 }}>
                    {(sel.data.members || []).length} nodes inside · 1 merged input · 1 merged output
                  </div>
                  <div style={{ marginTop: 8, color: "#9aa0aa", fontSize: 11 }}>
                    A collapsed group is a black box exposing one in / one out. Double-click to expand it in place and see every connection.
                  </div>
                </>
              )}
            </div>
          )}

          <div className="fd-zonehead">
            <span className="fd-zone-label">Add</span>
          </div>
          <div className="fd-addgrid">
            {["process", "input", "output", "decision", "note", "title", "mdnote"].map((k) => (
              <button key={k} type="button" className="fd-addbtn capitalize" onClick={() => addNode(k)} title={"add " + k}>
                + {k}
              </button>
            ))}
          </div>

          <Sec id="flow" title="Flow">
            <div className="k">Description</div>
            <Input className="h-8" value={flowDesc} placeholder="optional one-liner" onChange={(e) => setFlowDesc(e.target.value)} />
            <div className="k" style={{ marginTop: 6 }}>
              Port layout
            </div>
            <FSelect
              value={portMode}
              onValueChange={setPortMode}
              options={[
                ["quad", "Ports on all 4 sides (centred)"],
                ["lr", "Left / right only (classic header)"],
              ]}
            />
            <div className="k" style={{ marginTop: 6 }}>
              Connection style
            </div>
            <FSelect value={estyle} onValueChange={setEstyle} options={EDGE_STYLES} />
            <div className="fd-toggle-row" style={{ marginTop: 8 }}>
              <span>Hide port labels</span>
              <Switch checked={hidePortLabels} onCheckedChange={setHidePortLabels} />
            </div>
          </Sec>

          <Sec id="actions" title="Actions">
            <div className="fd-bar">
              <Button variant="outline" size="sm" className="flex-1" onClick={undo}>
                ↶ Undo
              </Button>
              <Button variant="outline" size="sm" className="flex-1" onClick={redo}>
                ↷ Redo
              </Button>
            </div>
            <Button variant="outline" size="sm" className="w-full justify-start" onClick={autoLayout}>
              Auto-layout
            </Button>
            <div className="fd-bar">
              <Button variant="outline" size="sm" className="flex-1" onClick={groupSelected} disabled={selCount < 2}>
                ⧉ Group{selCount > 1 ? " (" + selCount + ")" : ""}
              </Button>
              <Button variant="outline" size="sm" className="flex-1" onClick={() => sel && ungroup(sel.id)} disabled={!sel || sel.type !== "group"}>
                Ungroup
              </Button>
            </div>
            <div className="fd-bar">
              <Button variant="outline" size="sm" className="flex-1" onClick={exportJ}>
                Export ↓
              </Button>
              <Button variant="outline" size="sm" className="flex-1" onClick={importJ}>
                Import ↑
              </Button>
            </div>
          </Sec>

          <Sec id="shortcuts" title="Shortcuts">
            <dl className="fd-keys">
              <div>
                <kbd>Space</kbd>+drag<span>pan</span>
              </div>
              <div>
                <kbd>drag</kbd>
                <span>box-select</span>
              </div>
              <div>
                <kbd>⌘</kbd>+click<span>multi-select</span>
              </div>
              <div>
                <kbd>⌘G</kbd> / <kbd>⌘⇧G</kbd>
                <span>group / ungroup</span>
              </div>
              <div>
                <kbd>⌘Z</kbd> / <kbd>⌘⇧Z</kbd>
                <span>undo / redo</span>
              </div>
              <div>
                <kbd>⌘C</kbd> / <kbd>⌘V</kbd>
                <span>copy / paste</span>
              </div>
              <div>
                <kbd>⌘I</kbd> / <kbd>⌘O</kbd>
                <span>add in / out port</span>
              </div>
              <div>
                <kbd>c</kbd> <kbd>t</kbd> <kbd>d</kbd>
                <span>chip / title / desc</span>
              </div>
              <div>
                <kbd>⌥</kbd>+arrows<span>align (⌥ mac · ctrl win)</span>
              </div>
              <div>
                <kbd>⌫</kbd>
                <span>delete</span>
              </div>
            </dl>
          </Sec>
        </div>
      </div>
    </div>
  );

  // ---- file import/export (kept from the standalone tool) ----
  function exportJ() {
    const b = new Blob([JSON.stringify(stripEd(foldAll()), null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(b);
    a.download = (flowNameRef.current || "flow").replace(/[^a-z0-9]+/gi, "-").toLowerCase() + ".flow.json";
    a.click();
    flash("exported");
  }
  function importJ() {
    fileRef.current && fileRef.current.click();
  }
  function onFile(ev) {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = () => {
      try {
        const d = JSON.parse(rd.result);
        applyGraph(d);
        dirtyRef.current = true;
        scheduleSave();
        flash("imported " + f.name);
      } catch (err) {
        flash("bad JSON file");
      }
    };
    rd.readAsText(f);
    ev.target.value = "";
  }
}
