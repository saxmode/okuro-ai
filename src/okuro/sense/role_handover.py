# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role-handover storage — Stream A (agent-to-agent subtask handover).
# index: imports | def _embed_text | def _vec_bytes | def _row_to_dict |
#   def write_role_handover | def read_role_handover | def list_role_handovers |
#   def supersede_role_handover | def read_declared_project_path |
#   def _emit_kg_edges | _BRIEF_REQUIRED
# AGENT_HEADER_END -->
"""Role-handover storage — Stream A (agent-to-agent subtask handover).

Replaces the dispatcher's "first 40 lines of dep .md" subagent handover
with a structured row + KG mirror + cortex refs. Agent-optimized: refs
only, no inlined source. Stream B (user-facing report) lives in
``artifacts`` (kind='report').

Schema lives in migration ``035_role_handovers.sql``.

Hard rules (HR1-HR13 in convention memory). Validators V1-V8 in
``okuro.sense.mcp_middleware.pre_tool_call`` reject violations BEFORE
the DB insert; this module trusts the inputs it receives.

Brief convention — ``project_path``
-----------------------------------
When a subagent realizes work in a project directory (scaffolds a webapp,
builds a service, edits a repo), it MUST set ``brief["project_path"]`` to
the absolute path of that project root. The orchestrator engine reads the
freshest non-empty ``project_path`` across all handovers for a task on
finalize and persists it to ``task.yaml``; this powers the preview
"See result" feature without heuristic inference. Subagents that do not
realize work in a project (research, analysis, doc-only) leave the field
unset.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)


_ALLOWED_STATUS = {"success", "partial", "failed"}
_BRIEF_REQUIRED = {"summary", "outcome"}
_EMBED_BODY_PREFIX_CHARS = 500


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _embed_text(brief: dict, cortex_refs: list[dict]) -> str:
    """Compose the text used for the vector embedding.

    Concentrates the relevance signal: summary + first decision rationales
    + cortex-ref purposes. Skips the rest of the JSON to keep similarity
    sharp on the brief's intent.
    """
    parts: list[str] = []
    summary = (brief.get("summary") or "").strip()
    if summary:
        parts.append(summary[:_EMBED_BODY_PREFIX_CHARS])
    decisions = brief.get("decisions") or []
    for d in decisions[:3]:
        if isinstance(d, dict):
            chunk = " — ".join(
                str(d.get(k, "")).strip()
                for k in ("decision", "rationale")
                if d.get(k)
            )
            if chunk:
                parts.append(chunk)
    purposes = [
        str(r.get("purpose", "")).strip()
        for r in (cortex_refs or [])
        if isinstance(r, dict) and r.get("purpose")
    ]
    if purposes:
        parts.append(" / ".join(purposes[:5]))
    return "\n\n".join(parts)


def _vec_bytes(text: str) -> bytes | None:
    try:
        from okuro.embed.client import embed_one, to_bytes
        return to_bytes(embed_one(text))
    except Exception:
        return None


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in ("brief", "cortex_refs", "kg_edges", "artifact_refs", "memory_refs"):
        val = out.get(col)
        if isinstance(val, str):
            try:
                out[col] = json.loads(val) if val else (
                    {} if col == "brief" else []
                )
            except (TypeError, ValueError):
                out[col] = {} if col == "brief" else []
    return out


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def write_role_handover(
    *,
    subtask_id: str,
    from_role: str,
    brief: dict,
    task_id: str | None = None,
    to_subtask_id: str | None = None,
    to_role: str | None = None,
    cortex_refs: list[dict] | None = None,
    kg_edges: list[dict] | None = None,
    artifact_refs: list[str] | None = None,
    memory_refs: list[str] | None = None,
    supersedes: str | None = None,
    confidence: float = 0.8,
    created_by: str | None = None,
    handover_id: str | None = None,
    project: str | None = None,
    dispatch_epoch: str | None = None,
) -> str:
    """Insert a role-handover row + mirror lineage edges into kg_triples.

    Returns the handover id on success, or a ``REJECTED: …`` string on a
    storage-layer guard miss. Schema validators (V1-V8) live in the MCP
    middleware and run BEFORE this function — the rejections here are
    only a defence-in-depth backstop for direct callers.

    ``dispatch_epoch`` (C12) — ISO timestamp of the dispatch generation
    the writer believes it belongs to. When supplied, the row is
    silently superseded on insert (confidence=0.1) if a supersede sweep
    already ran for the same ``(task_id, subtask_id)`` after that
    epoch. Mirrors :func:`okuro.sense.artifacts.artifact_write` so the
    V7 race closes uniformly across both brain surfaces — a straggler
    MCP write from a prior retry can no longer reach the next reviewer
    pass as current truth.
    """
    if not subtask_id or not subtask_id.strip():
        return "REJECTED: subtask_id is required (non-empty)."
    if not from_role or not from_role.strip():
        return "REJECTED: from_role is required (non-empty)."
    if not isinstance(brief, dict) or not brief:
        return "REJECTED: brief must be a non-empty object."
    missing = _BRIEF_REQUIRED - set(brief.keys())
    if missing:
        return f"REJECTED: brief is missing required keys: {sorted(missing)}."
    outcome = brief.get("outcome")
    if outcome not in _ALLOWED_STATUS:
        return (
            f"REJECTED: brief.outcome={outcome!r} must be one of "
            f"{sorted(_ALLOWED_STATUS)}."
        )

    from okuro.db import get_db

    db = get_db()
    hid = handover_id or str(uuid.uuid4())
    refs = list(cortex_refs or [])
    edges = list(kg_edges or [])
    arts = list(artifact_refs or [])
    mems = list(memory_refs or [])

    embed_input = _embed_text(brief, refs)
    vec_bytes = _vec_bytes(embed_input) if embed_input else None
    resolved_task_id = task_id or ""

    with db.write():
        if project:
            from okuro.sense.progress import _ensure_project
            _ensure_project(db, project)

        if supersedes:
            db.execute(
                "UPDATE role_handovers SET confidence = 0.1 WHERE id = ?",
                (supersedes,),
            )

        # C12 — write-epoch fence. If a supersede sweep already ran for
        # this (task_id, subtask_id) AFTER the writer's declared epoch,
        # the writer is a straggler from a prior dispatch generation;
        # land its row at confidence=0.1 so list_role_handovers's
        # ``include_superseded=False`` default skips it. Reads the max
        # ``consumed_at`` across already-superseded rows — that column
        # is bumped by ``supersede_role_handovers_for_subtask`` every
        # time the engine advances the dispatch epoch for this slot.
        # ``created_at`` would NOT work because the supersede sweep
        # updates an existing row in place — the row's creation
        # timestamp predates the epoch advance.
        if (dispatch_epoch and resolved_task_id and subtask_id):
            try:
                sweep_row = db.fetchone(
                    """SELECT MAX(consumed_at) AS ts FROM role_handovers
                           WHERE task_id = ? AND subtask_id = ?
                             AND confidence <= 0.1""",
                    (resolved_task_id, subtask_id),
                )
                sweep_ts = (
                    sweep_row["ts"] if isinstance(sweep_row, dict)
                    else (sweep_row[0] if sweep_row else None)
                ) if sweep_row else None
                if sweep_ts and sweep_ts > dispatch_epoch:
                    confidence = 0.1
            except Exception:
                # Defence in depth — failing the watermark probe must
                # not block the primary insert. Worst case is the
                # straggler lands at full confidence (original V7
                # behaviour) and gets superseded on the next retry
                # cycle, not an integrity regression.
                pass

        db.execute(
            """INSERT INTO role_handovers (
                    id, task_id, subtask_id, from_role, to_subtask_id, to_role,
                    brief, cortex_refs, kg_edges, artifact_refs, memory_refs,
                    supersedes, status, confidence, created_by
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hid, resolved_task_id, subtask_id, from_role, to_subtask_id, to_role,
                json.dumps(brief), json.dumps(refs), json.dumps(edges),
                json.dumps(arts), json.dumps(mems),
                supersedes, outcome, confidence, created_by,
            ),
        )

        if vec_bytes is not None:
            try:
                # No partition value: vec_role_handovers is unpartitioned as of
                # 2026-08-13. task_id mints a fresh partition per orchestrator
                # task, which cost 266 preallocated chunks for 942 vectors
                # (~1064 MiB for ~3.7 MB of data). Passing it now would only
                # take vec_write's "no column" fallback on every single write.
                from okuro.sense.retrieval import vec_write
                vec_write(db, "vec_role_handovers", hid, vec_bytes)
            except Exception:
                pass

        # KG mirror — synchronous in same write block. R3: lineage must be
        # queryable the instant the handover row exists. _emit_kg_edges
        # must never raise (best-effort) so a KG hiccup cannot block the
        # primary insert from committing.
        try:
            _emit_kg_edges(
                db,
                handover_id=hid,
                task_id=resolved_task_id,
                subtask_id=subtask_id,
                from_role=from_role,
                to_subtask_id=to_subtask_id,
                to_role=to_role,
                user_edges=edges,
                artifact_refs=arts,
                memory_refs=mems,
                cortex_refs=refs,
                supersedes=supersedes,
                project=project,
                confidence=confidence,
            )
        except Exception as exc:
            log.warning("role_handover %s: kg edge emit failed: %s", hid, exc)

    return hid


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def read_role_handover(
    subtask_id: str | None = None,
    *,
    handover_id: str | None = None,
    task_id: str | None = None,
    include_superseded: bool = False,
) -> dict | None:
    """Fetch the freshest handover for a subtask (or by explicit id).

    Returns None if no row matches. Default skips superseded rows
    (confidence=0.1). Pass include_superseded=True to inspect history.
    """
    from okuro.db import get_db

    db = get_db()
    if handover_id:
        row = db.fetchone(
            "SELECT * FROM role_handovers WHERE id = ?", (handover_id,)
        )
        return _row_to_dict(row)

    if not subtask_id:
        return None

    sql = "SELECT * FROM role_handovers WHERE subtask_id = ?"
    params: list[Any] = [subtask_id]
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    if not include_superseded:
        sql += " AND confidence > 0.1"
    sql += " ORDER BY created_at DESC LIMIT 1"
    return _row_to_dict(db.fetchone(sql, tuple(params)))


