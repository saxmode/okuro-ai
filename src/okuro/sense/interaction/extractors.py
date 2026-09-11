### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Declared-dependency registry — every module that derives data from agent_events raw text, and how it harvests.
# index: imports | TextExtractor | REGISTRY | READ_ONLY_CONSUMERS | register | registered | for_types | store_extracts | session_positions | record_scan | read_extracts | harvest_all | refusals
# AGENT_HEADER_END -->
"""Who reads ``agent_events`` raw text, and what they must persist first.

The problem this exists for is a class, not two bugs: **derived data whose
source is destroyable by another process.** ``lifecycle.compact_traces`` is
authorised to null ``agent_events.text``; two unrelated modules rebuild their
index by LIKE-scanning that same text. Neither knows about the other, so the
compactor cannot tell "this text is spent" from "this text is the only copy of
a link somebody will rebuild from next month".

A registry makes the dependency *declared* rather than implicit:

* every module deriving from raw text registers a :class:`TextExtractor` —
  its name, the event types and columns it scans, the LIKE patterns it looks
  for, and the handler that harvests those hits into
  ``trace_text_extracts``;
* :func:`harvest_all` runs the harvest side of every entry;
* ``compact_traces`` calls :func:`refusals` and **refuses** to null a session
  any registered extractor has not scanned THROUGH ITS LAST EVENT — fail
  closed, counted by distinct reason, never silently. "No extractor covers
  these types" is itself a refusal: an empty coverage set means nothing was
  checked, which is not the same as permission;
* ``tests/sense/test_trace_extract_registry.py`` fails when a raw-text scan of
  ``agent_events`` appears in a module the registry does not declare, so the
  third scanner cannot be added without either registering it or deleting the
  test.

Handlers are ``"module:function"`` strings resolved at call time, exactly like
:mod:`okuro.daemon.registry`. That is not decoration: the owning modules
(:mod:`.bridge`, :mod:`.improve`) import *this* module to read their extracts
back, so importing them here at module scope would be a cycle.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# An extractor that scans regardless of event type declares this. It is not a
# wildcard for convenience — it means the gate applies at every tier, which is
# the honest reading for anything hunting a string in bootstrap output: the
# packet lands in tool_result, but an agent quoting it lands in assistant.
ANY_TYPE = "*"


@dataclass(frozen=True)
class TextExtractor:
    """A declared dependency on ``agent_events`` raw text.

    Attributes:
        name: Registry key, also the ``trace_text_extracts.extractor`` value.
        handler: ``"module.path:function"``. The function takes an optional
            ``session_ids`` list and returns a dict with at least ``sessions``
            and ``hits``. It MUST be additive — harvesting twice is a no-op,
            and it must never delete from ``trace_text_extracts``.
        event_types: ``agent_events.type`` values scanned, or ``(ANY_TYPE,)``.
        columns: ``agent_events`` columns read as raw text.
        patterns: The SQL LIKE patterns used, verbatim. Documentation for the
            guard test and for whoever reads this next.
        rebuilds: Tables rebuilt from the harvest — what is lost if the
            harvest is skipped and the text is nulled.
    """

    name: str
    handler: str
    event_types: tuple[str, ...]
    columns: tuple[str, ...]
    patterns: tuple[str, ...]
    rebuilds: tuple[str, ...] = ()
    description: str = ""
    _extra_modules: tuple[str, ...] = field(default=(), repr=False)

    @property
    def module(self) -> str:
        """Dotted module path that owns the raw-text scan."""
        return self.handler.split(":", 1)[0]

    @property
    def modules(self) -> tuple[str, ...]:
        """Every module allowed to carry this extractor's raw-text scan."""
        return (self.module, *self._extra_modules)

    def resolve(self):
        """Import and return the harvest callable."""
        mod_path, _, func_name = self.handler.partition(":")
        return getattr(importlib.import_module(mod_path), func_name)

    def scans_type(self, event_type: str) -> bool:
        return ANY_TYPE in self.event_types or event_type in self.event_types


REGISTRY: dict[str, TextExtractor] = {}

# Modules that LIKE-scan agent_events raw text but derive nothing durable from
# it — they answer a question and return, so compaction degrades their answer
# rather than destroying an index. Declared HERE, next to the registry, so the
# guard test enforces a classification the code makes rather than one a test
# file keeps privately. Adding an entry is the visible act of asserting "this
# reader rebuilds nothing"; getting that wrong is how the third latent loss
# arrives.
READ_ONLY_CONSUMERS: dict[str, str] = {
    "okuro.trace.stats": (
        "after_signature matches e.text to find anchors for a behavioural "
        "query. Nothing is persisted from the match, so a compacted session "
        "simply stops contributing anchors — a quieter answer, not a lost one. "
        "The docstring already warns that it breaks on compacted tiers."
    ),
}


