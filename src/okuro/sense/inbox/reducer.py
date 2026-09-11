# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Inbox reducer — project active producer rows into the inbox overlay with salience.
# index: imports | def reduce_once | def surface_pass | row builders | def _supersede
# AGENT_HEADER_END -->
"""Inbox reducer — projects active producer rows into the ``inbox`` overlay.

Called by the daemon ``inbox-reduce`` task (and the manual POST
/api/inbox/reduce trigger). One pass:

1. Pull ACTIVE producer rows (bounded by ``max_rows``):
     - todos:               status IN (open, doing), id NOT LIKE 'stream-stub-%'
     - commitments:         status = 'open' AND not expired (kind='commitment')
     - signals:             status = 'open'
     - reminders:           status IN (pending, active, snoozed)
     - reminder_suggestions: accepted IS NULL
     - thoughts:            status = 'open' AND metadata.action_items present
       (action-items-only — keeps the ~136 actionable thoughts in, drops the
       ~150 non-actionable open thoughts that would otherwise flood research)
2. Classify each into a `kind`, normalize importance, look up type_weight /
   gravity / source_trust, compute age + salience via the pure scorer.
3. UPSERT keyed on id = "{ref_table}:{ref_id}". On insert state='new'. On
   conflict, refresh the scoring/title/project/kind columns but PRESERVE
   `state` (user disposition) and DO NOT touch dup_count / snoozed_until /
   surfaced_at.
4. SUPERSEDE: any inbox row in ('new','surfaced') whose source is no longer
   active this pass → state='superseded'. Snoozed/dismissed/acted rows are
   left alone.
5. SNOOZE-WAKE (Phase 2a): any 'snoozed' row whose snoozed_until has elapsed
   → back to state='new' (snoozed_until cleared).
6. DEDUP-COLLAPSE (Phase 2a): among ACTIVE rows ('new','surfaced') sharing a
   non-empty dedup_key, keep the single highest-salience row and supersede
   the rest; the keeper's dup_count records the group size (see _collapse_dups
   for the exact dup_count definition).

Returns ``{"upserted": n, "superseded": m, "revived": r, "woken": w,
"deduped": d, "by_kind": {...}, "truncated": bool}``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from okuro.db import get_db
from okuro.sense import signals as signals_svc

from . import scorer

log = logging.getLogger("okuro.sense.inbox.reducer")

# A thought that has been surfaced before and is older than this is treated
# as "forgotten" (re-surfacing repeatedly without resolution) rather than
# fresh "research". Tunable; documented threshold.
_FORGOTTEN_AGE_HOURS = 24.0 * 14  # 14 days

# Ingress producers that surface USER-CURATED content (things the user
# actively saved/favorited), not a raw firehose. Their todos are promoted out
# of the 'research' firehose tier so they can surface at all.
# Matched against todos.source_event_id via str.startswith.
#
# The two curated producers get DIFFERENT kinds, because they are different
# things competing for the same shelf:
#
#   saved-media producers → 'saved' — content the user favourited to look at
#                           later. Optional, evergreen, no deadline.
#   notes-extract         → 'note'  — a commitment the user WROTE, in their own
#                           words, often naming a colleague and a date.
#
# Measured 2026-07-15: lumping both into 'saved' put a dated presentation
# commitment naming two colleagues into a 7-slot cap already held by
# 43-day-old rows like a saved speaker ad. All 10 note-derived work items
# were locked out. A saved ad and a dated commitment to a colleague are not
# the same kind of thing and must not share a cap.
#
# External producers (personal ingest scripts) register their prefix→kind
# via the ``inbox.curated_kind_by_prefix`` convention; the product itself
# ships only its own producer.
def _curated_kind_by_prefix() -> dict[str, str]:
    from okuro.yu.conventions import get_convention

    table = {"notes-extract:": "note"}
    table.update(get_convention("inbox.curated_kind_by_prefix", {}) or {})
    return table


_CURATED_KIND_BY_PREFIX = _curated_kind_by_prefix()
_CURATED_SAVE_PREFIXES = tuple(_CURATED_KIND_BY_PREFIX)


def _curated_kind(source_event_id: str) -> str | None:
    """Return the inbox kind for a curated ingress producer, or None."""
    for prefix, kind in _CURATED_KIND_BY_PREFIX.items():
        if source_event_id.startswith(prefix):
            return kind
    return None


def reduce_once(max_rows: int = 6000) -> dict:
    """Run one inbox-reduce pass. See module docstring."""
    db = get_db()

    rows: list[dict] = []
    # ref_tables this pass enumerated COMPLETELY. Only these may be superseded
    # — see _supersede.
    complete_tables: set[str] = set()
    truncated = False

    # FAIR SHARE, not first-come. The budget used to be one FIFO pool drained
    # in pull order, which meant a producer that ballooned starved every pull
    # after it. Measured 2026-07-27 with max_rows=2000: signals took 1080 of
    # the pool, todos got 738 of the 1585 they wanted, and thoughts got ZERO
    # of 439 — 847 open todos and every open thought had no inbox row at all.
    #
    # Each pull now gets an equal share of what is left, and whatever a pull
    # does not use flows to the pulls after it. A single noisy producer can no
    # longer make the user's own todos invisible.
    # (pull, ref_table) — the table is declared, NOT read off the first row:
    # an EMPTY batch is a complete enumeration (the producer has no active
    # rows), and that is exactly the case supersede exists to handle. Inferring
    # the table from batch[0] would silently skip it.
    pulls = (
        (_pull_reminders, "reminders"),
        (_pull_commitments, "commitments"),
        (_pull_signals, "signals"),
        (_pull_suggestions, "reminder_suggestions"),
        (_pull_todos, "todos"),
        (_pull_thoughts, "thoughts"),
    )
    # ROUND 1 — every pull gets the same floor share. Ask for one MORE than
    # the share: a batch that comes back over it proves more exists, which a
    # batch exactly at it does not.
    base = max(1, max_rows // len(pulls))
    batches: dict[str, list[dict]] = {}
    hungry: list[tuple] = []          # pulls that had more to give
    for pull, table in pulls:
        batch = pull(db, base + 1)
        if len(batch) > base:
            hungry.append((pull, table))
            batches[table] = batch[:base]
        else:
            complete_tables.add(table)
            batches[table] = batch

    # ROUND 2 — a quiet pull's unused share is not lost. Whatever round 1 left
    # on the table is split among the pulls that wanted more, and only those
    # are re-read. Without this the budget is worse than useless at scale: at
    # max_rows=6000 a strict 1/6 floor would cut todos at 1000 while 3700 rows
    # of budget went unspent.
    if hungry:
        leftover = max_rows - sum(len(b) for b in batches.values())
        if leftover > 0:
            extra = -(-leftover // len(hungry))   # ceiling, so nobody gets 0
            for pull, table in hungry:
                limit = base + extra
                batch = pull(db, limit + 1)
                if len(batch) > limit:
                    batches[table] = batch[:limit]
                else:
                    complete_tables.add(table)
                    batches[table] = batch

    for _, table in pulls:
        rows.extend(batches[table])
    truncated = len(complete_tables) < len(pulls)

    # A final guard: rounding up in round 2 can overshoot the ceiling by a few
    # rows. Trim deterministically, and any table the trim touches is no longer
    # completely enumerated.
    if len(rows) > max_rows:
        kept = rows[:max_rows]
        complete_tables -= {r["ref_table"] for r in rows[max_rows:]}
        rows = kept
        truncated = True

    if truncated:
        log.warning(
            "inbox reduce_once truncated at max_rows=%d (%d rows processed, "
            "complete tables: %s)",
            max_rows,
            len(rows),
            sorted(complete_tables) or "none",
        )

    by_kind: dict[str, int] = {}
    active_ids: list[str] = []

    upsert_sql = (
        "INSERT INTO inbox "
        "(id, kind, ref_table, ref_id, project, title, salience, importance, "
        " type_weight, source_trust, gravity, age_anchor_at, dedup_key, "
        " state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', "
        " datetime('now'), datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  kind=excluded.kind, "
        "  project=excluded.project, "
        "  title=excluded.title, "
        "  salience=excluded.salience, "
        "  importance=excluded.importance, "
        "  type_weight=excluded.type_weight, "
        "  source_trust=excluded.source_trust, "
        "  gravity=excluded.gravity, "
        "  age_anchor_at=excluded.age_anchor_at, "
        "  dedup_key=excluded.dedup_key, "
        "  updated_at=datetime('now')"
        # NOTE: state / dup_count / snoozed_until / surfaced_at deliberately
        # untouched on conflict — preserve user disposition. Reviving a row
        # superseded by ABSENCE is a separate, narrower step: see _revive.
    )

    with db.write() as conn:
        # (a) Snooze-wake: elapsed snoozes return to the active pool BEFORE
        # the upsert/supersede so a re-touched source treats them as 'new'.
        woken = _wake_snoozed(conn)

        for r in rows:
            row_id = f"{r['ref_table']}:{r['ref_id']}"
            active_ids.append(row_id)
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            conn.execute(
                upsert_sql,
                (
                    row_id,
                    r["kind"],
                    r["ref_table"],
                    r["ref_id"],
                    r.get("project"),
                    r["title"],
                    r["salience"],
                    r["importance"],
                    r["type_weight"],
                    r["source_trust"],
                    r["gravity"],
                    r.get("age_anchor_at"),
                    r.get("dedup_key"),
                ),
            )
        revived = _revive(conn, active_ids)
        superseded = _supersede(conn, active_ids, complete_tables)
        # (b) Dedup-collapse runs AFTER supersede so stale rows are already
        # out of the active pool and don't get picked as keepers.
        deduped = _collapse_dups(conn)

    return {
        "upserted": len(active_ids),
        "superseded": superseded,
        "revived": revived,
        "woken": woken,
        "deduped": deduped,
        "by_kind": by_kind,
        "truncated": truncated,
    }


# ── surfacing gate (Phase 3b) ─────────────────────────────────────────

# Kinds that ALWAYS clear the bar when state='new' (high-value, never staged):
# user/agent commitments and near-complete continuations. Reminders are
# handled separately (all open reminders qualify — see surface_pass).
# Per-kind surfacing caps — the gate keeps the default Inbox focused AND
# BALANCED across kinds. A blanket "always-surface commitments" rule failed
# at scale: hundreds of LLM-inferred commitments crowded out every other kind
# (588 surfaced). Instead we surface the top-N 'new' rows of EACH kind by
# salience; the rest stay in the 'staging' tray. Reminders are time-critical
# and effectively uncapped (there are few). DP07: tune from dismiss behaviour.
_KIND_SURFACE_CAP = {
    "commitment": 10,
    "continue": 10,
    "reminder": 9999,
    "task": 8,
    # 'note' — its own cap, so a commitment the user wrote can never be blocked
    # by a backlog of favourited videos. Small on purpose: these are real
    # obligations to real people, and 5 is already more than one screen's worth.
    "note": 5,
    "saved": 7,
    "signal": 5,
    "research": 3,
    "forgotten": 2,
}
_DEFAULT_KIND_CAP = 5

# A 'running' session counts as "busy" only if it has been active within this
# window. Matches the session-hygiene orphan threshold
# (close_orphaned_sessions(timeout_minutes=30)) so a session that hygiene
# would already consider orphaned cannot starve the surfacing gate. Older
# 'running' rows are stale orphans (e.g. never-closed stream-stub stubs), not
# genuine active work.
_BUSY_FRESHNESS_MINUTES = 30


def surface_pass(db=None, budget: int = 60) -> dict:
    """Promote the highest-value 'new' inbox rows to 'surfaced' (the gate).

    The surfacing gate decides which staged ('new') rows clear the bar into
    the default Inbox view ('surfaced'). Runs on the ``inbox-heartbeat``
    DaemonTask (every 30m + on startup) and the manual POST
    /api/inbox/surface trigger.

    Behaviour
    ---------
    - DEFER-WHEN-BUSY: if a *genuinely active* orchestrator-managed session
      is running, do nothing and return ``{"deferred": True, "surfaced":
      0}``. "Active" = ``sessions_inline`` status='running' AND recent
      activity (within ``_BUSY_FRESHNESS_MINUTES``) AND not a
      ``stream-stub-%`` placeholder. Stale never-closed 'running' rows
      (orphans hygiene hasn't reaped yet) and stub rows do NOT count — they
      would otherwise starve the gate forever.
    - Contested pool = inbox rows with state IN ('new','surfaced').
      snoozed/dismissed/acted/superseded/expired are out by definition, so a
      user tombstone is never undone by a later pass.
    - WINNERS = the top ``_KIND_SURFACE_CAP[kind]`` rows of EACH kind by
      salience, drawn from the whole contested pool (per-kind caps keep the
      view focused AND balanced — no single high-volume kind, e.g. the
      hundreds of inferred commitments, can crowd out the rest). Unlisted
      kinds use ``_DEFAULT_KIND_CAP``; reminders are effectively uncapped.
      ``budget`` is an optional overall ceiling kept for API/back-compat.
    - Promote winners still in 'new' → 'surfaced'. DEMOTE surfaced rows that
      lost their seat → 'new' (surfaced_at cleared). The shelf is a
      leaderboard re-contested every pass, not a set of reservations handed
      out first-come — see the block comment below for the measured failure
      that forced this.
    - Idempotent: a stable shelf re-elects itself and both UPDATEs match zero
      rows.

    Returns ``{"surfaced": n, "demoted": d, "kept_staging": m,
    "deferred": False}`` (or the deferred / silent short-circuit dicts above).
    """
    if db is None:
        db = get_db()

    # DEFER-WHEN-BUSY — a genuinely active orchestrator-managed session only:
    # status='running', recent activity, and not a stream-stub placeholder.
    busy = db.fetchone(
        "SELECT 1 AS x FROM sessions_inline "
        "WHERE status='running' "
        "AND id NOT LIKE 'stream-stub-%' "
        "AND COALESCE(last_event_at, started_at) >= "
        f"    datetime('now', '-{_BUSY_FRESHNESS_MINUTES} minutes') "
        "LIMIT 1"
    )
    if busy:
        return {"deferred": True, "surfaced": 0}

    # LEADERBOARD, not a reservation. Each pass re-contests every slot: the
    # top `cap` rows of each kind across the COMBINED ('new','surfaced') pool
    # win the shelf; anything surfaced that loses its seat is demoted back to
    # 'new'.
    #
    # This replaces refill-to-cap, which computed `remaining = cap - already`
    # and only ever promoted into free slots. That assumed "a dismissed /
    # superseded row frees a slot the next pass refills" — but a slot freed
    # ONLY on user disposition or producer death, and there is no TTL. Measured
    # 2026-07-15 against the live DB: 0.19 dispositions/day against 25.5 new
    # items/day, so slots never freed. Every kind sat exactly at its cap with
    # 1151 rows stranded in 'new', 583 of them provably higher-salience than
    # the incumbent they were queued behind, and nothing new had entered the
    # todo / signal / saved lanes in 38-42 days. Both 'crit' signals in the
    # system were locked out by 43-day-old rows scoring 0.000052. Entry was
    # first-come-first-served; ranking never reached the user.
    #
    # Still idempotent: a stable shelf re-elects itself and writes nothing
    # (both UPDATEs are no-ops). Still capped, still per-kind — no high-volume
    # kind can crowd out the rest. User tombstones are untouched: 'acted',
    # 'dismissed' and 'snoozed' are outside the contested pool by definition,
    # so a dismissal is never undone by a later pass.
    _CONTESTED = ("new", "surfaced")

    winners: set[str] = set()
    kinds = db.fetchall(
        "SELECT DISTINCT kind FROM inbox WHERE state IN (?, ?)", _CONTESTED
    )
    for kr in kinds:
        kind = kr["kind"]
        cap = _KIND_SURFACE_CAP.get(kind, _DEFAULT_KIND_CAP)
        if cap <= 0:
            continue
        rows = db.fetchall(
            "SELECT id FROM inbox WHERE state IN (?, ?) AND kind=? "
            "ORDER BY salience DESC, updated_at DESC LIMIT ?",
            (*_CONTESTED, kind, cap),
        )
        winners.update(r["id"] for r in rows)

    # Optional overall ceiling (API/back-compat); keep highest-salience.
    budget = int(budget)
    if budget > 0 and len(winners) > budget:
        ranked = db.fetchall(
            "SELECT id FROM inbox WHERE id IN ({}) "
            "ORDER BY salience DESC, updated_at DESC LIMIT ?".format(
                ",".join("?" for _ in winners)
            ),
            (*winners, budget),
        )
        winners = {r["id"] for r in ranked}

    # Losers = currently surfaced but no longer good enough for a seat.
    losers = [
        r["id"]
        for r in db.fetchall("SELECT id FROM inbox WHERE state='surfaced'")
        if r["id"] not in winners
    ]
    # Winners that are not already on the shelf — the only rows to promote.
    pending = [
        r["id"]
        for r in db.fetchall("SELECT id FROM inbox WHERE state='new'")
        if r["id"] in winners
    ]

    # SILENT-WHEN-STABLE: a re-elected shelf changes nothing, so take no write
    # transaction and say nothing. Preserves the original silent contract —
    # now keyed on "no row moves" rather than "no candidates", which is what it
    # always meant.
    if not pending and not losers:
        return {"surfaced": 0, "silent": True}

    chunk = 500
    surfaced = 0
    demoted = 0
    with db.write() as conn:
        ids = pending
        for i in range(0, len(ids), chunk):
            part = ids[i : i + chunk]
            ph = ",".join("?" for _ in part)
            # surfaced_at is set on first promotion only — COALESCE keeps the
            # original impression time across re-elections, so "how long has
            # this been in front of me" stays honest.
            cur = conn.execute(
                f"UPDATE inbox SET state='surfaced', "
                f"surfaced_at=COALESCE(surfaced_at, datetime('now')), "
                f"updated_at=datetime('now') "
                f"WHERE id IN ({ph}) AND state='new'",
                tuple(part),
            )
            surfaced += cur.rowcount or 0

        for i in range(0, len(losers), chunk):
            part = losers[i : i + chunk]
            ph = ",".join("?" for _ in part)
            # Demote, don't dispose: back to 'new' so it can win a seat again
            # when the incumbents age out. surfaced_at is cleared — it has not
            # been in front of the user since.
            cur = conn.execute(
                f"UPDATE inbox SET state='new', surfaced_at=NULL, "
                f"updated_at=datetime('now') "
                f"WHERE id IN ({ph}) AND state='surfaced'",
                tuple(part),
            )
            demoted += cur.rowcount or 0

    kept = db.fetchone("SELECT count(*) c FROM inbox WHERE state='new'")["c"]
    return {
        "surfaced": surfaced,
        "demoted": demoted,
        "kept_staging": kept,
        "deferred": False,
    }


# ── supersession ──────────────────────────────────────────────────────


def _revive(conn, active_ids: list[str]) -> int:
    """Un-supersede rows whose producer is active again. Returns the count.

    'superseded' is a SYSTEM verdict, and it is reached two different ways:

      * BY ABSENCE — the producer stopped being active (_supersede). If the
        producer is active again, that verdict is simply wrong now, and
        nothing else would ever undo it: every clause of the upsert preserves
        state. 187 open todos were stranded exactly this way by a truncated
        pass before the budget fix.
      * BY DEDUP — a twin with higher salience holds the shelf
        (_collapse_dups). That verdict is still true, and reviving it would
        make every pass collapse the same row again: churn, and a `deduped`
        count that never settles to 0.

    The two are told apart without a new column: a dedup loser is a row whose
    dedup_key is still held by another ACTIVE row. No such holder means the
    row was superseded for absence.

    Never touches dismissed / acted / snoozed / expired — those are the user's
    or terminal.
    """
    active = set(active_ids)
    if not active:
        return 0

    candidates = conn.execute(
        "SELECT id, dedup_key FROM inbox WHERE state='superseded'"
    ).fetchall()
    held = {
        r["dedup_key"]
        for r in conn.execute(
            "SELECT DISTINCT dedup_key FROM inbox "
            "WHERE state IN ('new','surfaced') "
            "AND dedup_key IS NOT NULL AND dedup_key != ''"
        ).fetchall()
    }

    revivable = [
        c["id"] for c in candidates
        if c["id"] in active and (c["dedup_key"] or "") not in held
    ]
    if not revivable:
        return 0

    n = 0
    chunk = 500
    for i in range(0, len(revivable), chunk):
        part = revivable[i : i + chunk]
        placeholders = ",".join("?" for _ in part)
        cur = conn.execute(
            f"UPDATE inbox SET state='new', updated_at=datetime('now') "
            f"WHERE id IN ({placeholders}) AND state='superseded'",
            tuple(part),
        )
        n += cur.rowcount or 0
    return n


def _supersede(conn, active_ids: list[str], complete_tables: set[str]) -> int:
    """Mark active-state inbox rows not seen this pass as superseded.

    Only touches state IN ('new','surfaced') — snoozed / dismissed / acted /
    expired / already-superseded are preserved. Chunks the NOT IN set so a
    large active list never blows the SQLite parameter limit (~999).

    ONLY rows whose ``ref_table`` was enumerated COMPLETELY this pass are
    eligible. A truncated pull did not prove anything absent: it ran out of
    budget. Before this guard, absence-from-budget was read as
    absence-from-producer — measured 2026-07-27, 185 open todos sat at
    state='superseded' purely because the shared pool was exhausted before
    their pull. That is the same defect shape as retracting a signal because
    a capped scanner did not mention it, and it gets the same answer: silence
    under a cap is not evidence.
    """
    active = set(active_ids)
    if not complete_tables:
        return 0

    qmarks = ",".join("?" * len(complete_tables))
    if not active:
        # Nothing active → supersede every active-state row of the tables this
        # pass actually finished reading.
        cur = conn.execute(
            f"UPDATE inbox SET state='superseded', updated_at=datetime('now') "
            f"WHERE state IN ('new','surfaced') AND ref_table IN ({qmarks})",
            tuple(sorted(complete_tables)),
        )
        return cur.rowcount or 0

    # Build the candidate set, then diff in Python (avoids a giant NOT IN).
    candidates = conn.execute(
        f"SELECT id FROM inbox WHERE state IN ('new','surfaced') "
        f"AND ref_table IN ({qmarks})",
        tuple(sorted(complete_tables)),
    ).fetchall()
    stale = [c["id"] for c in candidates if c["id"] not in active]
    if not stale:
        return 0

    chunk = 500
    n = 0
    for i in range(0, len(stale), chunk):
        part = stale[i : i + chunk]
        placeholders = ",".join("?" for _ in part)
        cur = conn.execute(
            f"UPDATE inbox SET state='superseded', updated_at=datetime('now') "
            f"WHERE id IN ({placeholders})",
            tuple(part),
        )
        n += cur.rowcount or 0
    return n


# ── snooze-wake ───────────────────────────────────────────────────────


def _wake_snoozed(conn) -> int:
    """Return elapsed-snooze rows to 'new'.

    A 'snoozed' row whose snoozed_until is set and <= now is woken: state
    back to 'new', snoozed_until cleared. Future snoozes are left alone.
    """
    cur = conn.execute(
        "UPDATE inbox SET state='new', snoozed_until=NULL, "
        "updated_at=datetime('now') "
        "WHERE state='snoozed' AND snoozed_until IS NOT NULL "
        "AND snoozed_until <= datetime('now')"
    )
    return cur.rowcount or 0


# ── dedup-collapse ────────────────────────────────────────────────────


def _collapse_dups(conn) -> int:
    """Collapse active duplicate rows sharing a dedup_key.

    For each non-empty dedup_key with >1 ACTIVE row (state IN
    ('new','surfaced')): keep the single highest-salience row (ties broken
    by id for determinism), set every other active member to
    'superseded', and record dup_count on the keeper.

    dup_count definition (documented): the number of all inbox rows sharing
    the dedup_key whose state is NOT 'dismissed' and NOT 'acted' — i.e.
    active members + already-superseded/snoozed/expired members, but
    excluding rows the user has explicitly closed (dismiss/act). This makes
    dup_count a stable "how many things collapsed under this label" count
    that does not thrash as duplicates flip new→superseded across passes.

    Idempotent: once losers are 'superseded' they leave the active pool, so
    a re-run finds a singleton active group (or none) and makes no further
    state changes; the keeper's dup_count is simply recomputed to the same
    value. Empty/NULL dedup_keys are skipped entirely.

    Returns the count of rows newly set to 'superseded' this pass.
    """
    # Active groups with a real duplicate (>1 active member, non-empty key).
    dup_keys = conn.execute(
        "SELECT dedup_key FROM inbox "
        "WHERE state IN ('new','surfaced') "
        "AND dedup_key IS NOT NULL AND dedup_key != '' "
        "GROUP BY dedup_key HAVING count(*) > 1"
    ).fetchall()

    newly_superseded = 0
    for dk in dup_keys:
        key = dk["dedup_key"]
        members = conn.execute(
            "SELECT id, salience FROM inbox "
            "WHERE state IN ('new','surfaced') AND dedup_key=? "
            "ORDER BY salience DESC, id ASC",
            (key,),
        ).fetchall()
        keeper = members[0]["id"]
        losers = [m["id"] for m in members[1:]]
        if losers:
            placeholders = ",".join("?" for _ in losers)
            cur = conn.execute(
                f"UPDATE inbox SET state='superseded', "
                f"updated_at=datetime('now') WHERE id IN ({placeholders})",
                tuple(losers),
            )
            newly_superseded += cur.rowcount or 0
        # dup_count = all non-dismissed, non-acted rows sharing this key.
        cnt = conn.execute(
            "SELECT count(*) c FROM inbox "
            "WHERE dedup_key=? AND state NOT IN ('dismissed','acted')",
            (key,),
        ).fetchone()["c"]
        conn.execute(
            "UPDATE inbox SET dup_count=?, updated_at=datetime('now') "
            "WHERE id=?",
            (cnt, keeper),
        )
    return newly_superseded


# ── per-producer pulls (each returns scored row dicts) ────────────────


def _dedup_key(title: str | None) -> str:
    return (title or "").strip().lower()


def _note_anchors(db, note_ids) -> dict[str, str]:
    """note_id → the best available "when was this written" anchor.

    ``notes.authored_at`` when authorship was recorded, else the legacy
    ``frontmatter.original_date``, else ``notes.created_at``. ONE resolution,
    used by both note-derived pulls, so
    a todo and a signal lifted from the same note can never rank on different
    clocks.

    Why this exists: every timestamp in the Obsidian → note → signal chain is
    an INGESTION timestamp. Measured 2026-07-26, ranks 4-6 of the live
    surfaced inbox were notes written 2026-04-22..24 and migrated 2026-07-25 —
    ranked as one day old, 8x inflated, holding 8 of the 10 note+signal shelf
    slots. The notes themselves say "Written 2026-04-22. Migrated 2026-07-25";
    ``original_date`` carried it into frontmatter and nothing read it.

    Two guards, because frontmatter is agent-written and untyped:
      * the value must parse as a date — a garbage string must not become a
        ranking input;
      * it must not be LATER than ingestion. Authorship after import is
        impossible, so a later date is a bug or a lie, and created_at wins.

    Reaches authorship only where authorship was recorded: 8 notes of 491. The
    449 bulk-imported vault notes carry no date and cannot — 295 of 413 files
    shared one mtime before the importer ever ran. For those this returns
    created_at, exactly as before.
    """
    ids = [i for i in set(note_ids) if i]
    if not ids:
        return {}

    anchors: dict[str, str] = {}
    chunk = 500
    for i in range(0, len(ids), chunk):
        part = ids[i : i + chunk]
        qmarks = ",".join("?" * len(part))
        for n in db.fetchall(
            f"SELECT id, created_at, authored_at, frontmatter FROM notes "
            f"WHERE id IN ({qmarks})",
            tuple(part),
        ):
            created = n["created_at"]
            # authored_at is the typed column (migration 117); the frontmatter
            # arm stays for rows an agent writes that way, and is what 117
            # backfilled the column FROM.
            anchors[n["id"]] = (
                _clean_date(n.get("authored_at"), created)
                or _clean_date(_fm_original_date(n.get("frontmatter")), created)
                or created
            )
    return anchors


def _fm_original_date(frontmatter) -> str | None:
    """The legacy authorship stamp, before migration 117 gave it a column."""
    fm = _loads(frontmatter)
    raw = fm.get("original_date") if isinstance(fm, dict) else None
    return raw if isinstance(raw, str) else None


def _clean_date(raw, created_at: str | None) -> str | None:
    """An authorship candidate if it is a real date at or before ingestion."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        log.debug("inbox: ignoring unparseable authorship date %r", text[:40])
        return None
    # Authorship cannot postdate ingestion. Compare on the date prefix so a
    # date-only original_date is not judged against a same-day timestamp.
    if created_at and text[:10] > str(created_at)[:10]:
        log.debug("inbox: ignoring authorship date %r after created_at %r", text, created_at)
        return None
    return text


def _pull_todos(db, limit: int) -> list[dict]:
    """todos: status IN (open,doing), excluding stream-stub rows.

    kind: raw ingress → 'research' (firehose); user-curated saves (a scanner
    surfacing content the user actively saved, tagged via source_event_id) →
    'task' so they persist and surface; everything else → 'task'.
    importance: priority/5. source_trust: by source ('saved' tier for curated).
    """
    todos = db.fetchall(
        "SELECT id, title, priority, status, project, source, created_at, source_event_id "
        "FROM todos "
        "WHERE status IN ('open','doing') AND id NOT LIKE 'stream-stub-%' "
        "ORDER BY priority DESC, created_at DESC LIMIT ?",
        (limit,),
    )
    # Source-note anchors for notes-extract rows. A note maps to a todo whose
    # created_at is the INGESTION instant, so a bulk import (e.g. an Obsidian
    # sync) lands every note as "now" and floods the top of the inbox, burying
    # genuinely-recent items. _note_anchors moves the anchor to authorship
    # where a migration recorded one, and to notes.created_at otherwise.
    #
    # For the 449 bulk-imported vault notes it still does NOT reach authorship:
    # they carry no date and the vault had already lost the mtimes (295 of 413
    # files share 2026-03-19). For those the anchor remains ingestion time,
    # just an earlier ingestion than the todo's — strictly better than the
    # todo's own created_at, but do not read their rank as "how long ago
    # the owner wrote this".
    #
    # source_event_id is "notes-extract:<note_id>:<item_hash>" (note_id is the
    # middle segment; it is a UUID and carries no colon), or the legacy
    # "notes-extract:<note_id>". split(":")[1] recovers the note id from both.
    note_ids = {
        (t.get("source_event_id") or "").split(":")[1]
        for t in todos
        if (t.get("source_event_id") or "").startswith("notes-extract:")
    }
    note_created = _note_anchors(db, note_ids)

    out: list[dict] = []
    for t in todos:
        source = (t.get("source") or "").lower()
        sev = (t.get("source_event_id") or "")
        # Curated ingress carries a producer-declared event prefix. These are
        # user-intent signals, not raw firehose — each maps to its own kind so
        # they never share a surfacing cap. See _CURATED_KIND_BY_PREFIX.
        curated = _curated_kind(sev) if source == "ingress" else None
        if curated:
            kind = curated
            trust = scorer.source_trust(kind, kind)
        else:
            kind = "research" if source == "ingress" else "task"
            trust = scorer.source_trust(source or "agent", kind)
        importance = scorer.priority_to_importance(t.get("priority"))
        # Age anchor: the source note's authorship-or-creation date for
        # notes-extract rows (see _note_anchors), else the todo's own
        # created_at.
        anchor = t.get("created_at")
        if curated == "note" and sev.startswith("notes-extract:"):
            anchor = note_created.get(sev.split(":")[1]) or anchor
        out.append(_score(t["id"], "todos", kind, t.get("title") or "(untitled todo)",
                          importance, trust, anchor, t.get("project")))
    return out


def _pull_commitments(db, limit: int) -> list[dict]:
    """commitments: status='open' AND not expired. kind='commitment'.

    importance: a flat base of 0.7 — commitments are user-promised /
    agent-promised follow-ups, so they rank fairly high (above plain
    todos at default priority, below high-readiness continuations).
    They carry no per-row priority/urgency field, so a single documented
    base keeps the ranking stable. source='commitment' (trust 0.8).
    """
    rows = db.fetchall(
        "SELECT id, title, project, created_at "
        "FROM commitments "
        "WHERE status='open' AND expires_at > datetime('now') "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    out: list[dict] = []
    for c in rows:
        importance = 0.7  # documented base — see docstring.
        trust = scorer.source_trust("commitment", "commitment")
        out.append(_score(c["id"], "commitments", "commitment",
                          c.get("title") or "(commitment)",
                          importance, trust, c.get("created_at"), c.get("project")))
    return out


def _signal_project(evidence: dict | None) -> str | None:
    """Resolve an inbox row's project from a signal's evidence blob.

    Only ``source_kind='project'`` rows (stale_progress) carry a project slug
    in ``source_id``. Every other producer puts its own entity there — a mount
    path (sysinfo_disk), a gpu id (sysinfo_vram), a date bucket
    (session_failures), a note id (notes_extract) — so reading source_id as the
    project blanket-wide mislabels them and pollutes the project facet filter
    with paths and uuids.

    Prefer an explicit ``project`` key (stuck_todo, aging_thought and
    notes_extract all set one), fall back to source_id only where it really is
    a project, else no project at all.
    """
    if not isinstance(evidence, dict):
        return None
    project = evidence.get("project")
    if project:
        return str(project)
    if evidence.get("source_kind") == "project":
        source_id = evidence.get("source_id")
        return str(source_id) if source_id else None
    return None


def _pull_signals(db, limit: int) -> list[dict]:
    """signals: status='open'.

    kind: evidence.bucket='continuation' → 'continue'; else 'signal'.
    importance: continuation → readiness_percent/100; else severity map.
    source_trust: continue → 'continuation'; else signal source ('proactive'/'advisor').
    project: see _signal_project — NOT a blanket evidence.source_id read.

    Age anchor: note-derived signals anchor on the SOURCE NOTE (_note_anchors),
    everything else on the signal's own created_at. This pull used to pass
    created_at straight through with no note lookup at all — the mirror of the
    one _pull_todos already did — so a signal lifted from a note inherited the
    EXTRACTION instant. Measured 2026-07-26: 771 of 1097 active signal rows had
    a source note ≥30 days older than the signal, and the notes-extract backlog
    drains 5 notes per tick, so every future tick minted more.
    """
    sigs = db.fetchall(
        "SELECT id, summary, severity, evidence, source, source_ref, created_at "
        "FROM signals WHERE status='open' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    parsed = [(s, _loads(s.get("evidence"))) for s in sigs]
    note_anchor = _note_anchors(
        db,
        (signals_svc.note_id_of(s.get("source_ref"), ev) for s, ev in parsed),
    )

    out: list[dict] = []
    for s, evidence in parsed:
        bucket = evidence.get("bucket") if isinstance(evidence, dict) else None
        if bucket == "continuation":
            kind = "continue"
            importance = scorer.readiness_to_importance(evidence.get("readiness_percent"))
            # A continuation with no readiness still beats a stale info signal —
            # floor it on the severity map so it isn't zeroed out.
            if importance <= 0:
                importance = scorer.severity_to_importance(s.get("severity"))
            # Continuation signals all carry source='proactive', but a
            # continuation is an advisor-grade invitation — trust it as
            # 'continuation' (0.8), not raw proactive (0.6).
            trust = scorer.source_trust(None, "continue")
        else:
            kind = "signal"
            importance = scorer.severity_to_importance(s.get("severity"))
            trust = scorer.source_trust(s.get("source") or "proactive", kind)
        note_id = signals_svc.note_id_of(s.get("source_ref"), evidence)
        anchor = note_anchor.get(note_id or "") or s.get("created_at")
        out.append(_score(s["id"], "signals", kind, s.get("summary") or "(signal)",
                          importance, trust, anchor,
                          _signal_project(evidence)))
    return out


def _pull_reminders(db, limit: int) -> list[dict]:
    """reminders: status IN (pending,active,snoozed). kind='reminder'."""
    rems = db.fetchall(
        "SELECT id, what, urgency, when_due, created_at, source "
        "FROM reminders WHERE status IN ('pending','active','snoozed') "
        "ORDER BY urgency DESC, created_at DESC LIMIT ?",
        (limit,),
    )
    out: list[dict] = []
    for r in rems:
        importance = scorer.urgency_to_importance(r.get("urgency"))
        trust = scorer.source_trust(r.get("source") or "user", "reminder")
        # Anchor on when_due if set (urgency rises as due approaches in title
        # ranking via age), else created_at.
        anchor = r.get("when_due") or r.get("created_at")
        out.append(_score(r["id"], "reminders", "reminder", r.get("what") or "(reminder)",
                          importance, trust, anchor, None))
    return out


def _pull_suggestions(db, limit: int) -> list[dict]:
    """reminder_suggestions: accepted IS NULL.

    kind: source_type='progress' → 'continue'; else 'research'.
    importance: proposed_urgency/5.
    """
    sugs = db.fetchall(
        "SELECT id, proposed_what, proposed_urgency, source_type, created_at "
        "FROM reminder_suggestions WHERE accepted IS NULL "
        "ORDER BY proposed_urgency DESC, created_at DESC LIMIT ?",
        (limit,),
    )
    out: list[dict] = []
    for s in sugs:
        kind = "continue" if s.get("source_type") == "progress" else "research"
        importance = scorer.urgency_to_importance(s.get("proposed_urgency"))
        trust = scorer.source_trust(
            "continuation" if kind == "continue" else "agent", kind
        )
        out.append(_score(s["id"], "reminder_suggestions", kind,
                          s.get("proposed_what") or "(suggestion)",
                          importance, trust, s.get("created_at"), None))
    return out


def _pull_thoughts(db, limit: int) -> list[dict]:
    """thoughts: status='open' AND metadata.action_items present.

    kind: 'forgotten' if previously surfaced AND old (> 14d); else 'research'.
    importance: 0.5 (has action_items) — only actionable thoughts pulled.
    """
    ths = db.fetchall(
        "SELECT id, content, metadata, project, surface_count, created_at, last_surfaced "
        "FROM thoughts "
        "WHERE status='open' "
        "AND json_extract(metadata,'$.action_items') IS NOT NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    out: list[dict] = []
    for t in ths:
        meta = _loads(t.get("metadata"))
        action_items = meta.get("action_items") if isinstance(meta, dict) else None
        has_actions = bool(action_items)
        surfaced = (t.get("surface_count") or 0) > 0
        age = scorer.age_hours(t.get("created_at"))
        kind = "forgotten" if (surfaced and age > _FORGOTTEN_AGE_HOURS) else "research"
        importance = scorer.thought_importance(has_actions)
        trust = scorer.source_trust("thought", kind)
        title = (t.get("content") or "(thought)").strip().splitlines()[0][:200]
        out.append(_score(t["id"], "thoughts", kind, title,
                          importance, trust, t.get("created_at"), t.get("project")))
    return out


def _score(ref_id, ref_table, kind, title, importance, trust, anchor, project) -> dict:
    """Assemble a fully-scored row dict ready for upsert."""
    age = scorer.age_hours(anchor)
    sal = scorer.salience(importance, kind, trust, age)
    return {
        "ref_id": str(ref_id),
        "ref_table": ref_table,
        "kind": kind,
        "title": title,
        "project": project,
        "importance": importance,
        "type_weight": scorer.TYPE_WEIGHT.get(kind, 0.3),
        "source_trust": trust,
        "gravity": scorer.GRAVITY.get(kind, 1.0),
        "age_anchor_at": anchor,
        "salience": sal,
        "dedup_key": _dedup_key(title),
    }


def _loads(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


# ── hygiene ────────────────────────────────────────────────────────────


def purge_stale(db=None, grace_hours: int = 48) -> dict:
    """Delete terminal-state rows so the overlay + commitments tables don't
    grow unbounded.

    Removes (older than ``grace_hours``):
      - inbox rows in state IN ('superseded','expired') — dead, re-derivable
        by the reducer if their source ever reactivates. NOT 'dismissed' or
        'acted': those are user tombstones the upsert/supersede logic relies
        on to prevent resurrection.
      - commitments status='expired' — keyed on ``created_at`` (the table has
        no ``updated_at``, and ceiling-expired rows still hold a future
        ``expires_at``; 'expired' is terminal so created-age is the right grace).

    Deterministic, bounded, no LLM. Returns purge counts.
    """
    if db is None:
        db = get_db()
    cutoff = f"-{int(grace_hours)} hours"
    with db.write() as conn:
        inbox_n = (
            conn.execute(
                "DELETE FROM inbox WHERE state IN ('superseded','expired') "
                "AND updated_at <= datetime('now', ?)",
                (cutoff,),
            ).rowcount
            or 0
        )
        commit_n = (
            conn.execute(
                "DELETE FROM commitments WHERE status='expired' "
                "AND created_at <= datetime('now', ?)",
                (cutoff,),
            ).rowcount
            or 0
        )
    return {"inbox_purged": inbox_n, "commitments_purged": commit_n}
