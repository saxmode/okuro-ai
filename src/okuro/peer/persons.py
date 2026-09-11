# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Person profiles — communication partner management for adapted agent output.
# index:
#   def _slugify
#   def _embed_person
#   def person_add
#   def person_get
#   def person_list
#   def person_update
#   def person_update_sliders
#   def person_lens
#   def person_match
#   def _parse_person_row
#   def _update_json_list
#   def _update_json_field
#   def _format_person_markdown
#   def _text_search_fallback
# AGENT_HEADER_END -->
"""Person profiles — communication partner management for adapted agent output.

Ported from tm-launcher brain/persons.py.
All Postgres-specific syntax (JSONB, arrays, ILIKE) replaced with SQLite equivalents.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any
from okuro.sense.retrieval import candidate_pool


def _slugify(name: str) -> str:
    """Convert display name to slug: 'Marco Brenner' -> 'marco-brenner'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _embed_person(person_id: str, display_name: str, organization: str = None,
                  role: str = None, notes: str = None, tags: list = None):
    """Generate and store embedding vector for a person (best-effort)."""
    parts = [display_name]
    if organization:
        parts.append(organization)
    if role:
        parts.append(role)
    if tags:
        parts.append(" ".join(tags))
    if notes:
        parts.append(notes[:200])
    text = " | ".join(parts)

    try:
        from okuro.embed.client import embed_one, to_bytes

        vec = embed_one(text)
        vec_bytes = to_bytes(vec)

        from okuro.db import get_db
        db = get_db()
        # Upsert into vec_persons
        db.execute("DELETE FROM vec_persons WHERE id = ?", (person_id,))
        db.execute(
            "INSERT INTO vec_persons (id, embedding) VALUES (?, ?)",
            (person_id, vec_bytes),
        )
        db.conn.commit()
    except Exception:
        pass  # Embedding failure shouldn't block CRUD


# keep the change-feed bounded — only the tail matters for live sync
_EVENTS_KEEP = 500


def emit_person_event(db, person_id: str, kind: str, origin: str = "") -> None:
    """Append a person change to ``persons_events`` (migration 066).

    The cross-process realtime channel for the People page: every write path
    (person_add/person_update here, plus the web update/delete endpoints that
    write directly) calls this so an open canvas live-updates on ANY mutation,
    including agent writes from the separate stdio MCP process. ``origin`` is
    'agent' (MCP) or 'web' so the client can label the update. Does NOT commit —
    the caller's existing commit flushes it in the same transaction.
    """
    db.execute(
        "INSERT INTO persons_events (person_id, kind, origin) VALUES (?, ?, ?)",
        (person_id, kind, origin or ""),
    )
    # prune the change-feed to its tail
    db.execute(
        "DELETE FROM persons_events WHERE seq <= "
        "(SELECT MAX(seq) FROM persons_events) - ?",
        (_EVENTS_KEEP,),
    )


def persons_events_since(seq: int) -> list[dict]:
    """Change-feed rows with ``seq`` greater than the cursor (oldest first)."""
    from okuro.db import get_db

    db = get_db()
    return db.fetchall(
        "SELECT seq, person_id, kind, origin, ts FROM persons_events "
        "WHERE seq > ? ORDER BY seq ASC",
        (int(seq),),
    )


