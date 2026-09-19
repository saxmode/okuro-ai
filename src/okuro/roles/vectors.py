# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Keep vec_roles in step with the roles table — the one place a role's embedding is written.
# index:
#   imports
#   def embed_role_text
#   def write_role_vector
#   def ensure_role_vector
#   def backfill_missing_role_vectors
# AGENT_HEADER_END -->
"""The embedding side of a role row.

`roles_match` searches `vec_roles`, not `roles`, so a role row without an
embedding is invisible to every caller that matches by meaning — it exists,
`roles_get` returns it, and nothing ever routes to it. That was survivable
while the YAML catalog was a second birth path with its own embed call. It is
not survivable now: the database is a role's ONLY home, so every path that
writes a row owes it a vector, and one of them cannot pay.

"THE ONE PLACE" IS A CLAIM, so here is what makes it true. Until 2026-09-17 it
was not: `designer.store_designed_role`, `drafter.draft_role` and
`resolver.seed_role_embeddings` each ran their own DELETE/INSERT on
`vec_roles`, and none of the three set `description_embedded` — so the
staleness check below could not judge their vectors and re-embedded them on
every daemon start. All three now call `ensure_role_vector`, and every row
write goes through `roles/write.py`, which calls it too. If you are about to
add a fourth INSERT into `vec_roles`, this paragraph is the reason not to.

A SQL migration is the path that cannot pay — it has no way to reach the embed
service. `backfill_missing_role_vectors` is how it settles up: it runs at the
three seams that used to seed from the catalog (`okuro init`, onboarding
completion, daemon start) and embeds whatever has no vector yet. It is derived
data, not a second home — the text it embeds is read from `roles`, so it can
drift from the source only by being stale, never by disagreeing.

Both functions are advisory. A missing embed service degrades role MATCHING
until the next backfill; it must never take down an install, an onboarding, or
the daemon.
"""

from __future__ import annotations

import logging

log = logging.getLogger("okuro.roles.vectors")


def embed_role_text(role_id: str, description: str | None) -> bytes | None:
    """The NETWORK half: turn a description into a vector blob. Touches no DB.

    Split out of ``ensure_role_vector`` so a caller that needs the database
    writes inside a transaction can do the slow, failure-prone part FIRST and
    keep the transaction to local statements. Holding SQLite's writer lock
    across an HTTP call to the embed service is how one unreachable service
    becomes a stalled database.

    Returns None — never an exception — when there is nothing to embed or the
    service is unreachable. That None is what preserves the embed-before-delete
    guarantee: no blob means the caller writes nothing, so the role keeps the
    vector it already had instead of dropping out of the index entirely.
    """
    text = (description or "").strip()
    if not text:
        return None
    try:
        from okuro.embed import embed_one
        from okuro.embed.client import to_bytes

        return to_bytes(embed_one(text))
    except Exception as exc:  # noqa: BLE001 — matching degrades, nothing breaks
        log.warning("vec_roles embed failed for %s: %s", role_id, exc)
        return None


def write_role_vector(db, role_id: str, description: str | None, blob: bytes) -> bool:
    """The DATABASE half: replace the row's vector and record what it embeds.

    Records the exact text embedded in ``roles.description_embedded``, which is
    what lets the backfill below tell a CURRENT vector from a stale one.

    Three statements under a SAVEPOINT, not a transaction. The distinction is
    the point:

    * a TRANSACTION cannot be nested, and this runs inside the one
      ``roles/write.py`` opens around the whole write;
    * a SAVEPOINT can, and works standalone too, so the three statements are
      all-or-nothing either way.

    Without it, the DELETE landing and the INSERT failing would COMMIT the
    delete as part of the caller's transaction — the role would lose the vector
    it had, which is the exact failure the embed-before-delete ordering exists
    to prevent, reintroduced one statement later.

    Returns False rather than raising: a dead ``vec_roles`` degrades matching,
    and must not take the role's content write down with it.
    """
    db.execute("SAVEPOINT role_vector")
    try:
        db.execute("DELETE FROM vec_roles WHERE id = ?", (role_id,))
        db.execute(
            "INSERT INTO vec_roles (id, embedding) VALUES (?, ?)",
            (role_id, blob),
        )
        db.execute(
            "UPDATE roles SET description_embedded = ? WHERE role_id = ?",
            (description, role_id),
        )
    except Exception as exc:  # noqa: BLE001
        db.execute("ROLLBACK TO role_vector")
        db.execute("RELEASE role_vector")
        log.warning("vec_roles write failed for %s: %s", role_id, exc)
        return False
    db.execute("RELEASE role_vector")
    return True


def ensure_role_vector(db, role_id: str, description: str | None) -> bool:
    """Write this role's embedding into ``vec_roles``, replacing any prior one.

    Embeds BEFORE it deletes, on purpose: if the embed service is unreachable
    the old vector keeps serving matches instead of the role dropping out of
    the index entirely. That ordering is now structural rather than a rule to
    remember — the embed is a separate function whose failure returns None,
    and no DB statement runs on that path at all.

    Returns True when a vector landed. False — never an exception — when there
    is nothing to embed or the embed service is unreachable.
    """
    blob = embed_role_text(role_id, description)
    if blob is None:
        return False
    return write_role_vector(db, role_id, description, blob)


def backfill_missing_role_vectors(db=None) -> int:
    """Embed every role whose vector is missing OR no longer matches its text.

    Two conditions, and the second is the one that is easy to forget. A vector
    is built from the role's DESCRIPTION, so rewriting a description leaves a
    vector that still answers to the OLD wording — the role keeps matching the
    thing it used to be. Checking only for a MISSING vector would never notice:
    the row is there, it is just wrong. ``description_embedded`` records what
    each vector was actually built from, so "stale" is a fact in the table
    rather than something a caller has to remember to declare.

    Returns how many vectors were written. Cheap when there is nothing to do:
    one query, then no embed calls at all — which is what makes it safe to run
    on every daemon start.

    ``vec_roles`` may not exist yet on a very fresh store: the vec_* tables are
    created after the migrations by ``embed.repair.ensure_vec_dims``. That is a
    "come back next boot" condition, not a failure.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    try:
        stale = db.fetchall(
            "SELECT role_id, description FROM roles "
            "WHERE TRIM(COALESCE(description, '')) != '' "
            "  AND (role_id NOT IN (SELECT id FROM vec_roles) "
            "       OR description_embedded IS NOT description)"
        )
    except Exception as exc:  # noqa: BLE001 — vec_roles not created yet
        log.info("vec_roles backfill skipped (%s)", exc)
        return 0

    written = 0
    for row in stale:
        if ensure_role_vector(db, row["role_id"], row["description"]):
            written += 1
    if written:
        log.info(
            "vec_roles: embedded %d role(s) whose vector was missing or stale",
            written,
        )
    elif stale:
        log.warning(
            "vec_roles: %d role(s) need embedding but none could be written — "
            "role matching is running on stale or absent vectors",
            len(stale),
        )
    return written
