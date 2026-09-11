/**
 * PeopleGraph — user-center graph view with ambient motion.
 *
 * Uses @xyflow/react for pan/zoom/hit-testing and a radial layout helper for
 * positioning. Me sits at the origin; persons orbit around at a radius that
 * scales mildly with headcount. Motion is CSS-driven (see graph.css) and
 * respects prefers-reduced-motion.
 *
 * Selection is lifted to the parent via `onSelect`; this component stays
 * presentational so it can be re-skinned later without touching page logic.
 *
 * Persistence: positions, groups, and the connection-style setting are all
 * stored in browser localStorage (see graph-storage.ts).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  Panel,
  getBezierPath,
  getSimpleBezierPath,
  getSmoothStepPath,
  getStraightPath,
  useNodesState,
  useEdgesState,
  ReactFlowProvider,
  type Node,
  type Edge,
  type EdgeProps,
  type NodeProps,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./graph.css";
import { Plus, Users, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import type { PeopleGraph as PeopleGraphData } from "@/lib/people-api";
import { targetGroupsApi } from "@/lib/handover-api";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { layoutRadial } from "./graph-layout";
import {
  type ConnectionStyle,
  type Group,
  GROUP_GEOM_VERSION,
  newGroupId,
  readGroups,
  readPositions,
  readSettings,
  writeGroups,
  writePositions,
  writeSettings,
} from "./graph-storage";

export type GraphSelection =
  | { type: "me" }
  | { type: "person"; id: string }
  | { type: "edge"; personId: string };

type MeNodeData = {
  displayName: string;
  role?: string;
  selected?: boolean;
};

type PersonNodeData = {
  displayName: string;
  role?: string | null;
  organization?: string | null;
  relationType?: string | null;
  haloDelay: string;
  selected?: boolean;
};

type EdgeData = {
  count: number;
  lastAt: string | null;
  pulseDelay: string;
  intent: string;
  /** Drives label visibility. Set per-render based on selection state. */
  reveal: boolean;
  /** Drives edge geometry; mirrors the parent's connectionStyle. */
  style: ConnectionStyle;
};

// ── Custom nodes ─────────────────────────────────────────────────────

function MeNode({ data }: NodeProps<Node<MeNodeData>>) {
  return (
    <div className="relative">
      <span className="people-graph__me-glow" aria-hidden />
      <div
        className={
          "flex h-28 w-28 flex-col items-center justify-center rounded-full " +
          "bg-accent/15 border-2 border-accent"
        }
      >
        <div className="text-[10px] font-medium uppercase tracking-wider text-accent">
          You
        </div>
        <div className="mt-1 max-w-[96px] truncate px-2 text-center text-sm font-semibold text-fg">
          {data.displayName}
        </div>
        {data.role && (
          <div className="mt-0.5 max-w-[96px] truncate px-2 text-center text-[10px] text-fg-subtle">
            {data.role}
          </div>
        )}
      </div>
      <Handle
        id="out"
        type="source"
        position={Position.Top}
        style={{
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: 1,
          height: 1,
          minWidth: 1,
          minHeight: 1,
          border: "none",
          background: "transparent",
          opacity: 0,
          pointerEvents: "none",
        }}
      />
    </div>
  );
}

function PersonNode({ data }: NodeProps<Node<PersonNodeData>>) {
  return (
    <div className="relative">
      <span
        className="people-graph__halo"
        style={{ ["--halo-delay" as string]: data.haloDelay }}
        aria-hidden
      />
      <div
        className={
          "flex h-24 w-24 flex-col items-center justify-center rounded-full " +
          "border border-border bg-surface-elevated hover:border-accent/60 " +
          "transition-colors"
        }
      >
        <div className="max-w-[88px] truncate px-2 text-center text-xs font-semibold text-fg">
          {data.displayName}
        </div>
        {data.role && (
          <div className="mt-0.5 max-w-[88px] truncate px-2 text-center text-[10px] text-fg-subtle">
            {data.role}
          </div>
        )}
        {data.organization && (
          <div className="mt-0.5 max-w-[88px] truncate px-2 text-center text-[9px] text-fg-faint">
            {data.organization}
          </div>
        )}
      </div>
      <Handle
        id="in"
        type="target"
        position={Position.Top}
        style={{
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          width: 1,
          height: 1,
          minWidth: 1,
          minHeight: 1,
          border: "none",
          background: "transparent",
          opacity: 0,
          pointerEvents: "none",
        }}
      />
    </div>
  );
}

// ── Custom edge — geometry switches on data.style; label only when revealed.

/** Arc path: a circular arc connecting source → target. Bulge side is
 *  fixed (sweep-flag 1) so all edges curve consistently. Radius is a bit
 *  larger than the chord, giving a gentle bow rather than a tight loop. */
function getArcPath(args: {
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
}): [string, number, number] {
  const { sourceX, sourceY, targetX, targetY } = args;
  const dx = targetX - sourceX;
  const dy = targetY - sourceY;
  const chord = Math.hypot(dx, dy);
  if (chord < 0.5) {
    return [`M ${sourceX},${sourceY}`, sourceX, sourceY];
  }
  const radius = chord * 0.95;
  const sweep = 1; // SVG: 1 = positive-angle (clockwise in y-down coords)
  const d = `M ${sourceX},${sourceY} A ${radius},${radius} 0 0 ${sweep} ${targetX},${targetY}`;
  // Label sits at the arc midpoint = chord midpoint offset perpendicularly
  // by the sagitta. Sweep=1 in y-down screen coords bulges to the side
  // (-dy, +dx) / chord.
  const half = chord / 2;
  const sagitta = radius - Math.sqrt(Math.max(0, radius * radius - half * half));
  const nx = -dy / chord;
  const ny = dx / chord;
  const labelX = (sourceX + targetX) / 2 + nx * sagitta;
  const labelY = (sourceY + targetY) / 2 + ny * sagitta;
  return [d, labelX, labelY];
}

