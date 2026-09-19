# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: One parser for the timestamps the roles tables store.
# index:
#   def parse_ts
#   def now_iso
# AGENT_HEADER_END -->
"""The roles subsystem reads three kinds of timestamp out of SQLite and has to
turn all of them into one aware-UTC datetime: ``datetime('now')`` defaults
(naive, space-separated), ISO strings written by Python (offset-aware), and
the occasional ``Z`` suffix. One function, because two copies of a date parser
disagree the first time either learns a new format and nothing says which
answer is the right one.

``fit.py`` and ``source_poll.py`` carried this byte-for-byte. It lives here
now and both import it.

Scope note: ``ingress/adapters/telegram.py`` and
``orchestrator/reviewer/side_effect_evidence.py`` also define a ``_parse_ts``.
Neither is this function — telegram parses a Unix epoch and returns a
non-optional value, and the reviewer one takes ``Any`` with different failure
behaviour. Pulling them in would merge three contracts that only share a name.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(value) -> datetime | None:
    """Parse a SQLite datetime string or ISO timestamp into aware UTC.

    Returns None for an empty value or an unparseable one — a caller deciding
    "how long ago" needs to tell "no timestamp" from "the epoch", and an
    exception here would turn one malformed row into a failed sweep.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_iso() -> str:
    """The timestamp the roles tables are written with: aware UTC, ISO-8601.

    ``write.py`` and ``actions.py`` each carried this as a private ``_now``.
    Two copies of "what time is it" is not a correctness problem today and is
    exactly the shape that becomes one: the day either grows a precision, a
    timezone or a format, rows written by two modules into the same column stop
    sorting against each other and nothing says which is right.

    Paired with :func:`parse_ts` on purpose — one module owns both ends of the
    roles subsystem's clock.
    """
    return datetime.now(timezone.utc).isoformat()
