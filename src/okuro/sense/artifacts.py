# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Artifacts — first-class storage for reports, evidence, and plans.
# index: imports | def _embed_text | def artifact_write | def artifact_get | def artifact_search | def artifact_list | def artifact_stats | def artifact_supersede | def artifact_evict | def artifact_delete
# AGENT_HEADER_END -->
"""Artifacts — first-class storage for reports, evidence, and plans.

Parallel to agent_memory. Agent memories are POINTERS (atomic facts,
<800 chars, rejected if they look like documents). Artifacts are the
correct shape for long attributable compositions: audit reports,
pentest protocols, evidence dumps, execution plans.

**What DOES NOT belong here — code.** Shell scripts, Python modules,
SQL migrations, templates, generated configs: those live in the
relevant git repo (okuro itself or the target project). Git has
versioning, diffing, review, and executable semantics; artifacts
has none of those. The `deliverable` kind was a category error in
the original 026 design and was removed in 027.

Embedding input is ``title + summary + first ~500 chars of body`` to
keep the relevance signal concentrated in the intro; full-body
embeddings dilute.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from okuro.sense.retrieval import candidate_pool, scoped_vec_search, vec_write

# Structural supersede exclusion — the FK twin of the confidence gate.
# artifact_supersede lowers a superseded row to confidence 0.1, and the read
# paths hide `confidence <= 0.1`. But the two can drift: a supersede whose
# confidence UPDATE never fired, or a target pointed at by a later insert,
# leaves the OLD row at full confidence and rendering as current (measured
# 2026-07-20: 3 such rows at confidence 0.8-0.95). agent_memory guards this
# with a structural filter (sense/memory.py _EXCLUDE_SUPERSEDED), not confidence
# alone; artifacts claimed to mirror it but only ever checked confidence. This
# closes that gap: a row anyone points at via supersedes is hidden regardless
# of its confidence. Column name varies by query (`id` vs `a.id`), so it is a
# format string.
_EXCLUDE_SUPERSEDED = (
    " AND {col} NOT IN "
    "(SELECT supersedes FROM artifacts WHERE supersedes IS NOT NULL AND supersedes != '')"
)


# Kinds with distinct lifecycle, consumer, AND supersession semantics:
#   report   = post-execution synthesis (audits, retros, research output)
#   evidence = raw captured output a report cites (logs, traces, probes)
#   plan     = pre-execution document (migration plans, design specs, ADRs);
#              superseded as scope shifts, closed when executed (034)
# Adding a new kind requires all three of those axes to differ — otherwise
# reuse an existing kind. `deliverable` was removed in 027 because code
# belongs in git. Schema CHECK enforces at the DB layer; this set mirrors
# at the python layer so the error surfaces early instead of as a cryptic
# SQLite CHECK failure.
_ALLOWED_KINDS = {"report", "evidence", "plan"}
_ALLOWED_AUDIENCES = {"user", "process", "agent"}

# Authors whose artifacts are machine/pipeline output, not user deliverables.
# Kept in sync with migration 077's backfill — one rule, both places (DP10).
_AGENT_AUTHORS = {"compressor"}
# ROCK-SOLID v5 P5.3 — authors whose writes are MACHINERY, not deliverables.
#
# The plan asked to "default machine-shaped authors to process". Measured
# against the real store before implementing it, that rule is unsound: the
# artifacts actually in there are authored by `claude-code/fable-5`,
# `claude-code/opus-5`, `rock-solid-mappers-opus`, `orch-researcher`,
# `visual-communication-expert` — every one machine-shaped, and every one a
# genuine user-facing report. `created_by` names the AGENT, and agents write
# both traces and deliverables; it does not distinguish stream. A
# shape-based default would have demoted nearly every deliverable in the
# store, including the ones the P5.1 preview detector now looks for.
#
# So this stays an explicit allowlist, extended with the internal writers
# found in the codebase rather than guessed at. Each is a named call site
# that emits bookkeeping: verdicts, traces, prompts, escalations.
_PROCESS_AUTHORS = {
    "reviewer-autofix", "critic", "scorer",
    "phase-summarizer", "documenter", "workforce-reviewer",
    # Engine + reviewer machinery (P5.3).
    "reviewer-pipeline", "dispatcher_streaming", "engine-phase-gate",
    "orchestrator-detector", "dispatcher.build_role_prompt",
    "bootstrap.assembler", "roles_get.mcp", "capability_gap.escalate",
}


def _is_ac_evidence_title(title: str | None) -> bool:
    """True for the reviewer-evidence title the M3 contract reserves.

    Matches the terminal phrase rather than the whole shape, because the
    contract's `"<subtask> — AC evidence"` has a free-text subtask segment
    and observed titles vary in the separator ("1.1 — name — AC evidence").
    """
    return (title or "").strip().lower().endswith("ac evidence")


def _infer_audience(created_by: str | None, kind: str, title: str) -> str:
    """Which communication stream this artifact belongs to when the caller
    didn't say.

    Conservative BY DESIGN, and P5.3 keeps it that way after measuring: only
    demote KNOWN machine authors, never machine-SHAPED ones. Getting this
    wrong in the demoting direction is the worse failure — a deliverable
    filed as `process` disappears from the artifacts tab and from the preview
    proposer, while a trace filed as `user` is merely noise the reader can
    see and ignore.

    The caller passing `audience=` explicitly always wins over this.
    """
    cb = (created_by or "").strip()
    if cb in _AGENT_AUTHORS or (kind == "plan" and (title or "").startswith("Decision trace")):
        return "agent"
    if cb in _PROCESS_AUTHORS:
        return "process"
    # The AC-evidence artifact of the M3 evidentiary contract.
    #
    # Found live 2026-08-01: an AC-evidence artifact — grep commands, byte
    # counts, "0 matches" tables — was DELIVERED to a customer board member
    # through Stream C. The contract in dispatcher.py prescribes BOTH
    # `audience="agent"` AND `title="<subtask> — AC evidence"` in the same
    # prompt block. The subagent honoured the title and omitted the audience,
    # so this function defaulted it to "user".
    #
    # That is discipline where a mechanism belongs: the contract asked, and
    # nothing enforced. The title is prescribed by the same contract as the
    # audience, so it is a reliable stand-in when the audience is absent —
    # and this function is the single place that decides audience when the
    # caller did not, so fixing it here reaches the delivery fan-out, the
    # preview proposer and the artifacts tab at once rather than one filter
    # at a time.
    #
    # An explicit `audience=` from the caller still wins, per the docstring.
    # A genuine user deliverable whose title ends in "AC evidence" would be
    # demoted — accepted, because that title is reserved by the contract.
    if _is_ac_evidence_title(title):
        return "agent"
    return "user"
_EMBED_BODY_PREFIX_CHARS = 500


def _embed_text(title: str, summary: str | None, body: str | None) -> str:
    """Compose the text used for the vector embedding.

    Only the intro of ``body`` is included — relevance signal lives in
    the title + summary + first few hundred chars. Full-body embedding
    dilutes similarity across large artifacts.
    """
    parts: list[str] = [title.strip()]
    if summary and summary.strip():
        parts.append(summary.strip())
    if body:
        snippet = body[:_EMBED_BODY_PREFIX_CHARS].strip()
        if snippet:
            parts.append(snippet)
    return "\n\n".join(parts)


def _vec_bytes(text: str) -> bytes | None:
    """Embed a DOCUMENT and return float32 bytes, or None if embedding fails.

    Documents embed raw. Retrieval queries must NOT use this — see
    :func:`_query_vec_bytes`.
    """
    try:
        from okuro.embed.client import embed_one, to_bytes
        return to_bytes(embed_one(text))
    except Exception:
        return None


def _query_vec_bytes(text: str) -> bytes | None:
    """Embed a retrieval QUERY (asymmetric) and return float32 bytes.

    Qwen3-Embedding is instruction-tuned: a query must be wrapped while
    documents stay raw. Sharing one raw helper across both sides
    collapses discrimination (embed/client.py:255-266).
    """
    try:
        from okuro.embed.client import embed_query, to_bytes
        return to_bytes(embed_query(text))
    except Exception:
        return None


def _row_to_dict(row: dict) -> dict:
    """Parse JSON columns on a raw artifact row."""
    out = dict(row)
    refs = out.get("memory_refs")
    if isinstance(refs, str):
        try:
            out["memory_refs"] = json.loads(refs) if refs else []
        except (TypeError, ValueError):
            out["memory_refs"] = []
    return out


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def artifact_write(
    *,
    kind: str,
    title: str,
    summary: str | None = None,
    body: str | None = None,
    body_blob: bytes | None = None,
    media_type: str | None = None,
    project: str | None = None,
    parent_id: str | None = None,
    supersedes: str | None = None,
    memory_refs: list[str] | None = None,
    confidence: float = 0.8,
    created_by: str | None = None,
    audience: str | None = None,
    artifact_id: str | None = None,
    task_id: str | None = None,
    subtask_id: str | None = None,
    dispatch_epoch: str | None = None,
) -> str:
    """Store an artifact. Returns the artifact id (str).

    ``task_id`` / ``subtask_id`` route Stream B (user-facing subagent
    deliverables) into ``artifacts`` instead of disk .md. ArtifactsViewer
    queries via ``artifact_list(task_id=…)``. No FK — orchestrator state
    is JSON-on-disk.

    ``dispatch_epoch`` (C12) — ISO timestamp of the dispatch generation
    the writer believes it belongs to. When supplied, the row is
    silently superseded on insert (confidence=0.1) if a later supersede
    sweep already happened for the same ``(task_id, subtask_id)`` —
    closes the V7 race where a prior-retry subagent's MCP write lands
    AFTER ``artifact_supersede_subtask`` has run and the new row would
    otherwise read as current truth to the next M3 reviewer. The
    engine advances the epoch implicitly via ``artifact_supersede_subtask``;
    direct callers without a dispatch_epoch (legacy / non-engine
    writers) keep the historical behaviour.
    """
    if kind not in _ALLOWED_KINDS:
        hint = (
            "Code (shell scripts, Python, SQL, templates) belongs in a git repo "
            "(okuro/scripts, okuro/src, your project). `deliverable` was removed "
            "in migration 027 because it created overlap with git. "
            if kind == "deliverable"
            else ""
        )
        return (
            f"REJECTED: kind={kind!r} is not one of {sorted(_ALLOWED_KINDS)}. "
            f"{hint}"
            "Artifacts classify only synthesized work product: "
            "report=narrative (audit/pentest/retro), "
            "evidence=raw captured output a report cites (probe logs, traces), "
            "plan=pre-execution document (migration plan, design spec, ADR)."
        )
    if not title or not title.strip():
        return "REJECTED: title is required (min 3 chars after trim)."

    from okuro.db import get_db

    db = get_db()
    aid = artifact_id or str(uuid.uuid4())
    refs_json = json.dumps(list(memory_refs) if memory_refs else [])

    embed_input = _embed_text(title, summary, body)
    vec_bytes = _vec_bytes(embed_input) if embed_input else None

    with db.write():
        # Inherit the project when the caller did not name one. 2278 of the
        # 2287 untagged artifacts measured 2026-08-07 carried a task_id, so the
        # attribution was present at write time and simply never read.
        from okuro.sense.progress import resolve_write_project

        project = resolve_write_project(db, project, task_id=task_id)
        if project:
            from okuro.sense.progress import _ensure_project
            _ensure_project(db, project)

        # Wave-5 G13 — supersede-on-conflict for orchestrator artifacts.
        # When two writes target the same (task_id, subtask_id, kind)
        # logical slot — typical causes: parallel calls, retried subagent
        # that already shipped before kill, double-finalize race —
        # automatically mark the older row as superseded (confidence=0.1)
        # and link the new row to it. Without this _has_brain_artifact
        # passes wrongly with limit=1, ArtifactsViewer renders both, and
        # cortex returns duplicates → contract drift.
        if (task_id and subtask_id and not supersedes
                and kind in ("report", "evidence", "plan")):
            existing = db.fetchone(
                """SELECT id FROM artifacts
                       WHERE task_id = ? AND subtask_id = ? AND kind = ?
                         AND confidence > 0.1
                       ORDER BY created_at DESC LIMIT 1""",
                (task_id, subtask_id, kind),
            )
            if existing:
                old_id = existing[0] if not isinstance(existing, dict) else existing.get("id")
                if old_id:
                    db.execute(
                        "UPDATE artifacts SET confidence = 0.1 WHERE id = ?",
                        (old_id,),
                    )
                    supersedes = old_id

        if supersedes:
            db.execute(
                "UPDATE artifacts SET confidence = 0.1 WHERE id = ?",
                (supersedes,),
            )

        # C12 — write-epoch fence. If a supersede sweep already ran for
        # this (task_id, subtask_id) AFTER the writer's declared epoch,
        # the writer is a straggler from a prior dispatch generation;
        # land its row at confidence=0.1 so the next reviewer pass and
        # ``artifact_list(include_superseded=False)`` skip it. Reads
        # the max ``updated_at`` across already-superseded rows — that
        # column is bumped by the ``artifacts_touch_updated_at``
        # trigger every time confidence drops via
        # ``artifact_supersede_subtask``, so it is the implicit
        # watermark of "when did the engine last advance the dispatch
        # epoch for this slot". ``created_at`` would NOT work here
        # because the supersede sweep updates an existing row in place
        # — the row's creation timestamp predates the dispatch epoch.
        if (dispatch_epoch and task_id and subtask_id):
            try:
                sweep_row = db.fetchone(
                    """SELECT MAX(updated_at) AS ts FROM artifacts
                           WHERE task_id = ? AND subtask_id = ?
                             AND confidence <= 0.1""",
                    (task_id, subtask_id),
                )
                sweep_ts = (
                    sweep_row["ts"] if isinstance(sweep_row, dict)
                    else (sweep_row[0] if sweep_row else None)
                ) if sweep_row else None
                if sweep_ts and sweep_ts > dispatch_epoch:
                    confidence = 0.1
            except Exception:
                # Defence in depth — failing the watermark probe must
                # not block the primary write. Worst case is the
                # straggler lands at full confidence (original V7
                # behaviour) and gets superseded on the next retry
                # cycle, not an integrity regression.
                pass

        aud = (audience or "").strip().lower()
        if aud not in _ALLOWED_AUDIENCES:
            aud = _infer_audience(created_by, kind, title)

        db.execute(
            """INSERT INTO artifacts (
                    id, kind, title, summary, body, body_blob, media_type,
                    project, created_by, parent_id, supersedes, memory_refs,
                    confidence, task_id, subtask_id, audience
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                aid, kind, title.strip(), summary, body, body_blob, media_type,
                project, created_by, parent_id, supersedes, refs_json, confidence,
                task_id, subtask_id, aud,
            ),
        )

        if vec_bytes is not None:
            try:
                # `project` rides along as the partition value so a scoped
                # search can filter inside the KNN scan. A row written
                # without it is indexed and unreachable — see retrieval.py.
                vec_write(
                    db, "vec_artifacts", aid, vec_bytes,
                    partition=("project", project),
                )
            except Exception:
                pass

    return aid


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def artifact_get(artifact_id: str, include_body: bool = True) -> dict | None:
    """Fetch a single artifact by id. Returns None if missing.

    ``audience`` is in the select list deliberately. It was absent until
    2026-08-01, so every caller checking an artifact's audience through THIS
    function saw ``None`` while ``artifact_list`` returned the real value —
    and ``None`` is indistinguishable from "not addressed to anyone". Found
    while diagnosing a delivery that reached a person it should not have;
    the missing column briefly made the wrong diagnosis look right.
    """
    from okuro.db import get_db

    db = get_db()
    cols = (
        "id, kind, title, summary, body, media_type, project, created_at, "
        "updated_at, created_by, parent_id, supersedes, memory_refs, "
        "confidence, task_id, subtask_id, audience"
    )
    if not include_body:
        cols = cols.replace("body, ", "")

    row = db.fetchone(
        f"SELECT {cols} FROM artifacts WHERE id = ?",
        (artifact_id,),
    )
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# search (semantic) + list (filter)
# ---------------------------------------------------------------------------

