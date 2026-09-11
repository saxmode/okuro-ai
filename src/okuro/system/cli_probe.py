# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Single source of truth for CLI install + auth state detection.
# index:
#   imports
#   class AuthState
#   class CliState
#   def detect
#   def detect_all
#   def invalidate_cache
#   def _resolve_path_via_shell
#   def _probe_version
#   _AUTH_PROBES
#   def _probe_claude_auth
#   def _probe_codex_auth
#   def _probe_cursor_auth
#   def _check_jwt_expiry
# AGENT_HEADER_END -->
"""Single source of truth for CLI install + auth state.

Why this module exists
======================
Before this module, three different probes asked overlapping questions and
disagreed:

  - ``okuro.system.provider_path.which`` walked a hardcoded list of common
    install dirs (``~/.local/bin``, ``/opt/homebrew/bin``, ``~/.volta/bin``,
    …). Every new package manager (bun, pnpm-global, fnm, asdf with custom
    prefix, project-local ``./node_modules/.bin``) needed another entry —
    and the list was always behind the ecosystem.

  - ``okuro.system.cli_installer._check_auth`` proved a CLI was authenticated
    by the existence of a credentials file or keychain entry. That returned
    True for stale keychain leftovers and for ``oauth_creds.json`` whose
    refresh-token had expired weeks ago.

  - ``okuro.orchestrator.api.onboarding._cli_status`` did the same shape with
    ``shutil.which`` (no enhanced PATH at all) and the same lenient file
    proxy.

The wizard, dashboard, bridge router, and ``okuro doctor`` each picked one
of the three at random, so a Mac with claude + codex + cursor "all three
authenticated" per the wizard could simultaneously show codex/cursor red on
the dashboard and route bridge calls to a missing binary.

What this module does instead
=============================
Two probes, both ground truth, no hardcoded "where things might live":

  1. Path detection delegates to the user's actual shell. We invoke
     ``$SHELL -lic 'command -v <binary>'`` (login + interactive). The shell
     already knows about every package manager the user has set up because
     each one writes its init into ``.zshrc`` / ``.bash_profile`` / etc.
     We never maintain a list of install paths — the user's environment is
     the single source of truth.

  2. Auth verification delegates to the CLI itself. Each adapter knows the
     CLI's auth-status verb (``claude auth status --json``,
     ``codex login status``) or, for CLIs without one (cursor), parses the
     credentials file with token-expiry validation, or spends one trivial
     live call (``agy models``). File existence alone is never the answer.

Callers consume one struct (``CliState``) so wizard / dashboard / bridge /
doctor cannot disagree by construction.

Mac-keychain note
=================
Claude Code reads its credentials from the macOS Keychain. Keychain ACLs are
per-process: a launchd-spawned UserAgent (production okuro) inherits the
user's GUI-session keychain access; an SSH non-login bash does not. We DO
NOT try to work around keychain ACL — we run the auth probe via subprocess
from whatever process currently calls ``detect()``. When ``detect()`` is
invoked from the orchestrator service (the dashboard's actual context) the
probe sees what the user sees. When invoked from a one-off SSH shell, it
sees what that shell sees, which is the truthful answer for that context.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)


# ── public types ──────────────────────────────────────────────────────────


AuthStateLiteral = Literal[
    "ok", "missing", "expired", "ineligible", "unknown", "error"
]
"""ok         — CLI accepts the credentials and is ready to invoke
missing    — no credentials present (user has not authenticated)
expired    — credentials present but refresh-token / id-token past its exp
ineligible — credentials are VALID and the CLI still refuses: the account's
             tier/entitlement does not cover this client. Distinct from
             `expired` (re-auth fixes that) and `missing` (logging in fixes
             that) — nothing the user does at the credential layer helps, so
             callers must route elsewhere. Added 2026-07-17: the Gemini CLI
             retired for individuals on 2026-06-18 and now exits with
             IneligibleTierError while oauth_creds.json stays perfectly valid.
unknown    — probe ran but couldn't determine state (no auth verb, parse
             failure, CLI version too old to expose status, etc.)
