# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Canonical profile-delta writer — one ingress for preset/file/questionnaire person enrichment paths.
# index:
#   imports
#   _VALID_SOURCE_TYPES
#   SourceTier / SOURCE_TIERS / _validate_source
#   _new_id
#   def apply_profile_delta
#   def apply_preset
#   def list_sources
#   def list_source_groups
#   def remove_source
# AGENT_HEADER_END -->
"""Canonical profile-delta writer for person enrichment.

Every path that populates a person's communication / cognitive shape —
role preset, file drop (eml / pdf / notes), self-report questionnaire —
converges on `apply_profile_delta()`. It:

  1. Writes a row to `person_sources` per field (provenance + confidence)
  2. Merges the delta into the persons.communication / persons.cognitive
     JSON columns (shallow per-key merge — a new row supersedes a prior
     one for the same field_path)
  3. Re-embeds the person so vector search reflects the new content

This keeps the three population paths honest: they all share one merge
semantic, one provenance ledger, one re-embed pass. Without this, each
path tended to drift (different shapes, silent clobbers, no audit).

`apply_preset()` is a thin wrapper that resolves a role-catalog preset
and forwards to apply_profile_delta. File / questionnaire paths live
in their own modules but write through the same function.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

# Module-level so a test can monkeypatch the gate on THIS module — the same
# handle the rest of the people module is rolled back by.
from okuro.peer.flags import people_strict_enabled, warn_lenient

log = logging.getLogger("okuro.peer.sources")

_VALID_SOURCE_TYPES = (
    "preset",
    "manual",
    "eml",
    "notes",
    "pdf",
    "questionnaire",
    "inferred",
    "web",       # web-research extract — source_ref is the page URL
    "url",       # explicit single-URL scrape
    "negotiated",  # T3 — survived a blind A/B the person could have failed it on
)
# Safe to extend: migration 075 dropped person_sources' source_type CHECK
# constraint in favour of validating at the writer, which is why this tuple is
# the only gate. 028's original CHECK never listed web/url either.


# ── T0-T3 acquisition tiers ─────────────────────────────────────────
#
# What a value is worth depends on how it was obtained, so the tier is a
# property of the source_type rather than something a caller asserts.
#
#   T0  assumption      <=0.3   someone typed it. Labelled, never rendered
#                               as fact.
#   T1  sourced fact  0.5-0.7   traceable to an artifact. MUST carry a
#                               source_ref — a claim whose proof link is
#                               absent is a T0 assumption wearing a T1 badge,
#                               and person_sources exists precisely to answer
#                               "is this actually true?".
#   T2  self-authored    0.9    the person said it about themselves.
#   T3  negotiated       1.0    the person confirmed it against a prediction
#                               (P3.4). No writer yet — recorded so the tier
#                               vocabulary is complete rather than invented
#                               later.
#
# PROPOSAL-ONLY sits outside the tiers: an observed signal is evidence about
# behaviour, not a statement of preference, and consent-first means it may
# only ever be queued for confirmation.
@dataclass(frozen=True)
class SourceTier:
    tier: str
    max_confidence: float
    requires_source_ref: bool = False
    proposal_only: bool = False


SOURCE_TIERS: dict[str, SourceTier] = {
    "manual":        SourceTier("T0", 0.3),
    "preset":        SourceTier("T0", 0.3),
    "eml":           SourceTier("T1", 0.7),
    "notes":         SourceTier("T1", 0.7),
    "pdf":           SourceTier("T1", 0.7),
    # The two the rule bites on. Their source_ref is a URL the reader can
    # open; the file types above carry a filename that may no longer exist,
    # so demanding one of them would be theatre.
    "web":           SourceTier("T1", 0.7, requires_source_ref=True),
    "url":           SourceTier("T1", 0.7, requires_source_ref=True),
    "questionnaire": SourceTier("T2", 0.9),
    # T3's writer is peer.blind_check.record_outcome. It carries a source_ref
    # naming the pre-registered protocol artifact, so "why is this axis at
    # 1.0" is answerable from the ledger alone.
    "negotiated":    SourceTier("T3", 1.0, requires_source_ref=True),
    "inferred":      SourceTier("T0", 0.3, proposal_only=True),
}


# ── P3.5: what a THIRD PARTY's page may assert about someone ────────
#
# The distinction is WHO AUTHORED the material, not the tier band. eml,
# notes and pdf are things the person wrote — a preference stated in their
# own words is theirs. A web page is what somebody ELSE wrote about them, and
# "reads like a visual thinker" from a conference bio is a stranger's guess
# recorded at the same confidence as an answer.
#
# So a web source may carry FACTS — what they do, at what level — and nothing
# about how they think or want to be written to. Traits are dropped and named
# in `refused_fields` rather than silently discarded; a caller who dropped a
# page expecting a full profile deserves to be told what did not land.
FACT_ONLY_SOURCE_TYPES = frozenset({"web", "url"})

# Deliberately an allowlist. A denylist of trait keys would silently admit
# every field the extractor prompt grows next.
_FACT_KEYS: dict[str, frozenset[str]] = {
    "cognitive": frozenset({"profession", "function", "seniority"}),
    # Every communication field is a preference the person alone can state.
    "communication": frozenset(),
}


def _fact_filter(
    source_type: str, delta: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Strip trait inference from a third-party source. Returns (delta, refused).

    Gated by ``people.strict`` like every other behaviour change here, so the
    prior (everything-lands) behaviour is one config line away.
    """
    if source_type not in FACT_ONLY_SOURCE_TYPES:
        return delta, []
    if not people_strict_enabled():
        warn_lenient(
            "sources._fact_filter",
            f"{source_type} delta written unfiltered — trait inference from a "
            "third-party source is landing",
        )
        return delta, []

    out: dict[str, Any] = {}
    refused: list[str] = []
    for bucket in ("communication", "cognitive"):
        incoming = delta.get(bucket)
        if not isinstance(incoming, dict):
            continue
        allowed = _FACT_KEYS.get(bucket, frozenset())
        kept = {k: v for k, v in incoming.items() if k in allowed}
        refused.extend(f"{bucket}.{k}" for k in incoming if k not in allowed)
        if kept:
            out[bucket] = kept
    if refused:
        log.info(
            "%s source: dropped %d inferred field(s) — %s",
            source_type, len(refused), ", ".join(sorted(refused)),
        )
    return out, sorted(refused)


