import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useChrome } from "@/lib/chrome-context";
import { cn } from "@/lib/utils";

// Chrome-hosting window (pywebview) injects window.pywebview once the native
// bridge is wired up. In a normal browser it stays undefined, which is how
// we decide whether to render this bar at all — in the browser the OS tab
// bar already has URL + controls, so we don't want to duplicate.
interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface PywebviewBridge {
  api: {
    minimize: () => Promise<void>;
    toggle_maximize: () => Promise<void>;
    close: () => Promise<void>;
    get_bounds: () => Promise<Bounds | null>;
    resize_to: (w: number, h: number, x?: number | null, y?: number | null) => Promise<void>;
    // Routes a URL to the user's default system browser. pywebview drops
    // window.open(_blank) silently — see openExternal in lib/api.ts.
    open_external: (url: string) => Promise<boolean>;
  };
}

declare global {
  interface Window {
    pywebview?: PywebviewBridge;
  }
}

export function usePywebviewReady(): boolean {
  const [ready, setReady] = useState<boolean>(() => Boolean(window.pywebview));
  useEffect(() => {
    if (ready) return;
    // pywebview fires a `pywebviewready` event once the bridge is exposed.
    // On some backends the bridge exists at DOMContentLoaded, on others it
    // takes a tick — poll up to 2 s as a belt-and-braces fallback.
    const onReady = () => setReady(true);
    window.addEventListener("pywebviewready", onReady);
    let attempts = 0;
    const id = window.setInterval(() => {
      if (window.pywebview) {
        setReady(true);
        window.clearInterval(id);
      } else if (++attempts > 20) {
        window.clearInterval(id);
      }
    }, 100);
    return () => {
      window.removeEventListener("pywebviewready", onReady);
      window.clearInterval(id);
    };
  }, [ready]);
  return ready;
}

function useReloadShortcut(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const handler = (e: KeyboardEvent) => {
      const reload =
        e.key === "F5" ||
        ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "r");
      if (reload) {
        e.preventDefault();
        window.location.reload();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}

function IconRefresh() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden>
      <path
        d="M10 6a4 4 0 1 1-1.2-2.85"
        fill="none"
        stroke="currentColor"
        strokeWidth="1"
        strokeLinecap="round"
      />
      <path d="M10 1.5V4H7.5" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// Hard reset for pywebview, where there is no DevTools to evict a stale
// (caching) service worker. Unregister every SW, delete every Cache Storage
// entry, then navigate with a cache-busting param so even an HTTP-cached
// index.html is bypassed and the freshest bundle loads from the server.
async function hardResetAndReload() {
  try {
    if ("serviceWorker" in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((r) => r.unregister()));
    }
    if (window.caches) {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    }
  } catch {
    /* best-effort — fall through to the reload regardless */
  }
  const u = new URL(window.location.href);
  u.searchParams.set("_r", Date.now().toString());
  window.location.replace(u.toString());
}

function IconMin() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <line x1="1" y1="5" x2="9" y2="5" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

function IconMax() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <rect x="1" y="1" width="8" height="8" fill="none" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

function IconClose() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <line x1="1" y1="1" x2="9" y2="9" stroke="currentColor" strokeWidth="1" />
      <line x1="9" y1="1" x2="1" y2="9" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

type ButtonKind = "reload" | "minimize" | "maximize" | "close";

