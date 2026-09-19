# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The one write path into the `roles` table — validate, upsert, tag, embed, stamp.
# index:
#   imports
#   WRITABLE_FIELDS
#   class RoleWriteRefused
#   def upsert_role
#   def update_role_fields
#   def delete_role
#   def promote_role
# AGENT_HEADER_END -->
"""Every guarantee a role row owes, installed once instead of per endpoint.

A role row is not just its columns. It owes four things that nothing in the
schema enforces:

* a **canonical tier and live tool names** — a role outside the vocabulary
  hands its adopting agent a confident method built on a tool that hard-errors;
* **tags** — the M4 slice-picker reads ``roles.tags``, and a tagless role is
  invisible to it;
* **a vector** — ``roles_match`` searches ``vec_roles``, never ``roles``, so a
  row without an embedding exists, answers ``roles_get``, and is never routed
  to by anything that matches on meaning;
* **a truthful ``updated_at``** — staleness, maintenance scheduling and the
  freshness badge in the web UI all read it.

Before this module there were six writers and each one paid a different subset.
Measured on main, 2026-09-17:

===========================  ========  ====  ======  ==========
writer                       validate  tags  vector  updated_at
===========================  ========  ====  ======  ==========
``POST /api/roles``          yes       no    yes     no
``PUT /api/roles/{id}``      no        no    no      no
``DELETE /api/roles/{id}``   —         —     ORPHAN  —
``promote``                  no        no    if none no
``store_designed_role``      no        no    raw     no
``draft_role``               no        no    raw     no
===========================  ========  ====  ======  ==========

That is the CLASS, and it is not "the PUT endpoint forgot to re-embed". Six
sites each installing their own subset of one contract is a contract with no
home; the seventh writer will pick its own subset too. So the contract moves
here and the writers become callers (DP10/DP11).

ORDER IS PART OF THE CONTRACT, not an implementation detail:

1. ``validate_role_row`` — fatal. A refused row must not reach the table at
   all; validating after the write means the bad row is already live.
2. the upsert — content columns only. ``maturity`` is set on INSERT and left
   alone on conflict, because the runtime promotes roles and a content write
   must never silently demote one (see ``roles/columns.py``).
3. ``derive_tags_for_role`` — reads the row as written, so tags always describe
   the body that is actually stored.
4. ``ensure_role_vector`` — which embeds BEFORE it deletes. That ordering is
   why the embed step comes after the row and not inside a rollback: with the
   embed service down, the role keeps its previous vector and keeps matching on
   its previous wording, instead of dropping out of the index entirely. The
   result is REPORTED (``embedded``), never swallowed — a caller told "created"
   while the role is unmatchable has been told the opposite of what is true.
5. ``updated_at`` — last, so it means "this row and everything derived from it
   are settled".

``actor`` is required on every write and carried into the log line. The table
has no audit column, so the log is the only record of which surface wrote a
row; making the argument mandatory — with NO default on any of the four
functions — is what keeps a future caller from being anonymous. A default of
``"unknown"`` would have documented a requirement while quietly permitting its
opposite.

WHAT ``embedded`` MEANS. It is a string, not a bool, because "False" was four
different facts and a caller could not tell a dead embed service from a role
that simply did not need re-embedding:

======================  ===========================================================
``"written"``           a fresh vector landed in ``vec_roles``
``"unchanged"``         the stored vector already matches the text; nothing to do
``"failed"``            there was text to embed and it could not be embedded or
                        stored — the role keeps its PREVIOUS vector and matching
                        runs on stale wording until the next backfill
``"skipped"``           the role has no description; it can never be matched, and
                        that is a content defect, not a service failure
======================  ===========================================================

``tags`` is ``None`` when they were not recomputed, which is not the same fact
as ``[]`` — an empty list means the derivation ran and produced nothing.

TRANSACTION BOUNDARY. Each function does its network call FIRST and opens
``db.transaction()` only around local statements. Holding SQLite's writer lock
across an HTTP request to the embed service is how one unreachable service
becomes a stalled database. It also keeps embed-before-delete structural
rather than remembered: ``embed_role_text`` returning None means no vector
statement runs at all, so the role keeps the vector it had.

Inside that transaction the vector write takes its own SAVEPOINT (see
``vectors.write_role_vector``). A dead ``vec_roles`` must degrade matching, not
roll back the role's content — but a DELETE that commits while its INSERT
failed would be worse than either, so the three vector statements are
all-or-nothing within the larger write.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from okuro.roles._time import now_iso

log = logging.getLogger("okuro.roles.write")

__all__ = [
    "RoleExists",
    "RolePromotionRefused",
    "RoleWriteRefused",
    "WRITABLE_FIELDS",
    "insert_role",
    "upsert_role",
    "update_role_fields",
    "delete_role",
    "promote_role",
]


#: Columns a caller may name in ``update_role_fields``. This is the content
#: half of ``roles/columns.py``: everything the runtime OWNS (tags, vector
#: bookkeeping, session counters) is derived here and must not be settable by
#: hand, or the derivation and the hand-written value disagree with no way to
#: tell which is current. ``maturity`` is absent on purpose — it moves through
#: ``promote_role``, which is the only transition with post-steps.
WRITABLE_FIELDS = frozenset(
    {
        "domain",
        "description",
        "tier",
        "model",
        "tools",
        "prompt",
        "lean_prompt",
        "micro_prompt",
        "panel_eligible",
        "maintenance_schedule",
        "person_preset",
    }
)

#: A change to any of these makes the stored tags wrong: ``derive_tags_for_role``
#: builds them from exactly these three inputs plus the role id.
_TAG_INPUTS = ("description", "domain", "tier")


class RoleWriteRefused(ValueError):
    """The vocabulary gate refused this row. Carries one message per violation.

    Raised rather than returned: a caller that can ignore the refusal is a
    caller that will, and the whole point of the gate is that a role outside
    the vocabulary never reaches the table.
    """

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("role vocabulary violations: " + "; ".join(self.problems))


class RoleExists(ValueError):
    """``insert_role`` was asked to create a role_id that is already taken.

    Its own type, not a ``RoleWriteRefused``: a vocabulary violation is a bad
    VALUE the caller can correct, while this is a collision with a row that
    already belongs to something else. Callers that map exceptions to status
    codes need to tell 422 from 409, and a shared base would flatten them.
    """

    def __init__(self, role_id: str):
        self.role_id = role_id
        super().__init__(f"role {role_id!r} already exists")


class RolePromotionRefused(ValueError):
    """The role is not structurally complete enough to go active.

    Promotion is the moment a role becomes reachable by ``roles_match``, so
    it is the last boundary at which an incomplete body can be stopped before
    an adopting agent receives it as binding operating context. Raised rather
    than returned for the same reason as ``RoleWriteRefused``: a refusal a
    caller can ignore is a refusal a caller will ignore.
    """

    def __init__(self, role_id: str, missing: list[str]):
        self.role_id = role_id
        self.missing = list(missing)
        super().__init__(
            f"role {role_id!r} cannot be promoted — missing required sections: "
            + "; ".join(self.missing)
        )


#: One clock for the roles subsystem — see ``roles/_time.py``. This was a
#: private copy here and another in ``actions.py``; both wrote the same
#: columns, which is the case where two answers to "what time is it" stop
#: being harmless.
_now = now_iso


def _tools_json(tools: Any) -> str | None:
    """Normalise ``tools`` to the JSON-text shape the column stores."""
    if tools is None:
        return None
    if isinstance(tools, str):
        return tools
    return json.dumps(tools)


def _validate(role_id: str, tier: Any, tools: Any) -> None:
    """Run BOTH halves of the vocabulary gate.

    ``live=`` is not optional here even though the signature allows it. Without
    it, ``validate_role_row``'s dead-tool branch is
    ``elif live and _OKURO_VERB.match(name) ...`` — falsy on the first clause,
    so the half of the gate that catches an okuro verb the registry no longer
    serves never executes and the gate silently checks tiers only.

    ``live_tool_names()`` returns an EMPTY SET when the MCP registry is
    unreadable, which turns the check back off by itself. That is the right
    failure: refusing a write because the tool registry could not be imported
    would make role creation depend on a subsystem it has nothing to do with.
    """
    from okuro.roles.vocabulary import live_tool_names, validate_role_row

    problems = validate_role_row(
        {"role_id": role_id, "tier": tier, "tools": tools},
        live=live_tool_names(),
    )
    if problems:
        raise RoleWriteRefused(problems)


def _write_tags(db, role_id: str) -> list[str]:
    """Derive tags from the row AS STORED and write them back.

    Reads the row rather than trusting the caller's payload: a partial update
    leaves some tag inputs at their previous values, and deriving from the
    payload alone would produce tags for a role that does not exist.
    """
    from okuro.roles.skills import derive_tags_for_role

    row = db.fetchone(
        "SELECT role_id, domain, tier, description FROM roles WHERE role_id = ?",
        (role_id,),
    )
    if not row:
        return []
    tags = derive_tags_for_role(
        {
            "id": row["role_id"],
            "domain": row["domain"],
            "tier": row["tier"] or "standard",
            "description": row["description"] or "",
        }
    )
    db.execute(
        "UPDATE roles SET tags = ? WHERE role_id = ?",
        (json.dumps(tags), role_id),
    )
    return tags


def _touch(db, role_id: str) -> None:
    db.execute(
        "UPDATE roles SET updated_at = ? WHERE role_id = ?", (_now(), role_id)
    )


#: Content columns the upsert carries, with the value used when a row is being
#: CREATED and the caller said nothing. ``None`` means "no default — the column
#: stays NULL on insert".
#:
#: Every one of these is COALESCEd against the stored value on conflict, which
#: is the whole contract of this table: AN ABSENT KEY IS NOT AN INSTRUCTION TO
#: BLANK A COLUMN. Before that was true, `upsert_role({'role_id': 'reviewer',
#: 'domain': 'quality'})` nulled the description, reset tier and model to their
#: defaults, dropped all three bodies and flipped `panel_eligible` back on —
#: and then, because `embed_role_text` returns None for an empty description,
#: left `vec_roles` and `description_embedded` pointing at the OLD text. The
#: backfill could not repair it either: its WHERE opens with
#: `TRIM(COALESCE(description,'')) != ''`, so a blanked row is skipped forever.
#: One partial call, unrecoverable desync.
_CONTENT_COLUMNS: tuple[tuple[str, Any], ...] = (
    ("domain", None),
    ("description", None),
    ("tier", "standard"),
    ("model", "sonnet"),
    ("tools", None),
    ("prompt", None),
    ("lean_prompt", None),
    ("micro_prompt", None),
    ("panel_eligible", 1),
    ("maintenance_schedule", "monthly"),
    ("person_preset", None),
)


_CONTENT_COLS_SQL = ", ".join(c for c, _ in _CONTENT_COLUMNS)


def _content_binds(row: dict) -> tuple[list[Any], list[Any], list[str], list[str]]:
    """Bind one payload against ``_CONTENT_COLUMNS`` for both write paths.

    Two binds per column, because the insert path and the conflict path need
    DIFFERENT values while staying ONE statement: the insert takes
    ``COALESCE(?, <default>)``, the conflict takes the RAW ``?``. Resolving
    the default before the bind would make ``excluded.tier`` read 'standard'
    on conflict and reintroduce the blanking that cost a role its description.

    Shared by ``upsert_role`` and ``insert_role`` so the two cannot disagree
    about what a column's default is — a second hand-written copy of this loop
    is how the tier default became a literal in the callers this module
    replaced.
    """
    values: list[Any] = []
    conflict_values: list[Any] = []
    insert_exprs: list[str] = []
    set_clauses: list[str] = []
    for col, default in _CONTENT_COLUMNS:
        raw = row.get(col)
        if col == "tools":
            raw = _tools_json(raw)
        elif col == "panel_eligible" and raw is not None:
            raw = 1 if raw else 0
        values.append(raw)
        conflict_values.append(raw)
        insert_exprs.append("?" if default is None else "COALESCE(?, ?)")
        if default is not None:
            values.append(default)
        set_clauses.append(f"{col} = COALESCE(?, roles.{col})")
    return values, conflict_values, insert_exprs, set_clauses


def _settle(db, role_id: str, text: Any, blob, *, already: bool = False) -> tuple:
    """Tags, vector and ``updated_at`` — the post-steps every write owes.

    Returns ``(tags, embedded)``. Extracted so a new write path cannot ship
    three of the four guarantees, which is the exact defect this module was
    built to end.
    """
    from okuro.roles.vectors import write_role_vector

    tags = _write_tags(db, role_id)
    if already:
        embedded = "unchanged"
    elif blob is None:
        embedded = "skipped" if not (text or "").strip() else "failed"
    elif write_role_vector(db, role_id, text, blob):
        embedded = "written"
    else:
        embedded = "failed"
    _touch(db, role_id)
    return tags, embedded


def insert_role(row: dict, *, actor: str) -> dict:
    """Create a role that MUST NOT already exist. Raises ``RoleExists`` if it does.

    WHY THIS EXISTS ALONGSIDE ``upsert_role``. A caller whose contract is
    "create, never overwrite" cannot express that with an upsert. Asking the
    table first and then upserting is check-then-act: between the SELECT and
    the INSERT another writer can take the id, and the upsert then silently
    takes its CONFLICT path and OVERWRITES that role's content — the precise
    outcome the caller refused. Reporting ``created=False`` afterwards does
    not help, because by then the damage is written.

    So the collision is decided by the database, in one statement, with no
    ``ON CONFLICT`` clause to fall through to. ``sqlite3.IntegrityError`` on
    the primary key IS the answer, and it arrives before any content lands.

    Everything else matches ``upsert_role`` exactly — same column defaults via
    ``_content_binds``, same post-steps via ``_settle``, same network-call
    ordering (embed outside the writer lock), same ``embedded`` tri-state.

    Returns ``{role_id, created, embedded, tags}``; ``created`` is always True,
    because the only other outcome raised.
    """
    import sqlite3

    from okuro.db import get_db
    from okuro.roles.vectors import embed_role_text

    role_id = (row.get("role_id") or "").strip()
    if not role_id:
        raise ValueError("insert_role needs a role_id")
    if not row.get("domain"):
        raise ValueError(f"insert_role({role_id}) needs a domain")

    _validate(role_id, row.get("tier"), row.get("tools"))

    db = get_db()
    description = row.get("description")

    # Network first, outside the writer lock — same reason as upsert_role.
    blob = embed_role_text(role_id, description)

    values, _, insert_exprs, _ = _content_binds(row)
    now = _now()

    try:
        with db.transaction():
            db.execute(
                f"INSERT INTO roles ({_CONTENT_COLS_SQL}, role_id, maturity, "
                f"sessions, learnings, created_at, updated_at) "
                f"VALUES ({', '.join(insert_exprs)}, ?, ?, 0, 0, ?, ?)",
                tuple(values)
                + (role_id, row.get("maturity") or "active", now, now),
            )
            tags, embedded = _settle(db, role_id, description, blob)
    except sqlite3.IntegrityError as exc:
        # The primary key is the only unique constraint on this table, so a
        # collision here means the id was taken. Narrow on purpose: any other
        # IntegrityError is a real schema violation and must not be reported
        # to the caller as "that name is in use".
        if "roles.role_id" in str(exc) or "UNIQUE" in str(exc).upper():
            raise RoleExists(role_id) from exc
        raise

    log.info(
        "roles.write: inserted %s by %s (embedded=%s, tags=%d)",
        role_id,
        actor,
        embedded,
        len(tags),
    )
    return {
        "role_id": role_id,
        "created": True,
        "embedded": embedded,
        "tags": tags,
    }


def upsert_role(row: dict, *, actor: str) -> dict:
    """Create or update a role's CONTENT, then settle everything derived from it.

    ``row`` needs ``role_id`` and ``domain``. Every other content column is
    optional, and what "optional" means differs by case, which is the part that
    was wrong before:

    * **on INSERT** — an absent key takes the default in ``_CONTENT_COLUMNS``
      (``tier='standard'``, ``model='sonnet'``, ``panel_eligible=1``,
      ``maintenance_schedule='monthly'``), or stays NULL where there is none.
    * **on CONFLICT** — an absent key CHANGES NOTHING. Every content column is
      ``COALESCE(excluded.col, roles.col)``, so a partial call updates the keys
      it names and leaves the rest exactly as they were.

    A consequence worth stating plainly: ``upsert_role`` CANNOT CLEAR a column,
    because an explicit ``None`` is indistinguishable from an absent key in a
    dict. Clearing is a deliberate act and goes through ``update_role_fields``,
    which writes the fields it is given and nothing else.

    ``tier`` is taken from the caller and checked against
    ``orchestrator.config.CANONICAL_TIERS`` by the vocabulary gate. It is never
    hardcoded here — two of the callers this replaces wrote a literal
    ``'standard'`` into the INSERT, which is how a role designed for the top
    model came out running on the default one.

    ``maturity`` applies on INSERT only. On conflict the stored value stands:
    the runtime promotes roles (``knowledge.py`` auto-matures, ``promote_role``
    below) and a content rewrite that carried a stale ``maturity`` would demote
    a live role without anyone asking it to.

    Returns ``{role_id, created, embedded, tags}``; see ``embedded``'s four
    values in the module docstring.
    """
    from okuro.db import get_db
    from okuro.roles.vectors import embed_role_text, write_role_vector

    role_id = (row.get("role_id") or "").strip()
    if not role_id:
        raise ValueError("upsert_role needs a role_id")
    if not row.get("domain"):
        raise ValueError(f"upsert_role({role_id}) needs a domain")

    _validate(role_id, row.get("tier"), row.get("tools"))

    db = get_db()
    existing = db.fetchone(
        "SELECT description, description_embedded FROM roles WHERE role_id = ?",
        (role_id,),
    )
    created = existing is None

    values, conflict_values, insert_exprs, set_clauses = _content_binds(row)
    cols = _CONTENT_COLS_SQL
    now = _now()

    # The embed is a NETWORK call and must not happen while the writer lock is
    # held — one unreachable embed service would otherwise stall the database.
    # Doing it first also keeps embed-before-delete structural: no blob means
    # no vector statement runs and the old vector keeps serving.
    description = row.get("description")
    effective_description = (
        description if description is not None
        else (existing["description"] if existing else None)
    )
    already_embedded = (
        not created and existing["description_embedded"] == effective_description
    )
    blob = None
    if not already_embedded:
        blob = embed_role_text(role_id, effective_description)

    with db.transaction():
        db.execute(
            f"INSERT INTO roles ({cols}, role_id, maturity, sessions, learnings, "
            f"created_at, updated_at) "
            f"VALUES ({', '.join(insert_exprs)}, ?, ?, 0, 0, ?, ?) "
            f"ON CONFLICT(role_id) DO UPDATE SET {', '.join(set_clauses)}",
            tuple(values)
            + (role_id, row.get("maturity") or "active", now, now)
            + tuple(conflict_values),
        )

        tags = _write_tags(db, role_id)

        if already_embedded:
            embedded = "unchanged"
        elif blob is None:
            embedded = "skipped" if not (effective_description or "").strip() else "failed"
        elif write_role_vector(db, role_id, effective_description, blob):
            embedded = "written"
        else:
            embedded = "failed"

        _touch(db, role_id)

    log.info(
        "roles.write: %s %s by %s (embedded=%s, tags=%d)",
        "created" if created else "updated",
        role_id,
        actor,
        embedded,
        len(tags),
    )
    return {
        "role_id": role_id,
        "created": created,
        "embedded": embedded,
        "tags": tags,
    }


def update_role_fields(role_id: str, fields: dict, *, actor: str) -> dict:
    """Change SOME of a role's content columns, through the same post-steps.

    The post-steps are conditional because they are expensive and because
    running them unconditionally would be its own lie: re-embedding a role
    whose description did not change rewrites ``description_embedded`` with
    identical text and hides a genuinely stale vector behind a fresh timestamp.

    * tags — re-derived when any of ``description``, ``domain`` or ``tier``
      moves, because those are the only inputs ``derive_tags_for_role`` reads;
    * vector — re-embedded when ``description`` moves, because the description
      is the only text ``vec_roles`` holds;
    * ``updated_at`` — always, because the row changed.

    ``tier`` and ``tools`` go through the same fatal gate as creation: a value
    the POST endpoint refuses must not be reachable by editing an existing row,
    which was the hole before this helper.

    Unlike ``upsert_role``, this DOES write the value it is given, including an
    explicit empty one — naming a field here is a deliberate act, so clearing a
    column is expressible. That asymmetry is the reason both functions exist.

    Returns ``{role_id, fields, embedded, tags}``. ``fields`` is the list
    actually written; ``tags`` is None when they were not recomputed, which is
    not the same fact as an empty list.
    """
    from okuro.db import get_db
    from okuro.roles.registry import update_role_info
    from okuro.roles.vectors import embed_role_text, write_role_vector

    unknown = sorted(set(fields) - WRITABLE_FIELDS)
    if unknown:
        raise ValueError(
            f"update_role_fields({role_id}): {', '.join(unknown)} is not a "
            f"writable content column — the runtime derives it "
            f"(see okuro/roles/columns.py)"
        )

    db = get_db()
    current = db.fetchone(
        "SELECT tier, tools, description FROM roles WHERE role_id = ?", (role_id,)
    )
    if not current:
        raise KeyError(role_id)

    # Validate the row as it will BE, not only the half being sent: a payload
    # that changes tools alone still has to agree with the stored tier.
    _validate(
        role_id,
        fields["tier"] if "tier" in fields else current["tier"],
        fields["tools"] if "tools" in fields else current["tools"],
    )

    writes = dict(fields)
    if not writes:
        return {
            "role_id": role_id,
            "fields": [],
            "embedded": "unchanged",
            "tags": None,
        }
    if "tools" in writes:
        writes["tools"] = _tools_json(writes["tools"])

    # Network first, outside the writer lock — same reason as upsert_role.
    re_embed = (
        "description" in writes and writes["description"] != current["description"]
    )
    blob = embed_role_text(role_id, writes["description"]) if re_embed else None

    with db.transaction():
        update_role_info(role_id, writes)

        tags: list[str] | None = None
        if any(k in writes for k in _TAG_INPUTS):
            tags = _write_tags(db, role_id)

        if not re_embed:
            embedded = "unchanged"
        elif blob is None:
            embedded = (
                "skipped" if not (writes["description"] or "").strip() else "failed"
            )
        elif write_role_vector(db, role_id, writes["description"], blob):
            embedded = "written"
        else:
            embedded = "failed"

        _touch(db, role_id)

    log.info(
        "roles.write: updated %s by %s (fields=%s, embedded=%s)",
        role_id,
        actor,
        ",".join(sorted(writes)),
        embedded,
    )
    return {
        "role_id": role_id,
        "fields": sorted(writes),
        "embedded": embedded,
        "tags": tags,
    }


def _has_vec_roles(db) -> bool:
    """Whether the vec0 table exists yet.

    A very fresh store has the `roles` table but not `vec_roles`: the vec_*
    tables are created after the migrations by ``embed.repair.ensure_vec_dims``.
    That is a "come back next boot" condition and the ONLY reason a statement
    against `vec_roles` may be skipped. Asking sqlite_master answers it exactly,
    which is what lets the delete path stop swallowing every other error.
    """
    return bool(
        db.fetchone(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name = 'vec_roles'"
        )
    )


def delete_role(role_id: str, *, actor: str) -> bool:
    """Remove the role AND its vector, in one transaction.

    Deleting only the ``roles`` row left an orphan in ``vec_roles`` that still
    answered similarity queries. ``match_roles`` searches the vector index and
    then reads the row back, so the orphan produced a match for a role that no
    longer exists — recreating the id later would have silently inherited the
    dead role's embedding.

    Returns False when there was no such role.
    """
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT role_id FROM roles WHERE role_id = ?", (role_id,)):
        return False

    # A blanket `except Exception` here reinstated the orphan this function
    # exists to prevent, and still reported success: any failure to delete the
    # vector was logged and swallowed, leaving a row in `vec_roles` that keeps
    # answering similarity queries for a role that no longer exists. The ONLY
    # acceptable reason to skip the statement is that the table is not there
    # yet, and that is a question with an exact answer — so ask it, and let
    # every other failure roll the whole delete back.
    has_vectors = _has_vec_roles(db)
    if not has_vectors:
        log.info(
            "roles.write: vec_roles does not exist yet; deleting %s from roles only",
            role_id,
        )

    with db.transaction():
        db.execute("DELETE FROM roles WHERE role_id = ?", (role_id,))
        if has_vectors:
            db.execute("DELETE FROM vec_roles WHERE id = ?", (role_id,))

    log.info("roles.write: deleted %s by %s", role_id, actor)
    return True


def promote_role(role_id: str, *, actor: str) -> dict:
    """Make a role active, then settle what its invisibility was hiding.

    A promoted role becomes reachable by ``roles_match`` for the first time, so
    promotion is exactly the moment its derived data has to be correct. The
    vector is written when it is MISSING **or STALE** — the old endpoint only
    checked for missing, which left a role that was edited while in draft
    matching on the wording it had before the edit.

    THE STRUCTURE GATE, added after review. "A role only ships when it passes
    the structure audit" was written in three briefs and enforced nowhere: the
    creation paths defaulted an incomplete body to ``draft``, and then every
    promotion path — this function, the HTTP endpoint, ``confirm_promote`` —
    flipped it to active without looking at the body again. Draft was a
    holding pen with an unlocked door.

    The gate lives HERE, at the funnel, and not in the three callers, for the
    same reason the rest of this module exists: a rule installed once per
    caller is a rule the next caller will not install. It reads the bodies AS
    STORED, never a payload, because the body may have been edited since it
    was written and the audit has to judge what is actually about to go live.

    Refusal is fatal and nothing is written — not the maturity, not the
    vector, not ``updated_at``. A role that half-promoted would be worse than
    one that did not promote at all.

    Returns ``{role_id, embedded, tags, structure}``.
    """
    from okuro.db import get_db
    from okuro.roles.designer import gate_role_body
    from okuro.roles.vectors import embed_role_text, write_role_vector

    db = get_db()
    row = db.fetchone(
        "SELECT description, description_embedded, prompt, lean_prompt, "
        "micro_prompt FROM roles WHERE role_id = ?",
        (role_id,),
    )
    if not row:
        raise KeyError(role_id)

    gate = gate_role_body(
        {
            "full": row["prompt"] or "",
            "lean": row["lean_prompt"] or "",
            "micro": row["micro_prompt"] or "",
        }
    )
    if not gate["ok"]:
        missing = gate["flat_missing"] or ([gate["error"]] if gate["error"] else [])
        raise RolePromotionRefused(role_id, missing)

    has_vector = _has_vec_roles(db) and bool(
        db.fetchone("SELECT id FROM vec_roles WHERE id = ?", (role_id,))
    )
    stale = row["description_embedded"] != row["description"]
    needs_vector = not has_vector or stale

    # Network first, outside the writer lock — same reason as upsert_role.
    blob = embed_role_text(role_id, row["description"]) if needs_vector else None

    with db.transaction():
        db.execute(
            "UPDATE roles SET maturity = 'active' WHERE role_id = ?", (role_id,)
        )
        tags = _write_tags(db, role_id)

        if not needs_vector:
            embedded = "unchanged"
        elif blob is None:
            embedded = (
                "skipped" if not (row["description"] or "").strip() else "failed"
            )
        elif write_role_vector(db, role_id, row["description"], blob):
            embedded = "written"
        else:
            embedded = "failed"

        _touch(db, role_id)

    log.info(
        "roles.write: promoted %s by %s (embedded=%s, was_stale=%s)",
        role_id,
        actor,
        embedded,
        stale,
    )
    return {
        "role_id": role_id,
        "embedded": embedded,
        "tags": tags,
        "structure": {
            "ok": gate["ok"],
            "missing": gate["missing"],
            "advisory": gate["advisory"],
        },
    }
