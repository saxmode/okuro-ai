# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Keyring API — secret CRUD with password-gated session unlock.
# index:
#   imports
#   class UnlockRequest
#   class UnlockResponse
#   class KeyringStatus
#   class SecretCreateRequest
#   class SecretUpdateRequest
#   class SecretReveal
#   _SESSION_TTL
#   _sessions
#   def _require_localhost_kr
#   def _purge_expired
#   def _require_unlocked
#   def get_status
#   def unlock
#   def lock
#   def list_secrets
#   def create_secret
#   def update_secret
#   def delete_secret
#   def reveal_secret
# AGENT_HEADER_END -->
"""Keyring API — password-gated secret CRUD.

The encrypted vault at ``~/.okuro/keyring/keys.enc`` must never be
exposed as a stateless HTTP surface — anyone on localhost would be
able to read every secret. Instead:

1. Frontend calls ``POST /api/keyring/unlock`` with the master password.
2. Backend decrypts once, caches the ``KeyringStorage`` instance in
   ``_sessions`` keyed by a random session token, returns that token.
3. Subsequent CRUD calls include ``Authorization: Bearer <token>`` —
   but the *keyring* bearer token is separate from the main API token.
   We accept it via ``X-Keyring-Session`` header OR query param to
   avoid clobbering the existing bearer auth.
4. Sessions expire after 15 minutes of inactivity.

Localhost-only (same guard used by the onboarding keyring init).
"""

from __future__ import annotations

import logging
import secrets as _secrets
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.keyring")

router = APIRouter(prefix="/api/keyring", tags=["keyring"])


# ── Session cache ────────────────────────────────────────────────────
# In-memory only. Never persisted. Cleared on process restart.
# {session_token: (KeyringStorage, expires_at_epoch)}

_SESSION_TTL = 15 * 60  # 15 minutes
_sessions: dict[str, tuple[object, float]] = {}
_sessions_lock = threading.Lock()


def _require_localhost_kr(request: Request):
    """Reuse the main app's loopback guard — shared logic, single source."""
    from okuro.orchestrator.api.main import _require_loopback
    _require_loopback(request)


def _purge_expired() -> None:
    """Drop sessions past their TTL."""
    now = time.time()
    with _sessions_lock:
        dead = [tok for tok, (_, exp) in _sessions.items() if exp <= now]
        for tok in dead:
            _sessions.pop(tok, None)


def _session_token_from_request(request: Request) -> Optional[str]:
    """Read the keyring session token from header or query string.

    Preferred: ``X-Keyring-Session: <token>``.
    Fallback: ``?session=<token>`` (handy for debugging curl).
    """
    tok = request.headers.get("x-keyring-session")
    if tok:
        return tok
    tok = request.query_params.get("session")
    return tok or None


def _require_unlocked(request: Request):
    """Return the unlocked ``KeyringStorage`` for this session or 401.

    Extends the session TTL on each successful use.
    """
    _purge_expired()
    tok = _session_token_from_request(request)
    if not tok:
        raise HTTPException(401, "Keyring session required — unlock first")
    with _sessions_lock:
        entry = _sessions.get(tok)
        if not entry:
            raise HTTPException(401, "Keyring session expired or invalid")
        store, _exp = entry
        # Slide the TTL forward so active sessions don't timeout mid-use.
        _sessions[tok] = (store, time.time() + _SESSION_TTL)
        return store


# ── Request/response models ──────────────────────────────────────────


class UnlockRequest(BaseModel):
    password: str = Field(..., min_length=1)


class UnlockResponse(BaseModel):
    session_token: str
    expires_at: float  # epoch seconds
    ttl_seconds: int


class KeyringStatus(BaseModel):
    initialized: bool
    unlocked: bool
    count: Optional[int] = None


class SecretCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    value: str = Field(..., min_length=1)


class SecretUpdateRequest(BaseModel):
    value: str = Field(..., min_length=1)


class SecretReveal(BaseModel):
    name: str
    value: str


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/status")
def get_status(request: Request) -> KeyringStatus:
    """Report vault initialization / unlock state for the calling session.

    ``unlocked`` reflects whether THIS session token has an active
    cached storage — not whether any agent somewhere has the password.
    """
    _require_localhost_kr(request)
    _purge_expired()

    try:
        from okuro.keyring import KeyringStorage
        store = KeyringStorage()
        initialized = store.is_initialized
    except Exception as exc:
        logger.exception("keyring status check failed")
        raise HTTPException(500, f"Keyring status check failed: {exc}")

    tok = _session_token_from_request(request)
    unlocked = False
    count: Optional[int] = None
    if tok and initialized:
        with _sessions_lock:
            entry = _sessions.get(tok)
        if entry:
            session_store, _exp = entry
            unlocked = True
            try:
                count = len(session_store.list_keys())  # type: ignore[attr-defined]
            except Exception:
                count = None

    return KeyringStatus(initialized=initialized, unlocked=unlocked, count=count)


