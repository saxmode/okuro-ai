import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Plus, RefreshCw, Radar, Search, X } from "lucide-react";
import { roleApi } from "@/lib/api";
import { formatAge } from "@/lib/format";
import type {
  RoleInfo,
  StructureAction,
  StructureActionImplemented,
  StructureActionVerified,
} from "@/types/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RoleBrowser } from "@/components/role/role-browser";
import { RoleViewer } from "@/components/role/role-viewer";
import {
  SEGMENT_LABEL,
  SEGMENT_MEANING,
  SEGMENT_ORDER,
  formatScore,
  segmentTextTone,
} from "@/components/role/role-fit";
import { CreateRoleDialog } from "@/components/role/create-role-dialog";
import { StructureSourcesPanel } from "@/components/role/structure-sources";
import {
  StructureActionsPanel,
  readRejectAnswer,
} from "@/components/role/structure-actions";

/**
 * A failed mutation, said out loud.
 *
 * react-query hands back `error` and the two mutations on this view rendered
 * only `data`, so a 500 from a failed poll or a 409 from a run already going
 * left the button silent and the click looking ignored — which is how it gets
 * clicked again. One strip, both mutations, because this is a property of the
 * view rather than of either button.
 */
function MutationError({
  label,
  error,
  onDismiss,
}: {
  label: string;
  error: unknown;
  onDismiss: () => void;
}) {
  const detail = error instanceof Error ? error.message : String(error);
  return (
    <div
      className="flex items-start gap-3 rounded border border-error/40 bg-error/10 px-3 py-2 text-xs text-error"
      role="alert"
      data-testid="roles-mutation-error"
    >
      <span className="min-w-0 flex-1">
        <span className="font-medium">{label}.</span> {detail}
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

/**
 * THE ROLES VIEW OF WORK/AGENTS — a VIEW, not a page, since the AGENTS pass.
 *
 * It was `RolesPage`, a top-level page with its own `.page-shell` and its own
 * `PageHeader`, mounted inside a tab. That is why `?tab=roles` measured THREE
 * headings and `.page-shell` counted 2, nested. A view gives up the wrapper
 * and the title; its rich summary line (roles · stale · knowledge entries ·
 * last maintenance) and its two buttons keep the same row, which is where the
 * header had them.
 *
 * `openRoleId` is the leaf's `id` segment, so `/work/agents/<roleId>` opens
 * that role's viewer on a COLD load. `LeafView` has passed an id since p2 and
 * nothing consumed it here, so 103 roles had no address.
 */
export function RolesView({ openRoleId }: { openRoleId?: string } = {}) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [staleOnly, setStaleOnly] = useState(false);
  const [domainFilter, setDomainFilter] = useState<string | null>(null);
  const [selectedRole, setSelectedRole] = useState<string | undefined>();
  /* Seeded from the leaf's `id` segment, so `/work/agents/<roleId>` opens that
     role's viewer on a COLD load — the id `LeafView` has passed since p2 and
     that nothing here consumed, which left 103 roles with no address. It is a
     SEED rather than a binding: closing the viewer must not have to navigate,
     and a later id (the user clicked a different role) wins over the URL. */
  const [viewingRole, setViewingRole] = useState<string | undefined>(openRoleId);
  const [createOpen, setCreateOpen] = useState(false);

  const [worstFitFirst, setWorstFitFirst] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["roles"],
    queryFn: () => roleApi.list(),
    staleTime: 30_000,
  });

  /* The fleet rollup is its own read rather than a reduce over the list: the
     list carries a compact per-role fit, but the sourced-row ratio per month
     is computed over ALL knowledge rows and no per-role summary can produce
     it. That monthly ratio is the standing metric — it is the number that
     would have shown the June 2026 collapse in findings the week it happened
     rather than three months later in an audit. */
  const { data: fleet } = useQuery({
    queryKey: ["roles-fleet-fit"],
    queryFn: () => roleApi.fleetFit(),
    staleTime: 60_000,
  });

  const runAllMutation = useMutation({
    mutationFn: () => roleApi.runAllStale(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["roles"] });
      qc.invalidateQueries({ queryKey: ["roles-maintenance"] });
    },
  });

  /* The source registry and its last poll state. Separate from the fleet fit
     read because it is about the SOURCES a role definition is measured
     against, not about the roles — and because it changes only when somebody
     polls, which is rarely. */
  const { data: structureSources } = useQuery({
    queryKey: ["roles-structure-sources"],
    queryFn: () => roleApi.structureSources(),
    staleTime: 60_000,
  });

  /* Polls nine sources inline and only then spawns, so this mutation is slow
     by design — it takes as long as the fetches take. The ordering is the
     whole mechanism: the bodies are stored before the agent starts, so every
     sentence it quotes is checkable against something it did not fetch and
     cannot edit. */
  const structureResearchMutation = useMutation({
    mutationFn: () => roleApi.runStructureResearch(),
    /* onSettled, not onSuccess: a 409 or a failed poll still means the
       registry may have moved, and leaving the panel on its pre-click state
       is how a stale row outlives the run that would have corrected it. */
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["roles-structure-sources"] });
    },
  });

  /* The action container. Separate read from the sources because it answers a
     different question: the sources panel says whether the FEEDS are healthy,
     this one says what the feeds produced and who owes the next move. */
  const { data: structureActions } = useQuery({
    queryKey: ["roles-structure-actions"],
    queryFn: () => roleApi.structureActions(),
    staleTime: 30_000,
  });

  const [decidingId, setDecidingId] = useState<string | null>(null);
  /* A refusal this view produced itself, as opposed to one the API sent back.
     Both belong in the same strip: from the reader's side "the reject needs a
     reason" and "the source moved" are the same kind of answer. */
  const [decideNote, setDecideNote] = useState<string | null>(null);

  const decideMutation = useMutation({
    mutationFn: ({
      action,
      decision,
      reason,
    }: {
      action: StructureAction;
      decision: "approve" | "reject";
      reason?: string;
    }) =>
      decision === "approve"
        ? roleApi.approveStructureAction(action.id)
        : roleApi.rejectStructureAction(action.id, reason ?? ""),
    /* onSettled, not onSuccess: a 409 means the row may have MOVED — the hash
       drifted and it dropped back to researched — so the list is stale in
       exactly the case where the call failed. */
    onSettled: () => {
      setDecidingId(null);
      qc.invalidateQueries({ queryKey: ["roles-structure-actions"] });
      qc.invalidateQueries({ queryKey: ["roles-structure-sources"] });
    },
  });

  /* THE TWO STEPS THAT COME AFTER THE DECISION.

     Separate from `decideMutation` on purpose: approve and reject are the
     decision, implement and verify are carrying it out. Sharing one mutation
     would put "the action did not move" in front of a person who pressed
     Implement on a row the server refused because it is not approved — a true
     sentence about the wrong thing. The refusal texts differ and so should
     the paths that surface them.

     Verify answers 200 even when the measurement declines, so `refused` on a
     successful response is surfaced as a note rather than an error. The write
     happened; what did not happen is the improvement. */
  const runMutation = useMutation<
    StructureActionImplemented | StructureActionVerified,
    Error,
    { action: StructureAction; step: "implement" | "verify" }
  >({
    mutationFn: ({ action, step }) =>
      step === "implement"
        ? roleApi.implementStructureAction(action.id)
        : roleApi.verifyStructureAction(action.id),
    onSuccess: (result) => {
      const refused = (result as StructureActionVerified).refused;
      if (refused) setDecideNote(refused);
    },
    onSettled: () => {
      setDecidingId(null);
      qc.invalidateQueries({ queryKey: ["roles-structure-actions"] });
    },
  });

  /* CANCEL MEANS CANCEL.
     `window.prompt` returns null when the person dismisses it and "" when they
     press OK on an empty box. Collapsing both with `?.trim() ||` sent a
     placeholder reason either way — so Cancel REJECTED the action, with a
     reason nobody wrote, on a terminal transition that cannot be undone. The
     two answers mean opposite things and are now read as opposite things. */
  const rejectAction = (action: StructureAction) => {
    const answer = readRejectAnswer(window.prompt("Why is this rejected?"));
    if (answer.kind === "abort") return;
    if (answer.kind === "refuse") {
      setDecideNote(answer.message);
      return;
    }
    setDecideNote(null);
    setDecidingId(action.id);
    decideMutation.mutate({ action, decision: "reject", reason: answer.reason });
  };

  const allRoles: RoleInfo[] = data?.roles ?? [];

  const filtered = allRoles.filter((r) => {
    if (
      search &&
      !r.id.toLowerCase().includes(search.toLowerCase()) &&
      !r.description.toLowerCase().includes(search.toLowerCase())
    ) {
      return false;
    }
    if (staleOnly && !r.stale) return false;
    if (domainFilter && r.domain !== domainFilter) return false;
    return true;
  });

  const domains = useMemo(
    () => [...new Set(allRoles.map((r) => r.domain))].sort(),
    [allRoles],
  );

  const staleCount = allRoles.filter((r) => r.stale).length;
  const knowledgeTotal = allRoles.reduce(
    (s, r) => s + (r.knowledge_count ?? 0),
    0,
  );
  const lastMaintainedTs = allRoles
    .map((r) => r.last_maintained)
    .filter((t): t is string => !!t)
    .sort()
    .pop();

  /* The current calendar month's row, matched by key rather than taken as the
     last element: a month with no knowledge rows produces no bucket at all,
     and showing August's ratio under a "this month" label would be a lie. */
  const currentMonth = useMemo(() => {
    if (!fleet) return undefined;
    const now = new Date();
    const key = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    return fleet.knowledge_by_month.find((m) => m.month === key);
  }, [fleet]);

  const handleSelectRole = (id: string) => {
    setSelectedRole(id);
    setViewingRole(id);
  };

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-1">
          <p className="type-small text-fg-muted">
            {allRoles.length} roles · {staleCount} stale · {knowledgeTotal} knowledge entries
            {lastMaintainedTs ? (
              <>
                {" · "}
                <span title="When the maintenance sweep last ran anywhere in the fleet. Not a quality signal — the sweep resets this clock even when it found nothing.">
                  Sweep last ran {formatAge(lastMaintainedTs)}
                </span>
              </>
            ) : null}
          </p>
          {fleet ? (
            <p className="type-small text-fg-muted">
              <span title="Mean across the five fit segments, over every role. Segments that had nothing to measure are left out rather than counted as a pass.">
                Fleet fit {formatScore(fleet.overall_mean)}
              </span>
              {" · "}
              {SEGMENT_ORDER.map((segment, i) => (
                <span key={segment}>
                  {i > 0 ? " · " : null}
                  <span
                    className={segmentTextTone(fleet.segments[segment].mean)}
                    title={`${SEGMENT_LABEL[segment]}: ${SEGMENT_MEANING[segment]}\n${fleet.segments[segment].perfect} of ${fleet.segments[segment].scored} scored roles are at 100. ${fleet.segments[segment].not_applicable} had nothing to measure and are left out of this mean.`}
                  >
                    {SEGMENT_LABEL[segment].toLowerCase()}{" "}
                    {formatScore(fleet.segments[segment].mean)}
                  </span>
                </span>
              ))}
              {currentMonth ? (
                <>
                  {" · "}
                  <span
                    title={`Knowledge rows written in ${currentMonth.month}: ${currentMonth.claimed} ${currentMonth.sourced_label}, ${currentMonth.unverified} unverified, ${currentMonth.filler} no-change filler, ${currentMonth.suppressed} suppressed.`}
                  >
                    {currentMonth.claimed}/{currentMonth.scored_rows} findings
                    sourced this month
                  </span>
                  {" · "}
                  <span
                    className={segmentTextTone(
                      Math.round(100 * currentMonth.findings_ratio),
                    )}
                    title={`Of the ${currentMonth.active_rows} rows the sweep wrote in ${currentMonth.month}, ${currentMonth.scored_rows} were findings and ${currentMonth.filler} were no-change filler. THIS is the number that collapses when the sweep stops finding things — it ran 0.97 in April 2026 and 0.22 in July. The ratio to its left cannot show that, because filler is not in its denominator.`}
                  >
                    {currentMonth.scored_rows}/{currentMonth.active_rows} rows
                    were findings
                  </span>
                </>
              ) : null}
              {" · "}
              <span
                className="text-tertiary"
                title="The rubric these scores were taken under. Scores are only comparable within one version."
              >
                rubric {fleet.rubric_version}
              </span>
            </p>
          ) : null}
        </div>
        {(
          <div className="flex shrink-0 items-center gap-2">
            <Button
              size="sm"
              variant={staleCount === 0 ? "outline" : "default"}
              onClick={() => runAllMutation.mutate()}
              disabled={staleCount === 0 || runAllMutation.isPending}
              className="h-8 text-xs"
            >
              {runAllMutation.isPending ? (
                <>
                  <RefreshCw className="mr-1 h-3 w-3 animate-spin" /> Triggering…
                </>
              ) : (
                <>
                  <RefreshCw className="mr-1 h-3 w-3" />
                  Maintain all stale ({staleCount})
                </>
              )}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => structureResearchMutation.mutate()}
              disabled={structureResearchMutation.isPending}
              className="h-8 text-xs"
              title="Poll the registered specs, docs and papers, store their bodies, then dispatch role-architecture-researcher against whatever actually changed. The poll runs first so every sentence the agent quotes can be checked against a body it did not fetch."
            >
              {structureResearchMutation.isPending ? (
                <>
                  <Radar className="mr-1 h-3 w-3 animate-spin" /> Polling
                  sources…
                </>
              ) : (
                <>
                  <Radar className="mr-1 h-3 w-3" />
                  Research role structure
                </>
              )}
            </Button>
            <Button size="sm" onClick={() => setCreateOpen(true)} className="h-8">
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              Create
            </Button>
          </div>
        )}
      </div>

      {/* BOTH mutations render their error. A button that returns 500 and
          says nothing is the class defect here, not a property of the new
          one: the click looks ignored, so it gets clicked again. */}
      {runAllMutation.error && (
        <MutationError
          label="Maintenance sweep could not start"
          error={runAllMutation.error}
          onDismiss={() => runAllMutation.reset()}
        />
      )}

      {structureResearchMutation.error && (
        <MutationError
          label="Structure research could not start"
          error={structureResearchMutation.error}
          onDismiss={() => structureResearchMutation.reset()}
        />
      )}

      {runAllMutation.data && (
        <div className="rounded border border-info/40 bg-info/10 px-3 py-2 text-xs text-info flex items-center gap-3">
          <span>
            Spawned role-researcher task for{" "}
            {runAllMutation.data.role_count} stale role
            {runAllMutation.data.role_count === 1 ? "" : "s"}.
          </span>
          {runAllMutation.data.task_id && (
            <Link
              to={`/work/${runAllMutation.data.task_id}`}
              className="ml-auto text-info underline"
            >
              open task →
            </Link>
          )}
        </div>
      )}

      {structureResearchMutation.data && (
        <div
          className="flex items-center gap-3 rounded border border-info/40 bg-info/10 px-3 py-2 text-xs text-info"
          data-testid="structure-run-result"
        >
          <span>
            Polled {structureResearchMutation.data.polled} sources ·{" "}
            {structureResearchMutation.data.changed.length} changed
            {structureResearchMutation.data.alarmed.length
              ? ` · ${structureResearchMutation.data.alarmed.length} alarmed`
              : ""}
            .{" "}
            {structureResearchMutation.data.status === "failed"
              ? `Spawn failed: ${structureResearchMutation.data.error}`
              : "Dispatched role-architecture-researcher."}
          </span>
          {structureResearchMutation.data.task_id && (
            <Link
              to={`/work/${structureResearchMutation.data.task_id}`}
              className="ml-auto text-info underline"
            >
              open task →
            </Link>
          )}
        </div>
      )}

      {structureSources ? (
        <StructureSourcesPanel
          sources={structureSources.sources}
          alarmed={structureSources.alarmed}
          neverPolled={structureSources.never_polled}
        />
      ) : null}

      {structureActions ? (
        <StructureActionsPanel
          actions={structureActions.actions}
          byState={structureActions.by_state_filtered}
          busyId={decidingId}
          error={decideMutation.error ?? runMutation.error ?? decideNote}
          onDismissError={() => {
            decideMutation.reset();
            runMutation.reset();
            setDecideNote(null);
          }}
          onApprove={(action) => {
            setDecideNote(null);
            setDecidingId(action.id);
            decideMutation.mutate({ action, decision: "approve" });
          }}
          onReject={rejectAction}
          onImplement={(action) => {
            /* The one confirmation in this panel. Approve is already a
               deliberate act on a row the person read; Implement is the click
               that writes role bodies, and the snapshot behind it is worth
               saying out loud before rather than discovering after. */
            if (
              !window.confirm(
                "Write this change? The store is snapshotted first, and " +
                  "`okuro roles restore --action " +
                  action.id +
                  "` puts the affected roles back.",
              )
            ) {
              return;
            }
            setDecideNote(null);
            setDecidingId(action.id);
            runMutation.mutate({ action, step: "implement" });
          }}
          onVerify={(action) => {
            setDecideNote(null);
            setDecidingId(action.id);
            runMutation.mutate({ action, step: "verify" });
          }}
        />
      ) : null}

      {/* Cross-nav hint to global schedule view */}
      <div className="flex items-center gap-2 text-2xs text-tertiary">
        <CalendarClock className="h-3 w-3" />
        {/* The CANONICAL address. This was `/work?focus=role-refresh`, and the
            shell's legacy redirect used to drop the query — measured:
            `/work?focus=role-refresh` landed on `/work/tasks` with `focus`
            gone, so the link arrived at the bare run list with nothing
            expanded. `mergeSearch` fixed that class in the shell (009394cb);
            spelling it canonically means this link no longer depends on a
            redirect at all. */}
        <Link
          to="/work/tasks?focus=role-refresh"
          className="text-info hover:underline"
        >
          See schedule + history →
        </Link>
        <span>Daily role refresh runs at 5am by default.</span>
      </div>

      {/* Filters */}
      <div className="space-y-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search roles..."
            className="bg-surface pl-8 text-sm text-fg placeholder:text-tertiary"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              aria-label="Clear search"
              className="absolute right-2 top-2 text-tertiary hover:text-fg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:rounded-sm"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
        </div>

        {/* Domain chips + stale toggle */}
        <div className="flex flex-wrap items-center gap-1.5">
          <Chip
            active={domainFilter === null}
            onClick={() => setDomainFilter(null)}
            label={`all (${allRoles.length})`}
          />
          {domains.map((d) => {
            const count = allRoles.filter((r) => r.domain === d).length;
            return (
              <Chip
                key={d}
                active={domainFilter === d}
                onClick={() =>
                  setDomainFilter(domainFilter === d ? null : d)
                }
                label={`${d} (${count})`}
              />
            );
          })}
          {/* Sorts within each domain group by the role's WORST segment. Not
              by the mean: the mean of a structurally perfect role with no
              knowledge and a broken role with good knowledge is the same
              number, and only one of them needs work today. */}
          <button
            type="button"
            onClick={() => setWorstFitFirst((s) => !s)}
            title="Sort each domain by its worst fit segment instead of alphabetically."
            className={`ml-auto inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-2xs font-medium case-label tracking-wider transition-colors ${
              worstFitFirst
                ? "bg-warning/10 text-warning"
                : "bg-surface-elevated text-tertiary hover:text-fg-muted"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                worstFitFirst ? "bg-warning" : "bg-tertiary"
              }`}
            />
            Worst fit first
          </button>
          <button
            type="button"
            onClick={() => setStaleOnly((s) => !s)}
            className={`inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-2xs font-medium case-label tracking-wider transition-colors ${
              staleOnly
                ? "bg-error/10 text-error"
                : "bg-surface-elevated text-tertiary hover:text-fg-muted"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                staleOnly ? "bg-error" : "bg-tertiary"
              }`}
            />
            Stale only
          </button>
        </div>
      </div>

      {/* Role browser */}
      {isLoading ? (
        <p className="text-sm text-tertiary">Loading roles...</p>
      ) : (
        <RoleBrowser
          roles={filtered}
          onSelectRole={handleSelectRole}
          selectedRole={selectedRole}
          sortByWorstFit={worstFitFirst}
        />
      )}

      {/* Role viewer overlay */}
      {viewingRole && (
        <RoleViewer
          roleId={viewingRole}
          onClose={() => setViewingRole(undefined)}
        />
      )}

      {/* Create role dialog — tier/model selection + domain autocomplete */}
      <CreateRoleDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        existingDomains={domains}
      />
    </div>
  );
}

function Chip({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`h-6 rounded-full px-2.5 text-2xs font-medium transition-colors ${
        active
          ? "bg-accent/20 text-accent"
          : "bg-surface-elevated text-tertiary hover:text-fg-muted"
      }`}
    >
      {label}
    </button>
  );
}

/** Legacy name, kept so nothing that imported the page breaks. */
export const RolesPage = RolesView;
