import { useCallback, useEffect, useState } from "react";
import { Command } from "cmdk";
import * as Dialog from "@radix-ui/react-dialog";
import { useNavigate } from "react-router";
import { api } from "@/lib/api";
import { useFrameStamp } from "@/lib/ground-portal";
import { toast } from "@/components/ui/toast";
import { getHandoverIR, openHandover } from "@/lib/handover-context";
import { captureSnapshot } from "@/lib/capture-snapshot";
import { NAV_TREE } from "@/components/shell/nav-bar";
import {
  Camera,
  CornerDownRight,
  FilePlus2,
  Lightbulb,
  Plus,
  Share2,
} from "lucide-react";

// Single source of truth for entry points: the palette derives its "Create" and
// "Go to" lists from NAV_TREE, so a new feature added to the sidebar nav appears
// in Cmd+K for free (add `create: {label}` to its leaf for a "New X" action too).
const NAV_LEAVES = NAV_TREE.flatMap((g) => g.children);
const CREATE_LEAVES = NAV_LEAVES.filter((l) => l.create);
const titleCase = (s: string) => s.charAt(0) + s.slice(1).toLowerCase();
const createRoute = (to: string) => `${to}${to.includes("?") ? "&" : "?"}new=1`;

/** Fire this window event to open the palette without the Cmd/Ctrl+K shortcut
 *  (e.g. a mobile on-screen button). */
export const OPEN_COMMAND_PALETTE_EVENT = "okuro:open-command-palette";

