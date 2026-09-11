# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Continuous work logging — upsert pattern with rich history.
# index: imports | def _ensure_project | def _infer_project_path | def _fetch_linked_memories | def _current_session_id | def _link_session_project | def _project_from_task | def resolve_write_project | def log_progress | def merged_history | def get_progress
# AGENT_HEADER_END -->
"""Continuous work logging — upsert pattern with rich history.

Ported from tm-launcher brain/progress.py.
"""

import json
import os
import uuid
from pathlib import Path
from okuro.db.engine import okuro_home

_HISTORY_CAP = 20
_UUID_LEN = 36
_UUID_DASHES = 4

# Compact-history rendering caps, in characters. Measured 2026-08-07 over all
# 45 projects carrying progress history: at 3 entries with these caps the
# rendered block costs a median of 142 tokens, p90 209, max 220 — against a
# 13000-token bootstrap packet that currently assembles to ~11.6k. Uncapped,
# the same block costs up to 5591 tokens (project `okuro`, 20 entries), which
# is 43% of the whole budget for one section priced at 150.
_COMPACT_SUMMARY = 140
_COMPACT_NEXT = 110


def _ensure_project(db, project: str):
    """Auto-create project entry if it doesn't exist (avoids FK violations).

    Auto-created rows are stamped ``provisional=1`` — they are a side effect of
    a memory/artifact/progress write referencing an unseen slug, not a curated
    subject. Canonical surfaces (project finder, charter eligibility) exclude
    provisional rows so the registry stays clean; the gated clustering step
    later promotes/merges them. This choke point is the single leak that fills
    the registry with ``unknown/*`` dupes — quarantine it here, once, for every
    caller (DP10). Tag values are left untouched: rerouting a slug rewrites
    memory tags and is a ratify-gated operation, not a silent one.
    """
    row = db.fetchone("SELECT 1 FROM projects WHERE id = ?", (project,))
    if row is None:
        path = _infer_project_path(project)
        db.execute(
            """INSERT OR IGNORE INTO projects (id, name, path, description, active, provisional)
               VALUES (?, ?, ?, ?, 1, 1)""",
            (project, project, path, "Auto-registered by agent"),
        )


def _infer_project_path(slug: str) -> str:
    """Infer the filesystem path for a project slug.

    Lookup order:
      1. ``OKURO_PROJECT_ROOTS`` env var (``:``-separated list of directory
         prefixes under $HOME, e.g. ``projects:tools``) — each is checked
         for ``<prefix>/<slug>``.
      2. ``get_convention("directories")`` — same thing but from the user's
         profile conventions.
      3. ``$HOME/<slug>`` — direct home-rooted project (e.g. ``~/okuro``).
      4. ``os.getcwd()/<slug>`` — cwd-rooted fallback.

    Returns ``f"unknown/{slug}"`` when nothing matches so downstream inserts
    don't FK-violate on a new, unknown project.
    """
    from okuro.yu.conventions import get_convention

    directories = get_convention("directories", {})
    root = Path.home()

    # Env-var override (useful during testing or non-conventional layouts)
    env_roots = [r for r in os.environ.get("OKURO_PROJECT_ROOTS", "").split(":") if r]
    for prefix_dir in env_roots:
        candidate = root / prefix_dir / slug
        if candidate.exists():
            return str(candidate)

    # Profile-declared prefixes
    for prefix_dir in directories.keys() if isinstance(directories, dict) else []:
        candidate = root / prefix_dir / slug
        if candidate.exists():
            return str(candidate)

    # Home-rooted project (~/<slug>)
    home_candidate = root / slug
    if home_candidate.exists() and home_candidate.is_dir():
        return str(home_candidate)

    # CWD-rooted fallback
    cwd_candidate = Path.cwd() / slug
    if cwd_candidate.exists() and cwd_candidate.is_dir():
        return str(cwd_candidate)

    return f"unknown/{slug}"


