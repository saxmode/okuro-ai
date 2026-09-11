# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Managed-corpus lifecycle — add/sync/remove a non-git knowledge source,
#   materialize it as markdown, register it as a project (→ cortex root) and
#   index it. Delta sync driven by per-item version tokens in an on-disk manifest.
# index:
#   def add_corpus
#   def sync_corpus
#   def remove_corpus
#   def list_corpora
#   def get_corpus
#   def _materialize
#   def _read_manifest
#   def _write_manifest
#   def _register_project
#   def _index_cortex
# AGENT_HEADER_END -->
"""Turn any enumerable document source into a cortex-searchable project.

Flow (add_corpus):
  1. build the adapter for source_type from its config
  2. enumerate items (cheap — version tokens only)
  3. materialize changed items as markdown under
     <data-dir>/corpora/<workspace>/<name>   (skipped for non-materializing
     adapters, whose path IS the source directory)
  4. register the directory as a project so it becomes a cortex root
  5. cortex index → searchable via cortex_search(project=<id>)
  6. status: pending → fetching → indexing → ready | error

Why no new search tool: step 4 is the whole trick. Once the directory is a
project, every cortex_* tool already works on it — cortex_search,
cortex_route, cortex_read_header, cortex_search_code. A dedicated wiki_search
would be a second query surface over the same vectors, in the same embedding
space, with none of the ranking work cortex already does.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from okuro.corpus.adapters import build_adapter, detect_source
from okuro.corpus.adapters.base import frontmatter
from okuro.corpus.paths import corpus_id, corpus_path, safe_segment, slugify

log = logging.getLogger(__name__)

MANIFEST_NAME = ".okuro-corpus.json"

# Fetch failures are per-item and expected at scale (a page deleted between
# enumerate and fetch, a transient 500). A sync that aborts on the first one
# leaves a half-written corpus; a sync that ignores all of them silently ships
# an incomplete index. So: tolerate up to this FRACTION, then fail loudly.
_MAX_FAILURE_RATIO = 0.10


# ── manifest ─────────────────────────────────────────────────────────
#
# {key: {"version": str, "rel_path": str}} — the delta ledger. Kept on disk
# rather than in the DB so it cannot disagree with the files it describes
# (both are written in the same step).
#
# It lives OUTSIDE the indexed tree, under corpora/_state/<slug>/. Inside, it
# would be indexed as content: `.json` is in cortex's default extension set, so
# the first live run indexed a 363-entry ledger of paths and version numbers as
# a searchable document (364 files for 363 pages). A corpus directory holds
# content and nothing else — that also keeps local_folder from dropping a
# dotfile into the user's own vault, which is the same rule, not a special case.


def _state_dir(slug: str) -> Path:
    """Where okuro keeps sync state for a corpus, never inside the corpus."""
    from okuro.corpus.paths import corpora_root

    d = corpora_root() / "_state" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_manifest(state: Path) -> dict[str, dict]:
    f = state / MANIFEST_NAME
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data.get("items", {}) if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        # A corrupt manifest must degrade to "everything is new", never to a
        # crash — the corpus is still fully rebuildable from the source.
        log.warning("corpus manifest unreadable at %s (%s) — treating as empty", f, exc)
        return {}


def _write_manifest(state: Path, items: dict[str, dict], source: dict) -> None:
    payload = {"version": 1, "source": source, "items": items}
    tmp = state / f"{MANIFEST_NAME}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state / MANIFEST_NAME)  # atomic — a killed sync never truncates it


# ── materialization ──────────────────────────────────────────────────


def _rel_path_for(ref, taken: set[str]) -> str:
    """Build the on-disk relative path for an item, de-duplicating collisions.

    The path mirrors the source hierarchy so a cortex hit's file path reads as
    the breadcrumb ("DRUCKSHOP/Produkte/Artikeltyp: Buch.md"). Two siblings can
    legitimately share a title, so a collision appends the source key — stable
    across syncs, unlike a counter, which would renumber on every reordering
    and churn the whole index.
    """
    parts = [safe_segment(s) for s in ref.segments]
    stem = safe_segment(ref.title, fallback=f"item-{ref.key}")
    candidate = "/".join([*parts, f"{stem}.md"])
    if candidate.lower() in taken:
        candidate = "/".join([*parts, f"{stem} ({safe_segment(ref.key)}).md"])
    taken.add(candidate.lower())
    return candidate


def _materialize(adapter, root: Path, refs: list, prev: dict[str, dict],
                 force: bool = False) -> tuple[dict[str, dict], dict]:
    """Write changed items to disk; return (new manifest, change summary).

    Only items whose version token differs from the manifest are fetched. An
    item whose file has vanished is re-fetched regardless of token — the token
    tracks the SOURCE, and a missing file means our copy is the stale one.
    """
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    taken: set[str] = set()
    added = updated = unchanged = failed = 0
    errors: list[str] = []

    for ref in refs:
        rel = _rel_path_for(ref, taken)
        dest = root / rel
        old = prev.get(ref.key)
        fresh = (
            not force
            and old is not None
            and old.get("version") == ref.version
            and (root / old.get("rel_path", "")).exists()
        )
        if fresh:
            old_rel = old["rel_path"]
            if old_rel != rel:
                # Title or ancestry changed → the content is the same but its
                # place is not. Move rather than re-fetch.
                src = root / old_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    src.replace(dest)
                except OSError:
                    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                    src.unlink(missing_ok=True)
            manifest[ref.key] = {"version": ref.version, "rel_path": rel}
            unchanged += 1
            continue

        try:
            item = adapter.fetch(ref)
        except Exception as exc:  # noqa: BLE001 — per-item, budgeted below
            failed += 1
            errors.append(f"{ref.title}: {str(exc)[:120]}")
            log.warning("corpus fetch failed for %s (%s): %s", ref.title, ref.key, exc)
            # Keep the previous copy if we have one — a failed refresh must not
            # silently delete content that is still valid.
            if old and (root / old.get("rel_path", "")).exists():
                manifest[ref.key] = old
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"{frontmatter(item)}\n\n# {ref.title}\n\n{item.body}\n", encoding="utf-8")
        manifest[ref.key] = {"version": ref.version, "rel_path": rel}
        if old:
            updated += 1
        else:
            added += 1

    # Items that disappeared from the source lose their file, so a deleted wiki
    # page stops being retrievable instead of lingering as a confident answer.
    removed = 0
    dropped: list[str] = []
    live_paths = {m["rel_path"] for m in manifest.values()}
    for key, old in prev.items():
        if key in manifest:
            continue
        stale = old.get("rel_path")
        if stale and stale not in live_paths:
            (root / stale).unlink(missing_ok=True)
            dropped.append(str(root / stale))
            removed += 1

    # Deleting the FILE is not enough: cortex keeps its rows and vectors until
    # something tombstones them, so the page stays retrievable by cortex_search
    # after it is gone from the wiki. Verified live — a manifest file deleted
    # from disk still answered a scoped query afterwards.
    _reclaim_paths(dropped)
    _prune_empty_dirs(root)

    total = len(refs)
    if failed and total and (failed / total) > _MAX_FAILURE_RATIO:
        raise RuntimeError(
            f"{failed}/{total} items failed to fetch (>{_MAX_FAILURE_RATIO:.0%} budget). "
            f"First errors: {'; '.join(errors[:3])}"
        )

    change = {
        "added": added, "updated": updated, "unchanged": unchanged,
        "removed": removed, "failed": failed,
    }
    if errors:
        change["errors"] = errors[:10]
    return manifest, change


def _reclaim_paths(paths: list[str]) -> None:
    """Tombstone cortex docs for files that no longer exist, and drop vectors.

    Reuses cortex's own hygiene primitive rather than deleting rows here — it
    soft-deletes the doc (preserving history, matching cortex_prune_stale) while
    hard-deleting the vector so the dead row stops bloating every KNN scan.
    Best-effort: a corpus whose files are correct on disk must not fail because
    index cleanup did.
    """
    if not paths:
        return
    try:
        from okuro.cortex.reclaim import tombstone_and_purge
        from okuro.db import get_db

        stats = tombstone_and_purge(get_db(), paths)
        log.info("corpus reclaim: %s", stats)
    except Exception as exc:  # noqa: BLE001 — index hygiene is never fatal
        log.warning("corpus reclaim skipped for %d path(s): %s", len(paths), exc)


def _prune_empty_dirs(root: Path) -> None:
    """Remove directories left empty by a rename or deletion. Best-effort."""
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            next(d.iterdir())
        except StopIteration:
            d.rmdir()
        except OSError:
            pass


# ── registration / indexing ──────────────────────────────────────────


def _register_project(db, slug: str, name: str, path: Path, source_ref: str) -> None:
    """Register the corpus directory as a project so it becomes a cortex root.

    Mirrors repos.lifecycle._register_project, minus stack detection — a wiki
    has no package.json, and running the detector over prose would just record
    a misleading empty stack. Kept as its own function rather than importing
    the repos private: the two will diverge (corpora will grow a language/
    freshness axis that clones do not have), and a shared private import would
    make that divergence a refactor of someone else's module.
    """
    db.execute(
        "INSERT OR IGNORE INTO projects (id, name, path, url, stack, active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (slug, name, str(path), source_ref, json.dumps([])),
    )
    db.execute(
        "UPDATE projects SET path = ?, url = ?, active = 1, indexed = 1 WHERE id = ?",
        (str(path), source_ref, slug),
    )
    db.conn.commit()


def _index_cortex(path: Path, slug: str, force: bool = False,
                  extensions: set[str] | None = None) -> dict:
    """Index the corpus into cortex. Best-effort, same contract as repo add.

    A cortex failure (embed service down) must not fail the whole add — the
    files are on disk and the daemon's next refresh will pick them up. The
    return value reports what actually happened so the caller can say
    "indexed 363" or "deferred", never guess.

    ``extensions`` lets an adapter widen what cortex indexes for ITS corpus
    only. cortex's default set is code-oriented and omits .txt/.rst/.org — fine
    for a repo, wrong for a documents folder, where those files would be
    enumerated by the adapter and then silently missing from search.
    """
    try:
        from okuro.cortex.vectorstore import VectorStore

        kwargs = {"project": slug, "force": force}
        if extensions:
            kwargs["extensions"] = set(extensions)
        total, indexed = VectorStore().index_directory(path, **kwargs)
        return {"indexed": True, "files_seen": total, "files_indexed": indexed}
    except Exception as exc:  # noqa: BLE001 — cortex is best-effort here
        log.warning("cortex index for corpus %s deferred to daemon (%s)", slug, exc)
        return {"indexed": False, "deferred_reason": str(exc)[:300]}


# ── DB helpers ───────────────────────────────────────────────────────


def _row_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "source_type": r["source_type"],
        "source_ref": r["source_ref"],
        "workspace": r["workspace"],
        "name": r["name"],
        "path": r["path"],
        "materialized": bool(r["materialized"]),
        "config": json.loads(r["config"]) if r["config"] else {},
        "status": r["status"],
        "project_id": r["project_id"],
        "token_key": r["token_key"],
        "username": r["username"],
        "item_count": r["item_count"],
        "error": r["error"],
        "last_change": json.loads(r["last_change"]) if r["last_change"] else None,
        "last_synced_at": r["last_synced_at"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _set_status(db, cid: str, status: str, error: str | None = None) -> None:
    db.execute(
        "UPDATE managed_corpora SET status = ?, error = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (status, error, cid),
    )
    db.conn.commit()


def _config_with_credentials(config: dict, token_key: str | None, username: str | None) -> dict:
    """Merge credential POINTERS into the adapter config.

    token_key is a keyring entry NAME, never a secret — the token itself is
    resolved inside the adapter at call time and never persisted.
    """
    merged = dict(config)
    if token_key:
        merged["token_key"] = token_key
    if username:
        merged["username"] = username
    return merged


# ── public API ───────────────────────────────────────────────────────


def add_corpus(
    source_type: str | None = None,
    config: dict | None = None,
    name: str | None = None,
    workspace: str = "default",
    token_key: str | None = None,
    username: str | None = None,
    url: str | None = None,
) -> dict:
    """Register a document source as a cortex-searchable corpus.

    Args:
        source_type: adapter id — 'confluence' | 'local_folder'. Omit when
                     passing ``url``.
        config:      adapter config. confluence: {base_url, space_key,
                     include_archived?}; local_folder: {path, include?,
                     exclude?, extensions?}.
        name:        corpus name (defaults to the space key / folder name).
        workspace:   grouping bucket. Default 'default'.
        token_key:   keyring entry holding an API token, for private sources.
        username:    Atlassian account EMAIL paired with the token.
        url:         a pasted source url or path. When given, source_type and
                     the base config are DERIVED from it — this is the one-field
                     path the UI uses. Anything in ``config`` still wins, so a
                     caller can paste a link and override include_archived.

    Returns the managed_corpora row as a dict. Raises on failure, persisting
    status='error' with the message first.
    """
    from okuro.db import get_db

    config = dict(config or {})
    if url:
        detected = detect_source(url)
        source_type = source_type or detected["source_type"]
        config = {**detected["config"], **config}
    if not source_type:
        raise ValueError("pass either source_type + config, or url")

    full_config = _config_with_credentials(config, token_key, username)
    adapter = build_adapter(source_type, full_config)

    name = name or _default_name(source_type, full_config)
    ws = slugify(workspace)
    slug = corpus_id(ws, name)

    db = get_db()
    if db.fetchone("SELECT id FROM managed_corpora WHERE id = ?", (slug,)):
        raise ValueError(f"corpus '{slug}' already managed — use corpus_sync to update")
    if db.fetchone("SELECT id FROM managed_repos WHERE id = ?", (slug,)):
        raise ValueError(
            f"id '{slug}' is already a managed REPO — corpora and repos share the "
            f"project namespace, so pick a different name"
        )

    if adapter.materializes:
        dest = corpus_path(ws, name)
    else:
        if hasattr(adapter, "validate"):
            adapter.validate()
        dest = Path(full_config["path"]).expanduser().resolve()

    source_ref = _source_ref(source_type, full_config)
    db.execute(
        "INSERT INTO managed_corpora "
        "(id, source_type, source_ref, workspace, name, path, materialized, config, "
        " status, project_id, token_key, username) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (slug, source_type, source_ref, ws, name, str(dest),
         1 if adapter.materializes else 0,
         json.dumps(_redacted_config(full_config)), slug, token_key, username),
    )
    db.conn.commit()

    try:
        result = _run_sync(db, slug, adapter, dest, force=False)
    except Exception as exc:
        _set_status(db, slug, "error", str(exc)[:2000])
        raise
    finally:
        _close(adapter)

    log.info("corpus %s ready: %s", slug, result)
    return get_corpus(slug)


def sync_corpus(corpus_id_or_slug: str, full: bool = False) -> dict:
    """Re-enumerate the source and refresh only what changed.

    ``full=True`` re-fetches every item and forces a cortex re-index — needed
    after a chunking/embedding config change, which the per-item version token
    cannot see (it tracks the source, not our processing of it).
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM managed_corpora WHERE id = ?", (corpus_id_or_slug,))
    if not row:
        raise ValueError(f"unknown managed corpus '{corpus_id_or_slug}'")
    rec = _row_to_dict(row)

    config = _config_with_credentials(rec["config"], rec["token_key"], rec["username"])
    adapter = build_adapter(rec["source_type"], config)
    dest = Path(rec["path"])

    if not adapter.materializes and not dest.exists():
        _set_status(db, rec["id"], "error", f"source folder missing: {dest}")
        raise RuntimeError(f"source folder missing: {dest}")

    try:
        result = _run_sync(db, rec["id"], adapter, dest, force=full)
    except Exception as exc:
        _set_status(db, rec["id"], "error", str(exc)[:2000])
        raise
    finally:
        _close(adapter)

    log.info("corpus %s synced: %s", rec["id"], result)
    return get_corpus(rec["id"])


