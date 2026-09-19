import { useState } from "react";
import { Button } from "@/components/ui/button";
import { formatAge } from "@/lib/format";
import type {
  StructureAction,
  StructureActionState,
} from "@/types/api";

/**
 * THE ACTION CONTAINER, GROUPED BY STATE.
 *
 * What this panel exists to make visible is the DISTANCE a structural change
 * still has to travel, and who owes the next move. A flat list sorted by date
 * answers neither: three rows waiting on the owner and thirty waiting on an
 * agent look the same, and the three are the only ones he can do anything
 * about.
 *
 * So the groups are the states, in the machine's own order, and the only rows
 * carrying buttons are the ones in `critiqued` — the single point where the
 * workflow is waiting on a human. Rendering approve on a `proposed` row would
 * be offering to skip the research, the plan and the critique, which is
 * exactly the shortcut the state machine exists to prevent.
 *
 * `rejected` and `superseded` are hidden behind a toggle rather than dropped.
 * A rejected action is the record of a decision, and a panel that forgets its
 * rejections re-proposes them.
 */

/** The machine's order, which is also the reading order. */
export const ACTIVE_STATES: StructureActionState[] = [
  "proposed",
  "researched",
  "planned",
  "critiqued",
  "approved",
  "implemented",
  "verified",
];

/** Terminal, and hidden behind the same toggle. `restored` is closed rather
 *  than active because nothing moves out of it: a second attempt is a new
 *  action, which is what reusing the old approval would have avoided paying
 *  for. */
export const CLOSED_STATES: StructureActionState[] = [
  "restored",
  "rejected",
  "superseded",
];

export const STATE_LABEL: Record<StructureActionState, string> = {
  proposed: "proposed",
  researched: "researched",
  planned: "planned",
  critiqued: "critiqued — waiting on you",
  approved: "approved",
  implemented: "implemented",
  verified: "verified",
  restored: "restored — the write was reversed",
  rejected: "rejected",
  superseded: "superseded",
};

export const STATE_MEANING: Record<StructureActionState, string> = {
  proposed:
    "Opened with evidence: a verified quote from a body okuro stored, or an alarm from the poller. Nothing has looked at it yet.",
  researched:
    "A research artifact exists. What changed outside okuro has been read and written down.",
  planned: "A plan artifact exists. What okuro would change is named.",
  critiqued:
    "A critique artifact exists and the uninformed-critic pass has run. This is the only state where the workflow is waiting on you.",
  approved:
    "You approved it, and exactly one todo was created. Implement writes it: the store is snapshotted first, the roles a migration carries get a generated migration file, and the rest are written in the database.",
  implemented:
    "Written, with a snapshot and a migration id or diff artifact behind it, so there is a way back. Verify re-scores the affected roles against the baseline taken before the write.",
  verified:
    "The affected roles were re-scored under the same rubric and their structure did not go down.",
  restored:
    "The affected roles were put back from this action's own pre-write snapshot, and the columns it wrote were the columns reversed. Terminal: a second attempt is a new action.",
  rejected: "Decided against, with a reason. Terminal.",
  superseded: "Replaced by another action, with a reason. Terminal.",
};

const STATE_TONE: Record<StructureActionState, string> = {
  proposed: "text-tertiary",
  researched: "text-info",
  planned: "text-info",
  critiqued: "text-warning",
  approved: "text-success",
  implemented: "text-success",
  verified: "text-success",
  restored: "text-warning",
  rejected: "text-fg-muted",
  superseded: "text-fg-muted",
};

/**
 * WHAT THE REJECT PROMPT'S ANSWER MEANS. Three outcomes, never two.
 *
 * `window.prompt` returns null when the person dismisses it and "" when they
 * press OK on an empty box. Collapsing both with `?.trim() || "<placeholder>"`
 * sent a placeholder reason either way, so CANCEL REJECTED THE ACTION — with a
 * reason nobody wrote, on a terminal transition that cannot be undone.
 *
 * A pure function so the three cases are testable without a prompt.
 */
export type RejectAnswer =
  | { kind: "abort" }
  | { kind: "refuse"; message: string }
  | { kind: "reject"; reason: string };

