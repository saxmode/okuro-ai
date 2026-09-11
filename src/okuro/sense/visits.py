# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: surface_visits — when the user last LOOKED at a screen, so a surface
#   can render a diff instead of restating its whole state.
# index:
#   imports
#   def get_visit
#   def mark_visit
# AGENT_HEADER_END -->
"""Surface visits — the "since you last looked" reference point.

THE ONE SUBTLETY, AND IT IS THE WHOLE DESIGN. A naive implementation stores a
single `last_seen_at`, updates it when the screen opens, and compares against
it — which erases the delta in the act of looking at it. Open the board, the
timestamp becomes now, refresh, and "what changed since last time" is empty
forever.

So a visit SHIFTS rather than overwrites: the old `last_seen_at` moves to
`previous_seen_at`, and the diff reads `previous_seen_at`. That reference stays
put for the whole visit no matter how many times the page re-renders, and the
next visit compares against when THIS one started.

Mirrors the append-only instinct behind inbox_impressions without the volume:
the board needs one stable reference, not an audit trail of every render.
"""

from __future__ import annotations

from typing import Any

#: The surface id used by the RESUME BOARD. Named here so the route, the page
#: and any test agree on one spelling — a typo would silently create a second,
#: permanently-empty surface rather than fail.
PROJECTS_SURFACE = "projects"


def get_visit(surface: str) -> dict[str, Any]:
    """Read a surface's visit record WITHOUT recording a visit.

    Separate from `mark_visit` on purpose: a poll, a test or a second component
    on the same page must be able to ask "what is the reference point" without
    moving it.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT surface, last_seen_at, previous_seen_at, updated_at "
        "FROM surface_visits WHERE surface = ?",
        (surface,),
    )
    if row is None:
        # Never visited. `since` is None rather than epoch-zero: "no reference
        # point" and "nothing changed since 1970" must not render the same, and
        # a first-time board should say so instead of listing every project as
        # new.
        return {
            "surface": surface,
            "last_seen_at": None,
            "previous_seen_at": None,
            "first_visit": True,
        }
    return {
        "surface": row["surface"],
        "last_seen_at": row["last_seen_at"],
        "previous_seen_at": row["previous_seen_at"],
        "first_visit": False,
    }


def mark_visit(surface: str) -> dict[str, Any]:
    """Record that the user opened `surface` now, shifting the reference point.

    Returns the record as it stands AFTER the shift, so the caller gets the
    `previous_seen_at` it should diff against in the same round-trip it used to
    announce the visit — the alternative is a read-then-write pair that races
    with itself on a double mount.
    """
    from okuro.db import get_db

    db = get_db()
    # ONE statement, so it autocommits under isolation_level=None — no db.write()
    # wrapper, which exists for multi-statement DML that must be atomic. The
    # shift is expressed IN the UPSERT rather than as read-modify-write so two
    # near-simultaneous mounts cannot both read the same old value and each
    # write it forward, collapsing the reference point.
    db.execute(
        "INSERT INTO surface_visits (surface, last_seen_at, previous_seen_at, updated_at) "
        "VALUES (?, datetime('now'), NULL, datetime('now')) "
        "ON CONFLICT(surface) DO UPDATE SET "
        "  previous_seen_at = surface_visits.last_seen_at, "
        "  last_seen_at = datetime('now'), "
        "  updated_at = datetime('now')",
        (surface,),
    )
    return get_visit(surface)
