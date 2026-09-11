import { useEffect, useRef, useState } from "react";
import { Paperclip, X, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import type { ContinuationSuggestion, TaskStatus } from "@/types/api";
import { parseApiDate } from "@/lib/format";

// Matches backend _MAX_UPLOAD_BYTES (main.py).
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ACCEPT_TYPES =
  "image/*,application/pdf,text/markdown,.md,.txt,.csv,.json,.html";

interface ContinueBarProps {
  taskStatus: TaskStatus;
  suggestions: ContinuationSuggestion[];
  // May be async — handleSubmit awaits it so the input only clears on a
  // resolved submit and the bar can show an in-flight state. Optional files
  // are uploaded with the continuation (img/pdf/md/etc.).
  onContinue: (description: string, files?: File[]) => void | Promise<void>;
  onResume: () => void;
  onRetry: () => void;
  onSuggest: () => void;
  suggesting?: boolean;
}

// Single source of truth: which actions are available for a given task
// terminal state. Adding a new TaskStatus = add one row here; rendering
// below stays unchanged. Without this, status checks scatter across the
// component and a new state (e.g. "halted") silently loses affordances —
// exactly what happened before this refactor.
interface AvailableActions {
  retry: boolean;
  resume: boolean;
  suggest: boolean; // only meaningful when no active suggestions yet
}

function availableActions(status: TaskStatus): AvailableActions {
  switch (status) {
    case "done":
      return { retry: false, resume: false, suggest: true };
    // Terminal but incomplete: offer Retry (revive the failed subtask, which
    // un-strands everything held behind it) alongside suggestions.
    case "completed_partial":
      return { retry: true, resume: true, suggest: true };
    case "failed":
    case "blocked":
    case "halted":
      return { retry: true, resume: true, suggest: false };
    case "pending":
      return { retry: false, resume: true, suggest: false };
    default:
      return { retry: false, resume: false, suggest: false };
  }
}

const EFFORT_DOTS: Record<string, number> = {
  small: 1,
  medium: 2,
  large: 3,
};

const EFFORT_LABEL: Record<string, string> = {
  small: "Small effort",
  medium: "Medium effort",
  large: "Large effort",
};

const CATEGORY_LABEL: Record<string, string> = {
  followup: "Follow-up",
  research: "Research",
  build: "Build",
  validate: "Validate",
  business: "Business",
};

export function ContinueBar({
  taskStatus,
  suggestions,
  onContinue,
  onResume,
  onRetry,
  onSuggest,
  suggesting = false,
}: ContinueBarProps) {
  const [input, setInput] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [attachError, setAttachError] = useState("");
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [preview, setPreview] = useState<ContinuationSuggestion | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // The composer (free-text follow-up input) starts CLOSED. ContinueBar only
  // renders on a finished (not-active) task, and a permanently-open input box
  // dominated that view. Collapse it behind a compact trigger; the user opens
  // it when they actually want to add a follow-up.
  const [composerOpen, setComposerOpen] = useState(false);

  const activeSuggestions = suggestions.filter((s) => !s.dismissed);
  const actions = availableActions(taskStatus);

  // FIX C — submit reliably and give clear feedback. The prior fire-and-
  // forget cleared the input immediately and never awaited onContinue, so a
  // slow or failed POST left the user with a blank box and no signal —
  // reading as "nothing happened". Now we await the (possibly async)
  // handler, keep the typed text until it resolves, and block double-submits
  // while in flight. Errors surface via the caller's toast (runMutation),
  // so the input is preserved on failure for an easy retry.
  // Auto-grow the textarea with content (1 line → ~8 lines, then scroll).
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input, composerOpen]);

  const addFiles = (incoming: FileList | File[]) => {
    const ok: File[] = [];
    const oversized: string[] = [];
    for (const f of Array.from(incoming)) {
      if (f.size > MAX_UPLOAD_BYTES) oversized.push(f.name);
      else ok.push(f);
    }
    setAttachError(oversized.length ? `Skipped (>10 MB): ${oversized.join(", ")}` : "");
    if (ok.length) setFiles((prev) => [...prev, ...ok]);
  };
  const removeFile = (idx: number) =>
    setFiles((prev) => prev.filter((_, i) => i !== idx));

  const handleSubmit = async () => {
    const text = input.trim();
    if ((!text && files.length === 0) || submitting) return;
    if (!text) return; // a description is always required by the backend
    setSubmitting(true);
    try {
      await onContinue(text, files.length ? files : undefined);
      setInput("");
      setFiles([]);
      setAttachError("");
    } finally {
      setSubmitting(false);
    }
  };

  const handleAccept = async () => {
    if (!preview || submitting) return;
    const text = preview.suggestion_text;
    setSubmitting(true);
    try {
      await onContinue(text);
      setPreview(null);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-2 px-10 py-2">
      {/* Suggestion node cards */}
      {activeSuggestions.length > 0 && (
        <>
          <div className="text-3xs uppercase tracking-wider text-tertiary">
            Proposed next steps · click to review
          </div>
          <div className="flex flex-wrap justify-center gap-3">
            {activeSuggestions.map((s) => (
              <SuggestionNode
                key={s.id}
                suggestion={s}
                onSelect={() => setPreview(s)}
              />
            ))}
          </div>
        </>
      )}

      {/* Suggest button when status allows it and there's nothing to show yet */}
      {actions.suggest && activeSuggestions.length === 0 && (
        <Button
          variant="outline"
          size="sm"
          onClick={onSuggest}
          disabled={suggesting}
          aria-busy={suggesting}
          className="text-xs"
        >
          {suggesting ? (
            <>
              <span
                className="mr-1.5 inline-block h-2.5 w-2.5 animate-spin rounded-full border border-current border-t-transparent"
                aria-hidden="true"
              />
              Thinking…
            </>
          ) : (
            "Suggest next steps"
          )}
        </Button>
      )}

      {/* Retry / Resume row — visible when at least one is available */}
      {(actions.retry || actions.resume) && (
        <div className="flex gap-2">
          {actions.retry && (
            <Button size="sm" variant="outline" onClick={onRetry} className="text-xs">
              Retry
            </Button>
          )}
          {actions.resume && (
            <Button size="sm" variant="outline" onClick={onResume} className="text-xs">
              Resume
            </Button>
          )}
        </div>
      )}

      {/* Custom input — collapsed by default behind a trigger so a finished
          task's process view isn't dominated by an always-open input box. */}
      {composerOpen ? (
        <div
          className={cn(
            "rounded border bg-surface transition-colors",
            dragging ? "border-accent ring-1 ring-accent/40" : "border-border",
          )}
          onDragEnter={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragOver={(e) => e.preventDefault()}
          onDragLeave={(e) => {
            e.preventDefault();
            setDragging(false);
          }}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
          }}
        >
          <Textarea
            ref={textareaRef}
            autoFocus
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPaste={(e) => {
              const fs = Array.from(e.clipboardData.files);
              if (fs.length) {
                e.preventDefault();
                addFiles(fs);
              }
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void handleSubmit();
              } else if (e.key === "Escape" && !input.trim() && files.length === 0) {
                setComposerOpen(false);
              }
            }}
            disabled={submitting}
            placeholder="Continue with…  (Enter to send · Shift+Enter for newline · attach or drop files)"
            className="max-h-[200px] resize-none border-0 bg-transparent text-sm text-fg placeholder:text-tertiary focus-visible:ring-0"
          />

          {/* Attachment chips */}
          {files.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-2 pb-1.5">
              {files.map((f, i) => (
                <span
                  key={`${f.name}-${i}`}
                  className="flex items-center gap-1 rounded border border-border bg-surface-elevated px-1.5 py-0.5 text-3xs text-fg-muted"
                >
                  <FileText className="h-3 w-3 shrink-0" aria-hidden="true" />
                  <span className="max-w-[140px] truncate">{f.name}</span>
                  <button
                    type="button"
                    onClick={() => removeFile(i)}
                    disabled={submitting}
                    className="rounded p-0.5 hover:text-error focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-error/50"
                    aria-label={`Remove ${f.name}`}
                  >
                    <X className="h-2.5 w-2.5" aria-hidden="true" />
                  </button>
                </span>
              ))}
            </div>
          )}

          {/* Action row */}
          <div className="flex items-center gap-2 border-t border-border/60 px-2 py-1.5">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPT_TYPES}
              className="hidden"
              onChange={(e) => {
                if (e.target.files?.length) addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={submitting}
              className="rounded p-1 text-fg-muted transition-colors hover:bg-surface-elevated hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
              aria-label="Attach files"
              title="Attach img / pdf / md / etc. (10 MB each)"
            >
              <Paperclip className="h-4 w-4" aria-hidden="true" />
            </button>
            {attachError && (
              <span className="truncate text-3xs text-error">{attachError}</span>
            )}
            <Button
              size="sm"
              className="ml-auto"
              onClick={() => void handleSubmit()}
              disabled={!input.trim() || submitting}
              aria-busy={submitting}
            >
              {submitting ? (
                <>
                  <span
                    className="mr-1.5 inline-block h-2.5 w-2.5 animate-spin rounded-full border border-current border-t-transparent"
                    aria-hidden="true"
                  />
                  Sending…
                </>
              ) : (
                "Go"
              )}
            </Button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setComposerOpen(true)}
          className="w-full rounded border border-dashed border-border bg-surface/40 px-3 py-2 text-left text-xs text-tertiary transition-colors hover:border-fg-muted hover:text-fg-muted"
        >
          + Continue with a follow-up…
        </button>
      )}

      {/* Detail modal */}
      <Dialog open={!!preview} onOpenChange={(o) => !o && setPreview(null)}>
        {preview && (
          <SuggestionDetailDialog
            suggestion={preview}
            onAccept={handleAccept}
            onCancel={() => setPreview(null)}
          />
        )}
      </Dialog>
    </div>
  );
}

