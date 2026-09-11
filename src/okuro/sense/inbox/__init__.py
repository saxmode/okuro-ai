# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.sense.inbox — unified inbox overlay (Phase 1: scorer + reducer + read API).
# index: imports | def inbox_list
# AGENT_HEADER_END -->
"""okuro.sense.inbox — unified Inbox overlay (Phase 1).

A read-only projection layer. The daemon ``inbox-reduce`` task calls
:func:`reduce_once` to project active producer rows (todos / signals /
reminders / reminder_suggestions / thoughts) into the ``inbox`` overlay
table, each scored by the pure :mod:`okuro.sense.inbox.scorer`. The
orchestrator serves the ranked list via :func:`inbox_list`.

Phase 1 is additive and non-destructive: producers and UI are untouched,
no disposition writes happen, and user-set ``state`` is always preserved
across reduce passes.
"""

from __future__ import annotations

import json
import logging

from okuro.db import get_db

from .reducer import reduce_once, surface_pass

log = logging.getLogger("okuro.sense.inbox")

__all__ = ["reduce_once", "surface_pass", "inbox_list", "inbox_dispose"]

_ACTIVE_STATES = ("new", "surfaced")
# "all" excludes terminal/disposed states — these are not "active inbox".
_ALL_EXCLUDED_STATES = ("dismissed", "superseded", "expired", "acted")
_LIMIT_CAP = 200

# action → target state for the disposition state machine.
_DISPOSE_ACTIONS = {"act": "acted", "defer": "snoozed", "dismiss": "dismissed"}


