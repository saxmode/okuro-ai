// SPDX-License-Identifier: Apache-2.0
/**
 * DELIVER/SLIDES — three addresses behind one leaf.
 *
 *   /deliver/slides                  the deck gallery        (DECKS)
 *   /deliver/slides/{id}             the editor              (EDIT)
 *   /deliver/slides/{id}?view=play   the reader              (PLAY)
 *
 * WHY A MOUNT AND NOT A REGISTRY LINE. The leaf used to be one registry entry
 * and one declared section, and it opened `decks[0]` on arrival — D6. There was
 * no way to link to a deck, so there was no way to send one to anybody, on the
 * leaf whose entire purpose is sending decks to people.
 *
 * The id lives in the PATH and the mode in `?view=`, which is the grammar FLOW
 * and WORKFLOWS were jointly ruled onto. Unlike those two, SLIDES needs no
 * descendant `<Routes>`: the page reads its deck id from the `id` prop the
 * shell already resolved, and never calls `useParams`. A `<Routes>` here would
 * be decoration — the thing `mounts/work-tasks.tsx` warns it is not, when a
 * page genuinely does call `useParams`.
 *
 * So this file exists for ONE reason: to keep the page out of the business of
 * deciding whether it is a gallery. It hands the page what the router resolved
 * and lets the page render the right thing, which is the same split the other
 * mounts make.
 */

import { lazy } from "react";
import { retryOnce } from "@/lib/lazy-with-retry";
import type { LeafViewProps } from "../views/registry";

const SlidesPage = lazy(
  retryOnce(() => import("@/pages/slides").then((m) => ({ default: m.SlidesPage }))),
);

export default function DeliverSlides({ id, view, onSelectView }: LeafViewProps) {
  return <SlidesPage id={id} view={view} onSelectView={onSelectView} />;
}
