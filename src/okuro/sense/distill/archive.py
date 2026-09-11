### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cold archive for distilled trace sessions — zstd JSONL, append-only, read back before anything is deleted.
# index: imports | ARCHIVE_ROOT | ArchiveResult | archive_root | archive_path_for | serialize_events | write_archive | read_archive | ArchiveError
# AGENT_HEADER_END -->
"""Per-session cold archive for ``agent_events``.

Once the retention gate has run, this file is the only copy of the session's
events that exists anywhere: the DB was measured as a 3.9x superset of what
the provider still keeps on disk, and provider transcripts are themselves
retention-pruned at ~35 days. So the archive is not a backup of a live thing,
it is the thing.

Three properties follow from that, and each is enforced rather than intended:

**Readable, not merely written.** :func:`read_archive` is the same code path
the gate uses to verify an archive before deleting anything. A writer that is
never read is a writer nobody has tested.

**Append-only.** :func:`write_archive` refuses to overwrite an existing archive
whose content differs. Re-archiving the same session with the same events is
idempotent and returns the existing file; re-archiving it with *different*
events is a bug — either the session grew after distillation or the serializer
changed — and it raises rather than quietly replacing the only copy.

**Fails loudly when truncated.** :func:`read_archive` decompresses the whole
buffer in one call, which raises on a short frame. That decoder choice — not
the frame flags — is what makes truncation loud: measured, a streaming reader
on a half-written archive returns 0 bytes and no exception, with or without
content-size and checksum in the frame header. A silent short read is the worst
available outcome here, because the gate would then compare a truncated set
against a truncated set and conclude the archive was complete.

A checksum of our own output is a separate and weaker thing: it proves the
bytes we wrote reached the disk, and says nothing about whether the serializer
emitted every row. That is why the gate compares event-ID sets, not digests.

**Not self-contained, since blob externalization.** ``content_json`` is
serialized AS STORED. Base64 image and document bodies no longer live in that
column — ``db/blobs.py`` moved them to a content-addressed store and left a
``blob_ref`` behind — so an archived session contains POINTERS, NOT IMAGES.
Restoring one whole needs the archive *and* the blob store, which means the
blob store now carries this module's durability requirement: whatever copies
``archive_dir`` must also copy ``blobs.dir``, and the one-liner rsync above
became two. A ref whose blob is gone degrades rather than raising
(``blobs.resolve_content`` names the missing hash), so a lost blob store
costs images and not sessions.

Layout::

    <archive_dir>/<first-2-chars-of-session-id>/<session_id>.jsonl.zst

Sharded on the session id's first two characters because the corpus is ~10 000
sessions and a flat directory of that size is slow to list and unpleasant to
rsync. The shard layout is also why a second copy is a one-liner::

    rsync -a <archive_dir>/ <backup_dir>/

Structuring for that sync is in scope here; running it is not.

``archive_dir`` is resolved by :func:`archive_root`, in order: the
``OKURO_TRACE_ARCHIVE_DIR`` environment variable (tests only), then
``distill.archive_dir`` in ``~/.okuro/config.yaml``, then a directory under
``$OKURO_HOME``. Which volume the archive lives on is an operator decision —
cold storage differs per host, and a path baked into source is wrong on every
machine but one.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import zstandard

# Tests point this at a tmp_path. Never set in production.
_ARCHIVE_ROOT_ENV = "OKURO_TRACE_ARCHIVE_DIR"

# config.yaml key holding the operator's cold-storage path. The whole
# agent_events corpus is ~4.3 GB uncompressed, so capacity is not usually the
# constraint — room for the second copy is.
_CONFIG_SECTION = "distill"
_CONFIG_KEY = "archive_dir"

# Used only when neither the env var nor config names a directory. Under
# $OKURO_HOME rather than a real cold volume, so a misconfigured host writes
# somewhere harmless and obvious instead of somewhere plausible and wrong.
_FALLBACK_DIRNAME = "trace-archive"

# $OKURO_HOME is on the root filesystem. Landing the cold archive there is
# never what anyone intended, and it is a mistake that hides: the writes
# succeed, the gate's round-trip verifies, tombstones record a real path, and
# the only copy of a deleted corpus quietly accumulates on the wrong disk until
# something fills up. So the fallback is readable but NOT writable — writing to
# it requires saying so out loud.
_ALLOW_FALLBACK_ENV = "OKURO_ALLOW_FALLBACK_ARCHIVE_DIR"

# Level 10 rather than the default 3: this is written once and kept forever,
# so compression time is irrelevant and ratio is not.
_ZSTD_LEVEL = 10

# The columns archived, in a fixed order. Explicit rather than SELECT *: a
# future migration adding a column must consciously decide whether it belongs
# in the only surviving copy, instead of silently changing the archive format.
EVENT_COLUMNS: tuple[str, ...] = (
    "uuid",
    "session_id",
    "parent_uuid",
    "ord",
    "type",
    "role",
    "timestamp",
    "model",
    "text",
    "content_json",
    "tool_name",
    "tokens_in",
    "tokens_out",
)

ARCHIVE_FORMAT_VERSION = 1


class ArchiveError(RuntimeError):
    """Raised when an archive cannot be written, read back, or trusted."""


@dataclass(frozen=True)
class ArchiveResult:
    """What :func:`write_archive` produced, and what the tombstone records."""

    path: Path
    sha256: str          # digest of the compressed file AS WRITTEN to disk
    event_count: int
    already_existed: bool


def archive_root() -> Path:
    """Resolve the archive root: env override, then config, then $OKURO_HOME.

    Resolved on every call rather than cached at import, matching
    ``db.engine.okuro_home()``: a cached path captured at import time is the
    staleness bug that module's comments already record.
    """
    override = os.environ.get(_ARCHIVE_ROOT_ENV)
    if override:
        return Path(override)

    from okuro.db.engine import _load_config, okuro_home

    configured = (_load_config().get(_CONFIG_SECTION) or {}).get(_CONFIG_KEY)
    if configured:
        return Path(str(configured)).expanduser()
    return okuro_home() / _FALLBACK_DIRNAME


def archive_root_is_fallback() -> bool:
    """True when no operator has chosen a cold-storage directory."""
    if os.environ.get(_ARCHIVE_ROOT_ENV):
        return False
    from okuro.db.engine import _load_config

    return not (_load_config().get(_CONFIG_SECTION) or {}).get(_CONFIG_KEY)


def _assert_writable_root() -> None:
    """Refuse to write the only surviving copy to the default fallback.

    Reading a fallback archive is fine — if one exists, it is real data. It is
    CREATING one there that has to be deliberate.
    """
    if not archive_root_is_fallback():
        return
    if os.environ.get(_ALLOW_FALLBACK_ENV) == "1":
        return
    raise ArchiveError(
        f"refusing to write the cold archive to {archive_root()} — that is the "
        f"$OKURO_HOME fallback on the root filesystem, not cold storage. Set "
        f"`{_CONFIG_SECTION}.{_CONFIG_KEY}` in ~/.okuro/config.yaml to the "
        f"archive volume, or set {_ALLOW_FALLBACK_ENV}=1 if writing there is "
        f"genuinely what you want."
    )


def archive_path_for(session_id: str) -> Path:
    """Deterministic archive path for a session.

    Deterministic matters twice: the gate has to find the archive it wrote on
    a previous pass, and a tombstone's ``archive_path`` has to still resolve
    years later.
    """
    if not session_id or "/" in session_id or session_id in (".", ".."):
        raise ArchiveError(f"unusable session_id for an archive path: {session_id!r}")
    shard = session_id[:2]
    return archive_root() / shard / f"{session_id}.jsonl.zst"


def serialize_events(rows: Iterable[dict[str, Any]]) -> bytes:
    """Render event rows as canonical JSONL bytes.

    One header line carrying the format version, then one line per event in
    ``ord`` order. Keys are sorted so the same rows always produce the same
    bytes — required for the append-only comparison in :func:`write_archive`
    to mean "different events" rather than "different dict ordering".
    """
    ordered = sorted(rows, key=lambda r: (r.get("ord") if r.get("ord") is not None else -1))
    out: list[str] = [
        json.dumps(
            {"_archive_format": ARCHIVE_FORMAT_VERSION, "_columns": list(EVENT_COLUMNS)},
            sort_keys=True,
        )
    ]
    for row in ordered:
        out.append(json.dumps({c: row.get(c) for c in EVENT_COLUMNS}, sort_keys=True))
    return ("\n".join(out) + "\n").encode("utf-8")


def _compress(payload: bytes) -> bytes:
    # Frame-level integrity metadata: content size lets a decoder know how
    # much should have come out, checksum covers the payload. Defence in
    # depth, NOT the mechanism that catches truncation — measured, a truncated
    # frame raises from the whole-buffer decompress() in read_archive whether
    # or not these are set. They are cheap and they make the frame
    # self-describing for any tool that later reads these archives without
    # going through this module.
    cctx = zstandard.ZstdCompressor(
        level=_ZSTD_LEVEL, write_content_size=True, write_checksum=True
    )
    return cctx.compress(payload)


def read_archive(path: str | Path) -> list[dict[str, Any]]:
    """Decompress an archive and return its event rows.

    Raises :class:`ArchiveError` on a missing, truncated, corrupted, or
    malformed archive. Never returns a partial result: a caller that gets rows
    back has the whole file, which is the property the gate depends on.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError as exc:
        raise ArchiveError(f"archive missing: {p}") from exc
    except OSError as exc:
        raise ArchiveError(f"archive unreadable: {p}: {exc}") from exc

    try:
        payload = zstandard.ZstdDecompressor().decompress(raw)
    except zstandard.ZstdError as exc:
        # Truncated mid-frame, corrupted byte, or not zstd at all.
        raise ArchiveError(f"archive failed to decompress: {p}: {exc}") from exc

    lines = payload.decode("utf-8").splitlines()
    if not lines:
        raise ArchiveError(f"archive is empty: {p}")

    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"archive header is not JSON: {p}: {exc}") from exc
    if header.get("_archive_format") != ARCHIVE_FORMAT_VERSION:
        raise ArchiveError(
            f"archive format {header.get('_archive_format')!r} != "
            f"{ARCHIVE_FORMAT_VERSION} (this okuro cannot read it): {p}"
        )

    rows: list[dict[str, Any]] = []
    for i, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ArchiveError(f"archive line {i} is not JSON: {p}: {exc}") from exc
    return rows


