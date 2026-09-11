# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro cortex — manage codebase index, sidecars, and LLM enrichment
# index:
#   imports
#   def cortex
#   def add
#   def ls
#   def enrich
#   def status
#   def refresh
# AGENT_HEADER_END -->
"""okuro cortex — manage codebase index, sidecars, and LLM enrichment."""

import os
from pathlib import Path

import click

from .output import console, ok, warn, info, heading, data_table


@click.group()
def cortex():
    """Manage codebase index and LLM enrichment."""


@cortex.command("health")
@click.option("--window-days", default=30, help="Query-metrics window (days). 0 = all time.")
def health(window_days):
    """Show cortex index health + retrieval observability (F39/F40).

    Index health: last indexed time, live vectors, orphan vectors, worktree-dup
    docs. Query metrics: search volume, zero-result rate, hit rate, avg latency
    — aggregated from the cortex query log.
    """
    from okuro.cortex.observability import index_health, query_metrics

    heading("Cortex — index health & retrieval metrics")

    h = index_health()
    win = None if not window_days else window_days
    qm = query_metrics(window_days=win)

    health_rows = [
        ["Last indexed at", str(h.get("last_indexed_at") or "—")],
        ["Live documents", str(h.get("live_documents", 0))],
        ["Live vectors", str(h.get("live_vectors", 0))],
        ["Orphan vectors", str(h.get("orphan_vectors", 0))],
        ["Worktree-dup docs", str(h.get("worktree_docs", 0))],
    ]
    console.print(data_table(["Index health", "Value"], health_rows))

    lat = qm.get("avg_latency_ms")
    metric_rows = [
        ["Search volume", str(qm.get("search_volume", 0))],
        ["Zero-result rate", f"{qm.get('zero_result_rate', 0.0):.1%}"],
        ["Hit rate", f"{qm.get('hit_rate', 0.0):.1%}"],
        ["Avg latency (ms)", f"{lat:.1f}" if lat is not None else "—"],
        ["Window (days)", "all" if win is None else str(win)],
    ]
    console.print()
    console.print(data_table(["Retrieval metrics", "Value"], metric_rows))

    orphans = h.get("orphan_vectors", 0) or 0
    wt = h.get("worktree_docs", 0) or 0
    if orphans or wt:
        info(
            f"{orphans} orphan vector(s) + {wt} worktree-dup doc(s) — "
            "run [bold]okuro cortex reclaim[/bold] to purge."
        )


@cortex.command("add")
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--slug", default=None, help="Project id (defaults to lowercased directory name).")
@click.option("--name", default=None, help="Display name (defaults to directory name).")
@click.option("--reindex/--no-reindex", default=False, help="Kick off a full index for this root after registering.")
def add(path, slug, name, reindex):
    """Register a directory so cortex indexes it.

    Appends a row to the projects table; the daemon + /api/cortex/reindex
    will pick it up automatically. Use --reindex to index it right now.
    """
    from okuro.cortex.roots import register_root

    heading("Cortex — register project root")

    try:
        result = register_root(path, slug=slug, name=name)
    except ValueError as exc:
        warn(str(exc))
        raise SystemExit(1)

    action_msgs = {
        "created": f"Registered new project [bold]{result['slug']}[/bold] at {result['path']}",
        "updated": (
            f"Updated existing row for {result['path']} "
            f"(was '{result['pre_existing_slug']}', now '{result['slug']}')"
        ),
        "already_active": f"[bold]{result['slug']}[/bold] already active at {result['path']} — no change.",
    }
    ok(action_msgs.get(result["action"], str(result)))

    if not reindex:
        info("Next daemon tick will pick up new/changed files. Pass --reindex to index now.")
        return

    try:
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        total, indexed = vs.index_directory(
            Path(result["path"]), project=result["slug"]
        )
        ok(f"Indexed {indexed} of {total} files under {result['slug']}")
    except Exception as exc:
        warn(f"Reindex failed: {exc}")


