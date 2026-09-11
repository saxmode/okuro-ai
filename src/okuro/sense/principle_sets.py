# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Principle sets — named bundles of principle IDs composable by a brand.
# index:
#   imports
#   def seed_principle_sets
#   def list_principle_sets
#   def get_principle_set
#   def upsert_principle_set
#   def delete_principle_set
# AGENT_HEADER_END -->
"""Principle sets — named bundles of decision-principle IDs.

Where ``sense/principles.py`` defines the atomic DPs (DP01..DP10) and serves
them to the user profile, this module composes them into *project-level*
constraints. A brand's ``principles`` slot references one principle_set.

Storage: tables ``principle_sets`` + ``principle_set_members`` (migration 023).
Seed source: ``sense/data/principle_sets.yaml`` — idempotent, INSERT OR REPLACE.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("okuro.sense.principle_sets")

DATA_DIR = Path(__file__).parent / "data"
SEED_YAML = DATA_DIR / "principle_sets.yaml"


def seed_principle_sets() -> int:
    """Seed principle_sets + principle_set_members from YAML. Idempotent."""
    from okuro.db import get_db

    if not SEED_YAML.exists():
        return 0

    data = yaml.safe_load(SEED_YAML.read_text()) or {}
    items = data.get("principle_sets", [])
    if not items:
        return 0

    db = get_db()

    for s in items:
        set_id = s["id"]
        db.execute(
            "INSERT OR REPLACE INTO principle_sets "
            "(id, name, description, status, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (
                set_id,
                s.get("name", set_id),
                s.get("description", ""),
                s.get("status", "active"),
            ),
        )
        # Members are authoritative from YAML — wipe + re-insert.
        db.execute(
            "DELETE FROM principle_set_members WHERE set_id = ?", (set_id,)
        )
        for i, principle_id in enumerate(s.get("principles", []) or []):
            if not principle_id:
                continue
            try:
                db.execute(
                    "INSERT INTO principle_set_members "
                    "(set_id, principle_id, sort_order) VALUES (?, ?, ?)",
                    (set_id, principle_id, i),
                )
            except Exception as exc:
                logger.warning(
                    "Skipping invalid principle %s in set %s: %s",
                    principle_id, set_id, exc,
                )

    db.conn.commit()
    return len(items)


def _ensure_seeded() -> None:
    from okuro.db import get_db
    db = get_db()
    row = db.fetchone("SELECT COUNT(*) AS n FROM principle_sets")
    if not row or row["n"] == 0:
        seed_principle_sets()


def _hydrate_principle(row: dict) -> dict:
    out = dict(row)
    if isinstance(out.get("examples"), str):
        try:
            out["examples"] = json.loads(out["examples"])
        except (TypeError, ValueError):
            out["examples"] = []
    out["active"] = bool(out.get("active"))
    return out


def list_principle_sets(status: str | None = None) -> list[dict]:
    """List principle sets (with member count attached)."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    sql = "SELECT * FROM principle_sets"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY name"

    rows = db.fetchall(sql, tuple(params))
    for r in rows:
        n = db.fetchone(
            "SELECT COUNT(*) AS n FROM principle_set_members WHERE set_id = ?",
            (r["id"],),
        )
        r["member_count"] = n["n"] if n else 0
    return rows


def get_principle_set(set_id: str) -> dict | None:
    """Fetch a principle set with its members expanded to full principle rows."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    s = db.fetchone("SELECT * FROM principle_sets WHERE id = ?", (set_id,))
    if not s:
        return None

    s = dict(s)
    members = db.fetchall(
        "SELECT p.*, psm.sort_order AS _sort "
        "FROM principle_set_members psm "
        "JOIN principles p ON p.id = psm.principle_id "
        "WHERE psm.set_id = ? "
        "ORDER BY psm.sort_order, p.priority, p.id",
        (set_id,),
    )
    s["principles"] = [_hydrate_principle(m) for m in members]
    return s


def upsert_principle_set(
    set_id: str,
    *,
    name: str,
    description: str = "",
    status: str = "active",
    principles: list[str] | None = None,
) -> dict:
    """Create or update a principle set.

    ``principles=None`` leaves members untouched; passing a list (even empty)
    replaces the membership.
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    db.execute(
        "INSERT INTO principle_sets (id, name, description, status, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  name = excluded.name, "
        "  description = excluded.description, "
        "  status = excluded.status, "
        "  updated_at = datetime('now')",
        (set_id, name, description, status),
    )

    if principles is not None:
        db.execute(
            "DELETE FROM principle_set_members WHERE set_id = ?", (set_id,)
        )
        for i, pid in enumerate(principles):
            if not pid:
                continue
            try:
                db.execute(
                    "INSERT INTO principle_set_members "
                    "(set_id, principle_id, sort_order) VALUES (?, ?, ?)",
                    (set_id, pid, i),
                )
            except Exception as exc:
                logger.warning(
                    "Skipping invalid principle %s in set %s: %s", pid, set_id, exc,
                )

    db.conn.commit()
    return get_principle_set(set_id) or {}


def delete_principle_set(set_id: str) -> bool:
    """Delete a principle set. Cascades to members via FK."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    cur = db.execute("DELETE FROM principle_sets WHERE id = ?", (set_id,))
    db.conn.commit()
    # SQLite's rowcount is reliable for DELETE.
    return (cur.rowcount if cur is not None else 0) > 0
