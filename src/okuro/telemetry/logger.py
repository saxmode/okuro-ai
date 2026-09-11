# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MCP tool usage telemetry logger.
# index:
#   imports
#   def _lock_file
#   def _unlock_file
#   def _marker_path
#   def _reported_path
#   def _my_provider
#   def _ensure_dir
#   def _scan_markers
#   def get_session_info
#   def get_session_id
#   def _sanitize_provider
#   def _sanitize_task_hint
#   def write_bootstrap_marker
#   def check_bootstrap
#   def check_session_end
#   def mark_session_reported
#   def log_call
#   def rotate_if_needed
# AGENT_HEADER_END -->
"""MCP tool usage telemetry logger.

Writes to ~/.okuro/telemetry/usage.jsonl (append-only).
Reads bootstrap marker for session context.
Zero heavy dependencies — stdlib only.

Session isolation: Each provider gets its own marker file
so concurrent agents don't collide.
"""

import glob as _glob
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from okuro.db.engine import okuro_home

TELEMETRY_DIR = okuro_home() / "telemetry"
USAGE_FILE = TELEMETRY_DIR / "usage.jsonl"
ARCHIVE_DIR = TELEMETRY_DIR / "archive"
MARKER_DIR = Path(tempfile.gettempdir())
MARKER_PREFIX = ".okuro-session-"
REPORTED_PREFIX = ".okuro-reported-"

ROTATE_THRESHOLD_BYTES = 10 * 1024 * 1024  # 10 MB
ROTATE_KEEP_DAYS = 14

# Keyed by session id, NOT process-global: one HTTP daemon serves many clients,
# so a single cached dict would hand the first caller's session_id and provider
# to every other client for the whole TTL. Stdio has exactly one session ("stdio")
# and so keeps a one-entry map — same behaviour as before.
_session_cache: dict[str, tuple[dict, float]] = {}
_SESSION_CACHE_TTL = 60

_PROCESS_START_TIME = time.time()
_tool_call_count = 0

_KNOWN_PROVIDERS = {"claude-code", "gemini", "codex", "cursor", "antigravity-cli", "unknown"}


def _lock_file(f):
    """Acquire an exclusive advisory lock on an open file. Cross-platform."""
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _unlock_file(f):
    """Release advisory lock. Cross-platform."""
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _marker_path(provider: str, pid: int | None = None) -> Path:
    """Per-(provider, pid) bootstrap marker.

    Historical (≤ 2026-04-20) behaviour was one marker per provider
    regardless of how many MCP subprocesses were running. Under concurrent
    claude-code terminals, each bootstrap overwrote the shared file and
    every subprocess's subsequent telemetry got attributed to whichever
    CLI had bootstrapped most recently — making pulse/Live-Agents show
    one row for "claude-code" no matter how many were actually alive.

    Pid-scoping kills that class of bug: each process has its own marker,
    its own session id, its own reported marker. get_session_info() reads
    its OWN pid's marker; cross-process discovery (check_bootstrap) still
    works via the existing glob scan.
    """
    safe = provider.replace("/", "-").replace(" ", "-")
    p = pid if pid is not None else os.getpid()
    return MARKER_DIR / f"{MARKER_PREFIX}{safe}-{p}"


def _reported_path(provider: str, pid: int | None = None) -> Path:
    """Per-(provider, pid) 'session_report called' sentinel."""
    safe = provider.replace("/", "-").replace(" ", "-")
    p = pid if pid is not None else os.getpid()
    return MARKER_DIR / f"{REPORTED_PREFIX}{safe}-{p}"


def _cache_key() -> str:
    """Cache scope: the MCP session under HTTP, the constant "stdio" otherwise."""
    try:
        from okuro.sense.session_state import current_session_id

        return current_session_id.get()
    except Exception:
        return "stdio"


