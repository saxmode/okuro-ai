// SPDX-License-Identifier: Apache-2.0
/**
 * DELIVER/PRISM — four live routes behind one leaf, three of them ruled.
 *
 *   /deliver/prism                      pages/prism.tsx        the documents list
 *   /deliver/prism?view=deck            pages/prism-deck.tsx   ruled, routes.ts:275
 *   /deliver/prism?view=redline         pages/redline.tsx      ruled, memory 0e3cfeaa
 *   /deliver/prism/{id}?view=redline    pages/redline.tsx      the reviewed document
 *
 * REDLINE'S ADDRESS, AND THE ONE PLACE IT DEPARTS FROM THE RULING'S SPELLING.
 * The owner ruled 2026-09-14: "redline lives at /deliver/prism?view=redline&id=…".
 * The id is carried in the PATH here rather than in a second query parameter,
 * because the shell's grammar is `/{topic}/{leaf}/{id}` and `?view=` is
 * deliberately the only thing that ever lives in the query (memory 2b036d78,
 * fact 3). Two id spellings would make one of them unreachable by `pathFor`,
 * and the deep link the ruling protects is preserved either way. Flagged in the
 * p2 report rather than decided quietly.
 *
 * `redline.tsx:83` reads `useParams<{ document_id: string }>()`, so the
 * descendant `<Routes>` names the segment `:document_id` — the page's own
 * contract, restored without editing it.
 *
 * `prism-gallery` IS DELIBERATELY ABSENT. It has no ruling: The owner, on the
 * inventory's Q3, said "I don't know. Prism is work in progress" and asked for
 * okuro-prism's own history to be read first. So no GALLERY section is declared
 * and the page stays reachable at its legacy path, named in `routes.ts::UNHOMED`
 * where an unplaced leaf is supposed to be named rather than swallowed.
 */

import { lazy } from "react";
import { Route, Routes } from "react-router";
import { retryOnce } from "@/lib/lazy-with-retry";
import type { LeafViewProps } from "../views/registry";
import { sectionSlugs } from "../views/sections";

const PrismPage = lazy(
  retryOnce(() => import("@/pages/prism").then((m) => ({ default: m.PrismPage }))),
);
const PrismDeckPage = lazy(
  retryOnce(() => import("@/pages/prism-deck").then((m) => ({ default: m.PrismDeckPage }))),
);
const RedlinePage = lazy(
  retryOnce(() => import("@/pages/redline").then((m) => ({ default: m.RedlinePage }))),
);

const SLUGS = sectionSlugs("deliver", "prism");
const DECK = SLUGS.indexOf("deck");
const REDLINE = SLUGS.indexOf("redline");

export default function DeliverPrism({ id, view }: LeafViewProps) {
  if (REDLINE >= 0 && view === REDLINE) {
    if (!id) return <RedlinePage />;
    return (
      <Routes>
        <Route path=":topic/:leaf/:document_id" element={<RedlinePage />} />
      </Routes>
    );
  }
  if (DECK >= 0 && view === DECK) return <PrismDeckPage />;
  return <PrismPage />;
}