def artifact_search(
    query: str,
    *,
    kind: str | None = None,
    project: str | None = None,
    parent_id: str | None = None,
    limit: int = 10,
    min_confidence: float = 0.0,
    audience: str | None = None,
    include_superseded: bool = False,
) -> list[dict]:
    """Semantic search over artifacts. Falls back to empty when embedding fails.

    ``include_superseded=False`` (default) drops rows with confidence
    <= 0.1 — the threshold ``artifact_supersede`` stamps. Without it a
    superseded artifact competes with, and can outrank, the very row
    that replaced it. This mirrors ``read_memory``'s _EXCLUDE_SUPERSEDED
    (sense/memory.py:848); the two sibling stores had diverged.

    ``audience`` filters to 'user' / 'agent' / 'process'.
    """
    from okuro.db import get_db


    db = get_db()
    vec = _query_vec_bytes(query)
    if vec is None:
        return []

    # Every narrowing below (project / kind / audience / confidence /
    # supersede) runs AFTER this scan, so the pool must carry headroom for
    # what they will discard. `limit * 3` did not: at limit=2 it asked for six
    # store-wide neighbours and a project filter emptied all six, which is how
    # a handover written 90 seconds earlier read as "never written". See
    # sense/retrieval.py.
    scoped = bool(project or kind or audience or parent_id
                  or not include_superseded)
    matches = scoped_vec_search(
        db, "vec_artifacts", vec,
        limit=candidate_pool(limit, scoped=scoped),
        partition=("project", project) if project else None,
    )
    if not matches:
        return []

    order = {m["id"]: (i, 1 - m["distance"]) for i, m in enumerate(matches)}
    ids = list(order.keys())
    placeholders = ", ".join("?" * len(ids))

    sql = (
        "SELECT id, kind, title, summary, project, created_at, updated_at, "
        "parent_id, supersedes, confidence, task_id, subtask_id "
        f"FROM artifacts WHERE id IN ({placeholders}) AND confidence >= ?"
    )
    params: list[Any] = [*ids, min_confidence]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if project:
        sql += " AND project = ?"
        params.append(project)
    if parent_id:
        sql += " AND parent_id = ?"
        params.append(parent_id)
    if audience:
        sql += " AND audience = ?"
        params.append(audience)
    if not include_superseded:
        sql += " AND confidence > 0.1" + _EXCLUDE_SUPERSEDED.format(col="id")

    rows = db.fetchall(sql, tuple(params))
    enriched = []
    for r in rows:
        d = _row_to_dict(r)
        d["relevance"] = order[r["id"]][1]
        enriched.append(d)
    enriched.sort(key=lambda d: order[d["id"]][0])
    return enriched[:limit]


