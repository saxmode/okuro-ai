# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Unified surface log — objective-utility substrate for memory + thought surfacings (Meta-Harness P8).
# index: imports | logger | _VALID_KINDS | _resolve_session_id | log_surface | log_thought_surface | log_memory_surface
# AGENT_HEADER_END -->
"""Unified surface log — objective-utility substrate for memory + thought surfacings.

Every time a memory or a thought is shown to an agent (bootstrap sections,
read_memory, search_thoughts, digest, etc.) callers log a row here. Joining
``surface_log`` against ``sessions.compliance_normalized`` yields an external
(non-self-rated) signal for whether surfacing a given entity correlates with
better or worse session outcomes — see Meta-Harness P8.

For thoughts, ``log_thought_surface`` also bumps ``thoughts.surface_count`` and
stamps ``thoughts.last_surfaced`` atomically so the "Forgotten Ideas" bucket
(digest) and the ``surface_count=0`` filters in maintenance have real signal.

Supersedes the memory-only ``memory_surface_log`` from migration 018; the
backfill + drop happens in migration 024.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_VALID_KINDS = ("memory", "thought")

# Process-local counter for surface-log write failures. Bucket math
# (forgotten thoughts, never-surfaced memories) silently corrupts when
# surface rows fail to land — previously losses only went to stderr.
# `run_hygiene` reads this so a stuck DB or schema drift becomes
# immediately visible in the maintenance report instead of accumulating
# invisibly. Thread-safe because daemon callers + MCP callers can race.
_failure_counts: dict[str, int] = {"memory": 0, "thought": 0}
_failure_lock = threading.Lock()


def get_and_reset_failure_counts() -> dict[str, int]:
    """Return current failure counts and reset them. Called by maintenance."""
    with _failure_lock:
        snapshot = dict(_failure_counts)
        for k in _failure_counts:
            _failure_counts[k] = 0
        return snapshot


def _record_failure(kind: str) -> None:
    with _failure_lock:
        _failure_counts[kind] = _failure_counts.get(kind, 0) + 1


def _resolve_session_id() -> Optional[str]:
    """Best-effort lookup of the current okuro session id.

    Mirrors the pattern in ``okuro.sense.memory._log_surfaces``: imports
    lazily, swallows any failure, returns ``None`` when no session is active.
    """
    try:
        from okuro.sense.session_state import get_session_state

        return (get_session_state() or {}).get("session_id")
    except Exception:
        return None


# G6 instrumentation. surface_log.session_id is ~89% NULL, starving the only
# automated confidence loop (memory_utility joins surface_log against
# sessions.compliance_normalized on session_id). Root cause is a split: the
# telemetry sid the join needs is bound ONLY by bootstrap, into THIS request's
# session_state dict; the transport contextvar (current_session_id) is a
# different id. When resolution yields None on an agent-facing context, this
# records WHY, bucketed by the transport id so the dominant mechanism is
# measurable before a fix is chosen:
#   'worker_thread_lost_contextvar' — contextvar fell back to the "stdio"
#       default (HTTP request work ran in a thread without copy_context),
#       so get_session_state() returned the unbound stdio dict.
#   'anonymous_client' — http-anon / inline-anon caller, genuinely
#       session-less; a null here is expected, not a defect.
#   'sid_never_bound' — a real transport session whose telemetry sid was
#       never bound (e.g. a standalone read_memory with no bootstrap in it).
# Background surfacings (digest, morning brief, hygiene) legitimately have no
# session and are NOT counted. Read tallies via get_and_reset_null_session_diag().
_SESSIONFUL_CONTEXTS = ("bootstrap_", "read_memory", "search_thoughts")

_null_session_diag: dict[str, int] = {}
_null_session_lock = threading.Lock()


def _null_session_bucket(context: str) -> Optional[str]:
    """Transport-id bucket for an unresolved session, or None if not counted."""
    if not (context.startswith("bootstrap_")
            or context in _SESSIONFUL_CONTEXTS):
        return None
    try:
        from okuro.sense.session_state import current_session_id
        tid = current_session_id.get()
    except Exception:
        return "sid_never_bound"
    if tid == "stdio":
        return "worker_thread_lost_contextvar"
    if tid.endswith("-anon"):
        return "anonymous_client"
    return "sid_never_bound"


def _record_null_session(context: str) -> None:
    """Tally + throttled-WARN an agent-facing surfacing that resolved no sid."""
    bucket = _null_session_bucket(context)
    if bucket is None:
        return
    key = f"{context}|{bucket}"
    with _null_session_lock:
        n = _null_session_diag.get(key, 0) + 1
        _null_session_diag[key] = n
    if n == 1 or n % 500 == 0:
        logger.warning(
            "surface session_id UNRESOLVED — context=%s bucket=%s count=%d "
            "(memory_utility signal lost for this row)", context, bucket, n)


def get_and_reset_null_session_diag() -> dict[str, int]:
    """Return per-(context|bucket) null-session counts and reset. For maintenance."""
    with _null_session_lock:
        snapshot = dict(_null_session_diag)
        _null_session_diag.clear()
        return snapshot


def log_surface(
    kind: str,
    entity_id: str,
    context: str,
    session_id: Optional[str] = None,
) -> None:
    """Log a surfacing of a memory or thought. Dispatches by ``kind``.

    Args:
        kind: Either ``'memory'`` or ``'thought'``.
        entity_id: The memory id or thought id.
        context: Short label for the surface path
            (e.g. ``'read_memory'``, ``'bootstrap_relevant'``,
            ``'search_thoughts'``, ``'digest'``).
        session_id: Active okuro session id. If omitted, resolved from the
            session_state contextvar.

    Raises:
        ValueError: if ``kind`` is not a recognised surface kind.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"invalid surface kind {kind!r}; expected one of {_VALID_KINDS}"
        )
    if kind == "thought":
        log_thought_surface(entity_id, context, session_id)
    else:
        log_memory_surface(entity_id, context, session_id)


