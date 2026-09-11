import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  Loader2,
  Check,
  X,
  Lightbulb,
  HardDrive,
  Code2,
  Layers,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { SectionLabel } from "@/components/ui/section-label";
import { toast } from "@/components/ui/toast";
import { PageHeader } from "@/components/shell/page-header";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatAge } from "@/lib/format";

/**
 * /lessons — the human half of the self-improvement loop.
 *
 * Mining finds defects that recur across a cluster of sessions and writes them
 * as CANDIDATE lessons. Corroboration proves the defect is real; it does not
 * prove the proposed remedy is right, and an active lesson is a rule okuro
 * applies to itself. So candidate → active is a decision a person makes, and
 * this page is one of the three places they can make it — the CLI (`okuro
 * distill`) and the MCP tools being the other two.
 *
 * This page holds NO rules of its own. Approve and reject post to
 * /api/lessons/{id}/{approve,reject}, which call the same functions the CLI
 * drives, over a schema where an unapproved active lesson is unrepresentable
 * (migration 143). Approving here cannot force a thin lesson through any more
 * than approving on the CLI can: it records the decision, and the next
 * maintenance pass promotes it only if corroboration and the dwell clock also
 * allow. The buttons cannot cheat the gate, so they do not try to explain it.
 *
 * The routing badge likewise arrives ON the row from the backend
 * (classify_lesson_routing) rather than being computed here — three front-ends
 * deriving it separately is three answers to one question.
 */

// ── Types ────────────────────────────────────────────────────────────

type LessonStatus = "candidate" | "active" | "retired";

type Routing = {
  routing: "local" | "code_surface";
  label: string;
  known_surface: boolean;
};

type Evidence = {
  session_id: string;
  snippet: string;
};

type Lesson = {
  id: number;
  lesson_text: string;
  lesson_class: string | null;
  target_surface: string | null;
  target_ref: string | null;
  cluster_id: number | null;
  corroboration_count: number;
  markers: string[];
  status: LessonStatus;
  created_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  retired_reason: string | null;
  evidence: Evidence[];
  evidence_session_count: number;
  routing: Routing;
};

// ── API ──────────────────────────────────────────────────────────────

const lessonsApi = {
  list: (status: LessonStatus) =>
    api<{ status: LessonStatus; lessons: Lesson[] }>(
      `/api/lessons?status=${status}&limit=100`,
    ),
  approve: ({ id, approvedBy }: { id: number; approvedBy: string }) =>
    api<{ lesson_id: number; approved_by: string; note: string }>(
      `/api/lessons/${id}/approve`,
      { method: "POST", body: JSON.stringify({ approved_by: approvedBy }) },
    ),
  reject: ({ id, reason, rejectedBy }: { id: number; reason: string; rejectedBy: string }) =>
    api<{ lesson_id: number; status: string; retired_reason: string }>(
      `/api/lessons/${id}/reject`,
      {
        method: "POST",
        body: JSON.stringify({ reason, rejected_by: rejectedBy }),
      },
    ),
};

// ── Routing badge ────────────────────────────────────────────────────

/**
 * Where approving this lesson actually lands.
 *
 * The distinction a reviewer cannot get from the lesson text: a role-pitfall or
 * profile lesson is a row in his own database and is finished when he clicks;
 * a bootstrap / middleware / brief-template lesson names a defect in okuro
 * itself and approving it only records that the defect is real.
 *
 * Colour follows that meaning rather than any severity: local is the accent
 * (this is yours, it is done), code surface is warning-toned (something else
 * has to happen). Text comes from the backend so the two never disagree.
 */
function RoutingBadge({ routing }: { routing: Routing }) {
  const isLocal = routing.routing === "local";
  const Icon = isLocal ? HardDrive : Code2;
  return (
    <Badge
      className={cn(
        "gap-1",
        isLocal ? "bg-accent/15 text-accent" : "bg-warning/15 text-warning",
      )}
      title={
        isLocal
          ? "Approving this changes your own okuro data. Nothing else needed."
          : "Approving this records the decision. The fix is a change to okuro itself."
      }
    >
      <Icon className="size-3" />
      {routing.label}
      {!routing.known_surface && (
        <span
          className="opacity-70"
          title="This surface is not in the classifier's map — routed to the code side by the fail-closed default."
        >
          ?
        </span>
      )}
    </Badge>
  );
}

