// time-axis — TimeRuler: a DOM overlay of date labels pinned to the top of the
// gantt canvas, tracking the live ReactFlow viewport (pan/zoom). A plain DOM
// layer (not an SVG canvas child) so layering is predictable; gridlines
// themselves come from RF's own <Background variant=Lines>.

import { useViewport } from "@xyflow/react";

import { type GanttZoom, AXIS_HEIGHT, addDays, dateToX, startOfDay, xToDate } from "./gantt-scale";

const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const isMonday = (d: Date) => d.getDay() === 1;
const isFirstOfMonth = (d: Date) => d.getDate() === 1;
const isFirstOfQuarter = (d: Date) => isFirstOfMonth(d) && d.getMonth() % 3 === 0;

// Which days carry a label, per zoom, and how to format it.
function labelRule(zoom: GanttZoom) {
  switch (zoom) {
    case "day":
      return { show: (_d: Date) => true, fmt: (d: Date) => `${WEEKDAY[d.getDay()]} ${d.getDate()}` };
    case "week":
      return { show: isMonday, fmt: (d: Date) => `${d.getDate()}.${d.getMonth() + 1}` };
    case "month":
      return { show: isFirstOfMonth, fmt: (d: Date) => `${MONTH[d.getMonth()]} ${String(d.getFullYear()).slice(2)}` };
    case "quarter":
      return { show: isFirstOfMonth, fmt: (d: Date) => `${MONTH[d.getMonth()]} ${String(d.getFullYear()).slice(2)}` };
    case "year":
    default:
      return { show: isFirstOfQuarter, fmt: (d: Date) => `Q${Math.floor(d.getMonth() / 3) + 1} ${d.getFullYear()}` };
  }
}

export function TimeRuler({ origin, zoom }: { origin: Date; zoom: GanttZoom }) {
  const { x: tx, zoom: rfZoom } = useViewport();
  const rule = labelRule(zoom);

  // Start just off the left edge (screen x = 0 -> flow x = -tx/rfZoom) and walk
  // forward a day at a time until we run past a generous screen extent. No
  // container-width dependency; the ruler's overflow clips the tail.
  const start = startOfDay(xToDate(-tx / rfZoom, origin, zoom));
  const labels: { x: number; text: string }[] = [];
  let d = new Date(start);
  for (let i = 0; i < 800; i++) {
    const screenX = dateToX(d, origin, zoom) * rfZoom + tx;
    if (screenX > 6000) break;
    if (screenX > -120 && rule.show(d)) labels.push({ x: screenX, text: rule.fmt(d) });
    d = addDays(d, 1);
  }

  return (
    <div className="fd-gantt-ruler" style={{ height: AXIS_HEIGHT }}>
      {labels.map((l, i) => (
        <span key={i} className="fd-gantt-rlabel" style={{ left: l.x }}>
          {l.text}
        </span>
      ))}
    </div>
  );
}
