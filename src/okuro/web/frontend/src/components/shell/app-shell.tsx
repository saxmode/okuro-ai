import { useState, useEffect } from "react";
import { Outlet } from "react-router";
import { FeedbackButton } from "@/components/review/feedback-button";
import { NavBar } from "./nav-bar";
import { Sidebar } from "./sidebar";
import { WindowChrome, usePywebviewReady } from "./window-chrome";
import { OkuroThinker } from "./okuro-thinker";
import { PulseCanvas } from "@/components/pulse/pulse-canvas";
import { ChatCapabilities } from "./chat-capabilities";
import { SidebarPanels } from "./sidebar-panels";
import { MobileTopBar } from "./mobile-top-bar";
import { usePulseData } from "@/hooks/use-activity-stream";
import { useIsMobile } from "@/hooks/use-is-mobile";
import { CommandPalette } from "@/components/ui/command-palette";
import { HandoverHost } from "@/components/handover/handover-host";
import { CreateDialog } from "@/components/task/create-dialog";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { ChromeProvider, useChrome } from "@/lib/chrome-context";
import { PanelLeftOpen } from "lucide-react";

/**
 * ChromeRestoreButton — top-left affordance shown ONLY while the app chrome
 * (NavBar tabs + Sidebar pulse panel) is hidden via the minimize control in
 * PulseCanvas. Inverse of that hide: brings the chrome back. Lives inside
 * ChromeProvider (desktop tree) so it can read `hidden`. Persistent (not
 * hover-reveal) because, with the panels hidden, there is nothing to hover.
 */
function ChromeRestoreButton() {
  const { hidden, show } = useChrome();
  // In the pywebview shell the directional chevron next to the `okuro` wordmark
  // (WindowChrome) owns restore, so this bottom-left fallback is browser-only.
  const pywebview = usePywebviewReady();
  if (!hidden || pywebview) return null;
  return (
    <button
      onClick={show}
      aria-label="Show tab bar and side panel"
      title="Restore — show tabs + side panel"
      className="absolute left-2 top-2 z-40 flex h-6 w-6 items-center justify-center rounded border border-border bg-surface/80 text-tertiary backdrop-blur transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
    >
      <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
    </button>
  );
}

const SIDEBAR_WIDTH_KEY = "okuro-sidebar-width";
const DEFAULT_WIDTH = 340;

function loadWidth(): number {
  try {
    const stored = localStorage.getItem(SIDEBAR_WIDTH_KEY);
    if (stored) return Number(stored);
  } catch {
    /* ignore */
  }
  return DEFAULT_WIDTH;
}

