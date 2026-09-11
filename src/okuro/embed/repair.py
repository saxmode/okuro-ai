# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Realign vec_* tables with the active embedding tier dimension.
# index:
#   imports
#   class VecSpec
#   const SPECS
#   def _current_dim
#   def _row_text_*
#   def ensure_vec_dims
#   def main
# AGENT_HEADER_END -->
"""Realign vec_* tables with the active embedding tier dimension AND metric.

Why this exists: ``001_core.sql`` and friends hard-code ``float[384]`` — the
old bge-small dim. After the 2026-05-09 swap to Qwen3-Embedding-0.6B (1024d),
only ``vec_cortex`` got fixed (migration 038). Every other vec table either
errors on insert or holds stale 384d vectors that can't be queried with the
new model. Symptom: bootstrap intake reports ``Dimension mismatch — Expected
384 dimensions but received 1024``.

DISTANCE METRIC (2026-07-15). A vec0 table with no ``distance_metric=`` uses
sqlite-vec's default: **L2**. Every vec_* table except ``vec_cortex`` was
created that way, while the callers scored ``similarity = 1 - distance`` —
which is only true for cosine. On L2 over unit vectors the identity is
``L2^2 = 2(1 - cos)``, so ``1 - distance`` is neither cosine nor bounded: a
genuine 0.65-cosine match scores 0.166 and an orthogonal pair scores -0.414.
Measured consequences before this fix:

  * ``read_memory``  — floor ``_SIM_FLOOR = 0.4`` effectively demanded cosine
    > 0.82, so real matches were discarded and EVERY query silently fell back
    to recency ordering. Semantic memory recall had been dead since the
    2026-05-10 Qwen3 swap (bge-small's compressed ~0.9+ band happened to clear
    the floor, which masked the bug for two months).
  * ``write_memory`` — the 0.9 dedup/reinforce gate demanded cosine > 0.995,
    so near-duplicates were never folded and confidence never grew from
    repeated learning.

Migration 059 diagnosed exactly this for vec_cortex ("F23 — declare
distance_metric=cosine ... goes negative once distance > 1") and fixed only
that table. This module now enforces the same shape for the other eleven, so
the metric is declared in the schema rather than re-derived (and re-botched)
at each call site.

Behaviour: idempotent. For each spec, compares the live column dim against
``EmbedConfig.dim`` and the live metric against ``cosine``.

  * dim mismatch    → drop, recreate, backfill by RE-EMBEDDING source rows
    (needs the embed service; honours ``empty_only``).
  * metric mismatch only → drop, recreate, backfill by COPYING the existing
    vectors. No re-embed, no embed service, no data loss — the vectors are
    already correct, only the table's declared metric was wrong. Safe to run
    with rows present, so ``empty_only`` does not defer it.

Run via ``python -m okuro.embed.repair`` (live) or ``--dry-run`` to preview.
Survives tier switches because the target dim is read from config each call.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("okuro.embed.repair")


# Per-writer caps — copied from the source modules to avoid pulling their
# unrelated imports. Keep these in sync if the writers ever change them.
_ARTIFACT_BODY_CHARS = 500
_DELIVERY_BODY_CHARS = 500
_DIARY_CHARS = 4000
_TRANSCRIPT_CHARS = 4000
_HANDOVER_SUMMARY_CHARS = 500


def _row_text_thoughts(row: dict) -> Optional[str]:
    return (row.get("content") or "").strip() or None


def _row_text_memory(row: dict) -> Optional[str]:
    return (row.get("content") or "").strip() or None


def _row_text_persons(row: dict) -> Optional[str]:
    parts = [row.get("display_name") or ""]
    for col in ("organization", "role"):
        v = row.get(col)
        if v:
            parts.append(str(v))
    tags = row.get("tags")
    if isinstance(tags, str) and tags:
        try:
            tag_list = json.loads(tags)
            if isinstance(tag_list, list) and tag_list:
                parts.append(" ".join(str(t) for t in tag_list))
        except (TypeError, ValueError):
            pass
    notes = row.get("notes")
    if notes:
        parts.append(str(notes)[:200])
    text = " | ".join(p for p in parts if p)
    return text or None


def _row_text_roles(row: dict) -> Optional[str]:
    return (row.get("description") or "").strip() or None


def _row_text_role_knowledge(row: dict) -> Optional[str]:
    return (row.get("content") or "").strip() or None


def _row_text_artifacts(row: dict) -> Optional[str]:
    title = (row.get("title") or "").strip()
    if not title:
        return None
    parts = [title]
    summary = (row.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    body = row.get("body")
    if body:
        snippet = str(body)[:_ARTIFACT_BODY_CHARS].strip()
        if snippet:
            parts.append(snippet)
    return "\n\n".join(parts)


def _row_text_transcripts(row: dict) -> Optional[str]:
    content = row.get("content") or ""
    return content[:_TRANSCRIPT_CHARS] or None


def _row_text_interaction_turns(row: dict) -> Optional[str]:
    """Cleaned input-side turn text.

    Cleaning matters more here than for other specs: the raw text carries
    harness furniture (system-reminders, IDE notices, resume preambles) that
    is identical across thousands of turns and would pull every vector toward
    the same boilerplate centroid.
    """
    from okuro.sense.interaction.turns import EMBED_CHAR_CAP, clean_turn_text

    cleaned = clean_turn_text(row.get("text"))
    return cleaned[:EMBED_CHAR_CAP] or None


def _row_text_role_diary(row: dict) -> Optional[str]:
    entry = row.get("entry") or ""
    return entry[:_DIARY_CHARS] or None


def _row_text_role_handovers(row: dict) -> Optional[str]:
    """Mirror sense.role_handover._embed_text."""
    raw = row.get("brief")
    if isinstance(raw, str):
        try:
            brief = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            brief = {}
    elif isinstance(raw, dict):
        brief = raw
    else:
        brief = {}
    parts: list[str] = []
    summary = (brief.get("summary") or "").strip()
    if summary:
        parts.append(summary[:_HANDOVER_SUMMARY_CHARS])
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
    refs_raw = row.get("cortex_refs")
    if isinstance(refs_raw, str):
        try:
            refs = json.loads(refs_raw) if refs_raw else []
        except (TypeError, ValueError):
            refs = []
    elif isinstance(refs_raw, list):
        refs = refs_raw
    else:
        refs = []
    purposes = [
        str(r.get("purpose", "")).strip()
        for r in refs
        if isinstance(r, dict) and r.get("purpose")
    ]
    if purposes:
        parts.append(" / ".join(purposes[:5]))
    return "\n\n".join(parts) or None


def _row_text_deliveries(row: dict) -> Optional[str]:
    title = (row.get("title") or "").strip()
    parts: list[str] = []
    if title:
        parts.append(title)
    body = row.get("body")
    if body:
        snippet = str(body)[:_DELIVERY_BODY_CHARS].strip()
        if snippet:
            parts.append(snippet)
    if not parts:
        return None
    return "\n\n".join(parts)


def _row_text_note_chunks(row: dict) -> Optional[str]:
    # note_chunks rows already hold one pre-chunked, embed-ready span of a note.
    text = (row.get("text") or "").strip()
    return text or None


# Every vec_* table this module owns is queried by callers that score
# ``similarity = 1 - distance``. That identity holds for cosine distance and
# nothing else, so cosine is the shape we enforce. Declared here once rather
# than spelled out at each CREATE.
TARGET_METRIC = "cosine"

# A vec0 partition key must never receive NULL. `NULL = NULL` is never true, so
# vec0 finds no existing chunk for a NULL value and allocates a fresh one per
# row — and a chunk is preallocated whole (1024 slots x dim x 4 B = 4 MiB at
# 1024d) regardless of how many vectors land in it.
#
# Measured on this install 2026-08-12, sqlite-vec 0.1.9, 600 vectors at dim 128:
#     partition=NULL      -> 600 chunks
#     partition=''        ->   1 chunk
#     two values, alternating -> 2 chunks   (so the driver is the VALUE, not
#                                            insert order)
# Live cost of getting this wrong: vec_artifacts held 2320 NULL chunks for 3577
# vectors — 9434 MB allocated where 14 MB was needed. vec_cortex hit the same
# wall in June 2026 (24.5 GB -> 151 MB after repack) and grew its own sentinel;
# this constant is that lesson applied to every table in SPECS.
#
# THE EMPTY STRING IS LOAD-BEARING, NOT AN ARBITRARY PLACEHOLDER. Every read
# site scopes with `partition=(col, value) if value else None`, so a FALSY
# sentinel makes an unscoped read pass `None` and fall through to the global
# scan that reaches these rows. A truthy sentinel like "__none__" would be
# written happily and then be unreachable from every query — indexed, present,
# invisible. cortex's NO_PROJECT_PARTITION is "" for the same reason. Do not
# "improve" this to something more readable without changing every read site.
NO_PARTITION = ""


def partition_value(raw: object) -> object:
    """Coalesce a missing partition value to the sentinel.

    Every write into a partitioned vec_* table goes through here. A row whose
    scope column is NULL is legitimate in the SOURCE table (an artifact with no
    project is a real artifact) — it is only the VECTOR partition that cannot
    carry NULL, so the coalescing lives at the vector boundary and the source
    row keeps its true value.
    """
    return NO_PARTITION if raw is None else raw


def _create_vec_sql(vec_table: str, dim: int, partition: Optional[str] = None) -> str:
    """The canonical CREATE for an okuro vec0 table.

    Single source of truth for vec_* shape: id + embedding at the active dim,
    with the distance metric DECLARED. Omitting the clause silently yields L2
    (see module docstring) — the defect this function exists to make
    unrepresentable.

    ``partition`` declares a vec0 PARTITION KEY, which is what lets a scoped
    query filter inside the scan. Omitting one where a scope dominates is the
    second defect this function now makes unrepresentable — a spec declares
    its scope column and every rebuild path picks it up.
    """
    cols = [f"{partition} TEXT partition key, "] if partition else []
    return (
        f"CREATE VIRTUAL TABLE {vec_table} USING vec0("
        f"id TEXT PRIMARY KEY, "
        + "".join(cols)
        + f"embedding float[{dim}] distance_metric={TARGET_METRIC})"
    )


def _current_partition(db, vec_table: str) -> Optional[str]:
    """Return the declared vec0 partition-key column of vec_table, or None.

    Read off the table's own DDL, like ``_current_metric``, so the schema
    stays the single source of truth for shape.
    """
    sql = _table_ddl(db, vec_table)
    if not sql:
        return None
    lowered = sql.lower()
    if "partition key" not in lowered:
        return None
    head = lowered.split("partition key", 1)[0].rstrip()
    # "... , project text" -> the column name is the second-to-last token.
    parts = head.replace(",", " ").split()
    return parts[-2] if len(parts) >= 2 else None


def _rebuild_shape_only(db, spec: "VecSpec", dim: int, entry: dict) -> None:
    """Re-declare vec_table's metric and/or partition key, keeping its vectors.

    vec0 fixes the metric at CREATE time, so "changing" it means drop +
    recreate. The vectors themselves are metric-agnostic bytes and stay
    valid, so they are read out first and written straight back — no
    re-embedding, and the source text is never consulted.

    Read-then-write via Python is deliberate: ``INSERT ... SELECT`` between
    two vec0 tables fails ("no such table: <t>_rowids"), and ``ALTER TABLE
    ... RENAME`` does not carry vec0's shadow tables. Recreating under the
    original name sidesteps both.
    """
    vec_table = spec.vec_table
    rows = db.fetchall(f"SELECT id, embedding FROM {vec_table}")
    payload = [(r["id"], r["embedding"]) for r in rows]

    # A partition key needs a VALUE per row, and the vectors do not carry it —
    # it lives on the source row. Read it once here rather than re-embedding:
    # the vectors stay byte-identical, only the table's shape changes.
    part_vals: dict = {}
    if spec.partition:
        for r in db.fetchall(spec.source_sql):
            raw = r[spec.partition] if spec.partition in r.keys() else None
            part_vals[r["id"]] = partition_value(raw)

    cols = "id, " + (f"{spec.partition}, " if spec.partition else "") + "embedding"
    marks = "?, ?, ?" if spec.partition else "?, ?"
    with db.write():
        db.execute(f"DROP TABLE IF EXISTS {vec_table}")
        db.execute(_create_vec_sql(vec_table, dim, spec.partition))
        for vid, blob in payload:
            # ``.get`` misses on an ORPHAN vector — one whose source row is
            # gone (measured: vec_memory carried 4078 vectors against 3803
            # source rows). Those are exactly the ids with no partition value
            # to inherit, so they are the ones that would land NULL.
            args = (
                (vid, partition_value(part_vals.get(vid)), blob)
                if spec.partition
                else (vid, blob)
            )
            db.execute(
                f"INSERT INTO {vec_table} ({cols}) VALUES ({marks})", args
            )
    after = db.fetchone(f"SELECT COUNT(*) AS n FROM {vec_table}")["n"]
    entry["copied"] = after
    if after != len(payload):
        entry["error"] = (
            f"vector count changed during metric rebuild: "
            f"{len(payload)} → {after}"
        )
        logger.error(
            "%s: metric rebuild copied %d of %d vectors",
            vec_table, after, len(payload),
        )


def _row_text_target_groups(row: dict) -> Optional[str]:
    """Delegate to the owning module rather than rebuilding the text here.

    Unlike every other spec, a group's text is not a concatenation of its own
    columns: membership for a company + role_class audience derives from
    affiliations, and the merged cognitive shape only exists once members
    resolve. Duplicating that logic here would let the backfill produce a
    different description than the write path — the classic drift where a
    rebuilt index no longer matches the live one.
    """
    from okuro.peer.target_groups import group_embed_text

    return group_embed_text(row["id"])


def _row_text_distill_sessions(row: dict) -> Optional[str]:
    """Delegate to the owning module, like ``_row_text_target_groups``.

    A distill session's vector is built from its facet JSON plus the tool
    names tier-0 recorded, and that recipe lives in
    ``sense.distill.corpus.facet_summary_text``. Rebuilding it here would let
    the backfill embed different text than the live writer — the drift where a
    rebuilt index quietly stops matching the one it replaced.
    """
    from okuro.sense.distill.corpus import row_embed_text

    return row_embed_text(row)


def _row_text_distill_lessons(row: dict) -> Optional[str]:
    """The lesson's own text. No delegation needed — unlike a facet or a target
    group, a lesson's vector is built from ONE column, so there is no second
    definition that could drift from the live writer
    (``sense.distill.lessons.embed_lesson`` embeds the same string).
    """
    return (row.get("lesson_text") or "").strip() or None


@dataclass(frozen=True)
class VecSpec:
    """One vec_* table and how to rebuild it from its source."""

    vec_table: str
    source_sql: str  # SELECT yielding rows with an 'id' column
    text_fn: Callable[[dict], Optional[str]]
    # Column to declare as a vec0 PARTITION KEY, so a scoped query filters
    # inside the KNN scan instead of after it. When set, ``source_sql`` MUST
    # also yield that column — the rebuild reads the value from there.
    # See PARTITION KEYS in the module docstring for what goes wrong without
    # one. None = no dominant scope worth partitioning on.
    partition: Optional[str] = None


SPECS: list[VecSpec] = [
    VecSpec(
        "vec_thoughts",
        "SELECT id, content FROM thoughts",
        _row_text_thoughts,
    ),
    VecSpec(
        "vec_memory",
        "SELECT id, content, project FROM agent_memory",
        _row_text_memory,
        partition="project",
    ),
    VecSpec(
        "vec_persons",
        "SELECT id, display_name, organization, role, tags, notes FROM persons",
        _row_text_persons,
    ),
    VecSpec(
        "vec_roles",
        "SELECT role_id AS id, description FROM roles",
        _row_text_roles,
    ),
    VecSpec(
        "vec_knowledge",
        "SELECT id, content, role_id FROM role_knowledge",
        _row_text_role_knowledge,
        partition="role_id",
    ),
    VecSpec(
        "vec_artifacts",
        "SELECT id, title, summary, body, project FROM artifacts",
        _row_text_artifacts,
        partition="project",
    ),
    VecSpec(
        "vec_transcript_messages",
        "SELECT id, content FROM transcript_messages",
        _row_text_transcripts,
    ),
    VecSpec(
        "vec_interaction_turns",
        "SELECT uuid AS id, text FROM agent_events "
        "WHERE type = 'user' AND text IS NOT NULL AND text != ''",
        _row_text_interaction_turns,
    ),
    VecSpec(
        "vec_role_diary",
        "SELECT id, entry FROM role_diary_entries",
        _row_text_role_diary,
    ),
    # UNPARTITIONED since 2026-08-13. task_id was declared as a partition key
    # for filter push-down, and the cost was measured at 266 chunks for 942
    # vectors — ~1064 MiB preallocated where ~3.7 MB is needed, because a
    # partition value is minted per orchestrator task and never retired. The
    # scoped read already post-filters against list_role_handovers(task_id=…),
    # so the push-down was an optimisation, not the thing that made the answer
    # correct. At this corpus size an unpartitioned scan is one chunk-block.
    VecSpec(
        "vec_role_handovers",
        "SELECT id, brief, cortex_refs, task_id FROM role_handovers",
        _row_text_role_handovers,
    ),
    VecSpec(
        "vec_deliveries",
        "SELECT id, title, body FROM deliveries",
        _row_text_deliveries,
    ),
    VecSpec(
        "vec_note_chunks",
        "SELECT id, text FROM note_chunks",
        _row_text_note_chunks,
    ),
    VecSpec(
        "vec_target_groups",
        "SELECT id FROM target_groups WHERE active = 1",
        _row_text_target_groups,
    ),
    # UNPARTITIONED, deliberately and permanently. The corpus is ~10 000
    # session-level vectors, which is one chunk-block; the only column anyone
    # would reach for as a partition key is the session id itself, and a
    # partition value per session would preallocate 4 MiB per session (see
    # NO_PARTITION above for the measured cost of getting this wrong).
    VecSpec(
        "vec_distill_sessions",
        "SELECT session_id AS id, facet, distinct_tools FROM distill_facets "
        "WHERE facet IS NOT NULL",
        _row_text_distill_sessions,
    ),
    # UNPARTITIONED, for the same reason as vec_distill_sessions above and one
    # more. Lessons number in the hundreds, so the whole table is a fraction of
    # one chunk-block; and the only column anyone would reach for as a
    # partition key is the lesson id itself, which would preallocate 4 MiB PER
    # LESSON. cluster_id is the other tempting choice and is worse than
    # useless: it is nullable, it is REASSIGNED on every re-cluster (the labels
    # are relative to the population), and a partition value that changes under
    # a stored vector strands it in the old partition.
    VecSpec(
        "vec_distill_lessons",
        "SELECT CAST(id AS TEXT) AS id, lesson_text FROM distill_lessons "
        "WHERE lesson_text IS NOT NULL AND lesson_text != ''",
        _row_text_distill_lessons,
    ),
]


def _table_ddl(db, vec_table: str) -> Optional[str]:
    """Return the CREATE statement for vec_table, or None if absent."""
    row = db.fetchone(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (vec_table,),
    )
    if not row or not row["sql"]:
        return None
    return row["sql"]


def _current_dim(db, vec_table: str) -> Optional[int]:
    """Return the declared float[N] dim of vec_table, or None if absent."""
    sql = _table_ddl(db, vec_table)
    if not sql or "float[" not in sql:
        return None
    try:
        return int(sql.split("float[", 1)[1].split("]", 1)[0])
    except (IndexError, ValueError):
        return None


def _current_metric(db, vec_table: str) -> Optional[str]:
    """Return the declared distance metric of vec_table.

    vec0 accepts ``distance_metric=<m>`` on the vector column and defaults to
    L2 when the clause is absent — which is the whole bug this guards against.
    Read it off the table's own DDL so the schema stays the single source of
    truth; returns None only when the table does not exist.
    """
    sql = _table_ddl(db, vec_table)
    if sql is None:
        return None
    lowered = sql.lower()
    if "distance_metric" not in lowered:
        return "l2"  # vec0 default — declared by omission
    tail = lowered.split("distance_metric", 1)[1].lstrip()
    if tail.startswith("="):
        tail = tail[1:].lstrip()
    metric = ""
    for ch in tail:
        if ch.isalnum() or ch == "_":
            metric += ch
        else:
            break
    return metric or "l2"


def ensure_vec_dims(dry_run: bool = False, empty_only: bool = False) -> dict:
    """Realign all vec_* tables with EmbedConfig.dim + the cosine metric.

    ``empty_only=True`` is the safe mode used by the migration runner: only
    re-embed tables whose source table is empty (fresh install). Mismatched
    tables with data are reported but left alone so a missing embed service
    can't wipe their vectors. The manual CLI run repairs the rest later.

    ``empty_only`` bounds RE-EMBEDS, not repairs. A metric-only fix copies
    existing vectors and never calls the embed service, so it is applied even
    with rows present — deferring it would leave recall broken for exactly
    the installs that have data to recall.
    """
    from okuro.db import get_db
    from okuro.embed.config import load_or_init

    cfg = load_or_init()
    target_dim = cfg.dim
    db = get_db()

    summary = {
        "target_dim": target_dim,
        "tier": cfg.tier,
        "model": cfg.model_id,
        "dry_run": dry_run,
        "empty_only": empty_only,
        "tables": [],
    }

    # Lazy embed import — repair script also serves as a hardware check; if
    # the embed service is down we still want the dim audit to print.
    embed_one = None
    to_bytes = None
    if not dry_run:
        from okuro.embed.client import embed_one as _emb, to_bytes as _tb
        embed_one = _emb
        to_bytes = _tb

    for spec in SPECS:
        entry = {"vec_table": spec.vec_table}
        cur = _current_dim(db, spec.vec_table)
        entry["current_dim"] = cur

        # Source row count (independent of vec table state).
        try:
            src_n = db.fetchone(
                f"SELECT COUNT(*) AS n FROM ({spec.source_sql})"
            )["n"]
        except Exception as exc:
            entry["error"] = f"source query failed: {exc}"
            summary["tables"].append(entry)
            continue
        entry["source_rows"] = src_n
        cur_metric = _current_metric(db, spec.vec_table)
        entry["current_metric"] = cur_metric
        cur_part = _current_partition(db, spec.vec_table)
        entry["current_partition"] = cur_part

        dim_ok = cur == target_dim
        metric_ok = cur_metric == TARGET_METRIC
        part_ok = cur_part == spec.partition

        if dim_ok and metric_ok and part_ok:
            entry["action"] = "skip (already aligned)"
            summary["tables"].append(entry)
            continue

        # Metric-only repair: the stored vectors are already correct at the
        # right dim — only the table's declared metric was wrong. Copy them
        # across verbatim. No embed service, no re-embed, no data loss, so
        # empty_only does NOT defer this (unlike a dim change, which must
        # re-embed and therefore needs the service up).
        if dim_ok and not (metric_ok and part_ok):
            changes = []
            if not metric_ok:
                changes.append(f"metric {cur_metric}→{TARGET_METRIC}")
            if not part_ok:
                changes.append(f"partition {cur_part}→{spec.partition}")
            entry["action"] = (
                f"drop+recreate ({', '.join(changes)}) "
                f"+ copy {src_n} vectors (no re-embed)"
            )
            summary["tables"].append(entry)
            if dry_run:
                continue
            _rebuild_shape_only(db, spec, target_dim, entry)
            continue

        if empty_only and src_n > 0:
            entry["action"] = (
                f"defer ({cur}→{target_dim}, {src_n} rows — run "
                f"`python -m okuro.embed.repair` when embed service is up)"
            )
            summary["tables"].append(entry)
            continue

        entry["action"] = (
            f"drop+recreate ({cur}→{target_dim}, metric {cur_metric}→"
            f"{TARGET_METRIC}) + backfill {src_n} rows"
        )
        summary["tables"].append(entry)

        if dry_run:
            continue

        # Drop + recreate at target dim. vec0 declares dim AND metric at
        # CREATE time — neither can be ALTERed later.
        with db.write():
            db.execute(f"DROP TABLE IF EXISTS {spec.vec_table}")
            db.execute(
                _create_vec_sql(spec.vec_table, target_dim, spec.partition)
            )

        # Backfill row-by-row. Bulk insert isn't an option because each row
        # needs an embed_one() round-trip. Embedding is the bottleneck
        # anyway (HTTP to 13334 → GPU), so per-row commits are negligible.
        rows = db.fetchall(spec.source_sql)
        wrote = 0
        skipped = 0
        for row in rows:
            text = spec.text_fn(dict(row))
            if not text:
                skipped += 1
                continue
            try:
                vec = embed_one(text)
                vec_bytes = to_bytes(vec)
            except Exception as exc:
                logger.warning(
                    "%s: embed failed for id=%s (%s)",
                    spec.vec_table, row["id"], exc,
                )
                skipped += 1
                continue
            try:
                if spec.partition:
                    db.execute(
                        f"INSERT INTO {spec.vec_table} "
                        f"(id, {spec.partition}, embedding) VALUES (?, ?, ?)",
                        (
                            row["id"],
                            partition_value(dict(row).get(spec.partition)),
                            vec_bytes,
                        ),
                    )
                else:
                    db.execute(
                        f"INSERT INTO {spec.vec_table} (id, embedding) "
                        f"VALUES (?, ?)",
                        (row["id"], vec_bytes),
                    )
                db.conn.commit()
                wrote += 1
            except Exception as exc:
                logger.warning(
                    "%s: insert failed for id=%s (%s)",
                    spec.vec_table, row["id"], exc,
                )
                skipped += 1

        entry["backfilled"] = wrote
        entry["skipped"] = skipped

    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Realign okuro vec_* tables with the active embedding tier.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan without dropping or backfilling anything.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-row logging (warnings still print).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = ensure_vec_dims(dry_run=args.dry_run)

    print()
    print(f"Target tier: {result['tier']} ({result['model']}, dim={result['target_dim']})")
    print(f"Mode:        {'DRY-RUN' if result['dry_run'] else 'LIVE'}")
    print()
    print(f"{'TABLE':<26} {'CUR':>5} {'METRIC':>7} {'SRC':>6}  ACTION")
    print("-" * 96)
    for t in result["tables"]:
        cur = t.get("current_dim")
        src = t.get("source_rows", "-")
        metric = t.get("current_metric") or "-"
        action = t.get("action") or t.get("error", "?")
        note = ""
        if "backfilled" in t:
            note = f"  [+{t['backfilled']} -{t['skipped']}]"
        elif "copied" in t:
            note = f"  [copied {t['copied']}]"
        print(
            f"{t['vec_table']:<26} "
            f"{cur if cur is not None else '-':>5} "
            f"{metric:>7} "
            f"{src:>6}  {action}{note}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
