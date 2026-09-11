# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Provider tool-naming convention probe — detects how each provider's CLI surfaces okuro tools.
# index:
#   imports | _CACHE_DIR | _PROBE_PROMPT | def get_cli_version | def probe_provider
#   def load_observed | def save_observed | def detect_version_changes
#   def maybe_probe_async | def cli_for_provider
#   def _load_attempts | def _save_attempts | def _cooldown_for | def _within_cooldown
#   def _record_attempt | def _acquire_probe_lock | def probe_health_snapshot
# AGENT_HEADER_END -->
"""Probe how each provider's CLI surfaces okuro MCP tools.

Why
---
Tool-naming convention drifts silently when a provider's CLI updates
(seen 2026-04-26: Codex 0.125 stripped the server-prefix, breaking
every previously-hardcoded `okuro.<tool>` reference). The shared
template (providers/template.py) lists three known forms and lets the
agent pick — that's update-tolerant — but the listed forms still need
to stay current.

This module:

1. Runs a probe prompt through each provider's CLI in non-interactive
   mode and captures the agent-reported tool name.
2. Caches the result + the CLI's version string so the next probe
   knows whether to re-run.
3. Exposes ``maybe_probe_async()`` — called from ``bootstrap()`` — that
   does a fast version-string compare and forks a background probe
   only when a CLI's version actually changed. Zero cost on the steady
   state.

Storage
-------
``~/.okuro/cache/cli_versions.json``        last-seen ``<cli> --version`` per provider.
``~/.okuro/cache/observed_conventions.yaml`` last probe result per provider.
``~/.okuro/cache/probe-attempts.json``      per-provider attempt history (cooldown + backoff).
``~/.okuro/cache/probe.lock``               cross-process flock — concurrency cap = 1.

All four are user-scoped caches — wipe them and the next bootstrap re-probes.

Sprint-3F audit (#31): per-provider cooldown, exponential backoff on
repeated failure, ``OKURO_DISABLE_PROBE`` opt-out, ``OKURO_PROBE_TIMEOUT``
override, and a single-flight lock so a 3-CLI version-bump cannot fan out
into 3× 90-second background probes hammering the user's billing quota.
"""

from __future__ import annotations

try:
    import fcntl  # POSIX-only; the single-flight probe lock is a no-op on Windows
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from okuro.db.engine import okuro_home


log = logging.getLogger(__name__)


_CACHE_DIR = okuro_home() / "cache"
_VERSIONS_FILE = _CACHE_DIR / "cli_versions.json"
_OBSERVED_FILE = _CACHE_DIR / "observed_conventions.yaml"
_ATTEMPTS_FILE = _CACHE_DIR / "probe-attempts.json"
_LOCK_FILE = _CACHE_DIR / "probe.lock"


# Cooldown defaults (seconds). Sprint-3F audit #31.
_BASE_COOLDOWN_SECONDS = 4 * 3600           # 4h on success or single failure
_MAX_COOLDOWN_SECONDS = 24 * 3600           # cap after repeated failure
_STUCK_LOCK_SECONDS = 3600                  # lock-file mtime older than this = suspicious
_DEFAULT_PROBE_TIMEOUT = 90                 # per-provider probe timeout default
_FAILURE_BACKOFF_THRESHOLD = 3              # # of consecutive failures before doubling

# Module-level guard so the OKURO_DISABLE_PROBE info-log fires once per process.
_disable_logged = False


def _safe_probe_cwd() -> str:
    """Return a user-owned directory safe to run the probe CLIs from.

    audit fix #3: the probe spawns claude/codex/agy with all-safety-bypass
    flags (--dangerously-skip-permissions, --dangerously-bypass-approvals-
    and-sandbox). Previously cwd was inherited from whatever shell
    started the parent — if the user's shell sat in an attacker-controlled
    project directory containing a crafted .claude/, CLAUDE.md, or AGENTS.md,
    the probe agent would load that config and execute hooks under the
    user's UID. Pinning cwd to ~/.okuro removes the project-directory
    hijack vector. Falls back to /tmp if ~/.okuro doesn't exist.
    """
    home_okuro = okuro_home()
    if home_okuro.is_dir():
        return str(home_okuro)
    try:
        home_okuro.mkdir(parents=True, exist_ok=True)
        return str(home_okuro)
    except OSError:
        return "/tmp"


