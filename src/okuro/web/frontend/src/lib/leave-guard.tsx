/**
 * LEAVING AN AUTHORING PAGE WITH A SYSTEM YOU WORKED ON, AND NEVER MADE LIVE.
 *
 * The owner's rule, 2026-09-12, verbatim: "if a user worked on a design system
 * and leaves the engine editor, it should ask whether the current system should
 * be set as default."
 *
 * THE THREE HALVES OF "WORKED ON AND NOT LIVE", and each one is there to stop a
 * prompt that would be noise:
 *
 *   1. the opened kit is NOT the active one. Asking whether the system already
 *      painting the app should be made the default is a question with no
 *      answer, so `opened === active` never prompts.
 *   2. it was EDITED this session — dirty now, or saved at least once since the
 *      page loaded. Inspecting rows is the ordinary way to use the library and
 *      must cost nothing; a dialog for it would be the modal that teaches people
 *      to dismiss modals.
 *   3. there is an opened kit at all.
 *
 * WHY THE NAVIGATOR IS PATCHED AND `useBlocker` IS NOT USED. `useBlocker`
 * (react-router 7.14) opens with `useDataRouterContext("useBlocker")` and
 * throws outside a data router. `app.tsx:318` mounts `<BrowserRouter>`, which
 * is not one — converting the whole app's routing to `createBrowserRouter` to
 * put a dialog on two pages is a change to every route in okuro for the benefit
 * of two. So the guard wraps the `navigator` the router already publishes on
 * `UNSAFE_NavigationContext`: every in-app navigation — `<Link>`, `navigate()`,
 * a redirect — goes through `push` / `replace` / `go` on that object, and the
 * wrapper is removed the moment the guard disarms or the page unmounts.
 *
 * WHAT THAT DOES NOT REACH, stated rather than implied: the browser's own BACK
 * button. `BrowserRouter` subscribes to `popstate` directly, so a history pop
 * never passes through `navigator`. Closing or reloading the tab is covered by
 * `beforeunload` below; a back-press out of the editor with unsaved edits is
 * not, and pretending otherwise by pushing a decoy history entry breaks the
 * back button for everyone to catch one case.
 *
 * NEVER `window.confirm` (charter): a native dialog blocks the event loop and
 * takes every Playwright gate with it. This is the vendored `Dialog` primitive.
 *
 * `beforeunload` IS REGISTERED FOR THE DIRTY CASE ONLY. The native prompt is
 * allowed there — it is the one place the browser will not let a page draw its
 * own — and it is the only case where leaving actually destroys something. A
 * saved-but-not-default kit is on disk; closing the tab loses nothing.
 */

import * as React from "react";
import { UNSAFE_NavigationContext } from "react-router";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** The subset of the router's navigator this guard wraps. */
type Navigator = {
  push: (...args: unknown[]) => void;
  replace: (...args: unknown[]) => void;
  go: (...args: unknown[]) => void;
};

export interface LeaveGuardOptions {
  /** The kit the authoring page currently has open. */
  openedKit: string | null | undefined;
  /** The kit `/engine.css` is painting the app with, read from the server. */
  activeKit: string | null | undefined;
  /** Unsaved edits on screen right now. */
  dirty: boolean;
  /** The opened kit was saved at least once since this page loaded. */
  savedThisSession: boolean;
  /** False for a shipped kit — `store.save` refuses it, so edits cannot land. */
  editable: boolean;
  /**
   * Save if there is anything to save, then make the kit the default and
   * repaint. The page owns the save because only the page knows what its draft
   * is; the default-write is `lib/active-kit.ts` for every caller.
   */
  onSetDefault: () => Promise<void>;
  /** Open the page's EXISTING duplicate flow. Never a second fork path. */
  onDuplicate: () => void;
}

export interface LeaveGuard {
  /** Mount this anywhere in the page. It renders nothing until it asks. */
  dialog: React.ReactNode;
  /** Whether the guard would currently intercept a navigation. For gates. */
  armed: boolean;
}