def artifact_list(
    *,
    kind: str | None = None,
    project: str | None = None,
    parent_id: str | None = None,
    task_id: str | None = None,
    subtask_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "created_at_desc",
    include_superseded: bool = True,
    audience: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
) -> list[dict]:
    """List artifacts with filters. Does not return body/body_blob.

    ``created_after`` / ``created_before`` (ISO dates or datetimes,
    inclusive of ``created_after``, exclusive of ``created_before``) and
    ``offset`` exist because without them a mid-store window was
    STRUCTURALLY UNREACHABLE. Measured 2026-07-29: an audit of the
    2026-05-10..07-17 blind window could page 50 rows from the newest end
    (reaching back only to 07-27) and 50 from the oldest end (stopping at
    05-29 with a kind filter), and had no way to address anything between.
    It reported "in-window report and evidence artifacts are entirely
    unenumerated — a tool limitation, not a time limitation".

    An observability gap in the sanctioned path is not neutral: the agent
    either reports the hole (best case, and what happened) or reaches for
    raw SQL, which the store hook correctly refuses. Either way the
    measurement does not get made.

    ``task_id`` / ``subtask_id`` filter Stream B subagent deliverables.
    ArtifactsViewer reads via ``task_id`` to render the per-task panel
    that previously walked ``tasks/{id}/artifacts/`` on disk.

    ``include_superseded=False`` filters out rows with confidence <= 0.1
    — the same threshold ``artifact_supersede`` uses. The M3 reviewer
    passes False so it never reads prior-loop deliverables as truth
    (audit gap from docs/audit-2026-05-26/03-m3-review-loop.md fix
    applied at the systemic layer, not as a per-task heuristic).
    Default True preserves backward compat for the ArtifactsViewer +
    audit trails.
    """
    from okuro.db import get_db

    db = get_db()
    # Project length(body) + length(body_blob) so the UI can show real
    # sizes without fetching the body itself. SQLite length() on NULL
    # returns NULL — coalesce so the sum is always an integer.
    sql = (
        "SELECT id, kind, title, summary, project, created_at, updated_at, "
        "parent_id, supersedes, confidence, media_type, task_id, subtask_id, "
        "created_by, audience, "
        "COALESCE(length(body), 0) + COALESCE(length(body_blob), 0) AS body_len "
        "FROM artifacts WHERE 1 = 1"
    )
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if project:
        sql += " AND project = ?"
        params.append(project)
    if parent_id:
        sql += " AND parent_id = ?"
        params.append(parent_id)
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    if subtask_id:
        sql += " AND subtask_id = ?"
        params.append(subtask_id)
    if not include_superseded:
        sql += " AND confidence > 0.1" + _EXCLUDE_SUPERSEDED.format(col="id")
    if audience:
        sql += " AND audience = ?"
        params.append(audience)
    if created_after:
        sql += " AND created_at >= ?"
        params.append(created_after)
    if created_before:
        sql += " AND created_at < ?"
        params.append(created_before)

    order_clause = {
        "created_at_desc": " ORDER BY created_at DESC",
        "created_at_asc":  " ORDER BY created_at ASC",
        "updated_at_desc": " ORDER BY updated_at DESC",
        "confidence_desc": " ORDER BY confidence DESC, created_at DESC",
    }.get(order, " ORDER BY created_at DESC")

    sql += order_clause + " LIMIT ? OFFSET ?"
    params.append(limit)
    params.append(max(0, int(offset or 0)))

    rows = db.fetchall(sql, tuple(params))
    return [_row_to_dict(r) for r in rows]


