// <!-- AGENT_HEADER
// role: code
// purpose: /workflows route — gallery when no workflow is targeted, editor
//   otherwise. ?id= opens one · ?new=1 starts a blank one.
// AGENT_HEADER_END -->
import { ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import React from "react";
import { useNavigate, useSearchParams } from "react-router";

import type { LeafViewProps } from "@/shell/views/registry";
import { sectionSlugs } from "@/shell/views/sections";

// The canvas chrome, tokens and side-panel layout come from okuro-flow's
// stylesheet: this surface is a DIFFERENT tool over the same components, so it
// shares the look without sharing a document store or any node semantics.
import "@/components/graph/graph.css";
import "@/components/workflow-designer/workflow-designer.css";
import { WorkflowDesigner } from "@/components/workflow-designer/workflow-designer";
import { WorkflowGallery } from "@/components/workflow-designer/workflow-gallery";

/** `?view=new` is the blank canvas — index of the "NEW" section, resolved from
 *  the declared list rather than pinned, so inserting a section cannot turn a
 *  gallery visit into a new document. */
const NEW_SECTION = sectionSlugs("work", "workflows").indexOf("new");

export function WorkflowsPage({ id: pathId, view }: Partial<LeafViewProps> = {}) {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  /**
   * THE ID LIVES IN THE PATH — ruled jointly with FLOW (spec Q1 option A),
   * because the two leaves share one save coordinator and a grammar that
   * differed between them would make the shared writer branch on which leaf
   * it is inside, which is the coupling the platform module removes.
   *
   *   /work/workflows              the gallery
   *   /work/workflows/{id}         the editor
   *   /work/workflows?view=new     a blank canvas   (was ?new=1)
   *
   * `?id=` is still READ, and deliberately: 106 flows and every saved
   * bookmark spell it that way, and the shell's own `mergeSearch` now
   * delivers it intact. The PATH is what this leaf writes.
   */
  const id = pathId ?? params.get("id");
  const isNew = view === NEW_SECTION || params.get("new") === "1";


  // THE ADDRESS IS THE ROUTER'S, NOT THE MODULE'S.
  //
  // The graph module used to write `?id=` with `window.history.replaceState`,
  // which is wrong under the shell for a reason no care inside the module can
  // fix: one query string is shared by all leaves and two panes stay mounted
  // through the 650 ms slide, so a write that resolves after the user has left
  // stamps this document's id onto the leaf they went to. Measured 2026-09-14:
  // switching away 60 ms or 150 ms after a load left `/work/agents?id=<a flow
  // id>`, and the arriving WORKFLOWS leaf then asked its own store for that
  // id and logged six 404s.
  //
  // The updater form MERGES. A fresh `URLSearchParams` here would be the same
  // fresh-object writer that evicts `?view=` on the gantt — the whole query
  // belongs to the shell, and this callback owns exactly one key of it.
  const syncId = React.useCallback(
    (next: string | null) => {
      /* The PATH is the address; the query keeps everything else. `?id=` is
         actively REMOVED when a path id exists, so one document never ends up
         with two spellings in the history. */
      const merged = new URLSearchParams(params);
      merged.delete("id");
      merged.delete("new");
      const q = merged.toString();
      navigate(
        (next ? `/work/workflows/${encodeURIComponent(next)}` : "/work/workflows") +
          (q ? `?${q}` : ""),
        { replace: true },
      );
    },
    [navigate, params],
  );

  if (!id && !isNew) {
    return (
      <div className="relative h-full w-full overflow-hidden">
        <WorkflowGallery />
      </div>
    );
  }

  return (
    <div className="relative h-full w-full overflow-hidden">
      <ReactFlowProvider>
        <WorkflowDesigner workflowId={id} syncId={syncId} />
      </ReactFlowProvider>
    </div>
  );
}
