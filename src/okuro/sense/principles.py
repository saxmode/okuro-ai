# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Decision principles reader with auto-seed from YAML.
# index: def seed_principles | def per_response_principle_ids | def get_principles
# AGENT_HEADER_END -->
"""Decision principles reader.

Ported from tm-launcher brain/principles.py.
Principles are stored in SQLite and auto-seeded from data/principles.yaml
on first access if the table is empty.
"""

import json
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).parent / "data"


def seed_principles() -> int:
    """Seed principles table from data/principles.yaml.

    Uses INSERT OR REPLACE so it's safe to re-run (upserts).
    Returns number of principles seeded.
    """
    from okuro.db import get_db

    yaml_path = DATA_DIR / "principles.yaml"
    if not yaml_path.exists():
        return 0

    data = yaml.safe_load(yaml_path.read_text())
    items = data.get("principles", [])
    if not items:
        return 0

    db = get_db()
    for p in items:
        examples = json.dumps(p.get("examples", []))
        db.execute(
            "INSERT OR REPLACE INTO principles "
            "(id, title, description, source, priority, active, examples, "
            "per_response_rank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["id"],
                p["title"],
                p.get("description", ""),
                p.get("source", ""),
                p.get("priority", 50),
                1 if p.get("active", True) else 0,
                examples,
                p.get("per_response_rank"),
            ),
        )
    db.conn.commit()
    return len(items)


# The set the renderer used to hardcode, kept ONLY as a degraded path: if the
# per_response_rank column is missing (a store that has not run migration 119)
# the packet must still carry operative rules rather than silently rendering
# none. Order matches the pre-migration tuple so a fallback is recognisable as
# such. DP10 is deliberately absent here — it is the promotion this change
# makes, and seeing it appear proves the data path is live.
_LEGACY_PER_RESPONSE_IDS = ("DP12", "DP13", "DP11", "DP03")


def per_response_principle_ids() -> list[str]:
    """IDs of the principles that govern EVERY response, in render order.

    Sourced from ``principles.per_response_rank`` — promotion is an attribute
    of the principle, not a tuple in a renderer. See migration 119 for why:
    the same promotion had already been hand-patched once for DP03, and DP10
    was left behind in exactly the same way.
    """
    from okuro.db import get_db

    try:
        rows = get_db().fetchall(
            "SELECT id FROM principles "
            "WHERE active = 1 AND per_response_rank IS NOT NULL "
            "ORDER BY per_response_rank ASC, id ASC"
        )
        ids = [r["id"] for r in rows]
        if ids:
            return ids
    except Exception:
        pass
    return list(_LEGACY_PER_RESPONSE_IDS)


def get_principles(ids: list[str] = None, source: str = None,
                   budget: int = None, preserve_id_order: bool = False) -> str:
    """Read decision principles.

    Args:
        ids: Optional list of principle IDs to filter (e.g. ["DP01", "DP03"]).
        source: Optional source filter.
        budget: Optional token budget — return highest priority first, truncate.
        preserve_id_order: Render in the order ``ids`` was given rather than by
            stored priority. The per-response block needs this: its order is
            carried by ``per_response_rank``, and priority ASC would sort DP10
            (priority 2) below the epistemic trio (priority 1) — putting the
            promoted rule back near the bottom, which is the defect being
            fixed. Under a budget, the tail is dropped first, so the caller's
            order also decides what survives truncation.
    """
    from okuro.db import get_db

    db = get_db()

    sql = "SELECT id, title, description, source, priority, examples FROM principles WHERE active = 1"
    params: list = []

    if ids:
        placeholders = ", ".join("?" * len(ids))
        sql += f" AND id IN ({placeholders})"
        params.extend(ids)

    if source:
        sql += " AND source = ?"
        params.append(source)

    sql += " ORDER BY priority ASC"
    rows = db.fetchall(sql, tuple(params))

    # Lazy seed: if table is empty and no filters applied, try seeding
    if not rows and not ids and not source:
        count = seed_principles()
        if count:
            rows = db.fetchall(sql, tuple(params))

    if not rows:
        return "No principles found."

    if preserve_id_order and ids:
        rank = {pid: i for i, pid in enumerate(ids)}
        rows = sorted(rows, key=lambda r: rank.get(r["id"], len(rank)))

    principles = []
    total_tokens = 0

    for r in rows:
        examples = json.loads(r["examples"]) if isinstance(r.get("examples"), str) else (r.get("examples") or [])
        block = f"**{r['id']}: {r['title']}** — {r['description']}"
        if examples:
            block += "\n" + "\n".join(f"  - {ex}" for ex in examples)

        block_tokens = int(len(block.split()) * 1.3)
        if budget and (total_tokens + block_tokens) > budget:
            break

        total_tokens += block_tokens
        principles.append(block)

    return "\n".join(principles)
