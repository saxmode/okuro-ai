### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Content-addressed blob store — base64 image/document bodies moved out of agent_events.content_json.
# index: imports | blob_root | min_bytes | blob_path_for | put_bytes | get_bytes | make_ref | is_ref | externalize_content | externalize_row | resolve_content | verify_ref | BlobError
# AGENT_HEADER_END -->
"""The bytes that do not belong in a row.

Measured 2026-08-13 over ALL 29,610 ``agent_events`` rows carrying
``content_json`` on a user turn — the corpus picked the sample, not us:
530.7 MB of 673 MB (79.7%) is base64 image/document block bodies, against
96.5 MB of actual human text. User rows are never compacted, so that mass is
permanent. It drives VACUUM cost and it is copied ten times over into the
retained pre-migration snapshots.

A base64 body is the ideal thing to move out of a database and the worst thing
to keep in one: it is large, it is opaque to every query, no index reaches
inside it, and it is *immutable*. Content-addressing follows directly from that
last property — a payload that never changes can be named by its own hash, and
then storing it twice is storing it once.

WHAT REPLACES THE BLOCK
-----------------------
In ``content_json`` the block::

    {"type": "image",
     "source": {"type": "base64", "media_type": "image/png", "data": "<400 KB>"}}

becomes::

    {"type": "blob_ref", "sha256": "<hex>", "mime": "image/png",
     "bytes": 300000, "original_type": "image",
     "original": {"type": "image", "cache_control": {"type": "ephemeral"},
                  "source": {"type": "base64", "media_type": "image/png"}}}

``bytes`` is the size of the DECODED payload — the size of the file on disk,
so a reader can decide whether it wants to page it in, and so
:func:`verify_ref` has something to check the file against. The base64 string
it replaced was ~4/3 of that; the backfill measures reclaimed DB bytes by
differencing the row instead of trusting arithmetic on this field.

``original`` is the whole source block with ``source.data`` removed, so that
sibling fields — ``cache_control``, a document's ``title`` / ``context`` /
``citations``, anything added later — survive the round trip. Carrying the
block wholesale rather than enumerating known-good fields is deliberate: an
enumeration is a schema this module would have to keep in step with
Anthropic's, and the failure mode of falling behind is silent, irreversible
loss of somebody's data at the moment the ref is written.

DECODED, NOT THE BASE64 TEXT
----------------------------
The store holds the decoded bytes. Two reasons, and the first is the load-
bearing one: dedup has to work on *content*. The same screenshot pasted into
two sessions can arrive as two different base64 strings — padding, line breaks,
a different encoder — and hashing the text would file them as two blobs. It
also means the file on disk is a real ``.png`` that any tool can open, rather
than something only okuro can read.

The cost is that resolution reconstructs an *equivalent* block, not a
byte-identical one. That is acceptable because the ref is now the stored form:
nothing downstream compares content_json against a pre-externalization copy,
and the cold archive (``sense/distill/archive.py``) serializes content_json AS
STORED — see RETENTION below.

WHY A MISSING BLOB IS NOT AN EXCEPTION
--------------------------------------
:func:`resolve_content` returns the ref block unchanged and names the missing
hash, rather than raising. A trace reader exists to show what an agent did; a
session whose screenshot is gone is degraded, not unreadable, and a reader that
crashes on one absent file makes the whole session unreadable to punish a
missing image. Callers that need to care get the ``missing`` list and can say
so; callers that do not, keep working.

RETENTION — WHAT AN ARCHIVE CONTAINS AFTER THIS
-----------------------------------------------
``distill/archive.py`` writes ``content_json`` verbatim into the cold zstd
JSONL. After externalization an archived session therefore contains BLOB REFS,
NOT IMAGES. The archive stops being self-contained: restoring a session's
images needs the archive *and* the blob store. That is a deliberate trade — the
archive exists so the row bodies survive deletion, and a ref is a row body —
but it means the blob store now carries archive-grade durability requirements
and must be included in whatever copies the archive. Blobs get NO lifecycle in
this build: nothing here deletes, prunes, or garbage-collects a blob, and an
orphan costs only disk. That is a separate decision and is deliberately not
bundled with this one.

ROOT RESOLUTION
---------------
:func:`blob_root` resolves, in order: ``OKURO_BLOB_DIR`` (tests), then
``blobs.dir`` in ``~/.okuro/config.yaml``, then ``$OKURO_HOME/blobs``. Same
shape as ``distill/archive.py::archive_root`` and for the same reason — which
volume this lives on is an operator decision, and a host path baked into source
is wrong on every machine but one. Unlike the archive there is no
refuse-to-write-to-the-fallback guard: the fallback must work on a fresh
install, because ingest calls this on the very first transcript and a new user
has configured nothing.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

# Tests point this at a tmp_path. Never set in production.
_BLOB_ROOT_ENV = "OKURO_BLOB_DIR"

_CONFIG_SECTION = "blobs"
_CONFIG_DIR_KEY = "dir"
_CONFIG_MIN_BYTES_KEY = "min_bytes"

_FALLBACK_DIRNAME = "blobs"

# 32 KiB of base64. Below this the ref block (~150 bytes) plus an inode plus a
# directory entry is not obviously cheaper than the payload, and the row stops
# being self-contained for no measured gain. Configurable because the right
# threshold depends on the filesystem, not on okuro.
DEFAULT_MIN_BYTES = 32768

REF_TYPE = "blob_ref"

# The block types whose base64 bodies this store owns. tool_use arguments and
# tool_result bodies are NOT in scope: they are text, they are what
# `trace/claude_code.py::_flatten_content` puts in the FTS-indexed `text`
# column, and the measurement found no qualifying blocks there (341 LIKE hits,
# 0 real blocks in user rows).
EXTERNALIZABLE_TYPES = frozenset({"image", "document"})


class BlobError(RuntimeError):
    """Raised when a blob cannot be written or read back."""


def blob_root() -> Path:
    """Resolve the blob store root: env override, then config, then $OKURO_HOME.

    Resolved on every call rather than cached at import, matching
    ``db.engine.okuro_home()`` and ``distill.archive.archive_root()``: a path
    captured at import time is the staleness bug those modules already record.
    """
    override = os.environ.get(_BLOB_ROOT_ENV)
    if override:
        return Path(override)

    from okuro.db.engine import _load_config, okuro_home

    configured = (_load_config().get(_CONFIG_SECTION) or {}).get(_CONFIG_DIR_KEY)
    if configured:
        return Path(str(configured)).expanduser()
    return okuro_home() / _FALLBACK_DIRNAME


def min_bytes() -> int:
    """Smallest base64 payload worth externalizing, in characters.

    Read fresh per call so an operator can retune without a restart, and so a
    test can move it without patching module state.
    """
    configured = (_load_blobs_config()).get(_CONFIG_MIN_BYTES_KEY)
    try:
        value = int(configured)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MIN_BYTES
    return value if value > 0 else DEFAULT_MIN_BYTES


def _load_blobs_config() -> dict:
    from okuro.db.engine import _load_config

    section = _load_config().get(_CONFIG_SECTION)
    return section if isinstance(section, dict) else {}


def blob_path_for(sha256: str) -> Path:
    """Deterministic path for a hash: ``<root>/<first-2>/<full-hash>``.

    Sharded on the first two hex characters for the same reason the archive
    shards on session id — a flat directory of hundreds of thousands of files
    is slow to list and unpleasant to rsync. Two characters gives 256 shards,
    which keeps a ~50k-blob store at ~200 entries per directory.
    """
    if not _is_sha256(sha256):
        raise BlobError(f"not a sha256 hex digest: {sha256!r}")
    return blob_root() / sha256[:2] / sha256


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def put_bytes(payload: bytes) -> tuple[str, Path, bool]:
    """Store ``payload`` under its own hash. Returns ``(sha256, path, existed)``.

    Idempotent by construction: the same bytes produce the same path, so a
    second write is detected as an existing file and skipped. That is also what
    makes dedup free — two sessions carrying the same screenshot converge on
    one file with no index, no lookup table, and no coordination.

    Written to a sibling temp file, fsynced, then renamed into place, so a
    crash leaves either the complete blob or nothing. A half-written blob would
    be the one failure mode a content-addressed store cannot detect by name.
    """
    digest = hashlib.sha256(payload).hexdigest()
    path = blob_path_for(digest)
    if path.exists():
        return digest, path, True

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BlobError(f"could not write blob {digest}: {exc}") from exc
    return digest, path, False


def get_bytes(sha256: str) -> bytes | None:
    """Read a blob back, or ``None`` when it is not in the store.

    ``None`` rather than an exception: the caller that matters here is a trace
    reader rendering a session, and see the module docstring on why a missing
    image must not make the session unreadable.
    """
    try:
        path = blob_path_for(sha256)
    except BlobError:
        return None
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise BlobError(f"blob unreadable: {sha256}: {exc}") from exc


def make_ref(
    sha256: str,
    mime: str | None,
    size: int,
    original_type: str,
    original: dict | None = None,
) -> dict:
    """Build the block that stands in for an externalized payload.

    ``original`` is the ENTIRE source block with only ``source.data`` removed —
    not an enumerated list of fields worth keeping. That distinction is the
    whole point. ``cache_control`` on an image, ``title`` / ``context`` /
    ``citations`` on a document are all legal today, and rebuilding a block as
    ``{type, source}`` drops them silently and irreversibly, because the
    original string is gone the moment the ref is written. Carrying the block
    verbatim means a field nobody has invented yet survives too, with no schema
    for this module to track.

    ``mime`` / ``bytes`` / ``original_type`` stay at the top level despite
    being derivable from ``original``: they are the READ surface. A reader
    deciding whether to page in a payload, and :func:`verify_ref` checking a
    file against its record, must not have to descend into a provider's block
    schema to do it.
    """
    ref = {
        "type": REF_TYPE,
        "sha256": sha256,
        "mime": mime,
        "bytes": size,
        "original_type": original_type,
    }
    if original is not None:
        ref["original"] = original
    return ref


def _strip_payload(block: dict) -> dict:
    """The block as stored on the ref: everything except the base64 itself."""
    source = block.get("source")
    template = {k: v for k, v in block.items() if k != "source"}
    if isinstance(source, dict):
        template["source"] = {k: v for k, v in source.items() if k != "data"}
    return template


def is_ref(block: Any) -> bool:
    """True for a well-formed blob_ref block.

    Checks the hash too, not just the type tag: a block claiming to be a ref
    without a usable digest is corruption, and treating it as a ref would make
    the corruption invisible to :func:`externalize_content` — which would then
    leave it alone forever.
    """
    return (
        isinstance(block, dict)
        and block.get("type") == REF_TYPE
        and _is_sha256(block.get("sha256"))
    )


def _base64_body(block: Any) -> tuple[str, str | None] | None:
    """Return ``(data, media_type)`` when a block carries a base64 body.

    Shape per the Anthropic content-block schema::

        {"type": "image"|"document",
         "source": {"type": "base64", "media_type": ..., "data": ...}}

    A ``source.type`` of ``url`` or ``file`` carries no bytes and is left
    alone — there is nothing to move.
    """
    if not isinstance(block, dict) or block.get("type") not in EXTERNALIZABLE_TYPES:
        return None
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    data = source.get("data")
    if not isinstance(data, str) or not data:
        return None
    media_type = source.get("media_type")
    return data, media_type if isinstance(media_type, str) else None


def externalize_content(
    obj: Any, *, threshold: int | None = None, store: bool = True
) -> tuple[Any, list[dict]]:
    """Replace qualifying base64 blocks anywhere in ``obj`` with refs.

    Walks the whole structure rather than the top-level content list: a
    tool_result carries its own nested content list, and an image pasted into
    one is the same mass in the same column. Returns the rewritten object and
    one record per externalized block, each carrying the ``data`` string that
    was removed so a caller can scrub the flat ``text`` column of the same
    bytes.

    Non-destructive on the way in: the input object is not mutated, because
    three of the four ingesters hand us the same dict they also read session
    metadata from.

    A block whose base64 does not decode is LEFT ALONE. The store cannot name
    what it cannot hash, and a payload we do not understand is exactly the one
    to keep verbatim rather than rewrite.

    ``store=False`` computes every hash and size but writes no file — what the
    backfill's dry run needs. The rewritten object it returns therefore points
    at blobs that do not exist and must be discarded, never persisted. Kept as
    a flag on this function rather than as a parallel "scan" implementation
    because a measurement that does not walk the identical code path is
    measuring something else.
    """
    limit = threshold if threshold is not None else min_bytes()
    removed: list[dict] = []

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if is_ref(node):
                # Explicit, not incidental. A ref happens to lack the `source`
                # shape that _base64_body looks for, so leaving this out still
                # behaves — until a ref gains a field that looks like a source.
                # Re-ingest runs this path over already-externalized rows on
                # every resumed transcript, so the no-op is a property worth
                # stating rather than inheriting.
                return node
            body = _base64_body(node)
            if body is not None:
                data, media_type = body
                if len(data) >= limit:
                    try:
                        payload = base64.b64decode(data, validate=True)
                    except (binascii.Error, ValueError):
                        payload = None
                    if payload:
                        if store:
                            digest, _path, existed = put_bytes(payload)
                        else:
                            digest = hashlib.sha256(payload).hexdigest()
                            existed = blob_path_for(digest).exists()
                        removed.append(
                            {
                                "sha256": digest,
                                "mime": media_type,
                                "bytes": len(payload),
                                "b64_len": len(data),
                                "original_type": node.get("type"),
                                "data": data,
                                "deduped": existed,
                            }
                        )
                        return make_ref(
                            digest,
                            media_type,
                            len(payload),
                            str(node.get("type")),
                            _strip_payload(node),
                        )
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(obj), removed


def resolve_content(obj: Any) -> tuple[Any, list[str]]:
    """Inflate every blob_ref in ``obj`` back into a base64 block.

    Returns ``(resolved, missing)``. A ref whose blob is absent is returned
    UNCHANGED and its hash is named in ``missing`` — the degraded read. See the
    module docstring for why that is not an exception. Note that a degraded ref
    still carries its ``original`` block, so the siblings survive a missing
    blob: they were never in the blob.

    DOES NOT VERIFY. The bytes are returned as the store holds them, without
    re-hashing — a read path that hashed every payload would make rendering a
    session O(bytes) in the images it contains. Integrity is a separate,
    deliberate pass: :func:`verify_ref`, reachable via
    ``scripts/blob_backfill.py --verify``. So a tampered blob resolves quietly
    here and is caught there.
    """
    missing: list[str] = []

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if is_ref(node):
                payload = get_bytes(node["sha256"])
                if payload is None:
                    missing.append(node["sha256"])
                    return node
                data = base64.b64encode(payload).decode("ascii")
                template = node.get("original")
                if isinstance(template, dict):
                    restored = copy.deepcopy(template)
                    source = restored.get("source")
                    if not isinstance(source, dict):
                        source = {"type": "base64", "media_type": node.get("mime")}
                        restored["source"] = source
                    source["data"] = data
                    return restored
                # Ref written before the sibling carry existed. A backfill
                # interrupted across versions leaves both shapes in the table,
                # so this is a live path rather than dead compatibility code.
                return {
                    "type": node.get("original_type") or "image",
                    "source": {
                        "type": "base64",
                        "media_type": node.get("mime"),
                        "data": data,
                    },
                }
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(obj), missing


def verify_ref(ref: dict) -> tuple[bool, str]:
    """Check one ref against the store. Returns ``(ok, reason)``.

    Re-hashes the file rather than trusting its name. A content-addressed store
    makes exactly one failure undetectable by naming — a file whose bytes no
    longer match its path — and that is the one this checks. Size is verified
    too, so a truncated blob is reported as truncation rather than as a generic
    hash mismatch.
    """
    if not is_ref(ref):
        return False, "not a blob_ref"
    digest = ref["sha256"]
    payload = get_bytes(digest)
    if payload is None:
        return False, "missing"
    expected = ref.get("bytes")
    if isinstance(expected, int) and len(payload) != expected:
        return False, f"size {len(payload)} != recorded {expected}"
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        return False, f"hash {actual} != name {digest}"
    return True, "ok"


def iter_refs(obj: Any):
    """Yield every blob_ref block in a decoded content structure."""
    if isinstance(obj, dict):
        if is_ref(obj):
            yield obj
            return
        for value in obj.values():
            yield from iter_refs(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_refs(value)


def scrub_text(text: str | None, removed: list[dict]) -> str | None:
    """Remove externalized base64 from the flat ``text`` column.

    ``agent_events.text`` is FTS-indexed and is built by each ingester's
    content flattener. The claude-code flattener ignores image blocks, so the
    common case leaves nothing here to do — but codex serializes a whole
    ``function_call_output`` and gemini takes any ``text`` key it finds, so the
    same payload CAN reach this column by a different route. Handled by
    substring rather than by re-deriving the flattener's output: this is the
    one operation that is correct for all four providers and for whatever the
    fifth does.

    Returns ``text`` unchanged (same object) when nothing matched, so a caller
    can tell "no leak" from "scrubbed" by identity.
    """
    if not text or not removed:
        return text
    out = text
    for record in removed:
        data = record.get("data")
        if data and data in out:
            out = out.replace(data, f"[{REF_TYPE} {record['sha256']}]")
    return out


def externalize_row(
    row: dict, *, threshold: int | None = None, store: bool = True
) -> dict | None:
    """Externalize one agent_events row dict in place. Returns stats or None.

    ``None`` means the row was left untouched — the overwhelmingly common case,
    and the one that has to stay cheap. The length pre-check is what keeps it
    cheap: a row whose entire ``content_json`` is smaller than the threshold
    cannot contain a qualifying payload, so it is dismissed without a JSON
    parse. Rows that DO parse are rows we are about to rewrite anyway.

    Mutates ``row`` because every caller is an ingester assembling a row dict
    it is about to hand to ``executemany`` — copying would allocate a second
    dict per event for no reader's benefit.
    """
    limit = threshold if threshold is not None else min_bytes()
    blob = row.get("content_json")
    if not isinstance(blob, str) or len(blob) < limit:
        return None
    try:
        decoded = json.loads(blob)
    except (TypeError, ValueError):
        return None

    rewritten, removed = externalize_content(decoded, threshold=limit, store=store)
    if not removed:
        return None

    new_blob = json.dumps(rewritten, ensure_ascii=False)
    # Read once. A provider that never sets `text` (or sets it to None) is
    # legal — antigravity already writes rows with columns the others fill —
    # and re-reading with row["text"] after row.get("text") would raise on
    # exactly that row.
    original_text = row.get("text")
    scrubbed = scrub_text(original_text, removed)
    text_scrubbed = scrubbed is not original_text
    # Measured against the actual column, not inferred from base64 arithmetic:
    # ensure_ascii and the ref block itself both move the number.
    reclaimed = (
        len(blob) - len(new_blob) + (len(original_text or "") - len(scrubbed or ""))
    )
    row["content_json"] = new_blob
    row["text"] = scrubbed

    return {
        "blocks": len(removed),
        "blob_bytes": sum(r["bytes"] for r in removed),
        "b64_bytes": sum(r["b64_len"] for r in removed),
        "reclaimed": reclaimed,
        "deduped": sum(1 for r in removed if r["deduped"]),
        "text_scrubbed": text_scrubbed,
        "sha256": [r["sha256"] for r in removed],
    }
