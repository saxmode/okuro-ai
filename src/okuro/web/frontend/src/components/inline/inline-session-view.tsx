import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { useSessionStream } from "@/hooks/use-session-stream";
import { SessionHeader } from "@/components/inline/session-header";
import { Transcript } from "@/components/inline/transcript";
import {
  ToolCardStack,
  type ToolCardStackHandle,
} from "@/components/inline/tool-card-stack";
import { MessageInput } from "@/components/inline/message-input";
import {
  approveCall,
  cancelSession,
  fetchSessionStatus,
  sendInput,
  SessionApiError,
} from "@/lib/inline-api";
import type { ApprovalDecision } from "@/types/inline";
import { cn } from "@/lib/utils";

interface InlineSessionViewProps {
  /** Required — drives the SSE URL and POST targets. */
  sessionId: string;
  /** Required — Authorization bearer for SSE query + POST headers. */
  bearer: string;
  /**
   * Optional — if provided, the view renders a "close panel" affordance
   * (separate from the session-Cancel button). Calling onClose hides
   * the panel WITHOUT cancelling the underlying session.
   */
  onClose?: () => void;
  /**
   * Tweak the document.title while this view is mounted. Defaults to
   * true on the full-page route; the slide-over passes false to avoid
   * mutating the host page's title.
   */
  manageDocumentTitle?: boolean;
  className?: string;
}

/**
 * Presentational inline-session shell. Hosts the SSE consumer, transcript,
 * tool-card stack, and composer. Identical behaviour to the legacy
 * ``/inline/:sessionId`` page — extracted so it can be rendered both as
 * a full route and inside a right-anchored slide-over panel.
 *
 * Accepts the bearer directly via props — no URL/postMessage reading.
 * The thin route wrapper handles bearer sourcing (legacy share links).
 */
export function InlineSessionView({
  sessionId,
  bearer,
  onClose,
  manageDocumentTitle = false,
  className,
}: InlineSessionViewProps) {
  const toolStackRef = useRef<ToolCardStackHandle>(null);
  const [sendBusy, setSendBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [inputHint, setInputHint] = useState<string | undefined>();

  const { view, pushOptimisticUser } = useSessionStream(sessionId, bearer);

  const { data: status } = useQuery({
    queryKey: ["inline-session-status", sessionId, bearer],
    queryFn: () => fetchSessionStatus(sessionId, bearer),
    enabled: Boolean(sessionId && bearer),
    staleTime: 30_000,
    retry: 1,
  });

  useEffect(() => {
    if (!manageDocumentTitle) return;
    const title = status?.todo_title
      ? `${status.todo_title} · okuro`
      : `Session ${sessionId.slice(0, 8)} · okuro`;
    document.title = title;
  }, [manageDocumentTitle, sessionId, status?.todo_title]);

  const handleSubmit = useCallback(
    async (text: string) => {
      setSendBusy(true);
      setInputHint(undefined);
      const msgId = `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      pushOptimisticUser(msgId, text);
      try {
        await sendInput(sessionId, bearer, text);
      } catch (e) {
        if (e instanceof SessionApiError) {
          setInputHint(`${e.message} (${e.status})`);
        } else {
          setInputHint(e instanceof Error ? e.message : String(e));
        }
        toast.error("Send failed", {
          description:
            e instanceof Error ? e.message : "unknown error sending input",
        });
        throw e;
      } finally {
        setSendBusy(false);
      }
    },
    [sessionId, bearer, pushOptimisticUser],
  );

  const handleDecide = useCallback(
    async (
      call_id: string,
      decision: ApprovalDecision,
      edited_args?: Record<string, unknown>,
    ) => {
      try {
        await approveCall(sessionId, bearer, call_id, decision, edited_args);
        toast.success(
          decision === "approve"
            ? "Approved"
            : decision === "deny"
              ? "Denied"
              : "Submitted edited args",
        );
      } catch (e) {
        toast.error("Approval failed", {
          description: e instanceof Error ? e.message : "unknown error",
        });
        throw e;
      }
    },
    [sessionId, bearer],
  );

  const handleCancel = useCallback(async () => {
    setCancelBusy(true);
    try {
      await cancelSession(sessionId, bearer);
      toast("Cancel requested");
    } catch (e) {
      toast.error("Cancel failed", {
        description: e instanceof Error ? e.message : "unknown error",
      });
    } finally {
      setCancelBusy(false);
    }
  }, [sessionId, bearer]);

  const scrollToTool = useCallback((call_id: string) => {
    toolStackRef.current?.scrollTo(call_id);
  }, []);

  const terminal = useMemo(
    () =>
      view.state === "done" ||
      view.state === "cancelled" ||
      view.state === "error",
    [view.state],
  );

  return (
    <div
      className={cn("flex h-full min-h-0 flex-col bg-surface text-fg", className)}
      data-testid="inline-session-view"
    >
      <div className="flex items-stretch">
        <div className="flex-1 min-w-0">
          <SessionHeader
            sessionId={sessionId}
            status={status}
            view={view}
            onCancel={handleCancel}
            cancelling={cancelBusy}
          />
        </div>
        {onClose ? (
          <div className="flex items-start border-b border-border-subtle bg-surface/95 pr-4 pt-4 backdrop-blur">
            <Button
              size="sm"
              variant="ghost"
              onClick={onClose}
              aria-label="Close panel"
              data-testid="inline-close-panel"
              title="Close panel (session keeps running)"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </Button>
          </div>
        ) : null}
      </div>
      <main className="relative flex flex-1 overflow-hidden">
        <Transcript view={view} onToolReferenceClick={scrollToTool} />
        <ToolCardStack
          ref={toolStackRef}
          cards={view.tool_cards}
          onDecide={handleDecide}
        />
      </main>
      <MessageInput
        onSubmit={handleSubmit}
        disabled={terminal}
        busy={sendBusy}
        hint={
          terminal
            ? `Session ${view.state}${view.state_detail ? ` — ${view.state_detail}` : ""}`
            : inputHint
        }
      />
    </div>
  );
}