@cortex.command("reclaim")
@click.option("--dry-run", is_flag=True, help="Report what would be purged without writing.")
@click.option("--no-reconcile", is_flag=True, help="Skip reconcile-on-exclude; only purge orphan vectors.")
@click.option("--repack", is_flag=True, help="Rebuild vec_cortex to fix NULL-partition chunk bloat, then VACUUM (heavy — rewrites the whole DB).")
@click.option("--purge-tombstones", is_flag=True, help="Hard-delete soft-deleted cortex_docs (frees FTS bloat). Irreversible; honours --tombstone-retention-days.")
@click.option("--tombstone-retention-days", default=0, help="Keep tombstones deleted within this many days; 0 purges all.")
def reclaim_cmd(dry_run, no_reconcile, repack, purge_tombstones, tombstone_retention_days):
    """Self-heal the cortex index against this install's own DB.

    Two idempotent, safe-to-re-run passes:

    \b
      1. Reconcile-on-exclude — tombstone + vec-delete live docs whose path
         now matches the current deny rules (worktrees, data-exhaust JSON).
         This makes deny-rule changes retroactive on an existing install.
      2. Orphan purge — delete vec_cortex rows whose cortex_docs row is
         missing or tombstoned (the 64.5%-dead-index root cause).

    Reports before/after counts and a reclaimed-space estimate. Use
    --dry-run to preview.
    """
    from okuro.cortex.reclaim import reclaim as run_reclaim

    heading("Cortex Reclaim" + (" (dry-run)" if dry_run else ""))

    # Resolve embedding dim for the space estimate (best-effort).
    embedding_dim = 0
    try:
        from okuro.cortex.vectorstore import VectorConfig
        embedding_dim = VectorConfig().embedding_dim
    except Exception:
        embedding_dim = 0

    try:
        report = run_reclaim(
            dry_run=dry_run, reconcile=not no_reconcile, repack=repack,
            purge_tombstones=purge_tombstones,
            tombstone_retention_days=tombstone_retention_days,
        )
    except Exception as exc:
        warn(f"Reclaim failed: {exc}")
        raise SystemExit(1)

    d = report.as_dict(embedding_dim=embedding_dim)
    rows = [
        ["Vectors (before)", str(d["vectors_before"])],
        ["Vectors (after)", str(d["vectors_after"])],
        ["Live docs (before)", str(d["docs_live_before"])],
        ["Live docs (after)", str(d["docs_live_after"])],
        ["Orphan vectors (before)", str(d["orphans_before"])],
        ["Orphan vectors (after)", str(d["orphans_after"])],
        ["Orphans purged", str(d["orphans_purged"])],
        ["Excluded docs tombstoned", str(d["excluded_tombstoned"])],
        ["Vectors reclaimed", str(d["vectors_reclaimed"])],
        ["Vec chunks (before)", str(d["chunks_before"])],
        ["Vec chunks (after)", str(d["chunks_after"])],
        ["Bloat ratio (before)", f"{d['bloat_ratio_before']}x"],
        ["Repacked", "yes" if d["repacked"] else "no"],
        ["Tombstones purged", str(d["tombstones_purged"])],
    ]
    if "reclaimed_bytes_estimate" in d:
        mb = d["reclaimed_bytes_estimate"] / (1024 * 1024)
        rows.append(["Reclaimed space (est.)", f"{mb:.1f} MB"])
    console.print(data_table(["Metric", "Count"], rows))

    if dry_run:
        info("Dry-run — nothing written. Re-run without --dry-run to apply.")
    else:
        ok(
            f"Reclaimed {d['vectors_reclaimed']} vector(s) "
            f"({d['orphans_purged']} orphans purged, "
            f"{d['excluded_tombstoned']} excluded docs tombstoned)."
        )


@cortex.command("discover")
@click.option("--min-files", default=10, help="Minimum code files for a candidate to qualify.")
@click.option("--apply", is_flag=True, help="Register every candidate. Without this flag the command only previews.")
@click.option("--skip", multiple=True, help="Candidate names to skip (repeatable).")
def discover(min_files, apply, skip):
    """Find unregistered project directories worth indexing.

    Walks the standard parents (``~``, the workspace dirs named by the
    ``cortex.search_parents`` convention,
    ``~/okuro``) and lists every directory that has a project marker
    (``.git`` / ``pyproject.toml`` / ``package.json`` / ``Dockerfile`` / etc.)
    plus at least ``--min-files`` indexable source files. Pass ``--apply`` to
    register them all in one go.
    """
    from okuro.cortex.roots import auto_discover_candidates, register_root

    heading("Cortex — auto-discover roots")
    cands = auto_discover_candidates(min_code_files=min_files)
    skip_set = {s.lower() for s in skip}

    if not cands:
        ok("No unregistered project candidates found.")
        return

    rows = []
    for c in cands:
        name = Path(c["path"]).name.lower()
        marker = "skip" if name in skip_set else ("register" if apply else "preview")
        rows.append([
            c["suggested_slug"],
            c["path"],
            str(c["code_files"]),
            "git" if c["has_git"] else "-",
            "mark" if c["has_marker"] else "-",
            marker,
        ])
    console.print(data_table(
        ["Slug", "Path", "Files", "Git", "Marker", "Action"], rows
    ))

    if not apply:
        info(f"{len(cands)} candidate(s). Re-run with --apply to register, or filter via --skip.")
        return

    registered = 0
    for c in cands:
        if Path(c["path"]).name.lower() in skip_set:
            continue
        try:
            result = register_root(c["path"], slug=c["suggested_slug"])
            ok(f"  {result['action']:>14}  {result['slug']}")
            if result["action"] in {"created", "updated"}:
                registered += 1
        except ValueError as exc:
            warn(f"  failed         {c['suggested_slug']}: {exc}")
    ok(f"Registered {registered} new root(s).")


