# SPDX-License-Identifier: Apache-2.0
"""Import an Obsidian vault into okuro notes, preserving its folder tree.

Why this exists: okuro-notes is the native note surface (see the deferred
``obsidian-sync`` daemon task, which fed the *thought* DB and was superseded).
Nothing ever moved the vault's markdown files into notes, so the vault stayed
the only copy. This module does that move, once, idempotently.

Design notes:
  * Vault subfolders are mirrored as okuro folders under a single root, so the
    user's mental map of the vault survives the move.
  * Provenance lives in each note's frontmatter as ``obsidian_source_path``.
    That is also the idempotency key — a re-run skips what it already imported
    rather than creating duplicates.
  * Syncthing/Obsidian machinery (``.obsidian``, ``.trash``, ``.stversions``,
    ``.stfolder``) is excluded. ``.stversions`` in particular holds stale
    revision copies that would import as convincing-looking duplicates.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from okuro.notes.storage import create_folder, list_folders, upsert_note

def _default_vault() -> Path:
    """The user's vault from conventions; a neutral guess otherwise."""
    from okuro.yu.conventions import get_convention

    return Path(get_convention("notes.obsidian_vault", "~/Obsidian")).expanduser()


DEFAULT_VAULT = _default_vault()
DEFAULT_ROOT_FOLDER = "Obsidian Notes"
ORIGIN = "obsidian-import"

# Directory names that are tooling, not user content.
EXCLUDED_DIRS = {".obsidian", ".trash", ".stversions", ".stfolder", ".git"}


def _iter_markdown(vault: Path) -> Iterable[Path]:
    """Yield every user-authored .md file in the vault, deepest paths included.

    Prunes excluded directories in-place so we never descend into them — this
    is what keeps .stversions revision copies out of the import.
    """
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            if name.lower().endswith(".md"):
                yield Path(dirpath) / name


def _existing_source_paths() -> set[str]:
    """Relative vault paths already imported, read from note frontmatter.

    Queries the DB directly: list_notes() returns row metadata without
    frontmatter, so it cannot answer this.
    """
    from okuro.db import get_db  # local import: keeps module import cheap

    db = get_db()
    rows = db.execute(
        "SELECT json_extract(frontmatter, '$.obsidian_source_path') AS src "
        "FROM notes WHERE frontmatter LIKE '%obsidian_source_path%'"
    ).fetchall()
    # Rows are dict-like (keyed by column name), not tuples — index by alias.
    return {r["src"] for r in rows if r and r["src"]}


def _apply_source_mtime(note_id: str, path: Path) -> None:
    """Backdate an imported note to its source file's mtime.

    upsert_note stamps created_at/updated_at with "now". Left alone, a bulk
    import lands every note at the same instant at the very top of
    ``ORDER BY updated_at DESC`` — and since the notes list API caps at 200
    rows, an import larger than that cap silently pushes the user's own notes
    off the end of their sidebar. It looks exactly like data loss.

    Preserving the vault's real dates is both the fix and the truthful value.
    """
    from okuro.db import get_db

    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    get_db().execute(
        "UPDATE notes SET created_at = ?, updated_at = ? WHERE id = ?",
        (ts, ts, note_id),
    )


def _folder_index() -> dict[tuple[Optional[str], str], str]:
    """Map (parent_id, name) -> folder_id for every existing folder.

    Lets us reuse folders across runs instead of creating a second "work"
    folder every time the importer is invoked.
    """
    return {(f.get("parent_id"), f["name"]): f["id"] for f in list_folders()}


def _ensure_folder_path(
    parts: tuple[str, ...],
    root_id: str,
    index: dict[tuple[Optional[str], str], str],
) -> str:
    """Resolve (creating as needed) the folder chain for a vault subpath.

    ``parts`` is the vault-relative directory chain, e.g. ("work", "2026").
    Returns the id of the deepest folder. Mutates ``index`` so sibling files
    reuse folders created earlier in the same run.
    """
    parent = root_id
    for part in parts:
        key = (parent, part)
        if key not in index:
            index[key] = create_folder(part, parent_id=parent)["id"]
        parent = index[key]
    return parent


def import_vault(
    vault: Path = DEFAULT_VAULT,
    root_folder: str = DEFAULT_ROOT_FOLDER,
    project: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """Import every markdown file under ``vault`` into okuro notes.

    Returns a summary dict. With ``dry_run=True`` (the default) nothing is
    written — it reports what a real run would do, so the counts can be
    checked before touching the user's note surface.
    """
    vault = Path(vault).expanduser()
    if not vault.is_dir():
        raise FileNotFoundError(f"vault not found: {vault}")

    files = list(_iter_markdown(vault))
    already = _existing_source_paths()

    pending: list[tuple[Path, str]] = []
    for path in files:
        rel = path.relative_to(vault).as_posix()
        if rel not in already:
            pending.append((path, rel))

    summary = {
        "vault": str(vault),
        "root_folder": root_folder,
        "markdown_files": len(files),
        "already_imported": len(files) - len(pending),
        "to_import": len(pending),
        "imported": 0,
        "folders_created": 0,
        "errors": [],
        "dry_run": dry_run,
    }

    if dry_run or not pending:
        summary["folders_needed"] = len(
            {p.relative_to(vault).parent.as_posix() for p, _ in pending}
        )
        return summary

    index = _folder_index()
    root_id = index.get((None, root_folder)) or create_folder(root_folder)["id"]
    index[(None, root_folder)] = root_id
    folders_before = len(index)

    for path, rel in pending:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            summary["errors"].append({"path": rel, "error": f"read: {exc}"})
            continue

        rel_parent = path.relative_to(vault).parent
        parts = tuple(p for p in rel_parent.parts if p not in (".", ""))
        try:
            folder_id = _ensure_folder_path(parts, root_id, index)
            note = upsert_note(
                title=path.stem,
                body=body,
                frontmatter={
                    "obsidian_source_path": rel,
                    "imported_from": "obsidian",
                },
                project=project,
                folder_id=folder_id,
                origin=ORIGIN,
            )
            _apply_source_mtime(note["id"], path)
            summary["imported"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the run
            summary["errors"].append({"path": rel, "error": str(exc)})

    summary["folders_created"] = len(index) - folders_before
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Import an Obsidian vault into okuro notes.")
    ap.add_argument("--vault", default=str(DEFAULT_VAULT))
    ap.add_argument("--root-folder", default=DEFAULT_ROOT_FOLDER)
    ap.add_argument("--project", default=None)
    ap.add_argument("--execute", action="store_true", help="actually write (default is dry-run)")
    args = ap.parse_args(argv)

    result = import_vault(
        vault=Path(args.vault),
        root_folder=args.root_folder,
        project=args.project,
        dry_run=not args.execute,
    )
    print(json.dumps(result, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
