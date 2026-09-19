// SPDX-License-Identifier: Apache-2.0
/**
 * WORK/TASKS — three live routes behind one leaf.
 *
 *   /work/tasks              pages/tasks.tsx        the run list
 *   /work/tasks?view=gantt   pages/work-gantt.tsx   ruled onto a section, routes.ts:274
 *   /work/tasks/{id}         pages/task-detail.tsx  the detail
 *
 * WHY A MOUNT FILE AND NOT THREE REGISTRY LINES. A registry line resolves an
 * ADDRESS to a module, and these three share one address — the leaf. Which of
 * them renders is decided by `?view=` and by the presence of a detail id, both
 * of which the router already resolved and handed down as props. So the choice
 * belongs one level below the registry, in a module that is itself a chunk.
 *
 * THE DESCENDANT `<Routes>` IS NOT DECORATION, IT IS THE PAGE'S CONTRACT.
 * `task-detail.tsx:82` reads `useParams<{ id: string }>()`. Under the shell
 * there is no `:id` route to read from — the frame is matched by ONE splat route
 * and the shell resolves the id itself — so `useParams()` would hand the page
 * `undefined` and it would render an error state for a task that exists. Four
 * lines restore exactly the shape the page was written against, and they edit no
 * page. The pattern is relative (no leading slash) because a descendant
 * `<Routes>` matches the remainder of the path the splat parent consumed.
 *
 * `tests/mounts.test.tsx` asserts that remainder resolves, because it is the one
 * thing here that is a claim about React Router rather than about okuro.
 */

import { lazy } from "react";
import { Route, Routes } from "react-router";
import { retryOnce } from "@/lib/lazy-with-retry";
import type { LeafViewProps } from "../views/registry";
import { sectionSlugs } from "../views/sections";

const TasksPage = lazy(
  retryOnce(() => import("@/pages/tasks").then((m) => ({ default: m.TasksPage }))),
);
const WorkGanttPage = lazy(
  retryOnce(() => import("@/pages/work-gantt").then((m) => ({ default: m.WorkGanttPage }))),
);
const TaskDetailPage = lazy(
  retryOnce(() => import("@/pages/task-detail").then((m) => ({ default: m.TaskDetailPage }))),
);

/** Resolved from the declared section list, never a pinned index — inserting a
 *  section above GANTT must not silently start rendering the run list. */
const GANTT = sectionSlugs("work", "tasks").indexOf("gantt");

export default function WorkTasks({ id, view }: LeafViewProps) {
  if (id) {
    return (
      <Routes>
        <Route path=":topic/:leaf/:id" element={<TaskDetailPage />} />
      </Routes>
    );
  }
  if (GANTT >= 0 && view === GANTT) return <WorkGanttPage />;
  /* RUNS and SCHEDULES are two sections of ONE page, so the page needs the
     index. GANTT is a different page, which is why it is dispatched here. */
  return <TasksPage view={view} />;
}
