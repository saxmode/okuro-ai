# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Persistent signal dismissal store — backed by okuro.db (SQLite).
# index: class SignalDismissalStore
# AGENT_HEADER_END -->
"""Persistent signal dismissal store — backed by okuro.db (SQLite).

Stores dismissed signal IDs so they never reappear after a service restart.
Falls back to in-memory-only mode if the database is unavailable.
"""

import logging
from datetime import datetime

log = logging.getLogger("okuro.orchestrator.signals.db")


class SignalDismissalStore:
    """Persist dismissed signal IDs to signal_dismissals table."""

    def __init__(self):
        self._db = None
        self._available = False
        self._connect()

    def _connect(self):
        try:
            from okuro.db import get_db
            self._db = get_db()
            self._available = True
            log.info("dismissal store: connected to okuro.db")
        except Exception as exc:
            log.warning("dismissal store: DB unavailable (%s) — falling back to in-memory", exc)
            self._available = False

    def _ensure(self) -> bool:
        if not self._available:
            return False
        if self._db is None:
            self._connect()
        return self._available

    def load_dismissed(self) -> set:
        if not self._ensure():
            return set()
        try:
            rows = self._db.fetchall(
                "SELECT signal_id FROM signal_dismissals "
                "WHERE dismissed_at > datetime('now', '-30 days')"
            )
            ids = {row["signal_id"] for row in rows}
            log.info("dismissal store: loaded %d dismissed IDs (last 30 days)", len(ids))
            return ids
        except Exception as exc:
            log.warning("dismissal store: load failed (%s)", exc)
            return set()

    def persist(self, signal_id: str, reason: str = "dismissed"):
        if not self._ensure():
            return
        try:
            self._db.execute(
                "INSERT OR IGNORE INTO signal_dismissals (signal_id, dismissed_at, reason) "
                "VALUES (?, ?, ?)",
                (signal_id, datetime.utcnow().isoformat(), reason),
            )
        except Exception as exc:
            log.warning("dismissal store: persist failed for %s (%s)", signal_id, exc)

    def close(self):
        pass  # okuro.db manages connection lifecycle
