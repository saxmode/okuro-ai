import { useRef, useCallback, useState, type ReactNode } from "react";
import { useChrome } from "@/lib/chrome-context";
import { cn } from "@/lib/utils";

interface SidebarProps {
  /** Content rendered in the top section (PULSE canvas). */
  pulse?: ReactNode;
  /** Content rendered in the bottom scrollable section (panels). */
  panels?: ReactNode;
  /** Full-height chat overlay (behind the pulse orb). */
  chat?: ReactNode;
  /** When true, the chat overlay is shown and the panels slide out. */
  chatOpen?: boolean;
  /** Current width in px. */
  width: number;
  /** Called when user drags the resize handle. */
  onResize: (width: number) => void;
}

const MIN_WIDTH = 260;
const MAX_WIDTH = 600;

export function Sidebar({ pulse, panels, chat, chatOpen = false, width, onResize }: SidebarProps) {
  // Full-screen API: when chrome is hidden the sidebar slides off the left edge
  // (negative margin reclaims the flex space so the page fills it) and fades out.
  const { hidden } = useChrome();
  return (
    <aside
      className={cn(
        "relative flex h-full shrink-0 flex-col bg-surface transition-[margin,opacity] duration-300 ease-in-out",
        hidden && "pointer-events-none opacity-0",
      )}
      style={{ width, marginLeft: hidden ? -width : 0 }}
      aria-hidden={hidden}
    >
      {/* Chat overlay — full sidebar height, BEHIND the pulse orb. Fades in
          when the orb is clicked; the pulse region above it goes transparent
          + click-through so the orb floats over the chat. */}
      {chat && (
        <div
          className={cn(
            "absolute inset-0 z-10 transition-opacity duration-300",
            chatOpen ? "opacity-100" : "pointer-events-none opacity-0",
          )}
        >
          {chat}
        </div>
      )}

      {/* PULSE canvas — fills available space. z-20 keeps the orb above the
          chat; when chat is open it stops intercepting pointer events so the
          chat behind it receives clicks. */}
      <div
        className={cn(
          "relative z-20 flex-1 min-h-0 overflow-hidden",
          chatOpen && "pointer-events-none",
        )}
      >
        {pulse}
      </div>

      {/* Collapsible panels — slide out the bottom when chat opens. */}
      {panels && (
        <div
          className={cn(
            "relative z-20 max-h-[60%] shrink-0 overflow-y-auto border-t border-border transition-all duration-300",
            chatOpen && "pointer-events-none translate-y-full opacity-0",
          )}
        >
          {panels}
        </div>
      )}

      {/* Resize handle */}
      <ResizeHandle
        minWidth={MIN_WIDTH}
        maxWidth={MAX_WIDTH}
        onResize={onResize}
      />
    </aside>
  );
}

function ResizeHandle({
  minWidth,
  maxWidth,
  onResize,
}: {
  minWidth: number;
  maxWidth: number;
  onResize: (width: number) => void;
}) {
  const [dragging, setDragging] = useState(false);
  const startXRef = useRef(0);
  const startWidthRef = useRef(0);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      setDragging(true);
      startXRef.current = e.clientX;
      const sidebar = (e.target as HTMLElement).parentElement;
      startWidthRef.current = sidebar?.offsetWidth ?? 340;

      const onPointerMove = (ev: PointerEvent) => {
        const delta = ev.clientX - startXRef.current;
        const next = Math.max(minWidth, Math.min(maxWidth, startWidthRef.current + delta));
        onResize(next);
      };
      const onPointerUp = () => {
        setDragging(false);
        window.removeEventListener("pointermove", onPointerMove);
        window.removeEventListener("pointerup", onPointerUp);
      };
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
    },
    [minWidth, maxWidth, onResize],
  );

  return (
    <div
      onPointerDown={onPointerDown}
      className={cn(
        "absolute right-0 top-0 z-30 h-full w-1 cursor-col-resize transition-colors",
        dragging ? "bg-accent" : "hover:bg-border-hover",
      )}
    />
  );
}
