/**
 * FlowFeedbackCard — the F3 post-flow rating widget.
 *
 * Renders on the task-detail page only when the flow has reached a terminal
 * state. The user gives a usability score (1..5), an outcome classification,
 * and an optional free-text note "for the next flow to consider". The note is
 * injected into the NEXT flow's plan prompt (decomposer), and the rating
 * back-labels every autopilot_decision the flow emitted (F3 learning loop).
 *
 * A flow already rated pre-fills its prior values; submitting again REPLACEs
 * the row (one feedback per task, server-side ON CONFLICT).
 *
 * DISCLOSURE MODEL — collapsed by default, stars only. The stars ARE the
 * status readout: an unrated flow shows dim outline stars + a "Rate this flow"
 * prompt, a rated flow shows accent-filled stars + its outcome + a check. So
 * the overview answers "did I already review this?" without opening anything.
 * Clicking a star both sets that score AND expands the detail form (one
 * gesture, no separate "expand" affordance). Escape or a click outside
 * dismisses — with an explicit no-silent-loss rule, see `attemptCollapse`.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Star, Send, Check, AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/components/ui/toast";
import { taskApi, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { FlowFeedback, OutcomeClass } from "@/types/api";

interface FlowFeedbackCardProps {
  taskId: string;
}

const OUTCOME_OPTIONS: { value: OutcomeClass; label: string }[] = [
  { value: "success", label: "Success — did what was asked" },
  { value: "partial", label: "Partial — some value, gaps remain" },
  { value: "failed", label: "Failed — did not deliver" },
  { value: "off_track", label: "Off track — solved the wrong problem" },
  { value: "needs_rework", label: "Needs rework — must be redone" },
];

/** Collapsed-row wording — the long <select> labels are too heavy inline. */
const OUTCOME_SHORT: Record<OutcomeClass, string> = {
  success: "success",
  partial: "partial",
  failed: "failed",
  off_track: "off track",
  needs_rework: "needs rework",
};

/**
 * The 1..5 star control — shared by the collapsed row and the expanded form so
 * the score never renders two different ways.
 */
function StarRow({
  value,
  rated,
  expanded,
  draft = false,
  onSelect,
}: {
  value: number;
  rated: boolean;
  expanded: boolean;
  /** Score is an UNSAVED draft — render hollow so it cannot read as stored. */
  draft?: boolean;
  onSelect: (n: number) => void;
}) {
  const [hover, setHover] = useState(0);
  const shown = hover || value;

  return (
    <div className="flex items-center gap-0.5" onMouseLeave={() => setHover(0)}>
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          aria-label={`${n} star${n > 1 ? "s" : ""}`}
          aria-expanded={expanded}
          onMouseEnter={() => setHover(n)}
          onClick={() => onSelect(n)}
          className="rounded p-0.5 outline-none focus:ring-1 focus:ring-accent"
        >
          <Star
            className={cn(
              "h-4 w-4 transition-colors",
              n <= shown
                ? // Filled == STORED. A draft is outlined in the accent colour
                  // instead: still visible and still clickable to restore, but
                  // never mistakable for a rating that was actually saved.
                  draft
                  ? "text-accent"
                  : "fill-accent text-accent"
                : // Unrated reads as a faint prompt, not as a 1-star verdict.
                  rated
                  ? "text-tertiary hover:text-fg-muted"
                  : "text-tertiary/40 hover:text-fg-muted",
            )}
          />
        </button>
      ))}
    </div>
  );
}

