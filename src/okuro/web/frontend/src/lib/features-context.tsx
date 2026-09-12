import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  FEATURES_OPEN,
  fetchFeatures,
  isRouteWithheld,
  type FeatureInfo,
  type FeaturesResponse,
} from "@/lib/features-api";

/**
 * The release maturity switch, loaded once for the whole shell.
 *
 * Same shape as `chrome-context.tsx`: a provider near the root, a hook that
 * degrades to a safe no-op outside it so no caller needs a null check. Here
 * "safe" means EVERYTHING VISIBLE — see the fail-open note below.
 *
 * LOADING BEHAVIOUR — THE CHOICE, AND WHY.
 * Two options, and they fail in opposite directions:
 *
 *   A. Hide nothing until loaded. Nav paints immediately, then Agents and
 *      Flow vanish a moment later. A withheld page also MOUNTS and fires its
 *      queries before the answer arrives.
 *   B. Render the nav only once the fetch has settled. One round trip of an
 *      empty nav row on first paint, then the correct nav, forever (the
 *      answer is cached for the session).
 *
 * B ships. A flash of a surface someone deliberately switched off is the
 * exact failure this whole module exists to prevent, and it is not
 * recoverable — the user saw it. An empty nav row for one same-origin
 * request is not a failure, it is a paint. The cost is paid once per app
 * load, not per navigation: `staleTime: Infinity` means every route change
 * afterwards reads the cache.
 *
 * FAIL-OPEN ON ERROR, deliberately, and this is the one place the two rules
 * disagree. "Settled" includes FAILED: if the endpoint 500s or the network
 * drops, `withheldRoutes` is empty and the whole UI is visible. A visibility
 * switch that blanks the app when its own API hiccups turns a cosmetic
 * setting into an outage, and the release install where this matters is the
 * one least able to debug it. The trade is explicit: a broken /api/features
 * may briefly expose a preview page. That is strictly better than a nav bar
 * with nothing in it, and it is logged to the console so it is diagnosable.
 */

export interface FeaturesApi {
  /** Every declared feature and its state. Empty while loading or on error. */
  features: Record<string, FeatureInfo>;
  /** Route prefixes to withhold. Empty while loading or on error (fail-open). */
  withheldRoutes: string[];
  /** True once the fetch has resolved OR failed. Gates the nav — see above. */
  settled: boolean;
}

const OPEN: FeaturesApi = {
  features: FEATURES_OPEN.features,
  withheldRoutes: FEATURES_OPEN.withheld_routes,
  // Outside the provider (embed mode, tests, a standalone story) there is no
  // fetch to wait for, so "settled" is true and everything is visible.
  settled: true,
};

const FeaturesContext = createContext<FeaturesApi | null>(null);

export function FeaturesProvider({ children }: { children: ReactNode }) {
  const { data, isSuccess, isError, error } = useQuery<FeaturesResponse>({
    queryKey: ["features"],
    queryFn: fetchFeatures,
    // The switch is read fresh per REQUEST on the server; per SESSION here.
    // Flipping a feature in config.yaml takes effect on the next page load,
    // which is the same contract the CLI and MCP seams have (both read at
    // process start).
    staleTime: Infinity,
    retry: 1,
  });

  if (isError) {
    // Not a toast: the user cannot act on this and the UI is fully usable.
    // A console line is what the person debugging "why is Flow visible in
    // the release build" needs to find.
    console.warn(
      "[features] /api/features unreachable — failing OPEN, every surface " +
        "is visible. Withheld routes cannot be enforced until it answers.",
      error,
    );
  }

  const value = useMemo<FeaturesApi>(
    () => ({
      features: data?.features ?? {},
      withheldRoutes: data?.withheld_routes ?? [],
      settled: isSuccess || isError,
    }),
    [data, isSuccess, isError],
  );

  return (
    <FeaturesContext.Provider value={value}>{children}</FeaturesContext.Provider>
  );
}

export function useFeatures(): FeaturesApi {
  return useContext(FeaturesContext) ?? OPEN;
}

/**
 * Is the given path inside a feature that is switched off?
 *
 * Answers `false` until the fetch settles, so nothing is refused on a guess.
 * Pair it with `settled` when the caller must not render the page either way.
 */
export function useRouteWithheld(path: string): boolean {
  const { withheldRoutes, settled } = useFeatures();
  return settled && isRouteWithheld(path, withheldRoutes);
}
