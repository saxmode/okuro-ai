import { useCallback, useEffect, useRef, useState } from "react";
import { Send, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface MessageInputProps {
  onSubmit: (text: string) => Promise<void> | void;
  /** True while the session is terminal — input is hidden / disabled. */
  disabled?: boolean;
  /** Set while the most-recent submit is in flight. */
  busy?: boolean;
  /** Free-text reason rendered below the input — e.g. session_already_terminal. */
  hint?: string;
}

/**
 * Bottom-pinned composer. Cmd/Ctrl+Enter submits; bare Enter inserts a
 * newline (transcript-style). Auto-resizes up to 8 rows.
 */
export function MessageInput({
  onSubmit,
  disabled,
  busy,
  hint,
}: MessageInputProps) {
  const [text, setText] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize. Field-sizing-content (Tailwind) handles this in modern
  // browsers, but we explicitly bound the max height for older targets.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 192)}px`;
  }, [text]);

  const fire = useCallback(async () => {
    const trimmed = text.trim();
    if (!trimmed || disabled || busy) return;
    setText("");
    try {
      await onSubmit(trimmed);
    } catch {
      // Restore so the user can retry — the page surfaces the toast.
      setText(trimmed);
    }
  }, [text, disabled, busy, onSubmit]);

  return (
    <div className="border-t border-border-subtle bg-surface px-8 py-4">
      <div className="mx-auto max-w-[68ch]">
        <div
          className={cn(
            "flex items-end gap-2 rounded-md border border-border bg-surface-elevated px-3 py-2",
            "focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/30",
          )}
        >
          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                (e.metaKey || e.ctrlKey) &&
                !e.shiftKey &&
                !e.altKey
              ) {
                e.preventDefault();
                void fire();
              }
            }}
            disabled={disabled || busy}
            rows={1}
            placeholder={
              disabled
                ? "Session is closed — input disabled."
                : "Type a message (Cmd/Ctrl+Enter to send)"
            }
            className={cn(
              "flex-1 resize-none bg-transparent text-sm leading-relaxed outline-none placeholder:text-tertiary",
              "min-h-[3rem] max-h-48",
            )}
            aria-label="Message input"
          />
          <Button
            onClick={fire}
            disabled={!text.trim() || disabled || busy}
            size="sm"
            variant="default"
            aria-label="Send message"
          >
            {busy ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <Send className="h-3.5 w-3.5" aria-hidden />
            )}
            Send
          </Button>
        </div>
        {hint && (
          <p className="mt-1.5 text-3xs text-tertiary">{hint}</p>
        )}
      </div>
    </div>
  );
}