def log_thought_surface(
    thought_id: str,
    context: str,
    session_id: Optional[str] = None,
) -> None:
    """Log that a thought was surfaced.

    Atomically INSERTs the surface row AND bumps
    ``thoughts.surface_count`` / ``thoughts.last_surfaced``. The write is a
    single transaction: either the log row and the counter update both
    commit, or neither does.

    Failures are logged to stderr at WARNING level — surfacing a thought
    must never break the calling read path, but we refuse to silently
    swallow errors the way the old memory-only path did (that's part of
    how end-to-end surface tracking rotted in the first place).
    """
    if session_id is None:
        session_id = _resolve_session_id()
        if session_id is None:
            _record_null_session(context)
    try:
        from okuro.db import get_db

        db = get_db()
        with db.write() as conn:
            conn.execute(
                "INSERT INTO surface_log (kind, entity_id, session_id, context) "
                "VALUES ('thought', ?, ?, ?)",
                (thought_id, session_id, context),
            )
            conn.execute(
                "UPDATE thoughts "
                "SET surface_count = surface_count + 1, "
                "    last_surfaced = datetime('now') "
                "WHERE id = ?",
                (thought_id,),
            )
    except Exception as exc:  # pragma: no cover — defensive, covered by test
        _record_failure("thought")
        logger.warning(
            "log_thought_surface failed (thought_id=%s, context=%s): %s",
            thought_id,
            context,
            exc,
        )
        print(
            f"WARNING: log_thought_surface failed "
            f"(thought_id={thought_id}, context={context}): {exc}",
            file=sys.stderr,
        )


def log_memory_surface(
    memory_id: str,
    context: str,
    session_id: Optional[str] = None,
) -> None:
    """Log that a memory was surfaced.

    Inserts into ``surface_log`` only — memories don't have a denormalised
    counter column (``agent_memory.last_accessed`` is bumped separately by
    ``okuro.sense.memory._log_surfaces`` because some call sites batch many
    memories in one transaction).

    Failures are logged to stderr at WARNING level — see
    ``log_thought_surface`` for rationale.
    """
    if session_id is None:
        session_id = _resolve_session_id()
        if session_id is None:
            _record_null_session(context)
    try:
        from okuro.db import get_db

        db = get_db()
        db.execute(
            "INSERT INTO surface_log (kind, entity_id, session_id, context) "
            "VALUES ('memory', ?, ?, ?)",
            (memory_id, session_id, context),
        )
    except Exception as exc:  # pragma: no cover — defensive, covered by test
        _record_failure("memory")
        logger.warning(
            "log_memory_surface failed (memory_id=%s, context=%s): %s",
            memory_id,
            context,
            exc,
        )
        print(
            f"WARNING: log_memory_surface failed "
            f"(memory_id={memory_id}, context={context}): {exc}",
            file=sys.stderr,
        )