function pathFor(
  style: ConnectionStyle,
  args: {
    sourceX: number;
    sourceY: number;
    sourcePosition: Position;
    targetX: number;
    targetY: number;
    targetPosition: Position;
  },
): [string, number, number] {
  if (style === "straight") {
    const [d, lx, ly] = getStraightPath({
      sourceX: args.sourceX,
      sourceY: args.sourceY,
      targetX: args.targetX,
      targetY: args.targetY,
    });
    return [d, lx, ly];
  }
  if (style === "step") {
    const [d, lx, ly] = getSmoothStepPath(args);
    return [d, lx, ly];
  }
  if (style === "simple-bezier") {
    const [d, lx, ly] = getSimpleBezierPath(args);
    return [d, lx, ly];
  }
  if (style === "arc") {
    return getArcPath(args);
  }
  const [d, lx, ly] = getBezierPath(args);
  return [d, lx, ly];
}

// Visual radii: the source ("you") is 112px wide, persons are 96px.
// Edges shrink toward each other by these amounts so the line visibly
// originates ON the node's outline rather than at its center.
const SOURCE_RADIUS = 56;
const TARGET_RADIUS = 48;
const ENDPOINT_DOT_R = 3.5;

function TranslationEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  selected,
  data,
}: EdgeProps<Edge<EdgeData>>) {
  const style = data?.style ?? "bezier";

  // Trim the line ends to land on the perimeter of each node, not the
  // center. Direction is the chord (source → target); for arcs the
  // tangent at the endpoint differs slightly but the visible trim is
  // along the chord which still reads as "exits the circle".
  const dx = targetX - sourceX;
  const dy = targetY - sourceY;
  const d = Math.hypot(dx, dy) || 1;
  const ux = dx / d;
  const uy = dy / d;
  const sx = sourceX + ux * SOURCE_RADIUS;
  const sy = sourceY + uy * SOURCE_RADIUS;
  const tx = targetX - ux * TARGET_RADIUS;
  const ty = targetY - uy * TARGET_RADIUS;

  const [edgePath, labelX, labelY] = pathFor(style, {
    sourceX: sx,
    sourceY: sy,
    sourcePosition,
    targetX: tx,
    targetY: ty,
    targetPosition,
  });
  const count = data?.count ?? 0;
  const pulseDelay = data?.pulseDelay ?? "0s";
  const intent = data?.intent ?? "";
  const isLive = count > 0;
  const reveal = !!(selected || data?.reveal);

  const labelWidth = 640;
  const tooltip = isLive
    ? `${count} translation${count === 1 ? "" : "s"}${
        data?.lastAt ? ` · last ${data.lastAt}` : ""
      }`
    : "No translations yet · click to try one";

  return (
    <>
      <path
        id={id}
        d={edgePath}
        fill="none"
        stroke={
          reveal
            ? "var(--color-accent, #22c55e)"
            : "var(--color-fg-subtle, #909090)"
        }
        strokeWidth={reveal ? 2.2 : 1}
        // Two states only:
        //   reveal      → accent color + flowing dashes (.edge-flow class
        //                 supplies dasharray + animation)
        //   not reveal  → dim static dashes
        // Live status (translation_count > 0) is surfaced via the label
        // tooltip when selected — visually mixing live/cold here just made
        // the cold-but-live edge look special for no useful reason.
        strokeDasharray={reveal ? undefined : "4 4"}
        className={
          "people-graph__edge-path people-graph__edge-pulse" +
          (reveal ? " people-graph__edge-flow" : "")
        }
        style={{ ["--pulse-delay" as string]: pulseDelay }}
      />
      {/* Wider invisible hit target so label clicks still register the edge. */}
      <path d={edgePath} fill="none" stroke="transparent" strokeWidth={24} />
      {/* Connection anchors — small accent dots on each node's outline
       *  where the edge enters/exits. These make the connection feel
       *  "rooted" in the node rather than springing from its center. */}
      <circle
        cx={sx}
        cy={sy}
        r={ENDPOINT_DOT_R}
        fill="var(--color-accent, #22c55e)"
        stroke="var(--color-surface, #0a0a0a)"
        strokeWidth={1}
      />
      <circle
        cx={tx}
        cy={ty}
        r={ENDPOINT_DOT_R}
        fill="var(--color-accent, #22c55e)"
        stroke="var(--color-surface, #0a0a0a)"
        strokeWidth={1}
      />
      {reveal && (
        <foreignObject
          x={labelX - labelWidth / 2}
          y={labelY - 14}
          width={labelWidth}
          height={28}
          style={{ overflow: "visible" }}
        >
          <div
            className="flex h-7 items-center justify-center"
            style={{ width: labelWidth }}
          >
            <div
              className={
                "flex h-7 items-center rounded-full px-3 text-[11px] font-medium whitespace-nowrap " +
                (isLive
                  ? "border border-accent/40 bg-surface-elevated text-accent"
                  : "border border-border bg-surface-elevated text-fg-muted")
              }
              style={{ width: "fit-content" }}
              title={`${intent}\n\n${tooltip}`}
            >
              {intent || (isLive ? `${count}×` : "—")}
            </div>
          </div>
        </foreignObject>
      )}
    </>
  );
}

// ── Group bubble (dashed-circle outline + draggable title chip) ─────
// Group has its OWN stored identity (center + radius) — derived nothing
// from live positions. Members orbit; the circle stays put.