export function readRejectAnswer(answer: string | null): RejectAnswer {
  if (answer === null) return { kind: "abort" };
  const reason = answer.trim();
  if (!reason) {
    return {
      kind: "refuse",
      message:
        "A rejection needs a reason. It is terminal, and a row that ends with no stated reason is a decision nobody can revisit.",
    };
  }
  return { kind: "reject", reason };
}

/**
 * WHICH ROW CARRIES WHICH BUTTON, AND WHY IT IS NOT ONE RULE.
 *
 * Three states put a control in front of a human, and they are three
 * different asks:
 *
 * - `critiqued` — approve or reject. The decision.
 * - `approved`  — implement. NOT a second decision: the pipeline refuses this
 *   from any other state, so the button carries out a decision already taken.
 *   It exists because the write should happen when the owner is looking at the
 *   row, not whenever an agent next runs.
 * - `implemented` — verify. Re-scores against the baseline taken before the
 *   write. It can come back refused, and that is a finding rather than an
 *   error, so the panel shows the sentence and leaves the row where it is.
 *
 * Every other state shows no control at all. A button on a `proposed` row
 * would be an offer to skip the research, the plan and the critique.
 */
export function actionControl(
  state: StructureActionState,
): "decide" | "implement" | "verify" | null {
  if (state === "critiqued") return "decide";
  if (state === "approved") return "implement";
  if (state === "implemented") return "verify";
  return null;
}

/** Long enough to recognise the sentence, short enough not to own the row. */
const QUOTE_CHARS = 120;

/** Enough of an artifact id to recognise, and the whole one on the clipboard. */
export function shortArtifactId(id: string | null): string {
  if (!id) return "";
  return id.replace(/-/g, "").slice(0, 8);
}

export function truncateQuote(quote: string | null): string {
  if (!quote) return "";
  const text = quote.trim();
  if (text.length <= QUOTE_CHARS) return text;
  return `${text.slice(0, QUOTE_CHARS - 1).trimEnd()}…`;
}

/**
 * A failed approve or reject, said out loud.
 *
 * The same class the roles view already fixed for its two buttons: a mutation
 * that renders only `data` leaves a 409 silent, the click looks ignored, and
 * it gets clicked again. Here the 409 carries the refusal from the state
 * machine verbatim, which is the most useful sentence on the page when it
 * appears — "the source moved since this was proposed" is an answer, not an
 * error.
 */
export function ActionError({
  error,
  onDismiss,
}: {
  error: unknown;
  onDismiss: () => void;
}) {
  const detail = error instanceof Error ? error.message : String(error);
  return (
    <div
      className="flex items-start gap-3 rounded border border-error/40 bg-error/10 px-3 py-2 text-xs text-error"
      role="alert"
      data-testid="structure-action-error"
    >
      <span className="min-w-0 flex-1">
        <span className="font-medium">The action did not move.</span> {detail}
      </span>
      <button
        type="button"
        onClick={onDismiss}
        className="shrink-0 underline"
        aria-label="Dismiss error"
      >
        dismiss
      </button>
    </div>
  );
}