def persons_latest_seq() -> int:
    """Max event seq — the replay cursor for a freshly-opened SSE stream."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT MAX(seq) AS s FROM persons_events")
    return int(row["s"]) if row and row.get("s") is not None else 0


def person_add(display_name: str, organization: str = None, role: str = None,
               relation_to_user: str = None, relation_type: str = "professional",
               communication: dict = None, cognitive: dict = None,
               notes: str = None, contact: dict = None, tags: list = None,
               person_id: str = None, origin: str = "agent") -> str:
    """Add or upsert a person profile.

    Args:
        display_name: Full name.
        organization: Company/org.
        role: Their role/title.
        relation_to_user: Relationship description.
        relation_type: colleague, friend, family, professional, acquaintance, other.
        communication: Communication preferences dict.
        cognitive: Cognitive/accessibility preferences dict.
        notes: Freeform notes.
        contact: Contact info dict.
        tags: List of tags.
        person_id: Custom slug (auto-generated from name if omitted).
    """
    from okuro.db import get_db

    db = get_db()
    pid = person_id or _slugify(display_name)
    comm = json.dumps(communication or {})
    cog = json.dumps(cognitive or {})
    cont = json.dumps(contact or {})
    tag_json = json.dumps(tags or [])

    db.execute(
        """INSERT INTO persons (id, display_name, organization, role, relation_to_user,
                                relation_type, communication, cognitive, notes, contact, tags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (id) DO UPDATE SET
               display_name = excluded.display_name,
               organization = COALESCE(excluded.organization, persons.organization),
               role = COALESCE(excluded.role, persons.role),
               relation_to_user = COALESCE(excluded.relation_to_user, persons.relation_to_user),
               relation_type = excluded.relation_type,
               communication = excluded.communication,
               cognitive = excluded.cognitive,
               notes = COALESCE(excluded.notes, persons.notes),
               contact = excluded.contact,
               tags = excluded.tags,
               updated_at = datetime('now')
        """,
        (pid, display_name, organization, role, relation_to_user,
         relation_type, comm, cog, notes, cont, tag_json),
    )
    emit_person_event(db, pid, "added", origin)
    db.conn.commit()

    _embed_person(pid, display_name, organization, role, notes, tags)
    return f"Person added: **{display_name}** (`{pid}`)"


def person_get(person_id: str, format: str = "markdown") -> str:
    """Get a person's full profile by ID or fuzzy name match."""
    from okuro.db import get_db

    db = get_db()

    # Exact match first
    row = db.fetchone("SELECT * FROM persons WHERE id = ? AND active = 1", (person_id,))
    if not row:
        # Fuzzy match on display_name
        row = db.fetchone(
            "SELECT * FROM persons WHERE LOWER(display_name) LIKE ? AND active = 1",
            (f"%{person_id.lower()}%",),
        )
    if not row:
        return f"Person not found: {person_id}"

    person = _parse_person_row(row)

    if format == "json":
        return json.dumps(person, indent=2, default=str)
    return _format_person_markdown(person)


def person_list(tag: str = None, organization: str = None,
                active_only: bool = True) -> str:
    """List all persons, optionally filtered by tag or org."""
    from okuro.db import get_db

    db = get_db()

    query = "SELECT id, display_name, organization, role, relation_type, tags FROM persons WHERE 1=1"
    params: list = []

    if active_only:
        query += " AND active = 1"
    if organization:
        query += " AND LOWER(organization) LIKE ?"
        params.append(f"%{organization.lower()}%")
    query += " ORDER BY display_name"

    rows = db.fetchall(query, tuple(params))

    if tag:
        # Filter by tag in JSON array (SQLite json_each)
        rows = [r for r in rows if tag in json.loads(r.get("tags", "[]"))]

    if not rows:
        return "No persons found."

    lines = [
        "| ID | Name | Org | Role | Type | Tags |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        tags_list = json.loads(r.get("tags", "[]"))
        lines.append(
            f"| {r['id']} | {r['display_name']} | {r.get('organization') or '-'} "
            f"| {r.get('role') or '-'} | {r.get('relation_type', '-')} "
            f"| {', '.join(tags_list)} |"
        )
    return "\n".join(lines)


def _reject_unvalidated_slider_write(
    col: str, subpath: str, action: str, value: Any
) -> str | None:
    """Refuse a slider write that bypasses person_update_sliders' guards.

    ``person_update`` accepts arbitrary dotted paths and writes them raw,
    so ``person_update(pid, "cognitive.sliders.jargon", 99)`` landed a 99
    on a 1..5 axis with no name check, no range check, no provenance and
    no event — an untyped bypass around a typed surface. The typed path is
    ``person_update_sliders``; this makes the bypass say so.

    Returns a refusal message, or None when the write may proceed.
    """
    if col != "cognitive":
        return None
    segments = subpath.split(".")
    if segments[0] != "sliders":
        return None

    from okuro.peer.cognitive_profile import SLIDER_NAMES

    # cognitive.sliders.<axis> — one axis, must be a known name in 1..5.
    if len(segments) == 2:
        axis = segments[1]
        if axis not in SLIDER_NAMES:
            return (
                f"Refused: `{axis}` is not a slider axis. "
                f"Known axes: {', '.join(SLIDER_NAMES)}."
            )
        if action != "set":
            return f"Refused: sliders support action='set' only, got {action!r}."
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            return (
                f"Refused: slider `{axis}` must be an int in 1..5, got {value!r}. "
                "Use person_update_sliders — it validates, records provenance "
                "and emits a change event."
            )
        return (
            f"Refused: write sliders through person_update_sliders("
            f"person_id, {axis}={value}). The dotted path skips provenance "
            "and the change event, leaving the profile unauditable."
        )

    # cognitive.sliders — wholesale vector replace.
    if len(segments) == 1:
        return (
            "Refused: write sliders through person_update_sliders(person_id, "
            "**axes). Replacing the vector wholesale skips validation, "
            "provenance and the change event."
        )

    return (
        f"Refused: `cognitive.{subpath}` is deeper than the slider model "
        "allows. Use person_update_sliders."
    )


def person_update(person_id: str, field: str, value, action: str = "set",
                  origin: str = "agent") -> str:
    """Update a specific field on a person profile.

    Args:
        person_id: Person slug.
        field: Top-level (display_name, organization, role, notes, etc.)
               or dotted path (communication.style, cognitive.accessibility).
        action: set, append, remove.
        value: The value.
    """
    from okuro.db import get_db

    db = get_db()

    row = db.fetchone("SELECT id FROM persons WHERE id = ?", (person_id,))
    if not row:
        return f"Person not found: {person_id}"

    simple_fields = {
        "display_name", "organization", "role", "relation_to_user",
        "relation_type", "notes",
    }

    if field in simple_fields:
        db.execute(
            f"UPDATE persons SET {field} = ?, updated_at = datetime('now') WHERE id = ?",
            (value, person_id),
        )
    elif field == "active":
        db.execute(
            "UPDATE persons SET active = ?, updated_at = datetime('now') WHERE id = ?",
            (int(value), person_id),
        )
    elif field == "tags":
        _update_json_list(db, person_id, "tags", action, value)
    elif "." in field:
        parts = field.split(".", 1)
        col = parts[0]
        subpath = parts[1]
        if col not in ("communication", "cognitive", "contact"):
            return f"Unknown JSON column: {col}. Use communication, cognitive, or contact."
        refusal = _reject_unvalidated_slider_write(col, subpath, action, value)
        if refusal:
            return refusal
        _update_json_field(db, person_id, col, subpath, action, value)
    else:
        return f"Unknown field: {field}. Use a top-level field or dotted path."

    emit_person_event(db, person_id, "updated", origin)
    db.conn.commit()

    # Re-embed after update
    r = db.fetchone(
        "SELECT display_name, organization, role, notes, tags FROM persons WHERE id = ?",
        (person_id,),
    )
    if r:
        _embed_person(person_id, r["display_name"], r["organization"],
                      r["role"], r["notes"], json.loads(r.get("tags", "[]")))

    return f"Updated `{person_id}`.{field} ({action})"


def person_update_sliders(person_id: str, lens_key: str | None = None, **sliders) -> str:
    """Patch the 17-axis cognitive slider vector on a person.

    Mirrors the HTTP endpoint ``PUT /api/people/{person_id}/sliders``
    (see ``src/okuro/orchestrator/api/people.py::update_person_sliders``)
    so agents can adjust recipient sliders via MCP without going through
    the web UI.

    Partial-update semantics: only the axes you pass are written; the
    rest preserve their stored value. Each axis must be an int in 1..5.
    Unknown axes raise ``ValueError`` (typo guard — silent drops would
    create the impression of a successful update).

    Args:
        person_id: Person slug (must exist in ``persons``).
        **sliders: Subset of the 17 canonical axes — the full set is
            ``cognitive_profile.SLIDER_NAMES``, which is the single source
            of truth. Each value an int in 1..5.

    Returns:
        Confirmation string ``"Sliders updated for `<id>`: <keys>"``,
        or ``"Person not found: <id>"`` when no row matches (does not
        raise — keeps the agent path forgiving for stale ids).

    Raises:
        ValueError: any axis name is not in ``SLIDER_NAMES`` or any
            value is not an int in 1..5.
    """
    from okuro.db import get_db
    from okuro.peer.cognitive_profile import SLIDER_NAMES

    if not sliders:
        return f"Sliders updated for `{person_id}`: (no changes)"

    allowed = set(SLIDER_NAMES)
    unknown = sorted(k for k in sliders if k not in allowed)
    if unknown:
        raise ValueError(
            f"Unknown slider axis: {', '.join(unknown)}. "
            f"Allowed: {', '.join(SLIDER_NAMES)}."
        )

    cleaned: dict[str, int] = {}
    for axis, value in sliders.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"Slider {axis} must be an int in 1..5, got {value!r}."
            )
        if value < 1 or value > 5:
            raise ValueError(
                f"Slider {axis} out of range: {value}. Must be in 1..5."
            )
        cleaned[axis] = value

    if not cleaned:
        return f"Sliders updated for `{person_id}`: (no changes)"

    db = get_db()

    row = db.fetchone("SELECT cognitive FROM persons WHERE id = ?", (person_id,))
    if not row:
        return f"Person not found: {person_id}"

    cog_raw = row["cognitive"]
    if isinstance(cog_raw, str) and cog_raw:
        try:
            cog = json.loads(cog_raw)
        except json.JSONDecodeError:
            cog = {}
    else:
        cog = cog_raw or {}
    if not isinstance(cog, dict):
        cog = {}

    # A lens_key writes a CONTEXT-SCOPED set instead of the base vector:
    # the same person wants depth from engineering and altitude on a board
    # seat, and flattening those into one vector loses both.
    if lens_key:
        lenses = cog.get("lenses")
        if not isinstance(lenses, dict):
            lenses = {}
        scoped = lenses.get(lens_key)
        if not isinstance(scoped, dict):
            scoped = {}
        current = scoped
    else:
        current = cog.get("sliders") or {}
        if not isinstance(current, dict):
            current = {}
    # Snapshot BEFORE the merge — this is the only moment the old values
    # still exist. A slider edit was a destructive in-place overwrite with
    # no event and no version, so "what was this axis three months ago" had
    # no answer anywhere in the system.
    previous = dict(current)
    current.update(cleaned)
    if lens_key:
        lenses[lens_key] = current
        cog["lenses"] = lenses
    else:
        cog["sliders"] = current

    # Per-axis provenance (additive, non-breaking). Stored PARALLEL to
    # ``sliders`` so every existing reader (lens, firewall, SlidersPanel) keeps
    # working untouched; new consumers can weight an axis by confidence/source.
    # This is the explicit user-set path → source=user, confidence=1.0.
    # Base vector only. slider_provenance is a flat axis->entry map with no
    # room for a context, so writing a lens value into it would make a
    # board-scoped override look like a change to the person's baseline.
    # A lens write is traced by the ledger and the history instead, both of
    # which carry the field_path that distinguishes them.
    if not lens_key:
        prov = cog.get("slider_provenance")
        if not isinstance(prov, dict):
            prov = {}
        stamp_at = datetime.now(timezone.utc).isoformat()
        for axis in cleaned:
            prov[axis] = {"value": cleaned[axis], "confidence": 1.0,
                          "source": "user", "updated_at": stamp_at}
        cog["slider_provenance"] = prov

    db.execute(
        "UPDATE persons SET cognitive = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(cog), person_id),
    )

    # The ledger must cover the primary write path. It never did: this
    # function wrote slider_provenance inline and skipped person_sources
    # entirely, so okuro ran two parallel provenance systems and the audit
    # trail was blind to the writes it exists to audit.
    #
    # ONE ROW PER CHANGED AXIS (migration 123). Before the axis column
    # existed this had to write the whole merged vector on every patch,
    # because a partial row was replayed wholesale on any later
    # remove_source and would delete every unlisted axis. A per-axis row
    # says "this one axis became this value", which is what a partial
    # update actually is — so the ledger now records the change, not a
    # snapshot of everything around it.
    from okuro.peer.sources import record_applied_value, record_history

    # A LENS WRITE NOW ENTERS person_sources (migration 127).
    #
    # cognitive.lenses is nested one level deeper than any other tracked
    # field — {lens_key: {axis: value}} — so until the ledger had a lens_key
    # column, replaying a lens row would have set
    # cognitive["lenses"] = {axis: value}, flattening away every lens_key.
    # The column supplies the missing coordinate and _rebuild_person_columns
    # groups by it, so a lens edit is now revertible by remove_source like
    # every other write. Fable's v1 asked for ONE ledger covering every write
    # path; this was the last one outside it.
    field_path = "cognitive.lenses" if lens_key else "cognitive.sliders"
    source_ref = (f"person_update_sliders:{lens_key}" if lens_key
                  else "person_update_sliders")

    for axis, value in sorted(cleaned.items()):
        record_applied_value(
            db,
            person_id,
            field_path,
            value,
            source_type="manual",
            source_ref=source_ref,
            confidence=1.0,
            axis=axis,
            lens_key=lens_key,
        )
        # The archive. person_sources says what a SOURCE claimed; this says
        # what the stored profile actually transitioned between, which is a
        # different question and the one an audit asks.
        record_history(
            db,
            person_id,
            field_path,
            old_value=previous.get(axis),
            new_value=value,
            axis=axis,
            source=source_ref,
        )

    # The People page live-updates off persons_events. person_add and
    # person_update emit; this path never did, so a slider change was
    # invisible until a manual reload.
    emit_person_event(db, person_id, "updated", "person_update_sliders")

    db.conn.commit()

    return f"Sliders updated for `{person_id}`: {', '.join(sorted(cleaned))}"


def questionnaire_timings(person_id: str | None = None) -> list[dict]:
    """How long each survey actually took — the reader for migration 125.

    P3.1's acceptance is stated as a duration ("each completion under four
    minutes"), so the duration has to be obtainable. Without this,
    ``started_at`` would be a fourth timestamp on a table nothing reads —
    the declared-but-unread shape this module already shipped twelve of.

    ``completion_seconds`` is ``answered_at - started_at``, and is **None**
    whenever either end is missing rather than a fabricated zero. A row
    written before migration 125 has ``started_at IS NULL``: that means "not
    measured", never "started when it was sent". Substituting ``sent_at``
    would silently report a 3-minute form as a 3-day one.

    N is 2-3 by design, so this returns every row rather than an aggregate —
    a median over three points would be theatre.
    """
    from okuro.db import get_db

    db = get_db()
    sql = (
        "SELECT person_id, token, status, sent_at, started_at, answered_at, "
        "applied_at FROM person_questionnaires"
    )
    params: tuple = ()
    if person_id:
        sql += " WHERE person_id = ?"
        params = (person_id,)
    sql += " ORDER BY sent_at DESC"

    out: list[dict] = []
    for row in db.fetchall(sql, params):
        r = dict(row)
        secs: int | None = None
        start, end = r.get("started_at"), r.get("answered_at")
        if start and end:
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                delta = datetime.strptime(str(end)[:19], fmt) - datetime.strptime(
                    str(start)[:19], fmt
                )
                secs = int(delta.total_seconds())
            except (ValueError, TypeError):
                secs = None
        r["completion_seconds"] = secs
        # Tri-state on purpose: None is "not measurable", not "failed".
        r["under_four_minutes"] = None if secs is None else secs < 240
        out.append(r)
    return out


def person_lens(person_id: str, context: str = None) -> str:
    """Generate a communication lens — a prompt fragment for adapting output to this person.

    PII firewall: the output is the ANONYMIZED cognitive projection only —
    no name, organization, contact, or free-text relation. The calling
    agent already knows the person id from its own call site, so the
    return value never repeats identifying information that would land
    in a downstream LLM prompt.

    Args:
        person_id: Person slug or display name. Resolved server-side; not
            rendered into the lens output.
        context: Optional context (e.g. "email about Q2 numbers"). Free
            text — caller is responsible for keeping it PII-free.
    """
    from okuro.db import get_db
    from okuro.peer.cognitive_profile import cognitive_profile_for_llm

    db = get_db()

    # Resolve the slug (caller may have passed a display-name fragment) so
    # the firewall can read the row. The resolved id is NOT echoed back —
    # it's just used to fetch the profile.
    row = db.fetchone(
        """SELECT id FROM persons
           WHERE (id = ? OR LOWER(display_name) LIKE ?) AND active = 1
           LIMIT 1""",
        (person_id, f"%{person_id.lower()}%"),
    )
    if not row:
        return f"Person not found: {person_id}"

    cog_proj = cognitive_profile_for_llm(row["id"])
    if not cog_proj:
        return "## Communication Lens (anonymous recipient)\n\n*No cognitive profile yet — translate using sender defaults.*"

    lines = ["## Communication Lens (anonymous recipient)"]
    archetype = cog_proj.get("relation_archetype")
    if archetype and archetype != "unknown":
        lines.append(f"*relation archetype: {archetype}*")
    lines.append("")
    lines.append("**Adapt your output as follows:**")

    if cog_proj.get("formality_baseline"):
        lines.append(f"- **Formality:** {cog_proj['formality_baseline']}")
    if cog_proj.get("jargon_tolerance"):
        lines.append(f"- **Jargon:** {cog_proj['jargon_tolerance']}")
    if cog_proj.get("decision_style"):
        lines.append(f"- **Decision making:** {cog_proj['decision_style']}")
    if cog_proj.get("attention_span"):
        lines.append(f"- **Attention span:** {cog_proj['attention_span']}")
    if cog_proj.get("learning_style"):
        lines.append(f"- **Learning style:** {cog_proj['learning_style']}")

    fmt_prefs = cog_proj.get("format_preferences") or []
    if fmt_prefs:
        lines.append(f"- **Format preferences:** {', '.join(map(str, fmt_prefs))}")

    avoid = cog_proj.get("avoid_structural") or []
    if avoid:
        lines.append(f"- **Avoid structurally:** {', '.join(map(str, avoid))}")

    sliders = cog_proj.get("sliders") or {}
    if sliders:
        from okuro.peer.cognitive_profile import format_sliders_for_prompt

        lines.append(f"- **Sliders:** {format_sliders_for_prompt(cog_proj)}")
        # Flag low-confidence axes (role-seeded priors / unconfirmed) so the
        # agent treats them as assumptions, not observed preferences.
        conf = cog_proj.get("slider_confidence") or {}
        low = [k for k, c in conf.items() if isinstance(c, (int, float)) and c < 0.6]
        if low:
            lines.append(
                f"- **Unconfirmed axes (priors, not observed):** {', '.join(low)} "
                "— treat as assumptions; confirm before relying."
            )

    topics = cog_proj.get("topic_interests") or []
    if topics:
        topic_labels = [t.get("topic", "") for t in topics if isinstance(t, dict) and t.get("topic")]
        if topic_labels:
            lines.append(f"- **Topics they care about:** {', '.join(topic_labels[:5])}")

    if context:
        lines.append("")
        lines.append(f"*Applying to:* {context}")
        ctx_lower = context.lower()
        if any(w in ctx_lower for w in ("email", "mail", "message")):
            if cog_proj.get("attention_span") == "short":
                lines.append("- Keep email body to 3-5 bullet points max")
        elif any(w in ctx_lower for w in ("presentation", "slides", "deck")):
            if cog_proj.get("attention_span") == "short":
                lines.append("- One idea per slide, strong visual hierarchy")

    return "\n".join(lines)


def person_match(query: str, limit: int = 3) -> str:
    """Find persons by semantic similarity to a query.

    Falls back to text search if embeddings aren't available.
    """
    from okuro.db import get_db

    db = get_db()

    try:
        from okuro.embed.client import embed_query, to_bytes

        vec = embed_query(query)
        vec_bytes = to_bytes(vec)
        matches = db.vec_search(
            "vec_persons", vec_bytes, limit=candidate_pool(limit, scoped=True)
        )

        if not matches:
            return _text_search_fallback(db, query, limit)

        ids = [m["id"] for m in matches]
        distances = {m["id"]: m["distance"] for m in matches}

        placeholders = ", ".join("?" * len(ids))
        rows = db.fetchall(
            f"SELECT id, display_name, organization, role FROM persons "
            f"WHERE id IN ({placeholders}) AND active = 1",
            tuple(ids),
        )

        if not rows:
            return _text_search_fallback(db, query, limit)

        lines = [
            "| Match | Name | Org | Role | Similarity |",
            "|---|---|---|---|---|",
        ]
        for i, r in enumerate(rows, 1):
            sim = 1 - distances.get(r["id"], 1)
            lines.append(
                f"| {i} | {r['display_name']} (`{r['id']}`) "
                f"| {r.get('organization') or '-'} | {r.get('role') or '-'} "
                f"| {sim:.2f} |"
            )
        return "\n".join(lines)

    except Exception:
        return _text_search_fallback(db, query, limit)


def person_merge(keep_id: str, merge_id: str) -> str:
    """Merge ``merge_id`` into ``keep_id`` — for duplicate rows (e.g. the same
    person entered twice under name variants like Robin / Robyn).

    Re-points all relations (affiliations, connections, engagements) to
    ``keep_id``, unions tags, appends the merged person's notes, fills empty
    cognitive/communication from the merged row, then deactivates ``merge_id``
    (active=0 — soft, recoverable; never a hard delete). Re-embeds ``keep_id``.

    Collision handling:
      - affiliations: merged rows are demoted to is_primary=0 before re-point
        so the partial-unique primary index can't reject the move.
      - engagements: a merged engagement for a project ``keep_id`` already
        covers is dropped (keep wins); the rest re-point.
    """
    from okuro.db import get_db

    db = get_db()
    if keep_id == merge_id:
        return "keep_id and merge_id are the same — nothing to merge."
    keep = db.fetchone("SELECT * FROM persons WHERE id = ?", (keep_id,))
    merge = db.fetchone("SELECT * FROM persons WHERE id = ?", (merge_id,))
    if not keep:
        return f"Person not found (keep): {keep_id}"
    if not merge:
        return f"Person not found (merge): {merge_id}"

    moved = {"affiliations": 0, "connections": 0, "engagements": 0, "dropped_engagements": 0}
    with db.write():
        # affiliations — demote merged primaries, then re-point.
        db.conn.execute(
            "UPDATE affiliations SET is_primary = 0 WHERE person_id = ?", (merge_id,)
        )
        cur = db.conn.execute(
            "UPDATE affiliations SET person_id = ? WHERE person_id = ?",
            (keep_id, merge_id),
        )
        moved["affiliations"] = cur.rowcount

        # connections — straight re-point.
        cur = db.conn.execute(
            "UPDATE connections SET person_id = ? WHERE person_id = ?",
            (keep_id, merge_id),
        )
        moved["connections"] = cur.rowcount

        # engagements — drop merged rows whose project keep_id already covers.
        cur = db.conn.execute(
            """DELETE FROM engagements WHERE person_id = ? AND project_slug IN
               (SELECT project_slug FROM engagements WHERE person_id = ?)""",
            (merge_id, keep_id),
        )
        moved["dropped_engagements"] = cur.rowcount
        cur = db.conn.execute(
            "UPDATE engagements SET person_id = ? WHERE person_id = ?",
            (keep_id, merge_id),
        )
        moved["engagements"] = cur.rowcount

        # tags — union.
        keep_tags = json.loads(keep["tags"] or "[]")
        merge_tags = json.loads(merge["tags"] or "[]")
        union_tags = keep_tags + [t for t in merge_tags if t not in keep_tags]

        # notes — append merged notes under a marker.
        notes = keep["notes"] or ""
        if merge["notes"]:
            sep = "\n\n" if notes else ""
            notes = f"{notes}{sep}[merged from {merge['display_name']}] {merge['notes']}"

        # fill empty identity/profile fields from the merged row.
        org = keep["organization"] or merge["organization"]
        role = keep["role"] or merge["role"]
        rel = keep["relation_to_user"] or merge["relation_to_user"]

        def _pick_json(a, b):
            try:
                av = json.loads(a or "{}")
            except (json.JSONDecodeError, TypeError):
                av = {}
            return a if av else (b or "{}")

        cognitive = _pick_json(keep["cognitive"], merge["cognitive"])
        communication = _pick_json(keep["communication"], merge["communication"])
        contact = _pick_json(keep["contact"], merge["contact"])

        db.conn.execute(
            """UPDATE persons SET organization = ?, role = ?, relation_to_user = ?,
                   cognitive = ?, communication = ?, contact = ?, notes = ?,
                   tags = ?, updated_at = datetime('now') WHERE id = ?""",
            (org, role, rel, cognitive, communication, contact, notes,
             json.dumps(union_tags), keep_id),
        )
        db.conn.execute(
            "UPDATE persons SET active = 0, updated_at = datetime('now') WHERE id = ?",
            (merge_id,),
        )

    # Re-embed keep with merged text.
    r = db.fetchone(
        "SELECT display_name, organization, role, notes, tags FROM persons WHERE id = ?",
        (keep_id,),
    )
    if r:
        _embed_person(keep_id, r["display_name"], r["organization"],
                      r["role"], r["notes"], json.loads(r.get("tags", "[]")))

    return (
        f"Merged `{merge_id}` → `{keep_id}` (deactivated source). "
        f"Re-pointed: {moved['affiliations']} affiliations, "
        f"{moved['connections']} connections, {moved['engagements']} engagements"
        + (f" ({moved['dropped_engagements']} dropped as duplicates)"
           if moved['dropped_engagements'] else "")
        + "."
    )


# --- Helpers ---


def _parse_person_row(row: dict) -> dict:
    """Parse JSON text columns into dicts."""
    person = dict(row)
    for col in ("communication", "cognitive", "contact", "tags"):
        val = person.get(col)
        if isinstance(val, str):
            try:
                person[col] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return person


def _update_json_list(db, person_id: str, column: str, action: str, value):
    """Update a JSON array column (tags)."""
    row = db.fetchone(f"SELECT {column} FROM persons WHERE id = ?", (person_id,))
    current = json.loads(row[column]) if row and row[column] else []

    if action == "set":
        current = value if isinstance(value, list) else [value]
    elif action == "append":
        if value not in current:
            current.append(value)
    elif action == "remove":
        current = [x for x in current if x != value]

    db.execute(
        f"UPDATE persons SET {column} = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(current), person_id),
    )


def _update_json_field(db, person_id: str, column: str, subpath: str,
                       action: str, value):
    """Update a nested field inside a JSON column."""
    row = db.fetchone(f"SELECT {column} FROM persons WHERE id = ?", (person_id,))
    data = json.loads(row[column]) if row and row[column] else {}

    keys = subpath.split(".")
    target = data
    for key in keys[:-1]:
        if key not in target:
            target[key] = {}
        target = target[key]

    final_key = keys[-1]

    if action == "set":
        target[final_key] = value
    elif action == "append":
        if final_key not in target or not isinstance(target[final_key], list):
            target[final_key] = []
        if value not in target[final_key]:
            target[final_key].append(value)
    elif action == "remove":
        if final_key in target and isinstance(target[final_key], list):
            target[final_key] = [x for x in target[final_key] if x != value]

    db.execute(
        f"UPDATE persons SET {column} = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(data), person_id),
    )


def _format_person_markdown(p: dict) -> str:
    """Format person dict as readable markdown."""
    lines = [f"# {p['display_name']}"]

    if p.get("organization") or p.get("role"):
        parts = [x for x in [p.get("role"), p.get("organization")] if x]
        lines.append(f"**{' — '.join(parts)}**")

    if p.get("relation_to_user"):
        lines.append(f"*Relationship:* {p['relation_to_user']} ({p.get('relation_type', 'professional')})")

    lines.append("")

    comm = p.get("communication", {})
    if isinstance(comm, dict) and comm:
        lines.append("## Communication")
        for key in ("style", "language", "formality", "response_length", "decision_style"):
            if comm.get(key):
                lines.append(f"- **{key.replace('_', ' ').title()}:** {comm[key]}")
        if comm.get("format_preferences"):
            lines.append(f"- **Prefers:** {', '.join(comm['format_preferences'])}")
        if comm.get("avoid"):
            lines.append(f"- **Avoid:** {', '.join(comm['avoid'])}")
        lines.append("")

    cog = p.get("cognitive", {})
    if isinstance(cog, dict) and cog:
        lines.append("## Cognitive / Accessibility")
        if cog.get("accessibility"):
            lines.append(f"- **Accessibility:** {', '.join(cog['accessibility'])}")
        if cog.get("learning_style"):
            lines.append(f"- **Learning style:** {cog['learning_style']}")
        if cog.get("attention_span"):
            lines.append(f"- **Attention span:** {cog['attention_span']}")
        if cog.get("expertise_level"):
            for domain, level in cog["expertise_level"].items():
                lines.append(f"- **{domain}:** {level}")
        if cog.get("pet_peeves"):
            lines.append(f"- **Pet peeves:** {', '.join(cog['pet_peeves'])}")
        lines.append("")

    contact = p.get("contact", {})
    if isinstance(contact, dict) and contact:
        lines.append("## Contact")
        for k, v in contact.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    if p.get("notes"):
        lines.append("## Notes")
        lines.append(p["notes"])
        lines.append("")

    tags = p.get("tags", [])
    if isinstance(tags, list) and tags:
        lines.append(f"*Tags:* {', '.join(tags)}")

    return "\n".join(lines)


def _text_search_fallback(db, query: str, limit: int) -> str:
    """Fallback: LIKE-based search on name, org, role, notes."""
    pattern = f"%{query.lower()}%"
    rows = db.fetchall(
        """SELECT id, display_name, organization, role
           FROM persons
           WHERE active = 1 AND (
               LOWER(display_name) LIKE ? OR
               LOWER(COALESCE(organization, '')) LIKE ? OR
               LOWER(COALESCE(role, '')) LIKE ? OR
               LOWER(COALESCE(notes, '')) LIKE ?
           )
           LIMIT ?""",
        (pattern, pattern, pattern, pattern, limit),
    )
    if not rows:
        return "No matching persons found."

    lines = [
        "| Match | Name | Org | Role |",
        "|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['display_name']} (`{r['id']}`) "
            f"| {r.get('organization') or '-'} | {r.get('role') or '-'} |"
        )
    return "\n".join(lines)
