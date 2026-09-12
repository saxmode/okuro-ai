import { useState } from "react";
import { NavLink } from "react-router";
import { Menu, Search, Sparkles } from "lucide-react";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { NAV_TREE, HealthDot } from "./nav-bar";
import { OPEN_COMMAND_PALETTE_EVENT } from "@/components/ui/command-palette";
import { useVisibleNavTree } from "@/lib/nav-visibility";
import { cn } from "@/lib/utils";

interface MobileTopBarProps {
  /** Opens the pulse/panels drawer (the shell sidebar, as a sheet). */
  onOpenPanel: () => void;
  /** Mirror of pulse "is okuro working" state — lights the pulse button. */
  isActive?: boolean;
}

// Mobile-only top bar. Renders below the 768px breakpoint in place of the
// fixed left Sidebar + horizontal NavBar, both of which overflow a phone
// viewport. Two affordances: a hamburger that opens the page-nav drawer,
// and a pulse button that opens the sidebar (pulse + panels + chat) drawer.
export function MobileTopBar({ onOpenPanel, isActive = false }: MobileTopBarProps) {
  const [navOpen, setNavOpen] = useState(false);
  // Same filtered tree the desktop bar and the palette get — one rule, three
  // surfaces (lib/nav-visibility.ts).
  const navTree = useVisibleNavTree(NAV_TREE);

  return (
    <>
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-3">
        <button
          type="button"
          aria-label="Open navigation"
          onClick={() => setNavOpen(true)}
          className="flex h-9 w-9 items-center justify-center rounded text-tertiary transition-colors hover:bg-surface-elevated hover:text-fg"
        >
          <Menu size={20} />
        </button>

        <span className="text-sm uppercase tracking-widest text-tertiary">okuro</span>

        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="Open command palette"
            onClick={() => window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT))}
            className="flex h-9 w-9 items-center justify-center rounded text-tertiary transition-colors hover:bg-surface-elevated hover:text-fg"
          >
            <Search size={18} />
          </button>

          <button
            type="button"
            aria-label="Open pulse panel"
            onClick={onOpenPanel}
            className="relative flex h-9 w-9 items-center justify-center rounded text-tertiary transition-colors hover:bg-surface-elevated hover:text-fg"
          >
            <Sparkles size={18} />
            {isActive && (
              <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-accent" />
            )}
          </button>
        </div>
      </header>

      <Sheet open={navOpen} onOpenChange={setNavOpen}>
        <SheetContent side="left" className="w-[min(300px,85vw)] gap-0 p-0">
          <SheetTitle className="px-5 pb-3 pt-5 text-xs uppercase tracking-widest text-tertiary">
            okuro
          </SheetTitle>
          <nav className="flex flex-1 flex-col overflow-y-auto px-2 pb-4">
            {navTree.map((group) => (
              <div key={group.label} className="mb-3">
                <p className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-widest text-fg-subtle">
                  {group.label}
                </p>
                {group.children.map(({ to, label }) => (
                  <NavLink
                    key={to}
                    to={to}
                    end={to === "/"}
                    onClick={() => setNavOpen(false)}
                    className={({ isActive }) =>
                      cn(
                        "rounded px-3 py-2.5 text-sm font-medium uppercase tracking-wider transition-colors",
                        isActive
                          ? "bg-surface-elevated text-accent"
                          : "text-tertiary hover:bg-surface-elevated hover:text-fg-muted",
                      )
                    }
                  >
                    {label}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>
          <div className="border-t border-border px-5 py-3">
            <HealthDot />
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