def write_archive(
    session_id: str, rows: Sequence[dict[str, Any]]
) -> ArchiveResult:
    """Write (or confirm) the cold archive for one session.

    Append-only. If an archive already exists:

    * identical content -> returns it, ``already_existed=True``, no write
    * different content -> raises :class:`ArchiveError`

    The write itself goes to a sibling temp file, is fsynced, and is then
    renamed into place, so an interrupted run leaves either the previous
    archive or none — never a half-written one that a later round-trip would
    have to catch.
    """
    if not rows:
        # An empty archive would satisfy a round-trip check against an empty
        # delete set and license deleting nothing while recording a tombstone.
        raise ArchiveError(f"refusing to archive zero events for {session_id}")

    _assert_writable_root()

    payload = serialize_events(rows)
    path = archive_path_for(session_id)

    if path.exists():
        existing = read_archive(path)
        if serialize_events(existing) == payload:
            return ArchiveResult(
                path=path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                event_count=len(rows),
                already_existed=True,
            )
        raise ArchiveError(
            f"archive for {session_id} already exists with DIFFERENT content "
            f"({len(existing)} events on disk vs {len(rows)} offered): {path}. "
            "Archives are append-only; refusing to replace the only copy."
        )

    blob = _compress(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # fsync the directory too, so the rename itself survives a crash.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ArchiveError(f"could not write archive {path}: {exc}") from exc

    return ArchiveResult(
        path=path,
        sha256=hashlib.sha256(blob).hexdigest(),
        event_count=len(rows),
        already_existed=False,
    )