/**
 * Suggestion node — mirrors SubtaskCard visual, but neutral/gray to signal
 * "not yet in the plan". Clicking opens the detail modal.
 */
function SuggestionNode({
  suggestion,
  onSelect,
}: {
  suggestion: ContinuationSuggestion;
  onSelect: () => void;
}) {
  const effortDots = EFFORT_DOTS[suggestion.effort] ?? 1;
  const categoryLabel =
    CATEGORY_LABEL[suggestion.category] ?? suggestion.category;

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className={cn(
        "relative cursor-pointer overflow-hidden rounded border border-dashed border-border bg-surface-elevated/50 pl-3 pr-3 py-2 text-left transition-all min-w-[180px] max-w-[220px]",
        "hover:border-fg-muted hover:bg-surface-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
      )}
    >
      {/* Gray rail — visually matches SubtaskCard but neutral */}
      <div className="absolute left-0 top-0 h-full w-1 bg-tertiary/40" />

      {/* Header row */}
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-2xs font-medium text-fg-muted">
          {categoryLabel}
        </span>
        <span className="text-3xs font-bold uppercase tracking-wider text-tertiary">
          Proposed
        </span>
      </div>

      {/* Body preview */}
      <div className="mt-1 line-clamp-2 text-3xs text-fg-muted">
        {suggestion.suggestion_text}
      </div>

      {/* Meta row */}
      <div className="mt-1.5 flex items-center justify-between gap-2 border-t border-border/40 pt-1.5">
        {suggestion.source_role ? (
          <span className="truncate text-3xs text-tertiary">
            {suggestion.source_role}
          </span>
        ) : (
          <span />
        )}
        <span
          className="text-3xs text-tertiary"
          title={EFFORT_LABEL[suggestion.effort] ?? suggestion.effort}
        >
          {"\u25CF".repeat(effortDots)}
          <span className="text-tertiary/40">
            {"\u25CF".repeat(3 - effortDots)}
          </span>
        </span>
      </div>
    </div>
  );
}

