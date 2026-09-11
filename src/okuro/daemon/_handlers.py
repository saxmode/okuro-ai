# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Thin wrapper handlers that adapt existing functions for no-arg daemon invocation.
# index:
#   imports
#   def _okuro_root
#   def refresh_context
#   def refresh_cortex
#   def session_hygiene
#   def memory_hygiene
#   def thoughts_aging
#   def db_vacuum
#   def profile_enrich
#   def telemetry_rotate
#   def proactive_scan
#   def proactive_advise
#   def inbox_reduce
#   def inbox_heartbeat
#   def commitment_infer
#   def reconcile_orphaned_engines
# AGENT_HEADER_END -->
"""Thin wrapper handlers that adapt existing functions for no-arg daemon invocation.

A2 — daemon owns DB hygiene.

  session_hygiene   every 5m   close orphaned sessions + aggregate compliance
  memory_hygiene    hourly     decay + prune superseded/stale + delete doc-dumps
  thoughts_aging    daily 02   dismiss stale opens, downgrade stuck ones
  db_vacuum         Sun 03     VACUUM + wal_checkpoint(TRUNCATE)
  telemetry_rotate  daily 03   rotate usage.jsonl past 10MB + prune old archives
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from okuro.db.engine import okuro_home

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cortex re-index convergence detector
# ---------------------------------------------------------------------------
# A healthy root re-embeds only what changed, so its indexed count decays to ~0.
# A count that stays CONSTANT and non-zero across cycles is the signature of a
# cache-miss loop: the same files fail the short-circuit forever (2026-07-17 —
# one workspace repo re-embedded exactly 1041 files every cycle for hours because it
# and its 19 nested roots re-tagged each other's files; it was noticed only via
# GPU noise). Constant beats "high": real churn varies, a loop does not.
#
# On detection this arms OKURO_CORTEX_DEBUG_REINDEX for exactly one cycle, so
# the per-file reasons are captured automatically while the loop is live and the
# evidence does not depend on a human being awake to flip a flag. It disarms
# itself after that cycle — the log is one line per re-indexed file, too costly
# to leave on.
_REINDEX_HISTORY: dict[str, list[int]] = {}
_REINDEX_HISTORY_LEN = 3
_REINDEX_LOOP_FLOOR = 20  # below this, a constant count is not worth the noise
_REINDEX_DEBUG_ENV = "OKURO_CORTEX_DEBUG_REINDEX"

# The same convergence guard, for the ENRICH half of the cortex tick.
#
# Only the re-index half was watched, and the re-index half is free — it is
# local embedding. The enrich half is the one that spends model calls, and it
# ran unwatched for months: 742 of 785 daily enrichments were the same six
# directories, one of which holds a single file and was enriched 126 times in
# 24 hours. Every one of those calls SUCCEEDED, so FAILURE_RETRY_BUDGET (which
# counts only failures) could never see it.
#
# A constant non-zero enrich count across cycles has exactly one cause: the
# purpose is being written and then reverted. Nothing legitimate produces it.
_ENRICH_HISTORY: dict[str, list[int]] = {}
_ENRICH_HISTORY_LEN = 3
# Lower floor than re-index: an enrich is a paid model call, so even a small
# steady count is worth flagging.
_ENRICH_LOOP_FLOOR = 3
_reindex_debug_armed_for: str | None = None


def _check_enrich_convergence(project: str, enriched: int) -> None:
    """Flag a root whose LLM enrichment count is not converging.

    Enrichment is inherently self-terminating: once a purpose is good, the
    enricher skips it. So a count that stays constant means something is
    reverting the work between cycles — the loop this detector exists to
    catch. Unlike the re-index guard there is no reason capture to arm; the
    count itself is the diagnosis.
    """
    history = _ENRICH_HISTORY.setdefault(project, [])
    history.append(enriched)
    del history[:-_ENRICH_HISTORY_LEN]

    if (
        len(history) == _ENRICH_HISTORY_LEN
        and enriched >= _ENRICH_LOOP_FLOOR
        and len(set(history)) == 1
    ):
        log.error(
            "cortex enrich loop [%s]: enriched exactly %d files for %d cycles "
            "running. Enrichment is self-terminating, so a constant count means "
            "the purpose is being reverted between ticks — check that "
            "scan_directory is preserving entries whose generated_by starts "
            "with 'bridge/'. Every one of these is a paid model call.",
            project,
            enriched,
            _ENRICH_HISTORY_LEN,
        )


def _check_reindex_convergence(project: str, indexed: int) -> None:
    """Flag a root whose re-embed count is not converging, and self-arm the
    per-file reason capture for the next cycle."""
    global _reindex_debug_armed_for

    history = _REINDEX_HISTORY.setdefault(project, [])
    history.append(indexed)
    del history[:-_REINDEX_HISTORY_LEN]

    looping = (
        len(history) == _REINDEX_HISTORY_LEN
        and indexed >= _REINDEX_LOOP_FLOOR
        and len(set(history)) == 1
    )

    if _reindex_debug_armed_for == project:
        # The reasons for this root were just captured — disarm and point at them.
        os.environ.pop(_REINDEX_DEBUG_ENV, None)
        _reindex_debug_armed_for = None
        log.error(
            "cortex loop [%s]: per-file reasons captured above — group by "
            "'reason=' to see the cause. Re-index reason capture disarmed.",
            project,
        )
        return

    if looping and not os.environ.get(_REINDEX_DEBUG_ENV):
        os.environ[_REINDEX_DEBUG_ENV] = "1"
        _reindex_debug_armed_for = project
        log.error(
            "cortex loop [%s]: re-embedded exactly %d files for %d cycles "
            "running — a constant count means a cache-miss loop, not churn. "
            "Arming re-index reason capture for the next cycle.",
            project,
            indexed,
            _REINDEX_HISTORY_LEN,
        )


def _okuro_root() -> str:
    """Resolve the okuro project root."""
    try:
        import okuro
        return str(Path(okuro.__file__).resolve().parent.parent.parent)
    except Exception:
        import os
        return os.getcwd()


def refresh_context() -> list[str]:
    """Regenerate agent context files to canonical paths (~/.claude/, ~/.okuro/, etc.)."""
    from okuro.sense.providers import generate_all
    return generate_all()


def refresh_cortex() -> dict:
    """Scan for new/changed files, update headers/sidecars, enrich purposes, rebuild index.

    Runs incrementally via git markers — only touches files changed since last scan.
    Walks every registered root (OKURO_ROOT + every active project with a path),
    so cortex covers multi-repo workspaces, not only one root.
    """
    from okuro.cortex.roots import registered_roots

    result = {
        "headers_updated": 0,
        "scanned": 0,
        "indexed": 0,
        "enriched": 0,
        "roots": [],
    }

    for root_rec in registered_roots():
        root = root_rec.path
        result["roots"].append(
            {"path": str(root), "project": root_rec.project}
        )

        # 1. Auto-generate AGENT_HEADERs / sidecars for new/changed files
        try:
            from okuro.cortex.scanner import scan_directory
            _results, updated = scan_directory(
                root, incremental=True, dry_run=False
            )
            result["headers_updated"] += updated
            log.info(
                "cortex scan [%s]: %d headers/sidecars updated",
                root_rec.project or root.name,
                updated,
            )
        except Exception as e:
            log.warning(
                "cortex header scan failed for %s: %s", root, e
            )

        # 2. Enrich weak-purpose sidecar entries via LLM
        try:
            from okuro.cortex.purpose import enrich_sidecar
            import os

            enriched_total = 0
            for dirpath, _dirnames, _filenames in os.walk(root):
                enrich_result = enrich_sidecar(Path(dirpath), root=root)
                enriched_total += enrich_result["enriched"]
                if enrich_result.get("no_bridge"):
                    break  # No point walking more directories
            result["enriched"] += enriched_total
            _check_enrich_convergence(root_rec.project or root.name, enriched_total)
            if enriched_total:
                log.info(
                    "cortex enrich [%s]: %d purposes improved",
                    root_rec.project or root.name,
                    enriched_total,
                )
        except Exception as e:
            log.warning(
                "cortex enrichment failed for %s: %s", root, e
            )

        # 3. Re-index changed files into vectorstore
        try:
            from okuro.cortex.vectorstore import VectorStore
            vs = VectorStore()
            # index_directory returns (total_seen, indexed) — NOT
            # (indexed, skipped). The old unpacking printed the file count as
            # "indexed" and the re-embed count as "skipped", i.e. exactly
            # backwards.
            scanned, indexed = vs.index_directory(
                root, project=root_rec.project
            )
            result["scanned"] += scanned
            result["indexed"] += indexed
            log.info(
                "cortex index [%s]: %d scanned, %d indexed",
                root_rec.project or root.name,
                scanned,
                indexed,
            )
            _check_reindex_convergence(root_rec.project or root.name, indexed)
        except Exception as e:
            log.warning("cortex index failed for %s: %s", root, e)

    # 4. Reap worktree overlays that no longer earn their space.
    #
    # Deliberately OUTSIDE the per-root loop: an overlay belongs to a branch,
    # not to a root, and running it per-root would evict the same slug N times.
    #
    # Nothing else can do this job. cortex_prune_stale tombstones only files
    # that VANISHED from disk, and a merged-but-still-present worktree never
    # qualifies — which is precisely how 141,738 stale duplicate documents
    # survived indefinitely in 2026. Without this tick an overlay outlives its
    # worktree forever.
    try:
        from okuro.cortex.overlay import reap

        report = reap()
        result["overlays_kept"] = len(report["kept"])
        result["overlays_evicted"] = len(report["evicted"])
        for entry in report["evicted"]:
            log.info(
                "cortex overlay evicted [%s]: %s, %d files",
                entry["slug"], entry["reason"], entry["files"],
            )
    except Exception as e:
        log.warning("cortex overlay reap failed: %s", e)

    return result


# ---------------------------------------------------------------------------
# A2 — daemon-owned hygiene handlers
# ---------------------------------------------------------------------------

def session_hygiene() -> dict:
    """Close orphaned sessions + aggregate provider compliance.

    Runs every 5 min so the 72% orphan rate observed pre-fix cannot
    re-accumulate regardless of agent discipline (session_report skipped).
    """
    from okuro.sense.telemetry import close_orphaned_sessions, aggregate_provider_compliance
    from okuro.sense.agents import close_dead_agents

    # Order matters: reap dead agents first so their still-open sessions
    # cascade-end with end_reason='agent_ended' before the per-session
    # reaper scores them as 'timeout'. This keeps the semantic clean —
    # "session ended because its agent died" vs "session timed out idle".
    agents_closed = close_dead_agents(heartbeat_timeout_min=30)
    orphan_msg = close_orphaned_sessions(timeout_minutes=30)
    compliance_msg = aggregate_provider_compliance(window_days=30)
    log.info(
        "session_hygiene: agents=%d closed | %s | %s",
        agents_closed, orphan_msg, compliance_msg,
    )

    # Orphan / stale MCP SERVER PROCESSES — distinct from the session ROWS the
    # reaper above closes. A server whose client exited (reparented to init) or
    # that is old-and-behind keeps serving frozen code with nobody watching; the
    # 25h windowless-desktop server nobody knew about was exactly this. Report
    # only — killing an MCP server may drop a live session on another terminal,
    # so reaping stays a deliberate human/opt-in action, never a sweep effect.
    server_findings: list[dict] = []
    try:
        from okuro.system.mcp_servers import find_mcp_servers
        servers = find_mcp_servers()
        server_findings = [s for s in servers
                           if s["classification"] in ("orphan", "stale")]
        if server_findings:
            log.warning(
                "session_hygiene: %d MCP server(s) need attention (report-only) — %s",
                len(server_findings),
                "; ".join(
                    f"pid={s['pid']} {s['classification']} age={s['age_hours']}h"
                    for s in server_findings
                ),
            )
    except Exception:  # noqa: BLE001 — a monitoring scan must never fail the sweep
        pass

    return {
        "agents_closed": agents_closed,
        "orphans": orphan_msg,
        "compliance": compliance_msg,
        "mcp_servers_flagged": len(server_findings),
    }


def memory_hygiene() -> dict:
    """Decay, prune, and kill doc-dump noise.

    Three passes:
      1. Decay: `confidence -= 0.1` for memories not accessed in 90d
         (reuses the existing logic from `_memory_hygiene` in
         sense/maintenance.py).
      2. Prune: delete memories where `confidence <= 0.05`, OR SUPERSEDED
         rows (pointed at by a correction whose replacement has stood >7d).
      3. Kill doc-dumps: legacy `_doc_indexing` rows.

    Two correctness fixes, 2026-07-16:
      * The superseded-prune targeted `supersedes IS NOT NULL` — rows that
        CARRY a pointer, i.e. the CORRECTIONS. It deleted the fix and kept the
        error. Measured on the live store: 90 superseded errors sat permanent
        at 0.1 while the delete's real target was the 91 corrections. Now it
        prunes rows that ARE pointed-at, and only once the replacement is >7d
        old (the audit window).
      * Every DELETE orphaned the row's vector — vec_memory has no FK/cascade
        to agent_memory, so nothing removed it, and the orphan kept surfacing
        in vec_search (superseded rows ranked #1 above their replacements in
        the candidate pool). The vectors are now deleted with the rows.
    """
    from okuro.db import get_db

    db = get_db()

    ninety_ago = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    seven_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with db.write():
        cur = db.execute(
            """UPDATE agent_memory SET confidence = MAX(confidence - 0.1, 0.0)
               WHERE last_accessed < ? AND confidence > 0.1""",
            (ninety_ago,),
        )
        decayed = cur.rowcount

        # Resolve the exact id set for each prune BEFORE deleting, so the row
        # and its vector go together and the FK null-out targets the right
        # rows. vec_memory has no cascade to agent_memory (see docstring).
        low_ids = [
            r["id"] for r in db.fetchall(
                "SELECT id FROM agent_memory WHERE confidence <= 0.05")
        ]
        # SUPERSEDED = rows that ARE pointed-at (an error that was replaced),
        # demoted to <=0.1, whose replacement (the correcting row) is >7d old.
        # NOT `supersedes IS NOT NULL`, which is the correction — the inversion
        # this fix removes.
        superseded_ids = [
            r["id"] for r in db.fetchall(
                """SELECT id FROM agent_memory
                   WHERE confidence <= 0.1 AND id IN (
                       SELECT supersedes FROM agent_memory
                       WHERE supersedes IS NOT NULL AND created_at < ?
                   )""",
                (seven_ago,),
            )
        ]
        # Doc-dump rows: the maintenance agent's bracketed-path prefix. The
        # pattern is workspace-agnostic on purpose — any '[<path>/docs/…'
        # row from this producer is a dump, whatever the repo is called.
        docdump_ids = [
            r["id"] for r in db.fetchall(
                """SELECT id FROM agent_memory
                   WHERE source_agent = 'okuro-maintenance'
                     AND content LIKE '[%/docs/%'""")
        ]
        pruned_low = len(low_ids)
        pruned_superseded = len(superseded_ids)
        pruned_docdumps = len(docdump_ids)

        all_ids = list({*low_ids, *superseded_ids, *docdump_ids})

        # Never delete the LAST retraction record of a surviving demoted row.
        #
        # artifact of the old behaviour: deleting a correcting row drops its
        # `supersedes` edge with it. Its target keeps the 0.1 demotion
        # `write_memory` stamped (memory.py:545) but nothing points at it any
        # more, so it can never be pruned either — `superseded_ids` requires a
        # live supersede edge and `low_ids` requires <= 0.05. The row lands in
        # a permanent zombie tier: embedded, FTS-indexed, and filtered out of
        # every read by `min_confidence` (memory.py:846) with the reason for
        # its demotion unrecoverable. Measured 2026-07-18: 84 such rows, 56 of
        # them substantive.
        #
        # Keeping the correction costs one row and preserves WHY the target was
        # retracted. Retention must not be able to strand knowledge silently.
        if all_ids:
            ph_a = ", ".join("?" * len(all_ids))
            keep = {
                r["id"] for r in db.fetchall(
                    f"""SELECT c.id FROM agent_memory c
                        WHERE c.id IN ({ph_a})
                          AND c.supersedes IS NOT NULL
                          -- Only when the target is ACTUALLY at risk: it
                          -- survives this prune AND sits at the demotion
                          -- floor. A target that was later reinforced back
                          -- above the read floor is retrievable on its own,
                          -- so its retraction record is not load-bearing and
                          -- retaining it would just grow the table.
                          AND EXISTS (SELECT 1 FROM agent_memory t
                                      WHERE t.id = c.supersedes
                                        AND t.id NOT IN ({ph_a})
                                        AND t.confidence <= 0.1)
                          AND NOT EXISTS (SELECT 1 FROM agent_memory o
                                          WHERE o.supersedes = c.supersedes
                                            AND o.id != c.id
                                            AND o.id NOT IN ({ph_a}))""",
                    (*all_ids, *all_ids, *all_ids),
                )
            }
            if keep:
                all_ids = [i for i in all_ids if i not in keep]
                low_ids = [i for i in low_ids if i not in keep]
                superseded_ids = [i for i in superseded_ids if i not in keep]
                docdump_ids = [i for i in docdump_ids if i not in keep]
                pruned_low = len(low_ids)
                pruned_superseded = len(superseded_ids)
                pruned_docdumps = len(docdump_ids)

        if all_ids:
            ph = ", ".join("?" * len(all_ids))
            # Null any supersedes FK pointing AT a row we're about to delete,
            # or the self-referential FK aborts the delete.
            db.execute(
                f"UPDATE agent_memory SET supersedes = NULL "
                f"WHERE supersedes IN ({ph})", all_ids)
            db.execute(
                f"DELETE FROM agent_memory WHERE id IN ({ph})", all_ids)
            db.execute(
                f"DELETE FROM vec_memory WHERE id IN ({ph})", all_ids)

    row = db.fetchone("SELECT COUNT(*) AS cnt FROM agent_memory")
    total = row["cnt"] if row else 0

    log.info(
        "memory_hygiene: total=%d decayed=%d pruned_low=%d pruned_superseded=%d pruned_docdumps=%d",
        total, decayed, pruned_low, pruned_superseded, pruned_docdumps,
    )
    return {
        "total": total,
        "decayed": decayed,
        "pruned_low_confidence": pruned_low,
        "pruned_superseded": pruned_superseded,
        "pruned_docdumps": pruned_docdumps,
    }


def thoughts_aging() -> dict:
    """Dismiss surfaced-but-unengaged opens > 30d, flag stuck opens > 14d.

    The dismissal gate is `last_surfaced` — a thought is only eligible once
    the system has actually shown it to the user and 30 days have passed
    without action. Thoughts that were never surfaced stay open forever;
    dismissing them would silently destroy data the user never saw.
    """
    from okuro.db import get_db

    db = get_db()
    thirty_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    fourteen_ago = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    cur = db.execute(
        """UPDATE thoughts SET status = 'dismissed'
           WHERE status = 'open'
             AND last_surfaced IS NOT NULL
             AND last_surfaced < ?""",
        (thirty_ago,),
    )
    dismissed = cur.rowcount

    row = db.fetchone(
        "SELECT COUNT(*) AS cnt FROM thoughts WHERE status = 'open' AND updated_at < ?",
        (fourteen_ago,),
    )
    stuck = row["cnt"] if row else 0

    db.conn.commit()
    log.info("thoughts_aging: dismissed=%d stuck_open=%d", dismissed, stuck)
    return {"dismissed": dismissed, "stuck_open": stuck}


def cortex_prune_stale() -> dict:
    """Tombstone cortex_docs rows whose file_path no longer exists AND delete
    their vec_cortex rows.

    Soft-deletes the doc (sets `deleted_at = now`, preserving history — the
    re-index path resurrects entries when a file reappears) but HARD-deletes
    the matching `vec_cortex` row. Without the vec delete, dead vectors
    accumulate forever and bloat the brute-force KNN scan (audit F1: 784k =
    64.5% of the index were orphans tombstoned-but-not-vec-deleted).

    Search queries filter `WHERE deleted_at IS NULL`, so tombstoned rows
    stop surfacing as soon as this tick runs; the vec delete additionally
    stops them being scanned at all.
    """
    from okuro.cortex.reclaim import tombstone_and_purge
    from okuro.db import get_db

    db = get_db()

    rows = db.fetchall(
        "SELECT DISTINCT file_path FROM cortex_docs WHERE deleted_at IS NULL"
    )

    missing_paths: list[str] = []
    for row in rows:
        fp = row["file_path"]
        if fp and not Path(fp).is_file():
            missing_paths.append(fp)

    if not missing_paths:
        log.info("cortex_prune_stale: 0 tombstoned")
        return {"scanned": len(rows), "tombstoned": 0, "vectors_deleted": 0}

    result = tombstone_and_purge(db, missing_paths)

    log.info(
        "cortex_prune_stale: scanned=%d tombstoned=%d vectors_deleted=%d "
        "(soft-deleted docs, hard-deleted vectors)",
        len(rows), result["tombstoned"], result["vectors_deleted"],
    )
    return {
        "scanned": len(rows),
        "tombstoned": result["tombstoned"],
        "vectors_deleted": result["vectors_deleted"],
        "paths": missing_paths[:20],  # sample for logging
    }


def cortex_reclaim() -> dict:
    """Periodic self-healing reclaim of the cortex index.

    Runs the same two idempotent passes as `okuro cortex reclaim`:
      1. reconcile-on-exclude (tombstone + vec-delete live docs whose path now
         matches the deny rules — worktrees, data-exhaust JSON),
      2. orphan-vector purge (vec rows whose doc is missing/tombstoned).

    Conservative by construction: both passes only touch rows that are already
    dead or already excluded, so the work is bounded by the size of the
    pollution, not the corpus. Every install self-heals without manual action.
    """
    from okuro.cortex.reclaim import reclaim as run_reclaim

    report = run_reclaim(reconcile=True)
    d = report.as_dict()
    log.info(
        "cortex_reclaim: orphans_purged=%d excluded_tombstoned=%d "
        "vectors_reclaimed=%d (%d -> %d)",
        d["orphans_purged"], d["excluded_tombstoned"], d["vectors_reclaimed"],
        d["vectors_before"], d["vectors_after"],
    )
    return d


def wal_truncate() -> dict:
    """Checkpoint + truncate WAL so it doesn't grow unbounded.

    Weekly VACUUM alone isn't enough: `wal_autocheckpoint=1000` is PASSIVE
    and cannot truncate while any reader holds an open snapshot. Long-lived
    MCP server subprocesses are such readers, so the WAL accumulates.
    Running TRUNCATE every 10 min keeps it bounded; if a reader blocks it,
    we retry next tick.
    """
    from okuro.db import get_db

    conn = get_db().conn
    cur = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    result = cur.fetchone()  # (busy, log_frames, checkpointed)
    log.info("wal_truncate: %s", result)
    return {"checkpoint": list(result or ())}


def memory_recall_gate() -> dict:
    """Daily recall regression check against the LIVE store.

    ``tests/sense/test_memory_eval_gate.py`` is the same check, but it only
    fires when someone runs pytest locally — the repo's only CI workflow
    (.github/workflows/windows-port.yml) runs two unrelated test files, and
    the gate self-skips without a populated store + reachable embed service,
    which a CI runner never has. So the assertion existed and nothing ever
    executed it. That is the same structural exposure that let recall sit
    dead from 2026-05-10 to 2026-07-15 while every other memory test passed —
    one layer up.

    Runs where the prerequisites actually live: this machine. Reports rather
    than raises — a daemon task that throws is noise; the numbers land in the
    log and the returned dict either way.

    Floors match the pytest gate. Measured 2026-07-18 (400 samples x 3 seeds):
    MRR 0.700, recall@10 0.926, junk rejection 1.000.
    """
    # Floors and the verdict live in memory_eval.evaluate_recall, NOT here.
    # They were inline until 2026-07-28, and the moment a second caller
    # appeared (the `memory_recall` MCP tool) that inline copy would have been
    # free to disagree with the thing that actually alarms. One implementation,
    # two callers — the daemon alarms on it, an agent reads it.
    from okuro.sense.memory_eval import evaluate_recall

    r = evaluate_recall(sample=120, k=10, seed=7)

    if r.get("ok") is None:
        log.warning("memory_recall_gate: eval could not run (%s)", r.get("error"))
    elif r["breaches"]:
        log.warning("memory_recall_gate: RECALL REGRESSION — %s",
                    "; ".join(r["breaches"]))
    else:
        log.info(
            "memory_recall_gate: ok (MRR %.3f, recall@10 %.3f, junk %.3f)",
            r.get("mrr", 0.0), r.get("recall_at_10", 0.0),
            r.get("junk_rejection", 0.0),
        )
    return r


def db_vacuum() -> dict:
    """Reclaim freelist + truncate WAL.

    Observed pre-fix: 380 MB file + 376 MB WAL on 12 MB real data (96%
    freelist). Running weekly keeps the file proportional to content.
    """
    from okuro.db import get_db

    db = get_db()
    conn = db.conn

    # wal_checkpoint must happen before VACUUM to minimize WAL growth.
    cur = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint = cur.fetchone()  # (busy, log_frames, checkpointed)
    conn.commit()

    conn.execute("VACUUM")
    conn.commit()

    db_path = okuro_home() / "okuro.db"
    size_bytes = db_path.stat().st_size if db_path.is_file() else 0
    log.info("db_vacuum: size=%d bytes, checkpoint=%s", size_bytes, checkpoint)
    return {"size_bytes": size_bytes, "checkpoint": list(checkpoint or ())}


def profile_enrich() -> dict:
    """Safety-net LLM enrichment of profile rule sections.

    The write-path hooks in ``_save_profile`` / ``update_profile`` already
    fire ``rules.auto_enrich_async`` after every save, so this daemon
    pass is pure insurance: it catches profile mutations that bypassed
    the API (raw DB edits, first-boot seed, crashed write-path hook).
    No-op when every rule already has a rationale.
    """
    from okuro.sense import rules
    from okuro.yu.profile import get_profile_raw

    profile = get_profile_raw() or {}
    targets = rules._collect_enrichment_targets(profile)
    if not targets:
        log.debug("profile_enrich: nothing to do")
        return {"targets": 0, "updated": 0, "persisted": False}

    result = rules.enrich(dry_run=False)
    log.info(
        "profile_enrich: targets=%s updated=%s persisted=%s",
        result.get("targets"), result.get("updated"), result.get("persisted"),
    )
    return result


# ---------------------------------------------------------------------------
# One-time migrations (called from okuro.daemon.__main__)
# ---------------------------------------------------------------------------

_MCPD_MARKER = okuro_home() / ".mcpd_folded_into_daemon"


def uninstall_legacy_mcpd_once() -> bool:
    """Stop + disable + uninstall ``okuro-mcpd.service`` if still installed.

    Subagent #15 folded the HTTP MCP surface into ``okuro-daemon``. Users
    who had the standalone ``okuro-mcpd`` unit installed from the pre-fold
    era will see an orphan unit on disk; this helper removes it on first
    daemon boot after the fold and writes a marker file so it's a no-op on
    every subsequent startup.

    Returns True if the uninstall was attempted this call, False otherwise.
    Safe to call on hosts that never installed okuro-mcpd — the marker is
    created anyway so we don't re-check.
    """
    if _MCPD_MARKER.exists():
        return False

    _MCPD_MARKER.parent.mkdir(parents=True, exist_ok=True)

    try:
        from okuro.system.service_manager import get_service_manager
    except Exception:
        # Service manager isn't available (wrong platform, missing
        # systemctl, etc). Mark as done so we don't keep retrying.
        _MCPD_MARKER.write_text("skipped: service manager unavailable\n")
        return False

    try:
        mgr = get_service_manager()
    except Exception as exc:
        _MCPD_MARKER.write_text(f"skipped: {exc}\n")
        return False

    # Ask the manager if the unit is installed. LinuxServiceManager
    # exposes _service_path(); fall back to status() for other backends.
    installed = False
    try:
        if hasattr(mgr, "_service_path"):
            installed = mgr._service_path("okuro-mcpd").exists()  # type: ignore[attr-defined]
        else:
            status = mgr.status("okuro-mcpd")
            installed = status.get("state") not in ("not-found", "unknown", None)
    except Exception:
        installed = False

    if not installed:
        _MCPD_MARKER.write_text("skipped: okuro-mcpd not installed\n")
        log.info("legacy okuro-mcpd: not installed — skipping migration")
        return False

    try:
        mgr.uninstall("okuro-mcpd")
        _MCPD_MARKER.write_text("uninstalled\n")
        log.info(
            "legacy okuro-mcpd: stopped + disabled + uninstalled "
            "(HTTP MCP is now hosted in okuro-daemon)"
        )
        return True
    except Exception as exc:
        log.warning("legacy okuro-mcpd: uninstall failed: %s", exc)
        # Do NOT write the marker — next boot retries.
        return False


def proactive_advise() -> dict:
    """LLM continuation advisor — daily 06:30 fire.

    Walks every active project, builds a cross-source bundle, asks
    claude (via bridge.invoke) whether the project is near-complete
    plus blocked by a single concrete step. Emits one continuation
    signal per high-readiness project.
    """
    from okuro.sense.proactive import find_continuations

    return find_continuations()


def proactive_scan() -> dict:
    """Run the proactive scanner once and emit any new signals.

    Hourly handler for the ``proactive-scan`` DaemonTask. Heuristic-only
    in P1: scans stale progress, stuck todos, aging actionable thoughts,
    disk pressure, VRAM pressure, and recent session failures, then
    writes `source='proactive'` signals into the queue. Existing open
    signals dedupe on ``source_ref`` so a single situation never floods.
    """
    from okuro.sense.proactive import scan_once

    return scan_once()


def notes_extract() -> dict:
    """Extract typed, tagged items from changed okuro notes.

    Handler for the ``notes-extract`` DaemonTask. Each changed note goes to
    the bridge once; the returned items route to todos (concrete actions,
    incl. prose commitments), signals (observations + open questions), or
    thoughts (speculative ideas). A per-note content-hash watermark means an
    idle tick costs one query and no LLM call.

    Replaces the ``obsidian-sync`` intake, which fed vault files into
    ``thoughts`` untyped and untagged.
    """
    from okuro.sense.notes_extract import extract_once

    return extract_once()


def inbox_reduce() -> dict:
    """Project active producer rows into the inbox overlay with salience.

    Handler for the ``inbox-reduce`` DaemonTask (every 30m + on startup).
    Additive overlay only — producers and UI are untouched, no disposition
    writes happen, and user-set inbox ``state`` is preserved across passes.
    """
    from okuro.sense.inbox.reducer import reduce_once

    return reduce_once()


def inbox_heartbeat() -> dict:
    """Apply the inbox surfacing gate — promote 'new' → 'surfaced'.

    Handler for the ``inbox-heartbeat`` DaemonTask (every 30m + on startup,
    after ``inbox-reduce``). Silent-when-empty, defer-when-busy: returns a
    short-circuit dict without writing when there is nothing to surface or an
    orchestrator-managed session is running.
    """
    from okuro.sense.inbox.reducer import surface_pass

    return surface_pass()


def commitment_infer() -> dict:
    """LLM-infer per-session implicit follow-ups from recent provider sessions.

    Handler for the ``commitment-infer`` DaemonTask (every 15m). Bounded
    (max sessions/run + per-call timeout) and provider-failure safe — a
    failed session is watermarked, never crashes the daemon. Folds the
    commitment-expiry sweep into the same pass.
    """
    from okuro.sense.commitments import infer_recent

    return infer_recent()


def inbox_hygiene() -> dict:
    """Purge terminal-state inbox + commitment rows so the tables stay bounded.

    Handler for the ``inbox-hygiene`` DaemonTask (daily). Deletes superseded/
    expired inbox rows and expired commitments past a grace window. Never
    touches user tombstones ('dismissed'/'acted').
    """
    from okuro.sense.inbox.reducer import purge_stale

    return purge_stale()


def telemetry_rotate() -> dict:
    """Rotate ~/.okuro/telemetry/usage.jsonl past 10 MB; prune archives > 14 days.

    Audit ref: 06-stability.md HIGH-5. The file is append-only JSONL with
    no size cap; heavy MCP users accumulate MB/day without this. The
    writer opens the file per-call, so the rename inside
    ``rotate_if_needed`` is race-safe vs. in-flight writes.
    """
    from okuro.telemetry.logger import rotate_if_needed, USAGE_FILE, ARCHIVE_DIR

    rotated = rotate_if_needed()
    archive_count = 0
    try:
        if ARCHIVE_DIR.exists():
            archive_count = sum(1 for p in ARCHIVE_DIR.iterdir() if p.is_file())
    except OSError:
        pass

    result = {
        "rotated": str(rotated) if rotated else None,
        "archive_count": archive_count,
        "usage_file": str(USAGE_FILE),
    }
    log.info("telemetry_rotate: %s", result)
    return result


# --- orphaned-engine reconciler -------------------------------------------
#
# Root cause it closes: orchestrator task status is driven by a per-task
# engine PROCESS (`python -m okuro.orchestrator.engine`). An update/restart —
# or any crash — kills in-flight engines and NOTHING respawns them, so tasks
# freeze in pending/active/planning forever. The on-demand reconciler
# (`api.main._reconcile_task_state`) only HALTS such orphans (data-lossy:
# subtasks -> failed) and only when a GET happens to touch the task; pending
# orphans it ignores entirely. This daemon job is the missing respawn path.

_RECONCILE_MAX_ATTEMPTS = 3
# Statuses that mean "an engine should be actively driving this right now".
# Everything else (done/cancelled/failed/halted/blocked/waiting_user) is
# either terminal or a legitimate park — left untouched.
_RECONCILE_RESUMABLE = frozenset({"pending", "active", "planning"})


def _tasks_dir() -> Path:
    """Resolve the orchestrator tasks directory (honors OKURO_ROOT)."""
    import os

    root = Path(os.environ.get("OKURO_ROOT", okuro_home() / "orchestrator"))
    return root / "tasks"


def _reconcile_progress_sig(task_dir: Path, status: str) -> str:
    """A cheap progress fingerprint: status + count of completed subtasks.

    When this changes between cycles the task genuinely advanced, so the
    backoff counter resets and we keep helping. When it stays flat across
    _RECONCILE_MAX_ATTEMPTS respawns, the task is broken — stop respawning.
    """
    import yaml

    done = 0
    plan = task_dir / "plan.yaml"
    if plan.is_file():
        try:
            pdata = yaml.safe_load(plan.read_text()) or {}
            for phase in pdata.get("phases") or []:
                for st in phase.get("subtasks") or []:
                    if (st.get("status") or "").lower() == "done":
                        done += 1
        except Exception:
            pass
    return f"{status}:{done}"


def _read_reconcile_state(task_dir: Path) -> dict:
    import json

    f = task_dir / ".reconcile.json"
    if f.is_file():
        try:
            return json.loads(f.read_text()) or {}
        except Exception:
            return {}
    return {}


def _write_reconcile_state(task_dir: Path, state: dict) -> None:
    import json

    try:
        (task_dir / ".reconcile.json").write_text(json.dumps(state))
    except OSError as exc:
        log.warning("reconcile: could not persist backoff state for %s: %s", task_dir.name, exc)


def _api_bearer_token() -> str | None:
    """Read the API bearer token the orchestrator now requires on EVERY /api/*
    route. Same lookup order as api.main._load_api_token, but READ-ONLY (never
    mints) so the daemon can't diverge from the API's token.

    Critical: without this the daemon's /resume POST hits the BearerAuthMiddleware
    and gets 401 -> treated as 'skip' -> orphaned engines are NEVER respawned
    (auto-recovery silently dead). Surfaced LIVE 2026-06-19: a SIGKILLed engine
    sat `active` for the full grace window because every reconcile cycle's
    /resume returned 401.
    """
    try:
        from okuro.keyring.storage import KeyringStorage
        tok = KeyringStorage().get_key("okuro/api_token")
        if tok:
            return tok
    except Exception:
        pass
    import os
    tok = os.environ.get("OKURO_API_TOKEN")
    if tok:
        return tok
    try:
        from pathlib import Path
        f = okuro_home() / "api_token"
        if f.is_file():
            t = f.read_text().strip()
            if t:
                return t
    except Exception:
        pass
    return None


def _post_resume(base_url: str, task_id: str) -> str:
    """POST /api/tasks/{id}/resume. Returns 'resumed' | 'skip' | 'unreachable'.

    The endpoint owns all guards: live engine -> 409, terminal -> 409,
    spawn-lock + spawn-grace prevent double-spawn. So a blind POST is
    race-safe — 202 means the task really was an orphan and was respawned.
    Carries the API bearer token (required on all /api/* routes — without it
    the POST 401s and recovery silently never fires).
    """
    import json as _json
    import urllib.error
    import urllib.request

    url = f"{base_url}/api/tasks/{task_id}/resume"
    req = urllib.request.Request(url, data=b"", method="POST")
    _tok = _api_bearer_token()
    if _tok:
        req.add_header("Authorization", f"Bearer {_tok}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return "resumed" if resp.status in (200, 202) else "skip"
    except urllib.error.HTTPError as exc:
        # 409 = live engine or terminal (not an orphan); 404 = gone. Not errors.
        return "skip"
    except (urllib.error.URLError, OSError):
        # Orchestrator API down (e.g. mid-restart) — try again next cycle.
        return "unreachable"


def reconcile_orphaned_engines() -> dict:
    """Respawn engines for orphaned in-flight tasks — the missing reconciler.

    Handler for the ``engine-reconciler`` DaemonTask (every 2m + on startup).
    Scans the task tree and asks the orchestrator API to ``/resume`` every
    task that should be running but whose engine is gone. Thin + race-safe:
    the endpoint owns the liveness/terminal/spawn-lock guards. A persistent
    ``.reconcile.json`` backoff caps respawns per progress-signature so a
    genuinely-broken task is not respawned forever.

    Silent-when-clean. Never raises — a bad task dir is skipped, not fatal.
    """
    import time

    import yaml

    from okuro.system.port_registry import orchestrator_port

    tasks_dir = _tasks_dir()
    if not tasks_dir.is_dir():
        return {"scanned": 0, "resumed": 0, "skipped": 0, "exhausted": 0}

    base_url = f"http://127.0.0.1:{orchestrator_port()}"
    scanned = resumed = skipped = exhausted = unreachable = 0
    resumed_ids: list[str] = []

    for task_dir in sorted(tasks_dir.glob("task-*")):
        task_yaml = task_dir / "task.yaml"
        if not task_yaml.is_file():
            continue
        scanned += 1
        try:
            # libyaml loader — identical SafeLoader semantics, ~13x faster.
            # This loop parses EVERY task.yaml in the corpus and the daemon
            # runs it on a */5 cron, so the cost is O(task dirs) forever.
            data = yaml.load(
                task_yaml.read_text(),
                Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader),
            ) or {}
        except Exception:
            continue

        status = (data.get("status") or "").lower()
        # Parked-on-user tasks legitimately have no live engine — leave them.
        if status not in _RECONCILE_RESUMABLE or data.get("awaiting") or data.get("status") == "waiting_user":
            skipped += 1
            continue

        # Backoff keyed on the progress signature.
        sig = _reconcile_progress_sig(task_dir, status)
        state = _read_reconcile_state(task_dir)
        if state.get("sig") != sig:
            state = {"sig": sig, "attempts": 0}
        if state.get("attempts", 0) >= _RECONCILE_MAX_ATTEMPTS:
            exhausted += 1
            continue

        outcome = _post_resume(base_url, task_dir.name)
        if outcome == "unreachable":
            unreachable += 1
            continue  # don't burn an attempt when the API is mid-restart
        if outcome == "resumed":
            state["attempts"] = state.get("attempts", 0) + 1
            state["last_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _write_reconcile_state(task_dir, state)
            resumed += 1
            resumed_ids.append(task_dir.name)
        else:  # "skip" — endpoint said 409/404: healthy, terminal, or gone.
            skipped += 1

    if resumed:
        log.info(
            "reconcile_orphaned_engines respawned %d orphaned task(s): %s",
            resumed, ", ".join(resumed_ids),
        )
    return {
        "scanned": scanned,
        "resumed": resumed,
        "skipped": skipped,
        "exhausted": exhausted,
        "unreachable": unreachable,
    }


def canon_drift_alarm() -> dict:
    """Warn when okuro's MCP registration has drifted from what canon writes.

    The daemon's 5-minute ``refresh`` re-runs instruction files and hooks but
    NEVER re-runs MCP config registration, so MCP is the one agent-facing
    surface with no reconciler. An app update that rewrites its config, or the
    ``~/.mcp.json`` project-scope shadow that makes okuro start disconnected
    every Claude Code session, goes unnoticed until someone opens Settings.
    This is the push side of ``registration_status`` — silent when in sync.

    Report-only: a background job MUST NOT rewrite a user's config (that is
    ``okuro canon deploy``, a user action). run_on_startup so a boot that is
    already drifted says so immediately.

    Alarms only on UNAMBIGUOUS drift: a registration that is present but wrong
    (``stale``), and the ``~/.mcp.json`` project-scope shadow. It does NOT alarm
    on a consumer where okuro simply was never registered (the user's choice,
    surfaced in the UI) nor on okuro being disabled for a project (deliberate).
    """
    try:
        from okuro.cli.mcp_config import registration_status
        st = registration_status()
    except Exception as exc:  # never let a probe kill the scheduler
        log.debug("canon_drift_alarm: probe failed (%s)", exc)
        return {"drift": None}

    providers = st.get("providers", {})
    stale = sorted(n for n, p in providers.items() if p.get("stale"))
    # scope == "project" is the ~/.mcp.json shadow (disconnect cause); a
    # "project:<path>" scope is a deliberate disabledMcpServers entry — not drift.
    shadow = [u for u in st.get("unmanaged", []) if u.get("scope") == "project"]

    if not stale and not shadow:
        return {"drift": False}

    if stale:
        log.warning(
            "CANON DRIFT — MCP registration is stale for: %s. The written entry "
            "no longer matches what okuro would write. Run: okuro canon deploy",
            ", ".join(stale),
        )
    for u in shadow:
        log.warning(
            "CANON DRIFT — %s carries a project-scope okuro shadow (%s) that "
            "outranks the managed user entry and starts disconnected. Run: "
            "okuro canon deploy",
            u.get("path"), u.get("server"),
        )
    return {"drift": True, "stale": stale, "shadow": len(shadow)}


def managed_repo_sync() -> dict:
    """Pull + re-ingest every healthy managed repo, one at a time.

    The sibling auth alarm answers "can we still reach the forge"; this answers
    "is what we indexed still what is there". They are different failures: a repo
    can be perfectly reachable and seven weeks out of date, and cortex will serve
    the stale copy with no signal that it is stale.

    Serialised on purpose — parallel ingest races the same SQLite index. Repos in
    error or mid-clone are skipped: a broken credential is the auth alarm's job,
    and re-running a failing pull hourly just fills the log.
    """
    try:
        from okuro.repos.lifecycle import list_repos, sync_repo
    except Exception as exc:
        log.debug("managed_repo_sync: import failed (%s)", exc)
        return {"synced": None}

    skip = {"pending", "cloning", "indexing", "error"}
    rows = [r for r in list_repos() if r.get("status") not in skip]
    synced, failed = [], []
    for rec in rows:
        try:
            sync_repo(rec["id"])
            synced.append(rec["id"])
        except Exception as exc:  # one bad repo must not end the sweep
            failed.append({"id": rec["id"], "detail": str(exc)[:200]})
            log.warning("managed_repo_sync: %s failed — %s", rec["id"], str(exc)[:200])
    if failed:
        log.warning(
            "MANAGED REPO SYNC — %d of %d repo(s) failed to sync: %s",
            len(failed), len(rows), ", ".join(f["id"] for f in failed),
        )
    return {"synced": len(synced), "failed": len(failed), "skipped": len(list_repos()) - len(rows)}


def repo_auth_alarm() -> dict:
    """Flag managed repos whose git credential a forge has started rejecting.

    Repo status was evaluated on demand only, so a revoked credential stayed
    invisible: the registry kept serving a weeks-old 'ready' as current state
    and only the repo someone happened to sync ever flipped to 'error'. This is
    that missing reconciler — the same declare-then-verify shape as its sibling
    drift alarms, applied to access instead of code.

    Probes per CREDENTIAL, not per repo (13 repos → ~2 network calls), and
    marks nothing on an unreachable network — only on an explicit rejection.
    run_on_startup so a boot that is already locked out says so immediately.
    Silent when every credential is healthy.
    """
    try:
        from okuro.repos.lifecycle import probe_credentials
        res = probe_credentials(mark=True)
    except Exception as exc:  # never let a probe kill the scheduler
        log.debug("repo_auth_alarm: probe failed (%s)", exc)
        return {"broken": None}
    if not res.get("broken"):
        return {"broken": False, "checked": res.get("checked", 0)}
    for grp in res["broken"]:
        log.warning(
            "REPO AUTH — %s scope on %s: %d repo(s) marked error (%s). "
            "Credential '%s' is valid but access was withdrawn — rotating the "
            "token will NOT fix a workspace removal.",
            grp["scope"], grp["host"], len(grp["repos"]),
            ", ".join(grp["repos"]), grp["credential"],
        )
    return {
        "broken": True,
        "checked": res["checked"],
        "probes": res["probes"],
        "marked": res["repos_marked"],
    }


def code_drift_alarm() -> dict:
    """Log a WARNING when the daemon's loaded code has fallen behind HEAD.

    The daemon is a long-lived process; Python freezes each module at first
    import, so a commit fixes nothing here until a restart — and until 2026-
    07-16 nothing surfaced the gap. The daemon served _SIM_FLOOR=0.4 for hours
    after the metric fix landed because it had booted before it, and no signal
    said so (Fable audit G1). This is the daemon-side counterpart to the
    per-session drift banner (system.code_version.detect_code_drift), which
    only warns interactive MCP sessions.

    Runs on startup (the highest-value moment — a fresh boot that is ALREADY
    behind means someone committed then forgot to restart) and every 30 min.
    Silent when in sync. The live /health payload also carries the same field
    for pull-based monitoring; this is the push side.
    """
    try:
        from okuro.system.code_version import detect_code_drift
        drift = detect_code_drift()
    except Exception as exc:  # never let a staleness check kill the scheduler
        log.debug("code_drift_alarm: detection failed (%s)", exc)
        return {"drift": None}
    if not drift:
        return {"drift": False}
    log.warning(
        "CODE DRIFT — okuro-daemon booted %s and is %d commit(s) behind HEAD "
        "%s (loaded %s). Committed fixes are NOT deployed here until restart: "
        "%s%s. Run: systemctl --user restart okuro-daemon",
        drift["booted"], drift["behind"], drift["head"], drift["loaded"],
        "; ".join(drift["subjects"]),
        " …" if drift["behind"] > len(drift["subjects"]) else "",
    )
    return {
        "drift": True,
        "behind": drift["behind"],
        "head": drift["head"],
        "loaded": drift["loaded"],
    }


# How far back the kind-drift alarm reads usage.jsonl. Wider than its own cron
# so a weekly task (maintenance, session-retros) is still visible on most runs;
# the file is append-only and small, so re-reading the same rows is cheap and
# the alarm is idempotent.
_KIND_DRIFT_WINDOW_HOURS = 24 * 8


def kind_drift_alarm() -> dict:
    """Log a WARNING when a daemon task's DECLARED kind/tier contradicts what it ran.

    The registry declares kind/tier per task (script | llm | mixed |
    orchestrator). Declarations rot: media-recurring advertised "no LLM agent"
    for months while making two model calls per due job, one at codex
    reasoning_effort=high — nothing contradicted the prose because nothing
    compared it to reality. This is that comparison, and the reason the
    declaration is trustworthy enough to price a schedule off.

    Only ever flags the UNSAFE direction — declared-no-model but a model ran,
    or a tier above the declared one. It cannot flag the reverse, and does not
    try: absence of calls is not evidence of a wrong label. A `mixed` task is
    *defined* by making no call when there's no work, a weekly task may not have
    fired inside the window, and the daemon persists no per-task run history to
    distinguish "didn't run" from "ran and stayed silent" (the /api/schedules
    `last_run` field reads ~/.okuro/daemon/state.yaml, which nothing writes).

    Silent when everything matches its declaration.
    """
    try:
        from okuro.bridge.providers import tier_of_model
        from okuro.daemon.registry import (
            KIND_ORCHESTRATOR, KIND_SCRIPT, TIER_FAST, TIER_QUALITY,
            TIER_STANDARD, get_all_tasks,
        )
    except Exception as exc:  # never let an alarm kill the scheduler
        log.debug("kind_drift_alarm: import failed (%s)", exc)
        return {"checked": 0}

    observed = _read_bridge_usage(_KIND_DRIFT_WINDOW_HOURS)
    if not observed:
        return {"checked": 0, "drift": []}

    # Rank tiers so "ran hotter than declared" is answerable. Unknown tiers
    # (codex maps every tier to "", so tier_of_model can't disambiguate) sort
    # outside this and are skipped rather than guessed at.
    order = {TIER_FAST: 0, TIER_STANDARD: 1, TIER_QUALITY: 2}

    drift: list[str] = []
    declared_by_id = {t.id: t for t in get_all_tasks()}
    for task_id, calls in sorted(observed.items()):
        task = declared_by_id.get(task_id)
        if task is None:
            continue  # a caller that isn't a daemon task — not ours to judge

        if task.kind in (KIND_SCRIPT, KIND_ORCHESTRATOR):
            models = sorted({c["model"] for c in calls if c.get("model")})
            drift.append(
                f"{task_id}: declared kind={task.kind} (no model call) but made "
                f"{len(calls)} call(s) to {', '.join(models) or 'unknown'}"
            )
            continue

        declared_rank = order.get(task.tier)
        if declared_rank is None:
            continue
        for model, tier in sorted({
            (c["model"], tier_of_model(c["provider"], c["model"])) for c in calls
        }):
            rank = order.get(tier)
            if rank is not None and rank > declared_rank:
                drift.append(
                    f"{task_id}: declared tier={task.tier} but ran {model} "
                    f"(tier={tier}) — costs more than the overview claims"
                )

    if not drift:
        return {"checked": len(observed), "drift": []}

    log.warning(
        "DAEMON KIND DRIFT — %d task(s) ran hotter than the registry declares. "
        "The Scheduled overview is understating cost. Fix the declaration in "
        "daemon/registry.py (or the handler): %s",
        len(drift), "; ".join(drift),
    )
    return {"checked": len(observed), "drift": drift}


def _read_bridge_usage(window_hours: int) -> dict:
    """Group attributed bridge calls from the last *window_hours* by caller.

    Rows without a `caller` are dropped, not counted as anonymous: interactive
    use and engine subprocesses legitimately write unattributed rows, and an
    orchestrator task's engine calls land there too (a separate process never
    sees the ContextVar).
    """
    import json
    from datetime import datetime, timedelta, timezone
    from okuro.bridge.tracker import USAGE_FILE

    if not USAGE_FILE.exists():
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    grouped: dict[str, list] = {}
    try:
        with open(USAGE_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a torn final line is normal on an append-only log
                caller = row.get("caller")
                if not caller:
                    continue
                try:
                    ts = datetime.fromisoformat(str(row.get("ts", "")))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
                grouped.setdefault(caller, []).append(row)
    except OSError as exc:
        log.debug("kind_drift_alarm: usage read failed (%s)", exc)
        return {}
    return grouped


def full_suite() -> dict:
    """Run okuro's full test suite on a schedule and log a WARNING when red.

    THE TRIGGER THIS REPLACES. The suite gated the deploy for one day
    (2026-08-10) and charged every merge ~14 minutes before the services would
    run the code that was merged in order to be used. The owner moved it to the
    two triggers that cost interaction nothing: ``pre-push`` (the last moment a
    red result can stop something irreversible) and this.

    WHY THIS ONE EXISTS AT ALL, given pre-push. Pushes are rare here — the
    standing rule is that nothing gets pushed — so pre-push alone could leave a
    regression unseen for days. This is the clock that does not depend on
    anybody doing anything.

    WHY THE DAEMON RATHER THAN A NOTIFICATION. The rejected design was "deploy,
    then run the suite in the background and tell whoever merged". That needs a
    listener, and the agent that merged has usually ended. The daemon has no
    such lifetime problem: it is running either way, and its WARNING lands in
    the same place every other alarm does, for whoever looks next.

    Idle-hours by default (04:00) so a 14-minute CPU burn never lands mid-work.
    Silent when the run is green or only the known baseline fails.
    """
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[3]
    gate = repo / "scripts" / "full_suite_gate.py"
    if not gate.is_file():
        log.debug("full_suite: %s absent — not an okuro source checkout", gate)
        return {"ran": False}

    try:
        proc = subprocess.run(
            [sys.executable, str(gate)],
            cwd=repo, capture_output=True, text=True, timeout=3600,
        )
    except Exception as exc:  # never let a test run kill the scheduler
        log.debug("full_suite: run failed (%s)", exc)
        return {"ran": False, "error": str(exc)}

    out = (proc.stdout or "").strip()
    if proc.returncode == 0:
        return {"ran": True, "ok": True}

    # The gate already prints the offending node ids and the stash procedure
    # for proving one pre-existing; carry its own words rather than re-deriving
    # a summary that could disagree with the record it just wrote.
    log.warning(
        "FULL SUITE RED — failures beyond the known baseline on this checkout. "
        "A push will be refused until this is resolved.\n%s",
        out[-2000:] or "(no output)",
    )
    return {"ran": True, "ok": False}
