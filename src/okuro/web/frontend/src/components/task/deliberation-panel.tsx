import { useEffect, useState, useMemo } from "react";
import type {
  PositionSummary,
  DiscussionState,
  DeliberationStrategy,
} from "@/types/api";
import {
  useProposedPanel,
  useConfirmPanel,
  usePositions,
  useDiscussion,
  useSubmitAssignments,
  useSubmitStatement,
  useProceed,
  useRecouncil,
  useAuthorityMap,
  useGraph,
  useCapabilityGap,
  useAcceptCapabilityGap,
  useAllRoles,
  usePromoteRole,
} from "@/hooks/use-deliberation";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { stepLabel } from "@/lib/nouns";

// -- Draft Promotion List (R5) --
//
// After Phase 0 emits role drafts (role-designer persists with
// maturity='draft'), this list surfaces them with promote buttons. A
// draft is invisible to the panel proposer until promoted to 'active'.
// Promoting also invalidates the proposed-panel cache so the next
// PanelProposal render sees the freshly matched roles.

function DraftPromotionList({ taskId }: { taskId: string }) {
  const { data: roles } = useAllRoles();
  const promote = usePromoteRole(taskId);

  const drafts = (roles ?? []).filter((r) => r.maturity === "draft");

  if (drafts.length === 0) {
    return (
      <div className="space-y-2 rounded border border-success/50 bg-success-subtle/50 p-3">
        <div className="text-2xs uppercase tracking-wider text-success">
          {stepLabel(0)} complete — no drafts pending
        </div>
        <p className="text-xs text-fg-muted">
          Refresh the panel below to see freshly matched roles.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2 rounded border border-success/40 bg-success-subtle/40 p-3">
      <div className="text-2xs uppercase tracking-wider text-success">
        {stepLabel(0)} complete — promote drafts to activate
      </div>
      <p className="text-3xs text-tertiary">
        Drafts are invisible to the panel proposer. Promote each to
        ``active'' to make it available for the panel.
      </p>
      <ul className="space-y-1.5">
        {drafts.map((r) => (
          <li
            key={r.id}
            className="flex items-center justify-between gap-2 rounded border border-border bg-surface p-2"
          >
            <div className="min-w-0 flex-1">
              <div className="font-mono text-xs text-fg">{r.id}</div>
              <div className="truncate text-3xs text-tertiary">
                {r.domain} · {r.description || "no description"}
              </div>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => promote.mutate(r.id)}
              disabled={promote.isPending}
            >
              {promote.isPending && promote.variables === r.id
                ? "…"
                : "Promote"}
            </Button>
          </li>
        ))}
      </ul>
      {promote.isError && (
        <p className="text-3xs text-error">
          Promote failed: {String((promote.error as Error)?.message ?? "")}
        </p>
      )}
    </div>
  );
}


// -- Role Creation Hero --
//
// Visually dominant card surfaced while Phase 0 (role-researcher →
// role-designer) is running. Replaces the previous gray "Phase 0
// running" line item that blended into the background. Goals:
//   - Make it obvious the orchestrator is creating roles, not the task.
//   - Reveal each Phase 0 step's live status (running / done / pending).
//   - Surface drafted role names as soon as role-designer writes them.
//   - Block proceeding visually: the parent omits PanelProposal while
//     this hero is shown.