@router.post("/unlock", response_model=UnlockResponse)
def unlock(body: UnlockRequest, request: Request) -> UnlockResponse:
    """Verify the master password and open a decryption session.

    Returns a random session token the client must echo via
    ``X-Keyring-Session`` on subsequent calls. The token is the ONLY
    way to access secrets for this session — never written to disk.
    """
    _require_localhost_kr(request)

    from okuro.keyring import KeyringStorage
    if not KeyringStorage().is_initialized:
        raise HTTPException(400, "Keyring not initialized — use /api/onboarding/keyring first")

    # Build a fresh storage scoped to this password. Trigger a decrypt
    # to validate — list_keys() goes through the Fernet path.
    try:
        store = KeyringStorage(master_password=body.password)
        store.list_keys()
    except ValueError as exc:
        raise HTTPException(401, f"Unlock failed: {exc}")
    except Exception as exc:
        logger.exception("unlock failed")
        raise HTTPException(500, f"Unlock failed: {exc}")

    token = _secrets.token_urlsafe(32)
    expires_at = time.time() + _SESSION_TTL
    with _sessions_lock:
        _sessions[token] = (store, expires_at)

    return UnlockResponse(
        session_token=token,
        expires_at=expires_at,
        ttl_seconds=_SESSION_TTL,
    )


@router.post("/lock")
def lock(request: Request):
    """Drop this session's cached storage. Idempotent."""
    _require_localhost_kr(request)
    tok = _session_token_from_request(request)
    if tok:
        with _sessions_lock:
            _sessions.pop(tok, None)
    return {"status": "locked"}


@router.get("/secrets")
def list_secrets(request: Request):
    """List secret names (never values). Requires unlocked session."""
    _require_localhost_kr(request)
    store = _require_unlocked(request)
    try:
        names = store.list_keys()  # type: ignore[attr-defined]
    except Exception as exc:
        logger.exception("list_secrets failed")
        raise HTTPException(500, f"List failed: {exc}")
    return {"secrets": [{"name": n} for n in names]}


@router.post("/secrets")
def create_secret(body: SecretCreateRequest, request: Request):
    """Create a secret. Fails if name already exists."""
    _require_localhost_kr(request)
    store = _require_unlocked(request)
    try:
        existing = store.list_keys()  # type: ignore[attr-defined]
        if body.name in existing:
            raise HTTPException(409, f"Secret '{body.name}' already exists — use PUT to update")
        store.add_key(body.name, body.value)  # type: ignore[attr-defined]
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("create_secret failed")
        raise HTTPException(500, f"Create failed: {exc}")
    return {"status": "created", "name": body.name}


@router.put("/secrets/{name}")
def update_secret(name: str, body: SecretUpdateRequest, request: Request):
    """Update an existing secret's value. Name is immutable."""
    _require_localhost_kr(request)
    store = _require_unlocked(request)
    try:
        existing = store.list_keys()  # type: ignore[attr-defined]
        if name not in existing:
            raise HTTPException(404, f"Secret '{name}' not found")
        store.add_key(name, body.value)  # type: ignore[attr-defined]
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("update_secret failed")
        raise HTTPException(500, f"Update failed: {exc}")
    return {"status": "updated", "name": name}


@router.delete("/secrets/{name}")
def delete_secret(name: str, request: Request):
    """Delete a secret. 404 if it doesn't exist."""
    _require_localhost_kr(request)
    store = _require_unlocked(request)
    try:
        ok = store.delete_key(name)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.exception("delete_secret failed")
        raise HTTPException(500, f"Delete failed: {exc}")
    if not ok:
        raise HTTPException(404, f"Secret '{name}' not found")
    return {"status": "deleted", "name": name}


@router.get("/secrets/{name}/reveal", response_model=SecretReveal)
def reveal_secret(name: str, request: Request) -> SecretReveal:
    """Return a secret's plaintext value. Only called when user clicks Reveal."""
    _require_localhost_kr(request)
    store = _require_unlocked(request)
    try:
        value = store.get_key(name)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.exception("reveal_secret failed")
        raise HTTPException(500, f"Reveal failed: {exc}")
    if value is None:
        raise HTTPException(404, f"Secret '{name}' not found")
    return SecretReveal(name=name, value=value)
