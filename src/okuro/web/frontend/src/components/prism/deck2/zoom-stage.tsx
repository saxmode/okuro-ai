// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — React ZoomStage wrapper. Mounts the kit's
//   `.zoom-stage > .zoom-canvas` DOM and drives it with the kit's own scale math
//   (window.PrismZoomStage.apply) under a ResizeObserver, so a fixed 1600×900
//   slide scales-to-fit any cell/viewport (fit · 1:1 · manual ±). The kit owns
//   the CSS + the scale formula; this only mounts nodes and re-applies on resize.
// AGENT_HEADER_END -->
import { useCallback, useEffect, useRef, type ReactNode } from "react";
import { zoomStageApi } from "./kit-assets";

export type ZoomMode = "fit" | "100" | "manual";

export interface ZoomStageProps {
  children: ReactNode;
  /** Presenter controls (fit / − / readout / + / 1:1) rendered on the stage. */
  controls?: boolean;
  /** Enable +/−/0 keyboard zoom when the stage is focused. */
  keyboard?: boolean;
  className?: string;
}

/** A fit-to-box zoom stage around a 1600×900 canvas. */
export function ZoomStage({ children, controls = false, keyboard = false, className }: ZoomStageProps) {
  const stageRef = useRef<HTMLDivElement>(null);
  const readoutRef = useRef<HTMLSpanElement>(null);

  const apply = useCallback(() => {
    const stage = stageRef.current;
    if (!stage) return;
    zoomStageApi()?.apply(stage);
  }, []);

  // Re-apply on stage resize (fills large/ultra screens, shrinks on small).
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    apply();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", apply);
      return () => window.removeEventListener("resize", apply);
    }
    const ro = new ResizeObserver(() => apply());
    ro.observe(stage);
    return () => ro.disconnect();
  }, [apply]);

  const setMode = useCallback(
    (mode: ZoomMode) => {
      const stage = stageRef.current;
      if (!stage) return;
      stage.dataset.zoomMode = mode;
      apply();
    },
    [apply],
  );

  const bump = useCallback(
    (factor: number) => {
      const stage = stageRef.current;
      if (!stage) return;
      const api = zoomStageApi();
      const cur = parseFloat(stage.dataset.zoomManual || String(api ? api.fitScale(stage) : 1));
      stage.dataset.zoomMode = "manual";
      stage.dataset.zoomManual = String(cur * factor);
      apply();
    },
    [apply],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!keyboard) return;
      if (e.key === "+" || e.key === "=") {
        bump(1.1);
        e.preventDefault();
      } else if (e.key === "-" || e.key === "_") {
        bump(1 / 1.1);
        e.preventDefault();
      } else if (e.key === "0") {
        setMode("fit");
        e.preventDefault();
      }
    },
    [keyboard, bump, setMode],
  );

  return (
    <div
      ref={stageRef}
      className={`zoom-stage${className ? " " + className : ""}`}
      data-zoom-mode="fit"
      tabIndex={keyboard ? 0 : undefined}
      onKeyDown={onKeyDown}
    >
      <div className="zoom-canvas">{children}</div>
      {controls ? (
        <div className="zoom-controls">
          <button data-mode="fit" onClick={() => setMode("fit")}>
            fit
          </button>
          <button data-mode="out" onClick={() => bump(1 / 1.1)}>
            −
          </button>
          <span className="zoom-readout" ref={readoutRef}>
            100%
          </span>
          <button data-mode="in" onClick={() => bump(1.1)}>
            +
          </button>
          <button data-mode="100" onClick={() => setMode("100")}>
            1:1
          </button>
        </div>
      ) : null}
    </div>
  );
}