@cortex.command("deactivate")
@click.argument("slug")
def deactivate(slug):
    """Mark a registered root inactive — cortex stops indexing it.

    The project row is preserved; reactivate with ``okuro cortex add <path>``.
    """
    from okuro.cortex.roots import deactivate_root

    heading(f"Cortex — deactivate {slug}")
    result = deactivate_root(slug)
    if result["action"] == "deactivated":
        ok(f"{slug} deactivated.")
    elif result["action"] == "already_inactive":
        info(f"{slug} was already inactive — nothing to do.")
    else:
        warn(f"{slug} not found in projects table.")


@cortex.command("ls")
def ls():
    """List every registered cortex root with per-project index counts."""
    from okuro.cortex.roots import registered_roots
    from okuro.cortex.vectorstore import VectorStore

    heading("Cortex — registered roots")

    try:
        per_proj = {p["project"]: p for p in VectorStore().get_stats()["per_project"]}
    except Exception:
        per_proj = {}

    rows = []
    for r in registered_roots():
        stats = per_proj.get(r.project or "__unassigned__")
        rows.append([
            r.project or "(unregistered)",
            str(r.path),
            str(stats["files"]) if stats else "0",
            str(stats["documents"]) if stats else "0",
        ])
    console.print(data_table(["Slug", "Path", "Files", "Docs"], rows))


@cortex.command()
@click.option("--root", type=click.Path(exists=True), default=".", help="Project root to enrich.")
@click.option("--dry-run", is_flag=True, help="Show what would be enriched without calling LLM.")
@click.option("--max-files", default=1000, help="Max files per run.")
def enrich(root, dry_run, max_files):
    """Enrich weak-purpose sidecar entries via LLM.

    Scans all directories for .okuro-index.yaml files, finds entries with
    generic purposes ("utils module"), and generates better descriptions
    using your cheapest available model.
    """
    from okuro.cortex.purpose import enrich_sidecar, is_weak_purpose
    from okuro.cortex.sidecar import load, SIDECAR_FILENAME

    root_path = Path(root).resolve()
    heading("Cortex Enrichment")
    info(f"Root: {root_path}")

    # Collect directories with sidecars
    dirs_with_sidecars = []
    weak_count = 0
    total_count = 0

    for dirpath, _dirnames, filenames in os.walk(root_path):
        if SIDECAR_FILENAME in filenames:
            d = Path(dirpath)
            entries = load(d)
            total_count += len(entries)
            weak = sum(1 for e in entries.values() if is_weak_purpose(e.purpose))
            if weak > 0:
                dirs_with_sidecars.append((d, weak, len(entries)))
                weak_count += weak

    if not dirs_with_sidecars:
        ok(f"All {total_count} sidecar entries have strong purposes — nothing to enrich")
        return

    info(f"{weak_count} weak purposes across {len(dirs_with_sidecars)} directories ({total_count} total entries)")

    if dry_run:
        heading("Dry Run — would enrich:")
        for d, weak, total in dirs_with_sidecars:
            rel = d.relative_to(root_path) if d != root_path else Path(".")
            info(f"{rel}/ — {weak}/{total} entries need enrichment")
        return

    # Run enrichment
    enriched_total = 0
    failed_total = 0

    for d, weak, total in dirs_with_sidecars:
        rel = d.relative_to(root_path) if d != root_path else Path(".")
        result = enrich_sidecar(d, root=root_path)

        if result.get("no_bridge"):
            warn("No LLM providers available — configure a CLI (claude, gemini, codex) for enrichment")
            return

        enriched_total += result["enriched"]
        failed_total += result["failed"]

        if result["enriched"] > 0:
            ok(f"{rel}/ — {result['enriched']} enriched")
        elif result["failed"] > 0:
            warn(f"{rel}/ — {result['failed']} failed")

    console.print()
    ok(f"Enriched {enriched_total} of {weak_count} weak purposes ({failed_total} failed)")


