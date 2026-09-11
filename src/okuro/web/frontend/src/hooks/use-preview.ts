/**
 * Preview-launcher hooks. Adaptive polling for status (fast while
 * building, slow once ready, off when there's nothing to watch). Logs
 * stream via SSE EventSource so the drawer fills in real time during
 * install/build/serve — the previous polled-JSON approach pulled
 * journal-only data, which left non-systemd kinds (doc/image/audio/
 * video/slides/notebook/external) with an empty pane.
 *
 * Start is optimistic: the moment the user clicks "Build & see result",
 * we set local state to "building" so the button label flips
 * immediately. The real /start call kicks off a background launcher
 * thread on the server; subsequent /status polls (or WS pushes) carry
 * the real lifecycle.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getToken, previewApi } from "@/lib/api";
import type { PreviewState } from "@/types/api";

const ACTIVE_STATES: Array<PreviewState["state"]> = ["building", "starting"];

export function usePreviewStatus(taskId: string | undefined) {
  return useQuery({
    queryKey: ["preview", taskId, "status"],
    queryFn: () => previewApi.status(taskId!),
    enabled: !!taskId,
    refetchInterval: (query) => {
      const data = query.state.data as PreviewState | undefined;
      if (!data) return 5_000;
      if (ACTIVE_STATES.includes(data.state)) return 1_500;
      if (data.state === "ready") return 10_000;
      return false;
    },
  });
}

export function useStartPreview(taskId: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (opts?: { force_rebuild?: boolean }) =>
      previewApi.start(taskId!, opts),
    // Optimistic flip: stamp state="building" the instant the click
    // lands so the button shows progress without waiting for the
    // server response. The server start endpoint is non-blocking
    // (returns within ~100 ms with state="building") but every ms
    // counts for perceived responsiveness.
    onMutate: () => {
      const previous = qc.getQueryData<PreviewState>([
        "preview", taskId, "status",
      ]);
      if (previous) {
        qc.setQueryData(["preview", taskId, "status"], {
          ...previous,
          state: "building" as const,
          last_error: null,
        });
      }
      return { previous };
    },
    onError: (_err, _vars, ctx) => {
      // Roll back on failure so the user sees the prior state and we
      // don't leave the button stuck on "Building…".
      if (ctx?.previous) {
        qc.setQueryData(["preview", taskId, "status"], ctx.previous);
      }
    },
    onSuccess: (data) => {
      qc.setQueryData(["preview", taskId, "status"], data);
    },
  });
}

export function useStopPreview(taskId: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => previewApi.stop(taskId!),
    onSuccess: (data) => {
      qc.setQueryData(["preview", taskId, "status"], data);
    },
  });
}

/**
 * SSE-backed live log tail. EventSource attaches to
 * ``/api/preview/{taskId}/logs?stream=true`` while ``enabled`` is true;
 * lines are appended in arrival order, capped at ``maxLines`` so the
 * DOM doesn't unbound-grow on long builds.
 *
 * Returns an array (lines) plus connection state. The component renders
 * "No log output yet." only when both: enabled is true AND the array
 * is empty AND the stream hasn't connected — for SSE-aware UIs, prefer
 * the ``connected`` flag as the gate.
 */
export function usePreviewLogs(
  taskId: string | undefined,
  opts: { enabled: boolean; lines?: number },
) {
  const maxLines = opts.lines ?? 500;
  const [lines, setLines] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  // Reset the buffer whenever the task changes so opening the drawer
  // for task A, then navigating to task B, doesn't show A's tail.
  useEffect(() => {
    setLines([]);
    setConnected(false);
  }, [taskId]);

  useEffect(() => {
    if (!taskId || !opts.enabled) {
      sourceRef.current?.close();
      sourceRef.current = null;
      setConnected(false);
      return;
    }

    let cancelled = false;

    const append = (raw: string) => {
      setLines((prev) => {
        const next = prev.length >= maxLines
          ? prev.slice(prev.length - maxLines + 1)
          : prev.slice();
        next.push(raw);
        return next;
      });
    };

    // EventSource is GET-only and can't set an Authorization header, so the
    // bearer rides in the query string — the global /api/* middleware accepts
    // ?token= (same pattern as lib/flow-stream.ts and lib/slides-api.ts).
    // Without it every /api/preview/{id}/logs SSE connection 401s ("Bearer
    // token required") and the drawer hangs on "Connecting to log stream…".
    (async () => {
      const token = await getToken();
      if (cancelled) return;
      const url =
        `/api/preview/${encodeURIComponent(taskId)}/logs?stream=true` +
        `&token=${encodeURIComponent(token)}`;
      const es = new EventSource(url, { withCredentials: false });
      sourceRef.current = es;

      es.onopen = () => setConnected(true);
      es.onerror = () => setConnected(false);

      // SSE events are routed by `event: build|service` — the launcher
      // tags each line with its source so the UI can colour them
      // differently if it wants. Default-channel events (no `event:`
      // line) fall through to onmessage as a safety net.
      es.addEventListener("build", (ev) => append((ev as MessageEvent).data));
      es.addEventListener("service", (ev) => append((ev as MessageEvent).data));
      es.onmessage = (ev) => append(ev.data);
    })();

    return () => {
      cancelled = true;
      sourceRef.current?.close();
      sourceRef.current = null;
      setConnected(false);
    };
  }, [taskId, opts.enabled, maxLines]);

  // Adapter object matching the previous react-query `data` shape so
  // the consumer doesn't have to special-case.
  return useMemo(
    () => ({ data: { lines }, connected }),
    [lines, connected],
  );
}
