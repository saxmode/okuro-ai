import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  motion,
  AnimatePresence,
  LayoutGroup,
  useReducedMotion,
} from "motion/react";
import { ChevronRight, Moon, Sun } from "lucide-react";
import { useChrome } from "@/lib/chrome-context";
import { useVisibleNavTree } from "@/lib/nav-visibility";
import {
  applyThemeMode,
  currentThemeMode,
  getStoredThemeMode,
  type ThemeMode,
} from "@/lib/theme";
import { cn } from "@/lib/utils";

/**
 * Two-level navigation model. Five top groups (L1) act as containers only —
 * they never route, they reveal their children. Clicking a group opens a
 * horizontal accordion: its L2 leaves slide in inline (Framer stagger) and
 * downstream groups shift right (Framer `layout`). One group open at a time.
 *
 * A third level uses the same recipe: give a leaf its own `children` and the
 * accordion nests recursively. No L3 routes exist yet (sub-sections live as
 * tabs inside their pages), so leaves are flat for now.
 */
export interface NavLeaf {
  to: string;
  label: string;
  /** Declares a "new X" entry point. The command palette derives a Create action
   *  that navigates to `${to}?new=1`; the target page honors that intent. One
   *  line here surfaces the create action in Cmd+K — no palette edit needed. */
  create?: { label: string };
}
export interface NavGroup {
  label: string;
  children: NavLeaf[];
}

export const NAV_TREE: NavGroup[] = [
  {
    label: "START",
    children: [
      { to: "/", label: "NOW" },
      { to: "/inbox", label: "INBOX" },
      { to: "/projects", label: "PROJECTS" },
    ],
  },
  {
    label: "KNOW",
    children: [
      { to: "/brain", label: "BRAIN" },
      // Sits in KNOW rather than START: these are mined FROM the sessions
      // BRAIN shows, and they are browsed like the rest of this group. The
      // "you have candidates" nudge is already carried by the bootstrap
      // section and the review reminder, which is where the interrupt belongs.
      { to: "/lessons", label: "LESSONS" },
      { to: "/knowledge", label: "KNOWLEDGE" },
      { to: "/notes", label: "NOTES", create: { label: "New note" } },
      { to: "/cortex", label: "CORTEX" },
      { to: "/repos", label: "REPOS" },
      { to: "/corpora", label: "CORPORA" },
    ],
  },
  {
    label: "WORK",
    children: [
      { to: "/work", label: "TASKS" },
      { to: "/agents", label: "AGENTS" },
      { to: "/flow", label: "FLOW", create: { label: "New flow" } },
      { to: "/workflows", label: "WORKFLOWS", create: { label: "New workflow" } },
      { to: "/bridge", label: "BRIDGE" },
    ],
  },
  {
    label: "DELIVER",
    children: [
      { to: "/prism", label: "PRISM", create: { label: "New prism" } },
      { to: "/slides", label: "SLIDES" },
      { to: "/studio", label: "STUDIO" },
      { to: "/media", label: "PODCAST" },
      { to: "/assets?view=media", label: "MEDIA" },
      { to: "/assets", label: "ASSETS" },
      { to: "/resonance", label: "RESONANCE" },
      { to: "/people", label: "PEOPLE" },
    ],
  },
  {
    label: "SYSTEM",
    children: [
      { to: "/services", label: "SERVICES" },
      { to: "/health", label: "HEALTH" },
      { to: "/scheduled", label: "SCHEDULED" },
      { to: "/models", label: "MODELS" },
      { to: "/stack", label: "STACK" },
      { to: "/design-engine", label: "ENGINE" },
      { to: "/ds-engine-codex", label: "DS-ENGINE-CODEX" },
      { to: "/settings", label: "SETTINGS" },
      { to: "/about", label: "ABOUT" },
    ],
  },
];

/** A leaf is active on exact "/" match, or when the path enters its subtree. */
function isLeafActive(to: string, pathname: string): boolean {
  if (to === "/") return pathname === "/";
  return pathname === to || pathname.startsWith(`${to}/`);
}

