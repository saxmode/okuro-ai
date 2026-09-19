# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Which `roles` columns a migration may write, and which belong to the runtime.
# index:
#   RUNTIME_OWNED_COLUMNS
#   CONTESTED_COLUMNS
#   MIGRATION_WRITABLE_COLUMNS
#   def illegal_writes
# AGENT_HEADER_END -->
"""The column-authority split for the `roles` table, written down as data.

The `roles` table has two kinds of author and they write the same table. A
MIGRATION ships content — the text a role is born with. The RUNTIME accrues
everything a role earns afterwards: how often it was used, what it learned,
when it was last maintained. Both are legitimate; the defect is a write that
does not know which kind it is.

This used to be a paragraph in a migration header, which is exactly how it
gets lost: migration 155 derived the split by hand from one read-only
measurement, and the next roles migration would have had to derive it again,
correctly, from nothing. A comment cannot fail a test. This module can, via
``tests/roles/test_no_role_catalog.py`` — any migration touching `roles` is
checked against it.

The measurement behind each list, taken 2026-09-16 against the live store:

* ``sessions`` / ``learnings`` — knowledge.py:231 and :171 increment them.
* ``last_maintained``          — maintainer.py:135.
* ``tags``                     — skills.py:137 derives them from the body.
* ``description_embedded``     — vectors.py, and only vectors.py; it records
  what the embedding was built from, so a migration asserting a value would be
  claiming to have embedded something it cannot embed.
* ``maturity``                 — CONTESTED. knowledge.py:249 auto-matures at
  sessions>=10 AND learnings>=20, and drafter.py:180 promotes. A migration may
  set it when INSERTING a new role (it has to; the row has to start somewhere)
  but must not overwrite it on conflict, or it silently demotes a role the
  runtime promoted.
* ``effort``                   — migration 107 added it and NOTHING in src has
  ever written it. Left out of both lists: claiming authority over a column no
  code uses would be inventing a rule rather than recording one.
"""

from __future__ import annotations

import re

#: Columns the runtime owns outright. A migration that writes one of these is
#: overwriting something the database earned, and there is no version of that
#: which is correct.
RUNTIME_OWNED_COLUMNS = frozenset(
    {
        "sessions",
        "learnings",
        "last_maintained",
        "tags",
        "description_embedded",
    }
)

#: Columns a migration may set on INSERT but must not overwrite on conflict.
#:
#: `origin` was the other member until migration 162 dropped the column. It is
#: not moved to another list here — a column that does not exist has no
#: authority to assign, and leaving a dead name in a frozenset is how a rule
#: outlives the thing it governed.
CONTESTED_COLUMNS = frozenset({"maturity"})

#: Content columns. A migration is the authority here; the runtime may still
#: edit them (that is what `roles_maintenance` does), which is why a migration
#: that rewrites one should expect to be the newer author, not merely a louder
#: one.
MIGRATION_WRITABLE_COLUMNS = frozenset(
    {
        "role_id",
        "domain",
        "description",
        "tier",
        "model",
        "prompt",
        "lean_prompt",
        "micro_prompt",
        "tools",
        "panel_eligible",
        "maintenance_schedule",
        "person_preset",
        "updated_at",
    }
)

#: `col = ...` inside an UPDATE / ON CONFLICT DO UPDATE SET clause.
_ASSIGNMENT = re.compile(r"(?m)^\s*(?:SET\s+)?([a-z_]+)\s*=(?!=)")


def illegal_writes(sql: str) -> set[str]:
    """Runtime-owned columns this SQL assigns to in an UPDATE-shaped clause.

    Deliberately crude: it reads assignments out of the text rather than
    parsing SQL, because the question is "did somebody type
    ``sessions = 0`` into a migration", and a regex answers that without
    pretending to understand the statement. Over-matching is harmless here —
    a false positive names a real assignment and a human reads one line.
    """
    found: set[str] = set()
    for chunk in re.split(r"(?is)\bDO\s+UPDATE\s+SET\b|\bUPDATE\s+roles\s+SET\b",
                          sql)[1:]:
        # Stop at the end of the statement; assignments cannot cross it.
        clause = chunk.split(";", 1)[0]
        for name in _ASSIGNMENT.findall(clause):
            if name in RUNTIME_OWNED_COLUMNS or name in CONTESTED_COLUMNS:
                found.add(name)
    return found
