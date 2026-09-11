// gantt-node — the bar a single EpicNode renders as in gantt mode. Separate
// from the editor's FlowNode: same canvas engine, different node type. The WORK
// gantt page registers this under nodeTypes; the flow editor never sees it.
//
// Horizontal-only resize (NodeResizer with height locked to BAR_HEIGHT) = change
// duration. Left/right handles carry finish->start dependency edges. Status drives
// the fill overlay.

import { Handle, NodeResizer, Position } from "@xyflow/react";

import { BAR_HEIGHT, PX_PER_DAY } from "./gantt-scale";

export type EpicStatus = "pending" | "running" | "verified" | "failed";

interface GanttBarData {
  title?: string;
  color?: string;
  status?: EpicStatus;
  fillPct?: number; // 0..100, live progress
}

export function GanttBar({ data, selected }: { data: GanttBarData; selected: boolean }) {
  const status = data.status || "pending";
  const fill = Math.max(0, Math.min(100, data.fillPct ?? 0));

  // NodeResizer + Handles are SIBLINGS of the bar (not inside it): the bar clips
  // its own content (overflow:hidden for the label/fill), which would otherwise
  // clip the resize controls and connector dots, making the bar un-resizable.
  return (
    <>
      <NodeResizer isVisible={selected} minWidth={PX_PER_DAY.day} minHeight={BAR_HEIGHT} maxHeight={BAR_HEIGHT} />
      <Handle type="target" position={Position.Left} className="fd-gbar-handle" />
      <Handle type="source" position={Position.Right} className="fd-gbar-handle" />
      <div
        className={"fd-gbar fd-gbar-" + status + (selected ? " selected" : "")}
        style={{ height: BAR_HEIGHT, borderColor: data.color || undefined }}
      >
        {fill > 0 && <div className="fd-gbar-fill" style={{ width: fill + "%", background: data.color || undefined }} />}
        <span className="fd-gbar-label">{data.title || "untitled"}</span>
      </div>
    </>
  );
}