const NODE_HALF = 48; // person nodes are 96px (h-24 w-24)

/** React Flow's node.position is the TOP-LEFT of the node wrapper.
 *  Group geometry (gravity center, drop-zone circle) is in CENTER
 *  coordinates, so anywhere we compare a node's position to a group
 *  center we must offset by half the node's width/height. */
function personCenter(p: { x: number; y: number }): { x: number; y: number } {
  return { x: p.x + NODE_HALF, y: p.y + NODE_HALF };
}

// Default visible radius for newly-created groups. Sized so the
// orbit-spring leaves comfortable padding for the 96px nodes + their
// halo (~+8px) inside the dashed boundary.
const DEFAULT_GROUP_RADIUS = 200;

type GroupBubbleData = {
  name: string;
  radius: number;
  groupId: string;
};

function GroupBubble({ data }: NodeProps<Node<GroupBubbleData>>) {
  return (
    <div className="people-graph__group-bubble">
      <div
        className="people-graph__group-circle"
        style={{
          width: data.radius * 2,
          height: data.radius * 2,
        }}
      />
      {/* Chip sits AT the geometric center of the circle — it IS the
       *  gravity well that members orbit, so anchoring the label there
       *  ties the visible name to the felt force. */}
      <div className="people-graph__group-title-chip">{data.name}</div>
    </div>
  );
}

function computeCentroid(
  ids: string[],
  positions: Record<string, { x: number; y: number }>,
): { x: number; y: number } | null {
  const present = ids
    .map((id) => positions[id])
    .filter((p): p is { x: number; y: number } => !!p);
  if (present.length === 0) return null;
  const sx = present.reduce((a, p) => a + p.x, 0);
  const sy = present.reduce((a, p) => a + p.y, 0);
  return { x: sx / present.length, y: sy / present.length };
}

/** Resolve a group's center, falling back to member centroid for groups
 *  stored before the `center` field existed. */
function resolveGroupCenter(
  g: Group,
  positions: Record<string, { x: number; y: number }>,
): { x: number; y: number } | null {
  return g.center ?? computeCentroid(g.memberIds, positions);
}

function resolveGroupRadius(g: Group): number {
  return g.radius ?? DEFAULT_GROUP_RADIUS;
}

/** Pick the group whose visible circle contains the given point. Uses
 *  the stored center+radius — same boundary the user sees, so drop-
 *  detection matches the visual cue exactly. */
function findContainingGroup(
  groups: Group[],
  positions: Record<string, { x: number; y: number }>,
  point: { x: number; y: number },
): Group | null {
  for (const g of groups) {
    const c = resolveGroupCenter(g, positions);
    if (!c) continue;
    const r = resolveGroupRadius(g);
    if (Math.hypot(point.x - c.x, point.y - c.y) <= r) return g;
  }
  return null;
}

// ── Main component ──────────────────────────────────────────────────

const nodeTypes = {
  me: MeNode,
  person: PersonNode,
  "group-bubble": GroupBubble,
};
const edgeTypes = { translation: TranslationEdge };

// ── Spring-cluster simulation ────────────────────────────────────────
// Tunables — kept low so the motion is "settles in half a second", not
// "bouncy ball". The user reads it as "the group title pulls them in",
// not as "physics demo".

const SPRING_K = 0.018; // attraction toward orbit point per tick
const FRICTION = 0.82;
const REPULSION_RADIUS = 200; // members closer than this push each other apart
const REPULSION_K = 22; // strong push so persons don't overlap
const REST_VELOCITY = 0.05; // when the fastest body moves slower than this, stop ticking
const MAX_TICKS = 800; // safety cap

/**
 * Target orbit radius — members are pulled toward a point on this ring
 * around the group center. With NODE_HALF=48 + halo ~8 = 56 effective
 * radius per member, ORBIT_RADIUS + 56 must stay comfortably below
 * DEFAULT_GROUP_RADIUS so the cluster sits inside the dashed circle
 * without grazing the boundary.
 */
const ORBIT_RADIUS = 95;

function buildFlowNodes(
  graph: PeopleGraphData,
  positions: Record<string, { x: number; y: number }>,
): Node[] {
  const layout = layoutRadial(graph.nodes);
  return [
    {
      id: "me",
      type: "me",
      position: { x: layout.me.x, y: layout.me.y },
      data: {
        displayName: graph.me.display_name,
        role: graph.me.role,
      } satisfies MeNodeData,
      draggable: false,
      selectable: true,
    },
    ...graph.nodes.map<Node>((p, i) => {
      const stored = positions[p.id];
      const pos = stored ?? layout.people[i] ?? { x: 0, y: 0 };
      return {
        id: p.id,
        type: "person",
        position: { x: pos.x, y: pos.y },
        data: {
          displayName: p.display_name,
          role: p.role,
          organization: p.organization,
          relationType: p.relation_type,
          haloDelay: `${(i % 5) * 0.7}s`,
        } satisfies PersonNodeData,
        draggable: true,
        selectable: true,
      };
    }),
  ];
}