def _run_sync(db, slug: str, adapter, dest: Path, force: bool) -> dict:
    """Shared add/sync body: enumerate → materialize → register → index."""
    _set_status(db, slug, "fetching")
    refs = list(adapter.enumerate())
    state = _state_dir(slug)
    prev = _read_manifest(state)

    if adapter.materializes:
        manifest, change = _materialize(adapter, dest, refs, prev, force=force)
    else:
        # Nothing is copied. The delta is still computed so the caller learns
        # what moved, but cortex's own content hashing is the real gate for
        # what gets re-embedded.
        manifest = {r.key: {"version": r.version, "rel_path": r.key} for r in refs}
        change = {
            "added": len([k for k in manifest if k not in prev]),
            "updated": len([k for k, v in manifest.items()
                            if k in prev and prev[k].get("version") != v["version"]]),
            "unchanged": len([k for k, v in manifest.items()
                              if k in prev and prev[k].get("version") == v["version"]]),
            "removed": len([k for k in prev if k not in manifest]),
            "failed": 0,
        }

    _write_manifest(state, manifest, adapter.describe())
    item_count = len(manifest)

    _set_status(db, slug, "indexing")
    _register_project(db, slug, slug.split("__", 1)[-1], dest, _describe_ref(adapter))
    index_result = _index_cortex(
        dest, slug, force=force, extensions=getattr(adapter, "index_extensions", None)
    )
    change["cortex"] = index_result

    db.execute(
        "UPDATE managed_corpora SET status = 'ready', item_count = ?, "
        "last_synced_at = datetime('now'), last_change = ?, error = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (item_count, json.dumps(change), slug),
    )
    db.conn.commit()
    return change