export function HealthDot() {
  const { data } = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const res = await fetch("/api/health");
      if (!res.ok) throw new Error("unhealthy");
      return (await res.json()) as { status: string };
    },
    refetchInterval: 10_000,
    retry: false,
  });

  const healthy = data?.status === "healthy";

  return (
    <div className="flex items-center gap-1.5">
      <span
        className={cn(
          "inline-block h-2 w-2 rounded-full",
          healthy ? "bg-success" : "bg-error",
        )}
      />
      <span className="text-xs uppercase tracking-wider text-tertiary">
        {healthy ? "online" : "offline"}
      </span>
    </div>
  );
}

/** Dark/light toggle. Applies + persists the mode globally (remembered across
 *  brands); the engine emits both grounds and switches on the
 *  `[data-appearance=…]` block this sets. */
export function ThemeModeToggle() {
  const [mode, setMode] = useState<ThemeMode>(() => getStoredThemeMode() ?? "dark");

  // After mount the tokens are loaded, so reflect the true active mode (stored
  // choice, else inferred from the brand's native background).
  useEffect(() => {
    setMode(currentThemeMode());
  }, []);

  const toggle = () => {
    const next: ThemeMode = mode === "dark" ? "light" : "dark";
    applyThemeMode(next);
    setMode(next);
  };

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={`Switch to ${mode === "dark" ? "light" : "dark"} mode`}
      title="Toggle dark / light"
      className="flex items-center text-fg opacity-40 transition-opacity duration-200 hover:opacity-100 focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
    >
      {mode === "dark" ? <Moon size={14} /> : <Sun size={14} />}
    </button>
  );
}