function buildGroupBubbleNodes(
  groups: Group[],
  positions: Record<string, { x: number; y: number }>,
): Node[] {
  return groups
    .map((g): Node | null => {
      const c = resolveGroupCenter(g, positions);
      if (!c) return null;
      const radius = resolveGroupRadius(g);
      const size = radius * 2;
      return {
        id: `group:${g.id}`,
        type: "group-bubble",
        // Bubble IS the circle — top-left so the geometric center
        // aligns exactly with the stored group center.
        position: { x: c.x - radius, y: c.y - radius },
        width: size,
        height: size,
        data: {
          name: g.name,
          radius,
          groupId: g.id,
        } satisfies GroupBubbleData,
        // Draggable via the chip — the circle stays click-through so
        // persons inside remain reachable.
        draggable: true,
        dragHandle: ".people-graph__group-title-chip",
        // Selectable so clicking the chip reveals the whole audience's edge
        // labels (deriveRevealedPersonIds reads RF's selection state, since
        // handleNodeClick only reports me/person upward). REQUIRES the
        // .react-flow__node-group-bubble pointer-events override in
        // graph.css — RF's .selectable class sets `pointer-events: all` on
        // its wrapper, which otherwise makes this 2r-wide box swallow every
        // click meant for a person orbiting inside it.
        selectable: true,
        focusable: false,
        zIndex: -1,
        style: { width: size, height: size },
      };
    })
    .filter((n): n is Node => n !== null);
}

function buildFlowEdges(
  graph: PeopleGraphData,
  revealedPersonIds: Set<string>,
  connectionStyle: ConnectionStyle,
): Edge[] {
  return graph.edges.map((e, i) => ({
    id: `me-${e.target}`,
    type: "translation",
    source: "me",
    target: e.target,
    data: {
      count: e.translation_count,
      lastAt: e.last_translated_at,
      pulseDelay: `${(i % 4) * 0.9}s`,
      intent: e.intent,
      reveal: revealedPersonIds.has(e.target),
      style: connectionStyle,
    } satisfies EdgeData,
    selectable: true,
  }));
}

// Derive the SET of person ids whose edges should be highlighted /
// animated. When a single person is selected (via parent selection or
// RF single-select), include them; if they're in a group, include all
// group members too — clicking one node visually surfaces the whole
// cluster's relationships.
/** Which edges show their label.
 *
 *  The rule is "reveal exactly what you clicked":
 *    · a group bubble → every member, because the bubble IS the audience
 *    · a person       → that person alone
 *
 *  It used to expand a person to their whole group, so clicking one
 *  colleague lit up every edge in their circle and you could not read a
 *  single person's translation focus — the labels all rendered at once and
 *  overlapped.
 */
export function deriveRevealedPersonIds(
  external: GraphSelection | null,
  rfSelectedIds: string[],
  rfSelectedGroupIds: string[],
  groups: Group[],
): Set<string> {
  // Group wins: clicking the bubble is the deliberate "show me this whole
  // audience" gesture, and RF marks the bubble selected even though
  // handleNodeClick only reports me/person upward.
  if (rfSelectedGroupIds.length) {
    const ids = new Set<string>();
    for (const gid of rfSelectedGroupIds) {
      const group = groups.find((g) => g.id === gid);
      if (group) for (const id of group.memberIds) ids.add(id);
    }
    return ids;
  }

  if (external?.type === "person") return new Set([external.id]);
  if (external?.type === "edge") return new Set([external.personId]);
  // Marquee/shift multi-select still reveals each person picked — that is
  // "what you clicked", just more than one of them.
  return new Set(rfSelectedIds.filter((id) => id !== "me"));
}