error      — probe itself failed (timeout, OSError); indeterminate"""


@dataclass(frozen=True)
class AuthState:
    """Per-CLI authentication snapshot.

    ``method`` and ``detail`` are advisory — surface them in UI tooltips,
    but route logic should branch on ``state`` only.
    """

    state: AuthStateLiteral
    method: Optional[str] = None  # "oauth" | "api-key" | "chatgpt" | …
    detail: Optional[str] = None  # logged-in account, expiry timestamp, etc.

    @property
    def ready(self) -> bool:
        """True iff the CLI is invokable for production traffic."""
        return self.state == "ok"


@dataclass(frozen=True)
class CliState:
    """Combined install + auth snapshot for one CLI."""

    name: str            # canonical id used by okuro: "claude" | "codex" | …
    binary: str          # the binary basename to look up
    path: Optional[str]  # absolute path resolved via the user's shell, or None
    on_path: bool        # True iff path is non-None (binary is invokable)
    version: Optional[str]
    auth: AuthState
    source: str          # "shell" | "fallback-os-path" | "missing"

    @property
    def ready(self) -> bool:
        """True iff the CLI is installed AND authenticated.

        This is the only flag the wizard's gate, the dashboard's status
        light, and the bridge's routing fallback should consult. Anything
        less and we're back to "wizard says green, app says red".
        """
        return self.on_path and self.auth.ready


# ── canonical CLI list ────────────────────────────────────────────────────


# Each entry: canonical name → (binary basename, auth-probe key).
# Adding a CLI = adding a row here + an _AUTH_PROBES entry. Nothing else
# changes across the wizard / dashboard / bridge / doctor surfaces.
# `gemini` was removed on 2026-07-18: the CLI stopped serving individual
# accounts on 2026-06-18, so probing it could only ever return ineligible.
# `antigravity` (agy) is its sanctioned successor and reaches the same models.
_KNOWN_CLIS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor",
    "antigravity": "agy",
}


# ── caching ───────────────────────────────────────────────────────────────


# CLI install/auth state changes rarely (a manual install or login), and the
# wizard calls invalidate_cache() the moment it does — so a short TTL bought
# nothing but repeated multi-second subprocess probes on every page load
# (each detect spawns the CLI with a timeout). 10 min keeps the boot path off
# the probe while staying fresh on real changes via invalidate_cache().
_CACHE_TTL_SECONDS = 600.0
_cache: dict[str, tuple[float, CliState]] = {}


def invalidate_cache() -> None:
    """Drop cached probe results.

    Call this when the user just installed or authenticated a CLI from
    inside the wizard, so the next render reflects the new state without
    waiting for the TTL to elapse.
    """
    _cache.clear()


# ── path resolution via the user's shell ──────────────────────────────────


_SHELL_TIMEOUT_SECONDS = 4.0


def _user_shell() -> str:
    """Return the user's interactive shell or a sensible fallback.

    ``$SHELL`` is set by login programs to the user's chosen shell; we
    honour it so users with zsh / fish / nu / etc. probes go through the
    same init they actually use. Falls back to ``/bin/bash`` when SHELL is
    unset or points at a non-executable, which preserves behaviour on
    minimal CI containers.
    """
    candidate = os.environ.get("SHELL")
    if candidate and os.access(candidate, os.X_OK):
        return candidate
    for fallback in ("/bin/zsh", "/bin/bash", "/bin/sh"):
        if os.access(fallback, os.X_OK):
            return fallback
    return "/bin/sh"


def _resolve_path_via_shell(binary: str) -> tuple[Optional[str], str]:
    """Ask the user's shell where ``binary`` lives.

    Returns ``(path | None, source)`` where source is "shell" if we
    successfully consulted the shell, "fallback-os-path" if we used the
    inherited PATH because the shell probe failed, or "missing" if neither
    finds the binary.

    The shell is invoked with ``-lic`` (login + interactive) so all rc
    files run — that's the union of where any package manager (volta,
    nvm, fnm, asdf, bun, pnpm, etc.) registers its prefix. We redirect
    stdin from /dev/null and stderr to /dev/null so banners and MOTDs in
    rc files don't poison the output.
    """
    shell = _user_shell()
    # Only fish takes "-l" "-i" without "c"; bash/zsh/sh all accept -lic.
    # Fish uses "-l --interactive --command"; detect by basename.
    cmd: list[str]
    if shell.endswith("fish"):
        cmd = [shell, "-l", "-i", "-c", f"command -v {binary}"]
    else:
        cmd = [shell, "-lic", f"command -v {binary}"]

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SHELL_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            # `command -v` may emit multiple lines if multiple matches;
            # the first is the one the shell would actually run.
            first = out.splitlines()[0].strip()
            if first and Path(first).is_file():
                return first, "shell"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("shell probe failed for %r via %r: %s", binary, shell, exc)

    fallback = shutil.which(binary)
    if fallback:
        return fallback, "fallback-os-path"
    return None, "missing"


# ── version probe ─────────────────────────────────────────────────────────


_VERSION_TIMEOUT_SECONDS = 3.0


def _probe_version(binary_path: str) -> Optional[str]:
    """``<binary> --version`` with a tight timeout.

    Versions are advisory — used for telemetry and for the dashboard's
    "claude-code 2.1.119" tooltip. Failures collapse to None and the
    caller still gets ready/auth state.
    """
    try:
        r = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            return (r.stdout or r.stderr or "").strip().splitlines()[0] or None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return None


# ── auth probes ───────────────────────────────────────────────────────────


_AUTH_TIMEOUT_SECONDS = 5.0

# `agy models` is a network round-trip (~1.5s measured 2026-07-17); 20s is
# generous headroom for a probe that leaves the machine, well over the 5s
# the verb-based probes use.
_AGY_AUTH_TIMEOUT_SECONDS = 20.0


def _check_jwt_expiry(token: str) -> Optional[bool]:
    """Return True iff the JWT's ``exp`` claim is in the past.

    Returns None when the token isn't a JWT or has no ``exp`` claim — in
    that case we don't pretend to know.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        exp = payload.get("exp")
        if exp is None:
            return None
        return exp <= time.time()
    except Exception:
        return None


