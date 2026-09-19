# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The checks store — a maintenance sweep's "I looked and nothing
#   moved" outcome, kept as an observation instead of forged into knowledge.
# index:
#   imports
#   NO_CHANGE_MARKER / CHECK_OUTCOMES
#   def classify_no_change
#   def write_check
#   def checks_for_role
#   def check_summaries_for_role
#   def check_summaries_by_role
#   def latest_check_at
# AGENT_HEADER_END -->
"""Where a no-change maintenance outcome goes now that it is not a learning.

**The class this module exists for.** A daily sweep asks every role "did
anything change". For most roles on most days the honest answer is no, and
that answer is worth recording — it says the role was checked, against what,
and when. It is not knowledge, because nothing was learned. okuro filed it as
knowledge anyway for five months, because ``roles_learn`` was the only verb
the sweep had, and 386 of 844 rows in ``role_knowledge`` are the result.

Migration 159 moved those rows here. This module is what stops them coming
back: the routing is in :func:`okuro.roles.knowledge.write_knowledge`, at the
code level, so it holds no matter what the sweep's prompt says. A prompt can
be edited by anyone with a text editor and is not versioned with the schema;
the three previous attempts to fix this class of defect were all prompt edits.

**The marker is a fallback, not the mechanism.** ``roles_learn`` grew an
explicit ``outcome`` parameter so a caller can SAY it found nothing.
:func:`classify_no_change` catches the caller that only writes prose — which is
every agent written before that parameter existed. Preferring the explicit
signal and keeping a heuristic underneath is the difference between a migration
path and a flag day.

**The heuristic is narrow, and the wide version is what migration 159 used.**
Those are two different jobs and they get two different rules. The migration
matched a bare substring against a FROZEN corpus of 386 rows, where the match
could be verified by reading what it caught. A live write has no such luxury:
the same rule applied to new prose files a genuine finding as a receipt and
loses it. So the live rule demands the marker at the start, a short body and a
named period, all three — see :func:`classify_no_change`.
"""

from __future__ import annotations

import re
import uuid
from typing import Iterable

from okuro.db import get_db


#: The substring five months of sweep output happens to share, matched
#: case-insensitively. Migration 159 used it for the one-time move of 386 rows
#: and this module keeps it for the caller that passes no explicit outcome.
#:
#: It lives HERE and is imported by ``roles.fit`` rather than the other way
#: round: the scorer reads a rule it does not own, and the module that owns
#: the checks table owns the definition of what belongs in it.
NO_CHANGE_MARKER = "no significant change"

#: Mirrors the CHECK constraint in migration 159. Kept in Python too because a
#: SQLite CHECK violation surfaces as an opaque IntegrityError at the bottom of
#: a write path, and the caller that passed a typo deserves to be told which
#: word was wrong.
CHECK_OUTCOMES: tuple[str, ...] = ("no_change", "error", "skipped")

#: The outcomes that reset a role's maintenance clock.
#:
#: ONLY ``no_change``. This is the whole reason ``outcome`` is a column rather
#: than a boolean. ``error`` means the sweep tried to look and could not, and
#: ``skipped`` means it never looked at all — treating either as a completed
#: check would push the role's staleness out by a full cycle on the strength of
#: a failure, which is precisely how a dead feed stays invisible. As an
#: undifferentiated "no significant change" knowledge row, six weeks of a
#: broken fetch and six weeks of a stable source were the same string.
CLOCK_RESETTING_OUTCOMES: frozenset[str] = frozenset({"no_change"})


#: Longest a body may be and still be read as a receipt by the heuristic.
#: A sweep's no-change line is one or two sentences. Past this the entry is
#: saying something, and something is knowledge whatever it opens with.
RECEIPT_MAX_CHARS = 240

#: A receipt names the period it covers — that is what makes it a receipt
#: rather than a claim about the world. A four-digit year, an ISO date, or one
#: of the interval words the sweep template uses.
_PERIOD = re.compile(
    r"\b(19|20)\d{2}\b|\b(week|month|quarter|fortnight|cycle|sweep|today)\b",
    re.IGNORECASE,
)


def classify_no_change(text: str | None) -> tuple[bool, str]:
    """Is this body a sweep receipt? Returns the verdict AND the reason.

    **Why this is narrow, and what the wide version cost.** The first version
    routed on the bare substring, which means a genuine finding saying "the
    2026-06 revision makes no significant change to the tool-result envelope,
    but it does rename …" was filed as a check: no embedding, no row in
    ``role_knowledge``, invisible to every later search. Silently converting a
    finding into a receipt is a worse failure than the one this whole phase
    exists to fix, because the receipt at least existed.

    So the heuristic now has to clear three bars together, and an entry that
    clears two of them stays knowledge:

    1. the body OPENS with the marker — a receipt leads with its verdict;
    2. it is shorter than :data:`RECEIPT_MAX_CHARS` — a receipt has nothing to
       elaborate;
    3. it NAMES A PERIOD — which is what a receipt is actually reporting, and
       what a claim about the world does not need.

    The reason string is returned rather than logged because the caller puts it
    in the response: an agent whose finding was filed as a check, or whose
    receipt was filed as knowledge, deserves to be told which rule decided and
    not to have to guess. The explicit ``outcome`` parameter outranks all of
    this and is the path every new caller should take.
    """
    body = (text or "").strip()
    if not body:
        return False, "empty body — not routed as a check"

    lowered = body.lower()
    if NO_CHANGE_MARKER not in lowered:
        return False, "no no-change marker"
    if not lowered.startswith(NO_CHANGE_MARKER):
        return (
            False,
            f"the marker appears but the entry does not open with it "
            f"(starts {body[:40]!r}) — treated as a finding",
        )
    if len(body) >= RECEIPT_MAX_CHARS:
        return (
            False,
            f"opens with the marker but runs to {len(body)} chars "
            f"(>= {RECEIPT_MAX_CHARS}) — too much said to be a receipt",
        )
    if not _PERIOD.search(body):
        return (
            False,
            "opens with the marker but names no period — a receipt reports a "
            "window, so this is treated as a finding",
        )
    return True, "matches the sweep receipt template (marker, short, dated)"


