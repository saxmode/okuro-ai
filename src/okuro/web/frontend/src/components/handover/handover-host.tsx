import { useEffect, useState } from "react";

import { HandoverDialog } from "@/components/handover/handover-dialog";
import { onOpenHandover } from "@/lib/handover-context";
import type { ContentIR } from "@/lib/handover-api";

/** One shared hand-over dialog for the whole app, opened via the handover bus
 *  (Cmd+K → "Hand over"). Mount once at the shell root. */
export function HandoverHost() {
  const [content, setContent] = useState<ContentIR | null>(null);
  useEffect(() => onOpenHandover(setContent), []);
  return (
    <HandoverDialog
      open={!!content}
      onOpenChange={(o) => !o && setContent(null)}
      content={content}
    />
  );
}