_PROBE_PROMPT = (
    "Integration probe. Call the okuro bootstrap tool once with "
    'task_hint="convention-probe" and provider="<self>". After the call, '
    "output exactly one line in this format and stop:\n"
    "  PROBE_RESULT tool_name=<exact tool name string> ok=<yes|no>\n"
    "Use whatever tool name appears in your tool list (common forms: "
    "mcp__okuro__bootstrap, mcp_okuro_bootstrap, bootstrap)."
)


# Which CLI binary + how to invoke it non-interactively.
# Provider ids match the value passed to bootstrap()'s ``provider`` argument.
_PROVIDER_CLI = {
    "claude-code": {
        "binary": "claude",
        "version_args": ["--version"],
        "exec_args": lambda prompt: ["-p", prompt, "--dangerously-skip-permissions"],
    },
    "codex": {
        "binary": "codex",
        "version_args": ["--version"],
        "exec_args": lambda prompt: [
            "exec", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ],
    },
    "antigravity": {
        "binary": "agy",
        "version_args": ["--version"],
        "exec_args": lambda prompt: [
            "-p", prompt, "--dangerously-skip-permissions",
        ],
    },
}


_PROBE_RESULT_RE = re.compile(
    r"PROBE_RESULT\s+tool_name=(?P<tool_name>\S+)\s+ok=(?P<ok>\w+)",
    re.IGNORECASE,
)


def cli_for_provider(provider_id: str) -> dict | None:
    """Return the CLI invocation spec for a provider, or None if unsupported."""
    return _PROVIDER_CLI.get(provider_id)


def _probe_timeout() -> int:
    """Effective per-probe timeout — ``OKURO_PROBE_TIMEOUT`` wins if valid."""
    raw = os.environ.get("OKURO_PROBE_TIMEOUT")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            log.warning("OKURO_PROBE_TIMEOUT=%r is not a positive int; using default", raw)
    return _DEFAULT_PROBE_TIMEOUT


def get_cli_version(provider_id: str) -> str | None:
    """Capture ``<cli> --version`` for a provider. Returns None if missing."""
    spec = cli_for_provider(provider_id)
    if not spec:
        return None
    try:
        proc = subprocess.run(
            [spec["binary"], *spec["version_args"]],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=_safe_probe_cwd(),  # audit fix #3: pin cwd
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout + proc.stderr).strip()
    return out.splitlines()[0].strip() if out else None


def probe_provider(provider_id: str, *, timeout: int | None = None) -> dict:
    """Run the probe prompt through a provider's CLI; return the parsed result.

    ``timeout`` defaults to :func:`_probe_timeout` (env-var aware).

    Returns a dict with keys:
      - ``provider_id``
      - ``tool_name``    parsed from the agent's PROBE_RESULT line, or None
      - ``ok``           "yes" / "no" / "unknown"
      - ``raw_tail``     last ~500 chars of the CLI output (for forensic use)
      - ``observed_at``  ISO timestamp
      - ``cli_version``  the version string at probe time, or None
      - ``error``        human-readable failure note, or None on success
    """
    spec = cli_for_provider(provider_id)
    if not spec:
        return {
            "provider_id": provider_id,
            "tool_name": None,
            "ok": "unknown",
            "raw_tail": "",
            "observed_at": _now_iso(),
            "cli_version": None,
            "error": f"no probe spec for provider_id={provider_id!r}",
        }

    cli_version = get_cli_version(provider_id)
    if cli_version is None:
        return {
            "provider_id": provider_id,
            "tool_name": None,
            "ok": "unknown",
            "raw_tail": "",
            "observed_at": _now_iso(),
            "cli_version": None,
            "error": f"binary {spec['binary']!r} not found on PATH",
        }

    effective_timeout = timeout if timeout is not None else _probe_timeout()
    try:
        proc = subprocess.run(
            [spec["binary"], *spec["exec_args"](_PROBE_PROMPT)],
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            cwd=_safe_probe_cwd(),  # audit fix #3: pin cwd to neutralise project-directory hijack
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "provider_id": provider_id,
            "tool_name": None,
            "ok": "no",
            "raw_tail": "",
            "observed_at": _now_iso(),
            "cli_version": cli_version,
            "error": f"timed out after {exc.timeout}s",
        }

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    match = _PROBE_RESULT_RE.search(output)
    raw_tail = output[-500:].strip()

    if not match:
        return {
            "provider_id": provider_id,
            "tool_name": None,
            "ok": "no",
            "raw_tail": raw_tail,
            "observed_at": _now_iso(),
            "cli_version": cli_version,
            "error": "no PROBE_RESULT line in CLI output",
        }

    return {
        "provider_id": provider_id,
        "tool_name": match.group("tool_name"),
        "ok": match.group("ok").lower(),
        "raw_tail": raw_tail,
        "observed_at": _now_iso(),
        "cli_version": cli_version,
        "error": None,
    }


