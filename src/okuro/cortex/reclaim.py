# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cortex index hygiene — vec-delete on tombstone, orphan purge, reconcile-on-exclude.
# index:
#   imports
#   def vec_delete_ids
#   def tombstone_and_purge
#   def purge_orphan_vectors
#   def reconcile_excluded
#   def reclaim
# AGENT_HEADER_END -->
"""Cortex index-hygiene primitives (Blueprint Phase 0 — F1/F2/F3/F4).

A single source of truth for keeping the cortex KNN index honest:

* **F1 — orphan vectors.** ``cortex_prune_stale`` historically tombstoned
  ``cortex_docs`` rows but never deleted the matching ``vec_cortex`` row, so
  dead vectors accumulated forever (audit: 784k = 64.5% of the index). The
  brute-force KNN scans every one of them on every query.
* **F2/F3 — newly-excluded paths.** When the deny rules change (worktrees,
  data-exhaust JSON) the reindex path skips those files going forward but
  never purges rows already indexed under the old rules.

These helpers are intentionally connection-level functions (not VectorStore
methods) so the daemon prune handler, the ``okuro cortex reclaim`` CLI, and
the periodic maintenance task all share one implementation. Each is idempotent
and safe to re-run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


def _vec_table_exists(db) -> bool:
    """True if the ``vec_cortex`` virtual table has been created.

    Migrations leave ``vec_cortex`` to VectorStore at runtime, so a fresh
    install can run the prune handler before the table exists. Every vec-delete
    path guards on this so hygiene never crashes on a vec-less DB.
    """
    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_cortex'"
    )
    return row is not None


# ---------------------------------------------------------------------------
# Low-level: vec_cortex deletion
# ---------------------------------------------------------------------------

def vec_delete_ids(db, doc_ids: Iterable[str]) -> int:
    """Delete the ``vec_cortex`` rows for ``doc_ids``. Returns rows removed.

    The reusable primitive every tombstone/exclude path funnels through so the
    vector delete is consistent (and never forgotten — the F1 root cause).
    Batched to keep the writer transaction small. Caller owns commit policy;
    we run inside the caller's transaction when one is open, else autocommit.
    No-op if ``vec_cortex`` doesn't exist yet (fresh install).
    """
    ids = [d for d in doc_ids if d]
    if not ids or not _vec_table_exists(db):
        return 0
    removed = 0
    BATCH = 500
    for i in range(0, len(ids), BATCH):
        chunk = ids[i : i + BATCH]
        placeholders = ",".join("?" * len(chunk))
        cur = db.execute(
            f"DELETE FROM vec_cortex WHERE id IN ({placeholders})", tuple(chunk)
        )
        # sqlite-vec vec0 reports rowcount on DELETE; fall back to len(chunk).
        rc = getattr(cur, "rowcount", -1)
        removed += rc if rc and rc >= 0 else len(chunk)
    return removed


# ---------------------------------------------------------------------------
# F1 — tombstone + vec delete
# ---------------------------------------------------------------------------

def tombstone_and_purge(db, file_paths: Iterable[str]) -> dict:
    """Tombstone every ``cortex_docs`` row for ``file_paths`` AND delete its
    ``vec_cortex`` row.

    Soft-deletes the doc (preserves history, matches ``cortex_prune_stale``
    semantics) but hard-deletes the vector so the dead row stops bloating the
    KNN scan. Only touches docs not already tombstoned, so re-running is a
    no-op. Returns ``{tombstoned, vectors_deleted}``.
    """
    paths = [p for p in file_paths if p]
    if not paths:
        return {"tombstoned": 0, "vectors_deleted": 0}

    tombstoned = 0
    vectors_deleted = 0
    with db.write():
        for fp in paths:
            ids = [
                r["id"]
                for r in db.fetchall(
                    "SELECT id FROM cortex_docs "
                    "WHERE file_path = ? AND deleted_at IS NULL",
                    (fp,),
                )
            ]
            if not ids:
                continue
            db.execute(
                "UPDATE cortex_docs SET deleted_at = datetime('now') "
                "WHERE file_path = ? AND deleted_at IS NULL",
                (fp,),
            )
            tombstoned += len(ids)
            vectors_deleted += vec_delete_ids(db, ids)
    return {"tombstoned": tombstoned, "vectors_deleted": vectors_deleted}


# ---------------------------------------------------------------------------
# F1 — orphan vector purge
# ---------------------------------------------------------------------------

def count_orphan_vectors(db) -> int:
    """Count ``vec_cortex`` rows whose ``cortex_docs`` row is missing OR
    tombstoned — i.e. dead weight in the KNN scan."""
    if not _vec_table_exists(db):
        return 0
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM vec_cortex v "
        "LEFT JOIN cortex_docs d ON d.id = v.id "
        "WHERE d.id IS NULL OR d.deleted_at IS NOT NULL"
    )
    return row["c"] if row else 0


def count_partition_drift(db) -> int:
    """Count live ``vec_cortex`` rows whose partition disagrees with their doc.

    A vector's ``project`` partition and its ``cortex_docs.project`` are set
    together at index time, but a later retag rewrites only the doc side
    (``roots.tag_untagged_docs`` UPDATEs cortex_docs.project; sqlite-vec cannot
    UPDATE a partition key, so the vector is frozen). The result is a vector
    reachable by NEITHER project's scoped search — the July 2026 in-tree
    clone drift (449 rows). This is a detector, not a healer: healing
    is a delete+reindex of the affected root (no in-place partition UPDATE
    exists). Returns 0 on a v1-shape table (no partition key to drift)."""
    if not _vec_table_exists(db) or not _vec_has_partition_key(db):
        return 0
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM vec_cortex v "
        "JOIN cortex_docs d ON d.id = v.id "
        "WHERE d.deleted_at IS NULL AND v.project != d.project"
    )
    return row["c"] if row else 0


def purge_orphan_vectors(db) -> int:
    """Delete every orphan ``vec_cortex`` row (missing or tombstoned doc).

    The direct fix for the 64.5%-dead index. Idempotent: a second run finds
    nothing. Returns the number of vectors removed.
    """
    if not _vec_table_exists(db):
        return 0
    orphan_ids = [
        r["id"]
        for r in db.fetchall(
            "SELECT v.id AS id FROM vec_cortex v "
            "LEFT JOIN cortex_docs d ON d.id = v.id "
            "WHERE d.id IS NULL OR d.deleted_at IS NOT NULL"
        )
    ]
    if not orphan_ids:
        return 0
    with db.write():
        return vec_delete_ids(db, orphan_ids)


# ---------------------------------------------------------------------------
# F5 — repack vec_cortex (NULL-partition bloat reclaim)
# ---------------------------------------------------------------------------

# sqlite-vec vec0 default chunk size (slots per chunk). A healthy table packs
# ~this many vectors per chunk; a NULL-partition table holds ~1 (proven).
_VEC_CHUNK_SLOTS = 1024


def _vec_has_partition_key(db) -> bool:
    """True if vec_cortex is the v2 partition-keyed shape (has a ``project``
    partition key). A v1-shape table (id, embedding) has no partition key and
    therefore cannot exhibit the NULL-partition bloat — repack is a no-op there.
    """
    row = db.fetchone(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_cortex'"
    )
    return bool(row) and "partition key" in (row["sql"] or "").lower()


def vec_chunk_stats(db) -> dict:
    """Measure vec_cortex chunk packing — the NULL-partition bloat signal.

    Returns ``{chunks, live, packed_min, bloat_ratio, null_partition}`` where
    ``bloat_ratio = chunks / packed_min`` (1.0 = perfectly packed; ~1024 = one
    vector per chunk, the 25GB pathology). ``packed_min`` is the chunk count an
    ideally-packed table would use for ``live`` vectors.
    """
    if not _vec_table_exists(db):
        return {"chunks": 0, "live": 0, "packed_min": 0, "bloat_ratio": 1.0,
                "null_partition": 0}
    chunks = db.fetchone(
        "SELECT COUNT(*) AS c FROM vec_cortex_vector_chunks00"
    )["c"]
    live = _vec_count(db)
    # v1-shape tables have no `project` column — no partition, no NULL bloat.
    if _vec_has_partition_key(db):
        null_rows = db.fetchone(
            "SELECT COUNT(*) AS c FROM vec_cortex WHERE project IS NULL"
        )
        null_partition = null_rows["c"] if null_rows else 0
    else:
        null_partition = 0
    # The floor is PER PARTITION, not global. vec0 never mixes two partition
    # values in one chunk, so a perfectly packed partitioned table still needs
    # sum(ceil(rows_p / slots)) chunks — 37 partitions of 3 vectors each cost 37
    # chunks, not 1. The old global `ceil(live/slots)` understated the floor by
    # the partition count and only looked right on vec_cortex, where 150k
    # vectors over 37 partitions make the two formulas nearly equal.
    #
    # That difference is not cosmetic: measured against the four SPECS tables
    # AFTER a correct repack, the global formula yields bloat ratios of 9.8,
    # 13.2, 266.0 and 95.0 against a 4.0 threshold — permanently TRUE, so a
    # generalised health tick would repack them on every pass, forever, with
    # nothing to gain. Fix the floor before generalising the detector.
    if _vec_has_partition_key(db):
        per_partition = db.fetchall(
            "SELECT COUNT(*) AS n FROM vec_cortex GROUP BY project"
        )
        packed_min = max(
            1, sum(-(-r["n"] // _VEC_CHUNK_SLOTS) for r in per_partition)
        )
    else:
        packed_min = max(1, -(-live // _VEC_CHUNK_SLOTS))  # ceil
    bloat_ratio = (chunks / packed_min) if packed_min else 1.0
    return {"chunks": chunks, "live": live, "packed_min": packed_min,
            "bloat_ratio": round(bloat_ratio, 1), "null_partition": null_partition}


def needs_repack(db, *, min_ratio: float = 4.0, min_chunks: int = 16) -> bool:
    """True when vec_cortex is bloated enough to warrant a repack.

    Gated so a healthy, well-packed table is never rebuilt needlessly: requires
    BOTH a high bloat ratio AND enough chunks that the win is real.
    """
    if not _vec_has_partition_key(db):
        return False  # v1 shape can't have NULL-partition bloat
    s = vec_chunk_stats(db)
    return s["chunks"] >= min_chunks and s["bloat_ratio"] >= min_ratio


def _vec_embedding_dim(db) -> int:
    """Resolve the stored embedding dimension (cortex_meta, else a sample blob)."""
    row = db.fetchone(
        "SELECT value FROM cortex_meta WHERE key = 'embedding_dim'"
    )
    if row and str(row["value"]).isdigit():
        return int(row["value"])
    sample = db.fetchone("SELECT embedding FROM vec_cortex LIMIT 1")
    if sample and sample["embedding"] is not None:
        return len(sample["embedding"]) // 4  # float32
    return 0


def repack_vec_cortex(db) -> dict:
    """Rebuild vec_cortex so vectors pack into dense chunks — the NULL-partition
    bloat fix.

    A NULL partition key makes vec0 allocate one near-empty chunk per row
    (~1024x bloat). VACUUM can't help: those chunk pages are live shadow-table
    rows, so ``freelist_count`` is 0. The only reclaim is to rebuild the table
    with a non-NULL partition for unscoped rows, which lets vec0 pack them; the
    freed chunk pages then go to the freelist and a follow-up VACUUM returns
    them to the OS.

    Reuses the stored vectors (no re-embedding) and preserves each row's id and
    real project (NULL -> sentinel). Returns ``{chunks_before, chunks_after,
    vectors, repacked}``. Caller runs VACUUM afterwards (it cannot run inside a
    transaction).
    """
    from .vectorstore import NO_PROJECT_PARTITION, _vec_schema

    if not _vec_table_exists(db) or not _vec_has_partition_key(db):
        # No table, or v1-shape (no partition key) — nothing to repack.
        return {"chunks_before": 0, "chunks_after": 0, "vectors": 0,
                "repacked": False}

    before = vec_chunk_stats(db)
    dim = _vec_embedding_dim(db)
    if not dim:
        return {"chunks_before": before["chunks"], "chunks_after": before["chunks"],
                "vectors": before["live"], "repacked": False,
                "note": "could not resolve embedding dim"}

    # Pull every vector out (id, coalesced partition, raw bytes). Held in memory
    # briefly — at dim*4 bytes/vector this is a few hundred MB even for ~100k
    # vectors, and the bloated tables we target hold far fewer.
    rows = db.fetchall("SELECT id, project, embedding FROM vec_cortex")
    payload = [
        (r["id"], r["project"] if r["project"] else NO_PROJECT_PARTITION,
         r["embedding"])
        for r in rows
    ]

    # vec0 virtual-table DDL is not transactional; DROP/CREATE run in autocommit
    # (the connection's default), then the bulk insert runs in one writer txn
    # (db.executemany opens its own) so the vectors pack into dense chunks.
    db.conn.execute("DROP TABLE IF EXISTS vec_cortex")
    db.conn.execute(_vec_schema(dim))
    db.executemany(
        "INSERT INTO vec_cortex (id, project, embedding) VALUES (?, ?, ?)",
        payload,
    )

    after = vec_chunk_stats(db)
    return {"chunks_before": before["chunks"], "chunks_after": after["chunks"],
            "vectors": len(payload), "repacked": True}


# ---------------------------------------------------------------------------
# F2/F3 — reconcile-on-exclude
# ---------------------------------------------------------------------------

@dataclass
class _RootSpec:
    path: Path
    project: Optional[str] = None


def _iter_registered_roots() -> list[_RootSpec]:
    """Best-effort list of registered roots. Empty on any failure so reclaim
    still purges orphans even if the roots table is unavailable."""
    try:
        from .roots import registered_roots

        out: list[_RootSpec] = []
        for r in registered_roots():
            p = Path(str(r.path))
            out.append(_RootSpec(path=p, project=getattr(r, "project", None)))
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("reclaim: could not list registered roots (%s)", exc)
        return []


def find_excluded_live_docs(db, roots: Optional[list[_RootSpec]] = None) -> list[str]:
    """Return distinct live ``file_path``s that now match an exclusion rule.

    Reconciles existing rows against the *current* deny rules (worktrees,
    data-exhaust JSON, per-root overrides). A path is "excluded" if, relative
    to the root it lives under, the root's matcher excludes it OR the
    data-exhaust heuristic flags it. Paths under no registered root are left
    alone (we can't build a matcher without a root anchor — the orphan/
    tombstone passes still clean those if their files vanish).
    """
    from .exclusions import build_matcher, is_data_exhaust

    if roots is None:
        roots = _iter_registered_roots()
    if not roots:
        return []

    # Build one matcher per root, longest-path-first so nested roots win.
    roots_sorted = sorted(
        roots, key=lambda r: len(str(r.path)), reverse=True
    )
    matchers: list[tuple[Path, object]] = []
    for r in roots_sorted:
        try:
            matchers.append((r.path, build_matcher(r.path)))
        except Exception:  # noqa: BLE001
            continue

    rows = db.fetchall(
        "SELECT DISTINCT file_path FROM cortex_docs WHERE deleted_at IS NULL"
    )
    excluded: list[str] = []
    for row in rows:
        fp = row["file_path"]
        if not fp:
            continue
        p = Path(fp)
        for root_path, matcher in matchers:
            try:
                rel = p.relative_to(root_path)
            except ValueError:
                continue
            # Found the owning root — decide and stop.
            if matcher.is_excluded(rel) or is_data_exhaust(p):
                excluded.append(fp)
            break
    return excluded


def reconcile_excluded(db, roots: Optional[list[_RootSpec]] = None) -> dict:
    """Tombstone + vec-delete live docs whose path now matches the deny rules.

    This is what makes the worktree / data-JSON deny changes retroactive:
    rows indexed under the old rules get cleaned over time. Returns
    ``{tombstoned, vectors_deleted}``.
    """
    paths = find_excluded_live_docs(db, roots=roots)
    if not paths:
        return {"tombstoned": 0, "vectors_deleted": 0}
    return tombstone_and_purge(db, paths)


# ---------------------------------------------------------------------------
# G12 — hard-purge aged tombstones (dead-data reclaim)
# ---------------------------------------------------------------------------

def count_tombstoned_docs(db, older_than_days: int = 0) -> int:
    """Count soft-deleted cortex_docs older than the retention window."""
    cutoff = f"datetime('now', '-{int(older_than_days)} days')"
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM cortex_docs "
        f"WHERE deleted_at IS NOT NULL AND deleted_at < {cutoff}"
    )
    return row["c"] if row else 0


def purge_tombstoned_docs(
    db,
    older_than_days: int = 0,
    batch_size: int = 5000,
    dry_run: bool = False,
) -> int:
    """Hard-delete soft-deleted cortex_docs beyond the retention window.

    Soft-deletes (``deleted_at`` set) stay in cortex_docs forever — and, because
    the ``cortex_fts_au`` trigger re-inserts the row on every UPDATE, they stay
    in cortex_fts too (measured 2026-07-16: 1.12M tombstones = 89% of
    cortex_docs, 9.1x the live-doc count in FTS). Query correctness is
    unaffected (every read filters ``deleted_at``), but the mass is pure cost.

    A hard DELETE fires ``cortex_fts_ad``, which removes the row from the FTS
    index — so this is the one operation that reclaims the FTS bloat. Batched
    so the writer lock is released between chunks (the daemon keeps serving).
    ``older_than_days=0`` purges every tombstone; a positive window keeps
    recent deletions. Space returns to the OS only after a later VACUUM; the
    FTS scan cost drops immediately. Returns the number of rows purged (or the
    candidate count under ``dry_run``)."""
    total = count_tombstoned_docs(db, older_than_days)
    if dry_run or total == 0:
        return total

    cutoff = f"datetime('now', '-{int(older_than_days)} days')"
    purged = 0
    while True:
        ids = [
            r["id"]
            for r in db.fetchall(
                "SELECT id FROM cortex_docs "
                f"WHERE deleted_at IS NOT NULL AND deleted_at < {cutoff} "
                "LIMIT ?",
                (batch_size,),
            )
        ]
        if not ids:
            break
        placeholders = ",".join("?" * len(ids))
        with db.write():
            # Defensive: tombstoned docs are usually already orphan-purged, but
            # delete any lingering vectors before the doc rows go.
            vec_delete_ids(db, ids)
            db.execute(
                f"DELETE FROM cortex_docs WHERE id IN ({placeholders})",
                tuple(ids),
            )
        purged += len(ids)
    return purged


# ---------------------------------------------------------------------------
# Top-level: reclaim
# ---------------------------------------------------------------------------

@dataclass
class ReclaimReport:
    """Before/after counts for a reclaim pass."""

    vectors_before: int = 0
    docs_live_before: int = 0
    orphans_before: int = 0
    orphans_purged: int = 0
    excluded_tombstoned: int = 0
    excluded_vectors_deleted: int = 0
    tombstones_purged: int = 0
    vectors_after: int = 0
    docs_live_after: int = 0
    orphans_after: int = 0
    chunks_before: int = 0
    chunks_after: int = 0
    bloat_ratio_before: float = 1.0
    repacked: bool = False
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def vectors_reclaimed(self) -> int:
        return max(0, self.vectors_before - self.vectors_after)

    # Rough on-disk estimate: each vec row ≈ dim * 4 bytes + key/index overhead.
    def reclaimed_bytes_estimate(self, embedding_dim: int) -> int:
        per_vec = embedding_dim * 4 + 64  # float32 payload + key/index slack
        return self.vectors_reclaimed * per_vec

    def as_dict(self, embedding_dim: int = 0) -> dict:
        d = {
            "dry_run": self.dry_run,
            "vectors_before": self.vectors_before,
            "vectors_after": self.vectors_after,
            "docs_live_before": self.docs_live_before,
            "docs_live_after": self.docs_live_after,
            "orphans_before": self.orphans_before,
            "orphans_after": self.orphans_after,
            "orphans_purged": self.orphans_purged,
            "excluded_tombstoned": self.excluded_tombstoned,
            "excluded_vectors_deleted": self.excluded_vectors_deleted,
            "tombstones_purged": self.tombstones_purged,
            "vectors_reclaimed": self.vectors_reclaimed,
            "chunks_before": self.chunks_before,
            "chunks_after": self.chunks_after,
            "bloat_ratio_before": self.bloat_ratio_before,
            "repacked": self.repacked,
            "notes": self.notes,
        }
        if embedding_dim:
            d["reclaimed_bytes_estimate"] = self.reclaimed_bytes_estimate(
                embedding_dim
            )
        return d


def _vec_count(db) -> int:
    if not _vec_table_exists(db):
        return 0
    row = db.fetchone("SELECT COUNT(*) AS c FROM vec_cortex")
    return row["c"] if row else 0


def _live_doc_count(db) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS c FROM cortex_docs WHERE deleted_at IS NULL"
    )
    return row["c"] if row else 0


def reclaim(
    db=None,
    *,
    dry_run: bool = False,
    reconcile: bool = True,
    repack: bool = False,
    purge_tombstones: bool = False,
    tombstone_retention_days: int = 0,
    roots: Optional[list[_RootSpec]] = None,
) -> ReclaimReport:
    """Self-heal the cortex index against the install's own DB.

    Steps (all idempotent):
      1. Reconcile-on-exclude — tombstone + vec-delete live docs whose path
         now matches the deny rules (worktrees, data-JSON). Optional via
         ``reconcile`` (the periodic daemon variant runs it conservatively).
      2. Purge orphan vectors — delete vec rows whose doc is missing or
         tombstoned (the 64.5%-dead-index fix). This also sweeps up the
         vectors freed by step 1.
      3. Repack vec_cortex (opt-in via ``repack``) — rebuild the table so
         vectors pack into dense chunks, fixing NULL-partition bloat that
         VACUUM alone can't reclaim, then VACUUM to return pages to the OS.
         Off by default (heavy + rewrites the whole DB) — the CLI ``--repack``
         flag enables it; the periodic daemon does not.

    ``dry_run=True`` reports the same before/after deltas without writing.
    Returns a :class:`ReclaimReport`.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    rep = ReclaimReport(dry_run=dry_run)
    rep.vectors_before = _vec_count(db)
    rep.docs_live_before = _live_doc_count(db)
    rep.orphans_before = count_orphan_vectors(db)
    _stats = vec_chunk_stats(db)
    rep.chunks_before = _stats["chunks"]
    rep.bloat_ratio_before = _stats["bloat_ratio"]

    # Partition drift is silent (a vector reachable by no scoped search) and
    # reclaim cannot heal it in place. Surface a count so a recurrence — a new
    # in-tree git clone under a local root, then a retag — becomes visible
    # instead of accumulating like the 449-row drift did.
    _drift = count_partition_drift(db)
    if _drift:
        rep.notes.append(
            f"WARNING: {_drift} vector(s) have a partition that disagrees with "
            "their doc's project — unreachable by scoped search. Heal by "
            "reindexing the affected root (no in-place partition UPDATE exists)."
        )

    if dry_run:
        # Measure what *would* happen without mutating.
        excluded = (
            find_excluded_live_docs(db, roots=roots) if reconcile else []
        )
        rep.excluded_tombstoned = len(excluded)
        # Orphans-now + the vectors those excluded docs would free ≈ purge size.
        rep.orphans_purged = rep.orphans_before
        rep.vectors_after = rep.vectors_before  # nothing written
        rep.docs_live_after = rep.docs_live_before
        rep.orphans_after = rep.orphans_before
        rep.chunks_after = rep.chunks_before
        would_repack = repack and needs_repack(db)
        rep.notes.append(
            f"dry-run: would tombstone {len(excluded)} excluded live doc(s) "
            f"and purge {rep.orphans_before} orphan vector(s)"
            + (f"; would repack vec_cortex (bloat ratio "
               f"{rep.bloat_ratio_before}x, {rep.chunks_before} chunks)"
               if would_repack else "")
        )
        if purge_tombstones:
            rep.tombstones_purged = count_tombstoned_docs(
                db, tombstone_retention_days)
            rep.notes.append(
                f"dry-run: would purge {rep.tombstones_purged} tombstoned "
                f"doc(s) older than {tombstone_retention_days}d"
            )
        return rep

    if reconcile:
        rec = reconcile_excluded(db, roots=roots)
        rep.excluded_tombstoned = rec["tombstoned"]
        rep.excluded_vectors_deleted = rec["vectors_deleted"]

    rep.orphans_purged = purge_orphan_vectors(db)

    # Hard-purge aged tombstones (opt-in). Reclaims the FTS bloat the soft-delete
    # triggers leave behind. Off by default — it's an irreversible delete of the
    # dead-data mass, run deliberately (CLI flag / scheduled), not on every pass.
    if purge_tombstones:
        rep.tombstones_purged = purge_tombstoned_docs(
            db, older_than_days=tombstone_retention_days
        )
        if rep.tombstones_purged:
            rep.notes.append(
                f"purged {rep.tombstones_purged} tombstoned doc(s) older than "
                f"{tombstone_retention_days}d (FTS rows freed via cascade; "
                "VACUUM to return pages to the OS)"
            )

    # Repack the vec table (opt-in) when it's bloated, then VACUUM to return the
    # freed pages to the OS. VACUUM cannot run inside a transaction, so it runs
    # after the repack's writer txn has committed.
    if repack and needs_repack(db):
        res = repack_vec_cortex(db)
        rep.repacked = bool(res.get("repacked"))
        if rep.repacked:
            db.conn.execute("VACUUM")
            rep.notes.append(
                f"repacked vec_cortex: {res['chunks_before']} -> "
                f"{res['chunks_after']} chunks for {res['vectors']} vectors; "
                "VACUUM reclaimed freed pages"
            )

    rep.vectors_after = _vec_count(db)
    rep.docs_live_after = _live_doc_count(db)
    rep.orphans_after = count_orphan_vectors(db)
    rep.chunks_after = vec_chunk_stats(db)["chunks"]
    return rep
