import { api } from "@/lib/api";

/**
 * The release maturity switch, client side.
 *
 * Mirrors `GET /api/features` (src/okuro/web/app.py::api_features), which
 * reads `okuro.features.FEATURES` fresh on every request. The declaration
 * lives in Python; this module never hard-codes a feature name or a route —
 * adding a feature is one line in `features.py` and nothing here changes.
 *
 * WHY THE API AND NOT A BUILD-TIME CONSTANT. The switch is per-INSTALL: the
 * same bundle ships to a developer box with `flow: true` and to a release
 * install with nothing set. Baking it in at build time would mean a rebuild
 * per install, which is exactly the property the config-time switch exists
 * to avoid.
 */

export interface FeatureInfo {
  enabled: boolean;
  /** Human sentence — what the feature is. Shown when a URL is refused. */
  summary: string;
  /** This feature's OWN route prefixes — what makes the owner resolvable. */
  routes: string[];
}

export interface FeaturesResponse {
  features: Record<string, FeatureInfo>;
  /**
   * The flat UNION of every off feature's routes — what the nav filter and
   * the route gate test against. Per-feature `routes` above is the mapping
   * that turns a refused URL back into a config key.
   */
  withheld_routes: string[];
}

/** Fail-open shape. See the fetch-failure note in `features-context.tsx`. */
export const FEATURES_OPEN: FeaturesResponse = {
  features: {},
  withheld_routes: [],
};

export function fetchFeatures(): Promise<FeaturesResponse> {
  return api<FeaturesResponse>("/api/features");
}

/**
 * Is `path` inside a withheld route subtree?
 *
 * Prefix match on SEGMENT boundaries: "/flow" withholds "/flow" and
 * "/flow/abc" but never "/flow-lab". A bare `startsWith` would swallow the
 * neighbour, which is the kind of silent over-hiding a visibility switch must
 * not do.
 */
export function isRouteWithheld(path: string, withheld: string[]): boolean {
  const clean = bareRoute(path);
  return withheld.some(
    (prefix) => clean === prefix || clean.startsWith(`${prefix}/`),
  );
}

/** Strip query and hash — a nav leaf may carry them (`/assets?view=media`). */
function bareRoute(path: string): string {
  return path.split("?", 1)[0]?.split("#", 1)[0] ?? "";
}

/**
 * Which declared feature owns `path` — LOOKED UP, never guessed.
 *
 * Each feature carries its own `routes` in the payload, so the owner of a
 * refused URL is a fact the server already knows. An earlier version inferred
 * it from the path instead ("/studio" must be "studio") because the payload
 * only had the flat union; that produced a literal `<name>` placeholder the
 * moment a feature was not spelled like its route, which is exactly the case
 * a config key most needs pre-filling.
 *
 * Only features that are OFF can own a refusal — an enabled feature's routes
 * are not withheld, so matching them would name a switch that is already on.
 * On an overlap the longest matching prefix wins, so a feature gating
 * "/studio/beta" is preferred over one gating "/studio".
 */
export function featureForRoute(
  path: string,
  features: Record<string, FeatureInfo>,
): string | null {
  const clean = bareRoute(path);
  let best: string | null = null;
  let bestLength = -1;

  for (const [name, info] of Object.entries(features)) {
    if (info.enabled) continue;
    for (const prefix of info.routes ?? []) {
      const hit = clean === prefix || clean.startsWith(`${prefix}/`);
      if (hit && prefix.length > bestLength) {
        best = name;
        bestLength = prefix.length;
      }
    }
  }
  return best;
}
