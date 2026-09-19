// <!-- AGENT_HEADER
// role: code
// purpose: /flow route — gallery when no flow is targeted, editor otherwise.
//   ?id= opens a flow · ?new=1 starts a blank flow · ?embed=1 read-only embed ·
//   bare /flow shows the thumbnail gallery (find / organise flows).
// AGENT_HEADER_END -->
import { ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import React from "react";
import { useNavigate, useSearchParams } from "react-router";

import type { LeafViewProps } from "@/shell/views/registry";
import { sectionSlugs } from "@/shell/views/sections";

import { FlowDesigner } from "@/components/flow-designer/flow-designer";
import "@/components/graph/graph.css";
import { FlowGallery } from "@/components/flow-designer/flow-gallery";

/** `?view=new` is the blank canvas — resolved from the declared list rather
 *  than pinned, so inserting a section cannot turn a gallery visit into a new
 *  document. */
const NEW_SECTION = sectionSlugs("work", "flow").indexOf("new");

export function FlowPage({ id: pathId, view }: Partial<LeafViewProps> = {}) {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  /**
   * THE ID LIVES IN THE PATH — ruled jointly with WORKFLOWS (spec Q1 option
   * A). The two leaves share one save coordinator, so a grammar that differed
   * between them would make the shared writer branch on which leaf it is
   * inside, which is the coupling the platform module removes.
   *
   *   /work/flow                  the gallery
   *   /work/flow/{id}             the editor
   *   /work/flow?view=new         a blank canvas          (was ?new=1)
   *   /work/flow/{id}?embed=1     the read-only embed     (a MODE, stays a query)
   *
   * `?id=` is still READ, deliberately: 106 flows' worth of bookmarks spell it
   * that way and the shell's `mergeSearch` now delivers it intact. The PATH is
   * what this leaf writes.
   */
  const id = pathId ?? params.get("id");
  const embed = params.get("embed") === "1";
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
      /* The PATH is the address; the query keeps everything else — `?embed=1`
         above all, which PRISM sets cross-leaf. `?id=` and `?new=` are
         actively REMOVED so one document never carries two spellings in the
         history. */
      const merged = new URLSearchParams(params);
      merged.delete("id");
      merged.delete("new");
      const q = merged.toString();
      navigate(
        (next ? `/work/flow/${encodeURIComponent(next)}` : "/work/flow") + (q ? `?${q}` : ""),
        { replace: true },
      );
    },
    [navigate, params],
  );

  // bare /flow (no id, not a new-flow request, not embed) → gallery landing
  if (!id && !isNew && !embed) {
    return (
      <div className="relative h-full w-full overflow-hidden">
        <FlowGallery />
      </div>
    );
  }

  return (
    <div className="relative h-full w-full overflow-hidden">
      <ReactFlowProvider>
        <FlowDesigner flowId={id} embed={embed} syncId={syncId} />
      </ReactFlowProvider>
    </div>
  );
}
