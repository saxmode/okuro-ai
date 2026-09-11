import { useState } from "react";
import { Square, AlertCircle, Loader2, CircleDot } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";
import type {
  InlineSessionView,
  SessionState,
} from "@/types/inline";
import type { SessionStatus } from "@/lib/inline-api";

const STATE_TONE: Record<
  SessionState,
  "success" | "warning" | "error" | "info" | "neutral" | "accent"
> = {
  running: "accent",
  paused: "warning",
  done: "neutral",
  cancelled: "neutral",
  error: "error",
};

const STATE_LABEL: Record<SessionState, string> = {
  running: "Running",
  paused: "Paused",
  done: "Done",
  cancelled: "Cancelled",
  error: "Error",
};

interface SessionHeaderProps {
  sessionId: string;
  status?: SessionStatus | null;
  view: InlineSessionView;
  onCancel: () => Promise<void> | void;
  cancelling?: boolean;
}

/**
 * Sticky header for the inline chat page.
 *
 * Layout: provider · model · todo title · token counts · status pill on
 * the left, connection dot + cancel button on the right. The cancel
 * button is a two-stage confirm — first click arms, second click fires.
 */
export function SessionHeader({
  sessionId,
  status,
  view,
  onCancel,
  cancelling,
}: SessionHeaderProps) {
  const [armed, setArmed] = useState(false);
  const state = view.state;
  const terminal = state === "done" || state === "cancelled" || state === "error";

  return (
    <header className="sticky top-0 z-20 border-b border-border-subtle bg-surface/95 backdrop-blur">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-6 gap-y-2 px-8 py-4">
        <div className="flex min-w-0 flex-1 items-center gap-x-6 gap-y-1 flex-wrap">
          <Field label="Session" value={shortId(sessionId)} mono />
          {status?.provider ? (
            <Field label="Provider" value={status.provider} />
          ) : null}
          {status?.model ? <Field label="Model" value={status.model} /> : null}
          {status?.todo_title ? (
            <Field
              label="Todo"
              value={status.todo_title}
              className="min-w-0 max-w-[28ch]"
              truncate
            />
          ) : null}
          <Field
            label="Tokens"
            value={`${view.usage.input_tokens}↓ / ${view.usage.output_tokens}↑`}
          />
        </div>

        <div className="flex items-center gap-3">
          <StatusBadge
            tone={STATE_TONE[state]}
            label={STATE_LABEL[state]}
            showDot
          />
          <ConnectionDot connected={view.connected} terminal={terminal} />
          {!terminal && (
            <Button
              variant={armed ? "destructive" : "outline"}
              size="sm"
              onClick={async () => {
                if (!armed) {
                  setArmed(true);
                  setTimeout(() => setArmed(false), 3_000);
                  return;
                }
                await onCancel();
                setArmed(false);
              }}
              disabled={cancelling}
              aria-label={armed ? "Confirm cancel" : "Cancel session"}
            >
              {cancelling ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <Square className="h-3.5 w-3.5" aria-hidden />
              )}
              {armed ? "Confirm cancel" : "Cancel"}
            </Button>
          )}
        </div>
      </div>
      {view.last_error ? (
        <div className="border-t border-error/30 bg-error/10 px-8 py-2 text-xs text-error">
          <AlertCircle className="-mt-0.5 mr-2 inline-block h-3.5 w-3.5" aria-hidden />
          {view.last_error.code}: {view.last_error.message}
        </div>
      ) : null}
    </header>
  );
}

function Field({
  label,
  value,
  mono,
  className,
  truncate,
}: {
  label: string;
  value: string;
  mono?: boolean;
  className?: string;
  truncate?: boolean;
}) {
  return (
    <div className={cn("flex items-baseline gap-1.5 min-w-0", className)}>
      <span className="text-3xs uppercase tracking-widest text-tertiary">
        {label}
      </span>
      <span
        className={cn(
          "text-xs text-fg",
          mono && "font-mono",
          truncate && "truncate",
        )}
      >
        {value}
      </span>
    </div>
  );
}

function ConnectionDot({
  connected,
  terminal,
}: {
  connected: boolean;
  terminal: boolean;
}) {
  if (terminal) return null;
  return (
    <span
      title={connected ? "Streaming" : "Reconnecting..."}
      aria-label={connected ? "Streaming" : "Reconnecting"}
      className="inline-flex items-center"
    >
      <CircleDot
        className={cn(
          "h-3 w-3",
          connected ? "text-success" : "text-warning animate-pulse",
        )}
        aria-hidden
      />
    </span>
  );
}

function shortId(id: string): string {
  // ULIDs are 26 chars — first 8 is plenty for human identification
  // while staying mono-narrow on the header line.
  return id.length > 10 ? `${id.slice(0, 8)}…` : id;
}
