// SPDX-License-Identifier: Apache-2.0
/**
 * THE SHELL'S ONE SUBSCRIPTION TO AGENT ACTIVITY.
 *
 * ===========================================================================
 * WHY THIS EXISTS — AND WHY `Blob.tsx`'s THIRD SUBTRACTION IS NOW WRONG
 * ===========================================================================
 * `Blob.tsx` shipped with: *"NO ACTIVITY IS FED IN. The engine idles on its own
 * defaults."* That made the blob's size CONSTANT — and the engine's size IS the
 * visualization:
 *
 *   baseR = min(w,h) * (0.12 + intensity * 0.22)
 *   intensity 0 -> 27.6px in a 230px box   (a small disc, forever)
 *   intensity 1 -> 78.2px in a 230px box   (what the design shows)
 *
 * So "agentic activity is not visualized" and "the open blob looks too small"
 * were one defect, not two. The owner, 2026-09-16: the panel's activity must be
 * visible again, and the `23 C · 5 A` bar meter from the prototype gets built.
 * Both read the same numbers, so both read them from HERE — one subscription,
 * not two.
 *
 * ===========================================================================
 * THE COST, STATED RATHER THAN HIDDEN
 * ===========================================================================
 * `use-activity-stream.ts` carries a comment that this change invalidates, and
 * it is updated in the same commit rather than left to rot:
 *
 *   *"VERIFIED SAFE TO PAUSE: `usePulseData` has exactly two consumers, both of
 *   them NOW's own files. `shell/components/Blob.tsx` deliberately does NOT
 *   subscribe, so no always-mounted chrome depends on these three."*
 *
 * It does now. `usePaneInterval` gates on the PANE a consumer sits in, and the
 * frame sits in none, so its default `true` applies and the three polls
 * (doctor 8s, roles 5s, live-agents 3s) run on every route instead of only
 * while `/start/now` is the visible pane.
 *
 * THREE THINGS KEEP THAT AFFORDABLE, and none of them is an assumption:
 *   1. React Query dedupes by `queryKey`. `pages/home.tsx` and
 *      `welcome-panel.tsx` mount the same hook with the same keys, so this is
 *      ONE set of requests shared with them, not a second set.
 *   2. `refetchIntervalInBackground` is not set, and its default is `false` —
 *      the intervals stop while okuro's tab is not visible.
 *   3. The websocket was never paused anyway (that hook's own decision, for
 *      `connected`), so the push channel costs nothing new.
 *
 * ===========================================================================
 * AN ABSENT PROVIDER IS NOT AN ERROR
 * ===========================================================================
 * Same contract as `CornerActionsProvider`: `?embed=1`, `/onboarding`, `/q/:token`
 * and every unit test render the consumers with no provider above them. They get
 * the idle default and behave exactly as they did before this file existed —
 * which is also what keeps `Blob.tsx` mountable in vitest with no query client.
 */

import { createContext, useContext, type ReactNode } from "react";
import { usePulseData, type UsePulseDataResult } from "@/hooks/use-activity-stream";

/** The idle reading — what a consumer outside a provider sees. */
const IDLE: UsePulseDataResult = {
  activity: undefined,
  activityStream: [],
  services: undefined,
  activeRoles: [],
  liveAgents: [],
  isActive: false,
  connected: false,
};

const PulseDataContext = createContext<UsePulseDataResult>(IDLE);

/**
 * Mounted by `Frame.tsx`, INSIDE the query-client provider and OUTSIDE `.app`.
 *
 * It is a component rather than a call in `Frame` itself for one reason: a hook
 * in `Frame` re-renders `Frame` on every socket message, and `Frame` owns
 * `CreateDialog`'s open state and the whole shell beneath it. Here the
 * re-render stops at this boundary and only the two consumers follow.
 */
export function PulseDataProvider({ children }: { children: ReactNode }) {
  const data = usePulseData();
  return <PulseDataContext.Provider value={data}>{children}</PulseDataContext.Provider>;
}

export function useShellPulse(): UsePulseDataResult {
  return useContext(PulseDataContext);
}
