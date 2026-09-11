# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro state backup + restore — WAL-consistent okuro.db snapshots
#   plus EVERYTHING else in ~/.okuro that is not a regenerable cache, used by
#   `okuro backup` and the update flow. The corruption guard is stopping the
#   DB-writer services, not closing windows.
# index:
#   paths
#   def create_backup
#   def list_backups
#   def restore_backup
#   service control
# AGENT_HEADER_END -->
"""okuro state backup + restore.

Every backup is a self-contained directory under ``~/.okuro/backups/``:

    <UTC-timestamp>-<label>/
        okuro.db          WAL-consistent online copy (sqlite3 .backup API)
        <every other entry of ~/.okuro>   config.yaml, keyring/, tokens,
                          corpora/, deliveries/, roles/, guard/, … — copied
                          as-is; other *.db files via the sqlite backup API
        manifest.json     timestamp, label, git sha, schema version, row
                          counts, the entries carried and the ones excluded

WHAT IS EXCLUDED, AND WHY IT IS A DENYLIST. The backup used to be an
allowlist — db, keyring, then three more folders added in 2026-08 — and it
drifted every time ~/.okuro gained a new kind of data: measured 2026-09-09,
config.yaml, the API tokens, corpora, deliveries and attachments (~1.8 GB
of data with no other copy) were not in it. The update stakes a user's data
on this backup, so the rule is inverted: EVERYTHING is carried except the
entries in EXCLUDED_ENTRIES, which are regenerable caches (models, indexes,
build output), clones with their own remotes, and the backups themselves.
A new kind of user data is therefore backed up on the day it appears.

Restore stops the DB-writer services first (so no stale process holds the
DB open), snapshots the CURRENT state as a safety net, swaps the files in,
then restarts. Pure SQLite + the OS-abstracting service manager → identical
on Linux (systemd-user) and macOS (launchd).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import os

from okuro.keyring.storage import real_user_home

# Number of full backups to keep. Each is a complete copy of okuro.db, which
# can be multi-GB (cortex index + event telemetry), so the default is modest.
# Override with OKURO_BACKUP_RETENTION for machines with small DBs / lots of disk.
try:
    RETENTION = max(1, int(os.environ.get("OKURO_BACKUP_RETENTION", "5")))
except ValueError:
    RETENTION = 5

# Total byte budget for all full backups of a single okuro.db. A multi-GB DB
# (cortex index + event telemetry) × RETENTION would dwarf the disk, so the
# effective retention is ALSO capped by this budget. Override with
# OKURO_BACKUP_MAX_BYTES. Default 50 GB.
_DEFAULT_MAX_BACKUP_BYTES = 50 * 1024 ** 3


def _max_backup_bytes() -> int:
    try:
        return max(1, int(os.environ.get("OKURO_BACKUP_MAX_BYTES",
                                         str(_DEFAULT_MAX_BACKUP_BYTES))))
    except ValueError:
        return _DEFAULT_MAX_BACKUP_BYTES


def _effective_keep(db_size: int) -> int:
    """Retention capped so total full backups stay within the byte budget.

    A 24 GB DB with the 50 GB budget keeps 2; a 50 MB DB keeps the full
    RETENTION. Always at least 1.
    """
    if db_size <= 0:
        return RETENTION
    return min(RETENTION, max(1, _max_backup_bytes() // db_size))

#: Top-level entries of ~/.okuro that a backup deliberately does NOT carry.
#: Every one is regenerable from the network, from the repo, or from the
#: data that IS carried — or is itself a backup. Anything not listed here
#: ships in every backup, by construction.
EXCLUDED_ENTRIES = frozenset({
    "backups", ".deploy-backups",          # the backups themselves
    "hf", "models", "comfyui",             # downloaded model weights
    "cortex", "prism-cache", "cache",      # indexes and caches rebuilt by the daemon
    "webview", "voice-previews",           # build output / generated previews
    "repos",                               # managed clones with their own remotes
    "proof",                               # regenerable screenshots
    "logs",                                # runtime logs
})

#: The main database and its WAL sidecars — copied via the sqlite backup
#: API, never as files.
_DB_ENTRIES = ("okuro.db", "okuro.db-wal", "okuro.db-shm")

# Stop order = writers last-to-first; start order = the reverse. embed does
# not write okuro.db but is cycled for a clean, consistent restart.
_STOP_ORDER = ["okuro-orchestrator", "okuro-daemon", "okuro-embed"]
_START_ORDER = ["okuro-embed", "okuro-daemon", "okuro-orchestrator"]


# ── paths ───────────────────────────────────────────────────────────────────

def okuro_dir() -> Path:
    return real_user_home() / ".okuro"


def db_path() -> Path:
    return okuro_dir() / "okuro.db"


def backups_dir() -> Path:
    return okuro_dir() / "backups"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _schema_version(db_file: Path) -> Optional[str]:
    """Latest applied migration name from the ``_migrations`` table."""
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT name FROM _migrations ORDER BY name DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except Exception:
        return None


_COUNT_TABLES = [
    "tasks", "agent_memory", "persons", "artifacts", "progress",
    "roles", "sessions", "todos", "reminders", "thoughts",
]


def _row_counts(db_file: Path) -> dict:
    counts: dict = {}
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            for t in _COUNT_TABLES:
                try:
                    counts[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                except Exception:
                    pass
        finally:
            con.close()
    except Exception:
        pass
    return counts


def _git_sha() -> Optional[str]:
    """HEAD sha of the okuro source tree this code runs from (best-effort)."""
    try:
        repo = Path(__file__).resolve().parents[3]  # src/okuro/system/.. → repo
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _backup_db_wal_safe(src: Path, dst: Path) -> None:
    """WAL-consistent online copy — never shutil.copy a live SQLite DB."""
    source = sqlite3.connect(str(src))
    try:
        dest = sqlite3.connect(str(dst))
        try:
            with dest:
                source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def backup_entries(base: Path) -> list[str]:
    """Every top-level entry of ``base`` a backup carries, sorted."""
    return sorted(
        p.name for p in Path(base).iterdir()
        if p.name not in EXCLUDED_ENTRIES and p.name not in _DB_ENTRIES
    )


def _tree_bytes(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        if p.is_symlink():
            continue
        if p.is_file():
            total += p.stat().st_size
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
    return total


def _copy_entry(src: Path, dst: Path) -> None:
    """Copy one ~/.okuro entry into a backup dir, preserving what it is."""
    if src.is_symlink():
        if dst.is_symlink() or dst.exists():
            dst.unlink() if not dst.is_dir() or dst.is_symlink() else shutil.rmtree(dst)
        os.symlink(os.readlink(src), dst)
    elif src.is_dir():
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    elif src.suffix == ".db":
        try:
            _backup_db_wal_safe(src, dst)
        except sqlite3.Error:
            shutil.copy2(src, dst)
    else:
        shutil.copy2(src, dst)


# ── create ──────────────────────────────────────────────────────────────────

def create_backup(label: str = "manual", *, include_keyring: bool = True,
                   base: Optional[Path] = None) -> dict:
    """Snapshot the current okuro state. Returns the manifest (+ ``path``)."""
    base = base or okuro_dir()
    src_db = base / "okuro.db"
    if not src_db.exists():
        raise FileNotFoundError(f"no okuro.db at {src_db} — nothing to back up")

    db_size = src_db.stat().st_size
    bdir = base / "backups"
    bdir.mkdir(parents=True, exist_ok=True)

    entries = [e for e in backup_entries(base) if include_keyring or e != "keyring"]
    total_bytes = db_size + _tree_bytes([base / e for e in entries])

    # Disk safety: prune to (keep-1) FIRST so the new copy reuses freed space,
    # then REFUSE if there still isn't room for the whole backup + 10%
    # headroom. Without this, backing up a multi-GB DB can fill the disk and
    # break the very update the backup was meant to protect.
    keep = _effective_keep(total_bytes)
    _prune(bdir, keep=max(0, keep - 1))
    free = shutil.disk_usage(bdir).free
    needed = int(total_bytes * 1.1)
    if free < needed:
        raise OSError(
            f"insufficient disk for backup: need ~{needed / 1e9:.1f} GB, only "
            f"{free / 1e9:.1f} GB free at {bdir}. Free space or prune "
            f"~/.okuro/backups (or lower OKURO_BACKUP_RETENTION / "
            f"OKURO_BACKUP_MAX_BYTES)."
        )

    safe_label = "".join(c if (c.isalnum() or c in "-_") else "-" for c in label) or "manual"
    stamp = _utc_stamp()
    dest = bdir / f"{stamp}-{safe_label}"
    dest.mkdir(parents=True, exist_ok=True)

    _backup_db_wal_safe(src_db, dest / "okuro.db")

    # Everything else, by denylist: config, tokens, keyring, personal roles,
    # design kits, the guard list, corpora, deliveries, attachments, flows,
    # stacks, … — whatever is there today and whatever appears tomorrow.
    for name in entries:
        _copy_entry(base / name, dest / name)
    excluded = sorted(
        p.name for p in base.iterdir() if p.name in EXCLUDED_ENTRIES
    )

    manifest = {
        "timestamp": stamp,
        "label": safe_label,
        "git_sha": _git_sha(),
        "schema_version": _schema_version(dest / "okuro.db"),
        "db_bytes": (dest / "okuro.db").stat().st_size,
        "bytes_total": total_bytes,
        "keyring": (dest / "keyring").exists(),
        "entries": entries,
        # kept for readers of older manifests: the carried entries other
        # than the keyring
        "extras": [e for e in entries if e != "keyring"],
        "excluded": excluded,
        "counts": _row_counts(dest / "okuro.db"),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    manifest["path"] = str(dest)

    _prune(bdir, keep=keep)
    return manifest


def _prune(bdir: Path, keep: Optional[int] = None) -> None:
    """Keep the newest ``keep`` backup dirs (timestamp-prefixed names sort).

    Reads the module-level RETENTION at call time (not as a default arg) so
    the limit stays overridable.
    """
    if keep is None:
        keep = RETENTION
    try:
        dirs = sorted(p for p in bdir.glob("*-*") if (p / "manifest.json").exists())
        for old in dirs[:-keep]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


# ── list ──────────────────────────────────────────────────────────────────

def list_backups(base: Optional[Path] = None) -> list[dict]:
    """Newest-first. Includes both full backups (dirs) and legacy
    migrate snapshots (okuro-premig-*.db files) so either can be restored."""
    base = base or okuro_dir()
    bdir = base / "backups"
    out: list[dict] = []
    if not bdir.exists():
        return out
    for p in sorted(bdir.glob("*-*"), reverse=True):
        mf = p / "manifest.json"
        if mf.exists():
            try:
                m = json.loads(mf.read_text())
            except Exception:
                m = {}
            m["id"] = p.name
            m["kind"] = "full"
            m["path"] = str(p)
            out.append(m)
    for f in sorted(bdir.glob("okuro-premig-*.db"), reverse=True):
        out.append({
            "id": f.name, "kind": "premig", "path": str(f),
            "db_bytes": f.stat().st_size,
            "schema_version": _schema_version(f),
        })
    return out


def _resolve_backup(backup_id: str, base: Path) -> Path:
    """Return the okuro.db file to restore from, for a full-dir id, a premig
    file name, the literal 'latest', or an absolute path."""
    bdir = base / "backups"
    if backup_id == "latest":
        items = list_backups(base)
        if not items:
            raise FileNotFoundError("no backups found")
        backup_id = items[0]["id"]
    cand_dir = bdir / backup_id
    if (cand_dir / "okuro.db").exists():
        return cand_dir / "okuro.db"
    cand_file = bdir / backup_id
    if cand_file.exists() and cand_file.suffix == ".db":
        return cand_file
    p = Path(backup_id)
    if p.exists():
        return (p / "okuro.db") if p.is_dir() else p
    raise FileNotFoundError(f"backup not found: {backup_id}")


# ── restore ─────────────────────────────────────────────────────────────────

def restore_backup(backup_id: str, *, manage_services: bool = True,
                   make_safety: bool = True, base: Optional[Path] = None) -> dict:
    """Stop writers → safety-snapshot current → swap DB (+ keyring) → restart."""
    base = base or okuro_dir()
    src_db = _resolve_backup(backup_id, base)
    src_dir = src_db.parent

    safety = None
    if make_safety and (base / "okuro.db").exists():
        safety = create_backup("pre-restore", base=base)

    stopped: list[str] = []
    if manage_services:
        stopped = stop_services()

    live = base / "okuro.db"
    # Drop stale WAL/SHM so the swapped-in DB is read cleanly.
    for sidecar in (live.with_name("okuro.db-wal"), live.with_name("okuro.db-shm")):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass
    shutil.copy2(src_db, live)

    # Everything the backup carries comes back — driven by the backup dir
    # itself, not by a second list that could drift from create_backup's.
    # Additive: entries that exist live but not in the backup are left alone.
    restored: list[str] = []
    if src_dir.is_dir():
        for entry in sorted(src_dir.iterdir()):
            if entry.name in ("okuro.db", "manifest.json"):
                continue
            _copy_entry(entry, base / entry.name)
            restored.append(entry.name)

    if manage_services:
        start_services()

    return {
        "restored_from": str(src_db),
        "restored_entries": restored,
        "safety_backup": safety["path"] if safety else None,
        "services_cycled": stopped,
    }


# ── export / import (single-file cross-machine transfer) ────────────────────

def export_backup(dest: Path, *, backup_id: Optional[str] = None,
                  label: str = "export", base: Optional[Path] = None) -> dict:
    """Pack a backup into one .tgz for scp to another machine.

    With no ``backup_id`` a fresh snapshot of the current state is taken first.
    The archive holds a single top-level dir (the backup id) so import is
    unambiguous.
    """
    base = base or okuro_dir()
    if backup_id:
        src_dir = base / "backups" / backup_id
        if not (src_dir / "okuro.db").exists():
            raise FileNotFoundError(f"backup not found: {backup_id}")
        bid = backup_id
        mf = src_dir / "manifest.json"
        manifest = json.loads(mf.read_text()) if mf.exists() else {}
    else:
        manifest = create_backup(label, base=base)
        src_dir = Path(manifest["path"])
        bid = src_dir.name

    dest = Path(dest)
    if not (dest.name.endswith(".tgz") or dest.name.endswith(".tar.gz")):
        dest = dest.with_name(dest.name + ".tgz")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(src_dir, arcname=bid)
    return {
        "path": str(dest), "id": bid,
        "keyring": manifest.get("keyring"),
        "bytes": dest.stat().st_size,
    }


def import_backup(tgz: Path, *, base: Optional[Path] = None) -> dict:
    """Unpack an exported .tgz into ~/.okuro/backups/. Returns the backup id."""
    base = base or okuro_dir()
    tgz = Path(tgz)
    if not tgz.exists():
        raise FileNotFoundError(f"archive not found: {tgz}")
    bdir = base / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz, "r:gz") as tar:
        names = tar.getnames()
        if not names:
            raise ValueError("empty archive")
        bid = Path(names[0]).parts[0]
        # filter='data' blocks path-traversal / absolute members (py3.12+).
        tar.extractall(bdir, filter="data")

    imported = bdir / bid
    if not (imported / "okuro.db").exists():
        raise ValueError(f"archive did not contain a valid backup ({bid})")
    keyring_present = (imported / "keyring").exists()
    return {"id": bid, "path": str(imported), "keyring": keyring_present}


# ── service control (best-effort, OS-abstracted) ────────────────────────────

def _manager():
    from okuro.system.service_manager import get_service_manager
    return get_service_manager()


def stop_services() -> list[str]:
    mgr = _manager()
    done = []
    for name in _STOP_ORDER:
        try:
            mgr.stop(name)
            done.append(name)
        except Exception:
            pass
    return done


def start_services() -> list[str]:
    mgr = _manager()
    done = []
    for name in _START_ORDER:
        try:
            mgr.start(name)
            done.append(name)
        except Exception:
            pass
    return done