interface CommandPaletteProps {
  onCreateTask: () => void;
  /**
   * Where the dialog portals. Defaults to the app's own `<body>`.
   *
   * THE SAME ESCAPE HATCH THE OTHER SEVEN PORTAL-CARRYING PRIMITIVES ALREADY
   * TAKE (`select`, `dialog`, `sheet`, `dropdown-menu`, `context-menu`,
   * `tooltip`, `detail-modal`). This one lacked it, which meant the palette was
   * the single component in the vendored set that could not be shown inside the
   * design-system showcase's frame -- it would have opened over the app, one
   * document up, wearing whatever the app wears rather than the ground it was
   * summoned from. cmdk's `Command.Dialog` forwards `container` to its Radix
   * portal, so this is a pass-through and not a new mechanism.
   */
  container?: HTMLElement | null;
  /**
   * Controlled open state. Omit for today's behaviour: the palette owns its own
   * state and opens on Cmd/Ctrl+K or the window event.
   *
   * WHY IT EXISTS. `app-shell.tsx` mounts one palette for the whole app, and its
   * listeners are on `window`. A SECOND instance -- which a showcase is -- would
   * answer the same Cmd+K, so two palettes would open at once. Passing `open`
   * makes an instance controlled AND silences its global listeners, so the
   * app's singleton stays the only thing the shortcut reaches.
   */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

type Mode = "root" | "capture";

/**
 * Global Cmd/Ctrl+K palette. Keyboard-first navigation and quick actions.
 *
 * Actions:
 *   - navigate to any major route
 *   - capture a thought (two-step: pick action, type content)
 *   - create new task (opens CreateDialog)
 */
export function CommandPalette({
  onCreateTask,
  container,
  open: controlledOpen,
  onOpenChange,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const controlled = controlledOpen !== undefined;
  const open = controlled ? controlledOpen : uncontrolledOpen;
  const setOpen = useCallback(
    (next: boolean | ((previous: boolean) => boolean)) => {
      const value = typeof next === "function" ? next(open) : next;
      if (!controlled) setUncontrolledOpen(value);
      onOpenChange?.(value);
    },
    [controlled, onOpenChange, open],
  );
  const [mode, setMode] = useState<Mode>("root");
  const [thoughtText, setThoughtText] = useState("");
  const [capturing, setCapturing] = useState(false);

  useEffect(() => {
    // A CONTROLLED INSTANCE TAKES NO GLOBAL LISTENERS. `app-shell` mounts the
    // one palette the shortcut belongs to; a second instance answering the same
    // window event would open two at once.
    if (controlled) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
      if (e.key === "Escape" && mode === "capture") {
        e.preventDefault();
        setMode("root");
        setThoughtText("");
      }
    };
    // External trigger — surfaces without a hardware keyboard (mobile top-bar
    // button) or where the shortcut is unavailable.
    const onExternalOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, onExternalOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, onExternalOpen);
    };
  }, [mode, controlled, setOpen]);

  // Reset mode when palette closes
  useEffect(() => {
    if (!open) {
      setMode("root");
      setThoughtText("");
    }
  }, [open]);

  const run = (fn: () => void) => {
    fn();
    setOpen(false);
  };

  const captureThought = async () => {
    if (!thoughtText.trim() || capturing) return;
    setCapturing(true);
    try {
      await api<{ ok: boolean; id: string }>("/api/dashboard/thought", {
        method: "POST",
        body: JSON.stringify({ content: thoughtText.trim() }),
      });
      toast.success("Thought captured", { description: thoughtText.trim().slice(0, 80) });
      setOpen(false);
    } catch (e) {
      toast.error("Capture failed", {
        description: e instanceof Error ? e.message : "unknown error",
      });
    } finally {
      setCapturing(false);
    }
  };

  const { probe, frameProps } = useFrameStamp();
  return (
    <>
      {/* In-tree marker: cmdk's Command.Dialog wraps a Radix Dialog + Portal, so
          the ground rides on the dialog element it spreads onto its own
          portalled root. Same mechanism, one less hop. `container` is cmdk's own
          pass-through to that portal, which is how the palette can be summoned
          inside the design-system showcase's frame rather than over the app. */}
      <span {...probe} />
      <Command.Dialog
        {...frameProps}
        container={container ?? undefined}
        open={open}
        onOpenChange={setOpen}
        label="Command palette"
        className="fixed inset-0 z-50 flex items-start justify-center bg-scrim pt-[15vh]"
        /* The ground stamp above paints `background-color`, and this element IS
           the scrim — so without the inline value the palette's backdrop is an
           opaque sheet of the ground instead of a veil over the page. Same
           defect and same fix as `DialogOverlay`, where it is measured. */
        style={{ backgroundColor: "var(--color-background-scrim)" }}
      >
        <Dialog.Title className="sr-only">Command palette</Dialog.Title>
        <Dialog.Description className="sr-only">
          Type to filter actions, or press Escape to close.
        </Dialog.Description>
        <div className="w-full max-w-lg rounded-md border border-border bg-surface-elevated shadow-lg">
          {mode === "capture" ? (
            <>
              <div className="border-b border-border px-3 py-2 text-2xs uppercase tracking-wider text-tertiary">
                Capture thought · Enter to save · Esc to back
              </div>
              <textarea
                autoFocus
                value={thoughtText}
                onChange={(e) => setThoughtText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !e.shiftKey)) {
                    e.preventDefault();
                    captureThought();
                  }
                }}
                placeholder="What's on your mind?"
                rows={4}
                className="w-full resize-none bg-transparent px-3 py-3 text-sm text-fg placeholder:text-tertiary focus:outline-none"
              />
              <div className="flex items-center justify-between border-t border-border px-3 py-2 text-3xs text-tertiary">
                <span>
                  <kbd className="rounded bg-surface px-1">Enter</kbd> save ·{" "}
                  <kbd className="rounded bg-surface px-1">Esc</kbd> back
                </span>
                <span className={capturing ? "text-accent" : ""}>
                  {capturing ? "Saving…" : `${thoughtText.length} chars`}
                </span>
              </div>
            </>
          ) : (
            <Command className="font-mono" loop>
              <Command.Input
                placeholder="Type a command or search..."
                className="w-full border-b border-border bg-transparent px-3 py-3 text-sm text-fg placeholder:text-tertiary focus:outline-none"
              />
              <Command.List className="max-h-80 overflow-auto p-1">
                <Command.Empty className="px-3 py-6 text-center text-xs text-tertiary">
                  No matching actions
                </Command.Empty>

                <Command.Group heading="Create" className="text-3xs uppercase tracking-wider text-tertiary">
                  <CmdItem
                    icon={<Plus className="h-3.5 w-3.5" />}
                    label="Create task"
                    hint="Opens task dialog"
                    onSelect={() => run(onCreateTask)}
                  />
                  <CmdItem
                    icon={<Lightbulb className="h-3.5 w-3.5" />}
                    label="Capture thought"
                    hint="Save a quick note"
                    onSelect={() => setMode("capture")}
                  />
                  {CREATE_LEAVES.map((leaf) => (
                    <CmdItem
                      key={leaf.to}
                      icon={<FilePlus2 className="h-3.5 w-3.5" />}
                      label={leaf.create!.label}
                      hint={createRoute(leaf.to)}
                      onSelect={() => run(() => navigate(createRoute(leaf.to)))}
                    />
                  ))}
                </Command.Group>

                <Command.Group heading="Actions" className="text-3xs uppercase tracking-wider text-tertiary">
                  <CmdItem
                    icon={<Share2 className="h-3.5 w-3.5" />}
                    label="Hand over…"
                    hint="Send what's open to another tool"
                    onSelect={() =>
                      run(() => {
                        const ir = getHandoverIR();
                        if (ir) openHandover(ir);
                        else
                          toast.error("Nothing to hand over here", {
                            description: "Open a note or select an asset first.",
                          });
                      })
                    }
                  />
                  <CmdItem
                    icon={<Camera className="h-3.5 w-3.5" />}
                    label="Hand over snapshot"
                    hint="Capture this view → a new task, note, or asset"
                    onSelect={() =>
                      run(async () => {
                        const ir = await captureSnapshot();
                        if (ir) openHandover(ir);
                        else
                          toast.error("Couldn't capture this view", {
                            description: "Nothing to snapshot here.",
                          });
                      })
                    }
                  />
                </Command.Group>

                <Command.Group heading="Go to" className="text-3xs uppercase tracking-wider text-tertiary">
                  {NAV_LEAVES.map((leaf) => (
                    <CmdItem
                      key={leaf.to}
                      icon={<CornerDownRight className="h-3.5 w-3.5" />}
                      label={titleCase(leaf.label)}
                      hint={leaf.to}
                      onSelect={() => run(() => navigate(leaf.to))}
                    />
                  ))}
                </Command.Group>
              </Command.List>
            </Command>
          )}
        </div>
      </Command.Dialog>
    </>
  );
}

interface CmdItemProps {
  icon: React.ReactNode;
  label: string;
  hint?: string;
  onSelect: () => void;
}

function CmdItem({ icon, label, hint, onSelect }: CmdItemProps) {
  return (
    <Command.Item
      onSelect={onSelect}
      className="flex items-center gap-3 rounded px-3 py-2 text-sm text-fg-muted cursor-pointer data-[selected=true]:bg-accent-subtle data-[selected=true]:text-fg"
    >
      <span className="text-fg-subtle">{icon}</span>
      <span className="flex-1">{label}</span>
      {hint && <span className="text-3xs text-tertiary">{hint}</span>}
    </Command.Item>
  );
}
