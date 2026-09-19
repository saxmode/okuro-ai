// SPDX-License-Identifier: Apache-2.0
/**
 * ONE PANE'S WORTH OF okuro — the boundary, the gate and the Suspense that used
 * to be three separate route wrappers in `app.tsx`.
 *
 * THE FALLBACK RENDERS NOTHING, AND THAT IS DELIBERATE. A spinner here would
 * appear and vanish inside the 650ms slide, which reads as a flash of debris
 * rather than as loading — and the pane it sits in is already animating, so the
 * user has motion to look at. An empty pane that fills in is the quieter lie.
 *
 * ONE BOUNDARY PER PANE rather than one around the plane: a boundary higher up
 * would suspend or blank the whole shell while a leaf loads or throws,
 * unmounting the bars that Law 4 says must stay. The thing that is allowed to
 * disappear is the pane, so that is where both wrappers sit. This is strictly
 * better than what it replaces — `withBoundary` wrapped a route element, so a
 * throwing page took the whole frame's content area with it.
 *
 * ---------------------------------------------------------------------------
 * THE FEATURE GATE MOVED HERE, AND IT HAD TO BE GIVEN THE OLD ADDRESS.
 * ---------------------------------------------------------------------------
 * It was a pathless layout route (`app.tsx::FeatureGate`) keyed on
 * `useLocation().pathname`. There is no route table under the shell, so it
 * becomes a per-leaf wrapper — and that move is exactly where it would have
 * broken. `src/okuro/features.py:245` is the only `routes=` declaration in the
 * whole feature registry; it reads `("/lessons",)`, and its feature ships
 * `default=False`. `isRouteWithheld("/know/lessons", ["/lessons"])` is FALSE, so
 * a gate testing only the new pathname would have quietly switched the feature
 * switch off. `routeAliases` hands it every spelling that addresses the leaf.
 *
 * THE THREE STATES ARE THE ONES `app.tsx` DOCUMENTED, unchanged, and the middle
 * one is still the deliberate part:
 *   - not settled → nothing, NOT the page. Rendering the page first would mount
 *     a withheld feature and fire its queries before the answer lands. It
 *     renders nothing rather than the old loader for the same reason the
 *     Suspense fallback does.
 *   - withheld → FeatureOffPage. A direct URL must explain itself: okuro's SPA
 *     fallback answers 200 for unknown paths, so "404" is not available and
 *     would be a lie anyway — the page exists, it is switched off.
 *   - otherwise → the page.
 */

import { Suspense } from "react";
import { ErrorBoundary } from "@/components/error-boundary";
import { FeatureOffPage } from "@/components/shell/feature-off-page";
import { isRouteWithheld } from "@/lib/features-api";
import { useFeatures } from "@/lib/features-context";
import { PaneActiveProvider } from "@/lib/pane-active";
import type { TopicId } from "../ia";
import { routeAliases } from "../routes";
import { viewFor } from "../views/registry";

export function LeafView({
  topic,
  leaf,
  id,
  view,
  onSelectView,
  active = true,
}: {
  topic: TopicId;
  leaf: string;
  id?: string;
  view: number;
  onSelectView?: (viewIndex: number) => void;
  /**
   * Whether this pane is the one the URL addresses — F5.
   *
   * DEFAULTS TO `true` SO EVERY CALLER OUTSIDE THE SHELL IS UNCHANGED.
   * `Frame.tsx`'s `EmbeddedLeaf` (`?embed=1`) renders one LeafView and nothing
   * is off-screen there, so it must keep polling exactly as before. Only
   * `TopicBar` has five panes to distinguish, and only it passes this.
   */
  active?: boolean;
}) {
  const View = viewFor(topic, leaf);
  const { withheldRoutes, settled } = useFeatures();
  const aliases = routeAliases(topic, leaf);

  if (!settled) return null;
  if (aliases.some((p) => isRouteWithheld(p, withheldRoutes))) {
    // Handed the same aliases: the page names the config key by matching a path
    // against each feature's declared `routes`, which are live addresses too.
    return <FeatureOffPage routes={aliases} />;
  }

  return (
    // THE PROVIDER WRAPS THE BOUNDARY, NOT THE PAGE, so a page that throws
    // still had the context while mounting and a retry gets the same answer.
    <PaneActiveProvider active={active}>
      <ErrorBoundary>
        <Suspense fallback={null}>
          <View topic={topic} leaf={leaf} id={id} view={view} onSelectView={onSelectView} />
        </Suspense>
      </ErrorBoundary>
    </PaneActiveProvider>
  );
}