def _my_provider() -> str:
    """Who is calling — the resolver first, per-session state second.

    Stdio gives every client its own process, so OKURO_PROVIDER is injected per
    client and is authoritative. HTTP does not: one daemon serves Claude, Codex,
    Cursor and Gemini together, its env carries no OKURO_PROVIDER, and every
    client would otherwise log as "unknown" — collapsing per-provider compliance
    into one bucket. bootstrap() already records the caller's provider in the
    per-session state, and the transport middleware scopes that state to this
    request via contextvar.

    ``resolve_agent_provider`` is what makes the first step honest on BOTH
    transports: it reads the per-request session binding (migration 128) when
    there is one and the process environment when there is not, so a dispatched
    subagent gets ``orch-<role>`` instead of the daemon's silence. The session
    state stays as the second step for browser/inline sessions that carry no
    dispatch identity.
    """
    from okuro.sense.work_identity import resolve_agent_provider

    env = resolve_agent_provider()
    if env:
        return env
    try:
        from okuro.sense.session_state import get_session_state

        provider = get_session_state().get("provider")
        if provider and provider != "unknown":
            return provider
    except Exception:
        pass
    return "unknown"


def _ensure_dir():
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)


def _scan_markers(max_age: int = 14400, pid: int | None = None) -> list[dict]:
    """Return live bootstrap markers under MARKER_DIR.

    With ``pid``, restrict the scan to markers owned by that process.
    Used by ``get_session_info`` so a stdio subprocess that lacks the
    OKURO_PROVIDER env var resolves to its own bootstrap marker rather
    than a fresher marker from another agent's terminal.
    """
    results = []
    now = time.time()
    suffix = f"-{pid}" if pid is not None else ""
    for p in MARKER_DIR.glob(f"{MARKER_PREFIX}*{suffix}"):
        try:
            data = json.loads(p.read_text())
            if data.get("ts", 0) > 0 and (now - data["ts"]) < max_age:
                results.append(data)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return results


def get_session_info() -> dict:
    """Read session info from bootstrap marker. Cached in-process.

    Resolution order:
      1. OKURO_PROVIDER / TM_PROVIDER env → per-(provider, this-pid) marker
      2. Any marker for THIS pid (cross-provider, pid-scoped)
      3. ``unknown``

    Prior behaviour fell back to the freshest marker across ALL pids and
    providers. That mis-attributed tool calls when concurrent agent CLIs
    ran: a Gemini stdio subprocess without OKURO_PROVIDER would pick up
    a fresher claude-code marker from another terminal and stamp every
    tool call as claude-code in ~/.okuro/telemetry/usage.jsonl.
    """
    cache_key = _cache_key()
    hit = _session_cache.get(cache_key)
    if hit is not None and (time.time() - hit[1]) < _SESSION_CACHE_TTL:
        return hit[0]

    info = {"session_id": "unknown", "provider": "unknown", "task_hint": "", "ts": 0.0}
    provider = _my_provider()

    # HTTP: the per-session state is authoritative and already holds this
    # client's own session_id and provider (set by bootstrap under its
    # contextvar). Markers are keyed by (provider, PID) — under one shared
    # daemon every client has the SAME pid, so two concurrent claude-code
    # clients would collide on one marker and a marker scan would hand back
    # whichever bootstrapped last. Prefer the session; fall back to markers for
    # stdio, whose pid genuinely identifies the client.
    try:
        from okuro.sense.session_state import get_session_state

        state = get_session_state()
        sid = state.get("session_id")
        if sid:
            info.update({
                "session_id": sid,
                "provider": state.get("provider") or provider,
                "task_hint": state.get("task_hint") or "",
            })
            _session_cache[cache_key] = (info, time.time())
            return info
    except Exception:
        pass

    if provider != "unknown":
        marker = _marker_path(provider)
        try:
            if marker.exists():
                data = json.loads(marker.read_text())
                info.update(data)
                _session_cache[cache_key] = (info, time.time())
                return info
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    markers = _scan_markers(pid=os.getpid())
    if markers:
        freshest = max(markers, key=lambda m: m.get("ts", 0))
        info.update(freshest)
        _session_cache[cache_key] = (info, time.time())

    return info


def get_session_id() -> str:
    return get_session_info().get("session_id", "unknown")