export function StructureActionRow({
  action,
  onApprove,
  onReject,
  onImplement,
  onVerify,
  busy,
}: {
  action: StructureAction;
  onApprove?: (action: StructureAction) => void;
  onReject?: (action: StructureAction) => void;
  onImplement?: (action: StructureAction) => void;
  onVerify?: (action: StructureAction) => void;
  busy?: boolean;
}) {
  const [showRoles, setShowRoles] = useState(false);
  const control = actionControl(action.state);
  const roles = action.affected_role_ids ?? [];

  return (
    <li
      className="space-y-1 py-2"
      data-testid={`structure-action-${action.id}`}
      data-state={action.state}
      data-kind={action.kind}
    >
      <div className="flex items-baseline gap-2 text-2xs">
        <span className="min-w-0 flex-1 truncate text-fg" title={action.title}>
          {action.title}
        </span>
        {action.source_id ? (
          <span
            className="shrink-0 text-tertiary"
            title={action.evidence_url ?? undefined}
          >
            {action.source_id}
          </span>
        ) : null}
        <span
          className="shrink-0 text-fg-muted"
          title={
            action.created_at
              ? `Opened ${action.created_at}`
              : "No creation timestamp recorded."
          }
        >
          {action.created_at ? formatAge(action.created_at) : "—"}
        </span>
      </div>

      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-2xs text-fg-muted">
        {action.okuro_element ? (
          <span title="The one okuro element this bears on. A finding that names none is knowledge, not structure.">
            element <span className="text-fg">{action.okuro_element}</span>
          </span>
        ) : null}

        {/* The count is a BUTTON, not a label. A count on its own is the claim
            shape this whole workstream exists to stop, so the list it stands
            for is always one click away. */}
        <button
          type="button"
          onClick={() => setShowRoles((s) => !s)}
          className="underline decoration-dotted underline-offset-2 hover:text-fg"
          title="Show the explicit role ids. This is never stored as a count."
          data-testid={`structure-action-roles-toggle-${action.id}`}
        >
          {roles.length} affected role{roles.length === 1 ? "" : "s"}
        </button>

        {!action.quote_verified && action.kind === "finding" ? (
          <span className="text-error" title="This row has no verified quote behind it.">
            quote unverified
          </span>
        ) : null}
      </div>

      {showRoles ? (
        <p
          className="text-2xs text-fg"
          data-testid={`structure-action-roles-${action.id}`}
        >
          {roles.length ? roles.join(", ") : "No roles named yet."}
        </p>
      ) : null}

      {action.last_refusal ? (
        /* A refused approval MOVES the row back rather than leaving it where
           it was, so without this line the panel shows a row sitting in
           `researched` for a reason only the event log holds — and the reader
           cannot tell it apart from one somebody put there deliberately. */
        <p
          className="text-2xs text-warning"
          data-testid={`structure-action-refusal-${action.id}`}
        >
          {action.last_refusal}
        </p>
      ) : null}

      {action.quoted_sentence ? (
        <p
          className="border-l-2 border-border/60 pl-2 text-2xs italic text-fg-muted"
          title={action.quoted_sentence}
        >
          “{truncateQuote(action.quoted_sentence)}”
        </p>
      ) : null}

      {/* THE DIFF, ON THE ROW THAT IS WAITING FOR A DECISION.
          Approving an idea and approving a diff are different acts, and only
          the second can be wrong in a way anybody notices. The reference is a
          copyable id rather than an anchor because this SPA has no address
          that opens one artifact — a link that 404s would be worse than the
          id, which is exactly what `artifact_get` takes. */}
      {action.diff_artifact_id ? (
        <p className="text-2xs text-fg-muted">
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard?.writeText(action.diff_artifact_id ?? "");
            }}
            className="underline decoration-dotted underline-offset-2 hover:text-fg"
            title={`Dry-run diff: artifact ${action.diff_artifact_id}. Click to copy the id, then open it with artifact_get.`}
            data-testid={`structure-action-diff-${action.id}`}
          >
            dry-run diff {shortArtifactId(action.diff_artifact_id)}
          </button>
          {control === "decide" ? (
            <span className="text-tertiary">
              {" "}
              — read this before approving; it is the change, not the idea.
            </span>
          ) : null}
        </p>
      ) : control === "decide" ? (
        <p className="text-2xs text-warning" data-testid={`structure-action-nodiff-${action.id}`}>
          No dry-run diff on this row yet. Approving now approves the idea.
        </p>
      ) : null}

      {control === "decide" ? (
        <div className="flex items-center gap-2 pt-0.5">
          <Button
            size="sm"
            variant="default"
            className="h-6 text-2xs"
            disabled={busy}
            onClick={() => onApprove?.(action)}
            data-testid={`structure-action-approve-${action.id}`}
          >
            Approve
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-6 text-2xs"
            disabled={busy}
            onClick={() => onReject?.(action)}
            data-testid={`structure-action-reject-${action.id}`}
          >
            Reject
          </Button>
          <span className="text-2xs text-tertiary">
            Approving creates exactly one todo.
          </span>
        </div>
      ) : null}

      {control === "implement" ? (
        <div className="flex items-center gap-2 pt-0.5">
          <Button
            size="sm"
            variant="default"
            className="h-6 text-2xs"
            disabled={busy}
            onClick={() => onImplement?.(action)}
            data-testid={`structure-action-implement-${action.id}`}
          >
            Implement
          </Button>
          <span className="text-2xs text-tertiary">
            Snapshots the store first, then writes. The generated migration is
            left in the worktree for review.
          </span>
        </div>
      ) : null}

      {control === "verify" ? (
        <div className="flex items-center gap-2 pt-0.5">
          <Button
            size="sm"
            variant="outline"
            className="h-6 text-2xs"
            disabled={busy}
            onClick={() => onVerify?.(action)}
            data-testid={`structure-action-verify-${action.id}`}
          >
            Verify
          </Button>
          <span className="text-2xs text-tertiary">
            Re-scores against the baseline taken before the write. You supply
            no numbers.
          </span>
          {action.migration_id ? (
            <span
              className="text-2xs text-info"
              title="The generated migration file, waiting in the worktree."
            >
              {action.migration_id}
            </span>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

export function StructureActionsPanel({
  actions,
  byState,
  onApprove,
  onReject,
  onImplement,
  onVerify,
  busyId,
  error,
  onDismissError,
}: {
  actions: StructureAction[];
  byState?: Partial<Record<StructureActionState, number>>;
  onApprove?: (action: StructureAction) => void;
  onReject?: (action: StructureAction) => void;
  onImplement?: (action: StructureAction) => void;
  onVerify?: (action: StructureAction) => void;
  busyId?: string | null;
  error?: unknown;
  onDismissError?: () => void;
}) {
  const [showClosed, setShowClosed] = useState(false);

  const closedCount = actions.filter((a) =>
    CLOSED_STATES.includes(a.state),
  ).length;
  // Three states now put a control in front of a person, not one. Counting
  // only `critiqued` would report "0 waiting on you" over a row with an
  // Implement button on it — the panel's whole job is to say who owes the
  // next move, and that number was the answer to a narrower question.
  const waiting = actions.filter((a) => actionControl(a.state) !== null).length;

  const visibleStates = showClosed
    ? [...ACTIVE_STATES, ...CLOSED_STATES]
    : ACTIVE_STATES;

  if (!actions.length) {
    return (
      <div className="space-y-1" data-testid="structure-actions-panel">
        <p
          className="type-small text-fg-muted"
          data-testid="structure-actions-empty"
        >
          No structure actions. A run that finds nothing produces none, and that
          is the expected outcome in a week where nothing moved.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2" data-testid="structure-actions-panel">
      <p className="type-small text-fg-muted">
        {actions.length} structure action{actions.length === 1 ? "" : "s"}
        {waiting ? (
          <>
            {" · "}
            <span
              className="text-warning"
              title="Critiqued rows are the only ones waiting on a human decision."
            >
              {waiting} waiting on you
            </span>
          </>
        ) : null}
        {closedCount ? (
          <>
            {" · "}
            <button
              type="button"
              onClick={() => setShowClosed((s) => !s)}
              className="underline decoration-dotted underline-offset-2 hover:text-fg"
              data-testid="structure-actions-closed-toggle"
              title="Rejected and superseded rows are the record of a decision. A panel that forgets them re-proposes them."
            >
              {showClosed ? "hide" : "show"} {closedCount} closed
            </button>
          </>
        ) : null}
      </p>

      {error != null ? (
        <ActionError error={error} onDismiss={() => onDismissError?.()} />
      ) : null}

      {visibleStates.map((state) => {
        const rows = actions.filter((a) => a.state === state);
        if (!rows.length) return null;
        return (
          <section key={state} data-testid={`structure-actions-group-${state}`}>
            <p
              className={`text-2xs case-label tracking-wider ${STATE_TONE[state]}`}
              title={STATE_MEANING[state]}
            >
              {STATE_LABEL[state]} ({byState?.[state] ?? rows.length})
            </p>
            <ul className="divide-y divide-border/40">
              {rows.map((action) => (
                <StructureActionRow
                  key={action.id}
                  action={action}
                  onApprove={onApprove}
                  onReject={onReject}
                  onImplement={onImplement}
                  onVerify={onVerify}
                  busy={busyId === action.id}
                />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
