/**
 * Keyring API client.
 *
 * The keyring HAS TWO auth layers:
 *   1. The main API bearer token (same as every other mutation) — the
 *      shared ``api()`` helper already handles this.
 *   2. A per-session "keyring session token" returned by POST /unlock.
 *      This token gates the actual decrypted-vault access and must be
 *      echoed on every subsequent call via X-Keyring-Session.
 *
 * The session token is kept in memory (module-scoped) — NOT in
 * localStorage. A full page reload drops the session and the user must
 * re-enter the master password. Inactivity auto-lock is handled by the
 * backend (15-min TTL, sliding) and the UI (countdown + auto-lock).
 */

import { api } from "./api";

let _kringSession: string | null = null;

export function setKeyringSession(token: string | null) {
  _kringSession = token;
}

export function getKeyringSession(): string | null {
  return _kringSession;
}

function sessionHeaders(): Record<string, string> {
  return _kringSession ? { "X-Keyring-Session": _kringSession } : {};
}

export type KeyringStatus = {
  initialized: boolean;
  unlocked: boolean;
  count: number | null;
};

export type UnlockResponse = {
  session_token: string;
  expires_at: number; // epoch seconds
  ttl_seconds: number;
};

export type SecretRef = { name: string };

export const keyringApi = {
  status: () =>
    api<KeyringStatus>("/api/keyring/status", {
      headers: sessionHeaders(),
    }),

  unlock: async (password: string): Promise<UnlockResponse> => {
    const res = await api<UnlockResponse>("/api/keyring/unlock", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    setKeyringSession(res.session_token);
    return res;
  },

  lock: async (): Promise<void> => {
    try {
      await api<{ status: string }>("/api/keyring/lock", {
        method: "POST",
        headers: sessionHeaders(),
      });
    } finally {
      setKeyringSession(null);
    }
  },

  list: () =>
    api<{ secrets: SecretRef[] }>("/api/keyring/secrets", {
      headers: sessionHeaders(),
    }),

  create: (name: string, value: string) =>
    api<{ status: string; name: string }>("/api/keyring/secrets", {
      method: "POST",
      headers: sessionHeaders(),
      body: JSON.stringify({ name, value }),
    }),

  update: (name: string, value: string) =>
    api<{ status: string; name: string }>(
      `/api/keyring/secrets/${encodeURIComponent(name)}`,
      {
        method: "PUT",
        headers: sessionHeaders(),
        body: JSON.stringify({ value }),
      },
    ),

  delete: (name: string) =>
    api<{ status: string; name: string }>(
      `/api/keyring/secrets/${encodeURIComponent(name)}`,
      {
        method: "DELETE",
        headers: sessionHeaders(),
      },
    ),

  reveal: (name: string) =>
    api<{ name: string; value: string }>(
      `/api/keyring/secrets/${encodeURIComponent(name)}/reveal`,
      { headers: sessionHeaders() },
    ),

  initVault: (password: string) =>
    api<{ status: string }>("/api/onboarding/keyring", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
};
