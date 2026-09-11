import { useEffect, useRef, useCallback, useMemo } from "react";
export interface Connection {
  id: string;
  fromId: string;
  toId: string;
  status: string;
}

const STATUS_COLOR: Record<string, string> = {
  done: "var(--color-status-success, #11d425)",
  running: "var(--color-status-success, #11d425)",
  active: "var(--color-status-success, #11d425)",
  pending: "var(--color-borders-default, #2a2a2a)",
  waiting_approval: "var(--color-status-warning, #c52998)",
  failed: "var(--color-status-error, #f92f77)",
  skipped: "var(--color-borders-default, #2a2a2a)",
  approved: "var(--color-status-warning, #c52998)",
};

const STATUS_DASH: Record<string, string | undefined> = {
  done: undefined,
  running: "8 4",
  active: "8 4",
  pending: "4 8",
  waiting_approval: "8 4",
  failed: undefined,
  skipped: "4 8",
  approved: "8 4",
};

/**
 * SVG bezier connection overlay.
 *
 * Draws once per connections change / container resize. The animation rAF
 * loop only runs when at least one connection is `running`, and updates
 * just the moving dot's `cx`/`cy` instead of rebuilding the SVG every
 * frame. Idle tasks burn zero per-frame work.
 */
export function ConnectionSVG({
  connections,
  containerRef,
}: {
  connections: Connection[];
  containerRef: React.RefObject<HTMLDivElement | null>;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const animRef = useRef<number>(0);
  const progressRef = useRef<Map<string, number>>(new Map());
  const pathsRef = useRef<Map<string, SVGPathElement>>(new Map());
  const dotsRef = useRef<Map<string, SVGCircleElement>>(new Map());

  const hasRunning = useMemo(
    () => connections.some((c) => c.status === "running"),
    [connections],
  );

  const drawPaths = useCallback(() => {
    const svg = svgRef.current;
    const container = containerRef.current;
    if (!svg || !container) return;

    const rect = container.getBoundingClientRect();
    svg.setAttribute("width", String(rect.width));
    svg.setAttribute("height", String(rect.height));
    svg.setAttribute("viewBox", `0 0 ${rect.width} ${rect.height}`);

    while (svg.firstChild) svg.removeChild(svg.firstChild);
    pathsRef.current.clear();
    dotsRef.current.clear();

    for (const conn of connections) {
      const fromEl = container.querySelector(`[data-subtask-id="${conn.fromId}"]`);
      const toEl = container.querySelector(`[data-subtask-id="${conn.toId}"]`);
      if (!fromEl || !toEl) continue;

      const fromRect = fromEl.getBoundingClientRect();
      const toRect = toEl.getBoundingClientRect();

      const x1 = fromRect.left - rect.left + fromRect.width / 2;
      const y1 = fromRect.top - rect.top + fromRect.height;
      const x2 = toRect.left - rect.left + toRect.width / 2;
      const y2 = toRect.top - rect.top;

      const cy = (y2 - y1) * 0.5;
      const d = `M ${x1} ${y1} C ${x1} ${y1 + cy}, ${x2} ${y2 - cy}, ${x2} ${y2}`;

      const color = STATUS_COLOR[conn.status] ?? STATUS_COLOR["pending"]!;
      const dash = STATUS_DASH[conn.status];

      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", color);
      path.setAttribute("stroke-width", "1.5");
      if (dash) path.setAttribute("stroke-dasharray", dash);
      svg.appendChild(path);
      pathsRef.current.set(conn.id, path);

      if (conn.status === "running") {
        const circle = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "circle",
        );
        circle.setAttribute("r", "3");
        circle.setAttribute("fill", color);
        svg.appendChild(circle);
        dotsRef.current.set(conn.id, circle);
      }
    }
  }, [connections, containerRef]);

  // Redraw on connections change.
  useEffect(() => {
    drawPaths();
  }, [drawPaths]);

  // Redraw on layout changes (container resize, window resize). The pipeline
  // column is user-resizable, so card positions move without `connections`
  // changing identity.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => drawPaths());
    ro.observe(container);
    window.addEventListener("resize", drawPaths);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", drawPaths);
    };
  }, [drawPaths, containerRef]);

  // Animate the moving dot only when something is actually running. Updates
  // `cx`/`cy` in place — paths and other DOM are untouched per frame.
  useEffect(() => {
    if (!hasRunning) return;
    let stopped = false;

    const tick = () => {
      if (stopped) return;
      for (const conn of connections) {
        if (conn.status !== "running") continue;
        const path = pathsRef.current.get(conn.id);
        const dot = dotsRef.current.get(conn.id);
        if (!path || !dot) continue;
        const prog = progressRef.current.get(conn.id) ?? 0;
        const next = (prog + 0.005) % 1;
        progressRef.current.set(conn.id, next);
        const pathLen = path.getTotalLength();
        const pt = path.getPointAtLength(pathLen * next);
        dot.setAttribute("cx", String(pt.x));
        dot.setAttribute("cy", String(pt.y));
      }
      animRef.current = requestAnimationFrame(tick);
    };

    animRef.current = requestAnimationFrame(tick);
    return () => {
      stopped = true;
      cancelAnimationFrame(animRef.current);
    };
  }, [hasRunning, connections]);

  return (
    <svg
      ref={svgRef}
      className="pointer-events-none absolute inset-0"
      style={{ zIndex: 0 }}
    />
  );
}
