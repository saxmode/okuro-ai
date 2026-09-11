import { getToken } from "./api";

/**
 * Streaming flow creation — the agent emits JSONL (meta → nodes → edges) and
 * we render each on the canvas AS IT IS WRITTEN. A tiny module-level queue
 * hands the prompt from the (global) chat draw action to the FlowDesigner that
 * mounts after navigation; FlowDesigner consumes the stream into its canvas.
 */
let _pending: string | null = null;

export function queueStreamDraw(prompt: string): void {
  _pending = prompt;
}

export function takeStreamDraw(): string | null {
  const p = _pending;
  _pending = null;
  return p;
}

/**
 * Start a live flow draw from anywhere: queue the prompt, then either draw in
 * the already-mounted canvas or navigate so one mounts and drains the queue.
 *
 * A mounted FlowDesigner requires an `id` or `new` param — bare `/flow` is the
 * GALLERY, which never drains the queue. Checking `pathname.startsWith("/flow")`
 * alone silently no-ops on the gallery (and on any non-flow page). Centralised
 * here so every caller (chat draw action, handover dialog) shares the one
 * correct predicate.
 */
export function startFlowDraw(prompt: string, navigate: (to: string) => void): void {
  queueStreamDraw(prompt);
  const onCanvas =
    window.location.pathname.startsWith("/flow") &&
    /[?&](id|new)=/.test(window.location.search);
  if (onCanvas) {
    window.dispatchEvent(new CustomEvent("okuro:flow-draw-stream", { detail: { prompt } }));
  } else {
    navigate("/flow?new=1");
  }
}

export interface FlowStreamHandlers {
  onMeta?: (m: { name: string; description: string }) => void;
  onNode?: (node: unknown) => void;
  onEdge?: (edge: unknown) => void;
  onActivity?: (msg: string) => void;
  onDone?: (r: { flow: { id: string; name?: string }; node_count: number; edge_count: number }) => void;
  onError?: (msg: string) => void;
}

export async function drawStream(prompt: string, h: FlowStreamHandlers): Promise<void> {
  // SSE (EventSource) — delivered incrementally in BOTH Chromium and WebKitGTK
  // (pywebview), unlike fetch ReadableStream which WebKit buffers. So nodes
  // appear one-by-one everywhere. Prompt + bearer go in the query string
  // (EventSource is GET-only and can't set headers; the global middleware
  // accepts the bearer as ?token=).
  const token = await getToken();
  const url =
    `/api/flow-designer/draw/sse?prompt=${encodeURIComponent(prompt)}` +
    `&token=${encodeURIComponent(token)}`;

  return new Promise<void>((resolve) => {
    const es = new EventSource(url);
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      es.close();
      resolve();
    };

    es.onmessage = (e: MessageEvent) => {
      let ev: {
        type?: string;
        msg?: string;
        name?: string;
        description?: string;
        node?: unknown;
        edge?: unknown;
        error?: string;
        flow?: { id: string; name?: string };
        node_count?: number;
        edge_count?: number;
      };
      try {
        ev = JSON.parse(e.data);
      } catch {
        return;
      }
      if (ev.type === "activity") h.onActivity?.(ev.msg ?? "");
      else if (ev.type === "meta")
        h.onMeta?.({ name: ev.name ?? "", description: ev.description ?? "" });
      else if (ev.type === "node") h.onNode?.(ev.node);
      else if (ev.type === "edge") h.onEdge?.(ev.edge);
      else if (ev.type === "done" && ev.flow) {
        h.onDone?.({ flow: ev.flow, node_count: ev.node_count ?? 0, edge_count: ev.edge_count ?? 0 });
        finish();
      } else if (ev.type === "error") {
        h.onError?.(ev.error ?? "stream error");
        finish();
      }
    };

    // Fires on connection close. If we already saw `done` this is a no-op;
    // otherwise the stream dropped before finishing.
    es.onerror = () => {
      if (!finished) {
        h.onError?.("stream connection lost");
        finish();
      }
    };
  });
}