function SuggestionDetailDialog({
  suggestion,
  onAccept,
  onCancel,
}: {
  suggestion: ContinuationSuggestion;
  onAccept: () => void;
  onCancel: () => void;
}) {
  const categoryLabel =
    CATEGORY_LABEL[suggestion.category] ?? suggestion.category;
  const effortLabel = EFFORT_LABEL[suggestion.effort] ?? suggestion.effort;

  return (
    <DialogContent className="max-w-lg">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <span className="rounded border border-border px-1.5 py-0.5 text-3xs font-bold uppercase tracking-wider text-fg-muted">
            {categoryLabel}
          </span>
          <span>Proposed next step</span>
        </DialogTitle>
        <DialogDescription className="sr-only">
          Review the suggestion before adding it to the task.
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-3 text-sm">
        <div className="rounded border border-border bg-surface-elevated p-3 text-fg">
          {suggestion.suggestion_text}
        </div>

        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-2xs uppercase tracking-wider text-tertiary">
            Effort
          </dt>
          <dd className="text-fg-muted">{effortLabel}</dd>

          {suggestion.source_role && (
            <>
              <dt className="text-2xs uppercase tracking-wider text-tertiary">
                Proposed by
              </dt>
              <dd className="text-fg-muted">{suggestion.source_role}</dd>
            </>
          )}

          {suggestion.timestamp && (
            <>
              <dt className="text-2xs uppercase tracking-wider text-tertiary">
                When
              </dt>
              <dd className="text-fg-muted">
                {parseApiDate(suggestion.timestamp).toLocaleString()}
              </dd>
            </>
          )}
        </dl>
      </div>

      <DialogFooter>
        <Button variant="outline" size="sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="sm" onClick={onAccept}>
          Accept and continue
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
