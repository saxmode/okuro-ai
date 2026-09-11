/**
 * Solve button + modal — opens a resolution path for a todo.
 *
 * Two paths:
 *   - Direct session (claude / gemini / codex) → mints an inline session
 *     and opens it inside a right-anchored slide-over panel on the
 *     same window. No popup. The session-bearer never enters the URL
 *     because it is passed directly to the panel as a prop.
 *   - Orchestrator → spawns the engine; toast carries the link.
 *
 * Concurrency: backend enforces one active resolution per todo —
 * second submit returns 409, which we surface via the toast.
 *
 * Reopen — two layers:
 *   1. Same-tab cache: while the SolveButton instance is mounted we
 *      remember the minted ``{session_id, bearer}`` so closing the
 *      panel can be undone with a follow-up click without a network
 *      round-trip.
 *   2. Cross-reload (wave 5): when the parent supplies
 *      ``claimedSessionId`` (from /api/todos.claimed_session_id) we
 *      treat the button as claim-aware. On click we POST
 *      /api/sessions/{id}/reissue-bearer to mint a fresh bearer, then
 *      open the slide-over panel. On 404/409 we fall back to the
 *      standard Solve dialog (session is gone or terminal).
 */

import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2, MessageSquare, Wand2 } from "lucide-react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Segmented } from "@/components/ui/segmented";
import { FieldLabel } from "@/components/ui/form-primitives";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { SolveSessionPanel } from "@/components/inline/solve-session-panel";
import {
  solveApi,
  type SolveMode,
  type SolveProvider,
  type SolveResult,
} from "@/lib/solve-api";

const MODE_OPTIONS: Array<{ value: SolveMode; label: string }> = [
  { value: "session", label: "Direct session" },
  { value: "orchestrator", label: "Orchestrator" },
];

const PROVIDER_OPTIONS: Array<{ value: SolveProvider; label: string }> = [
  { value: "claude", label: "Claude" },
  { value: "gemini", label: "Gemini" },
  { value: "codex", label: "Codex" },
];

interface SolveButtonProps {
  todoId: string;
  /**
   * If the parent already knows the todo has an in-flight session
   * (from /api/todos.claimed_session_id), passing it here switches the
   * button into "Open session" mode. On click the component mints a
   * fresh bearer via /api/sessions/{id}/reissue-bearer and opens the
   * inline panel. Survives page reload; the in-memory bearer cache is
   * still used to skip the round-trip within the same tab.
   */
  claimedSessionId?: string | null;
  disabled?: boolean;
  onResolved?: (result: SolveResult) => void;
}

interface ActiveSession {
  sessionId: string;
  bearer: string;
}

export function SolveButton({
  todoId,
  claimedSessionId,
  disabled,
  onResolved,
}: SolveButtonProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [active, setActive] = useState<ActiveSession | null>(null);
  const [reissuing, setReissuing] = useState(false);

  // Claim-aware: the parent says this todo has a live session even if
  // we have no in-memory bearer for it. Show "Open session" and route
  // clicks through the reissue endpoint instead of the Solve dialog.
  const hasInMemorySession = active !== null;
  const hasClaimedSession =
    !hasInMemorySession && Boolean(claimedSessionId);
  const showOpenSessionLabel = hasInMemorySession || hasClaimedSession;

  const handleResolved = (res: SolveResult) => {
    if (res.mode === "session") {
      setActive({ sessionId: res.session_id, bearer: res.bearer });
      setPanelOpen(true);
    }
    onResolved?.(res);
  };

  const reopen = async () => {
    if (hasInMemorySession) {
      setPanelOpen(true);
      return;
    }
    if (hasClaimedSession && claimedSessionId) {
      // Cross-reload reattach: mint a fresh bearer for the still-running
      // session. On 404/409 the session is gone/terminal — fall back to
      // the Solve dialog so the user can start a new one.
      setReissuing(true);
      try {
        const res = await solveApi.reissueBearer(claimedSessionId);
        setActive({ sessionId: claimedSessionId, bearer: res.bearer });
        setPanelOpen(true);
      } catch (err) {
        const status = err instanceof ApiError ? err.status : 0;
        if (status === 404 || status === 409) {
          toast.info("Session no longer active", {
            description: "Starting a fresh resolution.",
          });
          setDialogOpen(true);
        } else {
          toast.error("Couldn't reopen session", {
            description:
              err instanceof Error ? err.message : "Unknown error",
          });
        }
      } finally {
        setReissuing(false);
      }
      return;
    }
    setDialogOpen(true);
  };

  return (
    <>
      <Button
        size="sm"
        variant={showOpenSessionLabel ? "default" : "outline"}
        onClick={() => {
          void reopen();
        }}
        disabled={disabled || reissuing}
        title={
          showOpenSessionLabel
            ? "Open the running session for this todo"
            : "Solve — open a session or orchestrator on this todo"
        }
        data-testid={`todo-solve-${todoId}`}
      >
        {reissuing ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        ) : showOpenSessionLabel ? (
          <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
        ) : (
          <Wand2 className="h-3.5 w-3.5" aria-hidden="true" />
        )}
        {showOpenSessionLabel ? "Open session" : "Solve"}
      </Button>
      <SolveDialog
        todoId={todoId}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onResolved={handleResolved}
      />
      <SolveSessionPanel
        open={panelOpen}
        onOpenChange={setPanelOpen}
        sessionId={active?.sessionId ?? null}
        bearer={active?.bearer ?? null}
      />
    </>
  );
}

