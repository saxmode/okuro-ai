/**
 * BlockerCard — single dispatcher for every "engine handed control back
 * to user" state.
 *
 * C10 contract: the FE renders a blocker only when `snapshot.blocker` is
 * non-null. The component routes by `blocker.kind`:
 *
 *   panel_confirmation → rich PanelConfirmCard (proposed-role picker)
 *   decision_gate      → embedded GatePanel (option picker + ADR lock)
 *   capability_gap     → role-creation plan approval header
 *   discussion_proceed → generic Proceed button
 *   blocked_review     → "Override blocked phase" header + button
 *   timeout_cap        → 4-option resolve panel (extend / done / skip / fail)
 *
 * Mounted unconditionally on the task-detail page. Self-hides when
 * `blocker` is null. Replaces the previous AwaitingCard + GatePanel pair
 * (D13) and the deliberation-panel-gated CapabilityGapCard (D14).
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { SnapshotBlocker, GatePresentation } from "@/types/api";
import { api } from "@/lib/api";
import { humanizeSlug, parseApiDate } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { GatePanel } from "@/components/task/gate-panel";
import { PART } from "@/lib/nouns";

/**
 * GateBody — renders the plain-language gate copy. Prefers the backend's
 * structured `payload.presentation` (headline + explanation + action, with raw
 * diagnostics tucked into a collapsed disclosure); falls back to the flat
 * `summary` string for older payloads. This is where "what happened / should I
 * worry / what to do" is separated from engineer diagnostics.
 */
