import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, FileText, Paperclip, X } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { taskApi, roleApi, flowApi } from "@/lib/api";
import { peopleApi } from "@/lib/people-api";
import type { TaskMode, PhaseSummary } from "@/types/api";
import { signalThinking, signalIdle } from "@/hooks/use-orchestrator-state";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { PipelineView } from "@/components/task/pipeline-view";
import { cn } from "@/lib/utils";

/** P5.4 — mirrors DELIVERY_CHANNELS in orchestrator/api/main.py. MS formats
 *  (PPTX/DOCX/XLSX) are deliberately absent; the backend rejects them. */
const DELIVERY_CHANNELS = [
  { value: "markdown", label: "Markdown" },
  { value: "marp", label: "Slides" },
  { value: "microsite", label: "Microsite" },
  { value: "tts", label: "Audio" },
  { value: "podcast", label: "Podcast" },
];

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024; // matches backend _MAX_UPLOAD_BYTES

// Autosaved draft of the description textarea. Survives accidental dialog
// dismiss, page reload, and failed submits — users were losing long task
// descriptions when the modal unmounted. Cleared on a successful create.
const DRAFT_KEY = "okuro:create-task-draft";

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Fold the interview answers back into the task description so the backend
 * clarity gate re-assesses an enriched prompt — no backend change needed,
 * answers ride inside the description string the BE already accepts.
 *
 * Questions the user left blank are omitted, so a half-answered round still
 * produces a clean prompt. When nothing was answered the original is returned
 * unchanged (no empty "## Clarifications" heading).
 */
export function buildEnrichedDescription(
  original: string,
  questions: string[],
  answers: string[],
): string {
  const base = original.trim();
  const pairs = questions
    .map((q, i) => ({ q: q.trim(), a: (answers[i] ?? "").trim() }))
    .filter((p) => p.a.length > 0);
  if (pairs.length === 0) return base;
  const block = pairs.map((p) => `- Q: ${p.q}\n  A: ${p.a}`).join("\n");
  return `${base}\n\n## Clarifications\n${block}`;
}

interface CreateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * Prefill the description textarea when the dialog opens. Used by Now-tab
   * todo rows so the orchestrator inherits the thought's full context.
   */
  initialDescription?: string;
  /**
   * If the dialog was opened from a registered todo (Now-page click), the
   * todo id flows through here so the backend can mark it done when the
   * spawned task reaches a terminal state. Closes the todo→task→done loop.
   */
  sourceTodoId?: string;
  /**
   * If the dialog was opened from an action-item embedded in a thought
   * (Now-page Action Items click), the parent thought id flows through
   * here so the backend can mark the thought resolved on task done.
   */
  sourceThoughtId?: string;
}

