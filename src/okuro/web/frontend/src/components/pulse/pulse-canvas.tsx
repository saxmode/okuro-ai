import { useEffect, useRef, useState, useCallback, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { PanelLeftClose } from "lucide-react";
import { PulseEngine } from "@/lib/pulse-engine";
import type { PulseActivity } from "@/lib/pulse-engine";
import { useChrome } from "@/lib/chrome-context";
import { useFrameStamp } from "@/lib/ground-portal";
import { useIsMobile } from "@/hooks/use-is-mobile";
import { QuoteDisplay } from "./quote-display";
import { BriefDot, useBriefIndicator } from "./brief-indicator";

/**
 * Activity level (0..1) the orb is forced to while the morning brief plays.
 * Near the engine's ceiling (baseR maxes at intensity 1.0) so okuro reads as
 * mid-monologue rather than merely busy.
 */
const SPEAKING_INTENSITY = 0.85;

interface PulseCanvasProps {
  activity?: PulseActivity;
  activeRoles?: string[];
  isActive?: boolean;
  activityStream?: Array<{ ts: string; tool?: string; preview?: string }>;
  services?: Array<{ active: string }>;
  className?: string;
  /**
   * Optional live-status indicator (typically `<OkuroThinker />`),
   * rendered in the middle text stack between the CALLS/AGENTS row
   * and the ambient quote — co-located with the other "what's
   * happening right now" affordances rather than next to the
   * accordion controls below the canvas.
   */
  thinker?: ReactNode;
  /** When true the orb shrinks up + text fades, surfacing the chat behind. */
  chatOpen?: boolean;
  /** Clicking the orb opens the chat (no-op while already open). */
  onOpenChat?: () => void;
}

/**
 * Pulse panel — left-column sidebar canvas.
 *
 * Layout (top → bottom, per design mockup):
 *   1. Blob animation, constrained to the upper ~60% of the panel
 *   2. "OKURO" title, very wide letter-spacing
 *   3. One-line status subtitle (typewriter; "executing cortex_route…" / "idle")
 *   4. Two stat pairs — {number} {LABEL} × {CALLS, AGENTS}
 *   5. Ambient quote at the bottom
 *
 * Chrome demoted to corners: health dot (bottom-left), mode cycle (tiny,
 * bottom-right, only on hover), fullscreen (top-right, only on hover).
 */
export function PulseCanvas({
  activity,
  activeRoles,
  isActive,
  activityStream,
  services,
  className,
  thinker,
  chatOpen = false,
  onOpenChat,
}: PulseCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const fsCanvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<PulseEngine | null>(null);
  // Shell-chrome API: the minimize control hides the tab bar (NavBar) + side
  // panel (Sidebar) to give content the full window. Desktop-only — on mobile
  // the chrome isn't present and useChrome() is a no-op, so gate the button.
  const chrome = useChrome();
  const isMobile = useIsMobile();

  const [statusText, setStatusText] = useState("");
  const [statusVisible, setStatusVisible] = useState(false);
  const [cursorVisible, setCursorVisible] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const { probe, frameProps } = useFrameStamp();
  const [servicesTimedOut, setServicesTimedOut] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">(
    "idle",
  );
  const brief = useBriefIndicator();
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // If services data never arrives (backend down, CORS, …), stop lying with
  // "LOADING" after 8s and surface "UNREACHABLE" instead.
  useEffect(() => {
    if (services) {
      setServicesTimedOut(false);
      return;
    }
    const t = setTimeout(() => setServicesTimedOut(true), 8_000);
    return () => clearTimeout(t);
  }, [services]);

  const onStatusText = useCallback((text: string, cursor: boolean) => {
    setStatusText(text);
    setStatusVisible(text.length > 0);
    setCursorVisible(cursor);
  }, []);

  // Engine lifecycle
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const engine = new PulseEngine({
      canvas,
      onStatusText,
      // The orb never paints its own background — it composites onto the
      // sidebar surface (closed) or the chat behind it (open). clearRect is
      // no costlier than the old per-frame opaque fill.
      transparent: true,
    });

    engine.sizeImmediate();
    engine.start();
    engineRef.current = engine;

    return () => {
      engine.destroy();
      engineRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ResizeObserver on the CONTAINER, not the canvas.
  //
  // The canvas has explicit pixel width/height set by pulse-engine.resize()
  // (for DPR scaling). Once those px dimensions are baked in, an RO on the
  // canvas itself doesn't fire when the outer panel reflows — the CSS size
  // is pinned. Observing the container tracks every parent-driven size
  // change (accordion opens, sidebar resize, window resize) and pulse-engine
  // re-reads parent.clientWidth/Height inside resize().
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => {
      engineRef.current?.resize();
    });
    ro.observe(container);
    return () => ro.disconnect();
  }, []);

  // Activity data → engine
  useEffect(() => {
    if (activity && engineRef.current) {
      engineRef.current.updateActivity(activity);
    }
  }, [activity]);

  // Active roles → engine (satellites around the blob)
  useEffect(() => {
    if (engineRef.current) {
      engineRef.current.updateActiveRoles(activeRoles ?? [], isActive ?? false);
    }
  }, [activeRoles, isActive]);

  // Activity stream → engine
  useEffect(() => {
    if (activityStream && engineRef.current) {
      engineRef.current.processActivityStream(activityStream);
    }
  }, [activityStream]);

  // Brief playback → engine. While okuro is speaking the orb ignores real
  // agent activity and runs hot; releasing the override drops it back to
  // whatever is actually happening. The orb is okuro's mouth — a brief playing
  // over an idle-looking orb would read as a bug, not as speech.
  useEffect(() => {
    engineRef.current?.setActivityOverride(brief.speaking ? SPEAKING_INTENSITY : null);
  }, [brief.speaking]);

  // Fullscreen. Order matters: the fullscreen <canvas> lives inside a
  // `{fullscreen && (...)}` branch, so its ref is null until after React
  // rerenders with fullscreen=true. Calling engine.enterFullscreen here
  // would hit a null canvas and silently no-op — the regression that
  // made the icon look broken. We flip the state flag first, then wire
  // the canvas to the engine in a post-render effect.
  const handleFsEnter = useCallback(() => {
    setFullscreen(true);
  }, []);

  const handleFsExit = useCallback(() => {
    engineRef.current?.exitFullscreen();
    setFullscreen(false);
  }, []);

  useEffect(() => {
    if (!fullscreen) return;
    const fsCanvas = fsCanvasRef.current;
    if (!fsCanvas || !engineRef.current) return;
    engineRef.current.enterFullscreen(fsCanvas);
  }, [fullscreen]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && fullscreen) handleFsExit();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [fullscreen, handleFsExit]);

  // Double-click on the blob canvas → copy current frame as SVG.
  // The engine recomputes the live geometry into an inline <svg> string we
  // can paste straight into Figma. Clipboard write is async + can fail in
  // non-secure contexts, so surface a brief toast either way.
  const handleCopySVG = useCallback(async () => {
    const engine = engineRef.current;
    if (!engine) return;
    const svg = engine.snapshotSVG();
    try {
      await navigator.clipboard.writeText(svg);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    copyTimerRef.current = setTimeout(() => setCopyState("idle"), 1500);
  }, []);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    };
  }, []);

  // Health status from services — shown as a tiny dot + label in the
  // bottom-left, not a prominent top bubble.
  //
  // Severity mirrors doctor checks: real `fail` maps to "failed" in the
  // services array and is the only thing that escalates to ERROR. `warn`
  // maps to "degraded" and surfaces as DEGRADED — it should never be
  // mistaken for a broken system. Fresh installs frequently have warn-level
  // checks (no discrete GPU on Mac, embeddings model not cached, no AI CLI
  // authenticated yet); those must not paint the panel red.
  const failedCount = services
    ? services.filter((s) => s.active === "failed").length
    : -1;
  const degradedCount = services
    ? services.filter((s) => s.active === "degraded").length
    : 0;
  const healthLabel =
    failedCount < 0
      ? servicesTimedOut ? "UNREACHABLE" : "LOADING"
      : failedCount > 0
        ? "ERROR"
        : degradedCount > 0
          ? "DEGRADED"
          : "HEALTHY";
  const healthColor =
    failedCount < 0
      ? servicesTimedOut
        ? "var(--color-status-error, #f92f77)"
        : "var(--color-foreground-tertiary, #666)"
      : failedCount > 0
        ? "var(--color-status-error, #f92f77)"
        : degradedCount > 0
          ? "var(--color-status-warning, #c52998)"
          : "var(--color-status-success, #11d425)";

  // Status subtitle: the typewriter line for active work. When nothing is
  // happening, render "idle" as an invisible placeholder so the slot keeps
  // its height and the layout doesn't shift — okuro never sleeps.
  const subtitleActive = statusVisible && !!statusText;
  const subtitle = subtitleActive ? statusText : "idle";
  const subtitleCursor = statusVisible && cursorVisible;

  // Stats — "—" when we have no pulse data yet (distinguishes from "0").
  const callsText = activity ? String(activity.calls) : "\u2014";
  const agentsText = activity ? String(activity.live) : "\u2014";

  return (
    <>
      {/* The single <audio> for the brief, owned here rather than by either
          dot: both dots (sidebar + fullscreen) are mounted at once, so a
          per-dot player would keep playing after the other took over. */}
      {brief.audio}

      <div
        ref={containerRef}
        onDoubleClick={handleCopySVG}
        className={`group relative h-full overflow-hidden transition-colors duration-300 ${
          chatOpen ? "bg-transparent" : "bg-surface"
        } ${className ?? ""}`}
      >
        {/* Blob canvas — fills the whole panel so the animation is
            center-center. The text stack below is overlaid and positioned
            at 75% (midway between panel center and bottom). Double-click
            copies the current frame as inline SVG for Figma paste. */}
        <canvas
          ref={canvasRef}
          className="pointer-events-none absolute inset-0 block"
        />

        {/* Chat opener — scoped to the orb (centered on the blob, cx=w/2,cy=h/2)
            instead of the whole panel, so clicks on the rest of the sidepanel
            (e.g. near the minimize corner, or empty space) don't fire it.
            Only mounted when a host actually wires chat: an unwired opener is a
            ~230px transparent target sitting over the ~10px BriefDot, so every
            near-miss on the brief would land on a button that does nothing. */}
        {!chatOpen && onOpenChat && (
          <button
            type="button"
            onClick={() => onOpenChat()}
            aria-label="Chat with okuro"
            title="chat with okuro"
            className="absolute left-1/2 top-1/2 z-[5] aspect-square w-[68%] max-w-[240px] -translate-x-1/2 -translate-y-1/2 cursor-pointer rounded-full bg-transparent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          />
        )}

        {/* Text stack — OKURO block, stats block, quote block. Same stack
            re-rendered inside the fullscreen overlay below so nothing is
            lost when the pulse takes over the viewport. 24 px gap between
            blocks; the stack centerline sits at 75 % of panel height (the
            midpoint between panel center and bottom). */}
        <PulseTextStack
          subtitle={subtitle}
          subtitleActive={subtitleActive}
          subtitleCursor={subtitleCursor}
          callsText={callsText}
          agentsText={agentsText}
          isActive={isActive ?? false}
          thinker={thinker}
          collapsed={chatOpen}
          briefIndicator={<BriefDot state={brief} hidden={chatOpen} />}
        />

        {/* Health dot — small, bottom-left, unobtrusive. Hidden while the
            chat is open so it doesn't sit over the conversation. */}
        <div
          className={`pointer-events-none absolute bottom-3 left-3 z-10 flex items-center gap-2 transition-opacity duration-300 ${
            chatOpen ? "opacity-0" : "opacity-100"
          }`}
        >
          <div
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ background: healthColor }}
          />
          <span className="font-mono text-3xs uppercase tracking-[0.2em] text-tertiary">
            {healthLabel}
          </span>
        </div>

        {/* Copy-SVG toast — bottom-right, appears for ~1.5s after a
            double-click on the canvas. */}
        {copyState !== "idle" && (
          <div
            className="pointer-events-none absolute bottom-3 right-3 z-10 font-mono text-3xs uppercase tracking-[0.2em]"
            style={{
              color:
                copyState === "copied"
                  ? "var(--color-status-success, #11d425)"
                  : "var(--color-status-error, #f92f77)",
            }}
          >
            {copyState === "copied" ? "SVG COPIED" : "COPY FAILED"}
          </div>
        )}

        {/* Fullscreen — top-right, visible on hover only. */}
        {/* Minimize — top-left, mirror of the fullscreen control. Hides the
            tab bar + side panel (app chrome) for a full-window content view.
            Desktop-only; the restore control lives in AppShell (shown while
            chrome is hidden). Hover-reveal like its twin. */}
        {!isMobile && (
          <button
            onClick={() => chrome.hide()}
            aria-label="Hide tab bar and side panel"
            // Hover-reveal on pointer devices; always visible on touch (iPad has
            // no app header bar / wordmark chevron, so this is its only minimize).
            className="absolute left-2 top-2 z-[15] flex h-5 w-5 cursor-pointer items-center justify-center bg-transparent text-tertiary opacity-0 transition-colors hover:text-fg-muted focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
            title="Minimize — hide tabs + side panel"
          >
            <PanelLeftClose className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        )}

        <button
          onClick={handleFsEnter}
          aria-label="Open pulse canvas fullscreen"
          className="absolute right-2 top-2 z-[15] h-5 w-5 cursor-pointer bg-transparent text-center font-mono text-2xs leading-[18px] text-tertiary opacity-0 transition-colors hover:text-fg-muted focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 group-hover:opacity-100"
          title="Fullscreen"
        >
          <span aria-hidden="true">{"\u26F6"}</span>
        </button>
      </div>

      {/* Fullscreen overlay — same text stack as the collapsed view so
          agent activity, CALLS/AGENTS stats, and the ambient quote stay
          visible when the pulse takes over the whole viewport. The
          stack self-positions at 75 % of the container height, matching
          the compact sidebar render exactly.

          Portaled to <body>: `fixed inset-0 z-50` is not enough on its own,
          because this canvas renders inside the Sidebar's `relative z-20`
          wrapper. That wrapper opens a stacking context, so z-50 only ever
          competed INSIDE it — and the sessions/telemetry/progress/thoughts
          panels, a later sibling at the same z-20, painted straight over the
          "fullscreen" pulse. The portal lifts the overlay out to the one
          context where z-50 means what it says. */}
      {/* In-tree marker: the ground the pulse itself sits on, which is the
          ground its fullscreen overlay must wear once it leaves the tree. */}
      <span {...probe} />
      {fullscreen &&
        createPortal(
          <div
            {...frameProps}
            className="fixed inset-0 z-50 cursor-pointer bg-surface"
            onClick={handleFsExit}
          >
            <canvas ref={fsCanvasRef} className="block h-full w-full" />
            <PulseTextStack
              subtitle={subtitle}
              subtitleActive={subtitleActive}
              subtitleCursor={subtitleCursor}
              callsText={callsText}
              agentsText={agentsText}
              isActive={isActive ?? false}
              briefIndicator={<BriefDot state={brief} />}
              briefIndicatorPlacement="below-quote"
            />
            <div className="pointer-events-none absolute right-4 top-4 font-mono text-2xs text-tertiary">
              ESC to close
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}

/**
 * Text stack rendered both in the compact sidebar pulse and in the
 * fullscreen overlay. Same layout, same typography — the stack
 * self-positions at 75 % of its offset parent's height and is
 * pointer-events:none so the surrounding canvas can still receive
 * fullscreen-exit clicks.
 */
function PulseTextStack({
  subtitle,
  subtitleActive,
  subtitleCursor,
  callsText,
  agentsText,
  isActive,
  thinker,
  collapsed = false,
  briefIndicator,
  briefIndicatorPlacement = "above-wordmark",
}: {
  subtitle: string;
  subtitleActive: boolean;
  subtitleCursor: boolean;
  callsText: string;
  agentsText: string;
  isActive: boolean;
  thinker?: ReactNode;
  collapsed?: boolean;
  briefIndicator?: ReactNode;
  /**
   * Where the dot sits in the stack. The sidebar tucks it under the orb, above
   * the wordmark, where the gap is the only free space. Fullscreen has room to
   * spare, so it goes at the foot of the stack, under the quote.
   */
  briefIndicatorPlacement?: "above-wordmark" | "below-quote";
}) {
  return (
    <div
      className="pointer-events-none absolute inset-x-0 z-10 flex select-none flex-col items-center"
      style={{
        top: "75%",
        transform: "translateY(-50%)",
        gap: "24px",
      }}
    >
      {/* Block 0 — brief indicator. First in the stack so it sits horizontally
          centered just under the orb, in the gap above the wordmark. It is in
          FLOW rather than absolutely placed because the orb's radius tracks
          activity — an absolute dot would get swallowed whenever the pulse ran
          hot, which is exactly when the brief is playing. The stack wrapper is
          pointer-events:none, so the indicator re-enables it locally. */}
      {briefIndicatorPlacement === "above-wordmark" && briefIndicator}

      {/* Block 1 — OKURO + status subtitle (the live agent activity
          typewriter). OKURO = 32 px display. Subtitle = 14 px.
          Fades out when chat collapses the pulse. */}
      <div
        className="flex flex-col items-center transition-opacity duration-300"
        style={{ opacity: collapsed ? 0 : 1 }}
      >
        <h1
          className="font-mono font-light uppercase text-fg-primary"
          style={{ fontSize: "32px", letterSpacing: "0.6em", paddingLeft: "0.6em" }}
        >
          OKURO
        </h1>
        <p
          className={`mt-2 font-mono text-tertiary ${subtitleActive ? "" : "invisible"}`}
          style={{ fontSize: "14px" }}
          aria-hidden={!subtitleActive}
        >
          {subtitle}
          {subtitleCursor && <span className="animate-pulse">_</span>}
        </p>
      </div>

      {/* Block 2 — stats row. Hierarchy via color + uppercase tracking.
          Slides out the bottom when chat collapses the pulse. */}
      <div
        className="flex justify-center gap-10 font-mono transition-all duration-[400ms] ease-out"
        style={{
          opacity: collapsed ? 0 : 1,
          transform: collapsed ? "translateY(56px)" : "none",
        }}
      >
        <Stat value={callsText} label="CALLS" />
        <Stat value={agentsText} label="AGENTS" />
      </div>

      {/* Block 2.5 — live status thinker (OkuroThinker). Sits between the
          stats and the quote so it reads as part of the "what's happening"
          surface rather than next to the accordion controls below the
          canvas. The wrapper is pointer-events:none, so re-enable it
          locally — the close button needs to receive clicks. */}
      {thinker && (
        <div
          className={`mx-auto w-full px-4 transition-all duration-[400ms] ease-out ${
            collapsed ? "pointer-events-none" : "pointer-events-auto"
          }`}
          style={{
            maxWidth: "85%",
            opacity: collapsed ? 0 : 1,
            transform: collapsed ? "translateY(56px)" : "none",
          }}
        >
          {thinker}
        </div>
      )}

      {/* Block 3 — ambient quote. Capped at 85 % width so lines break
          before hitting the edges. Slides out with the stats on collapse. */}
      <div
        className="mx-auto w-full px-4 transition-all duration-[400ms] ease-out"
        style={{
          maxWidth: "85%",
          opacity: collapsed ? 0 : 1,
          transform: collapsed ? "translateY(56px)" : "none",
        }}
      >
        <QuoteDisplay isActive={isActive} />
      </div>

      {/* Block 4 — brief indicator, fullscreen placement. Same dot, same
          shared audio; only the seat in the stack differs. */}
      {briefIndicatorPlacement === "below-quote" && briefIndicator}
    </div>
  );
}

/**
 * One stat pair: large value + small label, baseline-aligned.
 * Frame-71 pattern: size/weight hierarchy within the same monospace family.
 */
function Stat({ value, label }: { value: string; label: string }) {
  return (
    <span className="inline-flex items-baseline gap-2" style={{ fontSize: "16px" }}>
      <span className="font-medium tabular-nums text-fg">{value}</span>
      <span className="uppercase tracking-[0.2em] text-tertiary">{label}</span>
    </span>
  );
}