def _fetch_linked_memories(db, memory_keys: list) -> dict:
    """Fetch memory content for a list of keys. Accepts UUIDs or topic strings.

    Returns {key: truncated_content} for display in progress context.
    """
    if not memory_keys:
        return {}

    uuids, topics = [], []
    for key in memory_keys:
        if isinstance(key, str) and len(key) == _UUID_LEN and key.count("-") == _UUID_DASHES:
            uuids.append(key)
        else:
            topics.append(key)

    results = {}

    if uuids:
        placeholders = ",".join(["?"] * len(uuids))
        rows = db.fetchall(
            f"SELECT id, topic, project, substr(content, 1, 200) AS content "
            f"FROM agent_memory WHERE id IN ({placeholders})",
            tuple(uuids),
        )
        for r in rows:
            results[r["id"]] = r["content"] or ""

    if topics:
        placeholders = ",".join(["?"] * len(topics))
        rows = db.fetchall(
            f"SELECT topic, project, substr(content, 1, 200) AS content, "
            f"       MAX(created_at) AS last "
            f"FROM agent_memory WHERE topic IN ({placeholders}) "
            f"GROUP BY topic",
            tuple(topics),
        )
        for r in rows:
            results[r["topic"]] = r["content"] or ""

    return results


def _current_session_id() -> str | None:
    """This session's okuro session id, or None outside a bootstrapped session.

    Stamping it on the progress row closes the join that never existed: a
    history entry recorded status/summary/updated_at and nothing identifying
    who wrote it, so `session_history(project=X)` and `get_progress(X,
    include_history=True)` described the same work with no shared key. This
    fixes it FORWARD ONLY — rows written before migration 130 carry NULL and
    can never be linked exactly, only correlated by time.
    """
    try:
        from okuro.telemetry.logger import get_session_info
        sid = get_session_info().get("session_id")
    except Exception:
        return None
    # get_session_info returns the literal "unknown" outside a bootstrapped
    # session (a bare script, a cold import). Storing that string would make
    # every such row look linked while joining to nothing — worse than NULL,
    # because project_status would count it as attributable.
    if not sid or sid == "unknown":
        return None
    return sid


def _link_session_project(db, session_id: str | None, project: str) -> bool:
    """Fill `sessions.project` when this session never declared one.

    Measured 2026-08-07: 1487 of 3422 sessions over 30 days carry
    project=NULL. `sessions.project` is written once, at bootstrap, from
    `resolve_project(task_hint)`; a session whose hint never named a project
    keeps NULL forever — even after it logs progress against that project.
    That, not a lossy join, is why `session_history(project=X)` under-reports.

    ONLY fills NULL. `sessions` is an audit record (envelope classifies it
    `derived` for exactly this reason) and overwriting a project a session
    actually declared would falsify history. Completing a blank with the value
    the session itself just asserted is the opposite: it records what happened.

    This is also the "right slug at creation" half of the anti-staleness
    design — enveloping stays a repair tool because new rows land linked.
    """
    if not session_id:
        return False
    try:
        row = db.fetchone(
            "SELECT project FROM sessions WHERE session_id = ?", (session_id,)
        )
        if row is None or row["project"]:
            return False
        db.execute(
            "UPDATE sessions SET project = ? WHERE session_id = ? AND project IS NULL",
            (project, session_id),
        )
        return True
    except Exception:
        return False


def _project_from_task(task_id: str | None) -> str | None:
    """Map an orchestrator ``task_id`` → projects.id via its declared path.

    Reads ``project_path`` from the task's ``task.yaml`` and resolves it with
    the SHARED path→slug resolver rather than a third copy of that query —
    ``state_reader._resolve_project_slug`` already says in its own docstring
    that it "mirrors okuro.sense.commitments._resolve_project", and two copies
    of one lookup is how they drift.

    Tolerant by contract: a missing task, unreadable yaml, absent
    ``project_path``, or an unregistered path all yield None. Inheritance is a
    best-effort completion of a blank, never a reason to fail a write.
    """
    if not task_id:
        return None
    try:
        from okuro.sense.commitments import _resolve_project

        task_yaml = (
            okuro_home() / "orchestrator" / "tasks" / task_id / "task.yaml"
        )
        if not task_yaml.is_file():
            return None
        import yaml

        with open(task_yaml) as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return None
        from okuro.db import get_db

        return _resolve_project(get_db(), data.get("project_path"))
    except Exception:
        return None