export function FlowFeedbackCard({ taskId }: FlowFeedbackCardProps) {
  const queryClient = useQueryClient();

  // Load any existing rating. A 404 means "unrated" — not an error.
  const { data: existing } = useQuery({
    queryKey: ["flowFeedback", taskId],
    queryFn: async (): Promise<FlowFeedback | null> => {
      try {
        return await taskApi.getFeedback(taskId);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    staleTime: 30_000,
  });

  const [usability, setUsability] = useState(0);
  const [outcome, setOutcome] = useState<OutcomeClass | "">("");
  const [comment, setComment] = useState("");
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState(false);
  // A note the user typed that could NOT be persisted yet (no outcome chosen).
  // Surfaced on the collapsed row so the loss is never silent.
  const [unsavedNote, setUnsavedNote] = useState(false);
  // Radix <Select> portals its listbox outside this card's DOM subtree, so an
  // option click looks like an "outside click". Suppress dismissal while open.
  const [selectOpen, setSelectOpen] = useState(false);

  const rootRef = useRef<HTMLDivElement>(null);
  // Which task the current form state belongs to. NOT a bare boolean: this
  // component is rendered from a route that only changes its `taskId` param,
  // and React Router reuses the element rather than remounting it. A
  // hydrated-once boolean therefore survives the navigation, so task B would
  // render task A's score — and an autosave on dismiss would write A's rating
  // to B's row, because `persist` reads taskId from props but the score from
  // state. Keying the guard on the id makes the reset automatic.
  const hydratedFor = useRef<string | null>(null);

  // Reset whenever the card is pointed at a different task, so no draft, score
  // or "unsaved" marker can bleed across tasks.
  useEffect(() => {
    if (hydratedFor.current !== null && hydratedFor.current !== taskId) {
      hydratedFor.current = null;
      setUsability(0);
      setOutcome("");
      setComment("");
      setUnsavedNote(false);
      setExpanded(false);
    }
  }, [taskId]);

  // Hydrate the form from a prior rating — once per task. A later refetch must
  // not clobber a draft the user is in the middle of typing.
  useEffect(() => {
    if (existing && hydratedFor.current !== taskId) {
      hydratedFor.current = taskId;
      setUsability(existing.usability);
      setOutcome(existing.outcome_class);
      setComment(existing.comment ?? "");
    }
  }, [existing, taskId]);

  const canSubmit = usability >= 1 && usability <= 5 && outcome !== "" && !saving;
  const dirty =
    usability !== (existing?.usability ?? 0) ||
    outcome !== (existing?.outcome_class ?? "") ||
    comment.trim() !== (existing?.comment ?? "");

  const persist = useCallback(async (): Promise<boolean> => {
    if (usability < 1 || outcome === "") return false;
    setSaving(true);
    try {
      await taskApi.submitFeedback(taskId, {
        usability,
        outcome_class: outcome,
        comment: comment.trim() || null,
      });
      await queryClient.invalidateQueries({ queryKey: ["flowFeedback", taskId] });
      setUnsavedNote(false);
      return true;
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to save rating");
      return false;
    } finally {
      setSaving(false);
    }
  }, [taskId, usability, outcome, comment, queryClient]);

  const handleSubmit = async () => {
    if (!canSubmit) return; // canSubmit already narrows outcome to OutcomeClass
    const ok = await persist();
    if (ok) {
      toast.success(existing ? "Rating updated" : "Thanks — rating saved");
      setExpanded(false);
    }
  };

  /**
   * Dismiss (Escape / outside click). No-silent-loss policy:
   *
   *  - Nothing changed        → just collapse.
   *  - Changed AND persistable → AUTOSAVE, then collapse. A dismiss gesture on
   *    a one-row-per-task store is unambiguous: there is no draft/publish
   *    distinction to preserve and re-rating simply REPLACEs, so writing costs
   *    the user nothing and warning would be pure friction.
   *  - Changed but NOT persistable (a note typed with no outcome picked — the
   *    schema makes outcome_class NOT NULL, migration 108:43, so this cannot be
   *    written) → collapse, KEEP the draft in state, and flag "unsaved note" on
   *    the collapsed row. A modal warning on every stray click would punish the
   *    dismiss gesture; a persistent visible marker keeps the loss non-silent
   *    and re-expanding restores the text verbatim.
   */
  const attemptCollapse = useCallback(async () => {
    if (saving) return;
    if (dirty) {
      if (usability >= 1 && outcome !== "") {
        const ok = await persist();
        if (ok) toast.success("Rating saved");
        else return; // save failed — stay open so the user keeps their input
      } else if (comment.trim()) {
        setUnsavedNote(true);
      }
    }
    setExpanded(false);
  }, [saving, dirty, usability, outcome, comment, persist]);

  // Outside click + Escape dismiss. Only bound while expanded.
  useEffect(() => {
    if (!expanded) return;

    const onPointerDown = (e: PointerEvent) => {
      if (selectOpen) return;
      const target = e.target as HTMLElement | null;
      if (!target) return;
      if (rootRef.current?.contains(target)) return;
      // Radix portals (select listbox, toasts) render outside the card.
      if (target.closest("[data-radix-popper-content-wrapper]")) return;
      void attemptCollapse();
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (selectOpen) return; // first Escape belongs to the open listbox
      void attemptCollapse();
    };

    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [expanded, selectOpen, attemptCollapse]);

  const handleStar = (n: number) => {
    setUsability(n);
    setExpanded(true);
  };

  // ---- Collapsed: stars only. Rated and unrated must not look alike. ----
  if (!expanded) {
    // Picking a star and dismissing without an outcome saves nothing
    // (outcome_class is NOT NULL), so that score must not render like a stored
    // rating — it would invert the one question this row exists to answer.
    // But hiding it is worse: the stars are the only affordance here, so
    // re-expanding would mean clicking a star, which overwrites the very draft
    // being restored. So the draft is SHOWN and rendered hollow instead.
    const draftOnly = !existing && usability > 0;
    return (
      <div ref={rootRef} className="flex items-center gap-2 py-1">
        <StarRow
          value={usability}
          rated={!!existing}
          expanded={false}
          draft={draftOnly}
          onSelect={handleStar}
        />
        {existing ? (
          <span className="flex items-center gap-1 text-3xs text-tertiary">
            <Check className="h-3 w-3 text-accent" />
            <span>rated</span>
            <span className="text-fg-muted">
              · {OUTCOME_SHORT[existing.outcome_class]}
            </span>
          </span>
        ) : (
          <span className="text-3xs uppercase tracking-wider text-tertiary/70">
            Rate this flow
          </span>
        )}
        {/* One marker, one message. A score + outcome always autosaves on
            dismiss, so the ONLY way anything stays unsaved is a missing
            outcome — naming that is more useful than "unsaved note", because
            it says what to do about it. */}
        {(unsavedNote || draftOnly) && (
          <span className="flex items-center gap-1 text-3xs text-warning">
            <AlertCircle className="h-3 w-3" />
            unsaved note — pick an outcome
          </span>
        )}
      </div>
    );
  }

  // ---- Expanded: the full form. ----
  return (
    <div
      ref={rootRef}
      className="flex flex-col gap-3 rounded border border-border bg-surface-elevated/20 p-3"
    >
      <div className="flex items-center justify-between">
        <span className="text-3xs uppercase tracking-wider text-tertiary">
          Rate this flow
        </span>
        {existing && (
          <span className="flex items-center gap-1 text-3xs text-tertiary">
            <Check className="h-3 w-3 text-accent" /> rated
          </span>
        )}
      </div>

      {/* Usability — 1..5 stars */}
      <div className="flex flex-col gap-1">
        <span className="text-2xs text-fg-muted">Usability</span>
        <StarRow
          value={usability}
          rated={!!existing}
          expanded
          onSelect={setUsability}
        />
      </div>

      {/* Outcome classification */}
      <div className="flex flex-col gap-1">
        <span className="text-2xs text-fg-muted">Outcome</span>
        <Select
          value={outcome || undefined}
          onValueChange={(v) => setOutcome(v as OutcomeClass)}
          onOpenChange={setSelectOpen}
        >
          <SelectTrigger className="h-7 w-full border-border bg-surface text-2xs text-fg">
            <SelectValue placeholder="How did it go?" />
          </SelectTrigger>
          <SelectContent>
            {OUTCOME_OPTIONS.map((o) => (
              <SelectItem key={o.value} value={o.value} className="text-2xs">
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Forward comment */}
      <div className="flex flex-col gap-1">
        <span className="text-2xs text-fg-muted">
          For the next flow to consider{" "}
          <span className="text-tertiary">(optional)</span>
        </span>
        <Textarea
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="e.g. keep it serial, split the UI step earlier…"
          rows={2}
          className="resize-none border-border bg-surface text-2xs"
        />
      </div>

      <div className="flex justify-end">
        <Button
          size="sm"
          disabled={!canSubmit}
          onClick={handleSubmit}
          className="h-7 gap-1 text-2xs"
        >
          <Send className="h-3 w-3" />
          {saving ? "Saving…" : existing ? "Update rating" : "Submit rating"}
        </Button>
      </div>
    </div>
  );
}
