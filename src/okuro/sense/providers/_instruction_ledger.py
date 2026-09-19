# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The timeline of okuro-written instruction content — what was live when.
# index:
#   PROVIDER_TO_ADAPTER
#   ADAPTER_TO_PROVIDER
#   BUNDLE_LEN
#   def note_version
#   def seed_from_manifest
#   def versions_at
#   def bundle_at
#   def bundle_of
#   def timeline
# AGENT_HEADER_END -->
"""When each okuro-managed instruction file's content changed.

WHY THIS EXISTS. A session's adoption rate is only a measurement if you know
which instructions that session received. okuro rewrites every provider
instruction file on the daemon's ``*/5`` refresh and on every profile save,
and until this module nothing recorded which text was live at any past
moment. A rate computed across an instruction change averages the before and
the after and reports the average with no visible loss of confidence.

WHAT IT IS NOT. ``_deployment.py``'s manifest is a SNAPSHOT — one entry per
path, overwritten in place, answering "what is live now". This is the
TIMELINE: append-only, one row per content change, so "what was live at
15:04" has an answer. The two are the same instrument's two halves and the
manifest is this one's seed (see :func:`seed_from_manifest`).

HOW ATTRIBUTION WORKS. A row is in force from its ``first_seen`` until the
next row for the same path. A session's start timestamp therefore falls in
exactly one interval per path, and the set of paths for a provider collapses
to one short ``bundle`` digest — one string naming the whole instruction
surface a session was given.

WHAT IS HONESTLY NULL. A timestamp before the ledger's earliest row for a
path returns nothing. Attributing such a session to the oldest version on
hand would read exactly like a measurement and be a fabrication; the caller
gets ``None`` and can count those sessions as unattributable, which is the
true state of them.

NEVER RAISES ON THE WRITE PATH. :func:`note_version` runs inside the daemon's
instruction refresh. A ledger failure there must cost a missing row, never a
failed deploy — the same doctrine ``_deployment.record`` already follows.
"""

from __future__ import annotations

import logging
from hashlib import sha256 as _sha256
from pathlib import Path

from okuro.clock import utc_iso

log = logging.getLogger(__name__)

# The trace store names providers after the CLI that produced the transcript;
# the adapters name themselves after the vendor. The two vocabularies were
# never reconciled and this is the one place that knows both.
#
# ``gemini`` is absent deliberately: the gemini-cli provider is retired and
# okuro writes no managed instruction file for it, so a gemini session has no
# version to be attributed to and must resolve to None rather than borrow
# antigravity's (both read ~/.gemini, and that coincidence is exactly the trap).
PROVIDER_TO_ADAPTER: dict[str, str] = {
    "claude-code": "claude",
    "codex": "codex",
    "antigravity": "antigravity",
    "cursor": "cursor",
}

ADAPTER_TO_PROVIDER: dict[str, str] = {
    adapter: provider for provider, adapter in PROVIDER_TO_ADAPTER.items()
}

# Length of the bundle digest. Short enough to read in a table row, long
# enough that a collision across a few thousand versions is not a concern.
BUNDLE_LEN = 12


def _db():
    from okuro.db import get_db

    return get_db()


