// SPDX-License-Identifier: Apache-2.0
/**
 * DELIVER/MEDIA — THE ONE LEAF WITH NO ROUTE OF ITS OWN, and this file is why
 * it needs a mount rather than a registry line.
 *
 * MEDIA is not a page. It is a MODE of `/assets`: `pages/assets.tsx:184` reads
 * `?view=media` once on mount and `:408` returns
 * `components/assets/media-grid.tsx` instead of the icon browser. Two IA leaves,
 * one page file, two unrelated endpoint families — `/api/assets/icons/*` for
 * ASSETS and `/api/assets/media/*` for MEDIA (inventory 7c43ed26 §6.2, §6.4).
 *
 * SO WHY NOT JUST MOUNT `AssetsPage` HERE. Because the two `?view=` grammars
 * collide on the same query key. `/assets?view=media` is the live address; the
 * shell's address for this leaf is `/deliver/media`, whose `?view=` slugs are
 * its OWN sections (ALL, IMAGE, AUDIO, VIDEO). Measured consequence: at
 * `/deliver/media` the page sees no `?view=media` and opens the icon browser,
 * and at `/deliver/media?view=image` it sees `view=image`, which is not
 * `"media"`, and opens the icon browser again. Mounting the page would show
 * ASSETS under the MEDIA leaf in every state.
 *
 * MOUNTING THE GRID DIRECTLY IS NOT A SHORTCUT PAST THAT — it is the same
 * component the live `?view=media` renders, reached by the same import
 * specifier, with no page edited. What the mount has to supply is the one prop
 * the page was supplying: `onBack`, which in the page flips its local state back
 * to the icon browser. Under the shell "back to icons" is a different leaf, so
 * it navigates to it — the shell's own address for ASSETS, which is what the
 * button now means.
 */

import { lazy } from "react";
import { useNavigate } from "react-router";
import { retryOnce } from "@/lib/lazy-with-retry";
import type { LeafViewProps } from "../views/registry";
import { leafSlugs, pathFor } from "../routes";
import { sectionSlugs } from "../views/sections";

const MediaGrid = lazy(
  retryOnce(() =>
    import("@/components/assets/media-grid").then((m) => ({ default: m.MediaGrid })),
  ),
);

/** Resolved from the IA, not pinned: ASSETS is index 1 under DELIVER today. */
const ASSETS = leafSlugs("deliver").indexOf("assets");

/**
 * THE FOUR DECLARED SECTIONS ARE THE FOUR KIND FILTERS, and the mapping lives
 * here rather than in the grid. `ALL` is section 0 and means no filter, so the
 * bare `/deliver/media` is what it always was and no address moves. Derived
 * from `sectionSlugs` rather than retyped: a rename in `sections.ts` then moves
 * both halves or fails the build.
 */
const MEDIA_SLUGS = sectionSlugs("deliver", "media");
/** section index -> the `kind` the endpoint takes. "" is All. */
const KIND_OF = (index: number): string => {
  const slug = MEDIA_SLUGS[index];
  return !slug || slug === "all" ? "" : slug;
};
const SECTION_OF = (kind: string): number => {
  const i = MEDIA_SLUGS.indexOf(kind || "all");
  return i < 0 ? 0 : i;
};

export default function DeliverMedia({ view, onSelectView }: Partial<LeafViewProps> = {}) {
  const navigate = useNavigate();
  return (
    <MediaGrid
      onBack={() => navigate(pathFor("deliver", ASSETS < 0 ? 0 : ASSETS))}
      kind={KIND_OF(view ?? 0)}
      /* `onSelectView` IS ABSENT outside the shell and for a pane on its way
         out, and the grid then falls back to its own state — the same reason
         `pane-active` defaults to true. Without the fallback the chips would be
         inert on the live `/assets?view=media` path. */
      onKindChange={
        onSelectView ? (kind) => onSelectView(SECTION_OF(kind)) : undefined
      }
    />
  );
}
