// <!-- AGENT_HEADER
// role: code
// purpose: /workflows route — gallery when no workflow is targeted, editor
//   otherwise. ?id= opens one · ?new=1 starts a blank one.
// AGENT_HEADER_END -->
import { ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useSearchParams } from "react-router";

// The canvas chrome, tokens and side-panel layout come from okuro-flow's
// stylesheet: this surface is a DIFFERENT tool over the same components, so it
// shares the look without sharing a document store or any node semantics.
import "@/components/flow-designer/flow-designer.css";
import "@/components/workflow-designer/workflow-designer.css";
import { WorkflowDesigner } from "@/components/workflow-designer/workflow-designer";
import { WorkflowGallery } from "@/components/workflow-designer/workflow-gallery";

export function WorkflowsPage() {
  const [params] = useSearchParams();
  const id = params.get("id");
  const isNew = params.get("new") === "1";

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
        <WorkflowDesigner workflowId={id} />
      </ReactFlowProvider>
    </div>
  );
}
