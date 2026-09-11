# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: SQL helpers for the integrations table — read config, persist
#   adapter state (status / last_seen / last_error) without leaking SQL
#   into adapters.
# index: imports | IntegrationRow | get_integration | upsert_config |
#   list_enabled | mark_status | mark_seen | mark_error
# AGENT_HEADER_END -->
"""DB helpers for the ``integrations`` table.

Adapters only see a typed row; they do not write SQL.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("okuro.ingress.storage")


@dataclass
class IntegrationRow:
    channel: str
    enabled: bool
    config: dict = field(default_factory=dict)
    status: str = "stopped"
    last_seen_at: Optional[str] = None
    last_message_at: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None


def _row_to_dc(row: dict) -> IntegrationRow:
    cfg_raw = row.get("config_json") or "{}"
    try:
        cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else dict(cfg_raw)
    except (TypeError, ValueError):
        log.warning("integrations.config_json invalid for %s; using {}", row.get("channel"))
        cfg = {}
    return IntegrationRow(
        channel=row["channel"],
        enabled=bool(row.get("enabled", 0)),
        config=cfg,
        status=row.get("status") or "stopped",
        last_seen_at=row.get("last_seen_at"),
        last_message_at=row.get("last_message_at"),
        last_error=row.get("last_error"),
        last_error_at=row.get("last_error_at"),
    )


def get_integration(channel: str) -> Optional[IntegrationRow]:
    from okuro.db import get_db
    db = get_db()
    row = db.fetchone(
        "SELECT * FROM integrations WHERE channel = ?",
        (channel,),
    )
    return _row_to_dc(row) if row else None


def list_enabled() -> list[IntegrationRow]:
    from okuro.db import get_db
    db = get_db()
    rows = db.fetchall("SELECT * FROM integrations WHERE enabled = 1") or []
    return [_row_to_dc(r) for r in rows]


def upsert_config(channel: str, *, enabled: Optional[bool] = None,
                  config: Optional[dict] = None) -> None:
    """Create the row if missing; patch enabled/config_json otherwise."""
    from okuro.db import get_db
    db = get_db()
    existing = get_integration(channel)
    if existing is None:
        db.execute(
            "INSERT INTO integrations (channel, enabled, config_json) "
            "VALUES (?, ?, ?)",
            (
                channel,
                1 if (enabled or False) else 0,
                json.dumps(config or {}),
            ),
        )
        return
    new_enabled = existing.enabled if enabled is None else enabled
    new_config = existing.config if config is None else config
    db.execute(
        "UPDATE integrations SET enabled = ?, config_json = ?, "
        "updated_at = datetime('now') WHERE channel = ?",
        (1 if new_enabled else 0, json.dumps(new_config), channel),
    )


def patch_config(channel: str, patch: dict) -> None:
    """Shallow-merge ``patch`` into config_json. Used by adapters to
    persist e.g. the last-seen poll offset without clobbering other keys."""
    existing = get_integration(channel)
    if existing is None:
        upsert_config(channel, enabled=False, config=dict(patch))
        return
    merged = dict(existing.config)
    merged.update(patch)
    upsert_config(channel, enabled=existing.enabled, config=merged)


def mark_status(channel: str, status: str) -> None:
    from okuro.db import get_db
    db = get_db()
    db.execute(
        "UPDATE integrations SET status = ?, updated_at = datetime('now') "
        "WHERE channel = ?",
        (status, channel),
    )


def mark_seen(channel: str, *, message: bool = False) -> None:
    """Stamp last_seen_at on every successful poll; also stamp
    last_message_at when an actual message was received."""
    from okuro.db import get_db
    db = get_db()
    if message:
        db.execute(
            "UPDATE integrations SET last_seen_at = datetime('now'), "
            "last_message_at = datetime('now'), updated_at = datetime('now') "
            "WHERE channel = ?",
            (channel,),
        )
    else:
        db.execute(
            "UPDATE integrations SET last_seen_at = datetime('now'), "
            "updated_at = datetime('now') WHERE channel = ?",
            (channel,),
        )


def mark_error(channel: str, error: str) -> None:
    from okuro.db import get_db
    db = get_db()
    db.execute(
        "UPDATE integrations SET status = 'error', last_error = ?, "
        "last_error_at = datetime('now'), updated_at = datetime('now') "
        "WHERE channel = ?",
        (error[:500], channel),
    )


def clear_error(channel: str) -> None:
    from okuro.db import get_db
    db = get_db()
    db.execute(
        "UPDATE integrations SET last_error = NULL, last_error_at = NULL, "
        "updated_at = datetime('now') WHERE channel = ?",
        (channel,),
    )
