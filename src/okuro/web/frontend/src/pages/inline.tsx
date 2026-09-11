import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { InlineSessionView } from "@/components/inline/inline-session-view";

/**
 * ``/inline/:sessionId?t=<bearer>``
 *
 * Thin route wrapper. Kept for legacy deep-links and CLI hand-offs:
 *   - Bearer can arrive in the ``?t=`` / ``?token=`` query string.
 *   - Or via a same-origin ``postMessage({kind: 'okuro:session-bearer'})``
 *     from a CLI / popup parent — token never lands in browser history.
 *
 * Rendering and SSE/state lives entirely in ``InlineSessionView``. The
 * Solve flow uses a slide-over panel that mounts that same view inline
 * (no popup), so this page is now only the share-link surface.
 */
export function InlineChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const [search] = useSearchParams();
  const queryToken = search.get("t") ?? search.get("token") ?? undefined;

  const [postedToken, setPostedToken] = useState<string | undefined>();
  useEffect(() => {
    function onMessage(ev: MessageEvent) {
      if (ev.origin !== window.location.origin) return;
      const data = ev.data;
      if (!data || typeof data !== "object") return;
      if (data.kind !== "okuro:session-bearer") return;
      if (typeof data.session_id !== "string" || data.session_id !== sessionId) return;
      if (typeof data.bearer !== "string" || !data.bearer) return;
      setPostedToken(data.bearer);
    }
    window.addEventListener("message", onMessage);
    // Tell the opener (if any) we are ready to receive.
    if (window.opener) {
      try {
        window.opener.postMessage(
          { kind: "okuro:session-ready", session_id: sessionId },
          window.location.origin,
        );
      } catch {
        /* same-origin only; ignore cross-origin opener */
      }
    }
    return () => window.removeEventListener("message", onMessage);
  }, [sessionId]);

  const token = queryToken ?? postedToken;

  if (!sessionId) {
    return (
      <MissingPage>
        Missing <code>:sessionId</code> in the URL.
      </MissingPage>
    );
  }
  if (!token) {
    return (
      <MissingPage>
        Missing session bearer. Append <code>?t=&lt;token&gt;</code> to the URL.
      </MissingPage>
    );
  }

  return (
    <div className="h-dvh">
      <InlineSessionView
        sessionId={sessionId}
        bearer={token}
        manageDocumentTitle
      />
    </div>
  );
}

function MissingPage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-dvh items-center justify-center bg-surface px-8 text-center text-sm text-tertiary">
      <div className="max-w-md space-y-2">
        <p>Inline session unavailable.</p>
        <p className="text-xs">{children}</p>
      </div>
    </div>
  );
}
