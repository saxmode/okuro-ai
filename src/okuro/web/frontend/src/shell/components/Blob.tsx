// SPDX-License-Identifier: Apache-2.0
/**
 * THE SIDEBAR'S BLOB — the REAL pulse engine, back in the panel.
 *
 * ===========================================================================
 * WHAT WAS LOST AND WHAT IS RETURNED
 * ===========================================================================
 * p2's W1 table put `PulseCanvas` in the "untouched, unmounted, with the owner"
 * column, and `.sb-blob` has painted a static `radial-gradient` in its place
 * ever since. The owner, walkthrough 2026-09-15 (todo `9a2e62aa`): *"sidebar gets
 * the REAL blob (the original pulse canvas) back instead of the static
 * gradient."*
 *
 * ===========================================================================
 * THE ENGINE, NOT THE PANEL — AND THAT DISTINCTION IS THE WHOLE DESIGN
 * ===========================================================================
 * `components/pulse/pulse-canvas.tsx` is NOT what comes back. It is a whole
 * sidebar: the orb plus an "OKURO" wordmark, a typewriter status line, a
 * CALLS/AGENTS stat row, an ambient quote, a health dot, a mode cycle and a
 * fullscreen control. The redesigned panel already has its own wordmark, its
 * own rows and its own quote, at Figma-measured positions. Mounting that
 * component would have painted a second copy of all four inside a 230px circle.
 *
 * So what is mounted is `lib/pulse-engine.ts` — the thing that actually draws
 * the blob — on one canvas filling the box `.sb-blob` already defines. The box
 * is untouched: 230px, `top:50%` + `translateY(-50%)`, so its centre stays at
 * y=512 in a 1024-tall frame, and the collapsed 40px variant and both
 * transitions are exactly as they were. Only the PAINT changed.
 *
 * ===========================================================================
 * D1 COSTS NOTHING HERE, WHICH IS WORTH STATING
 * ===========================================================================
 * The engine reads its colours from the document itself —
 * `pulse-engine.ts:163-166` reads `--color-accent`, `--color-accent-hover` and
 * `--color-surface` off `documentElement`, and those are ENGINE names, refilled
 * by `/engine.css` per appearance. It also watches `data-appearance` with a
 * MutationObserver and listens for the sheet-swap event, so an appearance flip
 * or a kit change repaints the blob without anything here knowing. No literal,
 * no bridge token, no second palette.
 *
 * THE PROFILE'S OWN PULSE PREFERENCE ALSO ARRIVES FOR FREE. Settings > Design
 * writes `--pulse-outlines-only` and `--pulse-outline-strength` via
 * `theme.ts::applyPulseOutlineOverride`, `main.tsx:27` replays it from cache
 * before first paint, and the engine reads both in the same `refreshTokens()`.
 * That is the one animation preference the engine has, and it is honoured
 * because it is honoured upstream.
 *
 * ===========================================================================
 * THREE DELIBERATE SUBTRACTIONS
 * ===========================================================================
 * 1. REDUCED MOTION STOPS THE CANVAS FROM MOUNTING AT ALL, and the static
 *    gradient is what the user sees instead. It is one mechanism rather than
 *    two: `.sb-blob:has(> canvas)` is what removes the gradient, so no canvas
 *    means the gradient is still there, already correct, already token-driven.
 *    A paused canvas would have been a blank hole. The query is LIVE — a user
 *    who flips the OS setting gets the change without a reload.
 *
 * 2. THE CANVAS TAKES NO POINTER EVENTS. The engine's constructor installs a
 *    `wheel` listener with `preventDefault` that drives a 1x-5x zoom, and its
 *    host component wires click-to-open-chat. Neither surface exists here: the
 *    panel toggles on click and has no chat. With pointer events off, the wheel
 *    handler cannot fire and a click on the blob still reaches the `<aside>` and
 *    toggles the panel — i.e. the shipped behaviour is preserved exactly. The
 *    blob is decorative in this shell; when it gets a surface, it gets its
 *    events back in the same change.
 *
 * 3. ACTIVITY IS FED IN AGAIN — this is where the third subtraction WAS, and
 *    it is reversed rather than edited, because it was the defect the owner
 *    reported as "agentic activity is not visualized anymore".
 *
 *    THE ENGINE'S SIZE IS THE VISUALIZATION, which is what the old text missed:
 *    `baseR = min(w,h) * (0.12 + intensity * 0.22)`. Fed nothing, intensity
 *    stays 0 and the blob sits at its FLOOR in both states — 27.6px of ink in a
 *    230px box. The design's blob is the same engine at intensity ~1. So "no
 *    activity" and "the open blob is too small" were one defect with one cause.
 *
 *    The subscription is NOT taken here. `PulseData.tsx` holds it, mounted by
 *    the frame, because the panel's bar meter reads the same numbers and two
 *    subscriptions would be two answers to one question. That file also states
 *    the cost this moves onto every route, and why the three polls stay
 *    affordable (query dedupe, background pause, the socket was never paused).
 */