def load_observed() -> dict[str, dict]:
    """Read ``observed_conventions.yaml`` — empty dict if absent or malformed."""
    if not _OBSERVED_FILE.is_file():
        return {}
    try:
        data = yaml.safe_load(_OBSERVED_FILE.read_text()) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def save_observed(observed: dict[str, dict]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _OBSERVED_FILE.write_text(yaml.safe_dump(observed, sort_keys=True))


def _load_versions() -> dict[str, str]:
    if not _VERSIONS_FILE.is_file():
        return {}
    try:
        return json.loads(_VERSIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_versions(versions: dict[str, str]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _VERSIONS_FILE.write_text(json.dumps(versions, indent=2, sort_keys=True))


# ── attempt history (cooldown + backoff) ──────────────────────────────


def _load_attempts() -> dict[str, dict]:
    """Read per-provider attempt history. Empty dict on missing/malformed."""
    if not _ATTEMPTS_FILE.is_file():
        return {}
    try:
        data = json.loads(_ATTEMPTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_attempts(attempts: dict[str, dict]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _ATTEMPTS_FILE.write_text(json.dumps(attempts, indent=2, sort_keys=True))


def _cooldown_for(provider_id: str, attempts: dict[str, dict] | None = None) -> int:
    """Effective cooldown (seconds) for ``provider_id`` given recent failures.

    Backoff doubles ``_BASE_COOLDOWN_SECONDS`` once at least
    ``_FAILURE_BACKOFF_THRESHOLD`` consecutive failures have been recorded,
    and again for each subsequent failure, capped at ``_MAX_COOLDOWN_SECONDS``.
    A successful probe resets the failure counter.
    """
    if attempts is None:
        attempts = _load_attempts()
    entry = attempts.get(provider_id) or {}
    failures = int(entry.get("consecutive_failures", 0) or 0)
    if failures < _FAILURE_BACKOFF_THRESHOLD:
        return _BASE_COOLDOWN_SECONDS
    # 3 failures → 8h, 4 → 16h, 5+ → 24h cap.
    extra = failures - _FAILURE_BACKOFF_THRESHOLD + 1
    cooldown = _BASE_COOLDOWN_SECONDS * (2 ** extra)
    return min(cooldown, _MAX_COOLDOWN_SECONDS)


def _within_cooldown(provider_id: str, attempts: dict[str, dict] | None = None) -> bool:
    """True if the last attempt for this provider is still inside its cooldown."""
    if attempts is None:
        attempts = _load_attempts()
    entry = attempts.get(provider_id)
    if not entry:
        return False
    last_ts = entry.get("last_attempt_ts")
    if not isinstance(last_ts, (int, float)):
        return False
    cooldown = _cooldown_for(provider_id, attempts)
    return (time.time() - float(last_ts)) < cooldown


def _record_attempt(provider_id: str, status: str) -> None:
    """Append one attempt outcome to the history file.

    ``status`` ∈ {"success", "error", "spawned"}. Only "success"/"error"
    influence the consecutive-failure counter; "spawned" merely stamps
    the cooldown timer (it means we forked a child probe and don't yet
    know its outcome — we still want the cooldown to apply so we don't
    re-fork the same provider on every bootstrap call).
    """
    attempts = _load_attempts()
    entry = attempts.get(provider_id) or {}
    failures = int(entry.get("consecutive_failures", 0) or 0)
    if status == "success":
        failures = 0
    elif status == "error":
        failures += 1
    # status == "spawned": leave failures untouched.
    entry["last_attempt_ts"] = time.time()
    entry["last_status"] = status
    entry["consecutive_failures"] = failures
    attempts[provider_id] = entry
    _save_attempts(attempts)


# ── single-flight lock ────────────────────────────────────────────────


def _acquire_probe_lock() -> int | None:
    """Try to take the cross-process probe lock.

    Returns the open fd holding an exclusive ``flock`` on
    ``~/.okuro/cache/probe.lock``, or ``None`` if another process already
    holds it. The caller is responsible for either closing the fd (which
    releases the lock) or for handing it to a child process via
    ``Popen(pass_fds=(fd,))`` — in the latter case the underlying open
    file description survives until every fd referring to it is closed,
    which keeps the kernel-held flock alive for the child's lifetime.
    """
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        log.warning("probe lock open failed: %s", exc)
        return None
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return None
    # else (Windows): no advisory flock — proceed without single-flight
    # protection. Worst case is a rare duplicate background probe, which the
    # per-provider cooldown already bounds. Better than crashing at import.
    # Stamp mtime + pid so doctor can spot a stuck lock.
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
    except OSError:
        pass
    try:
        os.set_inheritable(fd, True)
    except OSError:
        pass
    return fd


def detect_version_changes(provider_ids: list[str] | None = None) -> list[str]:
    """Compare current CLI versions vs cache; record current; return changed providers.

    Updates ``cli_versions.json`` in place. Returns the subset of
    ``provider_ids`` whose version differs from the cached value (or whose
    cache entry was missing). Cheap — three subprocess calls of <10ms each.
    """
    targets = provider_ids or list(_PROVIDER_CLI.keys())
    cached = _load_versions()
    current: dict[str, str] = dict(cached)  # carry forward unchanged providers
    changed: list[str] = []

    for pid in targets:
        version = get_cli_version(pid)
        if version is None:
            # CLI not installed; skip silently
            continue
        if cached.get(pid) != version:
            changed.append(pid)
        current[pid] = version

    _save_versions(current)
    return changed


def maybe_probe_async() -> list[str]:
    """Bootstrap-time version check. Forks at most ONE background probe per call.

    Sprint-3F audit (#31) constraints:
      * ``OKURO_DISABLE_PROBE`` env-var hard-skips the entire call.
      * Per-provider cooldown (``probe-attempts.json``) skips re-probes
        that happened in the last 4h (longer if recent failures).
      * Cross-process flock on ``probe.lock`` caps in-flight probes at 1
        across the whole user session; remaining changed providers stay
        marked-changed and will be picked up by a later bootstrap.

    Returns the list of providers whose probes were actually spawned.
    """
    global _disable_logged
    if os.environ.get("OKURO_DISABLE_PROBE") == "1":
        if not _disable_logged:
            log.info("probe disabled via OKURO_DISABLE_PROBE")
            _disable_logged = True
        return []

    try:
        changed = detect_version_changes()
    except Exception:
        return []

    if not changed:
        return []

    attempts = _load_attempts()
    spawned: list[str] = []
    for pid in changed:
        if _within_cooldown(pid, attempts):
            log.debug("probe cooldown active for %s; skipping", pid)
            continue

        lock_fd = _acquire_probe_lock()
        if lock_fd is None:
            # Another probe is in flight — bail. The remaining changed
            # providers stay marked-changed (cli_versions.json was already
            # updated), so the next bootstrap call will retry them once
            # the lock is free and their cooldown allows.
            log.debug(
                "probe lock held; deferring remaining providers (%s)",
                ",".join(changed[changed.index(pid):]),
            )
            break

        try:
            subprocess.Popen(  # noqa: S603 — controlled args
                [
                    sys.executable,
                    "-m", "okuro.sense.providers.probe",
                    "--provider", pid,
                    "--background",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=_safe_probe_cwd(),  # audit fix #3: pin cwd
                pass_fds=(lock_fd,),    # keep flock alive for child's lifetime
                close_fds=True,
            )
            spawned.append(pid)
            _record_attempt(pid, "spawned")
        except Exception as exc:
            log.warning("probe Popen failed for %s: %s", pid, exc)
        finally:
            # Close OUR fd. The kernel keeps the open-file-description (and
            # therefore the flock) alive because the child inherited the
            # same fd via ``pass_fds``. When the child exits, the kernel
            # drops the last reference and releases the lock automatically.
            try:
                os.close(lock_fd)
            except OSError:
                pass

        # Single-flight: only ever spawn ONE probe per maybe_probe_async() call.
        break

    return spawned


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_probe_result(result: dict) -> None:
    """Merge a probe result into the observed_conventions cache.

    If the probe detected a tool name not already in the template's default
    list, regenerate the matching provider's instructions file so the next
    session picks up the new form. Regen failures are swallowed — a probe
    must never fail loudly enough to break the bootstrap that triggered it.
    """
    observed = load_observed()
    pid = result["provider_id"]
    observed[pid] = {k: v for k, v in result.items() if k != "raw_tail"}
    save_observed(observed)

    # Update attempt history with the real outcome. ``ok == "yes"`` resets
    # the failure counter; anything else counts as an error and feeds the
    # backoff calculation.
    status = "success" if result.get("ok") == "yes" else "error"
    _record_attempt(pid, status)

    if result.get("ok") != "yes" or not result.get("tool_name"):
        return

    try:
        from .template import _DEFAULT_BOOTSTRAP_FORMS
        defaults = {name for name, _ in _DEFAULT_BOOTSTRAP_FORMS}
        if result["tool_name"] in defaults:
            return  # already covered by hardcoded list — no regen needed
        from . import get_provider
        # Probe uses bootstrap()'s provider-id form ("claude-code"); the
        # adapter registry uses the shorter binary-name form ("claude").
        adapter_name = _PROBE_TO_ADAPTER_NAME.get(pid, pid)
        adapter = get_provider(adapter_name)
        if adapter is not None:
            adapter.generate_instructions()
    except Exception:
        pass


# Map probe-side provider ids → registered adapter names.
_PROBE_TO_ADAPTER_NAME = {
    "claude-code": "claude",
}


# ── doctor surface ────────────────────────────────────────────────────


def probe_health_snapshot() -> dict:
    """Return a structured snapshot of probe state for doctor / diagnostics.

    Shape::

        {
          "disabled": bool,                  # OKURO_DISABLE_PROBE=1
          "lock": {
              "exists": bool,
              "age_seconds": int | None,     # mtime → now
              "stuck": bool,                 # age > _STUCK_LOCK_SECONDS
          },
          "providers": {
              "<pid>": {
                  "last_attempt_ts": float | None,
                  "last_status": str | None,
                  "consecutive_failures": int,
                  "cooldown_seconds": int,
                  "in_cooldown": bool,
              },
              ...
          },
        }
    """
    disabled = os.environ.get("OKURO_DISABLE_PROBE") == "1"

    lock_info: dict = {"exists": False, "age_seconds": None, "stuck": False}
    if _LOCK_FILE.exists():
        try:
            mtime = _LOCK_FILE.stat().st_mtime
            age = max(0, int(time.time() - mtime))
            # mtime alone is unreliable: probe children inherit the fd via
            # pass_fds and exit within ~90s, releasing the kernel flock —
            # but nothing unlinks the file or re-stamps mtime. Confirm a
            # genuinely held lock with a non-blocking flock probe before
            # flagging it as stuck.
            held = False
            if fcntl is not None:
                try:
                    fd = os.open(str(_LOCK_FILE), os.O_RDWR)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except (BlockingIOError, OSError):
                        held = True
                    finally:
                        os.close(fd)
                except OSError:
                    pass
            # On Windows (no fcntl) we can't probe lock-held state; report
            # not-held rather than crashing the doctor check.
            lock_info = {
                "exists": True,
                "age_seconds": age,
                "held": held,
                "stuck": held and age > _STUCK_LOCK_SECONDS,
            }
            if lock_info["stuck"]:
                log.warning(
                    "probe lock-file %s mtime is %ds old (>%ds) and flock held — stuck",
                    _LOCK_FILE, age, _STUCK_LOCK_SECONDS,
                )
        except OSError:
            pass

    attempts = _load_attempts()
    providers: dict[str, dict] = {}
    for pid in _PROVIDER_CLI:
        entry = attempts.get(pid) or {}
        cooldown = _cooldown_for(pid, attempts)
        providers[pid] = {
            "last_attempt_ts": entry.get("last_attempt_ts"),
            "last_status": entry.get("last_status"),
            "consecutive_failures": int(entry.get("consecutive_failures", 0) or 0),
            "cooldown_seconds": cooldown,
            "in_cooldown": _within_cooldown(pid, attempts),
        }

    return {
        "disabled": disabled,
        "lock": lock_info,
        "providers": providers,
    }


def _main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m okuro.sense.providers.probe --provider <id>``."""
    import argparse

    parser = argparse.ArgumentParser(prog="okuro-probe-conventions")
    parser.add_argument(
        "--provider",
        action="append",
        help="Provider id to probe. Repeatable. Default: every provider with a known CLI.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Per-provider probe timeout in seconds "
             "(default: OKURO_PROBE_TIMEOUT or 90).",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Quiet mode for background invocations from bootstrap.",
    )
    args = parser.parse_args(argv)

    targets = args.provider or list(_PROVIDER_CLI.keys())
    rows: list[dict] = []
    for pid in targets:
        result = probe_provider(pid, timeout=args.timeout)
        _record_probe_result(result)
        rows.append(result)

    if args.background:
        return 0

    # Foreground: print a tiny report.
    for r in rows:
        if r["ok"] == "yes" and r["tool_name"]:
            print(f"  {r['provider_id']:14s} → {r['tool_name']}    (cli={r['cli_version']})")
        else:
            print(f"  {r['provider_id']:14s} → FAILED    error={r['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
