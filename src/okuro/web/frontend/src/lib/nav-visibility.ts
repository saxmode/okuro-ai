import { useMemo } from "react";
import { isRouteWithheld } from "@/lib/features-api";
import { useFeatures } from "@/lib/features-context";
import type { NavGroup } from "@/components/shell/nav-bar";

/**
 * Drop the nav leaves that lead into a withheld route.
 *
 * ONE helper, THREE consumers — `nav-bar.tsx` (desktop accordion),
 * `mobile-top-bar.tsx` (drawer) and `command-palette.tsx` (Cmd+K "Go to" and
 * "Create" lists). They all read the same `NAV_TREE`, so filtering in each of
 * them would be three copies of one rule and three chances for a feature to
 * stay reachable from the surface nobody re-checked. The palette is the one
 * that would have been forgotten: it is not visible until someone presses a
 * key, so a leak there survives every screenshot.
 *
 * The import is type-only on purpose — this module must stay cheap to test
 * without pulling the whole nav component and its animation deps into jsdom.
 */
export function visibleNavTree(
  tree: NavGroup[],
  withheldRoutes: string[],
): NavGroup[] {
  if (withheldRoutes.length === 0) return tree;
  return tree
    .map((group) => ({
      ...group,
      children: group.children.filter(
        (leaf) => !isRouteWithheld(leaf.to, withheldRoutes),
      ),
    }))
    // A group whose every leaf is withheld would render as a label that opens
    // onto nothing. Drop it rather than leave an empty accordion.
    .filter((group) => group.children.length > 0);
}

/**
 * The nav each shell surface should actually render.
 *
 * Returns an EMPTY tree until the features fetch settles — option B in the
 * loading-behaviour note in `features-context.tsx`. Every consumer gets that
 * rule for free by calling this instead of reading NAV_TREE directly, which
 * is the point: the "don't paint before you know" decision is made once.
 */
export function useVisibleNavTree(tree: NavGroup[]): NavGroup[] {
  const { withheldRoutes, settled } = useFeatures();
  return useMemo(
    () => (settled ? visibleNavTree(tree, withheldRoutes) : []),
    [tree, withheldRoutes, settled],
  );
}
