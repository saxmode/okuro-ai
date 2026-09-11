# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: VRAM broker — atomic SQLite leases + single-authority admission.
# index:
#   imports
#   constants
#   class BrokerRejection
#   class Lease
#   class Broker
#     __init__ / _connect / _now / _gpu_total
#     request / _admissible / _evict_for
#     heartbeat / release / reap / active_leases / free_report
# AGENT_HEADER_END -->
"""VRAM broker — the single authority that decides whether a model may occupy
a GPU, and reaps dead holders.

Design (from the engine+broker research):
- **Atomic** — every admission runs inside one SQLite ``BEGIN IMMEDIATE``
  transaction so concurrent requests can't both win the same VRAM. Replaces
  the predecessor arbiter's non-atomic check-then-write JSON lock.
- **Single authority** — admissible VRAM = ``min(ledger_free, nvidia-smi free)
  − headroom``. The ledger tracks okuro's own reservations; nvidia-smi catches
  VRAM claimed by non-okuro users (ComfyUI, raw tm-inference, Ollama) so the
  broker never double-books.
- **Tiers** — PINNED (LLM, never evicted) · WARM (embeddings, TTL) · BURST
  (media, evict-first LRU). When a request doesn't fit, BURST then WARM leases
  on the target GPU are evicted (oldest heartbeat first); PINNED is sacred.
- **Heartbeat-TTL leases** — a holder must heartbeat; ``reap`` expires leases
  whose heartbeat is older than their TTL (replaces caller-pid + fixed grace).
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from okuro.db.engine import okuro_home

PINNED, WARM, BURST = "pinned", "warm", "burst"
_EVICT_ORDER = {BURST: 0, WARM: 1, PINNED: 2}  # lower = evicted first
_DEFAULT_HEADROOM_GB = 2.0
_DEFAULT_TTL_S = 300.0


class BrokerRejection(Exception):
    """A lease could not be admitted even after evicting evictable tenants."""


@dataclass
class Lease:
    lease_id: str
    gpu_index: int
    model_id: str
    tier: str
    vram_gb: float
    state: str
    created_at: float
    heartbeat_at: float
    ttl_s: float
    caller: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    lease_id     TEXT PRIMARY KEY,
    gpu_index    INTEGER NOT NULL,
    model_id     TEXT NOT NULL,
    tier         TEXT NOT NULL,
    vram_gb      REAL NOT NULL,
    state        TEXT NOT NULL,
    created_at   REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    ttl_s        REAL NOT NULL,
    caller       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_leases_active ON leases(gpu_index, state);
"""


def _default_db_path() -> Path:
    return Path(os.environ.get("OKURO_INFERENCE_DB", str(okuro_home() / "inference.db")))