def list_role_handovers(
    *,
    task_id: str | None = None,
    subtask_id: str | None = None,
    from_role: str | None = None,
    status: str | None = None,
    limit: int = 50,
    include_superseded: bool = False,
) -> list[dict]:
    """List handovers with filters. Newest first.

    Default skips superseded rows. Body fields (brief, refs) are returned
    in full — handovers are small structured payloads, not artifact bodies.
    """
    from okuro.db import get_db

    db = get_db()
    sql = "SELECT * FROM role_handovers WHERE 1 = 1"
    params: list[Any] = []
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    if subtask_id:
        sql += " AND subtask_id = ?"
        params.append(subtask_id)
    if from_role:
        sql += " AND from_role = ?"
        params.append(from_role)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if not include_superseded:
        sql += " AND confidence > 0.1"
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    rows = db.fetchall(sql, tuple(params))
    return [r for r in (_row_to_dict(row) for row in rows) if r is not None]


def rank_role_handovers_by_relevance(
    *,
    task_id: str,
    query: str,
    limit: int = 8,
    include_superseded: bool = False,
) -> list[dict]:
    """Return the task's role-handovers ranked by relevance to ``query``.

    Embeds ``query`` and vec-searches ``vec_role_handovers``, then joins to
    the active rows for ``task_id`` (most-similar first). Used by the
    continuation decomposer to inline only the top-K handovers that matter
    for the new instructions instead of the full accumulated dump.

    Best-effort: if embeddings or the vec table are unavailable, returns an
    empty list so the caller can fall back to a most-recent-K slice (still
    bounded). Never raises into the caller.
    """
    if not task_id or not query or not query.strip():
        return []
    vec = _vec_bytes(query)
    if vec is None:
        return []
    try:
        from okuro.db import get_db
        from okuro.sense.retrieval import exhaustive_pool, scoped_vec_search
        db = get_db()
        # EXHAUSTIVE, not candidate_pool — and the difference was measured, not
        # reasoned about. The table was unpartitioned on 2026-08-13 (task_id
        # minted a partition per task: 266 chunks for 942 vectors), so the scan
        # is global and the scoping happens below, against
        # list_role_handovers(task_id=…). That post-filter keeps the answer
        # CORRECT — every row returned really is this task's — but it cannot
        # keep it COMPLETE: with a 64-row pool over 942 vectors the default
        # limit=8 returned ONE of the eight handovers available.
        #
        # At 942 vectors the fix is to stop narrowing: ask for every row and
        # let the filter scope it. sqlite-vec brute-forces the scan either way.
        matches = scoped_vec_search(
            db, "vec_role_handovers", vec,
            limit=exhaustive_pool(db, "vec_role_handovers", limit),
        )
    except Exception:
        return []
    if not matches:
        return []

    rank = {m["id"]: i for i, m in enumerate(matches)}
    rows = list_role_handovers(
        task_id=task_id,
        limit=500,
        include_superseded=include_superseded,
    )
    ranked = [r for r in rows if r.get("id") in rank]
    ranked.sort(key=lambda r: rank.get(r.get("id"), 1 << 30))
    return ranked[:limit]