def resolve_write_project(
    db, explicit: str | None = None, task_id: str | None = None
) -> str | None:
    """The project a new brain row should carry. Completes a blank, never guesses.

    Measured 2026-08-07: 2287 of 3421 artifacts (66.9%) carry no project, and
    2278 of those DO carry a ``task_id`` — the attribution existed at write
    time and was simply never read. Same shape in todos (1160/2106) and
    thoughts (511/1439). Untagged rows are invisible to every project-scoped
    surface, so the backlog is not cosmetic.

    This is the CREATION-side counterpart to ``_link_session_project``:
    that one completes a NULL ``sessions.project`` from a project the session
    asserts, this one completes a NULL row project from context the session
    already established. Both only ever fill a blank.

    Precedence, and why:
    1. ``explicit`` — the caller's own argument always wins. An agent that
       names a project has made a claim; inference must never override it.
    2. ``task_id`` → the orchestrator task's declared ``project_path``. This is
       evidence, not a guess: the human or the finalizing agent declared it.
    3. the current session's ``sessions.project``, itself set at bootstrap from
       ``resolve_project(task_hint)``.
    4. None. There is deliberately no similarity/prose fallback — a row filed
       under the WRONG project is harder to find than an untagged one, which
       is exactly why the envelope scrape job was rejected on 2026-08-07.

    Never raises: every source is wrapped, because failing a write to attribute
    it would trade a tagging gap for data loss.
    """
    if explicit:
        return explicit

    slug = _project_from_task(task_id)
    if slug:
        return slug

    try:
        sid = _current_session_id()
        if sid:
            row = db.fetchone(
                "SELECT project FROM sessions WHERE session_id = ?", (sid,)
            )
            if row and row["project"]:
                return row["project"]
    except Exception:
        return None
    return None


