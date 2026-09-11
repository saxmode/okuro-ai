# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Durable running-engine registry — model_id → live endpoint, shared
#          across processes via the broker's SQLite db.
# index:
#   imports / constants / schema
#   def _pid_alive
#   class EngineRegistry
#     __init__ / _connect / register / unregister / resolve / list_running / reap_dead
# AGENT_HEADER_END -->
"""Durable running-engine registry.

:class:`~okuro.inference.engine.EngineRunner` keeps its live engines in an
in-process dict — invisible to the bridge, the MCP daemon, or a CLI in another
process. This registry persists ``model_id → endpoint`` in the *same* SQLite
file the broker uses (``OKURO_INFERENCE_DB``), so any process can resolve which
port currently serves a model. It is the cross-process half of the runner: the
runner writes a row on start and deletes it on stop; the bridge ``local``
provider reads it at invoke time to target a dynamically-launched engine
instead of the static config endpoint.

Rows are keyed by ``model_id`` (one engine per model, matching the runner's
one-per-bundle rule) and are self-healing: a resolve/reap drops any row whose
owning process is dead, so a crash never leaves the bridge pointing at a port
nothing is listening on.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional
from okuro.db.engine import okuro_home

_SCHEMA = """
CREATE TABLE IF NOT EXISTS engines (
    model_id   TEXT PRIMARY KEY,
    endpoint   TEXT NOT NULL,
    gpu_index  INTEGER NOT NULL,
    pid        INTEGER NOT NULL,
    lease_id   TEXT NOT NULL DEFAULT '',
    tier       TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL
);
"""


def _default_db_path() -> Path:
    return Path(os.environ.get("OKURO_INFERENCE_DB", str(okuro_home() / "inference.db")))


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists (signal 0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


class EngineRegistry:
    def __init__(
        self,
        db_path: str | os.PathLike | None = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        pid_alive: Optional[Callable[[int], bool]] = None,
    ):
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        self._pid_alive = pid_alive or _pid_alive
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def register(
        self,
        model_id: str,
        endpoint: str,
        gpu_index: int,
        pid: int,
        *,
        lease_id: str = "",
        tier: str = "",
    ) -> None:
        """Record (or replace) the live engine serving ``model_id``."""
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO engines "
                "(model_id, endpoint, gpu_index, pid, lease_id, tier, started_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (model_id, endpoint, int(gpu_index), int(pid), lease_id, tier, self._clock()),
            )

    def unregister(self, model_id: str) -> bool:
        with self._connect() as con:
            cur = con.execute("DELETE FROM engines WHERE model_id=?", (model_id,))
            return cur.rowcount > 0

    def resolve(self, model_id: str) -> Optional[str]:
        """Endpoint currently serving ``model_id``, or None.

        Self-heals: if the owning process is dead the stale row is dropped and
        None is returned, so the bridge never targets a dead engine.
        """
        with self._connect() as con:
            row = con.execute(
                "SELECT endpoint, pid FROM engines WHERE model_id=?", (model_id,)
            ).fetchone()
            if row is None:
                return None
            if not self._pid_alive(int(row["pid"])):
                con.execute("DELETE FROM engines WHERE model_id=?", (model_id,))
                return None
            return row["endpoint"]

    def list_running(self) -> list[dict]:
        with self._connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM engines ORDER BY started_at"
            ).fetchall()]

    def reap_dead(self) -> int:
        """Drop every row whose owning process no longer exists. Returns count."""
        with self._connect() as con:
            rows = con.execute("SELECT model_id, pid FROM engines").fetchall()
            dead = [r["model_id"] for r in rows if not self._pid_alive(int(r["pid"]))]
            for model_id in dead:
                con.execute("DELETE FROM engines WHERE model_id=?", (model_id,))
            return len(dead)