def remove_corpus(corpus_id_or_slug: str, delete_files: bool = False) -> dict:
    """Remove a corpus from the registry and deactivate its project.

    ``delete_files`` is honoured ONLY for materialized corpora living under the
    corpora root. A local_folder corpus points at the user's own directory, and
    deleting that is never okuro's call — the flag is refused there rather than
    silently ignored, so the caller learns their files are still present.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM managed_corpora WHERE id = ?", (corpus_id_or_slug,))
    if not row:
        raise ValueError(f"unknown managed corpus '{corpus_id_or_slug}'")
    rec = _row_to_dict(row)

    import shutil

    files_deleted = False
    if delete_files:
        if not rec["materialized"]:
            raise ValueError(
                f"refusing to delete files for '{rec['id']}': this corpus indexes your own "
                f"folder ({rec['path']}) in place. Remove without delete_files, then delete "
                f"the folder yourself if that is what you want."
            )
        from okuro.corpus.paths import corpora_root

        root = Path(rec["path"])
        if corpora_root() in root.parents and root.exists():
            shutil.rmtree(root, ignore_errors=True)
            files_deleted = True

    # Sync state is okuro's own bookkeeping, so it goes whenever the corpus is
    # deregistered — regardless of delete_files, which governs CONTENT only. A
    # surviving manifest would make a later re-add believe 363 pages are already
    # materialized and skip fetching every one of them.
    shutil.rmtree(_state_dir(rec["id"]), ignore_errors=True)

    # Deactivating the project stops FUTURE indexing; it does not retract what
    # is already indexed. Without this, a removed corpus keeps answering
    # cortex_search from rows whose files are gone. Only for materialized
    # corpora: a local_folder's files are the user's and still exist, so their
    # index entries stay valid until they deregister the path itself.
    reclaimed = {}
    if rec["materialized"]:
        reclaimed = _reclaim_project(rec["project_id"] or rec["id"])

    if rec["project_id"]:
        db.execute(
            "UPDATE projects SET active = 0, indexed = 0 WHERE id = ?",
            (rec["project_id"],),
        )
    db.execute("DELETE FROM managed_corpora WHERE id = ?", (rec["id"],))
    db.conn.commit()

    rec["status"] = "removed"
    rec["files_deleted"] = files_deleted
    rec["reclaimed"] = reclaimed
    return rec


def _reclaim_project(project: str) -> dict:
    """Tombstone every live cortex doc belonging to ``project``."""
    try:
        from okuro.cortex.reclaim import tombstone_and_purge
        from okuro.db import get_db

        db = get_db()
        paths = [
            r["file_path"]
            for r in db.fetchall(
                "SELECT DISTINCT file_path FROM cortex_docs "
                "WHERE project = ? AND deleted_at IS NULL",
                (project,),
            )
        ]
        stats = tombstone_and_purge(db, paths)
        log.info("corpus %s reclaimed from cortex: %s", project, stats)
        return stats
    except Exception as exc:  # noqa: BLE001 — index hygiene is never fatal
        log.warning("corpus reclaim for project %s skipped: %s", project, exc)
        return {"error": str(exc)[:200]}


def list_corpora(workspace: str | None = None) -> list[dict]:
    """List managed corpora, optionally filtered by workspace."""
    from okuro.db import get_db

    db = get_db()
    if workspace:
        rows = db.fetchall(
            "SELECT * FROM managed_corpora WHERE workspace = ? ORDER BY workspace, name",
            (slugify(workspace),),
        )
    else:
        rows = db.fetchall("SELECT * FROM managed_corpora ORDER BY workspace, name")
    return [_row_to_dict(r) for r in rows]


def get_corpus(corpus_id_or_slug: str) -> dict | None:
    """Fetch one managed corpus by id."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM managed_corpora WHERE id = ?", (corpus_id_or_slug,))
    return _row_to_dict(row) if row else None


# ── small helpers ────────────────────────────────────────────────────


def _default_name(source_type: str, config: dict) -> str:
    if source_type == "confluence":
        return config.get("space_key") or "confluence"
    if source_type == "local_folder":
        return Path(config["path"]).expanduser().resolve().name or "folder"
    return source_type


def _source_ref(source_type: str, config: dict) -> str:
    if source_type == "confluence":
        return f"{config.get('base_url', '').rstrip('/')}/spaces/{config.get('space_key', '')}"
    if source_type == "local_folder":
        return str(Path(config["path"]).expanduser().resolve())
    return source_type


def _describe_ref(adapter) -> str:
    d = adapter.describe()
    return d.get("base_url") or d.get("path") or adapter.source_type


def _redacted_config(config: dict) -> dict:
    """Config as persisted. token_key is a NAME and safe; a literal is not.

    Defence in depth: the API takes a keyring name, but a caller who passes a
    raw ``token``/``password`` key anyway must not have it written to the DB.
    """
    return {k: v for k, v in config.items() if k not in ("token", "password", "secret", "api_key")}


def _close(adapter) -> None:
    if hasattr(adapter, "close"):
        try:
            adapter.close()
        except Exception:  # noqa: BLE001 — teardown must never mask a real error
            pass