import { useEffect, useRef, useState } from "react";
import { PulseEngine } from "@/lib/pulse-engine";
import { useShellPulse } from "./PulseData";
import { ActivityMeter } from "./ActivityMeter";

const REDUCED = "(prefers-reduced-motion: reduce)";

export function Blob() {
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // THE ENGINE OUTLIVES EVERY DATA EFFECT BELOW, so it is a ref rather than
  // state: pushing activity must never re-run the mount effect, which would
  // destroy and rebuild the canvas on every socket message.
  const engineRef = useRef<PulseEngine | null>(null);
  const fsRef = useRef<HTMLCanvasElement>(null);
  const [full, setFull] = useState(false);
  const [zoom, setZoom] = useState(1);
  const { activity, activeRoles, isActive, activityStream } = useShellPulse();

  // Read once for the first paint, then track it. `matchMedia` is guarded
  // because this module is imported by the vitest environment too, where a
  // missing matchMedia would throw during render rather than in an effect.
  const [animate, setAnimate] = useState(
    () => typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? !window.matchMedia(REDUCED).matches
      : true,
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(REDUCED);
    const onChange = () => setAnimate(!mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  /* THE MIRROR IS WIRED WHEN ITS CANVAS EXISTS, not when the flag flips —
     React mounts the overlay on the same commit, so the ref is only populated
     by the time this effect runs. Leaving fullscreen tells the engine to stop
     mirroring before the node goes away, or it paints into a detached canvas. */
  useEffect(() => {
    const engine = engineRef.current;
    if (!engine) return;
    if (full && fsRef.current) engine.enterFullscreen(fsRef.current);
    else engine.exitFullscreen();
  }, [full]);

  /* THE CANVAS ZOOMS, NOT THE PAINT INSIDE IT — the owner, 2026-09-19: "the
     canvas zooms with the blob. so the blob isn't zooming within the canvas.
     edges of the canvas are not visible anymore."

     The engine's own wheel handler drives `ctx.scale`, which magnifies the
     drawing INSIDE a 230px bitmap: past about 1.5x the blob reaches the buffer
     edge and is cut off square, which is the square that appeared. Scaling the
     ELEMENT has no such edge — the bitmap is unchanged and the browser
     composites it larger, so the silhouette survives and there is nothing to
     crop. `.sb-blob` allows overflow, so it spills rather than clips.

     CAPTURE, because the engine listens on the canvas itself and registered
     first. Taking the event on the host in the capture phase is what stops the
     two zooms from both applying — otherwise the paint scales inside a box that
     is also scaling, and the two multiply. */
  /* THE BUFFER FOLLOWS THE BOX IN THE SAME FRAME. The ResizeObserver would get
     there on its 200ms debounce, which on a wheel is a visible soft step before
     it snaps sharp. `sizeImmediate` reads the parent's box, and by the time a
     rAF callback runs the new width is laid out. */
  useEffect(() => {
    const engine = engineRef.current;
    if (!engine) return;
    const id = requestAnimationFrame(() => engine.sizeImmediate());
    return () => cancelAnimationFrame(id);
  }, [zoom]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !animate) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setZoom((z) => Math.max(1, Math.min(5, z + (e.deltaY > 0 ? -0.15 : 0.15))));
    };
    host.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => host.removeEventListener("wheel", onWheel, { capture: true } as EventListenerOptions);
  }, [animate]);

  useEffect(() => {
    if (!full) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFull(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [full]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!animate || !canvas) return;

    /* `cssSized` — A-3 #6, AND IT IS WHAT MAKES THE CSS FIX POSSIBLE AT ALL.
       The engine's own sizing writes `canvas.style.width/height` in pixels, and
       an inline style beats any stylesheet, so the canvas could not be told to
       follow an animating box from CSS while the engine owned its element. With
       this flag the engine sizes only its BUFFER and `.sb-orb{width:100%;
       height:100%}` owns the box — which is how the canvas tracks the 650ms
       transition every frame with no JavaScript in the loop.

       MEASURED before it, per frame, both engines: on expand the canvas rect
       stayed 40px wide for the whole 1100ms window while its box eased to 204,
       so the blob painted in the top-left corner and snapped at the end. */
    const engine = new PulseEngine({ canvas, transparent: true, cssSized: true });
    engineRef.current = engine;
    // `sizeImmediate` before `start`: the debounced `resize` would leave the
    // buffer at 0x0 for 200ms, which is one visible blank frame on mount.
    engine.sizeImmediate();
    engine.start();

    // THE BOX ANIMATES, SO THE BUFFER HAS TO FOLLOW IT. `.sb-blob`'s height is a
    // `calc()` clamped against the panel's own height AND it transitions 230 ->
    // 40 on collapse, so a canvas sized once on mount would stretch. The engine
    // sizes from `canvas.parentElement`, which is this host.
    const host = hostRef.current;
    /* THE FIRST OBSERVATION RESAMPLES AT ONCE, THE REST ARE DEBOUNCED.
       `sizeImmediate()` above runs before the browser has laid the panel out,
       so the buffer it allocates is whatever the box measures pre-layout — in a
       flex column that is not yet the final 230 square. ResizeObserver delivers
       the first real size right after layout, and taking that one immediately
       is what stops the canvas painting a stale aspect into a correct box. The
       200ms debounce stays for everything after: a window drag streams sizes,
       and reallocating a buffer per frame there is the cost it was added to
       avoid. */
    let firstObservation = true;
    const ro = new ResizeObserver(() => {
      if (firstObservation) {
        firstObservation = false;
        engine.sizeImmediate();
        return;
      }
      engine.resize();
    });
    if (host) ro.observe(host);

    /* THE BUFFER RESAMPLES WHEN THE BOX STOPS, NOT 200ms AFTER IT — AND THAT IS
       AN EVENT, NOT A SECOND CLOCK. The 200ms debounce alone decided when a
       650ms transition was over, so the blob stayed scaled for 850ms. The
       debounce stays underneath as the backstop for a window resize, which
       fires no transition at all.

       THE EVENT MOVED, AND THIS IS THE BUG THAT TAUGHT US WHY. It used to be
       `transitionend` for `height` ON THIS HOST, because `.sb-blob` carried an
       animated `height: var(--sh-sb-blob-d)`. The panel became a flex column on
       2026-09-19 and the blob's height became `auto`, resolved from
       `flex-basis` — which nothing transitions. So the listener stopped firing
       and NOTHING resampled the buffer promptly: the canvas kept whatever
       aspect it had at mount and the paint stretched to fill a square box.
       The owner saw it immediately; the DOM box measured a correct 230x230 in
       both engines, which is exactly why the box was the wrong place to look.

       WHAT ACTUALLY MOVES THE BOX NOW is the panel: `.sidebar` transitions
       `flex-basis` between open and collapsed, and the blob's width follows it.
       So the arrival event belongs to the panel, not to the blob. Filtered on
       `propertyName` for the same reason as before — one reallocation per
       arrival, not one per property. */
    const panel = host?.closest(".sidebar") ?? null;
    const onArrived = (event: Event) => {
      if ((event as TransitionEvent).propertyName === "flex-basis") engine.sizeImmediate();
    };
    panel?.addEventListener("transitionend", onArrived);

    return () => {
      ro.disconnect();
      panel?.removeEventListener("transitionend", onArrived);
      // `destroy`, not `stop`: it also cancels the frame, clears three timers
      // and runs the cleanup list that removes the wheel listener. `stop` alone
      // leaks all four across a StrictMode double-mount.
      engineRef.current = null;
      engine.destroy();
    };
  }, [animate]);

  /* THREE PUSHES, THREE EFFECTS, EACH ON ITS OWN DEPENDENCY — the same split
     `pulse-canvas.tsx` uses, and for the same reason: the three arrive at
     different rates (socket, 5s, 3s) and one combined effect would replay all
     three whenever the fastest of them ticked.

     Guarded on the ref, not on `animate`: under reduced motion no engine is
     constructed, so every push is a no-op and the static gradient stays. */
  useEffect(() => {
    if (activity) engineRef.current?.updateActivity(activity);
  }, [activity]);

  useEffect(() => {
    engineRef.current?.updateActiveRoles(activeRoles, isActive);
  }, [activeRoles, isActive]);

  useEffect(() => {
    if (activityStream.length) engineRef.current?.processActivityStream(activityStream);
  }, [activityStream]);

  return (
    <>
      <div
        className="sb-blob"
        ref={hostRef}
        /* THE BOX CARRIES THE ZOOM, so the engine redraws at the new size and
           the geometry stays sharp. `--sh-blob-dur` swaps the clock: the wheel
           is direct manipulation and must answer at once, while the panel's own
           collapse of this same `width` stays on the shell's 650ms. */
        style={
          zoom === 1
            ? undefined
            : ({
                "--sh-blob-zoom": String(zoom),
                "--sh-blob-dur": "var(--interaction-duration)",
              } as React.CSSProperties)
        }
        /* DOUBLE-CLICK GOES FULLSCREEN — the owner, 2026-09-19. The engine has
           carried `enterFullscreen`/`exitFullscreen` all along and paints the
           mirror in its own frame loop; nothing here drives a second one. */
        onDoubleClick={() => {
          const engine = engineRef.current;
          if (!engine) return;
          setFull((v) => !v);
        }}
      >
        {animate && <canvas ref={canvasRef} className="sb-orb" aria-hidden="true" />}
      </div>
      {full && (
        /* THE MIRROR IS A SIBLING, NOT A CHILD. The panel clips its own box and
           is 300px wide; a fullscreen canvas inside it would be cropped to the
           blob. Escape closes it, and the surface itself is click-to-close so
           there is no chrome to explain. */
        <div
          className="sb-orb-full"
          role="presentation"
          onClick={() => setFull(false)}
          onDoubleClick={() => setFull(false)}
        >
          <canvas ref={fsRef} aria-hidden="true" />
          {/* THE WORDMARK SITS UNDER THE BLOB, THE METER AT THE FOOT — the owner,
              2026-09-19. Both are the panel's own furniture, re-placed rather
              than re-invented: `.sb-mark` is the same class the panel head
              wears, and `ActivityMeter` is the same component, so the type
              scale, the bar pitch and the reading all come from one place. The
              canvas is `position:absolute` behind them; these two sit above it
              in the grid and take no pointer events, so a click anywhere still
              closes. */}
          <span className="sb-mark sb-full-mark">OKURO</span>
          <div className="sb-full-meter">
            <ActivityMeter />
          </div>
        </div>
      )}
    </>
  );
}