def log_progress(project: str, status: str = "implementing", summary: str = "",
                 agent: str = "unknown", files_touched: list = None,
                 next_steps: str = None, blockers: str = None,
                 branch: str = None, memory_keys: list = None,
                 phase: str = None) -> str:
    """Log work progress with upsert. History preserves full context per entry.

    Args:
        project: Project slug.
        status: exploring, implementing, testing, blocked, waiting_for_user.
        summary: One-line summary.
        agent: Agent identifier. When left as "unknown" (the default), the
            session's provider is resolved from the bootstrap marker and
            stamped automatically — prevents gemini/codex/cursor calls from
            collapsing onto a shared `(project, "unknown")` upsert key and
            overwriting each other silently.
        files_touched: List of files modified.
        next_steps: What to do next.
        blockers: What's blocking.
        branch: Git branch name.
        memory_keys: List of memory topics or UUIDs linked to this entry.
        phase: Optional key of a phase declared via `project_phases_set`.
            Referencing a `pending` phase promotes it to `active` — the
            derived half of the phase model, so position-in-plan updates as a
            side effect of a call the agent was already making. An undeclared
            key is stored, never rejected (~2700 sessions of existing callers
            pass nothing here and must keep working), and reported as
            `undeclared` by `project_status` rather than silently counted.
    """
    from okuro.db import get_db

    # Auto-stamp agent from session context when caller passed the default.
    # Explicit values (including non-default strings) are preserved verbatim.
    if not agent or agent == "unknown":
        try:
            from okuro.telemetry.logger import get_session_info
            provider = get_session_info().get("provider") or "unknown"
            if provider and provider != "unknown":
                agent = provider
        except Exception:
            # Telemetry unavailable (isolated tests, cold import) — keep "unknown".
            pass

    db = get_db()

    session_id = _current_session_id()

    existing = db.fetchone(
        "SELECT status, summary, files_touched, next_steps, blockers, branch, "
        "       updated_at, history, memory_keys, phase, session_id "
        "FROM progress WHERE project = ? AND agent = ?",
        (project, agent),
    )

    history = []
    if existing:
        history_entry = {
            "status": existing["status"],
            "summary": existing["summary"],
            "updated_at": existing["updated_at"],
        }
        # Carried into history so a past entry stays attributable and
        # placeable. Both are NULL on every pre-migration-130 row; that is a
        # permanent gap in the historical record, not something a join can
        # recover, and project_status labels it as such rather than guessing.
        if existing.get("phase"):
            history_entry["phase"] = existing["phase"]
        if existing.get("session_id"):
            history_entry["session_id"] = existing["session_id"]
        if existing.get("files_touched"):
            ft = existing["files_touched"]
            history_entry["files_touched"] = json.loads(ft) if isinstance(ft, str) else ft
        if existing.get("next_steps"):
            history_entry["next_steps"] = existing["next_steps"]
        if existing.get("memory_keys"):
            mk = existing["memory_keys"]
            history_entry["memory_keys"] = json.loads(mk) if isinstance(mk, str) else mk

        prev = json.loads(existing["history"]) if isinstance(existing["history"], str) else (existing["history"] or [])
        history = [history_entry] + prev
        history = history[:_HISTORY_CAP]

    files_json = json.dumps(files_touched or [])
    history_json = json.dumps(history)
    memory_keys_json = json.dumps(memory_keys or [])

    # A7 trigger requires a non-empty id. Previous INSERT omitted the
    # column entirely, so SQLite wrote "" for the TEXT PRIMARY KEY (valid
    # under UNIQUE but useless; caught by the 008 migration as 8 legacy
    # rows quarantined). New rows carry a UUID; the UPSERT path preserves
    # the original id because ON CONFLICT DO UPDATE never touches id.
    row_id = str(uuid.uuid4())

    # Project upsert + progress upsert together — FK from progress.project
    # to projects.id needs to be visible in the same transaction.
    phase_action = None
    linked_session = False
    with db.write():
        _ensure_project(db, project)
        db.execute(
            """INSERT INTO progress (id, project, agent, status, summary, files_touched,
                                     next_steps, blockers, branch, history, memory_keys,
                                     phase, session_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT (project, agent) DO UPDATE SET
                   status = excluded.status,
                   summary = excluded.summary,
                   files_touched = excluded.files_touched,
                   next_steps = excluded.next_steps,
                   blockers = excluded.blockers,
                   branch = excluded.branch,
                   history = excluded.history,
                   memory_keys = excluded.memory_keys,
                   phase = COALESCE(excluded.phase, progress.phase),
                   session_id = excluded.session_id,
                   updated_at = datetime('now')""",
            (row_id, project, agent, status, summary, files_json,
             next_steps, blockers, branch, history_json, memory_keys_json,
             phase, session_id),
        )
        if phase:
            from okuro.sense.phases import touch_phase
            phase_action = touch_phase(db, project, phase)
        linked_session = _link_session_project(db, session_id, project)

    msg = f"Progress logged: {project}/{agent} — {status}: {summary}"
    if phase:
        if phase_action == "undeclared":
            msg += (
                f" | phase '{phase}' is NOT declared for this project — stored, but "
                f"it counts toward nothing until project_phases_set declares it"
            )
        elif phase_action == "activated":
            msg += f" | phase '{phase}' → active"
        elif phase_action:
            msg += f" | phase '{phase}'"
    if linked_session:
        msg += " | session linked to project (was unset)"
    return msg


def _clip(text, limit: int) -> str:
    """Whitespace-collapsed, length-capped one-liner."""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def merged_history(project: str, limit: int = 10, db=None) -> list[dict]:
    """Every progress entry for a project, newest first, across ALL agents.

    ``progress`` is upserted per ``(project, agent)``, so a project worked on
    by claude-code and codex holds TWO current rows and two independent
    history stacks. Reading one row's history — which is what
    ``get_progress`` does — silently omits the other agent's work. This merges
    them: each row's current state plus its history, time-ordered.
    """
    if db is None:
        from okuro.db import get_db
        db = get_db()

    rows = db.fetchall(
        "SELECT agent, status, summary, next_steps, blockers, files_touched, "
        "       updated_at, history, phase, session_id "
        "FROM progress WHERE project = ?",
        (project,),
    )
    entries: list[dict] = []
    for row in rows:
        agent = row["agent"]
        entries.append({
            "agent": agent,
            "current": True,
            "status": row["status"],
            "summary": row["summary"],
            "next_steps": row["next_steps"],
            "blockers": row["blockers"],
            "updated_at": row["updated_at"],
            "phase": row.get("phase"),
            "session_id": row.get("session_id"),
        })
        raw = row["history"]
        hist = json.loads(raw) if isinstance(raw, str) else (raw or [])
        for e in hist:
            entries.append({
                "agent": agent,
                "current": False,
                "status": e.get("status"),
                "summary": e.get("summary"),
                "next_steps": e.get("next_steps"),
                "blockers": None,
                "updated_at": e.get("updated_at"),
                "phase": e.get("phase"),
                "session_id": e.get("session_id"),
            })
    entries.sort(key=lambda e: str(e.get("updated_at") or ""), reverse=True)
    return entries[:limit]


