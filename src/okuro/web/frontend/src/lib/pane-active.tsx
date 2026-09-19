// SPDX-License-Identifier: Apache-2.0
//
// IS THIS PAGE THE ONE ON SCREEN? — the question a page under the shell could
// not ask, and the cause of a class of waste rather than of one bug.
//
// WHY IT IS NEEDED. Law 3 says nothing is ever removed: collapsed bars go to
// zero width and every bar keeps one pane mounted, so five pages are live at
// all times (memory deb246b8). Four of them are behind a 48px rail. Their
// `refetchInterval`s keep firing, their `window` keydown handlers stay armed,
// and neither is observable in a screenshot.
//
// MEASURED at HEAD cd25161db, isolated chromium, 16-second windows, the
// pathname asserted while away:
//
//   leaf            on its route   after switching topic   endpoints still polling
//   /work/agents     7 calls        7 calls                telemetry x2, gpu x2,
//                                                          host x2, dashboard/brain
//   /start/now      15 calls       15 calls                live-agents x6,
//                                                          active-roles x3, doctor x2,
//                                                          tasks?limit=20, brain
//   /system/health   2 calls        2 calls (both global chrome, not the page's)
//
// AND ONE CORRECTION TO WORK'S B7, which named `use-deliberation`'s 5s
// capability-gap poll on `task-detail` as the clearest case. Measured, THAT ONE
// ALREADY STOPS: 3 calls on the route, 0 after switching topic, 2 on return.
// Not because anything pauses it — because a topic switch strips the detail id
// (`App.tsx` passes `id` only to the active bar), so `task-detail` unmounts and
// the pane renders the task LIST. The polls that survive are the ones on a
// LEAF's own page, which is why the proof site here is `use-dashboard`.
//
// THE DEFAULT IS `true`, AND THAT IS THE IMPORTANT PART OF THE CONTRACT.
// Outside a provider — `?embed=1`, `/onboarding`, `/q/:token`, a unit test —
// there is no shell and nothing is off-screen, so every consumer must behave
// exactly as it did before this file existed. A default of `false` would
// silently stop polling on the six routes that render without the frame.
import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";

/** true = this pane is the addressed one. See the note above for the default. */
const PaneActiveContext = createContext<boolean>(true);

export function PaneActiveProvider({
  active,
  children,
}: {
  active: boolean;
  children: ReactNode;
}) {
  return <PaneActiveContext.Provider value={active}>{children}</PaneActiveContext.Provider>;
}

/**
 * Whether the calling component's pane is the one the URL addresses.
 *
 * `true` outside the shell, always — see the contract above.
 */
export function useIsPaneActive(): boolean {
  return useContext(PaneActiveContext);
}

/**
 * A `refetchInterval` that only runs while this pane is on screen.
 *
 *     refetchInterval: usePaneInterval(8_000)
 *
 * `false` is what TanStack Query reads as "do not poll", and it is reversible:
 * when the pane comes back the interval resumes and a stale query refetches at
 * once, so returning to a leaf shows current data rather than the numbers it
 * had when you left. That is why this pauses the INTERVAL rather than disabling
 * the query — `enabled: false` would also throw away the cached data and leave
 * the returning pane blank for a beat.
 *
 * It takes `number | false` so a caller that is already conditional stays
 * conditional: a query that was not polling does not start.
 */
export function usePaneInterval(ms: number | false): number | false {
  return useIsPaneActive() ? ms : false;
}

/**
 * Whether a `window`-level key handler should be armed.
 *
 * Same question, different consumer. Five panes are mounted, so a page's
 * `window.addEventListener("keydown", …)` fires while another topic is on
 * screen. WORK measured four such listeners and observed no collision —
 * nothing in WORK competes for the same key — which makes this a latent
 * defect rather than a live one, and the reason it is offered rather than
 * forced: arming logic belongs in the component that owns the listener.
 *
 * Adoption sites named by p3, all of them `src/components/**` and therefore
 * outside the frame batch: `flow-designer.tsx:247`, `flow-designer.tsx:835`,
 * `artifacts-viewer.tsx:874`, `flow-feedback-card.tsx:256`.
 */
export function usePaneKeyboardArmed(): boolean {
  return useIsPaneActive();
}

