import { Sheet, SheetContent, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { InlineSessionView } from "@/components/inline/inline-session-view";
import { VisuallyHidden } from "radix-ui";

interface SolveSessionPanelProps {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  sessionId: string | null;
  bearer: string | null;
}

/**
 * Right-anchored slide-over panel that hosts an inline session inside
 * the main okuro window — replaces the legacy ``window.open`` popup so
 * pywebview / WebKitGTK targets don't get blocked.
 *
 * Closing the panel hides it but does NOT cancel the underlying
 * session — the caller may reopen it later. The session-Cancel button
 * inside the InlineSessionView header is the only path that actually
 * stops the agent.
 *
 * If ``sessionId`` or ``bearer`` is null the panel renders nothing,
 * even when ``open`` is true — guards against accidental open before
 * a session is minted.
 */
export function SolveSessionPanel({
  open,
  onOpenChange,
  sessionId,
  bearer,
}: SolveSessionPanelProps) {
  const ready = Boolean(sessionId && bearer);

  return (
    <Sheet open={open && ready} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        showCloseButton={false}
        className="p-0"
        aria-describedby={undefined}
        data-testid="solve-session-panel"
        onInteractOutside={(e) => {
          // Backdrop click closes — default. Nothing extra to do.
          void e;
        }}
      >
        <VisuallyHidden.Root>
          <SheetTitle>Inline session</SheetTitle>
          <SheetDescription>
            Active session for the selected todo.
          </SheetDescription>
        </VisuallyHidden.Root>
        {sessionId && bearer ? (
          <InlineSessionView
            sessionId={sessionId}
            bearer={bearer}
            onClose={() => onOpenChange(false)}
          />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
