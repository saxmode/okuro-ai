# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The fetch-verified gate — answer "did THIS run actually store the
#   body of the URL this row claims", and give the daily sweep a way to earn
#   a yes instead of asserting one.
# index:
#   imports
#   def canonical_url
#   def auto_source_id
#   def fetch_verified_for
#   def record_knowledge_fetch
# AGENT_HEADER_END -->
"""Amendment A2, in code: ``fetch_verified`` is set only by what fetched.

**The defect.** ``role_knowledge.source_url`` is a string the writing agent
typed. The fit scorer's ``url_claimed`` mode counts it, and the bucket is
honestly named ``claimed`` for that reason — nothing has ever checked that the
URL was opened, let alone that the sentence quoted from it is in it. okuro has
three recorded fabrication incidents in role-refresh, and every one passed its
own acceptance criteria, because the criteria were judged by reading the
report the fabricating agent wrote.

**The gate.** Migration 157 added ``fetch_verified`` and the content-addressed
store behind it. A1/A2 say the column may be set by exactly one kind of code:
code that HAS the fetched body. So the rule here is a join, not a judgement —
there must be an observation row for this ``run_id`` against a source whose URL
matches, and that observation must point at a stored body with bytes in it.
There is no branch in :func:`fetch_verified_for` that consults a model, a
prompt, or the caller's own claim.

**Why the sweep can earn it rather than merely fail it.** The structure
researcher does not write knowledge at all (AC5 — its tool grant excludes
``roles_learn``), so a gate that only ever returns False would be a gate on an
empty road. The daily role-refresh sweep DOES write knowledge, and had no way
to store what it fetched. :func:`record_knowledge_fetch` is that way, and
``POST /api/roles/knowledge-fetch`` is its door.

**Why an auto-registered source row and not a fourth table.** The store already
has the right shape: a registry naming a URL, a content-addressed blob per
distinct body, and one observation per (run, source). What it lacked was a way
to hold a URL nobody wants the weekly poller to walk. ``enabled = 0`` is that
way — :func:`okuro.roles.source_poll.poll_all` selects ``WHERE enabled = 1``,
so an auto-registered row is invisible to the poll and fully visible to
``verify_quote``. The registry becomes "every URL this system has stored a body
for", and ``enabled`` answers the separate question of which ones it watches.
A fourth table would have meant a second ``verify_quote``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

# Read-only imports. source_poll is shared with the structural-maintenance
# work; the hashing rule is taken FROM it rather than restated here, so a
# body stored by this path and a body stored by the poller are addressed
# identically and one ``verify_quote`` covers both.
from .source_poll import sha256_of


#: Prefix for a source row this module registered rather than a human. Makes
#: the auto-registered rows greppable and keeps them out of the way of the
#: nine hand-seeded registry entries.
AUTO_SOURCE_PREFIX = "auto"

#: Stamped into ``added_by`` so a listing can say who put the row there.
AUTO_ADDED_BY = "roles.evidence:knowledge-fetch"

#: What an auto-registered source is declared as. ``docs`` is the honest
#: default for "a page an agent read"; the kind column drives how surprising a
#: change is, and these rows are never polled, so nothing reads it.
AUTO_SOURCE_KIND = "docs"


def canonical_url(url: str | None) -> str:
    """The comparison form of a URL. Strict on purpose: whitespace only.

    Deliberately NOT normalising case, trailing slashes, query order or
    fragments. Every one of those would widen the set of strings that earn a
    ``fetch_verified`` flag, and this function's whole job is to be the narrow
    end of a gate. An agent that posts a body for one URL and then cites a
    near-miss gets ``fetch_verified = 0``, which is the correct answer: what it
    stored and what it cited are not demonstrably the same page.
    """
    return (url or "").strip()


def auto_source_id(url: str) -> str:
    """A deterministic id for an auto-registered source.

    Content-addressed on the URL, so posting the same page twice reuses one
    registry row and the fetch store's ``(source_id, content_sha256)`` unique
    index keeps doing its job across runs.
    """
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()
    return f"{AUTO_SOURCE_PREFIX}-{digest[:16]}"


def fetch_verified_for(db, run_id: str | None, source_url: str | None) -> bool:
    """Did ``run_id`` store a non-empty body for ``source_url``?

    Both arguments are required for a True. A row claiming a URL with no run
    behind it cannot be checked against anything, and a run with no URL has
    nothing to check — in both cases the answer is False and the row keeps the
    honest ``claimed`` status it already had.

    The observation row is what carries the run, and its ``fetch_id`` is what
    carries the body. A run that recorded a failure has ``fetch_id IS NULL``
    and does not pass, which is the point: "I looked and got a 403" must not
    verify a quotation.
    """
    run_id = (run_id or "").strip()
    url = canonical_url(source_url)
    if not run_id or not url:
        return False

    row = db.fetchone(
        "SELECT 1 AS ok "
        "  FROM source_fetch_runs r "
        "  JOIN role_structure_sources s ON s.id = r.source_id "
        "  JOIN source_fetches f ON f.id = r.fetch_id "
        " WHERE r.run_id = ? "
        "   AND TRIM(s.url) = ? "
        "   AND f.body IS NOT NULL "
        "   AND LENGTH(f.body) > 0 "
        " LIMIT 1",
        (run_id, url),
    )
    return bool(row)


def record_knowledge_fetch(
    db,
    *,
    url: str,
    run_id: str,
    body: str,
    http_status: int = 200,
    name: str | None = None,
) -> dict:
    """Store a body an agent fetched during a maintenance run.

    Three writes, in the order the store's own invariants need:

      1. the registry row, ``enabled = 0`` so the weekly poller never walks it
         and ``INSERT OR IGNORE`` so the second post of the same URL reuses it;
      2. the body, content-addressed — the same page fetched by twelve runs is
         one row, and the row that comes back is the one to point at;
      3. the observation for this (run, source), upserted, because a retry
         inside one run must correct its observation rather than collide.

    ``changed`` is left 0. This path has no notion of "changed since when" —
    it is not a poll, it has no previous hash to compare against, and claiming
    a change it did not measure would put a fabricated signal into the same
    column the poller writes honestly.
    """
    url = canonical_url(url)
    if not url:
        raise ValueError("url is required")
    run_id = (run_id or "").strip()
    if not run_id:
        raise ValueError("run_id is required — a body with no run verifies nothing")
    if not body:
        raise ValueError("body is empty — there would be nothing to verify against")

    source_id = auto_source_id(url)
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        "INSERT OR IGNORE INTO role_structure_sources "
        "(id, name, url, kind, check_method, enabled, added_by, "
        " last_status, last_checked_at) "
        "VALUES (?, ?, ?, ?, 'content_hash', 0, ?, ?, ?)",
        (source_id, name or url, url, AUTO_SOURCE_KIND, AUTO_ADDED_BY,
         http_status, now),
    )

    digest = sha256_of(body)
    existing = db.fetchone(
        "SELECT id FROM source_fetches "
        " WHERE source_id = ? AND content_sha256 = ?",
        (source_id, digest),
    )
    if existing:
        fetch_id = existing["id"]
    else:
        fetch_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO source_fetches "
            "(id, source_id, run_id, fetched_at, http_status, "
            " content_sha256, content_length, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fetch_id, source_id, run_id, now, http_status,
             digest, len(body), body),
        )

    db.execute(
        "INSERT INTO source_fetch_runs "
        "(id, run_id, source_id, fetch_id, observed_at, http_status, "
        " changed, alarms) VALUES (?, ?, ?, ?, ?, ?, 0, '[]') "
        "ON CONFLICT(run_id, source_id) DO UPDATE SET "
        "  fetch_id = excluded.fetch_id, "
        "  observed_at = excluded.observed_at, "
        "  http_status = excluded.http_status",
        (str(uuid.uuid4()), run_id, source_id, fetch_id, now, http_status),
    )

    return {
        "source_id": source_id,
        "fetch_id": fetch_id,
        "run_id": run_id,
        "url": url,
        "content_sha256": digest,
        "content_length": len(body),
        "stored_at": now,
    }
