import { memo, useState } from "react";
import { MessageSquare, MessageSquarePlus, Trash2, Check, X } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatTime } from "@/lib/format";
import type { Intervention } from "@/types/api";

const SHORT_LIMIT = 80;

function shorten(text: string): string {
  const collapsed = text.replace(/\s+/g, " ").trim();
  if (collapsed.length <= SHORT_LIMIT) return collapsed;
  return collapsed.slice(0, SHORT_LIMIT - 1).trimEnd() + "…";
}

/**
 * Inline node in the pipeline view representing a user prompt — either
 * the original task description (`kind="initial"`) or a continuation /
 * retry instruction. Click opens a modal with the full untruncated text.
 *
 * Rendered in the same vertical column as SubtaskCards but visually
 * distinct (dashed border, no role/model meta) so it reads as a
 * different node type at a glance.
 */
export const InterventionCard = memo(function InterventionCard({
  intervention,
  onDelete,
}: {
  intervention: Intervention;
  /**
   * When supplied, a hover-revealed trash button appears on the card.
   * Only wired by the pipeline for *trailing* (queued, not-yet-started)
   * continuations — the only kind the BE permits deleting. Two-step:
   * the first click arms a confirm row, the second fires onDelete.
   */
  onDelete?: (interventionId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const isInitial = intervention.kind === "initial";
  const Icon = isInitial ? MessageSquare : MessageSquarePlus;
  const label = isInitial ? "PROMPT" : "INTERVENTION";

  return (
    <>
      <div className="group relative">
      <button
        type="button"
        onClick={() => setOpen(true)}
        // The connection-svg overlay finds nodes via `data-subtask-id` —
        // interventions piggyback on that attribute so the same renderer
        // can route bezier paths through them, keeping the prompt cards
        // wired into the flow chart instead of floating above the lines.
        // Intervention ids are namespaced with the "int-" prefix so they
        // never collide with subtask ids like "1.1".
        data-subtask-id={intervention.id}
        className="relative flex min-w-[180px] max-w-[260px] flex-col gap-1 overflow-hidden rounded border border-dashed border-fg-muted/50 bg-surface px-3 py-2 text-left transition-colors hover:border-accent/70 hover:bg-surface-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
        aria-label={`Show full ${label.toLowerCase()} text`}
      >
        <div className="flex items-center gap-2">
          <Icon className="h-3 w-3 shrink-0 text-fg-muted" aria-hidden="true" />
          <span className="text-3xs font-bold uppercase tracking-wider text-fg-muted">
            {label}
          </span>
          {intervention.ts && (
            <span className="ml-auto text-3xs text-tertiary">
              {formatTime(intervention.ts)}
            </span>
          )}
        </div>
        <p className="text-xs text-fg-muted">{shorten(intervention.text)}</p>
      </button>

      {onDelete &&
        (confirming ? (
          <div className="absolute -right-1 -top-1 z-20 flex items-center gap-0.5 rounded border border-border bg-surface-elevated px-0.5 py-0.5 shadow-sm">
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
                onDelete(intervention.id);
              }}
              className="rounded p-1 text-error transition-colors hover:bg-error/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-error/50"
              aria-label="Confirm delete queued prompt"
            >
              <Check className="h-3 w-3" aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded p-1 text-fg-muted transition-colors hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
              aria-label="Cancel delete"
            >
              <X className="h-3 w-3" aria-hidden="true" />
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="absolute -right-1 -top-1 z-20 rounded border border-border bg-surface-elevated p-1 text-fg-muted opacity-0 shadow-sm transition-opacity hover:text-error focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-error/50 group-hover:opacity-100"
            aria-label="Delete queued prompt"
          >
            <Trash2 className="h-3 w-3" aria-hidden="true" />
          </button>
        ))}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Icon className="h-4 w-4 text-fg-muted" aria-hidden="true" />
              <span>{label}</span>
              {intervention.ts && (
                <span className="ml-2 text-2xs font-normal text-tertiary">
                  {formatTime(intervention.ts)}
                </span>
              )}
            </DialogTitle>
          </DialogHeader>
          <pre className="max-h-[60vh] whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-fg overflow-y-auto">
            {intervention.text}
          </pre>
        </DialogContent>
      </Dialog>
    </>
  );
});