def _sanitize_provider(provider: str) -> str:
    # Same resolver as _my_provider — an os.environ read here answered with the
    # shared daemon's environment on every HTTP call.
    from okuro.sense.work_identity import resolve_agent_provider

    if provider in _KNOWN_PROVIDERS:
        return provider
    env = resolve_agent_provider() or "unknown"
    return env if env in _KNOWN_PROVIDERS else "unknown"


def _sanitize_task_hint(hint: str) -> str:
    cleaned = re.split(r'["\']?\s*>\s*<', hint, maxsplit=1)[0]
    return cleaned.strip()[:500]


def write_bootstrap_marker(provider: str = "unknown", task_hint: str = "") -> str:
    """Write bootstrap marker. Called by okuro.sense bootstrap(). Returns session_id."""
    global _session_cache, _session_cache_ts, _tool_call_count
    provider = _sanitize_provider(provider)
    task_hint = _sanitize_task_hint(task_hint)
    data = {
        "session_id": str(uuid.uuid4()),
        "provider": provider,
        "task_hint": task_hint,
        "ts": time.time(),
        "pid": os.getpid(),
    }
    try:
        _marker_path(provider).write_text(json.dumps(data))
    except OSError:
        pass
    try:
        _reported_path(provider).unlink(missing_ok=True)
    except OSError:
        pass
    _session_cache = data
    _session_cache_ts = time.time()
    _tool_call_count = 0
    return data["session_id"]


def check_bootstrap(max_age: int = 14400) -> str | None:
    """Check if bootstrap was called THIS session. Returns warning or None."""
    provider = _my_provider()

    if provider != "unknown":
        marker = _marker_path(provider)
        try:
            if marker.exists():
                data = json.loads(marker.read_text())
                if data.get("ts", 0) > _PROCESS_START_TIME:
                    return None
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    try:
        for marker_path in _glob.glob(str(MARKER_DIR / f"{MARKER_PREFIX}*")):
            try:
                data = json.loads(open(marker_path).read())
                if data.get("ts", 0) > _PROCESS_START_TIME:
                    return None
            except (OSError, json.JSONDecodeError, ValueError):
                pass
    except Exception:
        pass

    return (
        "bootstrap() not called this session. "
        "Call bootstrap(task_hint='your task') first."
    )


def check_session_end() -> str | None:
    """Return reminder if session_report() hasn't been called yet."""
    global _tool_call_count
    _tool_call_count += 1

    if _tool_call_count <= 1:
        return None

    provider = _my_provider()
    has_session = False
    if provider != "unknown":
        has_session = _marker_path(provider).exists()
    if not has_session:
        has_session = bool(_scan_markers(14400))
    if not has_session:
        return None

    try:
        if _reported_path(provider).exists():
            return None
    except OSError:
        pass

    return "Before ending: call session_report() to rate tools."


def mark_session_reported(provider: str | None = None):
    """Mark session_report() as called for this provider."""
    prov = provider or _my_provider()
    try:
        _reported_path(prov).write_text(json.dumps({"ts": time.time(), "provider": prov}))
    except OSError:
        pass