interface SolveDialogProps {
  todoId: string;
  open: boolean;
  onOpenChange: (next: boolean) => void;
  onResolved?: (result: SolveResult) => void;
}

export function SolveDialog({
  todoId,
  open,
  onOpenChange,
  onResolved,
}: SolveDialogProps) {
  const [mode, setMode] = useState<SolveMode>("session");
  const [provider, setProvider] = useState<SolveProvider>("claude");

  // Hold the latest mutation result so onResolved fires AFTER the
  // dialog has unmounted — avoids React state-during-render warnings
  // when the parent re-renders due to the resolved callback.
  const resolvedRef = useRef<SolveResult | null>(null);
  useEffect(() => {
    if (!open && resolvedRef.current) {
      const res = resolvedRef.current;
      resolvedRef.current = null;
      onResolved?.(res);
    }
  }, [open, onResolved]);

  const submit = useMutation({
    mutationFn: () =>
      solveApi.solve(todoId, {
        mode,
        provider: mode === "session" ? provider : undefined,
      }),
    onSuccess: (res) => {
      if (res.mode === "session") {
        toast.success("Session opened", {
          description: `${res.provider} · ${res.session_id.slice(0, 8)}`,
        });
      } else {
        toast.success("Orchestrator started", {
          description: (
            <Link to={res.status_url} className="text-accent hover:underline">
              {res.orchestrator_id}
            </Link>
          ) as unknown as string,
        });
      }
      resolvedRef.current = res;
      onOpenChange(false);
    },
    onError: (err: Error) =>
      toast.error("Solve failed", { description: err.message }),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Solve this todo</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2 text-sm">
          <div>
            <FieldLabel>Path</FieldLabel>
            <Segmented
              ariaLabel="Resolution path"
              value={mode}
              onChange={(v) => setMode(v as SolveMode)}
              options={MODE_OPTIONS}
            />
          </div>
          {mode === "session" && (
            <div data-testid="solve-provider-picker">
              <FieldLabel>Provider</FieldLabel>
              <Segmented
                ariaLabel="Session provider"
                value={provider}
                onChange={(v) => setProvider(v as SolveProvider)}
                options={PROVIDER_OPTIONS}
              />
              <p className="mt-2 text-3xs text-tertiary">
                Opens an inline chat in a right-side panel — same window,
                no popup.
              </p>
            </div>
          )}
          {mode === "orchestrator" && (
            <p className="text-3xs text-tertiary">
              Spawns the orchestrator engine with this todo as the task. The
              status link will be in the success toast.
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => submit.mutate()}
            disabled={submit.isPending}
            data-testid="solve-submit"
          >
            {submit.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : null}
            {mode === "session" ? "Open session" : "Start orchestrator"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
