// <!-- AGENT_HEADER
// role: code
// purpose: /flow route — gallery when no flow is targeted, editor otherwise.
//   ?id= opens a flow · ?new=1 starts a blank flow · ?embed=1 read-only embed ·
//   bare /flow shows the thumbnail gallery (find / organise flows).
// AGENT_HEADER_END -->
import { ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useSearchParams } from "react-router";

import { FlowDesigner } from "@/components/flow-designer/flow-designer";
import "@/components/flow-designer/flow-designer.css";
import { FlowGallery } from "@/components/flow-designer/flow-gallery";

export function FlowPage() {
  const [params] = useSearchParams();
  const id = params.get("id");
  const embed = params.get("embed") === "1";
  const isNew = params.get("new") === "1";

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
        <FlowDesigner flowId={id} embed={embed} />
      </ReactFlowProvider>
    </div>
  );
}