def inbox_list(
    state: str | None = "surfaced",
    kind: str | None = None,
    project: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return ranked inbox rows (salience DESC, then updated_at DESC).

    Args:
        state: gated/staging partitions over the inbox overlay —
            - ``"surfaced"`` (DEFAULT) → state='surfaced' only. The gated
              Inbox: rows that cleared the surfacing bar (see
              :func:`okuro.sense.inbox.reducer.surface_pass`).
            - ``"staging"`` → state='new'. Below the bar — not yet surfaced.
            - ``"active"`` → state IN ('new','surfaced'). Both partitions
              (back-compat with Phase 1/2).
            - ``"all"`` → not in ('dismissed','superseded','expired','acted')
              — every live row, terminal/disposed states excluded.
            - any other value → exact match on that single state.
        kind: optional exact kind filter (task/research/continue/...).
        project: optional exact project filter.
        limit: row cap (hard-capped at 200).
    """
    db = get_db()

    where: list[str] = []
    params: list = []

    if state == "active":
        placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        where.append(f"state IN ({placeholders})")
        params.extend(_ACTIVE_STATES)
    elif state == "staging":
        where.append("state = ?")
        params.append("new")
    elif state == "all":
        placeholders = ",".join("?" for _ in _ALL_EXCLUDED_STATES)
        where.append(f"state NOT IN ({placeholders})")
        params.extend(_ALL_EXCLUDED_STATES)
    elif state:
        # "surfaced" (default) and any explicit single state → exact match.
        where.append("state = ?")
        params.append(state)
    # state is None → no state filter

    if kind:
        where.append("kind = ?")
        params.append(kind)
    if project:
        where.append("project = ?")
        params.append(project)

    limit = max(1, min(int(limit), _LIMIT_CAP))

    sql = "SELECT * FROM inbox"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY salience DESC, updated_at DESC LIMIT ?"
    params.append(limit)

    rows = db.fetchall(sql, tuple(params))
    # Annotate each row with whether its source carries expandable content,
    # so the UI can show the expand affordance only when there's a body to
    # read, plus the tag block when its producer wrote one. Both come from the
    # one detail lookup already being made — no extra queries.
    # Bounded by the limit (≤cap), so the per-row lookup is cheap.
    for r in rows:
        detail = inbox_detail(r["ref_table"], r["ref_id"])
        body = detail.get("body")
        r["has_detail"] = bool(body and body.strip())
        r["tags"] = detail.get("tags")
    return rows


# (ref_table, action) → the producer status that disposition implies.
#
# Disposing an inbox row used to write ONLY the overlay, leaving the producer
# untouched: a signal the user dismissed stayed status='open' forever, so every
# other surface (todo_list, signal_list, the MCP tools, any agent asking "what
# is open?") still saw it as live work. Verified 2026-07-15 on the live DB —
# of 8 dispositions ever made, 2 signals sat 'open' after being acted on and
# dismissed. The overlay was the only thing that ever learned the answer.
#
# Only unambiguous transitions are mapped:
#   * `act` means "I am on it", NOT "it is finished" — todos → 'doing', never
#     'done'. commitments carry a literal 'acted' state, so they map exactly.
#     signals/thoughts have no state meaning "in progress" (a signal's
#     'promoted' means a todo was created from it, which dispose does not do),
#     so they are deliberately left alone rather than forced into a near-miss.
#   * `dismiss` is unambiguous everywhere — every producer has a
#     user-rejected state.
#   * `defer` is a UI-level snooze. The producer has not changed and must not
#     be touched — the row simply comes back later.
_PRODUCER_TRANSITION: dict[tuple[str, str], str] = {
    ("todos", "act"): "doing",
    ("todos", "dismiss"): "dropped",
    ("commitments", "act"): "acted",
    ("commitments", "dismiss"): "dismissed",
    ("signals", "dismiss"): "discarded",
    ("thoughts", "dismiss"): "dismissed",
    ("reminders", "dismiss"): "dismissed",
}

# Producer states that a disposition may overwrite. Anything already terminal
# (done, dropped, promoted, resolved…) is left as-is, so a disposition can
# never walk a finished row backwards, and a double-dispose is a no-op.
_PRODUCER_OPEN_STATES: dict[str, tuple[str, ...]] = {
    "todos": ("open", "doing"),
    "commitments": ("open",),
    "signals": ("open",),
    "thoughts": ("open",),
    "reminders": ("pending", "active", "snoozed"),
}


def log_impression(
    rows: list[dict],
    *,
    state: str | None = None,
    kind: str | None = None,
) -> None:
    """Record that the inbox was READ, and what it contained.

    Separates the two explanations for a low act-rate that the audit could not
    tell apart: "he saw it and ignored it" (the items are bad) versus "he never
    opened the inbox" (the items were never tested). `surfaced_at` cannot do
    this — it records when okuro put a row on the shelf, not when anyone looked.

    One row per FETCH, not per item: a list read is a single act of attention,
    and per-item rows would outnumber dispositions ~1000:1 for no extra insight.

    An API-level proxy, honestly bounded: it proves the list was requested and
    what came back, not that eyes crossed the screen. Silent on failure —
    telemetry must never break a page load.
    """
    try:
        db = get_db()
        ids = [r.get("id") for r in rows if r.get("id")]
        db.execute(
            "INSERT INTO inbox_impressions (state, kind, n_items, item_ids) "
            "VALUES (?, ?, ?, ?)",
            (state, kind, len(ids), json.dumps(ids)),
        )
    except Exception:  # noqa: BLE001
        log.debug("inbox impression log failed", exc_info=True)


def _log_disposition(db, row: dict, action: str) -> None:
    """Append the user's verdict to the permanent record.

    Every disposition is evidence, and okuro had been throwing it away: there
    was no disposition timestamp, and `inbox.updated_at` is rewritten by every
    reduce pass, so the audit could only say "8 dispositions ever, dates
    UNPROVEN". Append-only precisely because a mutable column is what failed.

    Scoring context is frozen here rather than joined later: the live row's
    salience decays and is rewritten on every pass, so "was the thing he acted
    on actually ranked highly?" is only answerable if the number is captured at
    click time.

    rank_in_view is computed against the surfaced set as it stands right now —
    a good-enough proxy for where the item sat on screen.
    """
    rank = None
    try:
        if row.get("state") == "surfaced" or action != "defer":
            r = db.fetchone(
                "SELECT count(*) + 1 AS rank FROM inbox "
                "WHERE state='surfaced' AND salience > ?",
                (row.get("salience") or 0.0,),
            )
            rank = r["rank"] if r else None
    except Exception:  # noqa: BLE001 — telemetry must never break a click
        rank = None

    age_hours = None
    try:
        anchor = row.get("age_anchor_at")
        if anchor:
            a = db.fetchone(
                "SELECT (julianday('now') - julianday(?)) * 24.0 AS h", (anchor,)
            )
            age_hours = round(a["h"], 2) if a and a["h"] is not None else None
    except Exception:  # noqa: BLE001
        age_hours = None

    db.execute(
        """INSERT INTO inbox_dispositions
           (item_id, ref_table, ref_id, kind, project, action,
            salience, importance, rank_in_view, surfaced_at, age_hours)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row.get("id"), row.get("ref_table"), row.get("ref_id"),
            row.get("kind"), row.get("project"), action,
            row.get("salience"), row.get("importance"), rank,
            row.get("surfaced_at"), age_hours,
        ),
    )


