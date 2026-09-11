// flow-canvas — presentational ReactFlow shell, extracted from flow-designer.
// purpose: one canvas engine, two consumers — the free flow editor and the
// WORK gantt. Provider-free: each page supplies its own <ReactFlowProvider>
// (the flow editor's lives in pages/flow.tsx). B1 extraction is behavior-
// preserving for mode="flow"; mode="gantt" is wired in B2.

import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  type Edge,
  type EdgeMouseHandler,
  type EdgeTypes,
  type Node,
  type NodeMouseHandler,
  type NodeTypes,
  type OnConnect,
  type OnEdgesChange,
  type OnNodesChange,
  type OnNodesDelete,
} from "@xyflow/react";
import React from "react";

import { type GanttZoom, LANE_HEIGHT, PX_PER_DAY, SNAP_DAYS } from "./gantt-scale";

interface FlowCanvasProps {
  mode?: "flow" | "gantt";
  nodes: Node[];
  edges: Edge[];
  nodeTypes: NodeTypes;
  edgeTypes: EdgeTypes;
  accent: string;
  // gantt mode only:
  ganttZoom?: GanttZoom;
  onNodesChange: OnNodesChange;
  onEdgesChange: OnEdgesChange;
  onConnect: OnConnect;
  onNodesDelete: OnNodesDelete;
  onNodeDragStop: NodeMouseHandler;
  onNodeClick: NodeMouseHandler;
  onPaneClick: (event: React.MouseEvent) => void;
  onEdgeClick?: EdgeMouseHandler;
  onEdgeDoubleClick: EdgeMouseHandler;
  onNodeDoubleClick: NodeMouseHandler;
  onNodeMouseEnter?: NodeMouseHandler;
  onNodeMouseLeave?: NodeMouseHandler;
  /** May the viewer CHANGE the graph — drag nodes, draw connections, delete?
   *
   *  Independent of `interactive` on purpose. The three views this canvas
   *  serves need three different combinations, and a single `readOnly` flag
   *  could only express two of them:
   *
   *    /flow, /workflows   editable + interactive   author freely
   *    orchestrator run    interactive only         navigate a live run, never author
   *    slide / deck embed  neither                  a fitted, frozen picture
   *
   *  The middle row is the one a single flag cannot say, and it is exactly what
   *  a 30-subtask run view needs: pan and zoom to read it, but nothing an
   *  operator does may edit a graph the engine owns. */
  editable?: boolean;
  /** May the viewer NAVIGATE — pan, zoom, select, use the Controls?
   *
   *  Selection lives here rather than under `editable` because clicking a node
   *  to read it is inspection, not authoring. A read-only view still needs it. */
  interactive?: boolean;
  /** Tablet multi-select mode: one-finger drag draws a selection box instead
   *  of panning. Pinch-zoom and two-finger pan stay active in both modes. */
  multiSelect?: boolean;
}

// Shared canvas. All graph state + handlers stay in the consumer and arrive as
// props — this component owns rendering only, no local graph state.
export function FlowCanvas({
  mode = "flow",
  nodes,
  edges,
  nodeTypes,
  edgeTypes,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onNodesDelete,
  onNodeDragStop,
  onNodeClick,
  onPaneClick,
  onEdgeClick,
  onEdgeDoubleClick,
  onNodeDoubleClick,
  onNodeMouseEnter,
  onNodeMouseLeave,
  ganttZoom = "week",
  editable = true,
  interactive = true,
  multiSelect = false,
}: FlowCanvasProps) {
  const gantt = mode === "gantt";
  // gantt: snap x to the zoom's day-cell, y to the lane height (vertical lock).
  const snapGrid: [number, number] = [SNAP_DAYS[ganttZoom] * PX_PER_DAY[ganttZoom], LANE_HEIGHT];

  return (
    <ReactFlow
      className={"fd-canvas-" + mode}
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={onConnect}
      onNodesDelete={onNodesDelete}
      onNodeDragStop={onNodeDragStop}
      onNodeClick={onNodeClick}
      onPaneClick={onPaneClick}
      onEdgeClick={onEdgeClick}
      onEdgeDoubleClick={onEdgeDoubleClick}
      onNodeDoubleClick={onNodeDoubleClick}
      onNodeMouseEnter={onNodeMouseEnter}
      onNodeMouseLeave={onNodeMouseLeave}
      // Inspection follows `interactive`; only graph MUTATION follows `editable`.
      edgesFocusable={interactive}
      elementsSelectable={interactive}
      nodesDraggable={editable}
      nodesConnectable={editable}
      zoomOnScroll={interactive}
      zoomOnPinch={interactive}
      zoomOnDoubleClick={interactive}
      // Touch-first defaults: one-finger drag pans, two fingers pinch-zoom/pan.
      // Multi-select mode flips one-finger drag to box-select (pinch stays on).
      // gantt keeps pan-on-drag; Space+drag still pans on desktop in flow mode.
      panOnDrag={gantt ? interactive : interactive && !multiSelect}
      selectionOnDrag={!gantt && interactive && multiSelect}
      panActivationKeyCode={interactive ? "Space" : undefined}
      defaultEdgeOptions={{ interactionWidth: 24 }}
      fitView
      fitViewOptions={{ padding: 0.12 }}
      minZoom={0.1}
      maxZoom={2}
      snapToGrid={gantt}
      snapGrid={snapGrid}
      deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
      proOptions={{ hideAttribution: true }}
    >
      {gantt ? (
        // vertical day gridlines via RF's own Background (reliable layering);
        // the huge y-gap suppresses horizontal lines. Date labels = TimeRuler
        // overlay in the page. ganttOrigin is consumed by that ruler.
        <Background variant={BackgroundVariant.Lines} gap={[PX_PER_DAY[ganttZoom], 100000]} lineWidth={1} color="rgba(255,255,255,0.08)" />
      ) : (
        <Background color="var(--color-border-subtle)" gap={26} size={1.4} />
      )}
      {interactive && <Controls position="bottom-right" orientation="horizontal" />}
    </ReactFlow>
  );
}