function GateBody({
  payload,
  fallback,
  extraDetails,
  taskId,
}: {
  payload?: Record<string, unknown>;
  fallback?: string;
  extraDetails?: React.ReactNode;
  /** Enables the P2.4 "re-check this wording"
   *  control inside the technical-details disclosure. */
  taskId?: string;
}) {
  const p = payload?.presentation as GatePresentation | undefined;
  const details = p?.technical_details?.trim();

  if (!p?.headline) {
    // Back-compat: no structured presentation — show the (already clean) text.
    return (
      <div className="space-y-2">
        {fallback && <p className="text-xs text-fg-muted">{fallback}</p>}
        {(details || extraDetails) && (
          <GateDetails text={details} extra={extraDetails} taskId={taskId} />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <p className="text-sm font-medium text-fg">{p.headline}</p>
      {p.explanation && (
        <p className="text-xs text-fg-muted">{p.explanation}</p>
      )}
      {p.action && (
        <p className="text-xs font-medium text-fg">{p.action}</p>
      )}
      {(details || extraDetails) && (
        <GateDetails text={details} extra={extraDetails} taskId={taskId} />
      )}
    </div>
  );
}

/** Collapsed "Show technical details" disclosure — engineer diagnostics only. */
function GateDetails({
  text,
  extra,
  taskId,
}: {
  text?: string;
  extra?: React.ReactNode;
  taskId?: string;
}) {
  return (
    <details className="mt-1 rounded border border-border bg-surface/60 p-2">
      <summary className="cursor-pointer select-none text-3xs uppercase tracking-wider text-tertiary">
        Show technical details
      </summary>
      {text && (
        <p className="mt-1.5 whitespace-pre-wrap font-mono text-3xs text-tertiary">
          {text}
        </p>
      )}
      {extra && <div className="mt-1.5">{extra}</div>}
      {taskId && <RefreshGateCopy taskId={taskId} />}
    </details>
  );
}

/**
 * "Re-check this wording" (ROCK-SOLID v5 P2.4).
 *
 * A gate's copy is written once, by the engine, at park time — and the park
 * is engineless, so nothing re-renders it afterwards. A card written before a
 * copy fix keeps the old wording until the gate is answered. This asks the
 * backend to re-author it from the inputs it was built with.
 *
 * Lives INSIDE the technical-details disclosure on purpose. The primary job
 * of this card is to get a decision out of the reader; a control that neither
 * answers the gate nor changes what answering it does has no business
 * competing with the buttons that do. It is here for the case where the copy
 * looks wrong — which is exactly when someone opens the details.
 */
function RefreshGateCopy({ taskId }: { taskId: string }) {
  const qc = useQueryClient();
  const [state, setState] = useState<"idle" | "busy" | "same" | "stale">("idle");

  async function run() {
    setState("busy");
    try {
      const res = await api<{ refreshed: boolean; changed?: boolean }>(
        `/api/tasks/${taskId}/awaiting/refresh`,
        { method: "POST", body: JSON.stringify({}) },
      );
      if (!res.refreshed) {
        // A gate parked before the backend recorded what its card was built
        // from. Say so plainly instead of showing a button that did nothing.
        setState("stale");
        return;
      }
      setState(res.changed ? "idle" : "same");
      await qc.invalidateQueries({ queryKey: ["taskSnapshot", taskId] });
      await qc.invalidateQueries({ queryKey: ["taskState", taskId] });
    } catch {
      setState("idle");
    }
  }

  return (
    <div className="mt-2 flex items-center gap-2">
      <button
        type="button"
        onClick={run}
        disabled={state === "busy"}
        className="rounded border border-border px-1.5 py-0.5 text-3xs uppercase tracking-wider text-tertiary transition-colors hover:text-fg-muted disabled:opacity-50"
      >
        {state === "busy" ? "Re-checking…" : "Re-check this wording"}
      </button>
      {state === "same" && (
        <span className="text-3xs text-tertiary">already up to date</span>
      )}
      {state === "stale" && (
        <span className="text-3xs text-tertiary">
          this one predates re-wording — answering it still works normally
        </span>
      )}
    </div>
  );
}

/**
 * The option the BACKEND named as this gate's safe default (P2.1).
 *
 * Every card used to hardcode its own idea of which button was primary —
 * timeout_cap picked extend_and_retry, blocked_review filled in BOTH "Decide"
 * and "Retry", so the row showed two equally-primary buttons and no default at
 * all. That was survivable while the recommendation was a constant per kind.
 * It stopped being one: `recommended` now varies by CAUSE within a single kind
 * — blocked_review/infra recommends `retry` ("your work is fine, the checker
 * blipped"), blocked_review/decision recommends `override`, because a bare
 * retry on a trade-off the AI already failed to settle just repeats the loop.
 *
 * A card that hardcodes the highlight therefore points at the wrong button on
 * one of those two paths, confidently. Reading it from the payload is what
 * makes the backend's judgement the one the user sees.
 */
function recommendedOption(payload?: Record<string, unknown>): string {
  const p = payload?.presentation as GatePresentation | undefined;
  return (p?.recommended ?? "").trim();
}

/** Button variant for one option: filled when it is the recommendation,
 *  outline otherwise. `destructive` wins regardless — an irreversible action
 *  is never dressed up as the safe default. */
function variantForOption(
  option: string,
  recommended: string,
  opts?: { destructive?: boolean },
): "default" | "outline" | "destructive" {
  if (opts?.destructive) return "destructive";
  return option === recommended ? "default" : "outline";
}

type ProposedRole = {
  role_id: string;
  similarity: number;
  match_type: string;
  why: string;
};

// ROCK-SOLID v5 P1.3 — used as "waiting {relativeSince(...)}" on all 5
// blocker card kinds. Was "stuck {N} min" everywhere (evidence inventory
// mismatch #7), including panel_confirmation where nothing is stuck — the
// system is waiting exactly as designed, INFO not ALARM.
function relativeSince(iso: string): string {
  if (!iso) return "";
  const ts = parseApiDate(iso).getTime();
  if (!isFinite(ts)) return "";
  const mins = Math.max(0, Math.round((Date.now() - ts) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `${hrs}h ${rem}m` : `${hrs}h`;
}

/**
 * The "waiting N min" line every blocker card carries, plus the P2.2 stale
 * note. One component rather than five copies: the copy on this line has
 * already drifted once (it read "stuck N min" on all five, including
 * panel_confirmation where nothing is stuck).
 *
 * Stale reads as INFORMATION, not alarm — same tertiary tone, no icon, no
 * colour change. Nothing is broken: the gate is open exactly as designed and
 * the run is fine. It says "this looks forgotten" because after two days that
 * is usually what it is, and the honest thing is to say so quietly.
 */
function WaitingFor({ blocker }: { blocker: SnapshotBlocker }) {
  if (!blocker.since) return null;
  return (
    <div className="text-3xs text-tertiary">
      waiting {relativeSince(blocker.since)}
      {blocker.stale && (
        <span className="ml-1.5">· still here whenever you are</span>
      )}
    </div>
  );
}

// Kinds with a dedicated rich card / canonical action verb. Anything NOT
// here is an unknown/forward-compat kind → safe humanized fallback, never
// a raw machine id in the UI.
// Keep in sync with _BLOCKER_KIND_LABEL in api/state_reader.py — an
// unregistered kind degrades the card on both sides at once (raw machine
// string as the label here, "unknown blocker" styling there).
const KNOWN_KINDS = new Set([
  "capability_gap",
  "panel_confirmation",
  "decision_gate",
  "discussion_proceed",
  "blocked_review",
  "timeout_cap",
  "subtask_approval",
]);

// snake_case / colon-scoped machine id → human label. "blocked_review" →
// "Blocked Review"; "reviewer:phase2:critic" → "Reviewer Phase2 Critic".
// Used ONLY as a de-emphasised context chip, never the primary header.
function humanizeKind(kind: string): string {
  return (kind || "")
    .split(/[_:]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// Kind-specific header label. Static FE labels for the kinds whose UX
// has a canonical action verb that doesn't depend on payload — keeps
// `blocker.label` free to carry phase- or payload-specific text from
// the BE for the other kinds.
function headerLabelForKind(blocker: SnapshotBlocker): string {
  switch (blocker.kind) {
    case "capability_gap":
      return "Approve role-creation plan";
    case "panel_confirmation":
      return "Panel confirmation required";
    case "decision_gate":
      return "Decision gate";
    case "discussion_proceed":
      return "Continue with deliberation";
    case "blocked_review":
      return blocker.label || "This step needs your decision";
    default:
      // Unknown/forward-compat kind: prefer a BE-supplied label, else a
      // safe universal header. NEVER fall through to the raw machine id —
      // that leaked strings like "timeout_cap_v2" into the UI (Phase 3).
      return blocker.label || "This task needs you";
  }
}

export function BlockerCard({
  blocker,
  taskId,
  onResolve,
}: {
  blocker: SnapshotBlocker | null;
  taskId: string;
  onResolve?: (response: unknown) => void;
}) {
  if (!blocker) return null;

  const header = headerLabelForKind(blocker);

  // decision_gate delegates entirely to GatePanel — the rich option
  // picker. BlockerCard owns the testid + the section header so the
  // duplicate-UI bug (D13) can't return.
  if (blocker.kind === "decision_gate") {
    return (
      <div data-testid="blocker-card" className="space-y-3">
        <div className="text-2xs uppercase tracking-wider text-warning">
          {header}
        </div>
        <GatePanel taskId={taskId} />
      </div>
    );
  }

  if (blocker.kind === "panel_confirmation") {
    return (
      <div data-testid="blocker-card">
        <PanelConfirmInner
          taskId={taskId}
          blocker={blocker}
          header={header}
          onResolve={onResolve}
        />
      </div>
    );
  }

  if (blocker.kind === "timeout_cap") {
    return (
      <div data-testid="blocker-card">
        <TimeoutCapInner
          taskId={taskId}
          blocker={blocker}
          header={header}
          onResolve={onResolve}
        />
      </div>
    );
  }

  // PR B — capability_gap gets its own rich card. Pre-fix the
  // capability_gap awaiting kind collapsed to GenericBlocker (a single
  // Proceed button), which hid the phase-0 plan + creator-role list
  // from users who weren't viewing the deliberation panel.
  if (blocker.kind === "capability_gap") {
    return (
      <div data-testid="blocker-card">
        <CapabilityGapInner
          taskId={taskId}
          blocker={blocker}
          header={header}
          onResolve={onResolve}
        />
      </div>
    );
  }

  // blocked_review gets a dedicated card: the reviewer FLAGGED the work, so
  // the user needs BOTH "retry the flagged work" (re-run + re-review) and
  // "override & continue" (accept the FAIL) — not a lone Continue button.
  if (blocker.kind === "blocked_review") {
    return (
      <div data-testid="blocker-card">
        <BlockedReviewInner
          taskId={taskId}
          blocker={blocker}
          header={header}
          onResolve={onResolve}
        />
      </div>
    );
  }

  // Default: generic Proceed/Override button for the remaining kinds
  // (discussion_proceed). Each of these still ships its rich UI inside
  // DeliberationPanel; this card guarantees the action is reachable in
  // EVERY mode (D14).
  return (
    <div data-testid="blocker-card">
      <GenericBlocker
        taskId={taskId}
        blocker={blocker}
        header={header}
        onResolve={onResolve}
      />
    </div>
  );
}

function CapabilityGapInner({
  taskId,
  blocker,
  header,
  onResolve,
}: {
  taskId: string;
  blocker: SnapshotBlocker;
  header: string;
  onResolve?: (response: unknown) => void;
}) {
  const qc = useQueryClient();
  const summary = (blocker.payload?.summary as string) ?? blocker.summary ?? "";
  const creatorRoles =
    (blocker.payload?.creator_roles as string[]) ?? [];
  const requestedRoles =
    (blocker.payload?.requested_roles as string[]) ??
    (blocker.payload?.missing_roles as string[]) ??
    [];
  const phase0Count =
    (blocker.payload?.phase0_count as number) ??
    (Array.isArray(blocker.payload?.phase0)
      ? (blocker.payload?.phase0 as unknown[]).length
      : 0);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function resolve(verb: "accept" | "decline") {
    setPending(verb);
    setError(null);
    try {
      const endpoint =
        verb === "accept"
          ? blocker.endpoint
          : `/api/tasks/${taskId}/capability-gap/decline`;
      const res = await api(endpoint, {
        method: "POST",
        body: "{}",
      });
      await qc.invalidateQueries({ queryKey: ["taskSnapshot", taskId] });
      await qc.invalidateQueries({ queryKey: ["taskState", taskId] });
      await qc.invalidateQueries({ queryKey: ["capabilityGap", taskId] });
      onResolve?.(res);
    } catch (e) {
      setError((e as Error).message ?? "Request failed");
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="space-y-3 rounded border border-warning/50 bg-warning-subtle/40 p-4">
      <div className="flex items-baseline justify-between">
        <div className="text-2xs uppercase tracking-wider text-warning">
          {header}
        </div>
        <WaitingFor blocker={blocker} />
      </div>
      {/* ROCK-SOLID v5 P1.3/1.5 — GateBody prefers payload.presentation
          (engine.py always sends one via humanize_gate for capability_gap);
          falls back to the flat summary for older payloads. */}
      <GateBody payload={blocker.payload} fallback={summary} taskId={taskId} />
      {requestedRoles.length > 0 && (
        <div className="text-3xs text-tertiary">
          <div className="uppercase tracking-wider">Missing roles</div>
          <ul className="mt-1 flex flex-wrap gap-1">
            {requestedRoles.map((r) => (
              <li
                key={r}
                title={r}
                className="rounded bg-surface/80 px-2 py-0.5 text-3xs text-fg"
              >
                {humanizeSlug(r)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {creatorRoles.length > 0 && (
        <div className="text-3xs text-tertiary">
          <span className="uppercase tracking-wider">Plan:</span>{" "}
          <span
            className="text-fg-muted"
            title={creatorRoles.join(" → ")}
          >
            {creatorRoles.map(humanizeSlug).join(" → ")}
          </span>{" "}
          {phase0Count > 0 && (
            <span className="text-fg-muted">
              ({phase0Count} {phase0Count === 1 ? "step" : "steps"})
            </span>
          )}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() => resolve("accept")}
          disabled={pending !== null}
          size="sm"
        >
          {pending === "accept" ? "…" : "Approve role creation"}
        </Button>
        <Button
          onClick={() => resolve("decline")}
          disabled={pending !== null}
          size="sm"
          variant="secondary"
        >
          {pending === "decline" ? "…" : "Decline"}
        </Button>
      </div>
      {error && <p className="text-3xs text-error">{error}</p>}
    </div>
  );
}

function PanelConfirmInner({
  taskId,
  blocker,
  header,
  onResolve,
}: {
  taskId: string;
  blocker: SnapshotBlocker;
  header: string;
  onResolve?: (response: unknown) => void;
}) {
  const qc = useQueryClient();
  const proposed = (blocker.payload?.proposed as ProposedRole[]) ?? [];
  const quality_weak = Boolean(blocker.payload?.quality_weak);
  const top_similarity = (blocker.payload?.top_similarity as number) ?? 0;
  const mentioned_roles =
    (blocker.payload?.mentioned_roles as string[]) ?? [];
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(quality_weak ? [] : proposed.map((p) => p.role_id)),
  );
  const [extras, setExtras] = useState<Set<string>>(() => new Set(mentioned_roles));
  const [extraTyped, setExtraTyped] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggleExtra(id: string) {
    setExtras((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  async function confirm() {
    const roles = Array.from(
      new Set([
        ...selected,
        ...extras,
        ...extraTyped.split(",").map((s) => s.trim()).filter(Boolean),
      ]),
    );
    if (roles.length === 0) {
      setError("Pick at least one role.");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const res = await api(blocker.endpoint, {
        method: "POST",
        body: JSON.stringify({ roles, strategy: "parallel" }),
      });
      await qc.invalidateQueries({ queryKey: ["taskSnapshot", taskId] });
      await qc.invalidateQueries({ queryKey: ["taskState", taskId] });
      onResolve?.(res);
    } catch (e) {
      setError((e as Error).message ?? "Request failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-3 rounded border border-warning/50 bg-warning-subtle/40 p-4">
      <div className="flex items-baseline justify-between">
        <div className="text-2xs uppercase tracking-wider text-warning">
          {header}
        </div>
        <WaitingFor blocker={blocker} />
      </div>
      {/* ROCK-SOLID v5 P1.5 — was a hardcoded sentence that ignored
          payload.presentation entirely, even though engine.py always sends
          one for panel_confirmation (humanize_gate). GateBody falls back to
          the same sentence for any older payload that lacks it. */}
      <GateBody
        payload={blocker.payload}
        fallback={`The orchestrator proposed ${proposed.length} role(s) for this task. Confirm, edit, or add roles before deliberation begins.`}
        taskId={taskId}
      />
      {quality_weak && (
        <div
          className="rounded border border-error/40 bg-error-subtle/30 p-2 text-2xs text-error"
          title={`top similarity ${top_similarity.toFixed(3)}`}
        >
          Weak match — the best proposed role doesn't confidently fit this
          task. Consider adding explicit roles below before confirming.
        </div>
      )}
      {mentioned_roles.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-3xs uppercase tracking-wider text-tertiary">
            Roles mentioned in task body — click to add
          </div>
          <div className="flex flex-wrap gap-1.5">
            {mentioned_roles.map((rid) => (
              <button
                key={rid}
                onClick={() => toggleExtra(rid)}
                title={rid}
                className={`rounded border px-2 py-1 text-3xs transition-colors ${
                  extras.has(rid)
                    ? "border-accent bg-accent-subtle text-accent"
                    : "border-border bg-surface text-fg-muted hover:border-border-hover"
                }`}
              >
                {extras.has(rid) ? "✓ " : "+ "}
                {humanizeSlug(rid)}
              </button>
            ))}
          </div>
        </div>
      )}
      <ul className="space-y-1.5">
        {proposed.map((r) => (
          <li key={r.role_id}>
            <label
              className={`flex cursor-pointer items-start gap-2 rounded border p-2 transition-colors ${
                selected.has(r.role_id)
                  ? "border-accent/50 bg-accent-subtle"
                  : "border-border bg-surface hover:border-border-hover"
              }`}
            >
              <input
                type="checkbox"
                checked={selected.has(r.role_id)}
                onChange={() => toggle(r.role_id)}
                className="mt-0.5"
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between">
                  <span
                    className="text-2xs font-medium uppercase tracking-wider text-fg"
                    title={r.role_id}
                  >
                    {humanizeSlug(r.role_id)}
                  </span>
                  {/* ROCK-SOLID v5 P1.5 — the raw similarity float is a
                      technical detail (D-A), not something a reader should
                      have to interpret; the match_type word alone is
                      enough context, the number moves to a tooltip. */}
                  <span
                    className="text-3xs text-tertiary"
                    title={`similarity ${r.similarity.toFixed(3)}`}
                  >
                    {r.match_type}
                  </span>
                </div>
                {r.why && (
                  <p className="mt-1 text-2xs text-fg-muted">{r.why}</p>
                )}
              </div>
            </label>
          </li>
        ))}
      </ul>
      <div className="space-y-1">
        <label className="text-3xs uppercase tracking-wider text-tertiary">
          Add more roles (comma-separated role ids)
        </label>
        <input
          type="text"
          value={extraTyped}
          onChange={(e) => setExtraTyped(e.target.value)}
          placeholder="linux-audio-engineer, qa-engineer"
          className="w-full rounded border border-border bg-surface px-2 py-1.5 text-xs text-fg placeholder:text-tertiary focus:border-accent focus:outline-none"
        />
      </div>
      <div className="flex gap-2">
        <Button onClick={confirm} disabled={pending} size="sm" className="flex-1">
          {pending
            ? "Confirming…"
            : `Confirm panel (${selected.size + extras.size + extraTyped.split(",").filter((s) => s.trim()).length})`}
        </Button>
      </div>
      {error && <p className="text-3xs text-error">{error}</p>}
    </div>
  );
}

function GenericBlocker({
  taskId,
  blocker,
  header,
  onResolve,
}: {
  taskId: string;
  blocker: SnapshotBlocker;
  header: string;
  onResolve?: (response: unknown) => void;
}) {
  const qc = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Forward-compat: a kind the FE doesn't recognise still gets a usable
  // card. Show the humanised kind as a context chip (never the raw id) and
  // only offer the resolve button when the BE actually gave us an endpoint —
  // otherwise the card is informational, with the recovery actions in the
  // ContinueBar below, so an unknown kind never dead-ends.
  const isUnknown = !KNOWN_KINDS.has(blocker.kind);
  const hasEndpoint = Boolean(blocker.endpoint);
  // Show the kind chip only when it adds information — i.e. the header is a
  // generic sentence ("This task needs you"), not when the BE label already
  // equals the humanised kind (would render the same text twice).
  const humanKind = humanizeKind(blocker.kind);
  const showKindChip = isUnknown && header !== humanKind;

  async function go() {
    if (!hasEndpoint) return;
    setPending(true);
    setError(null);
    try {
      const res = await api(blocker.endpoint, {
        method: blocker.method || "POST",
        body: (blocker.method || "POST") !== "GET" ? "{}" : undefined,
      });
      await qc.invalidateQueries({ queryKey: ["taskSnapshot", taskId] });
      await qc.invalidateQueries({ queryKey: ["taskState", taskId] });
      onResolve?.(res);
    } catch (e) {
      setError((e as Error).message ?? "Request failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-3 rounded border border-warning/50 bg-warning-subtle/40 p-4">
      <div className="flex items-baseline justify-between">
        <div className="flex items-baseline gap-2">
          <div className="text-2xs uppercase tracking-wider text-warning">
            {header}
          </div>
          {showKindChip && (
            <span className="rounded bg-surface/80 px-1.5 py-0.5 font-mono text-3xs text-tertiary">
              {humanKind}
            </span>
          )}
        </div>
        <WaitingFor blocker={blocker} />
      </div>
      {blocker.summary && (
        <p className="text-xs text-fg-muted">{blocker.summary}</p>
      )}
      <div className="flex gap-2">
        {hasEndpoint ? (
          <Button onClick={go} disabled={pending} size="sm">
            {pending ? "…" : "Continue"}
          </Button>
        ) : (
          <p className="text-3xs text-tertiary">
            This task needs you, but this okuro build doesn't recognise how to
            resolve it automatically — it may need an update.
          </p>
        )}
      </div>
      {error && <p className="text-3xs text-error">{error}</p>}
    </div>
  );
}

function BlockedReviewInner({
  taskId,
  blocker,
  header,
  onResolve,
}: {
  taskId: string;
  blocker: SnapshotBlocker;
  header: string;
  onResolve?: (response: unknown) => void;
}) {
  const qc = useQueryClient();
  const recommended = recommendedOption(blocker.payload);
  const cappedSubtasks = (blocker.payload?.capped_subtasks as string[]) ?? [];
  const findings =
    (blocker.payload?.critic_findings as {
      evidence?: string;
      file?: string;
      severity?: string;
      summary?: string;
      suggested_fix?: string;
    }[]) ?? [];
  const verdict = (blocker.payload?.verdict as string) ?? "";
  const needsDecision =
    verdict === "NEEDS_USER" || (blocker.payload?.cause as string) === "decision";
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showDecide, setShowDecide] = useState(needsDecision);
  const [decision, setDecision] = useState("");

  // action="retry" re-runs the flagged subtasks (work + review); "override"
  // accepts the FAIL; "decide" injects the user's authoritative ruling (locked
  // as an ADR + led into a scoped re-dispatch) so the agent applies the call
  // instead of re-inventing one each round. All hit the same endpoint; the
  // body's `action` selects the path.
  async function resolve(
    action: "retry" | "override" | "decide",
    extra?: Record<string, unknown>,
  ) {
    setPending(action);
    setError(null);
    try {
      const res = await api(blocker.endpoint, {
        method: blocker.method || "POST",
        body: JSON.stringify({ action, subtask_ids: cappedSubtasks, ...extra }),
      });
      await qc.invalidateQueries({ queryKey: ["taskSnapshot", taskId] });
      await qc.invalidateQueries({ queryKey: ["taskState", taskId] });
      onResolve?.(res);
    } catch (e) {
      setError((e as Error).message ?? "Request failed");
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="space-y-3 rounded border border-warning/50 bg-warning-subtle/40 p-4">
      <div className="flex items-baseline justify-between">
        <div className="text-2xs uppercase tracking-wider text-warning">
          {header}
        </div>
        <WaitingFor blocker={blocker} />
      </div>
      <GateBody
        payload={blocker.payload}
        fallback={blocker.summary}
        taskId={taskId}
        extraDetails={
          findings.length > 0 ? (
            <div className="space-y-1.5">
              <div className="text-3xs uppercase tracking-wider text-tertiary">
                What the quality check flagged
              </div>
              <ul className="space-y-1.5">
                {findings.slice(0, 5).map((f, i) => (
                  <li
                    key={i}
                    className="rounded border border-border bg-surface p-2 text-2xs text-fg-muted"
                  >
                    {f.severity === "load_bearing" && (
                      <span className="mr-1 rounded bg-error/15 px-1 text-3xs uppercase text-error">
                        must fix
                      </span>
                    )}
                    {f.summary ?? f.evidence ?? ""}
                    {f.file && (
                      <div className="mt-1 font-mono text-3xs text-tertiary">
                        in {f.file.split("/").pop()}
                      </div>
                    )}
                  </li>
                ))}
                {findings.length > 5 && (
                  <li className="text-3xs text-tertiary">
                    +{findings.length - 5} more item(s)
                  </li>
                )}
              </ul>
            </div>
          ) : undefined
        }
      />
      {cappedSubtasks.length > 0 && (
        <div className="text-3xs text-tertiary">
          <span className="uppercase tracking-wider">Affected part:</span>{" "}
          <span className="font-mono text-fg-muted">
            {cappedSubtasks.join(", ")}
          </span>
        </div>
      )}
      {showDecide && (
        <div className="space-y-1.5 rounded border border-accent/40 bg-accent-subtle/30 p-2">
          <div className="text-3xs uppercase tracking-wider text-accent">
            Your decision
          </div>
          <textarea
            value={decision}
            onChange={(e) => setDecision(e.target.value)}
            rows={3}
            placeholder="Tell the AI what to do — e.g. 'Keep the original viewport tag, ignore the QA note, and finish the deck.'"
            className="w-full rounded border border-border bg-surface p-2 text-2xs text-fg"
          />
          <Button
            onClick={() => resolve("decide", { decision, subtask_id: cappedSubtasks[0] ?? "" })}
            disabled={pending !== null || decision.trim().length === 0}
            size="sm"
          >
            {pending === "decide" ? "Applying…" : "Apply decision & re-run"}
          </Button>
          <p className="text-3xs text-tertiary">
            Saved as your final call. The AI applies it on the re-run and won't
            argue it again.
          </p>
        </div>
      )}
      {/* P2.1 — exactly ONE filled button, and which one comes from the
          backend's `recommended`, not from this file. Pre-fix both Decide and
          Retry were filled, so the row said "these two are equally right"
          while the payload had a specific answer. "Decide" is an affordance
          for retry-with-guidance, so it takes the highlight only when the
          gate recommends retry AND the form is not already open. */}
      <div className="flex flex-wrap gap-2">
        {!showDecide && (
          <Button
            onClick={() => setShowDecide(true)}
            disabled={pending !== null}
            size="sm"
            variant={variantForOption("retry", recommended)}
            className="flex-1"
          >
            Decide
          </Button>
        )}
        <Button
          onClick={() => resolve("retry")}
          disabled={pending !== null}
          size="sm"
          variant={showDecide ? variantForOption("retry", recommended) : "outline"}
          className={showDecide ? undefined : "flex-1"}
        >
          {pending === "retry" ? "Retrying…" : "Retry flagged work"}
        </Button>
        <Button
          onClick={() => resolve("override")}
          disabled={pending !== null}
          size="sm"
          variant={variantForOption("override", recommended)}
        >
          {pending === "override" ? "…" : "Override & continue"}
        </Button>
      </div>
      <p className="text-3xs text-tertiary">
        <strong>Decide</strong> — tell the AI what to do, then it re-runs (best
        when it's waiting on your call). <strong>Retry</strong> — run the flagged
        work again and re-check it. <strong>Override</strong> — accept the result
        as-is and move on.
      </p>
      {error && <p className="text-3xs text-error">{error}</p>}
    </div>
  );
}

function TimeoutCapInner({
  taskId,
  blocker,
  header,
  onResolve,
}: {
  taskId: string;
  blocker: SnapshotBlocker;
  header: string;
  onResolve?: (response: unknown) => void;
}) {
  const qc = useQueryClient();
  const recommended = recommendedOption(blocker.payload);
  const role = (blocker.payload?.role as string) ?? "";
  const subtaskId = (blocker.payload?.subtask_id as string) ?? "";
  const retries = (blocker.payload?.retries as number) ?? 0;
  const maxRetries = (blocker.payload?.max_retries as number) ?? 0;
  const lastTimeout = (blocker.payload?.last_timeout_s as number) ?? 0;
  const lastError = (blocker.payload?.last_error as string) ?? "";
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function resolve(action: string) {
    setPending(action);
    setError(null);
    try {
      const res = await api(blocker.endpoint, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      await qc.invalidateQueries({ queryKey: ["taskSnapshot", taskId] });
      await qc.invalidateQueries({ queryKey: ["taskState", taskId] });
      onResolve?.(res);
    } catch (e) {
      setError((e as Error).message ?? "Request failed");
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="space-y-3 rounded border border-warning/50 bg-warning-subtle/40 p-4">
      <div className="flex items-baseline justify-between">
        <div className="text-2xs uppercase tracking-wider text-warning">
          {header}
        </div>
        <WaitingFor blocker={blocker} />
      </div>
      {/* ROCK-SOLID v5 P1.5 — was blocker.summary rendered raw, ignoring
          payload.presentation (engine.py always sends one via
          humanize_gate for timeout_cap). GateBody also folds in the raw
          last-error text as a collapsed technical-details entry instead of
          a separate ad-hoc <details> block. */}
      <GateBody
        payload={blocker.payload}
        fallback={blocker.summary}
        taskId={taskId}
        extraDetails={
          lastError ? (
            <pre className="whitespace-pre-wrap break-all text-3xs text-fg-muted">
              {lastError}
            </pre>
          ) : undefined
        }
      />
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-3xs text-tertiary">
        {subtaskId && (
          <div>
            <span className="uppercase tracking-wider">{PART}</span>
            <span className="ml-2 font-mono text-fg-muted">{subtaskId}</span>
          </div>
        )}
        {role && (
          <div>
            <span className="uppercase tracking-wider">role</span>
            <span className="ml-2 font-mono text-fg-muted">{role}</span>
          </div>
        )}
        <div>
          <span className="uppercase tracking-wider">retries</span>
          <span className="ml-2 font-mono text-fg-muted">
            {retries}/{maxRetries}
          </span>
        </div>
        {lastTimeout > 0 && (
          <div>
            <span className="uppercase tracking-wider">last timeout</span>
            <span className="ml-2 font-mono text-fg-muted">{lastTimeout}s</span>
          </div>
        )}
      </div>
      {/* Visual hierarchy across the 4 options (evidence inventory #16 —
          same row, no cue which is the safe default): Extend + retry is
          the recommended default (primary, filled); Mark done / Skip are
          real but secondary choices (outline); Permanently fail stays
          destructive (red) — the one option that can't be undone by
          retrying again. */}
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() => resolve("extend_and_retry")}
          disabled={pending !== null}
          size="sm"
          variant={variantForOption("extend_and_retry", recommended)}
        >
          {pending === "extend_and_retry" ? "…" : "Extend + retry"}
        </Button>
        <Button
          onClick={() => resolve("mark_done")}
          disabled={pending !== null}
          size="sm"
          variant={variantForOption("mark_done", recommended)}
        >
          {pending === "mark_done" ? "…" : "Mark done"}
        </Button>
        <Button
          onClick={() => resolve("skip")}
          disabled={pending !== null}
          size="sm"
          variant="outline"
        >
          {pending === "skip" ? "…" : "Skip + cascade"}
        </Button>
        <Button
          onClick={() => resolve("permanent_fail")}
          disabled={pending !== null}
          size="sm"
          variant="destructive"
        >
          {pending === "permanent_fail" ? "…" : "Permanently fail"}
        </Button>
      </div>
      {error && <p className="text-3xs text-error">{error}</p>}
    </div>
  );
}

export default BlockerCard;