def note_version(
    path: str | Path,
    provider: str,
    digest: str,
    size: int | None = None,
    *,
    at: str | None = None,
    source: str = "write",
) -> bool:
    """Append ``digest`` as ``path``'s content from now on. No-op if unchanged.

    ``provider`` is the ADAPTER name (``claude``/``codex``/…), matching what
    ``_deployment.record`` stores. Returns True when a row was appended.

    The no-op case is the common one: the caller only reaches here when the
    manifest saw a change, but a fresh manifest and a populated ledger can
    disagree (delete the manifest and every path looks new again), and a
    duplicate row would fabricate a version change that never happened.
    """
    target = str(Path(path).expanduser())
    stamp = at or utc_iso()
    try:
        db = _db()
        newest = db.fetchone(
            """
            SELECT sha256, first_seen FROM instruction_versions
            WHERE path = ? ORDER BY first_seen DESC, id DESC LIMIT 1
            """,
            (target,),
        )
        if newest and newest.get("sha256") == digest:
            return False
        if newest and str(newest.get("first_seen") or "")[:19] > stamp[:19]:
            # Time went backwards — a clock change, or a seed arriving after a
            # live write. Appending here would put the older content in force
            # over the newer one for every session in between.
            log.warning(
                "instruction ledger: refusing out-of-order row for %s "
                "(newest %s, offered %s)",
                target, newest.get("first_seen"), stamp,
            )
            return False
        # SECOND RESOLUTION IS THE LEDGER'S RESOLUTION, and two changes inside
        # one second are therefore one interval. `okuro init` and a profile
        # save can both write within the same second, and utc_iso does not
        # separate them. The LATER content wins that second, because it is the
        # one a session starting after it actually read — an INSERT OR IGNORE
        # here would silently keep the earlier one and mislabel every session
        # in the interval that follows.
        db.execute(
            """
            INSERT INTO instruction_versions
                (path, provider, sha256, bytes, first_seen, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path, first_seen) DO UPDATE SET
                sha256   = excluded.sha256,
                bytes    = excluded.bytes,
                provider = excluded.provider,
                source   = excluded.source
            """,
            (target, provider, digest, size, stamp, source),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never break an instruction deploy
        log.warning("instruction ledger: cannot record %s: %s", target, exc)
        return False


def note_block(path: str | Path, fragment: str, provider: str, content: str) -> bool:
    """Record a version of content okuro owns only PART of ``path``.

    THE MANIFEST CANNOT HOLD THESE AND SHOULD NOT. ``_deployment`` digests
    whole files on purpose: a whole-file digest of a file the user co-owns
    reports "modified" the moment they edit their own half, which is the false
    alarm that record exists to avoid. So ``~/.codex/config.toml``'s
    ``developer_instructions`` and ``~/.gemini/GEMINI.md``'s managed block —
    the SYSTEM-TIER channel for each of those providers, the highest-weight
    instructions either one receives — are absent from it by design.

    The ledger has no such constraint: a row is (path, digest) and nothing
    requires the digest to cover every byte. Recorded under ``path#fragment``
    so it cannot collide with the whole-file row and a reader can see at a
    glance which channel it is.

    Without this the codex bundle would be unchanged by a rewrite of codex's
    system-tier instructions, and pre- and post-change sessions would land in
    one cohort — the exact mixing the ledger exists to end, reintroduced
    where nobody would look for it.
    """
    return note_version(
        f"{Path(path).expanduser()}#{fragment}",
        provider,
        _sha256(content.encode()).hexdigest(),
        len(content.encode()),
    )


def seed_from_manifest() -> int:
    """Recover the one interval the manifest knows, for paths not yet in the ledger.

    ``_deployment.record`` rewrites ``written_at`` only when the digest
    changes — it early-returns on an unchanged hash so the ``*/5`` refresh
    does not restamp it 288 times a day. That makes ``written_at`` exactly
    "when this content became live", and it is the only pre-ledger history
    that exists anywhere. Rows land with ``source='seed'`` so a reader can
    tell a recovered boundary from an observed one.

    Returns the number of rows appended. Safe to call repeatedly.
    """
    from ._deployment import load

    seeded = 0
    for target, rec in load().items():
        if not isinstance(rec, dict):
            continue
        digest = rec.get("sha256")
        written = rec.get("written_at")
        if not digest or not written:
            continue
        if note_version(
            target,
            str(rec.get("provider") or "unknown"),
            str(digest),
            rec.get("bytes"),
            at=str(written),
            source="seed",
        ):
            seeded += 1
    return seeded


def versions_at(adapter: str, when: str) -> dict[str, str]:
    """``{path: sha256}`` — the content live for ``adapter`` at ``when``.

    BOTH SIDES ARE TRUNCATED TO WHOLE SECONDS BEFORE COMPARING, and that is
    not tidiness. The ledger writes ``utc_iso`` (``…T15:00:59Z``) while
    claude-code and codex stamp events to the millisecond
    (``…T15:00:59.721Z``). Compared raw, ``.`` (0x2E) sorts before ``Z``
    (0x5A), so a session that started 721 ms AFTER a deploy reads as having
    started before it — a silent off-by-one-version at every boundary, on the
    two providers that produce almost all the traffic. Truncating to
    ``YYYY-MM-DDTHH:MM:SS`` makes both sides the same shape, and lexical order
    is then chronological order with no parsing in the hot path.

    Empty dict when the ledger has nothing at or before ``when`` — that is
    the unattributable case and the caller must not read it as "no
    instructions were deployed".
    """
    if not adapter or not when:
        return {}
    at = str(when)[:19]
    try:
        rows = _db().fetchall(
            """
            SELECT v.path AS path, v.sha256 AS sha256
            FROM instruction_versions v
            JOIN (
                SELECT path, MAX(substr(first_seen, 1, 19)) AS live
                FROM instruction_versions
                WHERE provider = ? AND substr(first_seen, 1, 19) <= ?
                GROUP BY path
            ) live ON live.path = v.path
                  AND live.live = substr(v.first_seen, 1, 19)
            WHERE v.provider = ?
            """,
            (adapter, at, adapter),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("instruction ledger: lookup failed: %s", exc)
        return {}
    # A tombstone (NULL sha) ends a path's interval. Without dropping it here
    # the last real row would stay in force forever and fold a file nobody
    # receives into every later bundle.
    return {r["path"]: r["sha256"] for r in rows if r["sha256"]}


def bundle_of(versions: dict[str, str]) -> str | None:
    """One short id for a whole instruction surface, or None if it is empty.

    A provider deploys more than one managed file — claude writes both
    ~/.claude/CLAUDE.md and the system-tier output style — and "which version
    did this session get" has to name all of them or it names the wrong thing
    the first time only one of them changes. Sorted so the id does not depend
    on row order.
    """
    if not versions:
        return None
    payload = "\n".join(f"{p}:{versions[p]}" for p in sorted(versions))
    return _sha256(payload.encode()).hexdigest()[:BUNDLE_LEN]


def bundle_at(adapter: str, when: str) -> str | None:
    """The bundle id live for ``adapter`` at ``when``. None = unattributable."""
    return bundle_of(versions_at(adapter, when))


def timeline(adapter: str | None = None, limit: int = 50) -> list[dict]:
    """Recorded content changes, newest first — the ledger as a reader sees it."""
    clause = "WHERE provider = ?" if adapter else ""
    params: tuple = (adapter, limit) if adapter else (limit,)
    try:
        return _db().fetchall(
            f"""
            SELECT path, provider, sha256, bytes, first_seen, source
            FROM instruction_versions {clause}
            ORDER BY first_seen DESC, id DESC LIMIT ?
            """,
            params,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("instruction ledger: timeline failed: %s", exc)
        return []


__all__ = [
    "ADAPTER_TO_PROVIDER",
    "BUNDLE_LEN",
    "PROVIDER_TO_ADAPTER",
    "bundle_at",
    "bundle_of",
    "note_version",
    "seed_from_manifest",
    "timeline",
    "versions_at",
]