class Broker:
    def __init__(
        self,
        db_path: str | os.PathLike | None = None,
        *,
        gpu_totals: Optional[dict[int, float]] = None,
        free_probe: Optional[Callable[[], dict[int, float]]] = None,
        headroom_gb: float = _DEFAULT_HEADROOM_GB,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.headroom_gb = headroom_gb
        self._clock = clock or time.time
        self._gpu_totals = gpu_totals
        self._free_probe = free_probe
        with self._connect() as con:
            con.executescript(_SCHEMA)

    # ---- infra ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None → explicit BEGIN IMMEDIATE control.
        con = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _now(self) -> float:
        return self._clock()

    def _gpu_total(self, gpu_index: int) -> float:
        if self._gpu_totals is not None:
            return self._gpu_totals.get(gpu_index, 0.0)
        from okuro.capability import capabilities

        for g in capabilities().get("gpus", []):
            if g["index"] == gpu_index:
                return float(g["vram_gb"])
        return 0.0

    def _smi_free(self, gpu_index: int) -> Optional[float]:
        probe = self._free_probe
        if probe is None:
            from okuro.capability import gpu_memory_free

            probe = gpu_memory_free
        free = probe()
        return free.get(gpu_index) if free else None

    # ---- admission -----------------------------------------------------
    def _ledger_used(self, con: sqlite3.Connection, gpu_index: int) -> float:
        row = con.execute(
            "SELECT COALESCE(SUM(vram_gb), 0) AS used FROM leases "
            "WHERE gpu_index=? AND state='active'",
            (gpu_index,),
        ).fetchone()
        return float(row["used"])

    def _admissible(self, con: sqlite3.Connection, gpu_index: int, *, smi_bonus: float = 0.0) -> float:
        total = self._gpu_total(gpu_index)
        ledger_free = total - self._ledger_used(con, gpu_index)
        smi_free = self._smi_free(gpu_index)
        if smi_free is None:
            effective = ledger_free  # no nvidia-smi → trust the ledger alone
        else:
            effective = min(ledger_free, smi_free + smi_bonus)
        return effective - self.headroom_gb

    def request(
        self,
        model_id: str,
        vram_gb: float,
        gpu_index: int,
        *,
        tier: str = BURST,
        caller: str = "",
        ttl_s: float = _DEFAULT_TTL_S,
    ) -> Lease:
        """Admit a model onto ``gpu_index`` or raise BrokerRejection.

        Evicts evictable tenants (BURST then WARM, oldest-heartbeat first) when
        the request doesn't fit; PINNED is never evicted.
        """
        if tier not in _EVICT_ORDER:
            raise ValueError(f"unknown tier: {tier}")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if vram_gb <= self._admissible(con, gpu_index):
                return self._grant(con, model_id, vram_gb, gpu_index, tier, caller, ttl_s)

            # Try to make room: evict evictable leases, oldest heartbeat first.
            freed = self._evict_for(con, gpu_index, vram_gb)
            if vram_gb <= self._admissible(con, gpu_index, smi_bonus=freed):
                return self._grant(con, model_id, vram_gb, gpu_index, tier, caller, ttl_s)

            con.execute("ROLLBACK")
            raise BrokerRejection(
                f"{model_id}: needs {vram_gb:.1f} GB on gpu{gpu_index}; "
                f"admissible {self._admissible_readonly(gpu_index):.1f} GB "
                f"after evicting evictable tenants."
            )
        except BrokerRejection:
            raise
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    def _grant(self, con, model_id, vram_gb, gpu_index, tier, caller, ttl_s) -> Lease:
        now = self._now()
        lease = Lease(
            lease_id=uuid.uuid4().hex[:12],
            gpu_index=gpu_index,
            model_id=model_id,
            tier=tier,
            vram_gb=float(vram_gb),
            state="active",
            created_at=now,
            heartbeat_at=now,
            ttl_s=float(ttl_s),
            caller=caller,
        )
        con.execute(
            "INSERT INTO leases VALUES (?,?,?,?,?,?,?,?,?,?)",
            (lease.lease_id, lease.gpu_index, lease.model_id, lease.tier,
             lease.vram_gb, lease.state, lease.created_at, lease.heartbeat_at,
             lease.ttl_s, lease.caller),
        )
        con.execute("COMMIT")
        return lease

    def _evict_for(self, con: sqlite3.Connection, gpu_index: int, need_gb: float) -> float:
        """Evict evictable leases (BURST then WARM, oldest heartbeat first) until
        the freed VRAM would satisfy ``need_gb``. Returns freed GB. PINNED is
        never touched."""
        rows = con.execute(
            "SELECT lease_id, tier, vram_gb FROM leases "
            "WHERE gpu_index=? AND state='active' AND tier IN ('burst','warm') "
            "ORDER BY CASE tier WHEN 'burst' THEN 0 ELSE 1 END, heartbeat_at ASC",
            (gpu_index,),
        ).fetchall()
        freed = 0.0
        for r in rows:
            if freed >= need_gb:
                break
            con.execute(
                "UPDATE leases SET state='killed' WHERE lease_id=?", (r["lease_id"],)
            )
            freed += float(r["vram_gb"])
        return freed

    def _admissible_readonly(self, gpu_index: int) -> float:
        con = self._connect()
        try:
            return self._admissible(con, gpu_index)
        finally:
            con.close()

    # ---- lifecycle -----------------------------------------------------
    def heartbeat(self, lease_id: str) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "UPDATE leases SET heartbeat_at=? WHERE lease_id=? AND state='active'",
                (self._now(), lease_id),
            )
            return cur.rowcount > 0

    def release(self, lease_id: str) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "UPDATE leases SET state='released' WHERE lease_id=? AND state='active'",
                (lease_id,),
            )
            return cur.rowcount > 0

    def reap(self) -> int:
        """Expire active leases whose heartbeat is older than their TTL."""
        now = self._now()
        with self._connect() as con:
            cur = con.execute(
                "UPDATE leases SET state='expired' "
                "WHERE state='active' AND (? - heartbeat_at) > ttl_s",
                (now,),
            )
            return cur.rowcount

    def active_leases(self, gpu_index: Optional[int] = None) -> list[Lease]:
        q = "SELECT * FROM leases WHERE state='active'"
        args: tuple = ()
        if gpu_index is not None:
            q += " AND gpu_index=?"
            args = (gpu_index,)
        q += " ORDER BY created_at"
        with self._connect() as con:
            return [_row_to_lease(r) for r in con.execute(q, args).fetchall()]

    def free_report(self, gpu_index: int) -> dict:
        """Admission snapshot for a GPU (for the Models page / diagnostics)."""
        con = self._connect()
        try:
            return {
                "gpu_index": gpu_index,
                "total_gb": self._gpu_total(gpu_index),
                "ledger_used_gb": round(self._ledger_used(con, gpu_index), 2),
                "smi_free_gb": self._smi_free(gpu_index),
                "admissible_gb": round(self._admissible(con, gpu_index), 2),
                "headroom_gb": self.headroom_gb,
            }
        finally:
            con.close()

    def tenants_report(self, gpu_index: Optional[int] = None, *, processes=None) -> dict:
        """Who holds each GPU: okuro's OWN named leases (from the ledger) plus
        EXTERNAL named processes (tm-inference / the legacy arbiter / comfyui / ollama)
        from nvidia-smi. Turns the broker from 'measures VRAM' into 'knows what
        is running'. ``processes`` is injectable for tests."""
        if processes is None:
            from okuro.capability import gpu_processes

            processes = gpu_processes()

        leases = self.active_leases(gpu_index)
        # Enumerate every detected GPU (incl. idle ones) so the report reads
        # like a full board, not only the busy cards.
        base: set[int] = set(self._gpu_totals or {})
        probe = self._free_probe
        if probe is None:
            try:
                from okuro.capability import gpu_memory_free as probe
            except Exception:  # pragma: no cover
                probe = None
        if probe is not None:
            try:
                base |= set(probe() or {})
            except Exception:  # pragma: no cover
                pass
        indices = set(processes) | {ln.gpu_index for ln in leases} | base
        if gpu_index is not None:
            indices = {gpu_index}

        report: dict[int, dict] = {}
        for idx in sorted(indices):
            procs = processes.get(idx, [])
            report[idx] = {
                "gpu_index": idx,
                "total_gb": self._gpu_total(idx),
                "smi_free_gb": self._smi_free(idx),
                "okuro_leases": [
                    {"model_id": ln.model_id, "tier": ln.tier, "vram_gb": ln.vram_gb}
                    for ln in leases if ln.gpu_index == idx
                ],
                "external": [
                    p for p in procs if p.get("tenant") != "okuro-engine"
                ],
            }
        return report


def _row_to_lease(r: sqlite3.Row) -> Lease:
    return Lease(
        lease_id=r["lease_id"], gpu_index=r["gpu_index"], model_id=r["model_id"],
        tier=r["tier"], vram_gb=r["vram_gb"], state=r["state"],
        created_at=r["created_at"], heartbeat_at=r["heartbeat_at"],
        ttl_s=r["ttl_s"], caller=r["caller"],
    )