def artifact_stats(
    *,
    group_by: str = "week",
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    kind: str | None = None,
) -> dict:
    """Creation counts per period, split active vs superseded. READ-ONLY.

    The aggregate half of the same gap ``artifact_list``'s date window
    closes. Paging a nine-week window one 50-row page at a time answers
    "which artifacts" but not "how many, and how many of them were later
    retracted" — and the second question is the one an audit of a blind
    window actually asks.

    ``group_by``: "day" | "week" | "month". Weeks are ISO-ish
    (``strftime('%Y-W%W')``); the grouping key is returned verbatim so a
    caller never has to re-derive it. ``since`` is inclusive, ``until``
    exclusive, both ISO strings.

    Superseded is the same threshold every other artifact surface uses:
    confidence <= 0.1, as set by ``artifact_supersede``.
    """
    from okuro.db import get_db

    fmt = {
        "day": "%Y-%m-%d",
        "week": "%Y-W%W",
        "month": "%Y-%m",
    }.get(group_by)
    if fmt is None:
        raise ValueError(
            f"group_by must be day|week|month, got {group_by!r}"
        )

    sql = (
        f"SELECT strftime('{fmt}', created_at) AS period, "
        "COUNT(*) AS total, "
        "SUM(CASE WHEN confidence <= 0.1 THEN 1 ELSE 0 END) AS superseded, "
        "SUM(CASE WHEN confidence > 0.1 THEN 1 ELSE 0 END) AS active "
        "FROM artifacts WHERE 1 = 1"
    )
    params: list[Any] = []
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if until:
        sql += " AND created_at < ?"
        params.append(until)
    if project:
        sql += " AND project = ?"
        params.append(project)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " GROUP BY period ORDER BY period ASC"

    rows = [dict(r) for r in get_db().fetchall(sql, tuple(params))]
    return {
        "group_by": group_by,
        "since": since,
        "until": until,
        "project": project,
        "kind": kind,
        "periods": rows,
        "total": sum(r["total"] for r in rows),
        "active": sum(r["active"] for r in rows),
        "superseded": sum(r["superseded"] for r in rows),
    }


