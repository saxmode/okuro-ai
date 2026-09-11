### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bridge okuro telemetry session ids to provider-native trace session ids — exact methods only.
# index: imports | _SESSION_ID_RE | _iter_declared | harvest_session_links | _iter_embedded | _iter_parented | bridge_sessions | resolve_telemetry | resolve_native
# AGENT_HEADER_END -->
"""Link okuro's telemetry session ids to the provider's native trace ids.

okuro carries two ids for one conversation::

    sessions.session_id         okuro's telemetry uuid, minted at bootstrap
    agent_sessions.session_id   the provider's native id (the JSONL filename)

They join on exactly 1 row out of 9369/10383. ``sense/retros.py`` documents
this and skips the join, which is why every retro to date ran on score and
tool-list metadata with no transcript access at all.

Exact methods only — and why
----------------------------
An earlier design proposed fuzzy matching on ``(provider, project_path,
start-time proximity)``. Ground truth on the pairs we *do* know killed it:

* start-time delta ranges 3 s → 210 543 s (mean 7.2 h), because Claude Code
  **resumes** sessions — one JSONL file accumulates across days, so
  ``agent_sessions.first_ts`` is when the *file* began, not when this okuro
  session began. Only 95 of 1930 known pairs fall within 60 s.
* ``sessions.project`` is empty and ``sessions.metadata`` is ``{}`` for the
  bridged rows, so there is no path to match on.
* the 1930 declared rows collapse to **118 distinct** native ids — many okuro
  sessions legitimately share one transcript.

Any timestamp heuristic would therefore invent pairings at a rate we could not
bound. This module implements only methods that are exact:

``declared``
    ``sessions.provider_session_id`` holds the native id outright. Populated
    from 2026-07 onward.

``embedded``
    The bootstrap packet prints ``**Session ID:** <telemetry-uuid>`` into its
    own output, which the transcript captures verbatim as a tool result. That
    string is recoverable back to 2026-04-12 — exactly the window
    ``provider_session_id`` misses. A hit is only accepted when the extracted
    uuid resolves to a real ``sessions`` row.

    The raw text this comes from is *not* its durable home: it is a
    ``tool_result`` body, which ``lifecycle.compact_traces`` nulls at the cool
    tier. :func:`harvest_session_links` persists every hit into
    ``trace_text_extracts`` and the rebuild reads from there, so compaction
    and rebuild no longer race for the same bytes. The dependency is declared
    in :mod:`.extractors`, which is what stops the compactor nulling text this
    module has not harvested yet.

``parent_path``
    Subagent transcripts live at ``.../<parent-native-id>/subagents/<agent>.jsonl``,
    so the parent link is read straight off the path. This covers all 4838
    subagent sessions and is structural, not inferred.

A session with no exact link simply stays unbridged. That is not a gap in the
analysis: :mod:`.detect` and :mod:`.analyze` read ``agent_events`` directly and
cover all 9369 traced sessions. The bridge exists purely to *enrich* a session
with its compliance score and task hint when that link is knowable.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Matches the bootstrap packet's own session line. The packet renders markdown
# (``- **Session ID:** <uuid>``) but plain-text providers strip the asterisks,
# so the separator is matched loosely rather than anchored to the markdown.
_SESSION_ID_RE = re.compile(
    r"Session ID:\**\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

# Confidence per method. All three are exact; the spread reflects how much
# indirection sits between the evidence and the claim, not match uncertainty.
_CONF_DECLARED = 1.0
_CONF_EMBEDDED = 0.95
_CONF_PARENTED = 0.90


def _iter_declared(db) -> list[dict]:
    """Pairs where ``sessions.provider_session_id`` names the native id."""
    rows = db.fetchall(
        """
        SELECT s.session_id AS telemetry_id,
               a.session_id AS native_id
        FROM sessions s
        JOIN agent_sessions a ON a.session_id = s.provider_session_id
        WHERE s.provider_session_id IS NOT NULL
          AND s.provider_session_id != ''
        """
    )
    return [
        {
            "telemetry_id": r["telemetry_id"],
            "native_id": r["native_id"],
            "method": "declared",
            "confidence": _CONF_DECLARED,
            "matched_on": {"column": "sessions.provider_session_id"},
        }
        for r in rows
    ]


def harvest_session_links(session_ids=None) -> dict:
    """Scan raw text for the embedded Session-ID line and persist every hit.

    This is the ONLY place that reads ``agent_events.text`` for this link, and
    it is registered in :mod:`.extractors` so ``compact_traces`` knows the
    dependency exists and refuses to null bodies this has not seen.

    Additive by contract: hits already in ``trace_text_extracts`` stay, so a
    session whose bodies were nulled after being harvested keeps its link. The
    converse is not recoverable — a session compacted *before* the first
    harvest reports zero hits here and is indistinguishable from one that never
    carried the string. That shortfall is visible only as a corpus total below
    the 623-link baseline, which is why the Phase 0 harvest reports its counts.

    Args:
        session_ids: Restrict the scan to these native session ids. None
            scans the whole corpus — the same cost the hourly bridge run has
            always paid, because the LIKE is what narrows it, not a window.
    """
    from okuro.db import get_db

    from . import extractors

    db = get_db()

    scope_ids = list(session_ids) if session_ids is not None else None
    if scope_ids is not None and not scope_ids:
        return {"extractor": "session_id_link", "sessions": 0, "hits": 0}
    if scope_ids is None:
        scope_ids = [
            r["session_id"]
            for r in db.fetchall("SELECT session_id FROM agent_sessions")
        ]

    # Positions BEFORE the scan, never after. Anything ingested while the scan
    # runs then falls beyond the watermark and is refused by the gate until the
    # next harvest, instead of being marked as read without being read.
    positions = extractors.session_positions(db, scope_ids)

    where = ["text LIKE '%Session ID:%'", "text IS NOT NULL"]
    params: list = []
    if session_ids is not None:
        where.append(f"session_id IN ({','.join('?' * len(scope_ids))})")
        params.extend(scope_ids)

    rows = db.fetchall(
        f"""
        SELECT session_id, ord, text
        FROM agent_events
        WHERE {' AND '.join(where)}
        """,
        tuple(params),
    )

    harvested: list[tuple] = []
    hits_by_session: dict[str, int] = {}
    for r in rows:
        match = _SESSION_ID_RE.search(r["text"] or "")
        if not match:
            continue  # "Session ID: unknown" and friends
        sid = r["session_id"]
        harvested.append((sid, r["ord"], match.group(1).lower(), {}))
        hits_by_session[sid] = hits_by_session.get(sid, 0) + 1

    written = extractors.store_extracts(db, "session_id_link", harvested)

    # BACKFILL FROM THE MATERIALISED INDEX. Harvesting raw text only rescues
    # links whose text still exists, and on this store it already does not:
    # measured 2026-08-13, 14 embedded links in session_bridge cannot be
    # reproduced from agent_events, and all 14 sit on cool sessions the
    # compactor nulled on 2026-07-27, 08-03 and 08-10. They survive today only
    # because the upsert is additive — the next rebuild=True would have taken
    # them, extract-then-null or not.
    #
    # So the harvest also imports what the index already knows. A link derived
    # once is durable from then on, whatever happens to the bytes it came from.
    # Provenance is recorded so the reader can still prefer a fresh extraction:
    # re-running after a regex change must not be outvoted by the answer the
    # old regex gave.
    backfill = [
        (r["native_session_id"], -1, r["telemetry_session_id"],
         {"source": "index_backfill"})
        for r in db.fetchall(
            "SELECT telemetry_session_id, native_session_id FROM session_bridge "
            "WHERE method = 'embedded'"
        )
    ]
    backfilled = extractors.store_extracts(db, "session_id_link", backfill)

    # Watermark EVERY session in scope, not just the ones that hit. A session
    # with no bootstrap echo has still been looked at; without its row the
    # compaction gate would hold it back forever.
    scanned = extractors.record_scan(db, "session_id_link", hits_by_session,
                                     scope_ids, positions)

    return {
        "extractor": "session_id_link",
        "sessions": scanned,
        "sessions_with_hits": len(hits_by_session),
        "hits": len(harvested),
        "rows_written": written,
        "index_backfilled": backfilled,
    }


def _iter_embedded(db) -> list[dict]:
    """Pairs recovered from the bootstrap packet echoed inside the transcript.

    Reads the PERSISTED harvest, never the raw text. That distinction is the
    whole point: ``bridge_sessions(rebuild=True)`` drops ``session_bridge``
    and rebuilds it from this function, and the raw text it used to read is
    scheduled for destruction by ``lifecycle.compact_traces``. Sourcing the
    rebuild from ``trace_text_extracts`` makes a rebuild-after-compaction
    lossless instead of one-way.

    Every extracted uuid is still verified against ``sessions`` before it is
    accepted — an id that names no real session is a rendering artifact, not
    a link.

    A freshly extracted hit beats a backfilled one. Both are durable, but
    ``rebuild=True`` exists to re-derive links after the extraction logic
    changes, and letting the previous answer win would make that a no-op for
    every session that already had one.
    """
    from . import extractors

    fresh: dict[str, str] = {}
    backfilled: dict[str, str] = {}
    for r in extractors.read_extracts(db, "session_id_link"):
        try:
            source = (json.loads(r["extra"] or "{}") or {}).get("source", "")
        except (TypeError, ValueError):
            source = ""
        target = backfilled if source == "index_backfill" else fresh
        # First hit wins: the bootstrap packet is emitted once, near the top,
        # and read_extracts orders by ord ascending.
        target.setdefault(r["native_session_id"], (r["value"] or "").lower())

    seen: dict[str, str] = dict(backfilled)
    seen.update(fresh)

    if not seen:
        return []

    # Verify each candidate resolves to a real session before accepting it.
    telemetry_ids = sorted(set(seen.values()))
    out: list[dict] = []
    chunk = 500
    real: set[str] = set()
    for i in range(0, len(telemetry_ids), chunk):
        batch = telemetry_ids[i : i + chunk]
        placeholders = ",".join("?" * len(batch))
        found = db.fetchall(
            f"SELECT session_id FROM sessions WHERE session_id IN ({placeholders})",
            tuple(batch),
        )
        real.update(row["session_id"] for row in found)

    for native_id, telemetry_id in seen.items():
        if telemetry_id not in real:
            continue
        out.append(
            {
                "telemetry_id": telemetry_id,
                "native_id": native_id,
                "method": "embedded",
                "confidence": _CONF_EMBEDDED,
                "matched_on": {"source": "bootstrap packet in transcript"},
            }
        )
    return out


def _iter_parented(db) -> list[dict]:
    """Subagent → spawning-parent links read off the transcript path.

    Path shape::

        .../projects/<slug>/<parent-native-id>/subagents/<agent-id>.jsonl

    The parent is the directory two levels above the file.

    This is a *different relation* from the telemetry bridge and has different
    cardinality — one parent spawns many subagents — so it lands in its own
    table keyed by the child. Folding it into ``session_bridge``, whose key is
    the telemetry side, silently collapsed 4838 links to 473 because every
    sibling collided on the shared parent id.
    """
    rows = db.fetchall(
        """
        SELECT session_id, source_path
        FROM agent_sessions
        WHERE session_id LIKE 'agent-%'
          AND source_path LIKE '%/subagents/%'
        """
    )
    out: list[dict] = []
    for r in rows:
        path = Path(r["source_path"])
        try:
            # <parent>/subagents/<file>.jsonl  →  parents[1] is <parent>
            parent_dir = path.parents[1]
        except IndexError:
            continue
        parent_id = parent_dir.name
        if not parent_id or parent_id == "projects":
            continue
        out.append({"child_id": r["session_id"], "parent_id": parent_id})
    return out


def bridge_sessions(rebuild: bool = False) -> dict:
    """Populate ``session_bridge`` from every exact method available.

    Harvests raw text into ``trace_text_extracts`` first, then rebuilds from
    the harvest. Both halves matter: the harvest keeps the persisted index
    current with newly ingested transcripts, and rebuilding from the harvest
    rather than the text is what makes ``rebuild=True`` safe on a compacted
    corpus.

    Args:
        rebuild: Drop existing rows first. Use after changing a method's
            extraction logic; otherwise the upsert keeps the strongest link.
            Safe on compacted sessions — see :func:`harvest_session_links`.

    Returns:
        Counts per method plus the resulting distinct-native-session coverage.
    """
    from okuro.db import get_db

    db = get_db()

    harvest = harvest_session_links()

    if rebuild:
        with db.write():
            db.execute("DELETE FROM session_bridge")
            db.execute("DELETE FROM session_parents")

    pairs = _iter_declared(db) + _iter_embedded(db)

    # Strongest method wins per telemetry id. Sorting ascending and letting
    # later writes overwrite would work too, but an explicit fold keeps the
    # precedence readable and avoids depending on INSERT ordering.
    best: dict[str, dict] = {}
    for p in pairs:
        current = best.get(p["telemetry_id"])
        if current is None or p["confidence"] > current["confidence"]:
            best[p["telemetry_id"]] = p

    written = 0
    with db.write():
        for p in best.values():
            db.execute(
                """
                INSERT INTO session_bridge
                    (telemetry_session_id, native_session_id, method,
                     confidence, matched_on)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telemetry_session_id) DO UPDATE SET
                    native_session_id = excluded.native_session_id,
                    method            = excluded.method,
                    confidence        = excluded.confidence,
                    matched_on        = excluded.matched_on
                WHERE excluded.confidence >= session_bridge.confidence
                """,
                (
                    p["telemetry_id"],
                    p["native_id"],
                    p["method"],
                    p["confidence"],
                    json.dumps(p["matched_on"], ensure_ascii=False),
                ),
            )
            written += 1

    # Subagent → parent, keyed by child so siblings do not collide.
    parents = _iter_parented(db)
    parents_written = 0
    with db.write():
        for p in parents:
            db.execute(
                """
                INSERT INTO session_parents (child_native_id, parent_native_id, source)
                VALUES (?, ?, 'transcript_path')
                ON CONFLICT(child_native_id) DO UPDATE SET
                    parent_native_id = excluded.parent_native_id
                """,
                (p["child_id"], p["parent_id"]),
            )
            parents_written += 1

    by_method = db.fetchall(
        "SELECT method, COUNT(*) AS n FROM session_bridge GROUP BY method"
    )
    distinct_native = db.fetchone(
        "SELECT COUNT(DISTINCT native_session_id) AS n FROM session_bridge"
    )

    return {
        "written": written,
        "by_method": {r["method"]: r["n"] for r in by_method},
        "distinct_native_sessions": (distinct_native or {}).get("n", 0),
        "subagent_parent_links": parents_written,
        "rebuilt": rebuild,
        "harvest": harvest,
    }


def resolve_telemetry(native_session_id: str) -> dict | None:
    """Return the okuro ``sessions`` row behind a native trace id, or None."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        """
        SELECT s.*, b.method, b.confidence
        FROM session_bridge b
        JOIN sessions s ON s.session_id = b.telemetry_session_id
        WHERE b.native_session_id = ?
        ORDER BY b.confidence DESC
        LIMIT 1
        """,
        (native_session_id,),
    )
    return dict(row) if row else None


def resolve_native(telemetry_session_id: str) -> str | None:
    """Return the native trace id behind an okuro telemetry id, or None."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT native_session_id FROM session_bridge WHERE telemetry_session_id = ?",
        (telemetry_session_id,),
    )
    return row["native_session_id"] if row else None