def get_progress(project: str, agent: str = None,
                 include_history: bool = False,
                 history_limit: int = None) -> str | None:
    """Get latest progress for a project.

    Args:
        project: Project slug.
        agent: Optional agent filter.
        include_history: Include full history — every entry, full text.
            Unchanged: this is what ~2700 sessions of callers already get.
        history_limit: Render at most N past entries, COMPACT (summary and
            next_steps clipped). Implies history without ``include_history``.
            Exists because the uncapped block costs up to 5591 tokens on a
            13000-token bootstrap budget, measured — a size no default caller
            can absorb. ``include_history=True`` still wins when both are
            passed, so no existing caller changes shape.
    """
    from okuro.db import get_db

    db = get_db()

    if agent:
        row = db.fetchone(
            "SELECT agent, status, summary, files_touched, next_steps, "
            "blockers, branch, updated_at, history, memory_keys, phase "
            "FROM progress WHERE project = ? AND agent = ?",
            (project, agent),
        )
    else:
        row = db.fetchone(
            "SELECT agent, status, summary, files_touched, next_steps, "
            "blockers, branch, updated_at, history, memory_keys, phase "
            "FROM progress WHERE project = ? ORDER BY updated_at DESC LIMIT 1",
            (project,),
        )

    if not row:
        return None

    lines = [f"**{project}** ({row['agent']}) — {row['status']}"]
    lines.append(f"Summary: {row['summary']}")

    files = row.get("files_touched")
    if files:
        fl = json.loads(files) if isinstance(files, str) else files
        if fl:
            lines.append(f"Files: {', '.join(fl)}")
    if row.get("next_steps"):
        lines.append(f"Next: {row['next_steps']}")
    if row.get("blockers"):
        lines.append(f"Blocked: {row['blockers']}")
    if row.get("branch"):
        lines.append(f"Branch: {row['branch']}")
    if row.get("phase"):
        lines.append(f"Phase: {row['phase']}")
    lines.append(f"Updated: {row.get('updated_at', 'unknown')}")

    mk_raw = row.get("memory_keys")
    memory_keys = json.loads(mk_raw) if isinstance(mk_raw, str) else (mk_raw or [])
    if memory_keys:
        linked = _fetch_linked_memories(db, memory_keys)
        if linked:
            lines.append("\n### Linked memories")
            for key, content in linked.items():
                lines.append(f"- **{key}**: {content}")

    if include_history or history_limit:
        history = json.loads(row["history"]) if isinstance(row["history"], str) else (row["history"] or [])
        # include_history keeps its exact legacy shape (all entries, full
        # text). history_limit alone selects the compact, budgeted rendering.
        compact = bool(history_limit) and not include_history
        if compact:
            history = history[:history_limit]
        if history:
            shown = len(history)
            header = "\n### Session history"
            if compact:
                header += f" (last {shown})"
            lines.append(header)
            for entry in history:
                ts = entry.get("updated_at", "?")
                if isinstance(ts, str) and len(ts) > 16:
                    ts = ts[:16].replace("T", " ")
                summary = entry.get("summary", "?")
                nxt = entry.get("next_steps")
                if compact:
                    summary = _clip(summary, _COMPACT_SUMMARY)
                    nxt = _clip(nxt, _COMPACT_NEXT) if nxt else None
                phase_tag = f" [{entry['phase']}]" if entry.get("phase") else ""
                lines.append(f"- {ts} — {entry.get('status', '?')}{phase_tag}: {summary}")
                if nxt:
                    lines.append(f"  Next: {nxt}")

    return "\n".join(lines)