def _validate_source(
    source_type: str, source_ref: str | None, *, apply: bool = True
) -> None:
    """The tier gate. ONE definition, called by every ledger writer.

    PLAN v5 puts this in :func:`record_applied_value` alone. Measured, that
    is the one writer it cannot protect: its only caller is
    ``persons.person_update_sliders`` with source_type="manual", while every
    ``web`` / ``url`` write reaches the ledger through
    :func:`apply_profile_delta` (api/people.py's file-drop, and
    :func:`person_source_add`). Enforcing there and not here would ship a
    hard-fail nothing can reach — a rule that reads like a guarantee and
    behaves like a comment.

    The unknown-source_type refusal is NOT behind ``people.strict``: it has
    always raised, so leniency there would be a new hole rather than a
    rollback. The two tier rules ARE gated, because they change what a
    shipped path accepts.
    """
    if source_type not in _VALID_SOURCE_TYPES:
        raise ValueError(
            f"Unknown source_type: {source_type}. "
            f"Valid: {', '.join(_VALID_SOURCE_TYPES)}."
        )
    spec = SOURCE_TIERS[source_type]

    if spec.requires_source_ref and not (source_ref or "").strip():
        if people_strict_enabled():
            raise ValueError(
                f"source_type={source_type!r} is tier {spec.tier} (sourced fact) "
                "and requires a non-empty source_ref — the URL the claim came "
                "from. Without it the row asserts provenance it cannot show. "
                "Use source_type='manual' for an unsourced assumption."
            )
        warn_lenient(
            "sources._validate_source.source_ref",
            f"{source_type} row written with no source_ref",
        )

    if spec.proposal_only and apply:
        if people_strict_enabled():
            raise ValueError(
                f"source_type={source_type!r} is an observed signal and may only "
                "be queued as a proposal — call with apply=False. Observed "
                "behaviour is evidence, not a self-reported preference; it is "
                "confirmed at T2/T3 or it expires."
            )
        warn_lenient(
            "sources._validate_source.proposal_only",
            f"{source_type} applied directly instead of queued",
        )


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ── Ledger-only writer ──────────────────────────────────────────────


