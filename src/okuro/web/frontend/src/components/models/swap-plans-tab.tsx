import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, ChevronDown, ChevronRight, Info, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Segmented } from "@/components/ui/segmented";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";
import {
  modelsApi,
  type SwapActionResponse,
  type SwapPlan,
  type SwapRow,
} from "@/lib/models-api";
import {
  DiffBlock,
  NotConfigured,
  RelationBadge,
  STATE_TONE,
  TierBadge,
  renderCapNote,
} from "./shared";

/**
 * Swap plans — Q4: is this an update, and which line do I edit.
 *
 * The one thing this surface must never do is look like it edits anything.
 * Apply is labelled for what it is: it records that a PERSON applied the plan
 * and hands back the patch. That is plan v1 §4, and the button copy says so
 * rather than leaving the reader to infer it from a docstring they cannot see.
 *
 * Ruling 7 is the checklist gate — a PROTECTED plan cannot reach `applied`
 * until every item is ticked. Ruling 8 is why a headroom row is rendered as a
 * warning beside the gate and never as something that blocks it.
 */

type StateFilter = "" | "proposed" | "testing" | "applied" | "rejected";

/** The fetch ceiling, named so R7's label can describe it. */
const FETCH_LIMIT = 100;

export function SwapPlansTab() {
  const qc = useQueryClient();
  const [state, setState] = useState<StateFilter>("");
  const [open, setOpen] = useState<string | null>(null);

  const q = useQuery({
    queryKey: ["models", "swaps", state],
    queryFn: () => modelsApi.swaps({ state: state || undefined, limit: FETCH_LIMIT }),
    staleTime: 30_000,
  });

  const refresh = () => qc.invalidateQueries({ queryKey: ["models", "swaps"] });
  const data = q.data;

  if (q.isLoading)
    return <div className="h-64 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />;
  if (data && !data.configured) return <NotConfigured reason={data.reason} />;

  const plans = data?.plans ?? [];
  const by = data?.totals.by_state ?? {};

  return (
    <div className="space-y-4">
      <p className="rounded-md border border-border-subtle bg-surface-subtle px-3 py-2 text-xs text-fg-subtle">
        A swap plan is a set of <em>proposed</em> line edits. okuro never edits a
        consumer config: Apply records that you applied it and gives you the
        patch. Propose a plan from the command line —{" "}
        <code className="font-mono text-3xs">
          okuro models swap propose --consumer C --old U --new N
        </code>
        .
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          ariaLabel="Plan state"
          value={state}
          onChange={(v) => setState(v as StateFilter)}
          options={[
            { label: "All", value: "", count: data?.totals.plans ?? 0 },
            { label: "Proposed", value: "proposed", count: by.proposed ?? 0 },
            { label: "Testing", value: "testing", count: by.testing ?? 0 },
            { label: "Applied", value: "applied", count: by.applied ?? 0 },
            { label: "Rejected", value: "rejected", count: by.rejected ?? 0 },
          ]}
        />
      </div>

      {/* R7 -- `totals.plans` IS the server's real count and the list is
          fetched at a ceiling, so when the two disagree the reader is told
          rather than left to assume the page shows everything. */}
      {plans.length > 0 && (
        <SectionLabel>
          {data?.totals.plans ?? "—"} plan
          {data?.totals.plans === 1 ? "" : "s"}
          {renderCapNote(data?.totals.plans ?? 0, FETCH_LIMIT)}
        </SectionLabel>
      )}

      {plans.length === 0 ? (
        <EmptyState
          title="No swap plans"
          description="Propose one with `okuro models swap propose --consumer C --old U --new N`. Nothing is written to any consumer config, at any point."
        />
      ) : (
        <div className="space-y-3">
          {plans.map((p) => (
            <PlanCard
              key={p.plan_group}
              plan={p}
              open={open === p.plan_group}
              onToggle={() => setOpen(open === p.plan_group ? null : p.plan_group)}
              onChanged={refresh}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function PlanCard({
  plan,
  open,
  onToggle,
  onChanged,
}: {
  plan: SwapPlan;
  open: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  const [result, setResult] = useState<SwapActionResponse | null>(null);
  const [why, setWhy] = useState("");
  const [rejecting, setRejecting] = useState(false);
  /**
   * R5 (372ccdb2), reversible branch -- APPLY IS A ONE-WAY STATE TRANSITION
   * AND HAD NO CONFIRM AT ALL.
   *
   * `swapApply` writes `state = applied`, after which `closed` is true: the
   * checklist can no longer be ticked and Reject is disabled. It edits no
   * consumer config -- that is the feature's whole promise -- so it is not the
   * modal case. It is also not nothing: it closes a PROTECTED plan's gate and
   * records that a person applied it, and the button sat one click away
   * directly beside a Reject that already asked twice. The arm makes the two
   * actions in the same row cost the same, which is the inconsistency that
   * made this visible.
   */
  const [armed, setArmed] = useState(false);
  const t = plan.totals;
  const closed = plan.state === "applied" || plan.state === "rejected";

  const tick = useMutation({
    mutationFn: (item: number) => modelsApi.swapTick(plan.plan_group, item),
    onSuccess: (r) => {
      setResult(r);
      onChanged();
    },
  });
  const applyIt = useMutation({
    mutationFn: () => modelsApi.swapApply(plan.plan_group),
    onSuccess: (r) => {
      setResult(r);
      onChanged();
    },
  });
  const rejectIt = useMutation({
    mutationFn: () => modelsApi.swapReject(plan.plan_group, why),
    onSuccess: (r) => {
      setResult(r);
      setRejecting(false);
      setArmed(false);
      onChanged();
    },
  });

  return (
    <section className="overflow-hidden rounded-md border border-border-subtle">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-3 bg-surface-subtle px-3 py-2 text-left transition-fast hover:bg-surface-elevated"
      >
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-fg-subtle" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-fg-subtle" aria-hidden="true" />
        )}
        <span className="font-mono text-xs text-fg">{plan.plan_group}</span>
        <StatusBadge tone={STATE_TONE[plan.state] ?? "neutral"} label={plan.state} showDot />
        <TierBadge tier={plan.tier} />
        <span className="text-sm font-medium text-fg">{plan.consumer}</span>
        <span className="text-xs text-fg-subtle">{plan.mode}</span>
        <span className="ml-auto flex items-center gap-3 text-xs text-fg-subtle">
          <span className="tabular-nums">
            gate {t.checklist_done}/{t.checklist_total}
          </span>
          <span className="tabular-nums">
            {t.rewritable}/{t.rows} lines over {t.files} files
          </span>
          {t.blocking > 0 && <StatusBadge tone="error" label={`${t.blocking} blocker`} />}
          {t.warnings > 0 && <StatusBadge tone="warning" label={`${t.warnings} warn`} />}
        </span>
      </button>

      {open && (
        <div className="space-y-4 p-3">
          {/* R3 -- `sm:` is 384px under the 8px root, so it was true in every
              state the shell can produce. `@2xl` is 672px against the named
              `pane` container, which is where two columns of a long unit id
              actually fit. */}
          <dl className="grid gap-x-6 gap-y-1 text-xs @2xl:grid-cols-2">
            <Field label="Old">
              <span className="font-mono text-3xs">{plan.unit_old}</span>
            </Field>
            <Field label={`New (${plan.new_kind})`}>
              <span className="font-mono text-3xs">{plan.new_ref}</span>
            </Field>
            <Field label="Relation">
              <RelationBadge relation={plan.relation} />
              {plan.relation_why && (
                <div className="mt-0.5 text-3xs text-fg-subtle">{plan.relation_why}</div>
              )}
            </Field>
            <Field label="Note">
              <span className="text-fg-muted">{plan.note ?? "—"}</span>
            </Field>
          </dl>

          <div>
            <SectionLabel>
              Gate — {t.checklist_done} of {t.checklist_total} ticked
            </SectionLabel>
            <ul className="mt-1 space-y-1">
              {plan.checklist.map((c, i) => (
                <li
                  /* THE SERVER'S IDENTITY FOR AN ITEM IS ITS ORDINAL, not its
                     text: `tick` takes `i + 1` and `swap.py` builds the list
                     from `checklist_for(consumer, tier)`, a plain list of
                     strings out of `conventions.ai_models.swap_checklists`. A
                     repeated string there is a config mistake, not an
                     impossibility, and it would collide. Keying on what the
                     mutation addresses cannot. */
                  key={`${i + 1}:${c.item}`}
                  className="flex items-center gap-2 text-sm"
                >
                  <Button
                    size="sm"
                    variant={c.done ? "ghost" : "outline"}
                    disabled={c.done || closed || tick.isPending}
                    onClick={() => tick.mutate(i + 1)}
                    aria-label={c.done ? `${c.item} — ticked` : `Tick: ${c.item}`}
                  >
                    <Check
                      className={cn("h-3 w-3", c.done ? "text-success" : "text-fg-subtle")}
                      aria-hidden="true"
                    />
                  </Button>
                  <span className={cn(c.done ? "text-fg-subtle line-through" : "text-fg")}>
                    {c.item}
                  </span>
                  {c.done_at && <span className="text-3xs text-fg-muted">{c.done_at}</span>}
                </li>
              ))}
            </ul>
          </div>

          {plan.blockers.length > 0 && (
            <ul className="space-y-1">
              {plan.blockers.map((b) => (
                <li
                  key={b.code}
                  className={cn(
                    "flex items-start gap-2 rounded-md border px-2 py-1.5 text-xs",
                    b.severity === "warn"
                      ? "border-warning/40 text-warning"
                      : "border-error/40 text-error",
                  )}
                >
                  {b.severity === "warn" ? (
                    <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
                  ) : (
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
                  )}
                  <span>
                    {b.text}
                    {b.severity === "warn" && (
                      <span className="text-fg-subtle"> — a warning, never a refusal</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}

          <div className="flex flex-wrap items-center gap-2">
            {!armed ? (
              <Button
                size="sm"
                disabled={closed || applyIt.isPending}
                onClick={() => setArmed(true)}
              >
                Mark applied + show patch
              </Button>
            ) : (
              <Button
                size="sm"
                variant="destructive"
                disabled={closed || applyIt.isPending}
                onClick={() => {
                  setArmed(false);
                  applyIt.mutate();
                }}
                aria-label="Confirm: mark this plan applied"
              >
                Mark applied? This closes the plan
              </Button>
            )}
            {!rejecting ? (
              <Button
                size="sm"
                variant="outline"
                disabled={plan.state === "applied"}
                onClick={() => setRejecting(true)}
              >
                <X className="h-3 w-3" aria-hidden="true" />
                Reject
              </Button>
            ) : (
              <>
                <Input
                  placeholder="Why is this swap not happening?"
                  value={why}
                  onChange={(e) => setWhy(e.target.value)}
                  className="w-72"
                />
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!why.trim() || rejectIt.isPending}
                  onClick={() => rejectIt.mutate()}
                >
                  Confirm reject
                </Button>
              </>
            )}
            <span className="text-3xs text-fg-subtle">
              okuro writes nothing to {plan.consumer}&apos;s config, at any point.
            </span>
          </div>

          {result && result.ok === false && (
            <p className="rounded-md border border-error/40 px-2 py-1.5 text-xs text-error">
              Refused — {result.reason}
            </p>
          )}
          {result?.apply_by_hand && (
            <div>
              <SectionLabel>Apply this by hand</SectionLabel>
              <DiffBlock diff={result.apply_by_hand} />
            </div>
          )}

          <div>
            <SectionLabel>
              {t.rows} line{t.rows === 1 ? "" : "s"} this swap would change ·{" "}
              {t.unrewritable} cannot be rewritten mechanically
            </SectionLabel>
            <div className="mt-1 space-y-2">
              {plan.rows.map((r) => (
                <RowCard key={r.id} row={r} />
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function RowCard({ row }: { row: SwapRow }) {
  return (
    <div className="rounded-md border border-border-subtle p-2">
      <div className="flex flex-wrap items-center gap-2 text-3xs">
        <span className="font-mono text-fg">{row.location}</span>
        <span className="text-fg-muted">{row.match_kind ?? "no match kind"}</span>
        {row.rewritable ? (
          <StatusBadge tone="success" label="rewritable" />
        ) : (
          <StatusBadge tone="warning" label="not rewritable" />
        )}
      </div>
      {row.rewrite_note && (
        <p className="mt-1 text-3xs text-fg-subtle">{row.rewrite_note}</p>
      )}
      {row.diff ? (
        <div className="mt-1">
          <DiffBlock diff={row.diff} />
        </div>
      ) : (
        <p className="mt-1 font-mono text-3xs text-fg-subtle">{row.old_ref}</p>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      {/* `case-label` reads `--type-label-transform`; the literal `uppercase`
          took the decision away from the kit (0d37d05e). `text-fg-muted`
          rather than `text-tertiary`, which is the standing AA failure the
          MODELS pass stopped feeding (kit todo c581c9b2). */}
      <dt className="case-label text-2xs tracking-wider text-fg-muted">{label}</dt>
      <dd className="text-fg">{children}</dd>
    </div>
  );
}