def write_check(
    role_id: str,
    outcome: str = "no_change",
    *,
    run_id: str | None = None,
    source_url: str | None = None,
    note: str | None = None,
    session_id: str | None = None,
    db=None,
) -> dict:
    """Record one maintenance observation. No embedding, no vector, no counter.

    All three omissions are deliberate:

    * **No embedding.** A check carries no fact, so there is nothing for a
      semantic search to find. The 386 migrated rows each held a vector in
      ``vec_knowledge``, and a scoped knowledge search has a finite candidate
      pool — every dead vector in it displaces a live one.
    * **No ``roles.learnings`` bump.** The counter answers "how much has this
      role learned". A check is the answer "nothing", and counting it was how
      four roles reached the auto-maturity threshold on sweep receipts.
    * **No maturity check.** Follows from the above; there is no new count to
      re-evaluate.

    ``session_id`` IS carried, because it is the only thing that ties a check
    back to the run that produced it when ``run_id`` is absent — which is every
    sweep written before ``run_id`` existed. Dropping it was how 360 of the 386
    migrated rows would have become unattributable the moment they moved.

    Raises ``ValueError`` on an unknown outcome rather than letting SQLite
    refuse it: the CHECK constraint's error message names the table, not the
    word.
    """
    if outcome not in CHECK_OUTCOMES:
        raise ValueError(
            f"outcome must be one of {CHECK_OUTCOMES}, got {outcome!r}"
        )

    db = db or get_db()
    record_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO role_knowledge_checks "
        "(id, role_id, checked_at, outcome, run_id, source_url, session_id, note) "
        "VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?)",
        (record_id, role_id, outcome, run_id, source_url, session_id, note),
    )
    return {
        "id": record_id,
        "role_id": role_id,
        "outcome": outcome,
        "run_id": run_id,
        "source_url": source_url,
        "session_id": session_id,
    }


#: Every column. Only the detail path wants these — ``note`` holds the whole
#: prose the sweep wrote, which is why it exists and why nothing that merely
#: COUNTS checks should be reading it.
_CHECK_COLS = "id, role_id, checked_at, outcome, run_id, source_url, session_id, note"

#: What the scorer and the list actually consume.
#:
#: The fit segment reads ``checked_at`` for recency and counts the rows. On the
#: live store that is 386 rows whose ``note`` averages a couple of hundred
#: characters — roughly 90 KB of prose dragged through SQLite, across the
#: process boundary and into a JSON payload, per render of a page that displays
#: a number. The lean read is the same query without the part nobody looks at.
_CHECK_SUMMARY_COLS = "role_id, checked_at, outcome"


def checks_for_role(role_id: str, db=None) -> list[dict]:
    """Every check row for one role, in full, newest first.

    The DETAIL reader: it carries ``note``, so use it where the prose is going
    to be shown and :func:`check_summaries_for_role` everywhere else.
    """
    db = db or get_db()
    return [
        dict(r)
        for r in db.fetchall(
            f"SELECT {_CHECK_COLS} FROM role_knowledge_checks "
            "WHERE role_id = ? ORDER BY checked_at DESC",
            (role_id,),
        )
    ]


def check_summaries_for_role(role_id: str, db=None) -> list[dict]:
    """One role's checks, without the prose. What the fit scorer needs."""
    db = db or get_db()
    return [
        dict(r)
        for r in db.fetchall(
            f"SELECT {_CHECK_SUMMARY_COLS} FROM role_knowledge_checks "
            "WHERE role_id = ? ORDER BY checked_at DESC",
            (role_id,),
        )
    ]


def check_summaries_by_role(
    db=None, role_ids: Iterable[str] | None = None
) -> dict[str, list[dict]]:
    """Every role's checks, without the prose, grouped — ONE query for a page.

    Both callers render a whole fleet: the fit rollup scores 104 roles and the
    roles list renders 104 rows. A query per role there is 104 round trips for
    a table of a few hundred rows, which is the shape ``roles_fit._load_fleet``
    already avoids for ``role_knowledge`` and which this function exists so the
    list does not reintroduce.
    """
    db = db or get_db()
    rows = [
        dict(r)
        for r in db.fetchall(
            f"SELECT {_CHECK_SUMMARY_COLS} FROM role_knowledge_checks"
        )
    ]
    wanted = set(role_ids) if role_ids is not None else None
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        rid = row["role_id"]
        if wanted is not None and rid not in wanted:
            continue
        grouped.setdefault(rid, []).append(row)
    return grouped


def latest_check_at(role_id: str, db=None) -> str | None:
    """When this role was last CHECKED, whatever the outcome was."""
    db = db or get_db()
    row = db.fetchone(
        "SELECT MAX(checked_at) AS latest FROM role_knowledge_checks "
        "WHERE role_id = ?",
        (role_id,),
    )
    return row["latest"] if row else None