def record_applied_value(
    db: Any,
    person_id: str,
    field_path: str,
    value: Any,
    *,
    source_type: str,
    source_ref: str | None = None,
    confidence: float = 1.0,
    axis: str | None = None,
    lens_key: str | None = None,
) -> str:
    """Write ONE applied provenance row. Does not touch the persons columns.

    For write paths that already own their column update and only need the
    ledger to cover them — the primary example being
    ``persons.person_update_sliders``, which has always written
    ``cognitive.slider_provenance`` inline and skipped ``person_sources``
    entirely, leaving the audit trail blind to the very writes it exists
    to audit.

    TWO ROW SHAPES, and the difference decides whether data survives:

    ``axis=None`` — a WHOLESALE row. ``value`` MUST be the complete
      post-merge value for ``field_path``, never the patch that produced it.
      :func:`_rebuild_person_columns` replays the latest surviving wholesale
      row in full, so a row saying ``cognitive.sliders`` = ``{"jargon": 4}``
      does not mean "jargon became 4" — it means "the vector IS {jargon: 4}",
      and a later removal enacts exactly that, deleting every other axis.

    ``axis="jargon"`` — a PER-AXIS row (migration 123). ``value`` is that one
      axis's value, and the rebuild reconstructs the dict axis by axis. This
      is what a partial update actually is, so partial writers should use it.

    ``lens_key="work"`` — a THREE-level row (migration 127). ``cognitive.lenses``
      is ``{lens_key: {axis: value}}``, one level deeper than anything else
      tracked, so the axis alone cannot locate the value. Both coordinates
      that do not fit in ``field_path`` live in columns, for the same reason
      the axis does.

    ``field_path`` is always exactly ``bucket.key``. A deeper path is
    REFUSED rather than stored: the rebuild splits on the first dot only, so
    ``cognitive.sliders.jargon`` would write the literal key
    ``"sliders.jargon"`` into ``persons.cognitive``, beside the real vector,
    and every reader would keep seeing the stale one. The axis belongs in
    its column, not in the path.

    Returns the new row id. Does not commit — the caller owns the tx.
    """
    _validate_source(source_type, source_ref)
    if field_path.count(".") != 1:
        raise ValueError(
            f"field_path must be exactly 'bucket.key', got {field_path!r}. "
            "Deeper paths corrupt persons.cognitive on rebuild — see the "
            "contract in record_applied_value's docstring."
        )
    row_id = _new_id()
    db.execute(
        """INSERT INTO person_sources (
               id, person_id, source_type, source_ref, field_path,
               extracted_value, confidence, applied, axis, lens_key
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            row_id,
            person_id,
            source_type,
            source_ref,
            field_path,
            json.dumps(value, default=str),
            float(confidence),
            axis,
            lens_key,
        ),
    )
    return row_id


def record_history(
    db: Any,
    person_id: str,
    field_path: str,
    *,
    old_value: Any,
    new_value: Any,
    axis: str | None = None,
    source: str | None = None,
) -> str | None:
    """Append one typed profile-change event. Never raises.

    A slider edit was a destructive in-place overwrite: slider_provenance
    keeps one entry per axis and the writer replaces it, so it is a
    last-write-wins map, not a history. "What was this axis three months
    ago" had no answer anywhere.

    The op is DERIVED from the values rather than passed in, so a caller
    cannot mislabel it: no old value is an ``add``, no new value is a
    ``delete``, anything else is an ``update``. An unchanged value writes
    nothing — a history of non-events buries the events.

    Swallows every error, including a missing table. This is an archive;
    history that breaks the present is worse than no history, and a DB
    restored from before migration 124 must keep working.
    """
    if old_value == new_value:
        return None
    if old_value is None:
        op = "add"
    elif new_value is None:
        op = "delete"
    else:
        op = "update"
    row_id = _new_id()
    try:
        db.execute(
            """INSERT INTO person_profile_history (
                   id, person_id, op, field_path, axis,
                   old_value, new_value, source
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                person_id,
                op,
                field_path,
                axis,
                None if old_value is None else json.dumps(old_value, default=str),
                None if new_value is None else json.dumps(new_value, default=str),
                source,
            ),
        )
    except Exception:  # noqa: BLE001 — un-migrated DB, or no table at all
        log.debug("profile history unavailable for %s", person_id)
        return None
    return row_id