function PeopleGraphInner({
  graph,
  selection: externalSelection,
  onSelect,
}: {
  graph: PeopleGraphData;
  selection: GraphSelection | null;
  onSelect: (s: GraphSelection | null) => void;
}) {
  const positionsRef = useRef<Record<string, { x: number; y: number }>>(
    readPositions(),
  );
  const [groups, setGroups] = useState<Group[]>(() => readGroups());
  const [settings, setSettings] = useState(() => readSettings());

  // Backfill / migrate group geometry. Two cases:
  //  1. Pre-center groups (no center/radius)        → derive from current centers
  //  2. Pre-v2 groups (center computed from RF top-lefts, off by NODE_HALF)
  //                                                  → re-snapshot from current centers
  // Both end up stamped with GROUP_GEOM_VERSION so we don't redo this.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    let mutated = false;
    const centersFromRef: Record<string, { x: number; y: number }> = {};
    for (const [id, p] of Object.entries(positionsRef.current)) {
      centersFromRef[id] = personCenter(p);
    }
    const next = groups.map((g) => {
      if (g.geomVersion === GROUP_GEOM_VERSION && g.center && g.radius != null) {
        return g;
      }
      const c =
        computeCentroid(g.memberIds, centersFromRef) ??
        g.center ??
        { x: 0, y: 0 };
      // v3 also bumps radius to the new default so older groups visually
      // accommodate the orbit-spring without members grazing the boundary.
      const r = Math.max(g.radius ?? 0, DEFAULT_GROUP_RADIUS);
      mutated = true;
      return { ...g, center: c, radius: r, geomVersion: GROUP_GEOM_VERSION };
    });
    if (mutated) {
      setGroups(next);
      writeGroups(next);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [groupNameDraft, setGroupNameDraft] = useState("");
  const [groupDialogOpen, setGroupDialogOpen] = useState(false);

  // All nodes (me + person + group bubbles) live in useNodesState so RF
  // can manage drag positions internally. Bubbles previously lived
  // outside this state and were rebuilt on every render — that fought
  // RF's drag delta and made the bubble snap-back instead of follow
  // the cursor smoothly.
  // useNodesState only takes a Node[] (no lazy initializer), so compute
  // the initial array once via a ref-guard rather than re-running the
  // builder on every render — same intent as a useState lazy-init.
  const initialNodesRef = useRef<Node[] | null>(null);
  if (initialNodesRef.current === null) {
    const persons = buildFlowNodes(graph, positionsRef.current);
    const initialCenters: Record<string, { x: number; y: number }> = {};
    for (const n of persons) {
      if (n.type === "person") initialCenters[n.id] = personCenter(n.position);
    }
    const bubbles = buildGroupBubbleNodes(groups, initialCenters);
    initialNodesRef.current = [...bubbles, ...persons];
  }
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>(initialNodesRef.current);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(
    buildFlowEdges(graph, new Set(), settings.connectionStyle),
  );

  // Sync bubble nodes when groups change (create/remove/center moved).
  // Skips any bubble currently being dragged so RF's in-flight position
  // isn't stomped mid-drag.
  useEffect(() => {
    setNodes((prev) => {
      const nonBubbles = prev.filter((n) => n.type !== "group-bubble");
      const personPos: Record<string, { x: number; y: number }> = {};
      for (const n of nonBubbles) {
        if (n.type === "person") personPos[n.id] = personCenter(n.position);
      }
      const fresh = buildGroupBubbleNodes(groups, personPos);
      const merged = fresh.map((bubble) => {
        const live = prev.find((n) => n.id === bubble.id);
        if (live && draggingRef.current.has(bubble.id)) {
          // Keep RF's in-flight position; only refresh data (name etc.).
          return { ...bubble, position: live.position };
        }
        return bubble;
      });
      return [...merged, ...nonBubbles];
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups]);

  const renderNodes = nodes;

  // Track multi-select from React Flow so the toolbar knows when to appear.
  const selectedPersonIds = useMemo(
    () =>
      nodes
        .filter((n) => n.selected && n.type === "person")
        .map((n) => n.id),
    [nodes],
  );

  // Bubbles never reach handleNodeClick (it only reports me/person), so the
  // group gesture has to be read from React Flow's own selection state.
  const selectedGroupIds = useMemo(
    () =>
      nodes
        .filter((n) => n.selected && n.type === "group-bubble")
        .map((n) => (n.data as GroupBubbleData).groupId),
    [nodes],
  );

  const revealedPersonIds = useMemo(
    () =>
      deriveRevealedPersonIds(
        externalSelection,
        selectedPersonIds,
        selectedGroupIds,
        groups,
      ),
    [externalSelection, selectedPersonIds, selectedGroupIds, groups],
  );
  const revealedKey = useMemo(
    () => Array.from(revealedPersonIds).sort().join("|"),
    [revealedPersonIds],
  );

  // Rebuild person/me nodes when graph shape changes; preserve bubbles
  // and selection state.
  const graphNodeKey = graph.nodes.map((n) => n.id).join("|");
  useEffect(() => {
    setNodes((prev) => {
      const bubbles = prev.filter((n) => n.type === "group-bubble");
      const selectedById = new Map(prev.map((n) => [n.id, n.selected]));
      const positionsById = new Map(prev.map((n) => [n.id, n.position]));
      const fresh = buildFlowNodes(graph, positionsRef.current).map((n) => ({
        ...n,
        position: positionsById.get(n.id) ?? n.position,
        selected: selectedById.get(n.id) ?? false,
      }));
      return [...bubbles, ...fresh];
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphNodeKey, graph.me.display_name]);

  // Rebuild edges when graph topology or connection-style or revealed
  // set changes. Edge state is rebuilt fully (no in-flight drag to
  // preserve).
  useEffect(() => {
    setEdges(
      buildFlowEdges(graph, revealedPersonIds, settings.connectionStyle),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    graphNodeKey,
    JSON.stringify(graph.edges),
    revealedKey,
    settings.connectionStyle,
  ]);

  const handleNodeClick = useCallback(
    (_: unknown, n: Node) => {
      if (n.id === "me") onSelect({ type: "me" });
      else if (n.type === "person") onSelect({ type: "person", id: n.id });
    },
    [onSelect],
  );

  // Double-click the group chip to rename. Uses native prompt() — no
  // modal cost for a one-line text input. Operates on the bubble node
  // (the chip is the only pointer-active surface inside the bubble).
  const handleNodeDoubleClick = useCallback(
    (_: unknown, n: Node) => {
      if (n.type !== "group-bubble") return;
      const data = n.data as GroupBubbleData;
      const next = window.prompt("Rename group", data.name);
      if (next == null) return;
      const trimmed = next.trim();
      if (!trimmed) return;
      const updated = groups.map((g) =>
        g.id === data.groupId ? { ...g, name: trimmed } : g,
      );
      setGroups(updated);
      writeGroups(updated);
    },
    [groups],
  );

  const handleEdgeClick = useCallback(
    (_: unknown, e: Edge) => {
      onSelect({ type: "edge", personId: e.target });
    },
    [onSelect],
  );

  const handlePaneClick = useCallback(() => {
    onSelect(null);
  }, [onSelect]);

  // Track which nodes (persons OR group bubbles) are currently being
  // dragged. The simulation must not touch dragged person nodes; while
  // ANY drag is in flight the simulation pauses entirely so RF's pointer
  // pipeline isn't competing with state updates from RAF (which is what
  // makes clicks misfire).
  const draggingRef = useRef<Set<string>>(new Set());

  const wrappedOnNodesChange = useCallback(
    (changes: NodeChange[]) => {
      // When a group bubble is being dragged, mirror its delta onto every
      // member so the whole cluster translates as a rigid body in real
      // time. Without this, members only catch up on drop (via the spring),
      // which feels broken.
      const extra: NodeChange[] = [];
      for (const ch of changes) {
        if (ch.type !== "position" || !ch.position || !ch.dragging) continue;
        const node = nodes.find((n) => n.id === ch.id);
        if (!node || node.type !== "group-bubble") continue;
        const data = node.data as GroupBubbleData;
        const dx = ch.position.x - node.position.x;
        const dy = ch.position.y - node.position.y;
        if (dx === 0 && dy === 0) continue;
        const group = groups.find((g) => g.id === data.groupId);
        if (!group) continue;
        for (const memberId of group.memberIds) {
          const member = nodes.find((n) => n.id === memberId);
          if (!member) continue;
          extra.push({
            type: "position",
            id: memberId,
            position: { x: member.position.x + dx, y: member.position.y + dy },
            dragging: true,
          });
        }
      }
      onNodesChange(extra.length ? [...changes, ...extra] : changes);

      for (const ch of changes) {
        if (ch.type !== "position") continue;
        if (ch.dragging) {
          draggingRef.current.add(ch.id);
        } else {
          draggingRef.current.delete(ch.id);
        }
      }
    },
    [nodes, groups, onNodesChange],
  );

  const handleNodeDragStop = useCallback(
    (_: unknown, node: Node) => {
      // ── Group bubble was dragged: update its stored center ──────
      if (node.type === "group-bubble") {
        const data = node.data as GroupBubbleData;
        const radius = data.radius;
        // Bubble is exactly the circle — center = top-left + radius.
        const newCenter = {
          x: node.position.x + radius,
          y: node.position.y + radius,
        };
        const next = groups.map((g) =>
          g.id === data.groupId
            ? { ...g, center: newCenter, radius, geomVersion: GROUP_GEOM_VERSION }
            : g,
        );
        setGroups(next);
        writeGroups(next);
        return;
      }

      // ── Person was dragged: update positions + reconcile membership.
      if (node.id === "me") return;

      // Position snapshot in CENTER coords (group.center is in centers).
      const positions: Record<string, { x: number; y: number }> = {};
      for (const n of nodes) {
        if (n.type === "person") positions[n.id] = personCenter(n.position);
      }
      positions[node.id] = personCenter(node.position);

      const currentGroup = groups.find((g) =>
        g.memberIds.includes(node.id),
      );
      const targetGroup = findContainingGroup(
        groups,
        positions,
        personCenter(node.position),
      );

      let mutatedGroups: Group[] | null = null;
      if ((currentGroup?.id ?? null) !== (targetGroup?.id ?? null)) {
        mutatedGroups = groups.map((g) => ({
          ...g,
          memberIds:
            g.id === targetGroup?.id
              ? Array.from(new Set([...g.memberIds, node.id]))
              : g.memberIds.filter((id) => id !== node.id),
        }));
        // Critical: do NOT drop groups for emptiness here. A group's
        // center is its identity; an empty group is still a valid drop
        // zone the user can drop into. (Without this guard, dragging
        // members in/out repeatedly made groups disappear once they
        // briefly lost all members.)
      }

      // Persist all person positions as RF top-lefts (the format RF
      // hands back when reading from storage on next mount).
      const nextPositions: Record<string, { x: number; y: number }> = {
        ...positionsRef.current,
      };
      for (const n of nodes) {
        if (n.type === "person") nextPositions[n.id] = n.position;
      }
      nextPositions[node.id] = node.position;
      positionsRef.current = nextPositions;
      writePositions(nextPositions);

      if (mutatedGroups) {
        setGroups(mutatedGroups);
        writeGroups(mutatedGroups);
      }
    },
    [groups, nodes],
  );

  // ── Spring simulation ────────────────────────────────────────────
  // Pulls grouped persons toward an orbit ring around the group's
  // STORED center. Pauses entirely while the user is dragging anything
  // (so RF's pointer pipeline isn't fighting RAF state churn — this is
  // what was causing clicks to misfire). Restarts only when the input
  // (groups, graph shape) actually changes.
  useEffect(() => {
    if (groups.length === 0) return;
    let raf = 0;
    let ticks = 0;
    let restTicks = 0;
    const velocities: Record<string, { vx: number; vy: number }> = {};

    const step = () => {
      ticks += 1;

      // Pause while user is interacting — resume on next mouseup via
      // the drag-stop handler bumping nothing (the RAF is already
      // self-perpetuating; we just yield this frame).
      if (draggingRef.current.size > 0) {
        raf = requestAnimationFrame(step);
        return;
      }

      let maxSpeed = 0;
      let positionsChanged = false;

      setNodes((prev) => {
        const byId = new Map(prev.map((n) => [n.id, n]));
        const updates: Record<string, { x: number; y: number }> = {};

        for (const g of groups) {
          const center = g.center;
          if (!center) continue; // backfill happens at first user action
          const liveMembers = g.memberIds
            .map((id) => byId.get(id))
            .filter(
              (n): n is Node => !!n && n.type === "person",
            );
          if (liveMembers.length === 0) continue;

          for (const m of liveMembers) {
            // Spring math is in CENTER coords — RF's m.position is the
            // top-left, so we offset by NODE_HALF on both sides. The
            // resulting force vector translates 1:1 to top-left motion
            // (since the conversion is the same constant for both
            // current and target).
            const dx = m.position.x + NODE_HALF - center.x;
            const dy = m.position.y + NODE_HALF - center.y;
            const d = Math.hypot(dx, dy);
            const ux = d > 0.001 ? dx / d : Math.cos(m.id.length);
            const uy = d > 0.001 ? dy / d : Math.sin(m.id.length);
            // Force = (target_offset - current_offset) * k, where
            // target_offset = ORBIT_RADIUS * unit-vector. This pulls
            // the member CENTER onto the orbit ring around g.center.
            let ax = (ORBIT_RADIUS * ux - dx) * SPRING_K;
            let ay = (ORBIT_RADIUS * uy - dy) * SPRING_K;

            // Inter-member repulsion → spreads them around the ring.
            for (const other of liveMembers) {
              if (other.id === m.id) continue;
              const ddx = m.position.x - other.position.x;
              const ddy = m.position.y - other.position.y;
              const d = Math.hypot(ddx, ddy);
              if (d > 0 && d < REPULSION_RADIUS) {
                const f =
                  ((REPULSION_RADIUS - d) / REPULSION_RADIUS) * REPULSION_K;
                ax += (ddx / d) * f;
                ay += (ddy / d) * f;
              }
            }

            const v = velocities[m.id] ?? { vx: 0, vy: 0 };
            v.vx = (v.vx + ax) * FRICTION;
            v.vy = (v.vy + ay) * FRICTION;
            velocities[m.id] = v;

            const speed = Math.hypot(v.vx, v.vy);
            if (speed > maxSpeed) maxSpeed = speed;

            // Apply only if the move is meaningful — avoids jitter
            // re-renders that keep the loop alive forever.
            if (Math.abs(v.vx) > 0.02 || Math.abs(v.vy) > 0.02) {
              updates[m.id] = {
                x: m.position.x + v.vx,
                y: m.position.y + v.vy,
              };
            }
          }
        }

        if (Object.keys(updates).length === 0) return prev;
        positionsChanged = true;
        return prev.map((n) =>
          updates[n.id] ? { ...n, position: updates[n.id]! } : n,
        );
      });

      // Periodic checkpoint: every 30 ticks (~0.5s) push positions
      // through to storage so reload always restores something close
      // to the settled cluster, even if user navigates away mid-tick.
      if (positionsChanged && ticks % 30 === 0) {
        setNodes((prev) => {
          const next = { ...positionsRef.current };
          for (const n of prev) {
            if (n.type === "person") next[n.id] = n.position;
          }
          positionsRef.current = next;
          writePositions(next);
          return prev;
        });
      }

      // Rest detection — require N consecutive low-energy frames so
      // numerical noise doesn't keep the loop alive indefinitely.
      if (maxSpeed <= REST_VELOCITY) restTicks += 1;
      else restTicks = 0;

      if (restTicks < 8 && ticks < MAX_TICKS) {
        raf = requestAnimationFrame(step);
      } else {
        // Final rest write.
        setNodes((prev) => {
          const next = { ...positionsRef.current };
          for (const n of prev) {
            if (n.type === "person") next[n.id] = n.position;
          }
          positionsRef.current = next;
          writePositions(next);
          return prev;
        });
      }
    };

    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups, graphNodeKey]);

  // ── Connection style toggle ──────────────────────────────────────
  const setConnectionStyle = (style: ConnectionStyle) => {
    setSettings((s) => {
      const next = { ...s, connectionStyle: style };
      writeSettings(next);
      return next;
    });
  };

  // ── Group operations ─────────────────────────────────────────────
  // Members can only belong to one group at a time — strip the freshly
  // selected ids from any group they previously belonged to. Snapshot
  // the centroid of the selection at creation time as the group's
  // STORED center; the simulation pulls members toward this fixed
  // point thereafter (no centroid drift).
  const createGroupFromSelection = (name: string) => {
    if (selectedPersonIds.length < 2) return;
    const trimmed = name.trim() || "Group";

    const positions: Record<string, { x: number; y: number }> = {};
    for (const n of nodes) {
      if (n.type === "person") positions[n.id] = personCenter(n.position);
    }
    const center =
      computeCentroid(selectedPersonIds, positions) ?? { x: 0, y: 0 };

    const cleaned = groups.map((g) => ({
      ...g,
      memberIds: g.memberIds.filter(
        (id) => !selectedPersonIds.includes(id),
      ),
    }));
    const next: Group[] = [
      ...cleaned,
      {
        id: newGroupId(),
        name: trimmed,
        memberIds: [...selectedPersonIds],
        center,
        radius: DEFAULT_GROUP_RADIUS,
        geomVersion: GROUP_GEOM_VERSION,
      },
    ];
    setGroups(next);
    writeGroups(next);
  };

  const removeGroup = (groupId: string) => {
    const next = groups.filter((g) => g.id !== groupId);
    setGroups(next);
    writeGroups(next);
  };

  // ── Placing an EXISTING audience on the canvas ───────────────────
  // Groups drawn here are target_groups, so the ones that already exist
  // elsewhere in okuro (the seeded templates, anything an agent created,
  // a company audience) are placeable too — the canvas just had no way to
  // show them. Only those without geometry are offered; the rest are
  // already on screen.
  const targetGroupsQuery = useQuery({
    queryKey: ["target-groups", "placeable"],
    queryFn: () => targetGroupsApi.list(),
    staleTime: 30_000,
  });

  const placeableGroups = useMemo(() => {
    const onCanvas = new Set(groups.map((g) => g.id));
    return (targetGroupsQuery.data?.groups ?? []).filter(
      (g) => !onCanvas.has(g.id),
    );
  }, [targetGroupsQuery.data, groups]);

  const placeExistingGroup = (groupId: string) => {
    const summary = placeableGroups.find((g) => g.id === groupId);
    if (!summary) return;

    const positions: Record<string, { x: number; y: number }> = {};
    for (const n of nodes) {
      if (n.type === "person") positions[n.id] = personCenter(n.position);
    }
    // Only members actually on the graph can anchor the circle. A template
    // with no live members (or whose members are all inactive) still gets a
    // bubble — it lands at the origin and the user drags it where they want.
    const memberIds = summary.member_ids ?? [];
    const center = computeCentroid(memberIds, positions) ?? { x: 0, y: 0 };

    // Same single-assignment rule the UI enforces everywhere else: a person
    // sits in one bubble, so pull these members out of any other group.
    const cleaned = groups.map((g) => ({
      ...g,
      memberIds: g.memberIds.filter((id) => !memberIds.includes(id)),
    }));

    const next: Group[] = [
      ...cleaned,
      {
        id: summary.id,
        name: summary.name,
        memberIds: [...memberIds],
        center,
        radius: DEFAULT_GROUP_RADIUS,
        geomVersion: GROUP_GEOM_VERSION,
      },
    ];
    setGroups(next);
    writeGroups(next);
  };

  return (
    <div className="people-graph h-full w-full">
      <ReactFlow
        nodes={renderNodes}
        edges={edges}
        onNodesChange={wrappedOnNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodeClick={handleNodeClick}
        onNodeDoubleClick={handleNodeDoubleClick}
        onEdgeClick={handleEdgeClick}
        onPaneClick={handlePaneClick}
        onNodeDragStop={handleNodeDragStop}
        fitView
        fitViewOptions={{ padding: 0.25, maxZoom: 1.1 }}
        minZoom={0.4}
        maxZoom={1.8}
        nodesConnectable={false}
        nodesDraggable
        elementsSelectable
        multiSelectionKeyCode="Shift"
        panOnDrag
        zoomOnScroll
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={28} size={1} color="var(--color-border, #262626)" />
        <Controls showInteractive={false} />

        {/* Top-left toolbar — connection-style selector. */}
        <Panel position="top-left" className="!m-2">
          <div className="flex items-center gap-2 rounded-md border border-border bg-surface-elevated px-2 py-1 text-xs">
            <span className="text-fg-subtle">Connections</span>
            <Select
              value={settings.connectionStyle}
              onValueChange={(v) => setConnectionStyle(v as ConnectionStyle)}
            >
              <SelectTrigger className="h-7 w-32 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="bezier">Bezier</SelectItem>
                <SelectItem value="simple-bezier">Simple bezier</SelectItem>
                <SelectItem value="straight">Straight</SelectItem>
                <SelectItem value="step">Step</SelectItem>
                <SelectItem value="arc">Arc</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </Panel>

        {/* Bottom-center toolbar — appears with a 2+ person multi-select. */}
        {selectedPersonIds.length >= 2 && (
          <Panel position="bottom-center" className="!mb-4">
            {!groupDialogOpen ? (
              <div className="flex items-center gap-2 rounded-md border border-accent/40 bg-surface-elevated px-3 py-2 text-xs shadow-md">
                <span className="text-fg">
                  {selectedPersonIds.length} selected
                </span>
                <Button
                  size="sm"
                  onClick={() => {
                    setGroupNameDraft("");
                    setGroupDialogOpen(true);
                  }}
                >
                  <Users className="h-3.5 w-3.5" />
                  Add to group
                </Button>
              </div>
            ) : (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  createGroupFromSelection(groupNameDraft);
                  setGroupDialogOpen(false);
                  setGroupNameDraft("");
                }}
                className="flex items-center gap-2 rounded-md border border-accent/40 bg-surface-elevated px-3 py-2 text-xs shadow-md"
              >
                <input
                  autoFocus
                  value={groupNameDraft}
                  onChange={(e) => setGroupNameDraft(e.target.value)}
                  placeholder="Group name (e.g. work)"
                  className="h-7 w-44 rounded border border-border bg-surface px-2 text-xs text-fg outline-none focus:border-accent"
                />
                <Button size="sm" type="submit">
                  Save
                </Button>
                <button
                  type="button"
                  onClick={() => setGroupDialogOpen(false)}
                  className="text-fg-subtle hover:text-fg"
                  aria-label="Cancel"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </form>
            )}
          </Panel>
        )}

        {/* Existing groups — stacked under the connections selector
         *  (top-left) so the page-level "Add person" button at top-right
         *  doesn't cover it. */}
        {(groups.length > 0 || placeableGroups.length > 0) && (
          <Panel position="top-left" className="!mx-2 !mt-12">
            <div className="flex max-w-xs flex-col gap-1 rounded-md border border-border bg-surface-elevated px-2 py-1.5 text-xs">
              <div className="text-2xs uppercase tracking-wider text-fg-subtle">
                Groups
              </div>
              {groups.map((g) => (
                <div
                  key={g.id}
                  className="flex items-center justify-between gap-2"
                >
                  <span className="truncate text-fg">{g.name}</span>
                  <span className="text-fg-subtle">
                    {g.memberIds.length}
                  </span>
                  <button
                    type="button"
                    onClick={() => removeGroup(g.id)}
                    className="text-fg-subtle hover:text-fg"
                    aria-label={`Remove group ${g.name}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}

              {placeableGroups.length > 0 && (
                <div className="mt-1 border-t border-border pt-1.5">
                  <Select value="" onValueChange={placeExistingGroup}>
                    <SelectTrigger
                      className="h-7 w-full text-xs"
                      aria-label="Place an existing group on the canvas"
                    >
                      <span className="flex items-center gap-1.5 text-fg-subtle">
                        <Plus className="h-3 w-3" />
                        Place existing group
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {placeableGroups.map((g) => (
                        <SelectItem key={g.id} value={g.id}>
                          {g.name}
                          <span className="ml-2 text-fg-subtle">
                            {g.member_count}
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </div>
          </Panel>
        )}
      </ReactFlow>
    </div>
  );
}

export function PeopleGraph(props: {
  graph: PeopleGraphData;
  selection: GraphSelection | null;
  onSelect: (s: GraphSelection | null) => void;
}) {
  return (
    <ReactFlowProvider>
      <PeopleGraphInner {...props} />
    </ReactFlowProvider>
  );
}