export function CreateDialog({
  open,
  onOpenChange,
  initialDescription,
  sourceTodoId,
  sourceThoughtId,
}: CreateDialogProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [description, setDescription] = useState(initialDescription ?? "");

  // Re-seed when the dialog opens — each invocation may carry a different
  // todo. We reset only on the open-edge so user edits aren't blown away
  // by a background re-render of the parent.
  useEffect(() => {
    if (open) {
      let draft = "";
      try {
        draft = localStorage.getItem(DRAFT_KEY) ?? "";
      } catch {
        draft = "";
      }
      // An explicit prefill (todo / thought) always wins over a stale draft.
      setDescription(initialDescription ?? draft);
    }
  }, [open, initialDescription]);

  // Autosave the description so an accidental dismiss / reload / failed submit
  // never loses a long prompt. Only persist non-empty text so reset() (which
  // blanks the field) can't clobber a real draft.
  useEffect(() => {
    if (!open) return;
    try {
      if (description.trim()) localStorage.setItem(DRAFT_KEY, description);
    } catch {
      /* localStorage unavailable — best-effort */
    }
  }, [description, open]);
  const [mode, setMode] = useState<TaskMode>("auto-execute");
  const [dryRun, setDryRun] = useState(false);
  const [autoApprove, setAutoApprove] = useState(true);
  // F2 — autopilot: auto-answer EVERY human gate from the profile (off by
  // default; enabling shows a risk warning below the toggle grid).
  const [autopilot, setAutopilot] = useState(false);
  const [maxIntelligence, setMaxIntelligence] = useState(false);
  // Review-on-closeout is opt-IN per task while it earns back trust: it
  // shipped with three production deadlocks in one day, so the installation
  // default stays per_artifact until it has run clean on the real stdio path
  // twice. Per-task keeps each experiment's blast radius to one run.
  const [closeoutReview, setCloseoutReview] = useState(false);
  // ROCK-SOLID v5 P5.4 — Stream C recipient. The backend has read
  // `task.audience` since Stream C landed and nothing ever wrote it, so the
  // delivery fan-out could not fire once. "" is genuinely no recipient, not
  // an unset default: most tasks are not addressed to anybody.
  // P6.7 — fast-track review profile. Off by default and never inferred:
  // the failure mode of guessing wrong is shipping unreviewed work.
  const [fastTrack, setFastTrack] = useState(false);
  const [audience, setAudience] = useState("");
  const [deliveryChannel, setDeliveryChannel] = useState("markdown");
  const [recurring, setRecurring] = useState(false);
  const [recurringTitle, setRecurringTitle] = useState("");
  const [recurringRole, setRecurringRole] = useState("");
  const [recurringSchedule, setRecurringSchedule] = useState("0 6 * * *");
  const [selectedRoles, setSelectedRoles] = useState<string[]>([]);
  const [roleSearch, setRoleSearch] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<PhaseSummary[] | null>(null);
  const [previewing, setPreviewing] = useState(false);

  // Clarity-gate intake. When the backend returns status="intake_required"
  // (confidence below CONFIDENCE_THRESHOLD) no task is spawned — instead we
  // surface the clarifying questions inline and let the user refine the
  // description and resubmit. Cleared whenever the description changes so a
  // refined answer is never shown against a stale question set.
  const [intake, setIntake] = useState<{
    confidence?: number;
    questions: string[];
    hint?: string;
  } | null>(null);

  // One answer string per intake question, index-aligned to intake.questions.
  // Reset whenever a fresh question set arrives so an answer is never shown
  // against a stale question. This is the interview's working buffer.
  const [answers, setAnswers] = useState<string[]>([]);

  // How many intake rounds the user has been through. 0 = first gate. >0 lets
  // us soften the copy ("a few more details") on subsequent passes.
  const [intakeRound, setIntakeRound] = useState(0);

  // Attachments — files uploaded with the task via multipart/form-data.
  // Backend saves them under tasks/{id}/inputs/ and appends an "Attached files:"
  // block to the task description. Disabled for recurring tasks (backend
  // recurring path is JSON-only).
  const [files, setFiles] = useState<File[]>([]);
  const [uploadLoaded, setUploadLoaded] = useState(0);
  const [dragCount, setDragCount] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const thumbUrls = useMemo(() => {
    const map = new Map<number, string>();
    files.forEach((f, i) => {
      if (f.type.startsWith("image/")) map.set(i, URL.createObjectURL(f));
    });
    return map;
  }, [files]);

  useEffect(() => {
    return () => {
      thumbUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [thumbUrls]);

  const addFiles = (incoming: FileList | File[]) => {
    const oversized: string[] = [];
    const ok: File[] = [];
    for (const f of Array.from(incoming)) {
      if (f.size > MAX_UPLOAD_BYTES) {
        oversized.push(`${f.name} (${formatSize(f.size)})`);
      } else {
        ok.push(f);
      }
    }
    if (oversized.length) {
      setError(`Files exceeding 10 MB skipped: ${oversized.join(", ")}`);
    } else {
      setError("");
    }
    setFiles((prev) => [...prev, ...ok]);
  };

  const removeFile = (idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const onDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    setDragCount((c) => c + 1);
  };
  const onDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setDragCount((c) => Math.max(0, c - 1));
  };
  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault();
  };
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragCount(0);
    if (recurring) return;
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
  };

  function fileStatus(idx: number): "pending" | "uploading" | "done" {
    if (!submitting) return "pending";
    let offset = 0;
    for (let i = 0; i < idx; i++) offset += files[i]?.size ?? 0;
    const endOffset = offset + (files[idx]?.size ?? 0);
    if (uploadLoaded >= endOffset) return "done";
    if (uploadLoaded > offset) return "uploading";
    return "pending";
  }

  function fileProgress(idx: number): number {
    if (!submitting) return 0;
    let offset = 0;
    for (let i = 0; i < idx; i++) offset += files[i]?.size ?? 0;
    const size = files[idx]?.size ?? 1;
    const endOffset = offset + size;
    if (uploadLoaded >= endOffset) return 100;
    if (uploadLoaded <= offset) return 0;
    return ((uploadLoaded - offset) / size) * 100;
  }

  const activeFiles = recurring ? [] : files;

  const { data: rolesData } = useQuery({
    queryKey: ["roles"],
    queryFn: () => roleApi.list(),
    staleTime: 60_000,
    enabled: open,
  });

  const { data: flowsData } = useQuery({
    queryKey: ["flows"],
    queryFn: () => flowApi.list(),
    staleTime: 30_000,
    enabled: open,
  });
  const flows = flowsData?.flows ?? [];

  // Only fetched while the dialog is open, like the flows query above — a
  // people list on every page mount would be a request nobody asked for.
  const { data: peopleData } = useQuery({
    queryKey: ["people", "for-delivery"],
    queryFn: () => peopleApi.list(),
    staleTime: 60_000,
    enabled: open,
  });
  const people = peopleData?.people ?? [];

  const applyFlow = (flowId: string) => {
    if (flowId === "__none__") {
      setSelectedRoles([]);
      return;
    }
    const flow = flows.find((f) => f.id === flowId);
    if (flow) setSelectedRoles(flow.role_ids);
  };

  const availableRoles = (rolesData?.roles ?? []).filter(
    (r) =>
      !selectedRoles.includes(r.id) &&
      (!roleSearch || r.id.toLowerCase().includes(roleSearch.toLowerCase())),
  );

  const reset = () => {
    setDescription("");
    setMode("auto-execute");
    setDryRun(false);
    setAutoApprove(true);
    setMaxIntelligence(false);
    setRecurring(false);
    setRecurringTitle("");
    setRecurringRole("");
    setRecurringSchedule("0 6 * * *");
    setSelectedRoles([]);
    setRoleSearch("");
    setError("");
    setPreview(null);
    setPreviewing(false);
    setIntake(null);
    setAnswers([]);
    setIntakeRound(0);
    setFiles([]);
    setUploadLoaded(0);
    setDragCount(0);
  };

  const handlePreview = async () => {
    if (!description.trim()) return;
    setPreviewing(true);
    setError("");
    signalThinking("Decomposing plan…");
    try {
      const res = await taskApi.preview({
        description: description.trim(),
        required_roles: selectedRoles.length > 0 ? selectedRoles : undefined,
      });
      setPreview(res.phases);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setPreviewing(false);
      signalIdle();
    }
  };

  /**
   * Create a task.
   *
   * @param opts.descriptionOverride - send this instead of the textarea value
   *   (used by the interview to submit the enriched, answer-folded description).
   * @param opts.skipIntake - force past the clarity gate. The interview's
   *   "Submit answers" sends false so the BE RE-ASSESSES the enriched prompt
   *   (iterative interview); "Run anyway" sends true to bypass the gate.
   */
  const handleSubmit = async (opts?: {
    descriptionOverride?: string;
    skipIntake?: boolean;
  }) => {
    const payloadDescription = (opts?.descriptionOverride ?? description).trim();
    if (!payloadDescription) return;
    setSubmitting(true);
    setUploadLoaded(0);
    setError("");
    signalThinking("Creating task…");
    try {
      const res = await taskApi.create(
        {
          description: payloadDescription,
          mode,
          dry_run: dryRun,
          auto_approve: autoApprove,
          autopilot,
          intelligence: maxIntelligence ? "max" : undefined,
          review_trigger: closeoutReview ? "closeout" : undefined,
          required_roles: selectedRoles.length > 0 ? selectedRoles : undefined,
          recurring,
          recurring_title: recurring ? recurringTitle : undefined,
          recurring_role: recurring ? recurringRole : undefined,
          recurring_schedule: recurring ? recurringSchedule : undefined,
          source_todo_id: sourceTodoId,
          source_thought_id: sourceThoughtId,
          skip_intake: opts?.skipIntake ?? false,
          fast_track: fastTrack || undefined,
          audience: audience || undefined,
          delivery_channel: audience ? deliveryChannel : undefined,
        },
        activeFiles.length > 0
          ? {
              files: activeFiles,
              onProgress: (loaded) => setUploadLoaded(loaded),
            }
          : undefined,
      );
      // Clarity gate blocked the task — no task_id was spawned. Surface the
      // clarifying questions inline so the user can answer and resubmit,
      // instead of navigating to /tasks/undefined.
      if (res.status === "intake_required" || !res.task_id) {
        const questions = res.questions ?? [];
        setIntake({
          confidence: res.confidence,
          questions,
          hint: res.hint,
        });
        // Fresh question set → fresh answer buffer (never mismatch a stale Q).
        setAnswers(questions.map(() => ""));
        setIntakeRound((r) => r + 1);
        return;
      }
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["systemStatus"] });
      try {
        localStorage.removeItem(DRAFT_KEY);
      } catch {
        /* best-effort */
      }
      reset();
      onOpenChange(false);
      navigate(`/tasks/${res.task_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create task");
    } finally {
      setSubmitting(false);
      signalIdle();
    }
  };

  // Interview actions — assemble answers into the description, then submit.
  const enriched = () =>
    intake ? buildEnrichedDescription(description, intake.questions, answers) : description;

  // "Submit answers": the user has answered — PROCEED with the enriched prompt.
  // We intentionally skip re-gating: the clarity threshold is high by design, and
  // genuinely-complex tasks never converge to it no matter how much detail is
  // added, which trapped the user in an endless question loop. The answers ARE
  // the clarification; fold them in and run. (Refining further = edit the
  // description directly, which re-opens the gate.)
  const handleSubmitAnswers = () => handleSubmit({ descriptionOverride: enriched(), skipIntake: true });

  // "Run anyway": bypass the gate with whatever we have (answers folded in if
  // any were given, else the original).
  const handleRunAnyway = () => handleSubmit({ descriptionOverride: enriched(), skipIntake: true });

  if (preview) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          className="flex w-[min(calc(100vw-2rem),48rem)] flex-col overflow-hidden border-border bg-surface-elevated"
          onInteractOutside={(e) => e.preventDefault()}
        >
          <DialogHeader className="shrink-0">
            <DialogTitle className="text-fg">
              Preview plan — {preview.length} phase{preview.length === 1 ? "" : "s"},{" "}
              {preview.reduce((n, p) => n + p.subtasks.length, 0)} subtask
              {preview.reduce((n, p) => n + p.subtasks.length, 0) === 1 ? "" : "s"}
            </DialogTitle>
          </DialogHeader>

          <p className="shrink-0 text-xs text-tertiary">
            The decomposer proposes this plan. Nothing has run yet.
          </p>

          <div className="flex min-h-0 flex-1 overflow-y-auto rounded border border-border bg-surface">
            <PipelineView phases={preview} />
          </div>

          {error && <p className="shrink-0 text-xs text-error">{error}</p>}

          <div className="flex shrink-0 justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPreview(null)}
                disabled={submitting}
              >
                Back
              </Button>
              <Button
                size="sm"
                onClick={() => handleSubmit()}
                disabled={submitting}
              >
                {submitting ? "Creating..." : "Execute this plan"}
              </Button>
            </div>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="border-border bg-surface-elevated"
        onInteractOutside={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle className="text-fg">Create Task</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {/* Description */}
          <div>
            <Label className="text-fg-muted">What to accomplish</Label>
            <Textarea
              value={description}
              onChange={(e) => {
                setDescription(e.target.value);
                // A directly edited description invalidates the previous
                // question set — and its in-flight interview answers.
                if (intake) {
                  setIntake(null);
                  setAnswers([]);
                  setIntakeRound(0);
                }
              }}
              rows={6}
              placeholder="Describe the task..."
              className="mt-1 bg-surface text-fg placeholder:text-tertiary"
              autoFocus
            />
          </div>

          {/* Clarity-gate interview — when the backend needs more detail before
              it can plan well, it returns clarifying questions instead of
              spawning. Soft framing: this is a quick interview, not an error.
              Each question gets its own answer box; answers fold into the
              description on submit and the BE re-assesses. */}
          {intake && (
            <div
              data-testid="intake-panel"
              className="space-y-3 rounded border border-accent/40 bg-accent-subtle p-3"
            >
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs font-medium text-fg">
                  {intakeRound > 1
                    ? "A couple more details and we're set"
                    : "A few quick answers will help me plan this well"}
                </p>
                {submitting && (
                  <span
                    data-testid="intake-rechecking"
                    className="text-2xs text-tertiary"
                  >
                    Re-checking…
                  </span>
                )}
              </div>
              {intake.questions.length > 0 && (
                <div className="space-y-3">
                  {intake.questions.map((q, i) => (
                    <div key={i} className="space-y-1">
                      <Label className="text-2xs text-fg-muted">{q}</Label>
                      <Textarea
                        data-testid={`intake-answer-${i}`}
                        value={answers[i] ?? ""}
                        onChange={(e) =>
                          setAnswers((prev) => {
                            const next = [...prev];
                            next[i] = e.target.value;
                            return next;
                          })
                        }
                        rows={2}
                        placeholder="Your answer (optional)…"
                        className="bg-surface text-xs text-fg placeholder:text-tertiary"
                      />
                    </div>
                  ))}
                </div>
              )}
              <div className="flex flex-wrap items-center justify-end gap-2">
                <Button
                  data-testid="intake-run-anyway"
                  variant="ghost"
                  size="xs"
                  onClick={handleRunAnyway}
                  disabled={submitting}
                  title="Skip the questions and run with what we have"
                >
                  Run anyway
                </Button>
                <Button
                  data-testid="intake-submit-answers"
                  size="xs"
                  onClick={handleSubmitAnswers}
                  disabled={submitting}
                >
                  {submitting ? "Sending…" : "Submit answers"}
                </Button>
              </div>
              <p className="text-3xs text-tertiary">
                Answers fold into the task and it's re-checked. You can also edit
                the description above directly.
              </p>
            </div>
          )}

          {/* Attachments */}
          <div>
            <div className="mb-1 flex items-center justify-between gap-2">
              <Label className="text-fg-muted">
                Attachments
                <span className="ml-1 text-3xs normal-case text-tertiary">
                  (saved to inputs/ — 10 MB each max)
                </span>
              </Label>
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={() => fileInputRef.current?.click()}
                disabled={recurring || submitting}
              >
                <Paperclip className="mr-1 h-3 w-3" />
                Add files
              </Button>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                if (e.target.files?.length) addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <div
              onDragEnter={onDragEnter}
              onDragLeave={onDragLeave}
              onDragOver={onDragOver}
              onDrop={onDrop}
              className={cn(
                "rounded border border-dashed p-3 text-xs",
                dragCount > 0 && !recurring
                  ? "border-accent bg-accent-subtle text-accent"
                  : "border-border text-tertiary",
                recurring && "opacity-50",
              )}
            >
              {activeFiles.length === 0 ? (
                <div className="text-center">
                  {recurring
                    ? "Attachments unavailable for recurring tasks"
                    : "Drop files here or click Add files"}
                </div>
              ) : (
                <div className="space-y-1">
                  {activeFiles.map((f, idx) => (
                    <FileChip
                      key={`${idx}-${f.name}-${f.size}`}
                      file={f}
                      thumb={thumbUrls.get(idx)}
                      status={fileStatus(idx)}
                      progress={fileProgress(idx)}
                      onRemove={() => removeFile(idx)}
                      disabled={submitting}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Mode */}
          <div>
            <Label className="text-fg-muted">Mode</Label>
            <div className="mt-1 flex gap-2">
              <Button
                variant={mode === "auto-execute" ? "default" : "outline"}
                size="sm"
                onClick={() => setMode("auto-execute")}
                className="text-xs"
              >
                AUTO-EXECUTE
              </Button>
              <Button
                variant={mode === "deliberate" ? "default" : "outline"}
                size="sm"
                onClick={() => setMode("deliberate")}
                className="text-xs"
              >
                DELIBERATE
              </Button>
            </div>
          </div>

          {/* Toggles */}
          <div className="grid grid-cols-2 gap-3">
            <ToggleRow
              label="Dry run"
              description="Plan the task without dispatching any work — see the steps and parts it would create."
              checked={dryRun}
              onCheckedChange={setDryRun}
            />
            <ToggleRow
              label="Auto-approve"
              description="Skip approval prompts for LOW and MEDIUM risk parts. HIGH risk still waits for you."
              checked={autoApprove}
              onCheckedChange={setAutoApprove}
            />
            <ToggleRow
              label="Max intelligence"
              description="Use the strongest available model tier for every part, not just complex ones."
              checked={maxIntelligence}
              onCheckedChange={setMaxIntelligence}
            />
            <ToggleRow
              label="Review at closeout"
              description="Review once at the end instead of after every part — faster, less back-and-forth."
              checked={closeoutReview}
              onCheckedChange={setCloseoutReview}
            />
            {/* P5.4 — Stream C. Hidden entirely when there are no people to
                deliver to, rather than shown as an empty dropdown: an empty
                chooser reads as a broken feature. */}
            {people.length > 0 && (
              <div className="space-y-1.5 py-1">
                <Label className="text-fg-muted">
                  Deliver to{" "}
                  <span className="ml-1 text-3xs normal-case text-tertiary">
                    (send each result to a person as it finishes)
                  </span>
                </Label>
                <div className="flex gap-2">
                  <Select
                    value={audience || "__none__"}
                    onValueChange={(v) => setAudience(v === "__none__" ? "" : v)}
                  >
                    <SelectTrigger className="h-7 flex-1 border-border bg-surface text-2xs">
                      <SelectValue placeholder="Nobody" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__none__" className="text-2xs">
                        — Nobody —
                      </SelectItem>
                      {people.map((person) => (
                        <SelectItem key={person.id} value={person.id} className="text-2xs">
                          {person.display_name}
                          {person.organization && (
                            <span className="text-tertiary"> · {person.organization}</span>
                          )}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {/* Only meaningful with a recipient, so it only appears
                      with one. A format picker above "Nobody" is a control
                      that does nothing. */}
                  {audience && (
                    <Select value={deliveryChannel} onValueChange={setDeliveryChannel}>
                      <SelectTrigger className="h-7 w-[140px] border-border bg-surface text-2xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {DELIVERY_CHANNELS.map((c) => (
                          <SelectItem key={c.value} value={c.value} className="text-2xs">
                            {c.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </div>
              </div>
            )}
            <ToggleRow
              label="Fast track"
              description="Skip the slow AI review on low-risk parts — the automatic checks still run, and anything skipped is shown in the activity feed."
              checked={fastTrack}
              onCheckedChange={setFastTrack}
            />
            <ToggleRow
              label="Recurring"
              description="Run this task again on a schedule instead of once."
              checked={recurring}
              onCheckedChange={setRecurring}
            />
            <ToggleRow
              label="Autopilot"
              description="Answer every gate automatically from your profile — nothing waits on you, including high-risk approvals."
              checked={autopilot}
              onCheckedChange={setAutopilot}
            />
          </div>

          {/* F2 — autopilot risk warning. Autopilot answers EVERY human gate,
              including high-risk approvals + capability-gap, unattended from
              the profile. Surface the risk on enable. */}
          {autopilot && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded border border-warning/40 bg-warning-subtle/40 px-3 py-2 text-2xs text-warning"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                Autopilot answers <strong>every</strong> gate for this task —
                including high-risk approvals and capability-gap — unattended,
                using your profile. The only safeguard is that the resolver
                parks when it isn&apos;t confident. Leave off unless you want a
                fully hands-off run.
              </span>
            </div>
          )}

          {/* Role selection */}
          <div>
            <div className="mb-1 flex items-center justify-between gap-2">
              <Label className="text-fg-muted">
                Roles{" "}
                <span className="ml-1 text-3xs normal-case text-tertiary">
                  (each listed role is required in the plan)
                </span>
              </Label>
              {flows.length > 0 && (
                <Select onValueChange={applyFlow}>
                  <SelectTrigger className="h-6 w-[160px] border-border bg-surface text-2xs text-fg-muted">
                    <SelectValue placeholder="Apply flow…" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__" className="text-2xs">
                      — Start from scratch —
                    </SelectItem>
                    {flows.map((f) => (
                      <SelectItem key={f.id} value={f.id} className="text-2xs">
                        {f.name}{" "}
                        <span className="text-tertiary">({f.role_count})</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
            {selectedRoles.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1">
                {selectedRoles.map((r) => (
                  <Badge
                    key={r}
                    variant="outline"
                    className="cursor-pointer text-2xs text-accent border-accent hover:bg-error/10 hover:text-error hover:border-error"
                    onClick={() =>
                      setSelectedRoles((prev) => prev.filter((x) => x !== r))
                    }
                  >
                    {r} ×
                  </Badge>
                ))}
              </div>
            )}
            <Input
              value={roleSearch}
              onChange={(e) => setRoleSearch(e.target.value)}
              placeholder="Search roles to add..."
              className="mt-1 bg-surface text-sm text-fg placeholder:text-tertiary"
            />
            {roleSearch && availableRoles.length > 0 && (
              <div className="mt-1 max-h-32 overflow-y-auto rounded border border-border bg-surface">
                {availableRoles.slice(0, 8).map((r) => (
                  <button
                    key={r.id}
                    onClick={() => {
                      setSelectedRoles((prev) => [...prev, r.id]);
                      setRoleSearch("");
                    }}
                    className="w-full px-2 py-1 text-left text-xs text-fg-muted hover:bg-surface-elevated"
                  >
                    <span className="text-fg">{r.id}</span>
                    <span className="ml-2 text-tertiary">{r.domain}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Recurring options */}
          {recurring && (
            <div className="space-y-2 rounded border border-border p-3">
              <Input
                value={recurringTitle}
                onChange={(e) => setRecurringTitle(e.target.value)}
                placeholder="Title (e.g. Morning Research Scan)"
                className="bg-surface text-fg"
              />
              <Input
                value={recurringRole}
                onChange={(e) => setRecurringRole(e.target.value)}
                placeholder="Role (e.g. research-analyst)"
                className="bg-surface text-fg"
              />
              <Input
                value={recurringSchedule}
                onChange={(e) => setRecurringSchedule(e.target.value)}
                placeholder="Cron (e.g. 0 6 * * *)"
                className="bg-surface text-fg"
              />
            </div>
          )}

          {/* Error */}
          {error && (
            <p className="text-xs text-error">{error}</p>
          )}

          {/* Submit */}
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handlePreview}
              disabled={!description.trim() || previewing || submitting || recurring}
              title={recurring ? "Preview unavailable for recurring tasks" : "Decompose without running"}
            >
              {previewing ? "Decomposing..." : "Preview plan"}
            </Button>
            {/* When the interview is open, the resubmit actions live in the
                intake panel (Submit answers / Run anyway). Hiding this keeps
                one clear primary action on screen. */}
            {!intake && (
              <Button
                size="sm"
                onClick={() => handleSubmit()}
                disabled={!description.trim() || submitting}
              >
                {submitting ? "Creating..." : "Create"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ToggleRow({
  label,
  description,
  checked,
  onCheckedChange,
}: {
  label: string;
  /** ROCK-SOLID v5 P1.5 — one-line explanation of what the toggle does,
   * shown as a native tooltip. Evidence inventory: these toggles carried
   * bare labels with no explanation of what any of them actually does. */
  description?: string;
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between" title={description}>
      <Label className="text-xs text-fg-muted">{label}</Label>
      <Switch checked={checked} onCheckedChange={onCheckedChange} />
    </div>
  );
}

function FileChip({
  file,
  thumb,
  status,
  progress,
  onRemove,
  disabled,
}: {
  file: File;
  thumb: string | undefined;
  status: "pending" | "uploading" | "done";
  progress: number;
  onRemove: () => void;
  disabled: boolean;
}) {
  return (
    <div className="flex items-center gap-2 rounded border border-border bg-surface px-2 py-1.5 text-left">
      {thumb ? (
        <img
          src={thumb}
          alt=""
          className="h-8 w-8 shrink-0 rounded object-cover"
        />
      ) : (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-surface-elevated">
          <FileText className="h-4 w-4 text-tertiary" />
        </div>
      )}
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs text-fg">{file.name}</div>
        <div className="flex items-center gap-2 text-3xs text-tertiary">
          <span>{formatSize(file.size)}</span>
          {status === "uploading" && (
            <span className="text-accent">{Math.floor(progress)}%</span>
          )}
          {status === "done" && <span className="text-success">uploaded</span>}
        </div>
        {status === "uploading" && (
          <div className="mt-1 h-0.5 w-full overflow-hidden rounded bg-border-subtle">
            <div
              className="h-full rounded bg-accent transition-fast"
              style={{ width: `${progress}%` }}
            />
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={onRemove}
        disabled={disabled}
        className="shrink-0 rounded p-0.5 text-tertiary hover:text-error disabled:opacity-30"
        aria-label={`Remove ${file.name}`}
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}
