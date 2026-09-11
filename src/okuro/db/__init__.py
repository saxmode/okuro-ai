# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.db — database abstraction.
# index: def get_db | def reset_db
# AGENT_HEADER_END -->
"""okuro.db — database abstraction."""

from contextlib import contextmanager

from .interface import OkuroDB

_instance: OkuroDB | None = None


def get_db(config: dict | None = None) -> OkuroDB:
    """Get the singleton database instance. Creates on first call."""
    global _instance
    if _instance is None:
        from .engine import create_db

        _instance = create_db(config)
    return _instance


def reset_db() -> None:
    """Close and reset the singleton. For testing only."""
    global _instance
    if _instance is not None:
        _instance.close()
        _instance = None


@contextmanager
def temporary_busy_timeout(db: OkuroDB, timeout_ms: int):
    """Temporarily lower SQLite lock waits for best-effort hot-path writes.

    Non-SQLite backends are a no-op. This keeps auxiliary telemetry from
    blocking MCP responses while preserving the normal connection setting for
    real database work.
    """
    conn = getattr(db, "conn", None)
    if conn is None:
        yield
        return

    previous: int | None = None
    try:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        if row is not None:
            if isinstance(row, dict):
                previous = int(next(iter(row.values())))
            else:
                previous = int(row[0])
        conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    except Exception:
        previous = None

    try:
        yield
    finally:
        if previous is not None:
            try:
                conn.execute(f"PRAGMA busy_timeout={previous}")
            except Exception:
                pass


__all__ = ["OkuroDB", "get_db", "reset_db", "temporary_busy_timeout"]
