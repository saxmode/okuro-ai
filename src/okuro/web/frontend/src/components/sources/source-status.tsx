import type { ReactNode } from "react";
import { CircleAlert, CircleCheck, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";

/**
 * THE TWO PRIMITIVES REPOS AND CORPORA WERE KEEPING TWO COPIES OF.
 *
 * Q10/Q-R2, ruled OPTION A: the two leaves stay two leaves and share their
 * PRIMITIVES — not one `<SourceRegistry>` (that is its own pass) and not one
 * merged leaf (the IA is ruled at seven KNOW leaves).
 *
 * WHAT WAS DUPLICATED, measured rather than asserted: `StatusBadge` at
 * `repos.tsx:263-281` and `corpora.tsx:185-203` were byte-identical apart from
 * the type annotation (`Repo["status"]` vs `Corpus["status"]`) — same three
 * branches, same tints, same icons. p3 filed it as REPOS' D4 and CORPORA's P3.
 *
 * WHY NOT `ui/status-badge.tsx`, WHICH ALREADY EXISTS: it is a different
 * design and it has its own consumers. That one is a tone-coded TEXT label
 * with an optional dot and no chrome; these rows show a filled `Badge` with a
 * state icon, and the spinner is what tells you a sync is running. Folding one
 * into the other would restyle both surfaces from inside a parity pass, which
 * is the opposite of what a parity pass is for. Three implementations become
 * two, and the two that remain are two different components rather than one
 * component written twice.
 */

/** Every status either leaf's backend reports; anything else reads as running. */
type SourceStatus = string;

export function SourceStatusBadge({ status }: { status: SourceStatus }) {
  if (status === "ready")
    return (
      <Badge className="gap-1 bg-success/15 text-success">
        <CircleCheck className="size-3" /> ready
      </Badge>
    );
  if (status === "error")
    return (
      <Badge className="gap-1 bg-error/15 text-error">
        <CircleAlert className="size-3" /> error
      </Badge>
    );
  return (
    <Badge className="gap-1 bg-warning/15 text-warning">
      <Loader2 className="size-3 animate-spin" /> {status}
    </Badge>
  );
}

/**
 * What the last sync moved.
 *
 * SILENCE ON "NOTHING CHANGED" WOULD READ AS A FAILED SYNC, so an all-unchanged
 * run says so explicitly. That sentence is CORPORA's own comment, carried here
 * on purpose: it is the one behaviour in either leaf that a rebuild drops
 * silently, and it is now in the shared component rather than in one of two
 * copies.
 *
 * The per-leaf DATA stays per leaf — REPOS counts commits and file deltas,
 * CORPORA counts added/updated/removed — so each page builds its own `parts`
 * and this holds the shape, the spacing and the ink tier.
 *
 * `text-fg-muted` and not `text-muted-foreground/70`: an ink tier expressed as
 * an alpha over another ink tier (D1, memory 6ba4d789), and it was the same
 * line in both files (`repos.tsx:233`, `corpora.tsx:167`).
 */
export function ChangeSummary({
  parts,
  idleLabel,
}: {
  parts: ReactNode[];
  /** What to say when nothing moved. Never empty — see the note above. */
  idleLabel: ReactNode;
}) {
  if (parts.length === 0)
    return <span className="text-fg-muted">{idleLabel}</span>;
  return <span className="inline-flex items-center gap-1.5">{parts}</span>;
}
