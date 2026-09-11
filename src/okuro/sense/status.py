# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: project_status — one view of where a project stands, with staleness made visible.
# index:
#   imports
#   def _age_days
#   def _observed_path
#   def _repo_activity
#   def _brain_activity
#   def _sessions_for
#   def _staleness
#   def project_status
# AGENT_HEADER_END -->
"""project_status — one view of where a project stands, with staleness visible.

Assembled from existing tables. Nothing here is a new source of truth: the
charter comes from ``projects``, the work log from ``progress``, the counts
from ``project_inventory`` (called, not reimplemented), the plan from
``project_phases``, the sessions from ``sessions``.

THE PROBLEM THIS TOOL IS BUILT AROUND — a status view that lies is worse
than none. Every field here is therefore one of:

  DERIVED      computed from rows written for another reason (inventory
               counts, repo commit time, newest memory). Cannot go stale
               independently of the thing it measures.
  DECLARED     written by a human or agent on purpose (phase states, progress
               summaries). CAN go stale, so every one is reported next to its
               own age.
  MEASURED-GAP named explicitly where the data cannot answer the question,
               instead of being papered over with a lossy join.

The `staleness` block is not a footnote — it is the field that decides how
much the rest of the answer is worth. It compares the newest DECLARED signal
(progress) against the newest DERIVED ones (git HEAD, newest brain row). When
the repo moved and progress did not, the tool says the status is unverified
rather than reporting a confident stale sentence.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Days of drift before a declared status is called stale. Not a guess about
# how fast work moves — it is the gap at which "the repo moved and nobody
# said what changed" stops being normal same-week lag.
_STALE_DAYS = 3


def _age_days(ts: str | None) -> float | None:
    """Whole-ish days between an ISO/SQLite timestamp and now. None if unparsable.

    TWO TIMESTAMP POPULATIONS REACH THIS FUNCTION, AND THEY DISAGREE ABOUT
    TIMEZONE — that is the whole difficulty, and getting it wrong is how a
    fresh commit came to report a NEGATIVE age:

      SQLite stamps (`progress.updated_at`, newest brain row) come from
      `datetime('now')`, which is UTC and carries no offset. Naive == UTC.

      Git stamps (`repo_activity.last_commit_at`) are strict ISO WITH an
      offset, e.g. ``2026-08-08T03:32:54+02:00``.

    The previous implementation truncated with ``raw[: len(fmt) + 2]``, which
    for ``%Y-%m-%d %H:%M:%S`` is 19 characters — exactly the length of the
    datetime and NOT the offset. So ``+02:00`` was silently sliced off and a
    CEST local time was subtracted from a UTC now: a systematic -2h bias on
    every git-derived age, confirmed to the digit (repo_age_days -0.04 where
    the true age was +0.039). Because `_staleness` compares a progress age
    against these, the bias moved real verdicts, not just a displayed number.

    So: parse the offset when one is present and normalise to UTC; treat a
    naive stamp as UTC, which is what SQLite gives us. Clamping is deliberate —
    a future timestamp (clock skew, a commit stamped ahead) reports 0.0 rather
    than a negative age, because "negative days old" is not a thing a status
    view should ever render.
    """
    if not ts:
        return None

    raw = str(ts).strip()
    dt = None

    # fromisoformat handles both shapes, including the offset, on 3.11+.
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        probe = raw.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(probe[: len(fmt) + 2].strip(), fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None

    # An offset-bearing stamp is converted; a naive one IS UTC (SQLite).
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = datetime.now(timezone.utc) - dt
    return max(0.0, round(delta.total_seconds() / 86400.0, 2))


def _observed_path(project_row) -> tuple[str | None, str]:
    """Which tree's git history describes this project's work, and why.

    ``projects.path`` answers OWNERSHIP — "this project IS this tree" — and is
    one-to-one, enforced by the unique index in migration 131. Observation is
    many-to-one: a brain project like okuro-design-systems owns decisions and
    artifacts while its code lives inside another project's repo, so several
    projects may legitimately watch one tree. ``observes_path`` carries that
    second meaning; it is deliberately read HERE and nowhere else, so watching
    a tree never makes a project a cortex root (cortex/roots.py) and never
    captures that tree's sessions (sense/commitments.py).

    Falls back to ``path`` when unset, which is every pre-131 row.
    """
    if project_row is None:
        return None, "none"
    try:
        observed = project_row["observes_path"]
    except (KeyError, IndexError):
        # Pre-131 schema — the column simply is not there yet.
        observed = None
    if observed:
        return observed, "observes_path"
    return project_row["path"], "path"


def _repo_activity(path: str | None, source: str = "path") -> dict[str, Any]:
    """Last commit time + short subject at ``path``, when it is a git repo.

    This is the honest counterweight to a hand-written status: commits happen
    because work happened, not because someone remembered to report it.

    ``source`` names which column supplied the path, so a reader can tell an
    observed tree from an owned one without a second query.
    """
    out: dict[str, Any] = {"path": path, "source": source, "is_git": False}
    if not path:
        out["why"] = "project has no path registered"
        return out
    p = Path(path)
    if not p.exists():
        out["why"] = "registered path does not exist on this host"
        return out
    try:
        r = subprocess.run(
            ["git", "-C", str(p), "log", "-1", "--format=%cI%x00%s"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            out["why"] = "not a git repository"
            return out
        stamp, _, subject = r.stdout.strip().partition("\x00")
        out["is_git"] = True
        out["last_commit_at"] = stamp
        out["last_commit_subject"] = subject
        out["age_days"] = _age_days(stamp[:19])
    except Exception as exc:  # pragma: no cover — git absent / timeout
        out["why"] = f"git probe failed: {exc}"
    return out


def _brain_activity(db, slug: str) -> dict[str, Any]:
    """Newest authored row across the knowledge tables, whatever the table.

    Same role as repo activity: a memory or artifact written yesterday proves
    the project is live even when nobody logged progress.
    """
    newest: dict[str, Any] = {"newest_at": None, "table": None, "age_days": None}
    probes = (
        ("agent_memory", "created_at"),
        ("artifacts", "created_at"),
        ("todos", "created_at"),
        ("thoughts", "created_at"),
        ("notes", "created_at"),
    )
    for table, col in probes:
        try:
            row = db.fetchone(
                f'SELECT MAX("{col}") AS m FROM "{table}" WHERE project = ?', (slug,)
            )
        except Exception:
            continue
        val = row["m"] if row else None
        if val and (newest["newest_at"] is None or str(val) > str(newest["newest_at"])):
            newest = {"newest_at": val, "table": table, "age_days": _age_days(val)}
    return newest


def _sessions_for(db, slug: str, limit: int) -> dict[str, Any]:
    """Sessions carrying this slug, plus the linkage gap stated in numbers.

    THE GAP, measured 2026-08-07 and not hidden by this tool:

    `sessions.project` is written ONCE, at bootstrap, from
    `resolve_project(task_hint)` (bootstrap/assembler.py -> create_session).
    A session whose hint never named a project keeps NULL forever — 1487 of
    3422 sessions over 30 days. Separately, progress history entries carried
    no session id at all before migration 130, so there was never a key to
    join on: `session_history(project='okuro-design-systems')` returned 1 row
    while progress held 5 entries, and those two numbers were never joinable,
    only comparable.

    Two fixes, both forward-only, both visible here:
      - `progress.session_id` (migration 130) makes NEW entries exactly
        attributable.
      - `log_progress` fills `sessions.project` when it is NULL, so a session
        that logs work against a project stops being invisible to this query.

    Historical rows cannot be repaired — there is no evidence to repair them
    WITH. `linkage.unlinked_entries` counts them instead of silently dropping
    them, which is the whole difference between a gap and a lie.

    THE AVERAGE EXCLUDES ZERO-TOOL SESSIONS, and that is not a cosmetic
    filter. A session that called no tool never had the chance to comply, so
    its score describes nothing; averaging it in measures a different
    population rather than worse behaviour. Measured 2026-08-07: this project
    read 0.166 across seven sessions, six of which were zero-tool bootstrap
    probes left behind while building this very tool — the one real session
    scored 0.83. `memory_utility` had already learned this for the global
    baseline; the predicate now lives in one place and both callers use it.

    Every session is still LISTED, each carrying its own `counts_toward_average`.
    Dropping them from the list would hide that the probes exist; dropping them
    from the average is the whole point.
    """
    from .memory_utility import active_session_sql

    rows = db.fetchall(
        "SELECT session_id, provider, started_at, ended_at, task_hint, "
        "       compliance_score, compliance_normalized, end_reason, "
        f"      ({active_session_sql('sessions')}) AS did_something "
        "FROM sessions WHERE project = ? ORDER BY started_at DESC LIMIT ?",
        (slug, limit),
    )
    sessions = [
        {
            "session_id": r["session_id"],
            "handle": (r["session_id"] or "")[:8],
            "provider": r["provider"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "task_hint": r["task_hint"],
            "compliance_score": r["compliance_score"],
            "compliance_normalized": r["compliance_normalized"],
            "end_reason": r["end_reason"],
            "counts_toward_average": bool(r["did_something"]),
        }
        for r in rows
    ]
    scored = [s["compliance_normalized"] for s in sessions
              if s["compliance_normalized"] is not None and s["counts_toward_average"]]
    excluded = sum(1 for s in sessions if not s["counts_toward_average"])
    return {
        "sessions": sessions,
        "count": len(sessions),
        "counted_for_average": len(scored),
        "excluded_zero_tool": excluded,
        "avg_compliance_normalized": (
            round(sum(scored) / len(scored), 3) if scored else None
        ),
        "average_note": (
            f"{excluded} of {len(sessions)} listed sessions called no tool and are "
            "excluded from the average — a session that used no tool never had the "
            "chance to comply, so its score describes nothing."
        ) if excluded else None,
    }


def _staleness(progress_age: float | None, repo: dict, brain: dict) -> dict[str, Any]:
    """Verdict on whether the DECLARED status can still be trusted.

    Reports the comparison, not just the conclusion — a caller that disagrees
    with the 3-day threshold can still read the numbers it was computed from.
    """
    repo_age = repo.get("age_days")
    brain_age = brain.get("age_days")
    evidence_ages = [a for a in (repo_age, brain_age) if a is not None]
    newest_evidence = min(evidence_ages) if evidence_ages else None

    if progress_age is None:
        verdict = "no_progress"
        detail = (
            "Nothing has ever been logged with log_progress for this slug. "
            "Everything below is derived from other rows; there is no declared "
            "status to be stale."
        )
    elif newest_evidence is None:
        verdict = "unverifiable"
        detail = (
            f"Progress is {progress_age}d old. No independent activity signal "
            f"exists (no git repo at the registered path, no dated brain rows), "
            f"so nothing can confirm or contradict it."
        )
    elif progress_age - newest_evidence > _STALE_DAYS:
        verdict = "stale"
        detail = (
            f"Progress is {progress_age}d old but the project moved "
            f"{newest_evidence}d ago — a {round(progress_age - newest_evidence, 2)}d "
            f"gap. Work happened that nobody logged. Treat the declared status "
            f"and phase states as UNVERIFIED."
        )
    else:
        verdict = "current"
        detail = (
            f"Progress ({progress_age}d) is within {_STALE_DAYS}d of the newest "
            f"independent activity ({newest_evidence}d)."
        )
    return {
        "verdict": verdict,
        "detail": detail,
        "progress_age_days": progress_age,
        "repo_age_days": repo_age,
        "newest_brain_row_age_days": brain_age,
        "threshold_days": _STALE_DAYS,
    }


def project_status(
    slug: str,
    history_limit: int = 8,
    session_limit: int = 10,
    include_charter: bool = False,
) -> dict[str, Any]:
    """Where does this project stand. READ-ONLY, assembled from existing tables.

    Answers, in one call, what previously took `get_project` +
    `get_progress(include_history=True)` + `session_history` +
    `project_inventory` + `todo_list` — and answers the one question none of
    them could: how far along is it.
    """
    from okuro.db import get_db
    from okuro.sense.envelope import project_inventory
    from okuro.sense.phases import read_phases, phase_summary
    from okuro.sense.progress import merged_history

    db = get_db()

    project_row = db.fetchone(
        "SELECT id, name, path, observes_path, url, description, active, "
        "       provisional, kind, charter, design_profile, created_at, updated_at "
        "FROM projects WHERE id = ?",
        (slug,),
    )

    result: dict[str, Any] = {"slug": slug, "registered": bool(project_row)}

    if project_row:
        charter = project_row["charter"]
        result["project"] = {
            "id": project_row["id"],
            "name": project_row["name"],
            "path": project_row["path"],
            "observes_path": project_row["observes_path"],
            "url": project_row["url"],
            "description": project_row["description"],
            "active": bool(project_row["active"]),
            "provisional": bool(project_row["provisional"]),
            "kind": project_row["kind"],
            "design_profile": project_row["design_profile"],
            "has_charter": bool(charter),
            "charter_chars": len(charter) if charter else 0,
            "created_at": project_row["created_at"],
            "updated_at": project_row["updated_at"],
        }
        if include_charter and charter:
            result["project"]["charter"] = charter
    else:
        result["warning"] = (
            f"'{slug}' is not a registered project. Rows may still be tagged with "
            f"it — project_inventory('{slug}') shows what exists. Register with "
            f"project_envelope(slug, dry_run=False) or `okuro cortex add`."
        )

    # ---- phases: the declared plan, the queryable "how far along" ----------
    phases = read_phases(slug, db)
    # `plan` is the row list; `declared` is phase_summary's COUNT. They were
    # briefly both called "declared", and the int silently overwrote the list —
    # the tool reported a plan length and returned no plan.
    result["phases"] = {
        "plan": phases,
        **phase_summary(phases),
    }
    if not phases:
        result["phases"]["hint"] = (
            "No phase plan declared. Without one, 'how far along' is only "
            "answerable by reading prose. Declare with project_phases_set("
            f"'{slug}', phases=[{{'key':'p1','title':'...'}}, ...])."
        )

    # ---- current progress row + merged multi-agent history ----------------
    cur = db.fetchone(
        "SELECT agent, status, summary, next_steps, blockers, branch, "
        "       files_touched, phase, session_id, updated_at, started_at "
        "FROM progress WHERE project = ? ORDER BY updated_at DESC LIMIT 1",
        (slug,),
    )
    progress_age = None
    if cur:
        import json as _json
        ft = cur["files_touched"]
        files = _json.loads(ft) if isinstance(ft, str) else (ft or [])
        progress_age = _age_days(cur["updated_at"])
        result["current"] = {
            "agent": cur["agent"],
            "status": cur["status"],
            "summary": cur["summary"],
            "next_steps": cur["next_steps"],
            "blockers": cur["blockers"],
            "branch": cur["branch"],
            "files_touched": files,
            "phase": cur["phase"],
            "session_id": cur["session_id"],
            "updated_at": cur["updated_at"],
            "age_days": progress_age,
        }
    else:
        result["current"] = None

    history = merged_history(slug, limit=history_limit, db=db)
    result["history"] = history
    agents = sorted({e["agent"] for e in history if e.get("agent")})
    result["history_meta"] = {
        "entries": len(history),
        "agents": agents,
        "note": (
            "Merged across ALL agents. progress is upserted per (project, agent), "
            "so a project worked by two providers holds two independent history "
            "stacks — get_progress reads only one of them."
        ) if len(agents) > 1 else None,
    }

    # ---- inventory: reuse project_inventory's own classification ----------
    # limit_per_table=1 / excerpt_chars=1 collapse the ROW LISTING only. The
    # counts come from separate COUNT(*) queries (envelope.py, project_inventory)
    # and are exact regardless of the listing limit — this is reuse of that
    # classification, not a reimplementation of it.
    inv = project_inventory(
        slug, include_superseded=False, limit_per_table=1, excerpt_chars=1,
    )
    result["inventory"] = {
        "counts": inv.get("counts"),
        # `listed`/`truncated` describe the ROW LISTING, which was collapsed to
        # 1 row per table on purpose. Passing them through would report every
        # table as truncated and read as a data loss that did not happen.
        "per_table": {
            t: {k: v for k, v in d.items() if k not in ("listed", "truncated")}
            for t, d in (inv.get("per_table") or {}).items()
        },
        "unclassified_warning": inv.get("unclassified_warning"),
    }

    # ---- todos bound to the slug ------------------------------------------
    todo_rows = db.fetchall(
        "SELECT id, title, status, priority, created_at, due_at "
        "FROM todos WHERE project = ? AND status IN ('open', 'doing') "
        "ORDER BY priority, created_at DESC LIMIT 25",
        (slug,),
    )
    result["open_todos"] = [
        {
            "handle": (r["id"] or "")[:8],
            "title": r["title"],
            "status": r["status"],
            "priority": r["priority"],
            "due_at": r["due_at"],
            "age_days": _age_days(r["created_at"]),
        }
        for r in todo_rows
    ]

    # ---- decisions: recent, with the honest caveat ------------------------
    dec_rows = db.fetchall(
        "SELECT id, substr(content, 1, 120) AS excerpt, created_at, confidence "
        "FROM agent_memory WHERE project = ? AND topic = 'decision' "
        "AND (confidence IS NULL OR confidence > 0.1) "
        "ORDER BY created_at DESC LIMIT 8",
        (slug,),
    )
    result["recent_decisions"] = {
        "rows": [
            {
                "handle": (r["id"] or "")[:8],
                "excerpt": " ".join((r["excerpt"] or "").split()),
                "created_at": r["created_at"],
            }
            for r in dec_rows
        ],
        "note": (
            "agent_memory has no open/closed state — a decision row records that a "
            "decision was MADE, never that one is pending. There is no 'open "
            "decisions' count to give. Pending work with an owner lives in todos "
            "(open_todos above); an undecided question logged as a memory is "
            "indistinguishable from a settled one by schema."
        ),
    }

    # ---- sessions + the linkage gap ---------------------------------------
    sess = _sessions_for(db, slug, session_limit)
    unlinked = sum(1 for e in history if not e.get("session_id"))
    sess["linkage"] = {
        "history_entries": len(history),
        "unlinked_entries": unlinked,
        "note": (
            "progress entries written before migration 130 carry no session_id and "
            "can never be linked exactly — only correlated by time. sessions.project "
            "is set once at bootstrap from the task hint (1487 of 3422 sessions over "
            "30 days measured NULL on 2026-08-07), so session count and progress "
            "count describe overlapping work and are NOT expected to match. "
            "log_progress now fills a NULL sessions.project and stamps "
            "progress.session_id, so new rows link exactly."
        ),
    }
    result["sessions"] = sess

    # ---- staleness: the field that prices everything above ----------------
    repo = _repo_activity(*_observed_path(project_row))
    brain = _brain_activity(db, slug)
    result["repo_activity"] = repo
    result["brain_activity"] = brain
    result["staleness"] = _staleness(progress_age, repo, brain)

    return result