def profile_history(
    db: Any, person_id: str, *, axis: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Change events for a person, newest first. ``[]`` when unavailable."""
    sql = ("SELECT op, field_path, axis, old_value, new_value, source, created_at "
           "FROM person_profile_history WHERE person_id = ?")
    params: list[Any] = [person_id]
    if axis:
        sql += " AND axis = ?"
        params.append(axis)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(int(limit))
    try:
        rows = db.fetchall(sql, tuple(params))
    except Exception:  # noqa: BLE001 — table absent on an older DB
        return []
    out: list[dict[str, Any]] = []
    for r in rows or []:
        entry = dict(r)
        for key in ("old_value", "new_value"):
            raw = entry.get(key)
            if raw is None:
                continue
            try:
                entry[key] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                pass
        out.append(entry)
    return out


# ── Core writer ─────────────────────────────────────────────────────


def apply_profile_delta(
    person_id: str,
    source_type: str,
    source_ref: str | None,
    delta: dict[str, Any],
    *,
    confidence: float = 0.8,
    apply: bool = True,
) -> dict[str, Any]:
    """Write provenance rows for `delta` and (optionally) merge into persons.

    Args:
        person_id: Slug of the person being enriched.
        source_type: One of _VALID_SOURCE_TYPES.
        source_ref: Free-form reference — filename, preset role_id,
            questionnaire id. Stored verbatim; no uniqueness constraint.
        delta: Shape ``{communication: {...}, cognitive: {...}}``.
            Nested dicts merged per top-level key; lists REPLACE (not
            append). Unknown top-level keys are ignored.
        confidence: 0.0-1.0. Preset defaults ~0.7, file extracts ~0.6,
            questionnaire self-report ~0.95.
        apply: If False, only provenance rows are written — the persons
            row is left untouched (preview mode).

    Returns ``{sources_written, fields_changed, apply}``.
    """
    _validate_source(source_type, source_ref, apply=apply)
    delta, refused_fields = _fact_filter(source_type, delta)

    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM persons WHERE id = ?", (person_id,))
    if not row:
        raise LookupError(f"Person not found: {person_id}")

    current: dict[str, Any] = {}
    for col in ("communication", "cognitive"):
        raw = row[col] if col in row.keys() else None
        try:
            current[col] = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            current[col] = {}

    sources_written = 0
    fields_changed: list[str] = []

    for bucket in ("communication", "cognitive"):
        incoming = delta.get(bucket)
        if not isinstance(incoming, dict):
            continue
        for key, value in incoming.items():
            if value is None:
                continue
            field_path = f"{bucket}.{key}"
            db.execute(
                """INSERT INTO person_sources (
                       id, person_id, source_type, source_ref, field_path,
                       extracted_value, confidence, applied
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _new_id(),
                    person_id,
                    source_type,
                    source_ref,
                    field_path,
                    json.dumps(value, default=str),
                    float(confidence),
                    1 if apply else 0,
                ),
            )
            sources_written += 1
            if apply:
                current[bucket][key] = value
                fields_changed.append(field_path)

    if apply and fields_changed:
        db.execute(
            """UPDATE persons
               SET communication = ?, cognitive = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (
                json.dumps(current["communication"]),
                json.dumps(current["cognitive"]),
                person_id,
            ),
        )

    db.conn.commit()

    # Re-embed on best-effort basis (mirrors persons._embed_person).
    if apply and fields_changed:
        try:
            from okuro.peer.persons import _embed_person

            r = db.fetchone(
                "SELECT display_name, organization, role, notes, tags "
                "FROM persons WHERE id = ?",
                (person_id,),
            )
            if r:
                tags_raw = r["tags"] or "[]"
                try:
                    tags = json.loads(tags_raw)
                except (TypeError, json.JSONDecodeError):
                    tags = []
                _embed_person(
                    person_id,
                    r["display_name"],
                    r["organization"],
                    r["role"],
                    r["notes"],
                    tags,
                )
        except Exception as exc:  # pragma: no cover — embed is best-effort
            log.debug("re-embed after delta failed: %s", exc)

    return {
        "sources_written": sources_written,
        "fields_changed": fields_changed,
        "apply": apply,
        # Named, not silently dropped — a caller who dropped a page expecting
        # a full profile has to be able to see what P3.5 refused.
        "refused_fields": refused_fields,
    }


# ── Preset applier ──────────────────────────────────────────────────


def person_source_add(
    person_id: str,
    field_path: str,
    value: Any,
    *,
    source_url: str | None = None,
    source_type: str = "web",
    confidence: float = 0.8,
    apply: bool = True,
) -> dict[str, Any]:
    """Record ONE sourced, confidence-scored claim about a person.

    The agent-facing primitive behind web/manual enrichment: every claim
    carries its source and certainty, so a user can later verify WHY a fact
    was registered (e.g. "understands numbers" <- conf 0.8 <- cv @ xy.com).

    field_path is ``communication.<key>`` or ``cognitive.<key>`` — the two
    merge buckets apply_profile_delta understands (e.g.
    ``cognitive.knowledge_areas``, ``cognitive.topic_interests``). Lists
    REPLACE the prior value (not append); read-modify-write to accumulate.
    ``source_url`` is stored as source_ref (the proof link).
    """
    if "." not in field_path:
        raise ValueError(
            "field_path must be 'communication.<key>' or 'cognitive.<key>'")
    bucket, key = field_path.split(".", 1)
    if bucket not in ("communication", "cognitive"):
        raise ValueError("field_path bucket must be 'communication' or 'cognitive'")
    return apply_profile_delta(
        person_id,
        source_type=source_type,
        source_ref=source_url,
        delta={bucket: {key: value}},
        confidence=confidence,
        apply=apply,
    )


def apply_preset(person_id: str, role_query: str) -> dict[str, Any]:
    """Resolve a role-catalog preset and write it as a source for this person.

    Returns the same shape as apply_profile_delta, plus a ``role_id`` field
    so callers can show "Applied <role_id> preset" in the UI.
    """
    from okuro.peer.presets import resolve_preset

    hit = resolve_preset(role_query)
    if not hit:
        return {
            "sources_written": 0,
            "fields_changed": [],
            "apply": False,
            "role_id": None,
            "note": f"No catalog preset matched '{role_query}'",
        }

    preset = hit.get("person_preset") or {}
    delta = {
        "communication": preset.get("communication") or {},
        "cognitive": preset.get("cognitive") or {},
    }
    result = apply_profile_delta(
        person_id,
        source_type="preset",
        source_ref=hit["role_id"],
        delta=delta,
        confidence=0.7,
    )
    result["role_id"] = hit["role_id"]
    return result


# ── Provenance listing ──────────────────────────────────────────────


def list_sources(person_id: str, applied_only: bool = True) -> list[dict[str, Any]]:
    """List the provenance rows for a person — used by the UI chip row.

    Returns newest-first; same field_path may appear multiple times across
    source_types (presets get superseded by self-report, etc).
    """
    from okuro.db import get_db

    db = get_db()
    sql = (
        "SELECT id, source_type, source_ref, field_path, extracted_value, "
        "confidence, applied, created_at "
        "FROM person_sources WHERE person_id = ?"
    )
    params: tuple = (person_id,)
    if applied_only:
        sql += " AND applied = 1"
    # Same tie-break as _rebuild_person_columns, so what the list shows as
    # newest is the row a rebuild would actually replay.
    sql += " ORDER BY created_at DESC, rowid DESC"

    rows = db.fetchall(sql, params)
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["extracted_value"] = json.loads(item.get("extracted_value") or "null")
        except (TypeError, json.JSONDecodeError):
            pass
        out.append(item)
    return out


def list_source_groups(person_id: str) -> list[dict[str, Any]]:
    """Group a person's applied provenance rows by (source_type, source_ref).

    One entry per uploaded file / preset / questionnaire — the removable unit
    the Enrich panel shows. Newest-first by the group's latest write.
    """
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT source_type, source_ref, COUNT(*) AS field_count, "
        "MAX(created_at) AS latest_at, MIN(confidence) AS confidence, "
        "GROUP_CONCAT(field_path) AS fields "
        "FROM person_sources WHERE person_id = ? AND applied = 1 "
        "GROUP BY source_type, source_ref "
        "ORDER BY latest_at DESC",
        (person_id,),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        raw = item.pop("fields", "") or ""
        item["fields"] = [f for f in raw.split(",") if f]
        out.append(item)
    return out


def _rebuild_person_columns(db: Any, person_id: str, field_paths: list[str]) -> list[str]:
    """Recompute `field_paths` on persons.{communication,cognitive} from the
    remaining applied source rows. Latest applied row wins; a field with no
    surviving source is dropped. Returns the field_paths actually reverted.

    Assumes the caller already deleted the rows being removed and holds an open
    transaction (commit is the caller's responsibility).
    """
    row = db.fetchone(
        "SELECT communication, cognitive FROM persons WHERE id = ?", (person_id,)
    )
    if not row:
        return []
    current: dict[str, dict] = {}
    for col in ("communication", "cognitive"):
        raw = row[col] if col in row.keys() else None
        try:
            current[col] = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            current[col] = {}

    reverted: list[str] = []
    for field_path in field_paths:
        if "." not in field_path:
            continue
        bucket, key = field_path.split(".", 1)
        if bucket not in current:
            continue
        # created_at is second-granularity (`datetime('now')`), so "latest
        # applied row wins" was decided by an ambiguous key: two writes in
        # the same second tied, and the rebuild could resurrect the OLDER
        # value — silently reverting a field to a superseded state. rowid
        # is monotonic per insert and breaks the tie the way wall-clock
        # intended.
        # THREE-level rows first (migration 127). cognitive.lenses is
        # {lens_key: {axis: value}}; rebuilding it with the per-axis branch
        # below would collapse every lens into one axis dict — the exact
        # corruption the lens_key column exists to prevent.
        per_lens = db.fetchall(
            "SELECT lens_key, axis, extracted_value FROM person_sources "
            "WHERE person_id = ? AND field_path = ? AND applied = 1 "
            "AND lens_key IS NOT NULL AND axis IS NOT NULL "
            "ORDER BY created_at ASC, rowid ASC",
            (person_id, field_path),
        )
        if per_lens:
            lenses: dict[str, dict[str, Any]] = {}
            for r in per_lens:
                try:
                    lenses.setdefault(r["lens_key"], {})[r["axis"]] = json.loads(
                        r["extracted_value"])
                except (TypeError, json.JSONDecodeError):
                    continue
            if lenses:
                current[bucket][key] = lenses
            else:
                current[bucket].pop(key, None)
            reverted.append(field_path)
            continue

        per_axis = db.fetchall(
            "SELECT axis, extracted_value FROM person_sources "
            "WHERE person_id = ? AND field_path = ? AND applied = 1 "
            "AND axis IS NOT NULL AND lens_key IS NULL "
            "ORDER BY created_at ASC, rowid ASC",
            (person_id, field_path),
        )
        if per_axis:
            # Per-axis rows (migration 123): rebuild the dict axis by axis,
            # last writer per axis wins. A wholesale replay here would be the
            # bug this shape exists to remove — it would drop every axis whose
            # row happened to be older than some other axis's row.
            rebuilt: dict[str, Any] = {}
            for r in per_axis:
                try:
                    rebuilt[r["axis"]] = json.loads(r["extracted_value"])
                except (TypeError, json.JSONDecodeError):
                    continue
            if rebuilt:
                current[bucket][key] = rebuilt
            else:
                current[bucket].pop(key, None)
            reverted.append(field_path)
            continue

        survivor = db.fetchone(
            "SELECT extracted_value FROM person_sources "
            "WHERE person_id = ? AND field_path = ? AND applied = 1 "
            "AND axis IS NULL "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (person_id, field_path),
        )
        if survivor:
            try:
                current[bucket][key] = json.loads(survivor["extracted_value"])
            except (TypeError, json.JSONDecodeError):
                current[bucket].pop(key, None)
        else:
            current[bucket].pop(key, None)
        reverted.append(field_path)

    db.execute(
        "UPDATE persons SET communication = ?, cognitive = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (json.dumps(current["communication"]), json.dumps(current["cognitive"]), person_id),
    )
    return reverted


def remove_source(
    person_id: str, source_type: str, source_ref: str | None
) -> dict[str, Any]:
    """Delete one source group and revert the fields it set.

    Deletes every person_sources row matching (person_id, source_type,
    source_ref), then rebuilds each affected field from the remaining applied
    sources (latest wins; orphaned fields are cleared). Re-embeds the person.

    Returns ``{removed_rows, fields_reverted}``.
    """
    from okuro.db import get_db

    db = get_db()

    ref_clause = "source_ref IS NULL" if source_ref is None else "source_ref = ?"
    where_params: tuple = (
        (person_id, source_type)
        if source_ref is None
        else (person_id, source_type, source_ref)
    )

    affected = db.fetchall(
        f"SELECT DISTINCT field_path FROM person_sources "
        f"WHERE person_id = ? AND source_type = ? AND {ref_clause}",
        where_params,
    )
    field_paths = [r["field_path"] for r in affected]
    if not field_paths:
        return {"removed_rows": 0, "fields_reverted": []}

    cur = db.execute(
        f"DELETE FROM person_sources "
        f"WHERE person_id = ? AND source_type = ? AND {ref_clause}",
        where_params,
    )
    removed_rows = getattr(cur, "rowcount", 0) or 0

    reverted = _rebuild_person_columns(db, person_id, field_paths)
    db.conn.commit()

    # Re-embed best-effort (mirrors apply_profile_delta).
    try:
        from okuro.peer.persons import _embed_person

        r = db.fetchone(
            "SELECT display_name, organization, role, notes, tags "
            "FROM persons WHERE id = ?",
            (person_id,),
        )
        if r:
            try:
                tags = json.loads(r["tags"] or "[]")
            except (TypeError, json.JSONDecodeError):
                tags = []
            _embed_person(
                person_id, r["display_name"], r["organization"],
                r["role"], r["notes"], tags,
            )
    except Exception as exc:  # pragma: no cover — embed is best-effort
        log.debug("re-embed after source removal failed: %s", exc)

    return {"removed_rows": removed_rows, "fields_reverted": reverted}