@cortex.command()
@click.option("--root", type=click.Path(exists=True), default=".", help="Project root.")
def status(root):
    """Show cortex index coverage and enrichment stats."""
    from okuro.cortex.sidecar import load, SIDECAR_FILENAME
    from okuro.cortex.purpose import is_weak_purpose, ENRICHMENT_LOG

    root_path = Path(root).resolve()
    heading("Cortex Status")
    info(f"Root: {root_path}")

    # Scan for sidecars
    sidecar_dirs = 0
    total_entries = 0
    strong_purposes = 0
    weak_purposes = 0
    by_generator: dict[str, int] = {}

    for dirpath, _dirnames, filenames in os.walk(root_path):
        if SIDECAR_FILENAME in filenames:
            sidecar_dirs += 1
            entries = load(Path(dirpath))
            for entry in entries.values():
                total_entries += 1
                if is_weak_purpose(entry.purpose):
                    weak_purposes += 1
                else:
                    strong_purposes += 1
                gen = entry.generated_by
                by_generator[gen] = by_generator.get(gen, 0) + 1

    # Vectorstore stats
    try:
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        vs_stats = vs.get_stats()
        vs_files = vs_stats.get("files_indexed", 0)
        vs_sections = vs_stats.get("sections_indexed", 0)
    except Exception:
        vs_files = 0
        vs_sections = 0

    # Enrichment log
    enrichment_count = 0
    if ENRICHMENT_LOG.exists():
        try:
            enrichment_count = sum(1 for _ in open(ENRICHMENT_LOG))
        except OSError:
            pass

    console.print()

    rows = [
        ["Sidecar directories", str(sidecar_dirs)],
        ["Sidecar entries", str(total_entries)],
        ["Strong purposes", str(strong_purposes)],
        ["Weak purposes", str(weak_purposes)],
        ["Vectorstore files", str(vs_files)],
        ["Vectorstore sections", str(vs_sections)],
        ["LLM enrichments (total)", str(enrichment_count)],
    ]
    table = data_table(["Metric", "Count"], rows)
    console.print(table)

    if by_generator:
        console.print()
        gen_rows = [[gen, str(count)] for gen, count in sorted(by_generator.items())]
        gen_table = data_table(["Generator", "Entries"], gen_rows)
        console.print(gen_table)

    if weak_purposes > 0:
        console.print()
        coverage = strong_purposes / total_entries * 100 if total_entries else 0
        info(f"Coverage: {coverage:.0f}% — run [bold]okuro cortex enrich[/bold] to improve")


@cortex.command()
@click.option("--root", type=click.Path(exists=True), default=None, help="Project root. Omit with --all to walk every registered root.")
@click.option("--all", "all_roots", is_flag=True, help="Reconcile every registered root.")
def reconcile(root, all_roots):
    """Drop sidecar entries for files that no longer exist.

    Deleted files leave behind stale entries in ``.okuro-index.yaml`` —
    they pollute search and lie about the project's shape. Reconcile walks
    each sidecar, drops orphans, deletes empty stubs.
    """
    from okuro.cortex.sidecar import reconcile_root
    from okuro.cortex.roots import registered_roots

    heading("Cortex Reconcile")

    targets: list[Path] = []
    if all_roots:
        for r in registered_roots():
            if r.path.is_dir():
                targets.append(Path(str(r.path)))
    else:
        targets.append(Path(root or ".").resolve())

    grand = {"directories": 0, "orphans_removed": 0, "kept": 0, "rewrote": 0, "deleted_empty": 0}
    for t in targets:
        res = reconcile_root(t)
        for k in grand:
            grand[k] += res[k]
        ok(
            f"{t}: {res['directories']} dirs, "
            f"{res['orphans_removed']} orphans removed, "
            f"{res['kept']} kept, "
            f"{res['rewrote']} rewrote, "
            f"{res['deleted_empty']} stubs deleted"
        )
    console.print()
    info(
        f"Totals — dirs: {grand['directories']}, "
        f"orphans removed: {grand['orphans_removed']}, "
        f"kept: {grand['kept']}, "
        f"rewrote: {grand['rewrote']}, "
        f"stubs deleted: {grand['deleted_empty']}"
    )