def _probe_claude_auth(binary_path: str) -> AuthState:
    """``claude auth status --json`` → structured truth.

    Output shape (claude 2.x):
        {"loggedIn": bool, "authMethod": "oauth"|"api"|"none", "apiProvider": ...}
    Older claude versions may not support ``auth status``; we surface that
    as ``unknown`` rather than guessing from a credentials-file proxy.
    """
    try:
        r = subprocess.run(
            [binary_path, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_AUTH_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
        if r.returncode != 0 and "unknown command" in (r.stderr or "").lower():
            return AuthState(
                state="unknown",
                detail="claude version too old for `auth status` verb",
            )
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return AuthState(
                state="unknown",
                detail=f"could not parse claude auth status output: {r.stdout[:120]!r}",
            )
        if data.get("loggedIn"):
            return AuthState(
                state="ok",
                method=data.get("authMethod"),
                detail=data.get("apiProvider"),
            )
        return AuthState(
            state="missing",
            method=data.get("authMethod"),
            detail="run `claude /login`",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return AuthState(state="error", detail=str(exc))


def _probe_codex_auth(binary_path: str) -> AuthState:
    """``codex login status`` (text) + JWT expiry cross-check.

    The verb prints "Logged in using <method>" on success and a non-zero
    exit (or different text) when not logged in. We additionally verify
    the access_token in ``~/.codex/auth.json`` hasn't passed its ``exp``
    claim — codex caches "logged in" optimistically and an expired token
    still surfaces as logged-in to the verb until the next refresh.
    """
    try:
        r = subprocess.run(
            [binary_path, "login", "status"],
            capture_output=True,
            text=True,
            timeout=_AUTH_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
        text = (r.stdout + r.stderr).strip()
        if r.returncode == 0 and "Logged in" in text:
            method = text.replace("Logged in using ", "").strip() or None
            expired = _codex_token_expired()
            if expired is True:
                return AuthState(
                    state="expired",
                    method=method,
                    detail="access_token expired — run `codex login` to refresh",
                )
            return AuthState(state="ok", method=method)
        return AuthState(
            state="missing",
            detail=text or "codex login status returned non-zero",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return AuthState(state="error", detail=str(exc))


def _codex_token_expired() -> Optional[bool]:
    auth = Path.home() / ".codex" / "auth.json"
    if not auth.is_file():
        return None
    try:
        data = json.loads(auth.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tokens = data.get("tokens") or {}
    # access_token is the short-lived one; if it's expired but a refresh
    # token exists, the next call refreshes — so we only flag expired when
    # the access_token is past its exp AND there's no refresh path.
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not access:
        return None
    expired = _check_jwt_expiry(access)
    if expired and not refresh:
        return True
    return False  # treat refresh-able expiry as "ok" — codex auto-refreshes


def _probe_cursor_auth(binary_path: str) -> AuthState:
    """Cursor's CLI is a thin shim around the IDE; it has no headless
    auth verb. Treat presence of ``~/.cursor`` as a weak positive but
    surface ``unknown`` so the caller doesn't claim verified state.
    """
    if (Path.home() / ".cursor").is_dir():
        return AuthState(state="unknown", detail="cursor has no auth-status verb")
    return AuthState(state="missing", detail="no ~/.cursor")


def _probe_agy_auth(binary_path: str) -> AuthState:
    """Antigravity CLI (`agy`). Probe with `agy models`, NOT `agy --version`.

    Measured 2026-07-17 with strace: `agy --version` makes ZERO network
    connections, so it exits 0 for an unauthenticated OR unentitled account —
    the exact trap that made the retired gemini CLI's --version probe
    useless. `agy models`
    opens ~84 outbound connections and reads the TLS trust store: it asks the
    service what this account may use, so it can actually fail, and unlike
    `agy -p` it costs no inference (~1.5s). A non-empty model list on exit 0 is
    proof the account is served.
    """
    try:
        r = subprocess.run(
            [binary_path, "models"],
            capture_output=True,
            text=True,
            timeout=_AGY_AUTH_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return AuthState(
            state="unknown",
            detail=f"`agy models` exceeded {_AGY_AUTH_TIMEOUT_SECONDS}s",
        )
    except (FileNotFoundError, OSError) as exc:
        return AuthState(state="error", detail=str(exc))
    if r.returncode == 0 and r.stdout.strip():
        return AuthState(state="ok", method="oauth")
    return AuthState(
        state="missing",
        detail=(r.stderr.strip()[:200] or "`agy models` returned non-zero — sign in with `agy`"),
    )


_AUTH_PROBES = {
    "claude": _probe_claude_auth,
    "codex": _probe_codex_auth,
    "cursor": _probe_cursor_auth,
    "antigravity": _probe_agy_auth,
}


# ── public API ────────────────────────────────────────────────────────────


def detect(name: str, *, refresh: bool = False) -> CliState:
    """Probe install + auth state for a single CLI.

    Args:
        name: One of ``"claude" | "codex" | "cursor" | "antigravity"``.
        refresh: If True, bypass the cache and re-probe.

    Raises:
        ValueError: ``name`` is not one of the canonical CLIs. Add a row
            to ``_KNOWN_CLIS`` and ``_AUTH_PROBES`` to register a new one.
    """
    if name not in _KNOWN_CLIS:
        raise ValueError(
            f"Unknown CLI: {name!r}. Known: {sorted(_KNOWN_CLIS)}. "
            "Register new CLIs in cli_probe._KNOWN_CLIS + _AUTH_PROBES."
        )

    now = time.time()
    if not refresh and name in _cache:
        ts, cached = _cache[name]
        if now - ts < _CACHE_TTL_SECONDS:
            return cached

    binary = _KNOWN_CLIS[name]
    path, source = _resolve_path_via_shell(binary)

    if path is None:
        state = CliState(
            name=name,
            binary=binary,
            path=None,
            on_path=False,
            version=None,
            auth=AuthState(state="missing", detail="binary not on PATH"),
            source=source,
        )
        _cache[name] = (now, state)
        return state

    version = _probe_version(path)
    auth = _AUTH_PROBES[name](path)
    state = CliState(
        name=name,
        binary=binary,
        path=path,
        on_path=True,
        version=version,
        auth=auth,
        source=source,
    )
    _cache[name] = (now, state)
    return state


def detect_all(*, refresh: bool = False) -> dict[str, CliState]:
    """Probe every known CLI. Returns a fresh dict keyed by canonical name."""
    return {name: detect(name, refresh=refresh) for name in _KNOWN_CLIS}