# ---------------------------------------------------------------------------
# supersede + delete
# ---------------------------------------------------------------------------

def artifact_supersede_subtask(task_id: str, subtask_id: str) -> int:
    """Mark every active artifact for ``(task_id, subtask_id)`` as
    superseded by dropping its confidence to 0.1.

    Called by the engine when a subtask is reset via the M3 retry path
    (`reviewer/pipeline.py::rerun_subtasks_from_verdict`). Without this,
    the brain-canonical reviewer accumulates artifacts across loops and
    flags the (correct) contradictions between current and prior work
    as load-bearing critic findings — guaranteeing a permanent FAIL
    loop for any task that retries. See audit
    docs/audit-2026-05-26/03-m3-review-loop.md and the systematic-fix
    discussion in the canonical-state migration.

    Scoped to (task_id, subtask_id) only — cannot touch artifacts for
    any other subtask or any other task. Returns the row count touched.
    """
    from okuro.db import get_db
    db = get_db()
    with db.write():
        cursor = db.execute(
            "UPDATE artifacts SET confidence = 0.1 "
            "WHERE task_id = ? AND subtask_id = ? AND confidence > 0.1",
            (task_id, subtask_id),
        )
        try:
            return int(getattr(cursor, "rowcount", 0) or 0)
        except Exception:
            return 0