export function NavBar() {
  // Full-screen API: when chrome is hidden the nav slides up off the top edge
  // (negative margin reclaims the row so the page fills it) and fades out.
  const { hidden } = useChrome();
  const location = useLocation();
  const reduced = useReducedMotion();
  const trackRef = useRef<HTMLDivElement>(null);

  // Leaves whose feature is switched off never reach the bar. Empty until the
  // features fetch settles, so a withheld entry cannot flash into view — see
  // the loading-behaviour note in lib/features-context.tsx.
  const navTree = useVisibleNavTree(NAV_TREE);

  // Which group owns the current route — used to auto-open on navigation so
  // the bar always shows where you are.
  const activeGroup = useMemo(
    () =>
      navTree.find((g) =>
        g.children.some((c) => isLeafActive(c.to, location.pathname)),
      )?.label ?? null,
    [navTree, location.pathname],
  );

  const [openGroup, setOpenGroup] = useState<string | null>(activeGroup);

  // Follow the route: land on a page → its group opens. Manual clicks still
  // win until the next navigation.
  useEffect(() => {
    if (activeGroup) setOpenGroup(activeGroup);
  }, [activeGroup]);

  // A wide group (e.g. DELIVER's 7 leaves) can't fit a narrow viewport
  // alongside the other four labels. Rather than clip, pull the opened group
  // to the track's left edge so its children are always visible; the other
  // groups stay one swipe away. Runs after the panel starts expanding.
  useEffect(() => {
    if (!openGroup || !trackRef.current) return;
    const el = trackRef.current.querySelector<HTMLElement>(
      `[data-nav-group="${openGroup}"]`,
    );
    el?.scrollIntoView({
      behavior: reduced ? "auto" : "smooth",
      inline: "start",
      block: "nearest",
    });
  }, [openGroup, reduced]);

  // Fade-only under reduced motion (no translate/width sweep → vestibular
  // safe) but never an instant "bang".
  const panelTransition = {
    duration: reduced ? 0.18 : 0.32,
    ease: [0.22, 1, 0.36, 1] as const,
  };

  return (
    <nav
      className={cn(
        "flex h-10 shrink-0 items-center bg-surface transition-[margin,opacity] duration-300 ease-in-out",
        hidden && "pointer-events-none opacity-0",
      )}
      style={{ marginTop: hidden ? -40 : 0 }}
      aria-hidden={hidden}
    >
      {/* Mirror page-shell's horizontal box EXACTLY — same max-width, auto
          side margins, AND the same `var(--spacing-xl)` inline gutter (40px
          in the built app where the design tokens are injected; empty/0 in a
          bare dev server). This is what makes the first label sit directly
          above the page's h1 at every width. Must stay in sync with the
          .page-shell rule in globals.css. */}
      <div
        className="mx-auto flex h-full w-full items-center justify-between"
        style={{
          maxWidth: "var(--max-width, 1280px)",
          paddingInline: "var(--spacing-xl)",
        }}
      >
        {/* Horizontal accordion track. Overflows sideways (scrollbar hidden)
            rather than wrapping, keeping the bar a single clean row. */}
        <LayoutGroup>
          <div
            ref={trackRef}
            className="flex min-w-0 flex-1 items-center overflow-x-auto scroll-smooth [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
          {navTree.map((group, idx) => {
            const isOpen = openGroup === group.label;
            return (
              <div
                key={group.label}
                data-nav-group={group.label}
                className="flex shrink-0 items-center"
              >
                <motion.button
                  type="button"
                  layout={reduced ? false : "position"}
                  onClick={() =>
                    setOpenGroup((prev) =>
                      prev === group.label ? null : group.label,
                    )
                  }
                  aria-expanded={isOpen}
                  className={cn(
                    "flex shrink-0 items-center gap-0.5 whitespace-nowrap px-2 text-xs font-semibold uppercase tracking-wide transition-[opacity,color] duration-200 focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
                    // First label's glyph aligns flush with page content.
                    idx === 0 && "pl-0",
                    // Occupancy hierarchy: the open group is the highlighted
                    // "selected" main point (100% + accent); the others recede
                    // to 20% and lift to full on hover.
                    isOpen
                      ? "text-accent opacity-100"
                      : "text-fg opacity-20 hover:opacity-100",
                  )}
                >
                  {group.label}
                  {/* Horizontal affordance: points right (opens rightward)
                      when closed, flips to point left (collapse) when open —
                      never a downward dropdown caret. */}
                  <ChevronRight
                    size={11}
                    className={cn(
                      "transition-transform duration-200",
                      isOpen && "rotate-180",
                    )}
                  />
                </motion.button>

                <AnimatePresence initial={false}>
                  {isOpen && (
                    <motion.div
                      key={`${group.label}-panel`}
                      initial={{ width: 0, opacity: 0 }}
                      animate={{ width: "auto", opacity: 1 }}
                      exit={{ width: 0, opacity: 0 }}
                      transition={panelTransition}
                      className="flex items-center overflow-hidden"
                    >
                      <span className="mx-1.5 h-3.5 w-px shrink-0 bg-border" />
                      <motion.div
                        className="flex items-center"
                        initial="closed"
                        animate="open"
                        variants={{
                          open: {
                            transition: {
                              delayChildren: reduced ? 0 : 0.06,
                              staggerChildren: reduced ? 0 : 0.04,
                            },
                          },
                        }}
                      >
                        {group.children.map((leaf) => (
                          <motion.div
                            key={leaf.to}
                            className="shrink-0"
                            variants={{
                              closed: { opacity: 0, x: reduced ? 0 : -8 },
                              open: { opacity: 1, x: 0 },
                            }}
                            transition={{ duration: reduced ? 0.15 : 0.25 }}
                          >
                            <NavLink
                              to={leaf.to}
                              end={leaf.to === "/"}
                              className={({ isActive }) =>
                                cn(
                                  "block whitespace-nowrap px-2 text-xs font-medium uppercase tracking-wide transition-[opacity,color] duration-200",
                                  // Selected leaf = 100% + highlight; the
                                  // unselected siblings sit at 40%, full on
                                  // hover.
                                  isActive
                                    ? "text-accent opacity-100"
                                    : "text-fg opacity-40 hover:opacity-100",
                                )
                              }
                            >
                              {leaf.label}
                            </NavLink>
                          </motion.div>
                        ))}
                      </motion.div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            );
          })}
          </div>
        </LayoutGroup>
        <div className="flex shrink-0 items-center gap-3 pl-4">
          <ThemeModeToggle />
          <HealthDot />
        </div>
      </div>
    </nav>
  );
}
