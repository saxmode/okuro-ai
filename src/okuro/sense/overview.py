# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: projects_overview — one compact row per project, the cross-project read that did not exist.
# index:
#   imports
#   def _compact
#   def _thin_staleness
#   def _clip
#   def _path_is_real
#   def _projects
#   def _phase_rollup
#   def _current_progress
#   def _todo_counts
#   def _brain_rollup
#   def projects_overview
# AGENT_HEADER_END -->
"""projects_overview — the cross-project read that did not exist.

``project_status`` answers "where does THIS project stand" in ~4 KB of JSON.
There was no way to ask it of every project at once, so nothing could render a
projects list: 95 sequential calls is not a UI, and the same absence made
charter/phase coverage unmeasurable by any typed tool.

TWO DESIGN CONSTRAINTS SHAPE EVERY QUERY BELOW.

COST MUST BE FLAT IN PROJECT COUNT. ``status._brain_activity`` probes five
tables per project — correct for one project, 475 queries for 95. Every lookup
here is instead ONE aggregate keyed by project, so the query count is fixed
(~10) whatever the registry size.

GIT IS ON THE DEFAULT PATH — BECAUSE IT WAS MEASURED, NOT ASSUMED. The design
started with ``include_repo=False``, reasoning from
``bootstrap/sections.py::build_progress``, which deliberately keeps a git
subprocess off the hot path. Measured warm over the live 97-project registry
that reasoning was wrong here: 12 ms without git, 80 ms with. 80 ms buys a
verdict corroborated by the repo instead of one guessing from brain rows
alone, so git is ON by default and ``include_repo=False`` remains for a caller
that genuinely needs the 12 ms path. A brain-only verdict is the degraded
answer, so it must be the one you opt into — and when you do, the payload says
so rather than leaving the omission to be inferred.

WHAT THIS IS NOT. It does not enumerate rows — ``project_inventory`` does that
per project, and calling it 95 times is exactly the cost this module exists to
avoid. Counts here are aggregates, not listings.
"""

from __future__ import annotations

from typing import Any

from okuro.sense.status import (
    _STALE_DAYS,
    _age_days,
    _brain_activity,
    _observed_path,
    _repo_activity,
    _staleness,
)

#: Rendering caps, in characters. next_steps gets the most room ON PURPOSE:
#: it is the RESUME line, the single field that answers "where was I" for a
#: reader returning to a project after working elsewhere. summary describes
#: what happened and is the cheaper of the two to lose.
_NEXT_CLIP = 180
_SUMMARY_CLIP = 100
_BLOCKER_CLIP = 120

#: Tables whose rows count as authored knowledge for this project, and whose
#: newest row proves the project is alive even when nobody logged progress.
#: Mirrors status._brain_activity's probe list so the two cannot disagree.
_BRAIN_TABLES = ("agent_memory", "artifacts", "todos", "thoughts", "notes")


def _compact(d: dict | None) -> dict | None:
    """Drop None-valued keys. Measured necessity, not tidiness.

    The first working version emitted every key on every row and cost 299
    tokens per project — a 38-row answer came to 11.4k tokens, 87% of the whole
    13k bootstrap budget, against a 200-token-per-project design target. Most
    of that was structure describing absence: six nulls for a project with no
    phase plan, four for one with no progress. An absent key already says
    "absent"; spelling it out 97 times says it 97 times.
    """
    if d is None:
        return None
    return {k: v for k, v in d.items() if v is not None}


def _thin_staleness(full: dict) -> dict:
    """Verdict + the ages it was computed from, WITHOUT the prose sentence.

    ``status._staleness`` writes a ~120-char explanation per project. Correct
    for project_status, which answers about ONE project; at 97x it was 13% of
    this payload to restate what the four numbers beside it already say. The
    numbers stay — "never let a declared field stand alone" is about the
    EVIDENCE being present, not about the sentence. ``threshold_days`` is a
    constant and moves to meta rather than repeating per row.
    """
    return _compact({
        "verdict": full.get("verdict"),
        "progress_age_days": full.get("progress_age_days"),
        "repo_age_days": full.get("repo_age_days"),
        "brain_age_days": full.get("newest_brain_row_age_days"),
    })