# ---------------------------------------------------------------------------
# supersede
# ---------------------------------------------------------------------------


def supersede_role_handovers_for_subtask(task_id: str, subtask_id: str) -> int:
    """Mark every active role_handover for ``(task_id, subtask_id)`` as
    superseded — confidence drops to 0.1, list_role_handovers excludes
    it by default (`include_superseded=False`).

    Engine calls this on M3 retry alongside ``artifact_supersede_subtask``
    so the next-loop subagent doesn't read prior-loop handover context as
    current truth. Scoped strictly to one subtask of one task.
    Returns the row count touched.

    C12 — also stamps ``consumed_at = datetime('now')`` on the same
    UPDATE. Without that the role_handovers table has no per-row
    "when was this row last touched" timestamp, which kills the write
    -epoch watermark probe in :func:`write_role_handover`. The
    schema already has ``consumed_at`` for the singleton supersede
    path (``supersede_role_handover``); reusing it for the bulk-sweep
    path keeps both behaviours uniform.
    """
    from okuro.db import get_db
    db = get_db()
    with db.write():
        cursor = db.execute(
            "UPDATE role_handovers SET confidence = 0.1, "
            "consumed_at = datetime('now') "
            "WHERE task_id = ? AND subtask_id = ? AND confidence > 0.1",
            (task_id, subtask_id),
        )
        try:
            return int(getattr(cursor, "rowcount", 0) or 0)
        except Exception:
            return 0