export function AppShell() {
  const [sidebarWidth, setSidebarWidth] = useState(loadWidth);
  const [createOpen, setCreateOpen] = useState(false);
  // Mobile-only: the shell sidebar (pulse + panels) lives in a sheet.
  const [panelOpen, setPanelOpen] = useState(false);
  const isMobile = useIsMobile();
  const { activity, activityStream, services, activeRoles, liveAgents, isActive } =
    usePulseData();

  // Satellite names around the blob = union of live agents (presence layer)
  // + orchestrator active roles (task-scoped). Dedup by label so an agent
  // running as an orchestrator subagent shows once, not twice. This is what
  // makes the sidebar show "claude-code · frontend-engineer · qa-engineer"
  // around the blob instead of only the orchestrator roles.
  const satellites = Array.from(
    new Set<string>([
      ...liveAgents.map((a) => a.provider),
      ...activeRoles,
    ]),
  );

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth));
    } catch {
      /* ignore */
    }
  }, [sidebarWidth]);

  // Pulse orb, assembled once and reused by both the desktop Sidebar and the
  // mobile sheet so behaviour stays identical across layouts.
  //
  // The pulse chat is unmounted for now: its overlay sat behind the orb and the
  // two traded pointer-events, which left the brief's play/stop dot unclickable
  // whenever chat was open. PulseChat/Sidebar still accept `chat` + `chatOpen`,
  // so re-enabling is a matter of passing them again once that layering is
  // reworked.
  const pulseCanvas = (
    <PulseCanvas
      activity={activity}
      activityStream={activityStream}
      services={services}
      activeRoles={satellites}
      isActive={isActive}
      thinker={<OkuroThinker />}
    />
  );

  // Chrome-less embed mode (?embed=1): render only the routed page, no nav /
  // sidebar / pulse. Used to embed a live okuro·flow chart inside a slide.
  // Cmd+K + snapshot hand-over still mount so the view is reachable everywhere;
  // #main-content gives captureSnapshot a target. (Palette navigation would move
  // the embed itself — a deliberate user action, not automatic.)
  if (typeof window !== "undefined" && new URLSearchParams(window.location.search).get("embed") === "1") {
    return (
      <div id="main-content" className="h-screen w-full overflow-hidden bg-surface">
        <Outlet />
        <CommandPalette onCreateTask={() => setCreateOpen(true)} />
        <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
        <HandoverHost />
      </div>
    );
  }

  // Mobile (<768px): the fixed left Sidebar and the 19-link horizontal NavBar
  // both overflow a phone viewport, so swap them for a top bar + drawers and
  // give the page full width. Desktop / pywebview never hit this branch.
  if (isMobile) {
    return (
      <div className="flex h-screen w-full flex-col overflow-hidden bg-surface">
        <a href="#main-content" className="skip-link">
          Skip to content
        </a>
        <ChatCapabilities />
        <MobileTopBar onOpenPanel={() => setPanelOpen(true)} isActive={isActive} />
        <main
          id="main-content"
          className="flex-1 overflow-y-auto [scrollbar-gutter:stable]"
          tabIndex={-1}
        >
          <Outlet />
        </main>

        <Sheet open={panelOpen} onOpenChange={setPanelOpen}>
          <SheetContent side="left" className="w-[min(380px,90vw)] p-0" showCloseButton={false}>
            <SheetTitle className="sr-only">Pulse panel</SheetTitle>
            <div className="relative flex h-full flex-col bg-surface">
              <div className="relative flex-1 min-h-0 overflow-hidden">
                {pulseCanvas}
              </div>
              <div className="relative max-h-[55%] shrink-0 overflow-y-auto border-t border-border">
                <SidebarPanels />
              </div>
            </div>
          </SheetContent>
        </Sheet>

        <CommandPalette onCreateTask={() => setCreateOpen(true)} />
        <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
        <HandoverHost />
      </div>
    );
  }

  return (
    <ChromeProvider>
      {/* `w-full`, NEVER `w-screen`. `html` carries `scrollbar-gutter: stable`,
          which reserves ~6px that `100vw` does not know about — so a `w-screen`
          shell is 6px wider than the body on EVERY page, the document gains a
          horizontal scroll of exactly that, and one focus call scrolls it. The
          measured symptom on /design-engine was the page root sitting at
          x = -6px with 6px of chrome off-screen left. */}
      <div className="flex h-screen w-full flex-col overflow-hidden bg-surface">
        <a href="#main-content" className="skip-link">
          Skip to content
        </a>
        <WindowChrome />
        <ChatCapabilities />
        <div className="flex flex-1 min-h-0">
          <Sidebar
            width={sidebarWidth}
            onResize={setSidebarWidth}
            pulse={pulseCanvas}
            panels={<SidebarPanels />}
          />
          <div className="relative flex flex-1 flex-col min-w-0">
            <ChromeRestoreButton />
            <NavBar />
            <main
              id="main-content"
              // scrollbar-gutter:stable reserves the scrollbar's horizontal
              // space even when content fits, so pages don't reflow sideways
              // the moment a list grows past the fold. Applied here (not on
              // html) because this is the element that actually scrolls.
              className="flex-1 overflow-y-auto [scrollbar-gutter:stable]"
              tabIndex={-1}
            >
              <Outlet />
            </main>
          </div>
        </div>
        <CommandPalette onCreateTask={() => setCreateOpen(true)} />
        <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
        <HandoverHost />
        {/* Fixed lower-right so it is reachable from every page. Mounted here
            rather than per-page precisely so it cannot be missing on the page
            someone wants to complain about. */}
        <FeedbackButton />
      </div>
    </ChromeProvider>
  );
}
