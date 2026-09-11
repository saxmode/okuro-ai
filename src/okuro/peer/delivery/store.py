# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.store — deliveries table CRUD + vec embedding.
# index: imports | def _embed_text | def delivery_write | def delivery_get |
#   def delivery_list | def delivery_delete
# AGENT_HEADER_END -->
"""Deliveries table CRUD + vector indexing.

Schema lives in migration ``036_deliveries.sql``. Mirrors translation_log
shape (typed cols + duration_ms + provider/model + success/error) plus
the channel-typed payload (body / body_blob / body_path).

Hard rule HR-C4: body_blob for binaries (PDF, MP3), body for text
(markdown), body_path only when the payload exceeds 1 MB (microsite
tarballs, podcast audio). Microsoft formats (PPTX/DOCX/XLSX) are
banned — see convention memory 50b49e1c.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)


_ALLOWED_CHANNELS = {
    "markdown", "marp", "microsite", "tts", "podcast",
}
_EMBED_PREFIX_CHARS = 500


def _embed_text(title: str | None, body: str | None) -> str:
    """Compose embed text. Same intuition as artifacts: title + body intro."""
    parts: list[str] = []
    if title and title.strip():
        parts.append(title.strip())
    if body:
        snippet = body[:_EMBED_PREFIX_CHARS].strip()
        if snippet:
            parts.append(snippet)
    return "\n\n".join(parts)


def _vec_bytes(text: str) -> bytes | None:
    if not text:
        return None
    try:
        from okuro.embed.client import embed_one, to_bytes
        return to_bytes(embed_one(text))
    except Exception:
        return None


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in ("outline", "theme"):
        v = out.get(col)
        if isinstance(v, str):
            try:
                out[col] = json.loads(v) if v else {}
            except (TypeError, ValueError):
                out[col] = {}
    out["success"] = bool(out.get("success", 1))
    return out


def delivery_write(
    *,
    artifact_id: str,
    channel: str,
    person_id: str | None = None,
    brand_id: str | None = None,
    title: str | None = None,
    outline: dict | None = None,
    theme: dict | None = None,
    body: str | None = None,
    body_blob: bytes | None = None,
    body_path: str | None = None,
    media_type: str | None = None,
    duration_ms: int | None = None,
    cost_usd: float | None = None,
    provider: str | None = None,
    model: str | None = None,
    success: bool = True,
    error: str | None = None,
    created_by: str | None = None,
    delivery_id: str | None = None,
) -> str:
    """Insert a delivery row. Returns the id, or REJECTED:... on guard miss."""
    if not artifact_id or not artifact_id.strip():
        return "REJECTED: artifact_id is required."
    if channel not in _ALLOWED_CHANNELS:
        return (
            f"REJECTED: channel={channel!r} is not one of "
            f"{sorted(_ALLOWED_CHANNELS)}."
        )

    from okuro.db import get_db

    db = get_db()
    did = delivery_id or str(uuid.uuid4())
    outline_json = json.dumps(outline or {})
    theme_json = json.dumps(theme or {})

    embed_input = _embed_text(title, body)
    vec = _vec_bytes(embed_input) if embed_input else None

    with db.write():
        db.execute(
            """INSERT INTO deliveries (
                    id, artifact_id, person_id, channel, brand_id, title,
                    outline, theme, body, body_blob, body_path, media_type,
                    duration_ms, cost_usd, provider, model, success, error,
                    created_by
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                did, artifact_id, person_id, channel, brand_id, title,
                outline_json, theme_json, body, body_blob, body_path,
                media_type, duration_ms, cost_usd, provider, model,
                1 if success else 0, error, created_by,
            ),
        )
        if vec is not None:
            try:
                db.execute(
                    "INSERT INTO vec_deliveries (id, embedding) VALUES (?, ?)",
                    (did, vec),
                )
            except Exception as exc:
                log.debug("vec_deliveries insert failed: %s", exc)

    return did


def delivery_get(
    delivery_id: str,
    *,
    include_body: bool = True,
    include_blob: bool = False,
) -> dict | None:
    """Fetch a single delivery. Body returned only when include_body=True
    so list-like callers stay cheap. body_blob is opt-in (large binaries).
    """
    from okuro.db import get_db

    db = get_db()
    cols = (
        "id, artifact_id, person_id, channel, brand_id, title, outline, "
        "theme, body_path, media_type, duration_ms, cost_usd, provider, "
        "model, success, error, created_by, created_at"
    )
    if include_body:
        cols += ", body"
    if include_blob:
        cols += ", body_blob"
    row = db.fetchone(f"SELECT {cols} FROM deliveries WHERE id = ?", (delivery_id,))
    return _row_to_dict(row)


def delivery_list(
    *,
    artifact_id: str | None = None,
    person_id: str | None = None,
    channel: str | None = None,
    brand_id: str | None = None,
    success: bool | None = None,
    limit: int = 50,
    order: str = "created_at_desc",
) -> list[dict]:
    """List deliveries with filters. Body-free by default (cheap)."""
    from okuro.db import get_db

    db = get_db()
    sql = (
        "SELECT id, artifact_id, person_id, channel, brand_id, title, "
        "outline, theme, body_path, media_type, duration_ms, cost_usd, "
        "provider, model, success, error, created_by, created_at "
        "FROM deliveries WHERE 1 = 1"
    )
    params: list[Any] = []
    if artifact_id:
        sql += " AND artifact_id = ?"
        params.append(artifact_id)
    if person_id:
        sql += " AND person_id = ?"
        params.append(person_id)
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    if brand_id:
        sql += " AND brand_id = ?"
        params.append(brand_id)
    if success is not None:
        sql += " AND success = ?"
        params.append(1 if success else 0)

    order_clause = {
        "created_at_desc": " ORDER BY created_at DESC",
        "created_at_asc":  " ORDER BY created_at ASC",
    }.get(order, " ORDER BY created_at DESC")
    sql += order_clause + " LIMIT ?"
    params.append(int(limit))

    rows = db.fetchall(sql, tuple(params))
    return [r for r in (_row_to_dict(row) for row in rows) if r is not None]


def delivery_delete(delivery_id: str) -> str:
    """Hard-delete a delivery (and vec row). Returns confirmation string."""
    from okuro.db import get_db

    db = get_db()
    with db.write():
        existing = db.fetchone(
            "SELECT id FROM deliveries WHERE id = ?", (delivery_id,)
        )
        if not existing:
            return f"REJECTED: delivery {delivery_id!r} does not exist."
        db.execute("DELETE FROM deliveries WHERE id = ?", (delivery_id,))
        try:
            db.execute("DELETE FROM vec_deliveries WHERE id = ?", (delivery_id,))
        except Exception:
            pass
    return f"Deleted: {delivery_id}"