def read_declared_project_path(task_id: str) -> str | None:
    """Return the freshest declared ``brief["project_path"]`` across all
    non-superseded handovers for ``task_id``.

    Walks newest-first; the first handover whose brief carries a
    non-empty ``project_path`` wins. Returns None when no handover
    declared one — the orchestrator then leaves ``task.project_path``
    empty and the UI surfaces an inline picker so the user can resolve
    the unknown.

    This is the canonical source of truth for "where did this task
    realize its result?". Pre-existing tasks (handovers written before
    the convention was documented) will return None — that is correct;
    the picker handles the migration.
    """
    if not task_id:
        return None
    rows = list_role_handovers(task_id=task_id, limit=200, include_superseded=False)
    for row in rows:
        brief = row.get("brief") or {}
        if not isinstance(brief, dict):
            continue
        candidate = (brief.get("project_path") or "").strip()
        if candidate:
            return candidate
    return None


def supersede_role_handover(old_id: str) -> str:
    """Drop confidence on ``old_id`` to 0.1 and stamp consumed_at = now.

    Mirrors ``artifact_supersede``. The new handover row carries
    supersedes=old_id (set by write_role_handover). Lineage stays
    queryable via kg_triples (predicate='supersedes').
    """
    from okuro.db import get_db

    db = get_db()
    with db.write():
        existing = db.fetchone(
            "SELECT id FROM role_handovers WHERE id = ?", (old_id,)
        )
        if not existing:
            return f"REJECTED: handover {old_id!r} does not exist."
        db.execute(
            "UPDATE role_handovers SET confidence = 0.1, "
            "consumed_at = datetime('now') WHERE id = ?",
            (old_id,),
        )
    return f"Superseded handover {old_id}"


# ---------------------------------------------------------------------------
# KG mirror
# ---------------------------------------------------------------------------


