import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import type {
  InlineSessionView,
  ToolCard,
  TranscriptMessage,
} from "@/types/inline";

interface TranscriptProps {
  view: InlineSessionView;
  /** Click handler — should scroll the matching tool card into view. */
  onToolReferenceClick?: (call_id: string) => void;
}

/**
 * Scrolling transcript column. Each bubble is keyed by message_id so
 * token deltas append in-place without remounting. Auto-scrolls to
 * bottom whenever new content arrives unless the user has scrolled up
 * (within 80px of the bottom counts as "still anchored").
 */
export function Transcript({ view, onToolReferenceClick }: TranscriptProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoscroll, setAutoscroll] = useState(true);

  // Lookup map for tool refs in the transcript margin.
  const toolMap = useMemo(() => {
    const m = new Map<string, ToolCard>();
    for (const c of view.tool_cards) m.set(c.call_id, c);
    return m;
  }, [view.tool_cards]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    setAutoscroll(distance < 80);
  }, []);

  useEffect(() => {
    if (!autoscroll) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [view.messages, view.tool_cards, autoscroll]);

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto px-8 py-6"
      >
        {view.messages.length === 0 ? (
          <EmptyHint />
        ) : (
          <div className="mx-auto max-w-[68ch] space-y-6">
            {view.messages.map((m) => (
              <Bubble
                key={m.message_id}
                message={m}
                toolMap={toolMap}
                onToolReferenceClick={onToolReferenceClick}
              />
            ))}
          </div>
        )}
      </div>
      {!autoscroll && (
        <button
          type="button"
          onClick={() => {
            setAutoscroll(true);
            const el = scrollRef.current;
            if (el) el.scrollTop = el.scrollHeight;
          }}
          className={cn(
            "absolute bottom-24 left-1/2 -translate-x-1/2",
            "rounded-full border border-border bg-surface-elevated px-3 py-1.5 text-3xs uppercase tracking-wider",
            "text-tertiary hover:text-fg transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
          )}
        >
          Jump to latest
        </button>
      )}
    </div>
  );
}

function EmptyHint() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-tertiary">
      Waiting for the assistant to begin...
    </div>
  );
}

function Bubble({
  message,
  toolMap,
  onToolReferenceClick,
}: {
  message: TranscriptMessage;
  toolMap: Map<string, ToolCard>;
  onToolReferenceClick?: (call_id: string) => void;
}) {
  const isUser = message.role === "user";
  return (
    <article
      className={cn(
        "flex flex-col gap-2",
        isUser ? "items-end" : "items-start",
      )}
    >
      <span className="text-3xs uppercase tracking-widest text-tertiary">
        {isUser ? "You" : "Assistant"}
      </span>
      <div
        className={cn(
          "rounded-md border px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap break-words max-w-full",
          isUser
            ? "border-border-subtle bg-surface-subtle text-fg"
            : "border-border bg-surface-elevated text-fg",
        )}
      >
        {message.text || (
          <span className="text-tertiary">
            {message.streaming ? "..." : "(empty)"}
          </span>
        )}
        {message.streaming && message.text ? (
          <span className="ml-1 inline-block h-3 w-2 animate-pulse bg-accent align-middle" />
        ) : null}
      </div>
      {message.tool_call_ids.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {message.tool_call_ids.map((cid) => {
            const card = toolMap.get(cid);
            if (!card) return null;
            return (
              <ToolReferencePill
                key={cid}
                card={card}
                onClick={() => onToolReferenceClick?.(cid)}
              />
            );
          })}
        </div>
      ) : null}
    </article>
  );
}

function ToolReferencePill({
  card,
  onClick,
}: {
  card: ToolCard;
  onClick: () => void;
}) {
  const tone =
    card.state === "pending-approval"
      ? "text-warning border-warning/40"
      : card.state === "denied" || card.state === "error"
        ? "text-error border-error/40"
        : card.state === "done"
          ? "text-tertiary border-border-subtle"
          : "text-accent border-accent/40";
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-3xs uppercase tracking-wider transition-colors hover:bg-surface-elevated",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        tone,
      )}
      title={`${card.tool_name} (${card.state})`}
    >
      <Wrench className="h-2.5 w-2.5" aria-hidden />
      {card.tool_name}
    </button>
  );
}
