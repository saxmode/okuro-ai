// SPDX-License-Identifier: Apache-2.0
/**
 * KNOW/REPOS — a list and a detail.
 *
 *   /know/repos        pages/repos.tsx        the list
 *   /know/repos/{id}   pages/repo-detail.tsx  the detail
 *
 * The prefix was already ruled (`routes.ts:287` `LEGACY_PREFIXES`), so a live
 * `/repos/abc` keeps its tail across the redirect. `repo-detail.tsx:186` reads
 * `useParams<{ id: string }>()`, hence the descendant `<Routes>` — see
 * `mounts/work-tasks.tsx` for why that is the page's contract rather than a
 * decoration.
 */

import { lazy } from "react";
import { Route, Routes } from "react-router";
import { retryOnce } from "@/lib/lazy-with-retry";
import type { LeafViewProps } from "../views/registry";

const ReposPage = lazy(
  retryOnce(() => import("@/pages/repos").then((m) => ({ default: m.ReposPage }))),
);
const RepoDetailPage = lazy(
  retryOnce(() => import("@/pages/repo-detail").then((m) => ({ default: m.RepoDetailPage }))),
);

export default function KnowRepos({ id }: LeafViewProps) {
  if (id) {
    return (
      <Routes>
        <Route path=":topic/:leaf/:id" element={<RepoDetailPage />} />
      </Routes>
    );
  }
  return <ReposPage />;
}