def _apply_producer_transition(db, ref_table: str, ref_id: str, action: str) -> str | None:
    """Propagate a disposition to the producer row. Returns the new status.

    Best-effort and guarded: unmapped (ref_table, action) pairs and rows that
    have already reached a terminal state are no-ops. reminder_suggestions is
    intentionally absent — it has no status column (accepted is a nullable
    int), and its accept/reject path lives in sense/reminders/suggestions.py.
    """
    new_status = _PRODUCER_TRANSITION.get((ref_table, action))
    if new_status is None:
        return None

    open_states = _PRODUCER_OPEN_STATES.get(ref_table)
    if not open_states:
        return None

    placeholders = ",".join("?" * len(open_states))
    db.execute(
        f"UPDATE {ref_table} SET status = ? "  # noqa: S608 — table name is from a fixed dict
        f"WHERE id = ? AND status IN ({placeholders})",
        (new_status, ref_id, *open_states),
    )
    return new_status


def engagement_report(days: int = 7) -> dict:
    """Is the inbox worth the user's attention? Answer from behaviour only.

    The pre-registered readout for the experiment opened on 2026-07-15. The
    baseline it is measured against, from the audit of the 43 days BEFORE the
    fixes (leaderboard gate, notes lane, priority repair, 48h age scale,
    producer retraction):

        corpus 1309 · ever acted 2 · ever disposed 8 (0.6%)
        25.5 items produced/day · 0.19 dispositions/day · 134:1

    Read it in this order — each question is only meaningful if the one above
    it passed:

      1. views_per_day — did he open it at all? If ~0, nothing else means
         anything: the ranking is untested, not vindicated.
      2. act_rate — of everything answered, how much was worth doing? This is
         the quality verdict. Mostly dismiss = the corpus is still wrong.
      3. by_kind — which producers earn their slot. Kill the losers with this,
         not with an opinion.
      4. median_hours_to_disposition — did items rot before he answered?

    Deliberately reports raw counts alongside every rate: at these volumes a
    single click swings a percentage, and a rate over n=3 is theatre.
    """
    db = get_db()
    since = f"-{int(days)} days"

    disp = db.fetchall(
        "SELECT action, kind, salience, rank_in_view, "
        "       (julianday(disposed_at) - julianday(surfaced_at)) * 24.0 AS hours_to "
        "  FROM inbox_dispositions WHERE disposed_at > datetime('now', ?)",
        (since,),
    )
    views = db.fetchone(
        "SELECT count(*) c FROM inbox_impressions WHERE viewed_at > datetime('now', ?)",
        (since,),
    )["c"]

    n = len(disp)
    acted = sum(1 for d in disp if d["action"] == "act")
    dismissed = sum(1 for d in disp if d["action"] == "dismiss")
    deferred = sum(1 for d in disp if d["action"] == "defer")

    by_kind: dict[str, dict] = {}
    for d in disp:
        k = by_kind.setdefault(d["kind"], {"act": 0, "dismiss": 0, "defer": 0})
        k[d["action"]] = k.get(d["action"], 0) + 1

    waits = sorted(d["hours_to"] for d in disp if d["hours_to"] is not None)
    median_wait = round(waits[len(waits) // 2], 1) if waits else None

    return {
        "window_days": days,
        "views": views,
        "views_per_day": round(views / days, 2) if days else 0,
        "dispositions": n,
        "dispositions_per_day": round(n / days, 2) if days else 0,
        "acted": acted,
        "dismissed": dismissed,
        "deferred": deferred,
        # None, not 0.0, when nothing was answered — an empty window is "no
        # data", not "0% good", and the difference decides the whole verdict.
        "act_rate": round(acted / n, 3) if n else None,
        "by_kind": by_kind,
        "median_hours_to_disposition": median_wait,
        "baseline_before_fixes": {
            "dispositions_per_day": 0.19,
            "ever_acted": 2,
            "disposition_rate": 0.006,
        },
    }


def inbox_dispose(
    item_id: str,
    action: str,
    snooze_until: str | None = None,
) -> dict:
    """Apply a user disposition to one inbox row AND its producer row.

    Actions:
      - ``act``     → state='acted'; set surfaced_at=now if still NULL.
                      Producer: todo → 'doing', commitment → 'acted'.
      - ``defer``   → state='snoozed'; snoozed_until = ``snooze_until`` or
                      (now + 1 day) in sqlite datetime format. Producer
                      untouched — a snooze is a UI decision, not a work
                      decision.
      - ``dismiss`` → state='dismissed'. Producer → its rejected state.

    The producer write is what makes a disposition mean something outside the
    overlay — see _PRODUCER_TRANSITION for why only some pairs are mapped.

    Raises:
        ValueError: if ``action`` is not one of act/defer/dismiss.

    Returns:
        The updated inbox row dict, or an empty dict ``{}`` if no row
        matched ``item_id`` (caller maps that to 404).
    """
    if action not in _DISPOSE_ACTIONS:
        raise ValueError(
            f"invalid action {action!r}; expected one of "
            f"{sorted(_DISPOSE_ACTIONS)}"
        )

    db = get_db()
    state = _DISPOSE_ACTIONS[action]

    if action == "act":
        sql = (
            "UPDATE inbox SET state='acted', "
            "surfaced_at=COALESCE(surfaced_at, datetime('now')), "
            "updated_at=datetime('now') WHERE id=?"
        )
        params: tuple = (item_id,)
    elif action == "defer":
        if snooze_until:
            sql = (
                "UPDATE inbox SET state='snoozed', snoozed_until=?, "
                "updated_at=datetime('now') WHERE id=?"
            )
            params = (snooze_until, item_id)
        else:
            sql = (
                "UPDATE inbox SET state='snoozed', "
                "snoozed_until=datetime('now','+1 day'), "
                "updated_at=datetime('now') WHERE id=?"
            )
            params = (item_id,)
    else:  # dismiss
        sql = (
            "UPDATE inbox SET state='dismissed', updated_at=datetime('now') "
            "WHERE id=?"
        )
        params = (item_id,)

    db.execute(sql, params)
    row = db.fetchone("SELECT * FROM inbox WHERE id=?", (item_id,))
    if row is None:
        return {}
    # Defensive: confirm the transition landed (it always should for a hit).
    assert row["state"] == state

    # Record the verdict permanently. Logged AFTER the update on purpose: the
    # disposed row has left the surfaced set, so rank_in_view counts exactly
    # the rows that outranked it, and `act` has already stamped surfaced_at.
    try:
        _log_disposition(db, dict(row), action)
    except Exception:  # noqa: BLE001 — telemetry must never break a click
        log.exception("inbox_dispose: disposition log failed for %s", item_id)

    # Propagate to the producer, so the disposition is true everywhere and not
    # just in the overlay. Best-effort by design: the user's click has already
    # landed, and failing to close a todo must never turn into a 500 that makes
    # the click look like it did nothing.
    try:
        _apply_producer_transition(db, row["ref_table"], row["ref_id"], action)
    except Exception:  # noqa: BLE001
        log.exception(
            "inbox_dispose: producer transition failed for %s/%s (%s) — "
            "overlay disposition stands",
            row["ref_table"], row["ref_id"], action,
        )
    return row


_TAG_KEYS = ("topic", "entities", "context", "rationale", "note_title", "note_id")


def _tag_block(blob: str | None) -> dict | None:
    """Lift the explain-yourself tag block out of a producer's JSON column.

    Producers hand the inbox a title and a ranking, which cannot answer "what
    is this about, where did it come from, and why is it on my list?". Those
    that can answer it write the block into their own JSON column —
    ``todos.context`` or ``signals.evidence`` — and this pulls it back out.

    Deliberately key-driven rather than producer-driven: any producer writing
    these keys gets rendered, with no allow-list to keep in sync. Returns None
    when a row carries none of them, so the UI branches on presence.
    """
    if not blob:
        return None
    try:
        data = json.loads(blob) if isinstance(blob, str) else blob
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    tags = {k: data[k] for k in _TAG_KEYS if data.get(k)}
    if not tags:
        return None

    confidence = data.get("confidence")
    if isinstance(confidence, (int, float)):
        tags["confidence"] = confidence
    return tags


def inbox_detail(ref_table: str, ref_id: str) -> dict:
    """Resolve the readable body of an inbox item's source row → markdown.

    The inbox overlay stores only title + ranking; the actual content lives
    in the producer table (todos.detail, thoughts.content, …). This switch
    normalizes each into a single markdown ``body`` so any inbox row can be
    expanded inline, regardless of kind. Unknown tables / missing rows return
    ``{"body": None}`` (the UI shows "no additional content").

    Rows whose producer wrote a tag block also carry ``tags`` (see
    :func:`_tag_block`). inbox_list already calls this once per row, so the
    widened SELECTs below add no extra queries.
    """
    db = get_db()

    def one(sql: str):
        return db.fetchone(sql, (ref_id,))

    if ref_table == "todos":
        r = one("SELECT detail, context FROM todos WHERE id=?")
        if not r:
            return {"body": None}
        return {"body": r["detail"], "tags": _tag_block(r["context"])}

    if ref_table == "thoughts":
        r = one("SELECT content FROM thoughts WHERE id=?")
        return {"body": (r["content"] if r else None)}

    if ref_table == "commitments":
        r = one("SELECT title, detail FROM commitments WHERE id=?")
        if not r:
            return {"body": None}
        return {"body": r["detail"] or r["title"] or None}

    if ref_table == "signals":
        r = one("SELECT summary, suggested_action, evidence FROM signals WHERE id=?")
        if not r:
            return {"body": None}
        body = r["summary"] or ""
        if r["suggested_action"]:
            body += f"\n\n**Suggested action:** {r['suggested_action']}"
        return {"body": body or None, "tags": _tag_block(r["evidence"])}

    if ref_table == "reminder_suggestions":
        r = one("SELECT proposed_what, proposed_when, reason FROM reminder_suggestions WHERE id=?")
        if not r:
            return {"body": None}
        parts = [r["proposed_what"] or ""]
        if r["proposed_when"]:
            parts.append(f"\n\n**When:** {r['proposed_when']}")
        if r["reason"]:
            parts.append(f"\n\n{r['reason']}")
        return {"body": "".join(parts) or None}

    return {"body": None}
