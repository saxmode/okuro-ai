// <!-- AGENT_HEADER
// role: code
// purpose: /prism route — gallery when no doc is targeted, viewer otherwise.
//   ?id= opens a doc · ?new=1 starts the generate panel · bare /prism shows
//   the tile gallery (find / open docs).
// AGENT_HEADER_END -->
import { useSearchParams } from "react-router";

import { PrismGallery } from "@/components/prism/prism-gallery";
import { PrismViewer } from "@/components/prism/prism-viewer";

export function PrismPage() {
  const [params] = useSearchParams();
  const id = params.get("id");
  const isNew = params.get("new") === "1";

  if (!id && !isNew) {
    return (
      <div className="relative h-full w-full overflow-hidden">
        <PrismGallery />
      </div>
    );
  }

  return (
    <div className="relative h-full w-full overflow-hidden">
      <PrismViewer docId={id} />
    </div>
  );
}