def register(extractor: TextExtractor) -> TextExtractor:
    """Add an extractor to the registry. Re-registering the same name replaces."""
    REGISTRY[extractor.name] = extractor
    return extractor


def registered() -> tuple[TextExtractor, ...]:
    """Every declared extractor, in registration order."""
    return tuple(REGISTRY.values())


def for_types(event_types) -> tuple[TextExtractor, ...]:
    """Extractors whose scan overlaps these ``agent_events.type`` values.

    This is what makes the compaction gate proportionate: a tier that only
    nulls ``tool_result`` need not wait on an extractor that reads nothing but
    ``user`` rows.
    """
    types = tuple(event_types)
    return tuple(
        e for e in REGISTRY.values() if any(e.scans_type(t) for t in types)
    )


# ---------------------------------------------------------------------------
# The persisted side — shared by every extractor
# ---------------------------------------------------------------------------


def store_extracts(db, extractor: str, rows) -> int:
    """Persist harvested hits. Additive: an existing row is left alone.

    ``rows`` is an iterable of ``(native_session_id, ord, value, extra_dict)``.

    Returns rows actually INSERTED, not rows attempted. The difference matters:
    the harvest is idempotent by design, so a counter that cannot fall to zero
    on a repeat run cannot show that the repeat was a no-op — it just reports
    the same inflated number forever and hides whether anything new was found.
    ``cursor.rowcount`` is 1 on insert and 0 when ON CONFLICT DO NOTHING fires.
    """
    written = 0
    rows = list(rows)
    if not rows:
        return 0
    with db.write():
        for sid, ord_, value, extra in rows:
            cur = db.execute(
                """
                INSERT INTO trace_text_extracts
                    (extractor, native_session_id, ord, value, extra)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(extractor, native_session_id, ord, value) DO NOTHING
                """,
                (extractor, sid, int(ord_ if ord_ is not None else -1), value,
                 json.dumps(extra or {}, ensure_ascii=False)),
            )
            written += max(0, getattr(cur, "rowcount", 0) or 0)
    return written


def session_positions(db, session_ids) -> dict[str, int]:
    """Highest ``agent_events.ord`` currently present, per session.

    This is the position a harvest can honestly claim to have scanned through.
    Capture it BEFORE the scan, never after: rows ingested while the scan runs
    would otherwise be watermarked as seen without having been read.
    """
    sids = list(session_ids)
    out: dict[str, int] = {}
    chunk = 400
    for i in range(0, len(sids), chunk):
        batch = sids[i : i + chunk]
        placeholders = ",".join("?" * len(batch))
        for row in db.fetchall(
            f"""
            SELECT session_id, MAX(ord) AS max_ord
            FROM agent_events
            WHERE session_id IN ({placeholders})
            GROUP BY session_id
            """,
            tuple(batch),
        ):
            out[row["session_id"]] = int(row["max_ord"] or -1)
    return out


def record_scan(db, extractor: str, hits_by_session: dict[str, int],
                session_ids, positions: dict[str, int] | None = None) -> int:
    """Mark how far this extractor scanned each session, hit or no hit.

    Every session in ``session_ids`` gets a row — a session with zero hits has
    still been looked at, and without that row the compaction gate would treat
    it as permanently unharvested and never reclaim it.

    ``positions`` is the ord each session was scanned THROUGH, from
    :func:`session_positions` taken before the scan. It only ever moves
    forward: a narrower re-harvest must not retract a watermark a wider one
    already earned.
    """
    sids = list(session_ids)
    if not sids:
        return 0
    positions = positions or {}
    with db.write():
        for sid in sids:
            db.execute(
                """
                INSERT INTO trace_extract_scanned
                    (extractor, native_session_id, hits, scanned_through_ord,
                     scanned_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(extractor, native_session_id) DO UPDATE SET
                    hits                = MAX(trace_extract_scanned.hits,
                                              excluded.hits),
                    scanned_through_ord = MAX(
                        trace_extract_scanned.scanned_through_ord,
                        excluded.scanned_through_ord),
                    scanned_at          = datetime('now')
                """,
                (extractor, sid, int(hits_by_session.get(sid, 0)),
                 int(positions.get(sid, -1))),
            )
    return len(sids)


def read_extracts(db, extractor: str) -> list[dict]:
    """Every persisted hit for an extractor, oldest ordinal first.

    This is the read path the rebuild routes must use. Reading raw text here
    instead is the defect the registry exists to prevent.
    """
    return db.fetchall(
        """
        SELECT native_session_id, ord, value, extra
        FROM trace_text_extracts
        WHERE extractor = ?
        ORDER BY native_session_id, ord
        """,
        (extractor,),
    )