function RoleCreationHero({
  taskId,
  gap,
}: {
  taskId: string;
  gap: {
    phase0: Array<{ id: string; role: string; description: string }>;
  };
}) {
  const { data: graph } = useGraph(taskId, true);
  const { data: roles } = useAllRoles();
  const drafts = (roles ?? []).filter((r) => r.maturity === "draft");

  const stepStatus = (id: string): string => {
    const node = graph?.nodes.find((n) => n.id === id);
    return node?.status ?? "pending";
  };

  const STATUS_GLYPH: Record<string, string> = {
    running: "●",
    done: "✓",
    failed: "✗",
    pending: "○",
    approved: "○",
  };
  const STATUS_TONE: Record<string, string> = {
    running: "text-warning",
    done: "text-success",
    failed: "text-error",
    pending: "text-tertiary",
    approved: "text-tertiary",
  };

  return (
    <div
      className="space-y-4 rounded-lg border-2 border-warning/60 bg-warning-subtle/50 p-5 shadow-sm"
      data-testid="role-creation-hero"
    >
      <div className="flex items-center gap-3">
        <span
          className="inline-block h-3 w-3 animate-pulse rounded-full bg-warning"
          aria-hidden="true"
        />
        <div className="flex-1">
          <div className="text-xs font-semibold uppercase tracking-wider text-warning">
            Creating new roles for this task
          </div>
          <p className="mt-1 text-sm text-fg">
            The registry has no roles above the similarity threshold.
            Two specialists are drafting them now. The panel proposer
            will rerun against the updated registry once {stepLabel(0)} finishes.
          </p>
        </div>
      </div>

      <div>
        <div className="mb-2 text-3xs uppercase tracking-wider text-tertiary">
          {stepLabel(0)} steps
        </div>
        <ul className="space-y-1.5">
          {gap.phase0.map((step) => {
            const st = stepStatus(step.id);
            const isRunning = st === "running";
            return (
              <li
                key={step.id}
                className={`flex items-start gap-3 rounded border border-border bg-surface px-3 py-2 ${
                  isRunning ? "ring-1 ring-warning/40" : ""
                }`}
              >
                <span
                  className={`mt-0.5 w-3 text-center ${STATUS_TONE[st] ?? "text-tertiary"} ${
                    isRunning ? "animate-pulse" : ""
                  }`}
                  aria-hidden="true"
                >
                  {STATUS_GLYPH[st] ?? "○"}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-2xs font-medium uppercase tracking-wider text-fg">
                      {step.role}
                    </span>
                    <span
                      className={`text-3xs uppercase tracking-wider ${STATUS_TONE[st] ?? "text-tertiary"}`}
                    >
                      {st}
                    </span>
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-3xs text-fg-muted">
                    {step.description}
                  </p>
                </div>
              </li>
            );
          })}
        </ul>
      </div>

      <div>
        <div className="mb-2 text-3xs uppercase tracking-wider text-tertiary">
          Drafted roles ({drafts.length})
        </div>
        {drafts.length === 0 ? (
          <p className="text-3xs italic text-tertiary">
            None yet — role-designer will write drafts here as it
            completes.
          </p>
        ) : (
          <ul className="space-y-1">
            {drafts.map((r) => (
              <li
                key={r.id}
                className="flex items-center gap-2 rounded border border-border bg-surface px-2 py-1.5"
              >
                <span className="font-mono text-2xs text-fg">{r.id}</span>
                <span className="text-3xs text-tertiary">·</span>
                <span className="truncate text-3xs text-fg-muted">
                  {r.domain}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="rounded border border-warning/30 bg-warning-subtle/30 px-3 py-2 text-3xs text-fg-muted">
        Proceed is blocked until {stepLabel(0)} completes — the panel proposer
        needs the new roles in the registry before it can recommend a
        panel for this task.
      </div>
    </div>
  );
}


// -- Capability Gap Card (R3) --
//
// When the engine writes capability_gap.json (no role above similarity
// threshold), the UI renders this card INSTEAD of the regular panel.
// User approves the proposed Phase 0 plan (role-researcher → role-designer)
// which then runs to populate the role registry with new drafts. After
// Phase 0 completes (status="phase0_complete"), the proposer re-runs
// against the updated registry and the panel proposal is shown.

function CapabilityGapCard({ taskId }: { taskId: string }) {
  const { data: gap } = useCapabilityGap(taskId);
  const accept = useAcceptCapabilityGap();
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [initialized, setInitialized] = useState(false);

  if (gap && !initialized) {
    setChosen(new Set(gap.creator_roles));
    setInitialized(true);
  }

  if (!gap) return null;

  if (gap.status === "phase0_complete") {
    return <DraftPromotionList taskId={taskId} />;
  }

  if (gap.status === "accepted") {
    return <RoleCreationHero taskId={taskId} gap={gap} />;
  }

  function toggleCreator(role: string) {
    setChosen((prev) => {
      const next = new Set(prev);
      if (next.has(role)) next.delete(role);
      else next.add(role);
      return next;
    });
  }

  function handleAccept() {
    const creator_roles = gap?.creator_roles.filter((r) => chosen.has(r)) ?? [];
    if (creator_roles.length === 0) return;
    accept.mutate({
      taskId,
      creatorRoles:
        creator_roles.length === gap?.creator_roles.length
          ? undefined
          : creator_roles,
    });
  }

  return (
    <div className="space-y-3 rounded border border-warning/40 bg-warning-subtle/30 p-3">
      <div>
        <div className="text-2xs uppercase tracking-wider text-warning">
          Capability gap — {gap.kind.replace("_", " ")}
        </div>
        <p className="mt-1 text-xs text-fg-muted">{gap.summary}</p>
      </div>

      {gap.payload?.closest && gap.payload.closest.length > 0 && (
        <div className="space-y-1">
          <div className="text-3xs uppercase tracking-wider text-tertiary">
            Closest sub-threshold matches (rejected)
          </div>
          <ul className="space-y-0.5 font-mono text-3xs text-tertiary">
            {gap.payload.closest.map((c) => (
              <li key={c.id}>
                {c.id} · sim={c.similarity.toFixed(3)}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <div className="text-3xs uppercase tracking-wider text-tertiary">
          {stepLabel(0)} plan
        </div>
        <ul className="mt-1 space-y-1">
          {gap.creator_roles.map((role) => (
            <li key={role}>
              <label className="flex cursor-pointer items-center gap-2 rounded border border-border bg-surface p-2 text-xs hover:border-border-hover">
                <input
                  type="checkbox"
                  checked={chosen.has(role)}
                  onChange={() => toggleCreator(role)}
                />
                <span className="font-mono">{role}</span>
              </label>
            </li>
          ))}
        </ul>
      </div>

      <Button
        onClick={handleAccept}
        disabled={accept.isPending || chosen.size === 0}
        size="sm"
        className="w-full"
      >
        {accept.isPending ? "Accepting…" : `Accept ${stepLabel(0)} (${chosen.size})`}
      </Button>
      {accept.isError && (
        <p className="text-3xs text-error">
          Accept failed: {String((accept.error as Error)?.message ?? "")}
        </p>
      )}
    </div>
  );
}


// -- Panel Proposal --

function PanelProposal({ taskId }: { taskId: string }) {
  const { data: proposed, isLoading } = useProposedPanel(taskId);
  const confirmMutation = useConfirmPanel();
  const [selectedRoles, setSelectedRoles] = useState<Set<string>>(new Set());
  const [strategy, setStrategy] = useState<DeliberationStrategy>("parallel");

  // Stable key over the current proposed-role IDs so the sync effect
  // fires when the proposer's result changes (e.g. after promoting a
  // draft and the query refetches). Without this, the one-shot
  // initializer never re-ran and `selectedRoles` kept IDs that no
  // longer appeared in the panel — the Confirm count would lie
  // (5 cards shown, "Confirm (7 roles)").
  const proposedIdsKey = useMemo(
    () => (proposed ?? []).map((r) => r.role_id).sort().join("|"),
    [proposed],
  );

  useEffect(() => {
    if (!proposed) return;
    const currentIds = new Set(proposed.map((r) => r.role_id));
    setSelectedRoles((prev) => {
      // Drop any selected id that is no longer in the proposed set.
      // Add ids that newly appeared — defaults to "all selected" the
      // first time, and matches that default after every refetch.
      const intersected = new Set<string>();
      for (const id of prev) if (currentIds.has(id)) intersected.add(id);
      for (const id of currentIds) if (!intersected.has(id)) intersected.add(id);
      return intersected;
    });
  }, [proposedIdsKey, proposed]);

  function toggleRole(roleId: string) {
    setSelectedRoles((prev) => {
      const next = new Set(prev);
      if (next.has(roleId)) next.delete(roleId);
      else next.add(roleId);
      return next;
    });
  }

  function handleConfirm() {
    const roles = [...selectedRoles];
    if (roles.length === 0) return;
    confirmMutation.mutate({ taskId, roles, strategy });
  }

  function handleJustGo() {
    if (!proposed?.length) return;
    confirmMutation.mutate({
      taskId,
      roles: proposed.map((r) => r.role_id),
      strategy: "parallel",
    });
  }

  if (isLoading) {
    return (
      <div className="space-y-2">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-16 animate-pulse rounded bg-surface-elevated" />
        ))}
      </div>
    );
  }

  if (!proposed?.length) {
    return (
      <p className="text-center text-sm text-tertiary">
        No roles proposed for this task.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="text-2xs uppercase tracking-wider text-tertiary">
        Proposed Panel
      </div>

      {/* Role cards */}
      <div className="space-y-1.5">
        {proposed.map((role) => (
          <button
            key={role.role_id}
            onClick={() => toggleRole(role.role_id)}
            className={`w-full rounded border p-3 text-left transition-colors ${
              selectedRoles.has(role.role_id)
                ? "border-accent/50 bg-accent-subtle"
                : "border-border bg-surface hover:border-border-hover"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-2xs font-medium uppercase tracking-wider text-fg">
                {role.role_id}
              </span>
              <span className="text-3xs text-tertiary">{role.domain}</span>
            </div>
            <p className="mt-1 text-xs text-fg-muted">{role.description}</p>
            {role.why && (
              <p className="mt-0.5 text-2xs italic text-tertiary">{role.why}</p>
            )}
          </button>
        ))}
      </div>

      {/* Strategy */}
      <div className="flex items-center gap-2">
        <span className="text-2xs uppercase tracking-wider text-tertiary">
          Strategy
        </span>
        {(["parallel", "sequential", "debate"] as const).map((s) => (
          <button
            key={s}
            onClick={() => setStrategy(s)}
            className={`rounded border px-2.5 py-1 text-3xs uppercase tracking-wider transition-colors ${
              strategy === s
                ? "border-primary text-fg"
                : "border-border text-tertiary hover:text-fg-muted"
            }`}
          >
            {s}
          </button>
        ))}
      </div>

      {/* Actions */}
      <div className="flex gap-2">
        <Button
          onClick={handleConfirm}
          disabled={confirmMutation.isPending || selectedRoles.size === 0}
          className="flex-1"
          size="sm"
        >
          {confirmMutation.isPending
            ? "Confirming..."
            : `Confirm (${selectedRoles.size} roles)`}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={handleJustGo}
          disabled={confirmMutation.isPending}
        >
          Just go
        </Button>
      </div>
    </div>
  );
}

// -- Position Card --

const POS_STATUS_COLOR: Record<string, string> = {
  pending: "text-tertiary",
  running: "text-warning",
  done: "text-success",
  failed: "text-error",
  dismissed: "text-tertiary",
};

function PositionCard({
  position,
  settled,
}: {
  position: PositionSummary;
  settled?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  // In a SETTLED (historical) deliberation — shown post-decompose inside the
  // collapsed council history — a position left at "running" is a stale graph
  // status (the engine moved on to execution without re-marking it). Display
  // it as done, not a live warning, so the panel never claims the orchestrator
  // is running deliberation when it is not.
  const displayStatus =
    settled && position.status === "running" ? "done" : position.status;

  // Section order encodes decision-making flow: claim → why → risks → next.
  const sections: Array<{ label: string; body: string | undefined; tone: string }> = [
    { label: "CLAIM", body: position.claim, tone: "text-fg" },
    { label: "REASONING", body: position.reasoning, tone: "text-fg-muted" },
    { label: "RISKS", body: position.risks, tone: "text-warning" },
    { label: "RECOMMENDATION", body: position.recommendation, tone: "text-accent" },
  ].filter((s) => s.body && s.body.trim().length > 0);

  const hasContent = sections.length > 0;

  return (
    <div className="rounded border border-border">
      <button
        onClick={() => hasContent && setExpanded(!expanded)}
        className="w-full p-3 text-left transition-colors hover:bg-surface-elevated"
      >
        <div className="flex items-center justify-between">
          <span className="text-2xs font-medium uppercase tracking-wider text-fg">
            {position.role}
          </span>
          <div className="flex items-center gap-2">
            <span className="text-3xs text-tertiary">R{position.round}</span>
            <span
              className={`text-3xs uppercase ${POS_STATUS_COLOR[displayStatus] ?? "text-tertiary"}`}
            >
              {displayStatus}
            </span>
            {hasContent && (
              <span className="text-3xs text-tertiary">{expanded ? "▾" : "▸"}</span>
            )}
          </div>
        </div>
        {position.claim && (
          <p className={`mt-2 text-xs leading-relaxed text-fg-muted ${expanded ? "" : "line-clamp-3"}`}>
            {position.claim}
          </p>
        )}
      </button>
      {expanded && hasContent && (
        <div className="space-y-4 border-t border-border bg-surface-elevated/40 p-3">
          {sections.map((s) => (
            <div key={s.label}>
              <div className={`mb-2 text-3xs font-medium uppercase tracking-wider ${s.tone}`}>
                {s.label}
              </div>
              <MarkdownContent variant="viewer">
                {s.body ?? ""}
              </MarkdownContent>
            </div>
          ))}
          {position.artifact && (
            <div className="border-t border-border pt-2 text-3xs text-tertiary">
              full artifact: {position.artifact}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function PositionList({
  taskId,
  round,
  settled,
}: {
  taskId: string;
  round?: number;
  settled?: boolean;
}) {
  const { data: positions, isLoading } = usePositions(taskId, round);

  if (isLoading) {
    return (
      <div className="space-y-1.5">
        {[1, 2].map((i) => (
          <div key={i} className="h-14 animate-pulse rounded bg-surface-elevated" />
        ))}
      </div>
    );
  }

  if (!positions?.length) {
    return <p className="py-3 text-center text-xs text-tertiary">No positions yet</p>;
  }

  const done = positions.filter((p) => p.status === "done");
  const pending = positions.filter((p) => p.status === "pending" || p.status === "running");
  const other = positions.filter((p) => !["pending", "running", "done"].includes(p.status));

  return (
    <div className="space-y-1.5">
      {[...done, ...pending, ...other].map((p) => (
        <PositionCard key={p.node_id} position={p} settled={settled} />
      ))}
    </div>
  );
}

// -- Discussion Resolver --

type AssignAction = "assign" | "acknowledge" | "dismiss";

const ACTION_LABELS: Record<AssignAction, { label: string; activeClass: string }> = {
  assign: { label: "LEADS", activeClass: "text-accent border-accent/50 bg-accent-subtle" },
  acknowledge: { label: "CONTEXT", activeClass: "text-info border-info/50 bg-info/5" },
  dismiss: { label: "DISMISS", activeClass: "text-tertiary border-disabled/50" },
};

function DiscussionResolver({
  taskId,
  discussion,
}: {
  taskId: string;
  discussion: DiscussionState;
}) {
  const [assignments, setAssignments] = useState<
    Record<string, { action: AssignAction; leads: string; reasoning: string }>
  >(() => {
    const init: Record<string, { action: AssignAction; leads: string; reasoning: string }> = {};
    for (const a of discussion.assignments) {
      init[a.role] = {
        action: a.action as AssignAction,
        leads: a.leads,
        reasoning: a.reasoning,
      };
    }
    return init;
  });
  const [statement, setStatement] = useState(discussion.user_statement);

  const submitAssignments = useSubmitAssignments();
  const submitStatement = useSubmitStatement();
  const proceedMutation = useProceed();
  const recouncilMutation = useRecouncil();

  // Default every newly-done position to action="acknowledge". Without
  // this the action chips have no initial selection; the Proceed button
  // silently disables (`allAssigned` requires action set on every done
  // position) and the user sees no feedback. Auto-default keeps the
  // button enabled by default so a click always produces a request.
  // User overrides to "assign" or "dismiss" as needed.
  useEffect(() => {
    setAssignments((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const p of discussion.positions) {
        if (p.status !== "done") continue;
        if (!next[p.role]?.action) {
          next[p.role] = {
            action: "acknowledge",
            leads: next[p.role]?.leads ?? "",
            reasoning: next[p.role]?.reasoning ?? "",
          };
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [discussion.positions]);

  const isResolved = discussion.status === "resolved";
  // Once resolved + decompose-in-flight, the assignments grid is audit
  // trail, not active editing surface. Collapse by default so the
  // decompose-running banner can dominate visual focus.
  const [auditExpanded, setAuditExpanded] = useState(false);
  const donePositions = discussion.positions.filter((p) => p.status === "done");
  const pendingCount = discussion.positions.filter(
    (p) => p.status === "pending" || p.status === "running",
  ).length;
  const allAssigned = donePositions.every((p) => assignments[p.role]?.action);
  // Roles marked `assign` MUST carry a non-empty `leads` value — the
  // backend rejects with a Pydantic 422 otherwise. We catch the same
  // condition client-side so Proceed is structurally impossible in the
  // invalid state. Per DP10: defend in depth — don't ship a UI that
  // invites the user into a guaranteed-fail submit.
  const invalidAssignRoles = donePositions
    .filter(
      (p) =>
        assignments[p.role]?.action === "assign" &&
        !(assignments[p.role]?.leads ?? "").trim(),
    )
    .map((p) => p.role);
  const hasStatement = statement.trim().length > 0;
  // Statement is OPTIONAL — assignments alone are sufficient authority.
  // The textbox stays for users who want to record their reasoning.
  // Proceed requires EVERY panelled position to be terminal — `every` on
  // an empty donePositions list is vacuously true, which lets a user
  // click Proceed before any position has finished and silently advance
  // the discussion with zero authority signal. The backend now 409s on
  // this, but block client-side too so the button reflects reality.
  const allPositionsDone =
    discussion.positions.length > 0 && pendingCount === 0;
  const canProceed =
    allPositionsDone && allAssigned && invalidAssignRoles.length === 0;

  function handleSaveAssignments() {
    const list = donePositions.map((p) => ({
      role: p.role,
      action: assignments[p.role]?.action ?? "acknowledge",
      leads: assignments[p.role]?.leads ?? "",
      reasoning: assignments[p.role]?.reasoning ?? "",
    }));
    submitAssignments.mutate({ taskId, nodeId: discussion.node_id, assignments: list });
  }

  function handleSaveStatement() {
    if (!statement.trim()) return;
    submitStatement.mutate({
      taskId,
      nodeId: discussion.node_id,
      statement: statement.trim(),
    });
  }

  return (
    <div className="rounded border border-border">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-2xs uppercase tracking-wider text-tertiary">
          Discussion
        </span>
        <div className="flex items-center gap-2">
          <span className="text-3xs text-tertiary">R{discussion.round}</span>
          <span
            className={`text-3xs uppercase ${isResolved ? "text-success" : "text-warning"}`}
          >
            {discussion.status}
          </span>
        </div>
      </div>

      {/* Decompose-in-flight banner — visible once the user has Proceeded
          and the orchestrator is calling decompose_task synchronously. The
          deliberation graph won't have execution children yet; once the LLM
          returns and phases populate, task-detail switches to pipeline view. */}
      {isResolved && (
        <div className="flex items-center gap-3 border-b border-border bg-accent-subtle/30 px-3 py-4">
          <div className="size-4 animate-spin rounded-full border-2 border-accent border-t-transparent" />
          <div className="flex flex-col">
            <span className="text-xs font-medium uppercase tracking-wider text-accent">
              Orchestrator · decomposing into steps
            </span>
            <span className="text-3xs text-tertiary">
              council resolved · LLM generating execution plan · usually 1-5 min
            </span>
          </div>
        </div>
      )}

      {/* Compact resolved summary + toggle (replaces the full audit
          surface when decompose is in flight). */}
      {isResolved && (
        <div className="flex items-center justify-between border-b border-border bg-surface/40 px-3 py-2">
          <span className="text-3xs text-tertiary">
            {donePositions.length} position
            {donePositions.length === 1 ? "" : "s"} resolved ·{" "}
            {Object.values(assignments).filter((a) => a?.action === "assign").length}{" "}
            assign ·{" "}
            {
              Object.values(assignments).filter(
                (a) => a?.action === "acknowledge",
              ).length
            }{" "}
            acknowledge
          </span>
          <button
            type="button"
            onClick={() => setAuditExpanded((v) => !v)}
            className="text-3xs text-tertiary underline-offset-2 hover:text-fg hover:underline"
          >
            {auditExpanded ? "Hide audit" : "Show audit"}
          </button>
        </div>
      )}

      {/* Role assignments — hidden once resolved unless user expands. */}
      {(!isResolved || auditExpanded) && (
      <div className="divide-y divide-border">
        {donePositions.map((p) => {
          const a = assignments[p.role];
          return (
            <div key={p.node_id} className="px-3 py-2">
              <div className="flex items-center justify-between">
                <span className="text-2xs font-medium uppercase text-fg">
                  {p.role}
                </span>
                <div className="flex gap-1">
                  {(["assign", "acknowledge", "dismiss"] as const).map((act) => {
                    const cfg = ACTION_LABELS[act];
                    const active = a?.action === act;
                    return (
                      <button
                        key={act}
                        onClick={() =>
                          !isResolved &&
                          setAssignments((prev) => ({
                            ...prev,
                            [p.role]: {
                              ...prev[p.role],
                              action: act,
                              leads: prev[p.role]?.leads ?? "",
                              reasoning: prev[p.role]?.reasoning ?? "",
                            },
                          }))
                        }
                        disabled={isResolved}
                        className={`rounded border px-2 py-0.5 text-3xs transition-colors ${
                          active
                            ? cfg.activeClass
                            : "border-border text-tertiary hover:text-tertiary"
                        }`}
                      >
                        {cfg.label}
                      </button>
                    );
                  })}
                </div>
              </div>
              {p.claim && (
                <p className="mt-1 line-clamp-2 text-2xs text-tertiary">
                  {p.claim}
                </p>
              )}
              {a?.action === "assign" && !isResolved && (
                <>
                  <input
                    value={a.leads}
                    onChange={(e) =>
                      setAssignments((prev) => ({
                        ...prev,
                        [p.role]: { ...prev[p.role]!, leads: e.target.value },
                      }))
                    }
                    placeholder="Required: domain this role leads (e.g. audio_server)"
                    aria-invalid={!a.leads.trim()}
                    className={`mt-1 w-full rounded border bg-surface px-2 py-1 text-2xs text-fg-muted placeholder:text-tertiary ${
                      a.leads.trim()
                        ? "border-border"
                        : "border-error/60 focus:border-error"
                    }`}
                  />
                  {!a.leads.trim() && (
                    <p className="mt-0.5 text-3xs text-error">
                      Required when action is{" "}
                      <span className="font-mono">assign</span>. Fill the
                      domain this role leads, or switch to{" "}
                      <span className="font-mono">acknowledge</span>.
                    </p>
                  )}
                </>
              )}
            </div>
          );
        })}
      </div>
      )}

      {/* Pending notice */}
      {pendingCount > 0 && (
        <div className="border-t border-border px-3 py-1.5 text-2xs text-warning">
          Waiting for {pendingCount} position(s)...
        </div>
      )}

      {/* Save assignments */}
      {!isResolved && (
        <div className="border-t border-border px-3 py-1.5">
          <button
            onClick={handleSaveAssignments}
            disabled={submitAssignments.isPending || !allAssigned}
            className="text-2xs text-tertiary transition-colors hover:text-fg disabled:opacity-30"
          >
            {submitAssignments.isPending ? "Saving..." : "Save assignments"}
          </button>
        </div>
      )}

      {/* User statement — hidden once resolved unless audit expanded.
          When the textbox is empty post-resolve there's nothing to read,
          so always-hide on resolved unless audit is expanded. */}
      {(!isResolved || auditExpanded) && (
        <div className="border-t border-border p-3">
          <div className="mb-1.5 text-2xs uppercase tracking-wider text-tertiary">
            Your Statement <span className="text-3xs lowercase">(optional)</span>
          </div>
          <Textarea
            value={statement}
            onChange={(e) => setStatement(e.target.value)}
            readOnly={isResolved}
            placeholder="Optional reasoning — assignments alone are enough to proceed."
            rows={3}
            className="bg-surface text-xs text-fg placeholder:text-tertiary"
          />
          {!isResolved && (
            <button
              onClick={handleSaveStatement}
              disabled={submitStatement.isPending || !hasStatement}
              className="mt-1 text-2xs text-tertiary transition-colors hover:text-fg disabled:opacity-30"
            >
              {submitStatement.isPending ? "Saving..." : "Save statement"}
            </button>
          )}
        </div>
      )}

      {/* Proceed / Re-council */}
      <div className="border-t border-border p-3">
        {/* Disabled-reason banner — make every disabled-Proceed state
            visible. Pre-fix: button silently grey, click did nothing,
            no console error, no toast. User had no idea why. */}
        {!isResolved && !canProceed && (
          <div className="mb-2 rounded border border-warning/40 bg-warning-subtle/30 px-2 py-1.5 text-3xs text-warning">
            Proceed disabled —{" "}
            {!allPositionsDone
              ? `${pendingCount} position(s) still running. Wait for completion.`
              : !allAssigned
                ? `pick an action chip (assign / acknowledge / dismiss) for every role above. Missing: ${donePositions
                    .filter((p) => !assignments[p.role]?.action)
                    .map((p) => p.role)
                    .join(", ")}.`
                : invalidAssignRoles.length > 0
                  ? `roles marked assign need a leads value: ${invalidAssignRoles.join(", ")}.`
                  : "unknown — open devtools and inspect."}
          </div>
        )}
        {invalidAssignRoles.length > 0 && !isResolved && (
          <div className="mb-2 rounded border border-error/40 bg-error-subtle/30 px-2 py-1.5 text-3xs text-error">
            {invalidAssignRoles.length} role
            {invalidAssignRoles.length === 1 ? "" : "s"} marked{" "}
            <span className="font-mono">assign</span> need a{" "}
            <span className="font-mono">leads</span> value:{" "}
            <span className="font-mono">
              {invalidAssignRoles.join(", ")}
            </span>
            . Fill the inputs above, or flip them to{" "}
            <span className="font-mono">acknowledge</span>.
          </div>
        )}
        {!isResolved && allAssigned && (
          <div className="mb-2 flex justify-end">
            <button
              type="button"
              onClick={() =>
                setAssignments((prev) => {
                  const next = { ...prev };
                  for (const p of donePositions) {
                    next[p.role] = {
                      action: "acknowledge",
                      leads: "",
                      reasoning: next[p.role]?.reasoning ?? "",
                    };
                  }
                  return next;
                })
              }
              className="text-3xs text-tertiary underline-offset-2 hover:text-fg hover:underline"
            >
              Set all to acknowledge
            </button>
          </div>
        )}
        <div className="flex gap-2">
          {!isResolved ? (
            <Button
              onClick={async () => {
                // Auto-save assignments + statement BEFORE proceed, otherwise
                // the backend rejects with 400 "No assignments set". Previous
                // behaviour relied on the user clicking two tiny "Save..." text
                // links first, which most users miss.
                try {
                  if (allAssigned) {
                    const list = donePositions.map((p) => ({
                      role: p.role,
                      action: assignments[p.role]?.action ?? "acknowledge",
                      leads: assignments[p.role]?.leads ?? "",
                      reasoning: assignments[p.role]?.reasoning ?? "",
                    }));
                    await submitAssignments.mutateAsync({
                      taskId,
                      nodeId: discussion.node_id,
                      assignments: list,
                    });
                  }
                  if (statement.trim()) {
                    await submitStatement.mutateAsync({
                      taskId,
                      nodeId: discussion.node_id,
                      statement: statement.trim(),
                    });
                  }
                  await proceedMutation.mutateAsync({
                    taskId,
                    nodeId: discussion.node_id,
                  });
                } catch (err) {
                  console.error("[deliberation] proceed chain failed:", err);
                }
              }}
              disabled={proceedMutation.isPending || !canProceed}
              className="flex-1"
              size="sm"
            >
              {proceedMutation.isPending
                ? "Resolving..."
                : submitAssignments.isPending
                  ? "Saving assignments..."
                  : submitStatement.isPending
                    ? "Saving statement..."
                    : "Proceed"}
            </Button>
          ) : (
            <Button
              variant="outline"
              onClick={() =>
                recouncilMutation.mutate({ taskId, nodeId: discussion.node_id })
              }
              disabled={recouncilMutation.isPending}
              className="flex-1"
              size="sm"
            >
              {recouncilMutation.isPending ? "Starting..." : "Re-council"}
            </Button>
          )}
        </div>

        {/* Inline error + engine-pid feedback so a click is never silent. */}
        {(() => {
          const err =
            (submitAssignments.error as Error | null) ||
            (submitStatement.error as Error | null) ||
            (proceedMutation.error as Error | null) ||
            (recouncilMutation.error as Error | null);
          if (err) {
            const msg =
              typeof err.message === "string" && err.message.trim()
                ? err.message
                : JSON.stringify(err);
            return (
              <p className="mt-2 break-words text-3xs text-error" role="alert">
                {msg}
              </p>
            );
          }
          const pid =
            (proceedMutation.data as { engine_pid?: number | null } | undefined)
              ?.engine_pid ??
            (recouncilMutation.data as { engine_pid?: number | null } | undefined)
              ?.engine_pid;
          if (proceedMutation.isSuccess || recouncilMutation.isSuccess) {
            return (
              <p className="mt-2 text-3xs text-success">
                {pid
                  ? `Engine respawned (pid ${pid}) — workers should start dispatching.`
                  : "Engine already running — work continues."}
              </p>
            );
          }
          return null;
        })()}
      </div>
    </div>
  );
}

// -- Authority Map --

function AuthorityMapView({ taskId }: { taskId: string }) {
  const { data: authority } = useAuthorityMap(taskId);
  if (!authority?.length) return null;

  return (
    <div className="rounded border border-border">
      <div className="border-b border-border px-3 py-2 text-2xs uppercase tracking-wider text-tertiary">
        Authority Map
      </div>
      <div className="divide-y divide-border">
        {authority.map((a, i) => (
          <div key={i} className="flex items-center gap-3 px-3 py-1.5">
            <span
              className={`rounded border px-1.5 py-0.5 text-3xs ${
                a.action === "assign"
                  ? "border-accent/30 text-accent"
                  : a.action === "acknowledge"
                    ? "border-info/30 text-info"
                    : "border-border text-tertiary"
              }`}
            >
              {a.action === "assign" ? "LEADS" : a.action === "acknowledge" ? "CONTEXT" : "OUT"}
            </span>
            <span className="text-2xs font-medium uppercase text-fg">
              {a.role}
            </span>
            {a.leads && (
              <span className="text-2xs text-tertiary">{a.leads}</span>
            )}
            <span className="ml-auto text-3xs text-tertiary">R{a.round}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// -- Main Panel --

type DeliberationPhase = "proposal" | "positions" | "discussion";

export function DeliberationPanel({
  taskId,
  settled,
  suppressProposal,
}: {
  taskId: string;
  settled?: boolean;
  // When a panel_confirmation blocker is active, the floating BlockerCard
  // already renders the proposed-role picker. Suppress the in-flow copy so
  // the two don't render the same panel twice (the overlay-over-inline
  // duplication that bled through on narrow viewports).
  suppressProposal?: boolean;
}) {
  const { data: graph } = useGraph(taskId, true);
  const { data: gap } = useCapabilityGap(taskId);
  const { data: allRoles } = useAllRoles();
  // Role creation is active while Phase 0 is running OR Phase 0 is
  // marked complete but drafts haven't been promoted yet. In both
  // cases the panel proposer is operating on a stale registry — do
  // not let the user confirm a sub-threshold panel by accident.
  const unpromotedDrafts = (allRoles ?? []).filter((r) => r.maturity === "draft").length;
  const roleCreationActive = !!gap && (
    gap.status === "accepted" ||
    (gap.status === "phase0_complete" && unpromotedDrafts > 0)
  );

  const phase: DeliberationPhase = useMemo(() => {
    if (!graph || graph.nodes.length === 0) return "proposal";
    const hasPositions = graph.nodes.some((n) => n.type === "position");
    const hasDiscussion = graph.nodes.some((n) => n.type === "discussion");
    if (!hasPositions) return "proposal";
    if (hasDiscussion) return "discussion";
    return "positions";
  }, [graph]);

  const activeDiscussionId = useMemo(() => {
    if (!graph) return null;
    const discussions = graph.nodes.filter(
      (n) => n.type === "discussion" && n.status !== "superseded",
    );
    return discussions[discussions.length - 1]?.id ?? null;
  }, [graph]);

  const currentRound = useMemo(() => {
    if (!graph) return 1;
    const posNodes = graph.nodes.filter((n) => n.type === "position");
    if (posNodes.length === 0) return 1;
    return Math.max(...posNodes.map((n) => n.round));
  }, [graph]);

  const { data: discussion } = useDiscussion(taskId, activeDiscussionId);

  return (
    <div className="space-y-4 p-4">
      {/* Phase indicator */}
      <div className="flex items-center gap-2 text-3xs">
        {(["proposal", "positions", "discussion"] as const).map((p, i) => (
          <div key={p} className="flex items-center gap-2">
            {i > 0 && <span className="text-tertiary">{"\u2192"}</span>}
            <span
              className={
                phase === p
                  ? "font-medium uppercase text-fg"
                  : "uppercase text-tertiary"
              }
            >
              {p}
            </span>
          </div>
        ))}
        {currentRound > 1 && (
          <span className="ml-auto text-warning">ROUND {currentRound}</span>
        )}
      </div>

      {phase === "proposal" && (
        <>
          <CapabilityGapCard taskId={taskId} />
          {!roleCreationActive && !suppressProposal && (
            <PanelProposal taskId={taskId} />
          )}
        </>
      )}

      {phase !== "proposal" && (
        <>
          <div>
            <div className="mb-2 text-2xs uppercase tracking-wider text-tertiary">
              Positions
            </div>
            <PositionList taskId={taskId} round={currentRound} settled={settled} />
          </div>

          {discussion && (
            <DiscussionResolver taskId={taskId} discussion={discussion} />
          )}

          <AuthorityMapView taskId={taskId} />
        </>
      )}
    </div>
  );
}