def _emit_kg_edges(
    db,
    *,
    handover_id: str,
    task_id: str,
    subtask_id: str,
    from_role: str,
    to_subtask_id: str | None,
    to_role: str | None,
    user_edges: list[dict],
    artifact_refs: list[str],
    memory_refs: list[str],
    cortex_refs: list[dict],
    supersedes: str | None,
    project: str | None,
    confidence: float,
) -> None:
    """Emit the canonical handover lineage edges into kg_triples.

    Runs INSIDE the caller's open ``db.write()`` block — kg_add cannot
    be used here because it opens its own write transaction and SQLite
    rejects nested BEGINs. We reproduce the minimum necessary subset
    inline: ensure the (subject, object) entities exist, then INSERT
    the triple with ``source_handover_id`` already set so the KG knows
    which handover asserted it from row one (no follow-up backstamp).
    """
    import hashlib

    def _ensure_entity(name: str, etype: str) -> None:
        if not name:
            return
        eid = hashlib.sha256(f"{name}:{etype}".encode("utf-8")).hexdigest()
        existing = db.fetchone(
            "SELECT 1 FROM kg_entities WHERE id = ?", (eid,),
        )
        if existing:
            return
        db.execute(
            "INSERT INTO kg_entities (id, name, type, project, properties) "
            "VALUES (?, ?, ?, ?, '{}')",
            (eid, name, etype, project),
        )

    # Umbrella audit fix #10 — track per-edge failures and surface them.
    # Pre-fix, individual INSERT failures emitted log.debug only — silent
    # at the default INFO level, so KG-handover lineage holes never
    # surfaced. Now we count failures, log a single warning at the end if
    # any happened, and surface the count back to the caller (which
    # writes a row-level note when non-zero).
    fail_count = [0]

    def _add(subject: str, predicate: str, obj: str,
             subject_type: str = "concept", object_type: str = "concept",
             user_conf: float | None = None) -> None:
        if not subject or not obj:
            return
        try:
            _ensure_entity(subject, subject_type)
            _ensure_entity(obj, object_type)
            tid = hashlib.sha256(
                f"{subject}\x1f{predicate}\x1f{obj}\x1f".encode("utf-8")
            ).hexdigest()
            existing = db.fetchone(
                "SELECT 1 FROM kg_triples WHERE id = ?", (tid,),
            )
            if existing:
                return
            db.execute(
                """INSERT INTO kg_triples
                   (id, subject, predicate, object, valid_from, valid_to,
                    project, source_memory_id, source_artifact_id,
                    source_handover_id, confidence)
                   VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, ?, ?)""",
                (
                    tid, subject, predicate, obj, project,
                    handover_id,
                    user_conf if user_conf is not None else confidence,
                ),
            )
        except Exception as exc:
            fail_count[0] += 1
            log.warning("kg edge (%s -%s-> %s) failed for handover %s: %s",
                        subject, predicate, obj, handover_id, exc)

    _add(handover_id, "produced_by", from_role, "handover", "role")
    if task_id:
        _add(handover_id, "for_task", task_id, "handover", "task")
    _add(handover_id, "for_subtask", subtask_id, "handover", "subtask")
    if to_role:
        _add(handover_id, "consumed_by", to_role, "handover", "role")
        _add(from_role, "handed_off_to", to_role, "role", "role")
    if to_subtask_id:
        _add(handover_id, "consumed_in", to_subtask_id, "handover", "subtask")
    for aid in artifact_refs:
        _add(handover_id, "references", aid, "handover", "artifact")
    for mid in memory_refs:
        _add(handover_id, "cites_memory", mid, "handover", "memory")
    for ref in cortex_refs:
        if not isinstance(ref, dict):
            continue
        path = (ref.get("path") or "").strip()
        if not path:
            continue
        s = ref.get("start_line")
        e = ref.get("end_line")
        target = f"{path}#L{s}-{e}" if s and e else path
        _add(handover_id, "references", target, "handover", "code_ref")
    if supersedes:
        _add(handover_id, "supersedes", supersedes, "handover", "handover")
    for edge in (user_edges or []):
        if not isinstance(edge, dict):
            continue
        s = (edge.get("subject") or "").strip()
        p = (edge.get("predicate") or "").strip()
        o = (edge.get("object") or "").strip()
        if s and p and o:
            user_conf = edge.get("confidence")
            _add(s, p, o,
                 user_conf=user_conf if isinstance(user_conf, (int, float)) else None)

    if fail_count[0]:
        log.warning(
            "_emit_kg_edges: %d edge(s) failed for handover %s — lineage "
            "may be incomplete (see preceding warnings)",
            fail_count[0], handover_id,
        )