def _clip(text: Any, limit: int) -> str | None:
    """Trim to `limit`, marking the cut so a truncated field never reads whole."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _projects(db, active_only: bool) -> list[dict]:
    """The registry rows themselves. One query."""
    cols = {c["name"] for c in db.fetchall("PRAGMA table_info(projects)")}
    has = lambda c: c in cols  # noqa: E731 — schema guard, pre-093/131 stores
    select = [
        "id", "name",
        "kind" if has("kind") else "NULL AS kind",
        "path",
        "observes_path" if has("observes_path") else "NULL AS observes_path",
        "charter" if has("charter") else "NULL AS charter",
        "provisional" if has("provisional") else "0 AS provisional",
        "active",
    ]
    sql = f"SELECT {', '.join(select)} FROM projects"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    return db.fetchall(sql) or []


def _phase_rollup(db) -> dict[str, dict]:
    """Declared phase plans, rolled up per project. Two queries, not 2N.

    `done` is never derived — it is read here exactly as declared, which is why
    a project with no plan reports total 0 and percent None rather than 0%.
    A 0% bar and "no plan declared" mean different things and must not collapse.
    """
    out: dict[str, dict] = {}
    try:
        rows = db.fetchall(
            "SELECT project, state, COUNT(*) AS n FROM project_phases GROUP BY project, state"
        ) or []
    except Exception:
        return out
    for r in rows:
        slot = out.setdefault(
            r["project"],
            {"done": 0, "total": 0, "pending": 0, "active": 0, "dropped": 0,
             "active_key": None, "active_title": None, "next_pending": None},
        )
        state, n = r["state"], r["n"]
        if state in slot:
            slot[state] = n
        # `dropped` phases are declared out of scope, so they must not sit in
        # the denominator — otherwise abandoning a phase lowers percent_done.
        if state != "dropped":
            slot["total"] += n

    try:
        marks = db.fetchall(
            "SELECT project, key, title, state, seq FROM project_phases "
            "WHERE state IN ('active','pending') ORDER BY project, seq"
        ) or []
    except Exception:
        marks = []
    for m in marks:
        slot = out.get(m["project"])
        if slot is None:
            continue
        if m["state"] == "active" and slot["active_key"] is None:
            slot["active_key"] = m["key"]
            slot["active_title"] = m["title"]
        elif m["state"] == "pending" and slot["next_pending"] is None:
            slot["next_pending"] = m["key"]

    for slot in out.values():
        slot["percent_done"] = (
            round(100.0 * slot["done"] / slot["total"], 1) if slot["total"] else None
        )
    return out


def _current_progress(db) -> dict[str, dict]:
    """The newest progress row per project. One query.

    `progress` is upserted per (project, agent), so a project worked by two
    providers holds two current rows and two independent history stacks. The
    newest across agents is the honest answer to "what happened last here" —
    and the agent is carried so the reader can see WHOSE last word it is.
    """
    out: dict[str, dict] = {}
    try:
        rows = db.fetchall(
            "SELECT project, agent, status, summary, next_steps, blockers, "
            "       phase, updated_at "
            "FROM progress ORDER BY project, updated_at"
        ) or []
    except Exception:
        return out
    # Ascending order means the last write per project wins the slot.
    for r in rows:
        out[r["project"]] = {
            "status": r["status"],
            "agent": r["agent"],
            "phase": r["phase"] if "phase" in r.keys() else None,
            "updated_at": r["updated_at"],
            "age_days": _age_days(r["updated_at"]),
            "summary": _clip(r["summary"], _SUMMARY_CLIP),
            "next_steps": _clip(r["next_steps"], _NEXT_CLIP),
            "blockers": _clip(r["blockers"], _BLOCKER_CLIP),
        }
    return out


#: Stored `todos.priority` values that the UI shows as the top urgency bands.
#: HIGHER IS MORE URGENT — this is the direction every okuro consumer uses and
#: the one this module got WRONG on first write:
#:   inbox/scorer.py::priority_to_importance  ->  importance = p / 5.0
#:   inbox/reducer.py::_pull_todos            ->  ORDER BY priority DESC
#:   bootstrap/sections.py                    ->  {5:"P0", 4:"P1", ... 1:"P4"}
#: The display INVERTS the stored number, so reading a rendered "P1" and
#: inferring that 1 is urgent is exactly the trap. Never name a field after the
#: raw integer — name it after the meaning.
_URGENT_PRIORITIES = (5, 4)


def _todo_counts(db) -> dict[str, dict]:
    """Open/doing todo counts per project, the urgent band separated. One query.

    Counted apart because "25 open" and "25 open, 3 of them urgent" prompt
    different behaviour.

    THIS SHIPPED INVERTED on 2026-08-08 and is worth remembering: the field was
    called `p1_todos` and counted `priority = 1`, described in this docstring as
    "okuro's top priority". Stored 1 renders as **P4** — the LEAST urgent band —
    so the count reported the quietest rows as the loudest. It was caught only
    when the number looked wrong to the owner on a real project: 405 TikTok saves
    read as urgent when they are the opposite. Same class of error as the
    producer bug it exposed in an ingest script's _priority_from_score.
    """
    out: dict[str, dict] = {}
    placeholders = ", ".join("?" for _ in _URGENT_PRIORITIES)
    try:
        rows = db.fetchall(
            f"SELECT project, COUNT(*) AS n, "
            f"       SUM(CASE WHEN priority IN ({placeholders}) THEN 1 ELSE 0 END) AS urgent "
            f"FROM todos WHERE status IN ('open','doing') AND project IS NOT NULL "
            f"GROUP BY project",
            tuple(_URGENT_PRIORITIES),
        ) or []
    except Exception:
        return out
    for r in rows:
        out[r["project"]] = {"open_todos": r["n"], "urgent_todos": r["urgent"] or 0}
    return out


def _brain_rollup(db) -> dict[str, dict]:
    """Newest authored row + total row count per project. One query per table.

    Five queries total rather than five PER PROJECT — the whole reason this
    module exists rather than a loop over project_status.
    """
    out: dict[str, dict] = {}
    for table in _BRAIN_TABLES:
        try:
            rows = db.fetchall(
                f'SELECT project, MAX(created_at) AS newest, COUNT(*) AS n '
                f'FROM "{table}" WHERE project IS NOT NULL GROUP BY project'
            ) or []
        except Exception:
            continue
        for r in rows:
            slot = out.setdefault(
                r["project"], {"newest_at": None, "table": None, "brain_rows": 0}
            )
            slot["brain_rows"] += r["n"] or 0
            newest = r["newest"]
            if newest and (slot["newest_at"] is None or str(newest) > str(slot["newest_at"])):
                slot["newest_at"] = newest
                slot["table"] = table
    for slot in out.values():
        slot["age_days"] = _age_days(slot["newest_at"])
    return out


def _normalise_stamp(ts: Any) -> str | None:
    """SQLite-shaped UTC string for lexical comparison, or None.

    Two timestamp populations meet here and they are NOT string-comparable as
    they arrive — the same split `_age_days` documents at length. SQLite stamps
    are naive UTC (`2026-08-10 00:12:46`); git stamps are ISO with an offset
    (`2026-08-10T02:12:46+02:00`), which sorts as if it were two hours later
    than it is. Comparing them raw is how a commit made BEFORE your last visit
    reports as new.
    """
    if not ts:
        return None
    from datetime import datetime, timezone

    raw = str(ts).strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _delta(since: str | None, prog: dict | None, repo: dict, br: dict) -> dict | None:
    """Which of the three signals moved after `since`. None when none did.

    Returns None rather than an empty block on purpose: Zone 1 is a DIFF, and a
    project that did not move must be ABSENT from it, not present-and-empty.
    Same reason `_compact` drops None keys — structure describing absence is
    still structure the reader has to parse.
    """
    ref = _normalise_stamp(since)
    if ref is None:
        return None

    candidates = (
        ("progress", (prog or {}).get("updated_at")),
        ("repo", repo.get("last_commit_at")),
        ("brain", br.get("newest_at")),
    )

    moved: list[str] = []
    newest: str | None = None
    for name, stamp in candidates:
        norm = _normalise_stamp(stamp)
        if norm is None or norm <= ref:
            continue
        moved.append(name)
        if newest is None or norm > newest:
            newest = norm

    if not moved:
        return None
    # `moved` names WHICH signals moved, not just that something did: "the repo
    # moved but nobody logged it" and "an agent logged progress" are different
    # events and the ribbon orders on the difference.
    return {"moved": moved, "newest_at": newest, "signals": len(moved)}


def _path_is_real(path: Any) -> bool:
    """True only for a path that names an actual tree.

    ``unknown/<slug>`` is the placeholder auto-registration writes, and 52 of
    97 live rows carry one. Counting those as "path known" is how a coverage
    number comes out 96/97 when the true figure is 45/97 — the same
    ``unknown/`` prefix ``update_project``'s curation signal already tests
    before it lifts a row out of quarantine.
    """
    if not path:
        return False
    return not str(path).strip().startswith("unknown/")


def projects_overview(
    include_repo: bool = True,
    active_only: bool = True,
    touched_within_days: float | None = None,
    slugs: list[str] | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """One compact row per project — the read a projects list is built on.

    Args:
        include_repo: add ``repo_age_days`` from git HEAD and fold it into the
            verdict. ON by default — measured at 80 ms vs 12 ms over 97
            projects, which is worth a corroborated verdict. Pass False only
            when that 68 ms matters; the payload then states that the verdict
            came from brain activity alone.
        active_only: restrict to ``projects.active = 1``.
        touched_within_days: keep only projects whose newest signal (progress or
            brain row, plus repo when included) is within this many days. This
            is the filter that turns ~95 registered projects into the handful
            actually live, and it is applied AFTER ages are computed so the
            payload can report how many it removed.
        slugs: restrict to specific slugs. Skips the filters above.
        since: a timestamp to diff against. Rows whose progress, git HEAD or
            newest brain row moved AFTER it gain a ``delta`` block naming WHICH
            signals moved; rows that did not move carry no ``delta`` key at all.
            Off by default, so the payload every existing caller receives is
            byte-identical — the compaction work this module exists to protect
            is not worth spending on a field most callers never read.

    Returns a dict with ``projects`` (list of compact rows) and ``meta``
    stating what was counted, what was filtered, and what was NOT computed —
    an omission a caller cannot see is an omission it will misread.
    """
    from okuro.db import get_db

    db = get_db()

    rows = _projects(db, active_only=active_only and not slugs)
    if slugs:
        wanted = set(slugs)
        rows = [r for r in rows if r["id"] in wanted]

    phases = _phase_rollup(db)
    progress = _current_progress(db)
    todos = _todo_counts(db)
    brain = _brain_rollup(db)

    out: list[dict] = []
    for r in rows:
        slug = r["id"]
        prog = progress.get(slug)
        br = brain.get(slug, {"newest_at": None, "table": None, "age_days": None,
                              "brain_rows": 0})

        if include_repo:
            path, source = _observed_path(r)
            repo = _repo_activity(path, source)
        else:
            repo = {"age_days": None, "skipped": True}

        charter = r["charter"] if "charter" in r.keys() else None
        ph = phases.get(slug)

        row = {
            "slug": slug,
            "name": r["name"],
            "kind": r["kind"],
            "provisional": bool(r["provisional"]),
            "path_known": _path_is_real(r["path"]) or _path_is_real(r["observes_path"]),
            "has_charter": bool(charter and str(charter).strip()),
            "charter_chars": len(str(charter)) if charter else 0,
            "phases": _compact(ph) if ph else {"total": 0, "percent_done": None},
            "progress": _compact(prog),
            "counts": _compact({
                **todos.get(slug, {"open_todos": 0, "urgent_todos": 0}),
                "brain_rows": br["brain_rows"] or None,
            }),
            "staleness": _thin_staleness(
                _staleness(prog["age_days"] if prog else None, repo, br)
            ),
        }
        # Added ONLY when the project moved. Deliberately not `"delta": None` —
        # top-level row keys are otherwise emitted even when null (`kind`,
        # `progress`), so a caller reading `row["kind"]` still gets its None,
        # and only the new key follows the absent-means-absent rule. Setting it
        # unconditionally would also cost a key on all ~100 rows for a field
        # most callers never pass `since` to populate.
        delta = _delta(since, prog, repo, br)
        if delta is not None:
            row["delta"] = delta
        out.append(row)

    filtered_out = 0
    if touched_within_days is not None and not slugs:
        kept = []
        for row in out:
            # Read the keys `_thin_staleness` ACTUALLY emits. It renames
            # status._staleness's `newest_brain_row_age_days` to `brain_age_days`
            # on the way out, and this filter was still asking for the old name —
            # so `.get()` returned None and brain activity never counted as a
            # touch. Measured on the live registry: two projects were dropped
            # from the 14d default despite brain rows inside the window, because
            # neither has a progress row or a git path to corroborate it.
            # Silent, because a filter that drops a row looks identical to a row
            # that was never live.
            ages = [
                a for a in (
                    (row["progress"] or {}).get("age_days"),
                    row["staleness"].get("repo_age_days"),
                    row["staleness"].get("brain_age_days"),
                ) if a is not None
            ]
            if ages and min(ages) <= touched_within_days:
                kept.append(row)
            else:
                filtered_out += 1
        out = kept

    return {
        "projects": out,
        "meta": {
            "returned": len(out),
            "filtered_out_by_age": filtered_out,
            # Constant across rows, so it lives here instead of 97 times.
            "stale_threshold_days": _STALE_DAYS,
            "shape_note": (
                "Compact by design: None-valued keys are OMITTED and the "
                "staleness prose sentence is dropped — an absent key says "
                "absent, and the four ages say what the sentence said. Call "
                "project_status(slug) for one project's full detail."
            ),
            "touched_within_days": touched_within_days,
            "active_only": bool(active_only and not slugs),
            "repo_ages_included": include_repo,
            # Echoed so a caller can tell "you asked for a diff and nothing
            # moved" from "you never asked for one" — both yield zero `delta`
            # blocks and mean opposite things.
            "since": since,
            "moved": sum(1 for row in out if "delta" in row) if since else None,
            "note": (
                "repo_age_days is None for every row and the staleness verdict "
                "was computed from brain activity ALONE — pass include_repo=True "
                "to add git, at the cost of one subprocess per project."
                if not include_repo else
                "Verdicts include git HEAD age. One subprocess per project ran."
            ),
            "counting_note": (
                "Counts are aggregates, never enumerations — project_inventory "
                "lists rows for ONE project and calling it per project is the "
                "cost this read exists to avoid. progress is upserted per "
                "(project, agent); the row shown is the newest across agents, "
                "and `agent` says whose."
            ),
        },
    }