function ChromeButton({
  kind,
  onClick,
  title,
  children,
}: {
  kind: ButtonKind;
  onClick: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={title ?? kind}
      title={title ?? kind}
      className={cn(
        "flex h-8 w-10 items-center justify-center text-tertiary transition-colors",
        kind === "close"
          ? "hover:bg-error/20 hover:text-error"
          : "hover:bg-surface-elevated hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}

// Edge direction encoded as a bitfield so resize math is a single pass.
type Edge =
  | "n"
  | "s"
  | "e"
  | "w"
  | "ne"
  | "nw"
  | "se"
  | "sw";

const EDGE_CURSOR: Record<Edge, string> = {
  n: "cursor-n-resize",
  s: "cursor-s-resize",
  e: "cursor-e-resize",
  w: "cursor-w-resize",
  ne: "cursor-ne-resize",
  nw: "cursor-nw-resize",
  se: "cursor-se-resize",
  sw: "cursor-sw-resize",
};

function EdgeHandle({ edge, className, onStart }: { edge: Edge; className: string; onStart: (edge: Edge, e: React.MouseEvent) => void }) {
  return (
    <div
      aria-hidden
      onMouseDown={(e) => onStart(edge, e)}
      className={cn(
        "fixed z-[9999] select-none",
        EDGE_CURSOR[edge],
        className,
      )}
    />
  );
}

// Frameless GTK windows lose WM-assisted edge-resize on GNOME Mutter. We
// paint eight invisible strips along the viewport edges, listen for pointer
// drags, and drive window.resize / window.move through the pywebview bridge.
// Only renders inside pywebview — in a normal browser the handles would be
// meaningless (and would fight the browser's own resize).
function WindowResizeEdges() {
  const dragRef = useRef<null | {
    edge: Edge;
    start: Bounds;
    startScreenX: number;
    startScreenY: number;
  }>(null);

  const onStart = (edge: Edge, e: React.MouseEvent) => {
    if (!window.pywebview) return;
    // preventDefault + stopPropagation must run synchronously — React pools
    // synthetic events and an awaited version would act on a stale object.
    // Screen coords are captured from the event now; bounds are filled in
    // asynchronously below and onMove no-ops until they land (~1 RTT to
    // pywebview js_api, typically sub-millisecond).
    e.preventDefault();
    e.stopPropagation();
    const startScreenX = e.screenX;
    const startScreenY = e.screenY;
    console.log("[pulse-resize] start", { edge, startScreenX, startScreenY });
    window.pywebview.api
      .get_bounds()
      .then((bounds) => {
        console.log("[pulse-resize] bounds", bounds);
        if (!bounds) return;
        dragRef.current = { edge, start: bounds, startScreenX, startScreenY };
      })
      .catch((err) => {
        console.warn("[pulse-resize] get_bounds failed", err);
      });
  };

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const dx = e.screenX - drag.startScreenX;
      const dy = e.screenY - drag.startScreenY;

      const { edge, start } = drag;
      let w = start.width;
      let h = start.height;
      let x: number | null = null;
      let y: number | null = null;

      if (edge.includes("e")) w = start.width + dx;
      if (edge.includes("w")) {
        w = start.width - dx;
        x = start.x + dx;
      }
      if (edge.includes("s")) h = start.height + dy;
      if (edge.includes("n")) {
        h = start.height - dy;
        y = start.y + dy;
      }

      window.pywebview?.api
        .resize_to(w, h, x, y)
        .catch((err) => console.warn("[pulse-resize] resize_to failed", err));
    };

    const onUp = () => {
      dragRef.current = null;
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  // 8 px edge strips; 16 px corner squares. Previous 4 px / 8 px zones
  // were the right size for a wireframe but impossible to hit — the user
  // would sweep the cursor past them with no signal that resize existed.
  // Electron/Tauri/browser scrollbars all use 8-16 px hit zones. We match.
  //
  // Top edge + NW/NE corners start at top-8 (32 px — same as the chrome
  // row) so they don't overlap the min/max/close buttons and steal their
  // clicks. e/w strips start at top-10 (40 px) for the same reason.
  return (
    <>
      <EdgeHandle edge="n" className="top-8 left-4 right-4 h-2" onStart={onStart} />
      <EdgeHandle edge="s" className="bottom-0 left-4 right-4 h-2" onStart={onStart} />
      <EdgeHandle edge="e" className="top-10 bottom-4 right-0 w-2" onStart={onStart} />
      <EdgeHandle edge="w" className="top-10 bottom-4 left-0 w-2" onStart={onStart} />
      <EdgeHandle edge="nw" className="top-8 left-0 w-4 h-4" onStart={onStart} />
      <EdgeHandle edge="ne" className="top-8 right-0 w-4 h-4" onStart={onStart} />
      <EdgeHandle edge="sw" className="bottom-0 left-0 w-4 h-4" onStart={onStart} />
      <EdgeHandle edge="se" className="bottom-0 right-0 w-4 h-4" onStart={onStart} />
    </>
  );
}

/**
 * Track whether the window is effectively maximized (by button click OR by
 * WM action — Super+Up, double-click drag region, display manager shortcut).
 *
 * The resize handles below intercept clicks in their position:fixed strips,
 * and in a maximized window there's nothing to resize — so the strips just
 * eat clicks from NavBar tabs / the pulse fullscreen toggle / whatever else
 * sits at the edges. Hide them when maximized.
 *
 * Detection is heuristic: if window.inner{Width,Height} matches screen.avail
 * dimensions within 2 px, we're maximized. Polling on window resize event is
 * enough — WM-initiated maximize fires a resize; button-initiated maximize
 * fires it too once pywebview processes the GTK Window.maximize() call.
 */
function useMaximized(): boolean {
  const [maximized, setMaximized] = useState<boolean>(() => _detect());
  useEffect(() => {
    const onResize = () => setMaximized(_detect());
    window.addEventListener("resize", onResize);
    // Also re-check on focus — some WMs fire resize only after the window
    // regains focus following a maximize.
    window.addEventListener("focus", onResize);
    // Poll lightly for 2 s after mount to catch startup-time maximize
    // (pywebview can spawn a maximized window before resize events bind).
    const id = window.setInterval(onResize, 250);
    const stop = window.setTimeout(() => window.clearInterval(id), 2000);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("focus", onResize);
      window.clearInterval(id);
      window.clearTimeout(stop);
    };
  }, []);
  return maximized;
}

function _detect(): boolean {
  if (typeof window === "undefined") return false;
  // Tolerance of 2 px to absorb HiDPI rounding + WM titlebar quirks.
  const widthMax = Math.abs(window.innerWidth - window.screen.availWidth) <= 2;
  const heightMax =
    Math.abs(window.innerHeight - window.screen.availHeight) <= 2;
  // Also consider true browser fullscreen API — covers F11-style fullscreen.
  const docFs = Boolean(document.fullscreenElement);
  return (widthMax && heightMax) || docFs;
}

/**
 * ChromeChevron — persistent directional toggle sitting to the right of the
 * `okuro` wordmark. Points left (◀) while the chrome (tab bar + pulse panel) is
 * visible → click to hide; points right (▶) once hidden → click to restore.
 * Replaces the old bottom-left restore button in the pywebview shell.
 */
function ChromeChevron() {
  const { hidden, toggle } = useChrome();
  const Icon = hidden ? ChevronRight : ChevronLeft;
  return (
    <button
      onClick={toggle}
      aria-label={hidden ? "Show tab bar and side panel" : "Hide tab bar and side panel"}
      title={hidden ? "Show tabs + side panel" : "Hide tabs + side panel"}
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-tertiary transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
    </button>
  );
}

export function WindowChrome() {
  const ready = usePywebviewReady();
  useReloadShortcut(ready);
  const maximized = useMaximized();

  if (!ready) return null;

  const bridge = window.pywebview!;
  return (
    <>
      <div className="flex h-8 shrink-0 select-none items-center bg-surface">
        <div className="pywebview-drag-region px-3 text-xs uppercase tracking-widest text-tertiary">
          okuro
        </div>
        <ChromeChevron />
        <div className="pywebview-drag-region h-full flex-1" />
        <ChromeButton
          kind="reload"
          title="Reload & clear cache"
          onClick={hardResetAndReload}
        >
          <IconRefresh />
        </ChromeButton>
        <ChromeButton kind="minimize" onClick={() => bridge.api.minimize()}>
          <IconMin />
        </ChromeButton>
        <ChromeButton kind="maximize" onClick={() => bridge.api.toggle_maximize()}>
          <IconMax />
        </ChromeButton>
        <ChromeButton kind="close" onClick={() => bridge.api.close()}>
          <IconClose />
        </ChromeButton>
      </div>
      {/* Resize edges render ONLY when the window can actually be resized
          by dragging — i.e. NOT maximized and NOT in full document
          fullscreen. In a maximized window they used to intercept clicks
          on NavBar tabs (3rd onward) and the pulse fullscreen toggle at
          y≈30-40 because of their z-[9999] fixed position. */}
      {!maximized && <WindowResizeEdges />}
    </>
  );
}