def artifact_supersede(old_id: str, new_id: str) -> str:
    """Point ``new_id`` at ``old_id`` and drop old confidence to 0.1.

    Prefer this over delete when history matters (it does, for audit
    trails). The vec row for the old id is kept so semantic search can
    still surface the supersede chain.
    """
    from okuro.db import get_db

    db = get_db()
    with db.write():
        existing_old = db.fetchone(
            "SELECT id FROM artifacts WHERE id = ?", (old_id,),
        )
        existing_new = db.fetchone(
            "SELECT id FROM artifacts WHERE id = ?", (new_id,),
        )
        if not existing_old:
            return f"REJECTED: old_id={old_id!r} does not exist."
        if not existing_new:
            return f"REJECTED: new_id={new_id!r} does not exist."
        db.execute(
            "UPDATE artifacts SET supersedes = ? WHERE id = ?",
            (old_id, new_id),
        )
        db.execute(
            "UPDATE artifacts SET confidence = 0.1 WHERE id = ?",
            (old_id,),
        )
    return f"Superseded: {old_id} -> {new_id}"


def artifact_evict(dry_run: bool = True, min_age_days: int = 7,
                   limit: int = 500) -> dict:
    """Evict superseded artifacts — the mirror of ``memory_hygiene``'s prune.

    WHY THIS EXISTS. 54.4% of the artifact store is superseded history and
    nothing ever removed any of it; ``agent_memory`` has had a prune pass since
    2026-07-16 and artifacts never got the twin. Measured 2026-07-29 for the
    2026-05-10..07-17 window alone: 2,374 artifacts, 1,334 of them superseded.

    ``dry_run=True`` IS THE DEFAULT AND IS DELIBERATE. This ships as a built,
    tested, callable path that does nothing until someone passes
    ``dry_run=False``. Eviction is destructive and the store is the audit trail
    for every investigation in the system, so arming it is a human decision.

    TARGETING mirrors memory_hygiene exactly, including the inversion it had to
    fix: evict rows that ARE POINTED AT (``id IN (SELECT supersedes ...)``) —
    the superseded originals — never rows that CARRY a pointer, which are the
    corrections. That bug deleted the fix and kept the error for months on the
    memory side; it is not repeated here.

    ``min_age_days`` gates on the REPLACEMENT's age, so a correction gets an
    audit window before its predecessor disappears.

    Vectors go with the rows. ``vec_artifacts`` has no FK or cascade to
    ``artifacts``, so a plain DELETE orphans the vector and the orphan keeps
    ranking in ``artifact_search`` — the same defect that had superseded
    memories ranking above their own replacements.
    """
    from datetime import datetime, timedelta, timezone

    from okuro.db import get_db

    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(min_age_days))
              ).isoformat()

    rows = db.fetchall(
        """SELECT a.id, a.kind, a.project, substr(a.title, 1, 90) AS title,
                  a.created_at
           FROM artifacts a
           WHERE a.confidence <= 0.1 AND a.id IN (
               SELECT supersedes FROM artifacts
               WHERE supersedes IS NOT NULL AND created_at < ?
           )
           ORDER BY a.created_at ASC
           LIMIT ?""",
        (cutoff, int(limit)),
    )
    candidates = [dict(r) for r in rows]

    total_superseded = (db.fetchone(
        "SELECT COUNT(*) AS n FROM artifacts WHERE confidence <= 0.1"
    ) or {}).get("n")

    result = {
        "dry_run": bool(dry_run),
        "min_age_days": int(min_age_days),
        "limit": int(limit),
        "total_superseded_in_store": total_superseded,
        "eligible": len(candidates),
        # Named so a caller can see WHAT would go, not just how many. A count
        # alone is how a destructive sweep gets approved without being read.
        "candidates": candidates,
        "evicted": 0,
    }
    if dry_run or not candidates:
        return result

    ids = [c["id"] for c in candidates]
    placeholders = ", ".join("?" * len(ids))
    with db.write():
        db.execute(
            f"UPDATE artifacts SET parent_id = NULL "
            f"WHERE parent_id IN ({placeholders})", tuple(ids),
        )
        db.execute(
            f"UPDATE artifacts SET supersedes = NULL "
            f"WHERE supersedes IN ({placeholders})", tuple(ids),
        )
        db.execute(
            f"DELETE FROM artifacts WHERE id IN ({placeholders})", tuple(ids),
        )
        try:
            db.execute(
                f"DELETE FROM vec_artifacts WHERE id IN ({placeholders})",
                tuple(ids),
            )
        except Exception:
            pass
    result["evicted"] = len(ids)
    return result


def artifact_delete(artifact_id: str) -> str:
    """Hard-delete an artifact and its vector row."""
    from okuro.db import get_db

    db = get_db()
    with db.write():
        existing = db.fetchone(
            "SELECT id FROM artifacts WHERE id = ?", (artifact_id,),
        )
        if not existing:
            return f"REJECTED: artifact_id={artifact_id!r} does not exist."
        db.execute(
            "UPDATE artifacts SET parent_id = NULL WHERE parent_id = ?",
            (artifact_id,),
        )
        db.execute(
            "UPDATE artifacts SET supersedes = NULL WHERE supersedes = ?",
            (artifact_id,),
        )
        db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        try:
            db.execute("DELETE FROM vec_artifacts WHERE id = ?", (artifact_id,))
        except Exception:
            pass
    return f"Deleted: {artifact_id}"