// ── One lesson ───────────────────────────────────────────────────────

function LessonRow({
  lesson,
  onApprove,
  onReject,
  busy,
}: {
  lesson: Lesson;
  onApprove?: (l: Lesson) => void;
  onReject?: (l: Lesson) => void;
  busy: boolean;
}) {
  const [open, setOpen] = useState(false);
  const decidable = lesson.status === "candidate";
  const hiddenEvidence = lesson.evidence_session_count - lesson.evidence.length;

  return (
    <div className="flex flex-col px-4 py-3">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-start gap-2 text-left focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
        >
          <ChevronRight
            size={14}
            className={cn(
              "mt-1 shrink-0 text-tertiary transition-transform duration-200",
              open && "rotate-90",
            )}
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs text-tertiary">#{lesson.id}</span>
              {lesson.lesson_class && (
                <Badge variant="outline">{lesson.lesson_class}</Badge>
              )}
              <RoutingBadge routing={lesson.routing} />
            </div>
            <p className={cn("mt-1 text-sm text-fg", !open && "line-clamp-2")}>
              {lesson.lesson_text}
            </p>
            <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-tertiary">
              <span className="font-mono">
                {lesson.target_surface ?? "?"}/
                {lesson.target_ref ?? "(surface-wide)"}
              </span>
              <span
                className="inline-flex items-center gap-1"
                title="Distinct sessions that asserted this defect"
              >
                <Layers className="size-3" />
                {lesson.corroboration_count}×
              </span>
              {lesson.cluster_id !== null && (
                <span>cluster {lesson.cluster_id}</span>
              )}
              {lesson.created_at && <span>{formatAge(lesson.created_at)}</span>}
            </div>
          </div>
        </button>

        {decidable && (
          <div className="flex shrink-0 items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              className="gap-1 text-accent hover:bg-accent/10"
              disabled={busy}
              onClick={() => onApprove?.(lesson)}
              title="Approve — records your decision; activation still needs corroboration and the dwell clock"
            >
              <Check className="size-4" /> Approve
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="gap-1 text-error hover:bg-error/10"
              disabled={busy}
              onClick={() => onReject?.(lesson)}
              title="Reject — retires it, with your reason on the row"
            >
              <X className="size-4" /> Reject
            </Button>
          </div>
        )}
      </div>

      {open && (
        <div className="mt-3 ml-6 flex flex-col gap-3 border-l border-border pl-4">
          {lesson.markers.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <SectionLabel as="div" variant="micro">
                markers
              </SectionLabel>
              {lesson.markers.map((m) => (
                <Badge key={m} variant="ghost" className="font-mono">
                  {m}
                </Badge>
              ))}
            </div>
          )}

          <div className="flex flex-col gap-2">
            <SectionLabel as="h4">
              Evidence
              <span className="ml-1.5 font-normal text-tertiary">
                {lesson.evidence_session_count} session
                {lesson.evidence_session_count === 1 ? "" : "s"}
              </span>
            </SectionLabel>
            {lesson.evidence.length === 0 ? (
              <p className="text-xs text-tertiary">
                No failure phrase was recorded for these sessions.
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {lesson.evidence.map((ev) => (
                  <li key={ev.session_id} className="flex flex-col gap-0.5">
                    <span className="truncate font-mono text-2xs text-tertiary">
                      {ev.session_id}
                    </span>
                    <p className="text-xs text-fg-muted">
                      {ev.snippet || "(no failure phrase recorded)"}
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {hiddenEvidence > 0 && (
              <p
                className="text-xs text-tertiary"
                title="The review surface is bounded on purpose — an unbounded one would rebuild the transcripts the distill pipeline exists to delete."
              >
                … and {hiddenEvidence} more evidence session
                {hiddenEvidence === 1 ? "" : "s"}, not shown
              </p>
            )}
          </div>

          {lesson.approved_by && (
            <p className="text-xs text-tertiary">
              approved by{" "}
              <span className="text-fg">{lesson.approved_by}</span>
              {lesson.approved_at ? ` · ${formatAge(lesson.approved_at)}` : ""}
            </p>
          )}
          {lesson.retired_reason && (
            <p className="text-xs text-tertiary">{lesson.retired_reason}</p>
          )}
        </div>
      )}
    </div>
  );
}

// ── A queue ──────────────────────────────────────────────────────────

/**
 * `active` and `retired` mount collapsed and only fetch when opened.
 *
 * Both queues are unbounded backlogs nobody opens the page to read — the
 * candidates are why you came. Fetching them eagerly would spend the request
 * and the scroll on the two sections that are not the job.
 */
function CollapsedQueue({
  status,
  title,
  description,
}: {
  status: LessonStatus;
  title: string;
  description: string;
}) {
  const [open, setOpen] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ["lessons", status],
    queryFn: () => lessonsApi.list(status),
    enabled: open,
  });
  const lessons = data?.lessons ?? [];

  return (
    <section className="flex flex-col gap-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-fit items-center gap-1.5 text-left focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
      >
        <ChevronRight
          size={12}
          className={cn(
            "text-fg-subtle transition-transform duration-200",
            open && "rotate-90",
          )}
        />
        <SectionLabel as="div">{title}</SectionLabel>
        {open && isLoading && (
          <Loader2 className="size-3 animate-spin text-tertiary" />
        )}
        {open && !isLoading && (
          <span className="text-xs text-tertiary">({lessons.length})</span>
        )}
      </button>

      {open && !isLoading && (
        lessons.length === 0 ? (
          <p className="text-xs text-tertiary">{description}</p>
        ) : (
          <div className="flex flex-col divide-y divide-border rounded-lg border border-border">
            {lessons.map((l) => (
              <LessonRow key={l.id} lesson={l} busy={false} />
            ))}
          </div>
        )
      )}
    </section>
  );
}

// ── Page ─────────────────────────────────────────────────────────────

export function LessonsPage() {
  const qc = useQueryClient();
  const [approving, setApproving] = useState<Lesson | null>(null);
  const [rejecting, setRejecting] = useState<Lesson | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["lessons", "candidate"],
    queryFn: () => lessonsApi.list("candidate"),
  });
  const candidates = data?.lessons ?? [];

  // Every decision moves a row between queues, so all three are invalidated —
  // an approved lesson that still shows as a candidate is the page lying about
  // a decision the user just made.
  const invalidate = () => qc.invalidateQueries({ queryKey: ["lessons"] });

  const approveMut = useMutation({
    mutationFn: lessonsApi.approve,
    onSuccess: (r) => {
      toast.success(`Lesson #${r.lesson_id} approved — ${r.note}`);
      setApproving(null);
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "approve failed"),
  });

  const rejectMut = useMutation({
    mutationFn: lessonsApi.reject,
    onSuccess: (r) => {
      toast.success(`Lesson #${r.lesson_id} retired`);
      setRejecting(null);
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "reject failed"),
  });

  const busy = approveMut.isPending || rejectMut.isPending;

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Lessons"
        subtitle="Defects mined from your own sessions. None is in force until you approve it."
      />

      <section className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <SectionLabel as="h2">Awaiting your decision</SectionLabel>
          {!isLoading && (
            <span className="text-xs text-tertiary">({candidates.length})</span>
          )}
        </div>

        {isLoading ? (
          <div className="flex justify-center py-16 text-tertiary">
            <Loader2 className="size-5 animate-spin" />
          </div>
        ) : candidates.length === 0 ? (
          <EmptyState
            icon={<Lightbulb className="size-6" />}
            title="Nothing awaiting review"
            description="Mining writes candidates weekly. If you expected some, check distill.mining_enabled and tier-1 coverage."
          />
        ) : (
          <div className="flex flex-col divide-y divide-border rounded-lg border border-border">
            {candidates.map((l) => (
              <LessonRow
                key={l.id}
                lesson={l}
                busy={busy}
                onApprove={setApproving}
                onReject={setRejecting}
              />
            ))}
          </div>
        )}
      </section>

      <CollapsedQueue
        status="active"
        title="In force"
        description="No lesson has been activated yet."
      />
      <CollapsedQueue
        status="retired"
        title="Retired"
        description="Nothing has been rejected or retired on evidence."
      />

      <ApproveDialog
        lesson={approving}
        onOpenChange={(o) => !o && setApproving(null)}
        submitting={approveMut.isPending}
        onConfirm={(approvedBy) =>
          approving && approveMut.mutate({ id: approving.id, approvedBy })
        }
      />

      {/* Keyed on the lesson so the fields cannot survive into the next one.
          A reason carried over from a different lesson is precisely the
          "cannot tell why this was retired" failure the required reason
          exists to prevent — and closing after a successful reject sets
          `rejecting` to null directly, which never calls onOpenChange. */}
      <RejectDialog
        key={rejecting?.id ?? "none"}
        lesson={rejecting}
        onOpenChange={(o) => !o && setRejecting(null)}
        submitting={rejectMut.isPending}
        onConfirm={(reason, rejectedBy) =>
          rejecting && rejectMut.mutate({ id: rejecting.id, reason, rejectedBy })
        }
      />
    </div>
  );
}