/**
 * Run `effect` ONCE, the first time this pane is the addressed one.
 *
 * THE CLASS THIS CLOSES: a mount-time side effect that treats "mounted" as
 * "the user looked at it". Law 3 broke that equivalence — five panes are live
 * at all times, so a mount says nothing about what is on screen. The two hooks
 * above stop WASTE of that shape (a poll, an armed key); this one stops a
 * WRITE, which is the expensive kind because it is not recoverable by waiting.
 *
 * MEASURED, the instance that found it: `POST /api/visits/projects` fired on
 * every address after `/work/projects` — 41 times across an 81-address sweep —
 * because the sweep loads a fresh document per address and the shell mounts the
 * remembered WORK leaf on each one. Nothing was on screen; the board's Zone 1
 * ribbon diffs against `previous_seen_at`, so each spurious visit moved the
 * reference for "what changed since you last looked".
 *
 * WHY NOT THE VALUE GUARD THAT FIXED THE PEOPLE GRAPH (memory 9bac74aa). That
 * one suppressed a re-push of IDENTICAL numbers at the persistence layer, which
 * is the right shape when a machine writer re-derives a value a human set. A
 * visit has no identical value to compare: a new timestamp is the entire
 * payload, and it is correct every time it is asked for. The only thing that
 * can separate a real visit from an incidental mount is whether this pane is
 * the address, which is exactly the question this module answers.
 *
 * ONCE PER MOUNT, deliberately, and it preserves the semantics the ref guard
 * at the call site already had: arriving, leaving and returning WITHIN one
 * document does not re-fire, because Law 3 never unmounted the pane. This adds
 * the missing condition; it does not invent a new rule about how often a visit
 * counts.
 *
 * THE CALLBACK IS HELD IN A REF so an inline closure — which is how every
 * caller will write it — cannot re-arm the effect. The guard would catch that
 * anyway; the ref is what keeps the dependency list honest instead of silenced.
 */
export function usePaneFirstActive(effect: () => void): void {
  const active = useIsPaneActive();
  const latest = useRef(effect);
  latest.current = effect;
  const ran = useRef(false);
  useEffect(() => {
    if (!active || ran.current) return;
    ran.current = true;
    latest.current();
  }, [active]);
}

/**
 * The `open` a modal may actually have, given whether its pane is on screen.
 *
 * WHY A MODAL NEEDS THIS AND A PANEL DOES NOT. A modal takes the whole app
 * hostage while it is up: Radix puts `pointer-events: none` on `<body>` and
 * lays an overlay over everything. Law 3 keeps the departing pane mounted
 * through a topic change, so the page does not unmount and its `open` state
 * survives — and the modal then hangs over a completely different leaf with the
 * entire app unclickable. MEASURED on 2026-09-15 at HEAD e94c6d7b4, real
 * gestures (topic-bar clicks, then browser Back), two KNOW leaves:
 *
 *   at /start/inbox after Back   REPOS   CORPORA
 *     dialogs mounted              1        1
 *     overlay up                   1        1
 *     body pointer-events         none     none
 *     clicking a topic bar      timeout  timeout
 *
 * Only Escape got the user out, and nothing on screen said so.
 *
 * WHY IT LIVES IN THE PRIMITIVE. 29 `<Dialog open={…}>` / `<Sheet open={…}>`
 * sites across 22 files have the same hole; a per-page fix is 22 chances to
 * forget one (DP11). "Close when your pane is off-screen" is never wrong for
 * something modal, and `useIsPaneActive` defaults to `true`, so nothing outside
 * the shell — `?embed=1`, `/onboarding`, `/q/:token`, a unit test — changes.
 *
 * TWO EFFECTS, BOTH NEEDED. The returned value closes the modal on the spot,
 * which is what frees the pointer; the `onOpenChange(false)` tells the owner so
 * its own state agrees, otherwise returning to the leaf brings the modal back.
 *
 * It only steers a CONTROLLED modal. The two uncontrolled ones in the tree are
 * both in the design-system showcase (`component-showcase.tsx:637,662`), which
 * renders trigger-driven demos inside an iframe and is not a leaf pane.
 */
export function usePaneModalOpen(
  open: boolean | undefined,
  onOpenChange?: (open: boolean) => void,
): boolean | undefined {
  const active = useIsPaneActive();
  const forceClosed = open === true && !active;
  useEffect(() => {
    if (forceClosed) onOpenChange?.(false);
  }, [forceClosed, onOpenChange]);
  return forceClosed ? false : open;
}
