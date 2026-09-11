/**
 * Per-session HTTP helpers — bearer auth via ``Authorization`` header.
 *
 * Distinct from ``src/lib/api.ts`` (which uses the global okuro bearer)
 * because every inline session has its own scoped token minted at
 * ``bridge_stream_start``. The bearer arrives in the URL query string;
 * the page hands it to these helpers.
 *
 * Security trade-off acknowledged: the SSE consumer must put the token
 * in ``?t=`` because EventSource cannot set custom headers (see
 * docs/research/cli-streaming-contract.md §3.1). The bearer is
 * single-session and bound to ``sessions_inline.id`` server-side, so
 * leak surface is limited to one session's transcript. POST routes
 * accept the header form too — we use the header here.
 */
import type { ApprovalDecision } from "@/types/inline";

export class SessionApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = "SessionApiError";
    this.status = status;
    this.detail = detail;
  }
}

interface PostOpts {
  signal?: AbortSignal;
}

async function post(
  url: string,
  token: string,
  body: Record<string, unknown>,
  { signal }: PostOpts = {},
): Promise<Record<string, unknown>> {
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
    signal,
  });
  const contentType = res.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? ((await res.json()) as Record<string, unknown>)
    : { detail: await res.text() };
  if (!res.ok) {
    const msg =
      typeof payload.error === "string"
        ? payload.error
        : `request failed (${res.status})`;
    throw new SessionApiError(msg, res.status, payload);
  }
  return payload;
}

export async function sendInput(
  sessionId: string,
  token: string,
  message: string,
  opts?: PostOpts,
): Promise<void> {
  await post(
    `/api/sessions/${encodeURIComponent(sessionId)}/input`,
    token,
    { message },
    opts,
  );
}

export async function approveCall(
  sessionId: string,
  token: string,
  invocationId: string,
  decision: ApprovalDecision,
  editedArgs?: Record<string, unknown>,
  opts?: PostOpts,
): Promise<void> {
  const body: Record<string, unknown> = {
    invocation_id: invocationId,
    decision,
  };
  if (decision === "edit" && editedArgs) body.edited_args = editedArgs;
  await post(
    `/api/sessions/${encodeURIComponent(sessionId)}/approve`,
    token,
    body,
    opts,
  );
}

export async function cancelSession(
  sessionId: string,
  token: string,
  opts?: PostOpts,
): Promise<void> {
  await post(
    `/api/sessions/${encodeURIComponent(sessionId)}/cancel`,
    token,
    {},
    opts,
  );
}

interface SessionStatus {
  id: string;
  provider?: string;
  model?: string;
  status?: string;
  todo_id?: string;
  todo_title?: string;
  [key: string]: unknown;
}

export async function fetchSessionStatus(
  sessionId: string,
  token: string,
): Promise<SessionStatus> {
  const res = await fetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/status`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!res.ok) {
    throw new SessionApiError(
      `status fetch failed (${res.status})`,
      res.status,
      await res.text(),
    );
  }
  return (await res.json()) as SessionStatus;
}

export type { SessionStatus };
