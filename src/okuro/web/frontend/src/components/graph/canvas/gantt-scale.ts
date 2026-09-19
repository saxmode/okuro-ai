// gantt-scale — pure x<->date mapping for the gantt view of the flow canvas.
// The canvas stays a normal ReactFlow plane; gantt is just a convention on top:
//   node.position.x  <->  start date     (via pxPerDay)
//   node width       <->  durationDays
//   node.position.y  <->  lane row
// No ReactFlow coupling here — just math, so it is trivially testable.

export type GanttZoom = "day" | "week" | "month" | "quarter" | "year";

// Pixels per calendar day at each zoom. Wider = more detail per day, and more
// horizontal room for dependency curves between consecutive units.
export const PX_PER_DAY: Record<GanttZoom, number> = {
  day: 120,
  week: 38,
  month: 9,
  quarter: 4,
  year: 1.6,
};

// Inset (px per side) of a bar inside its time slot, so the bar — and its
// connection handles — don't sit flush on the unit boundary. Gives dependency
// curves clean entry/exit room.
export const BAR_GUTTER = 8;

// Days of clearance a dependent keeps after its predecessor ends (finish->start).
export const DEP_GAP_DAYS = 2;

// Pixel step the canvas snaps to horizontally at each zoom (one "cell").
// day-zoom snaps per day; coarser zooms snap per week to stay usable.
export const SNAP_DAYS: Record<GanttZoom, number> = {
  day: 1,
  week: 1,
  month: 7,
  quarter: 7,
  year: 7,
};

export const LANE_HEIGHT = 56; // px per swimlane row
export const BAR_HEIGHT = 34; // px, slim bar inside the lane
export const AXIS_HEIGHT = 40; // px reserved at top for the time ruler

const MS_PER_DAY = 86_400_000;

export function daysBetween(from: Date, to: Date): number {
  return (startOfDay(to).getTime() - startOfDay(from).getTime()) / MS_PER_DAY;
}

export function addDays(d: Date, days: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + Math.round(days));
  return r;
}

export function startOfDay(d: Date): Date {
  const r = new Date(d);
  r.setHours(0, 0, 0, 0);
  return r;
}

// date -> canvas x (origin is the left edge = day 0)
export function dateToX(date: Date, origin: Date, zoom: GanttZoom): number {
  return daysBetween(origin, date) * PX_PER_DAY[zoom];
}

// canvas x -> date
export function xToDate(x: number, origin: Date, zoom: GanttZoom): Date {
  return addDays(origin, x / PX_PER_DAY[zoom]);
}

export function durationToWidth(days: number, zoom: GanttZoom): number {
  return Math.max(1, days) * PX_PER_DAY[zoom];
}

export function widthToDuration(width: number, zoom: GanttZoom): number {
  return Math.max(1, Math.round(width / PX_PER_DAY[zoom]));
}

// lane index -> canvas y (below the axis)
export function laneToY(lane: number): number {
  return AXIS_HEIGHT + lane * LANE_HEIGHT + (LANE_HEIGHT - BAR_HEIGHT) / 2;
}

// canvas y -> nearest lane index
export function yToLane(y: number): number {
  return Math.max(0, Math.round((y - AXIS_HEIGHT) / LANE_HEIGHT));
}

// snap a canvas x to the zoom's day grid, relative to origin
export function snapX(x: number, zoom: GanttZoom): number {
  const cell = SNAP_DAYS[zoom] * PX_PER_DAY[zoom];
  return Math.round(x / cell) * cell;
}