def log_call(
    server: str,
    tool: str,
    latency_ms: int | None = None,
    ok: bool = True,
    arguments: dict | None = None,
    session_id: str | None = None,
    preview: str | None = None,
):
    """Log a single MCP tool call to the JSONL telemetry file.

    The entry carries the writer's `pid` so downstream consumers (the
    orchestrator's agents-layer heartbeat stamp) can attribute presence
    to the process that actually ran the tool, regardless of what the
    marker-derived `session` field says. With per-pid markers this is
    belt-and-suspenders; without them it was the only correct signal.
    """
    _ensure_dir()
    info = get_session_info()
    provider = _my_provider()
    if provider == "unknown":
        provider = info.get("provider", "unknown")
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": server,
        "tool": tool,
        "session": session_id or info.get("session_id", "unknown"),
        "provider": provider,
        "pid": os.getpid(),
        "latency_ms": latency_ms,
        "ok": ok,
    }
    from okuro.sense.work_identity import resolve_session_type as _rst

    session_type = _rst()
    if session_type:
        entry["session_type"] = session_type
    # Subagent attribution — dispatcher.py:152-167 stamps these env vars
    # onto every subagent's child process. Without them on each tool-call
    # entry, the activity-feed component cannot scope events to a specific
    # subtask: role-researcher Phase 0 spawned tool calls that broadcast
    # arrived with task_id=null/subtask_id=null, the per-subtask filter
    # dropped them, and the panel looked silent while work was running.
    # Work identity comes from the RESOLVER, never os.environ. The live
    # MCP transport is HTTP to ONE shared daemon whose environment belongs
    # to no subagent, so an env read here is False on every real call and
    # this telemetry silently never fires. F1 fixed the three sites its
    # evidence named; this one survived. Measured 2026-08-03 on
    # task-20260801-124606: bootstrap_sizes 0, role_body_fetched 0, while
    # role_slice (emitted dispatcher-side, which DOES have the env) fired.
    from okuro.sense.work_identity import resolve_work_identity as _rwi

    _wi = _rwi()
    task_id = (_wi.task_id if _wi else None) or None
    if task_id:
        entry["task_id"] = task_id
    subtask_id = (_wi.subtask_id if _wi else None) or None
    if subtask_id:
        entry["subtask_id"] = subtask_id
    from okuro.sense.work_identity import resolve_subtask_role as _rsr

    subtask_role = _rsr()
    if subtask_role:
        entry["subtask_role"] = subtask_role
    if preview:
        entry["preview"] = preview[:40]
    if arguments:
        entry["arg_keys"] = sorted(arguments.keys())
    try:
        with open(USAGE_FILE, "a") as f:
            _lock_file(f)
            try:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
                f.flush()
            finally:
                _unlock_file(f)
    except OSError:
        pass


def rotate_if_needed(
    path: Path | None = None,
    threshold_bytes: int = ROTATE_THRESHOLD_BYTES,
    keep_days: int = ROTATE_KEEP_DAYS,
) -> Path | None:
    """Rotate ``usage.jsonl`` if it has grown past the threshold, prune old archives.

    Audit ref: 06-stability.md HIGH-5. The file is append-only JSONL with no
    size cap; heavy MCP users (multiple concurrent agents) would otherwise
    accumulate MB/day indefinitely. The daemon calls this once per day.

    Returns the archived path if rotation occurred, else None. Always runs
    the archive-retention sweep regardless of whether the current file was
    rotated so old files never linger.

    Race-safety: ``log_call`` opens the file fresh on every write (see above).
    Rename atomically repoints the inode; the next writer calls ``open(...,
    "a")`` and creates a new empty file. No risk of half-written entries in
    the archive — the archive is exactly whatever had been fsync'd before
    the rename.
    """
    target = path or USAGE_FILE
    rotated: Path | None = None

    try:
        _ensure_dir()
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        if target.exists() and target.stat().st_size > threshold_bytes:
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            archive_path = ARCHIVE_DIR / f"{target.stem}-{stamp}{target.suffix}"
            # ``Path.rename`` is POSIX rename(2) — atomic on the same FS.
            target.rename(archive_path)
            # Recreate an empty file with 0600 so the subsequent writer's
            # open-for-append doesn't fall back to umask-default perms
            # (which on some systems are 0644, leaking telemetry to any
            # local user). On Windows the chmod is a no-op; file perms
            # there rely on NTFS ACLs, which we don't try to manage here.
            target.touch(mode=0o600, exist_ok=True)
            rotated = archive_path
    except OSError:
        # Rotation is best-effort — a transient FS error shouldn't kill
        # the daemon task. The next invocation will retry.
        return None

    # Always prune old archives, even if we didn't rotate this pass.
    try:
        cutoff = time.time() - keep_days * 86400
        if ARCHIVE_DIR.exists():
            for f in ARCHIVE_DIR.iterdir():
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
    except OSError:
        pass

    return rotated