# ---------------------------------------------------------------------------
# Harvest + gate
# ---------------------------------------------------------------------------


def harvest_all(session_ids=None) -> dict:
    """Run every registered harvest. Returns per-extractor counts.

    Called before compaction (extract-then-null) and at the top of each
    rebuild path, so the persisted index is never behind the raw text.
    """
    out: dict[str, dict] = {}
    for extractor in registered():
        try:
            fn = extractor.resolve()
            out[extractor.name] = fn(session_ids=session_ids)
        except Exception as exc:  # noqa: BLE001 — one bad harvest must not
            # silently unlock compaction for the others; report and move on.
            # The gate below still refuses whatever this extractor did not scan.
            log.warning("harvest %s failed: %s", extractor.name, exc)
            out[extractor.name] = {"error": str(exc)}
    return out


def refusals(db, session_ids, event_types) -> dict[str, list[str]]:
    """Sessions that must NOT be compacted, keyed by the reason they must not.

    An empty dict means every session is safe. Anything else is a refusal the
    caller has to honour, and the key is the counter to report it under. Three
    reasons, deliberately distinct — a gate whose refusals all look alike
    cannot tell you which failure you have:

    ``no_extractor_covers:<types>``
        No registered extractor reads these event types. The honest reading is
        *unknown*, not *allowed*: an empty coverage set means nothing checked,
        and treating that as permission makes the gate silently inert the
        moment someone narrows an extractor's ``event_types`` — or forgets to
        register one at all. Fail closed.

    ``unharvested:<name>``
        The extractor has never scanned this session.

    ``stale:<name>``
        The extractor scanned this session, but events have arrived since.
        This is the case a boolean watermark cannot see: a resumed transcript
        appends rows carrying their ORIGINAL timestamps, so the session stays
        in the cool tier and stays compactable while holding text no harvest
        has read.
    """
    sids = list(session_ids)
    if not sids:
        return {}

    relevant = for_types(event_types)
    if not relevant:
        # Fail CLOSED. See the docstring above — this is the branch that used
        # to return {} and hand out permission nobody granted.
        label = ",".join(sorted(str(t) for t in event_types)) or "*"
        return {f"no_extractor_covers:{label}": sids}

    positions = session_positions(db, sids)

    out: dict[str, list[str]] = {}
    chunk = 400
    for extractor in relevant:
        scanned: dict[str, int] = {}
        for i in range(0, len(sids), chunk):
            batch = sids[i : i + chunk]
            placeholders = ",".join("?" * len(batch))
            for row in db.fetchall(
                f"""
                SELECT native_session_id, scanned_through_ord
                FROM trace_extract_scanned
                WHERE extractor = ?
                  AND native_session_id IN ({placeholders})
                """,
                (extractor.name, *batch),
            ):
                scanned[row["native_session_id"]] = int(
                    row["scanned_through_ord"] if row["scanned_through_ord"]
                    is not None else -1
                )

        never = [s for s in sids if s not in scanned]
        stale = [
            s for s in sids
            if s in scanned and positions.get(s, -1) > scanned[s]
        ]
        if never:
            out[f"unharvested:{extractor.name}"] = never
        if stale:
            out[f"stale:{extractor.name}"] = stale
    return out


# ---------------------------------------------------------------------------
# The declared dependencies
# ---------------------------------------------------------------------------
# Both entries scan for a string the BOOTSTRAP PACKET prints into its own
# output, which the transcript captures as a tool_result — the first body the
# cool tier nulls. Declaring ANY_TYPE rather than ('tool_result',) is
# deliberate: providers differ in which row carries the echo, and a gate that
# is too narrow fails open.

register(TextExtractor(
    name="session_id_link",
    handler="okuro.sense.interaction.bridge:harvest_session_links",
    event_types=(ANY_TYPE,),
    columns=("text",),
    patterns=("%Session ID:%",),
    rebuilds=("session_bridge",),
    description=(
        "Telemetry->native session link recovered from the bootstrap packet "
        "echoed in the transcript. The only recovery path back to 2026-04-12; "
        "623 links at last count (memory 9f438b2f)."
    ),
))

register(TextExtractor(
    name="role_adoption",
    handler="okuro.sense.interaction.improve:harvest_role_adoptions",
    event_types=(ANY_TYPE,),
    columns=("text", "tool_name"),
    patterns=("%Adopt this role%", "%roles_get%"),
    rebuilds=("session_roles",),
    description=(
        "Which role was driving a session — from an explicit roles_get call or "
        "the bootstrap packet's own assignment. 381 attributions at last count "
        "(migration 114)."
    ),
))