@cortex.command()
@click.option("--root", type=click.Path(exists=True), default=None, help="Project root. Omit with --all to walk every registered root.")
@click.option("--all", "all_roots", is_flag=True, help="Backfill every registered root.")
@click.option("--dry-run", is_flag=True, help="Show file counts, write nothing.")
def backfill(root, all_roots, dry_run):
    """Full (non-incremental) scan — write a sidecar entry for every eligible
    file, regardless of git marker state.

    First-time projects need this; incremental git-based discovery only
    sees files that have changed since the last marker, so a freshly
    registered root sits at 0% coverage forever without an explicit
    backfill.
    """
    from okuro.cortex.scanner import scan_directory
    from okuro.cortex.roots import registered_roots

    heading("Cortex Backfill")

    targets: list[Path] = []
    if all_roots:
        for r in registered_roots():
            if r.path.is_dir():
                targets.append(Path(str(r.path)))
    else:
        targets.append(Path(root or ".").resolve())

    total_scanned = 0
    total_updated = 0
    for t in targets:
        try:
            results, updated = scan_directory(
                t, incremental=False, dry_run=dry_run
            )
            total_scanned += len(results)
            total_updated += updated
            ok(f"{t}: scanned {len(results)} files, {updated} updated")
        except Exception as exc:
            warn(f"{t}: failed — {exc}")
    console.print()
    info(f"Totals — scanned {total_scanned}, updated {total_updated}")


@cortex.command()
@click.option("--limit", default=20, help="Number of most-recent failures to show.")
@click.option("--stage", default=None, help="Filter by stage (bridge|parse|weak|read|write).")
def failures(limit, stage):
    """Show recent enrichment failures.

    Each enrichment attempt that fails is appended to
    ``~/.okuro/cortex/failures.jsonl`` with file, stage, and error. Files
    that fail FAILURE_RETRY_BUDGET times within the window are skipped to
    save LLM cycles — this command surfaces them so you can investigate.
    """
    import json
    from okuro.cortex.purpose import FAILURES_LOG

    heading("Cortex — recent enrichment failures")
    if not FAILURES_LOG.is_file():
        info("No failures recorded yet.")
        return

    try:
        size = FAILURES_LOG.stat().st_size
        with open(FAILURES_LOG, "rb") as f:
            if size > 200_000:
                f.seek(size - 200_000)
                f.readline()
            tail = f.read().decode("utf-8", errors="replace")
    except OSError as exc:
        warn(f"Could not read failures log: {exc}")
        return

    events: list[dict] = []
    for raw in tail.splitlines():
        try:
            events.append(json.loads(raw))
        except (ValueError, json.JSONDecodeError):
            continue

    if stage:
        events = [e for e in events if e.get("stage") == stage]
    events = list(reversed(events))[:limit]

    if not events:
        info("No matching failures.")
        return

    rows = [
        [
            e.get("timestamp", "")[:19],
            e.get("stage", ""),
            (e.get("file", "") or "")[-60:],
            (e.get("error", "") or "")[:60],
        ]
        for e in events
    ]
    console.print(data_table(["Timestamp", "Stage", "File", "Error"], rows))


@cortex.command()
@click.option("--root", type=click.Path(exists=True), default=".", help="Project root.")
@click.option("--full", is_flag=True, help="Full rescan (ignore git markers).")
@click.option("--force", is_flag=True, help="Re-embed every file even if unchanged (needed after a chunking/embedding config change).")
def refresh(root, full, force):
    """Rescan files and rebuild sidecars + vectorstore index."""
    from okuro.cortex.scanner import scan_directory
    from okuro.cortex.vectorstore import VectorStore

    root_path = Path(root).resolve()
    heading("Cortex Refresh")

    # 1. Scan + update headers/sidecars
    try:
        results, updated = scan_directory(
            root_path, incremental=not full, dry_run=False
        )
        ok(f"Scanned {len(results)} files, {updated} updated")
    except Exception as e:
        warn(f"Scan failed: {e}")
        return

    # 2. Rebuild vectorstore
    try:
        vs = VectorStore()
        total, indexed = vs.index_directory(root_path, force=force)
        ok(f"Indexed {indexed} of {total} files")
    except Exception as e:
        warn(f"Indexing failed: {e}")