// ── Approve dialog ───────────────────────────────────────────────────

const FIELD =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring";

/**
 * Approval takes a name because the row records WHO decided.
 *
 * The backend refuses automation-shaped approvers (daemon, cron, agent, …) —
 * that check is not repeated here. A field that pre-validated would drift from
 * the list it copied, and the honest position is the one the migration takes:
 * this cannot verify a human typed the name, it can only make every cheap
 * forgery impossible.
 *
 * Deliberately NOT keyed on the lesson: the name persists across the queue, so
 * reviewing twenty-four candidates costs one typing of it. Safe here for the
 * reason it is unsafe on the reject dialog — the approver is a property of the
 * person, the rejection reason is a property of the lesson.
 */
function ApproveDialog({
  lesson,
  onOpenChange,
  submitting,
  onConfirm,
}: {
  lesson: Lesson | null;
  onOpenChange: (o: boolean) => void;
  submitting: boolean;
  onConfirm: (approvedBy: string) => void;
}) {
  const [approvedBy, setApprovedBy] = useState("");

  return (
    <Dialog open={!!lesson} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Approve lesson #{lesson?.id}?</DialogTitle>
          <DialogDescription>
            Records your decision. The next maintenance pass activates it only
            if corroboration and the dwell clock also allow — approving an
            under-corroborated lesson does not force it through.
          </DialogDescription>
        </DialogHeader>

        {lesson && (
          <div className="flex flex-col gap-3 py-2">
            <p className="text-sm text-fg-muted">{lesson.lesson_text}</p>
            <RoutingBadge routing={lesson.routing} />
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-tertiary">
                Who is approving?{" "}
                <span className="opacity-60">A person, not a daemon.</span>
              </span>
              <input
                className={FIELD}
                value={approvedBy}
                onChange={(e) => setApprovedBy(e.target.value)}
                autoFocus
              />
            </label>
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => onConfirm(approvedBy)}
            disabled={submitting || !approvedBy.trim()}
            className="gap-2"
          >
            {submitting && <Loader2 className="size-4 animate-spin" />}
            Approve
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Reject dialog ────────────────────────────────────────────────────

/**
 * A reason is required, and the button stays disabled without one.
 *
 * That is a convenience, not the rule: the backend refuses an empty reason on
 * its own, because a retired row with no reason cannot be told apart from one
 * the lifecycle retired on evidence, and the next reader cannot tell whether a
 * human decided or a counter did.
 */
function RejectDialog({
  lesson,
  onOpenChange,
  submitting,
  onConfirm,
}: {
  lesson: Lesson | null;
  onOpenChange: (o: boolean) => void;
  submitting: boolean;
  onConfirm: (reason: string, rejectedBy: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [rejectedBy, setRejectedBy] = useState("");

  return (
    // No reset here — the caller keys this component on the lesson id, which
    // clears the fields on every path, including the one onOpenChange misses.
    <Dialog open={!!lesson} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Reject lesson #{lesson?.id}?</DialogTitle>
          <DialogDescription>
            Retires it with your reason on the row. Retirement is terminal — a
            defect that recurs is re-learned by mining as a new row with its own
            evidence.
          </DialogDescription>
        </DialogHeader>

        {lesson && (
          <div className="flex flex-col gap-3 py-2">
            <p className="text-sm text-fg-muted">{lesson.lesson_text}</p>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-tertiary">
                Why should this not become a rule?
              </span>
              <textarea
                className={cn(FIELD, "min-h-20 resize-y")}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                autoFocus
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-tertiary">
                Who is rejecting? <span className="opacity-60">(optional)</span>
              </span>
              <input
                className={FIELD}
                value={rejectedBy}
                onChange={(e) => setRejectedBy(e.target.value)}
              />
            </label>
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={() => onConfirm(reason, rejectedBy)}
            disabled={submitting || !reason.trim()}
            className="gap-2"
          >
            {submitting && <Loader2 className="size-4 animate-spin" />}
            Reject
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