export function useLeaveGuard(options: LeaveGuardOptions): LeaveGuard {
  const {
    openedKit,
    activeKit,
    dirty,
    savedThisSession,
    editable,
    onSetDefault,
    onDuplicate,
  } = options;

  const armed = Boolean(
    openedKit && activeKit && openedKit !== activeKit && (dirty || savedThisSession),
  );

  const [pending, setPending] = React.useState<(() => void) | null>(null);
  const [working, setWorking] = React.useState(false);

  /* The latest values, without re-wrapping the navigator on every keystroke.
     Re-installing the wrapper mid-navigation is how an interception gets lost. */
  const armedRef = React.useRef(armed);
  armedRef.current = armed;

  const { navigator } = React.useContext(UNSAFE_NavigationContext) as {
    navigator: Navigator;
  };

  React.useEffect(() => {
    if (!navigator) return;
    const original = {
      push: navigator.push,
      replace: navigator.replace,
      go: navigator.go,
    };
    const wrap = (key: keyof typeof original) =>
      function wrapped(this: unknown, ...args: unknown[]) {
        if (!armedRef.current) {
          original[key].apply(navigator, args);
          return;
        }
        /* THE NAVIGATION IS KEPT, NOT RE-DERIVED. "Leave without" has to land on
           the destination the user asked for, and the only faithful record of
           that is the arguments the router was about to use. Re-reading a
           location later would send them somewhere else. */
        setPending(() => () => original[key].apply(navigator, args));
      };
    navigator.push = wrap("push");
    navigator.replace = wrap("replace");
    navigator.go = wrap("go");
    return () => {
      navigator.push = original.push;
      navigator.replace = original.replace;
      navigator.go = original.go;
    };
  }, [navigator]);

  /* THE NATIVE PROMPT, FOR THE ONE CASE THE BROWSER OWNS. Registered only while
     there are unsaved edits: a `beforeunload` handler that is always attached
     makes every reload of the page cost a click. */
  React.useEffect(() => {
    if (!armed || !dirty) return;
    const ask = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      /* Chromium still wants the legacy assignment; the string is never shown. */
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", ask);
    return () => window.removeEventListener("beforeunload", ask);
  }, [armed, dirty]);

  const leave = React.useCallback(() => {
    const go = pending;
    setPending(null);
    /* Disarm before replaying, or the wrapper intercepts its own replay. */
    armedRef.current = false;
    go?.();
  }, [pending]);

  const stay = React.useCallback(() => setPending(null), []);

  const setDefaultAndLeave = React.useCallback(async () => {
    setWorking(true);
    try {
      await onSetDefault();
      leave();
    } finally {
      setWorking(false);
    }
  }, [onSetDefault, leave]);

  const duplicateInstead = React.useCallback(() => {
    /* NOT a leave. The point of this branch is that the work has nowhere to go
       yet, so it opens the fork dialog and keeps the user where they are. */
    setPending(null);
    onDuplicate();
  }, [onDuplicate]);

  /* A SHIPPED KIT WITH UNSAVED EDITS CANNOT BE MADE THE DEFAULT, and saying so
     is the whole content of this branch. `store.save` refuses a shipped id with
     a 409; offering "Set as default" here would render a control whose
     precondition lives one layer down — the exact shape the charter records the
     accent picker dying of. Either lift the condition to the control, or delete
     the control. */
  const shippedWithEdits = dirty && !editable;

  const dialog = (
    <Dialog open={pending !== null} onOpenChange={(open) => { if (!open) stay(); }}>
      <DialogContent showCloseButton={false} data-leave-guard>
        <DialogHeader>
          <DialogTitle>
            {shippedWithEdits
              ? `${openedKit} ships with okuro and cannot be edited`
              : `Make ${openedKit} the design system okuro uses?`}
          </DialogTitle>
          <DialogDescription>
            {shippedWithEdits
              ? "Your changes are on screen only — a shipped system refuses a save. Duplicate it first and the changes come with the copy, which you can then set as the default."
              : dirty
                ? "You have unsaved changes. Setting it as the default saves them first, then repaints okuro with this system."
                : "You worked on this system but okuro is still painted with another one."}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={stay} data-leave-guard-action="stay">
            Stay
          </Button>
          <Button variant="outline" onClick={leave} data-leave-guard-action="leave">
            Leave without
          </Button>
          {shippedWithEdits ? (
            <Button onClick={duplicateInstead} data-leave-guard-action="duplicate">
              Duplicate first
            </Button>
          ) : (
            <Button
              onClick={() => void setDefaultAndLeave()}
              disabled={working}
              data-leave-guard-action="set-default"
            >
              {working ? "Setting…" : "Set as default"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return { dialog, armed };
}
