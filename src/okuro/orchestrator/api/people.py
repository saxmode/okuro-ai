# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: People API — list/get/add/update/delete/match/lens/translate/graph + graph layout + role-preset catalog.
# index:
#   imports
#   router
#   models
#   def _require_localhost_pe
#   def _row_to_dict
#   def _fetch_person
#   def list_people
#   def get_person
#   def create_person
#   def update_person
#   def delete_person
#   def match_people
#   def person_lens_endpoint
#   def translate_for_person
#   def list_role_presets
#   def people_graph
#   def _layout_saved_at
#   def get_people_graph_layout
#   def put_people_graph_layout
# AGENT_HEADER_END -->
"""People API — thin HTTP wrapper over okuro.peer.persons + translate + graph.

Gives the web UI a surface that mirrors the MCP person tools:

- GET  /api/people                list of {id, display_name, ...}
- GET  /api/people/{id}           full record (JSON dict)
- POST /api/people                create (body = partial record, requires display_name)
- PUT  /api/people/{id}           update whole record (localhost-only)
- DELETE /api/people/{id}         soft-delete = set active=0 (localhost-only)
- POST /api/people/match          body {query, limit?} → [{id, display_name, ...}]
- POST /api/people/{id}/lens      body {context?}      → {markdown: str}
- POST /api/people/{id}/translate body {source, context?, provider?} → translation result + log_id
- GET  /api/people/presets        list of role-based starter presets from roles/catalog
- GET  /api/people/graph          {me, nodes, edges} for the user-center graph view
- GET  /api/people/layout         {positions, groups, settings, saved_at} — stored arrangement
- PUT  /api/people/layout         replace the whole arrangement (localhost-only)

The on-disk schema uses JSON-text columns for ``communication``, ``cognitive``,
``contact`` and ``tags``. This router parses those on read and serialises them
on write, so the frontend never has to stringify them itself.

Mutating endpoints (POST/PUT/DELETE/translate) are localhost-only, matching the
pattern used by ``/api/keyring``, ``/api/cortex`` and ``/api/services``. Read
endpoints are unauthed — same data is already visible to any MCP agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.people")

router = APIRouter(prefix="/api/people", tags=["people"])


# ── Helpers ──────────────────────────────────────────────────────────


def _require_localhost_pe(request: Request) -> None:
    """Reuse main's loopback guard — same pattern as keyring/cortex/services."""
    from okuro.orchestrator.api.main import _require_loopback

    _require_loopback(request)


_JSON_COLS = ("communication", "cognitive", "contact", "tags")


def _row_to_dict(row: Any) -> dict:
    """Parse a persons row (sqlite Row / dict) to a plain JSON-ready dict."""
    person = dict(row)
    for col in _JSON_COLS:
        val = person.get(col)
        if isinstance(val, str):
            try:
                person[col] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                # Leave the raw string rather than crashing — frontend will show it.
                pass
    # Normalise booleanish active column to 0/1 int (SQLite already does this).
    if "active" in person and person["active"] is not None:
        person["active"] = int(person["active"])
    return person


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _fetch_person(person_id: str) -> dict:
    """Look up a person by id (exact). Returns parsed dict or raises 404."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM persons WHERE id = ?", (person_id,))
    if not row:
        raise HTTPException(404, f"Person not found: {person_id}")
    return _row_to_dict(row)


# ── Models ───────────────────────────────────────────────────────────


class PersonIn(BaseModel):
    """Create / update payload. All fields optional except display_name on create."""

    display_name: Optional[str] = None
    organization: Optional[str] = None
    role: Optional[str] = None
    relation_to_user: Optional[str] = None
    relation_type: Optional[str] = None  # colleague|friend|family|professional|acquaintance|other
    communication: Optional[dict] = None
    cognitive: Optional[dict] = None
    contact: Optional[dict] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None
    active: Optional[bool] = None


class MatchRequest(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1, le=25)


class LensRequest(BaseModel):
    context: Optional[str] = None


class LensResponse(BaseModel):
    person_id: str
    markdown: str


class TranslateRequest(BaseModel):
    source: str = Field(..., description="Raw text the user wants translated.")
    context: Optional[str] = None
    provider: Optional[str] = None


class ApplyPresetRequest(BaseModel):
    role_query: str = Field(..., min_length=1, max_length=120)


class IngestTextRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source_type: str = Field(default="notes")
    source_ref: Optional[str] = None


class RemoveSourceRequest(BaseModel):
    source_type: str = Field(..., min_length=1)
    source_ref: Optional[str] = None


class QuestionnaireMintRequest(BaseModel):
    role_hint: Optional[str] = None
    expires_hours: int = Field(default=72, ge=1, le=720)
    # Structured (default) = the redesigned slider/swipe survey scored
    # deterministically. False = legacy free-text questions parsed by the LLM.
    structured: bool = True
    # The language the RECIPIENT will read. Chosen by the sender at mint
    # time and stored with the token: asking the recipient to pick would
    # defeat the point of profiling how they prefer to be communicated with.
    lang: str = "en"
    # Send in a translation nobody has reviewed, knowingly. Explicit and
    # per-call so it appears in the request next to the person it affects.
    allow_unreviewed: bool = False


class SlidersIn(BaseModel):
    """Mode-B input payload. Every axis is a 1–5 int.

    All fields optional so a partial save (single-slider drag) only
    overwrites the dragged axis. Missing axes preserve the stored value
    on the server side.
    """

    information_depth: Optional[int] = Field(default=None, ge=1, le=5)
    format: Optional[int] = Field(default=None, ge=1, le=5)
    decision_framing: Optional[int] = Field(default=None, ge=1, le=5)
    time_horizon: Optional[int] = Field(default=None, ge=1, le=5)
    risk_framing: Optional[int] = Field(default=None, ge=1, le=5)
    jargon: Optional[int] = Field(default=None, ge=1, le=5)
    lead_with: Optional[int] = Field(default=None, ge=1, le=5)
    pace: Optional[int] = Field(default=None, ge=1, le=5)


# ── Graph layout models ──────────────────────────────────────────────
#
# These mirror the client types in
# web/frontend/src/components/people/graph-storage.ts FIELD FOR FIELD,
# camelCase included. The client PUTs exactly the object it holds and
# reads back exactly what it stores, so no mapping layer can drift.


class GraphPoint(BaseModel):
    x: float
    y: float


class GraphGroupIn(BaseModel):
    """Mirrors the client `Group` type.

    center/radius/geomVersion are optional because groups stored before
    geometry existed are migrated client-side on first read; the server
    must round-trip the absence rather than inventing values, or that
    backfill can never stamp them as done.
    """

    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., max_length=200)
    memberIds: list[str] = Field(default_factory=list)
    center: Optional[GraphPoint] = None
    radius: Optional[float] = None
    geomVersion: Optional[int] = None


class GraphSettingsIn(BaseModel):
    """Graph-view preferences. Closed enum, mirroring CONNECTION_STYLES."""

    connectionStyle: Optional[
        Literal["straight", "bezier", "simple-bezier", "step", "arc"]
    ] = None


class GraphLayoutIn(BaseModel):
    """Whole-arrangement replacement payload.

    Every field is a COMPLETE collection, never a delta: the client only
    ever holds and writes full maps (PeopleGraph.tsx rebuilds the entire
    positions record on each drag-stop and spring-rest tick), so a partial
    merge would have no well-defined meaning.
    """

    positions: dict[str, GraphPoint] = Field(default_factory=dict)
    groups: list[GraphGroupIn] = Field(default_factory=list)
    settings: GraphSettingsIn = Field(default_factory=GraphSettingsIn)


# ── Endpoints ────────────────────────────────────────────────────────
#
# NOTE on declaration order: FastAPI matches in file order. Static paths
# (`/graph`, `/presets`, `/match`) MUST appear before the `/{person_id}`
# parameterised routes so the static words don't get captured as an id.


@router.get("")
def list_people(include_inactive: bool = False) -> dict:
    """List every person (id + summary columns).

    Parsed ``tags`` returned as a list. Full record is available via ``/{id}``.
    """
    from okuro.db import get_db

    db = get_db()
    sql = (
        "SELECT id, display_name, organization, role, relation_to_user, "
        "relation_type, tags, active, updated_at FROM persons"
    )
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY display_name COLLATE NOCASE"
    rows = db.fetchall(sql)
    people = [_row_to_dict(r) for r in rows]
    return {"people": people}


@router.get("/events")
async def people_events(request: Request, since: int | None = None):
    """SSE change-feed. Streams `added`/`updated`/`deleted` events as persons
    mutate — including writes from the separate stdio MCP process (agents).

    `since` replays from that seq; omit to receive only future events. Declared
    BEFORE the /{person_id} route so it is not shadowed as an id of "events".
    Unauthed, matching the other people read endpoints.
    """
    from okuro.peer.persons import persons_events_since, persons_latest_seq

    start = since if since is not None else await asyncio.to_thread(persons_latest_seq)

    async def gen():
        cursor = start
        yield ": connected\n\n"
        ticks = 0
        while True:
            if await request.is_disconnected():
                break
            rows = await asyncio.to_thread(persons_events_since, cursor)
            for r in rows:
                cursor = r["seq"]
                yield f"event: {r['kind']}\ndata: {json.dumps(r)}\n\n"
            ticks += 1
            if ticks % 20 == 0:  # heartbeat ~ every 15s (20 * 0.75s)
                yield ": ping\n\n"
            await asyncio.sleep(0.75)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/graph")
def people_graph() -> dict:
    """Return the user-center graph: {me, nodes, edges}.

    - me.profile: communication + cognitive shape from user_profile
    - nodes: all active persons with compact profile fields
    - edges: one per person, stats aggregated from translation_log
    """
    from okuro.db import get_db
    from okuro.peer.translate import translation_stats, translation_intent

    db = get_db()

    try:
        from okuro.yu.profile import get_profile_raw

        profile = get_profile_raw() or {}
    except Exception:
        profile = {}
    identity = profile.get("identity") or {}
    me = {
        "id": "me",
        "display_name": identity.get("name") or "You",
        "role": identity.get("role") or identity.get("profession") or "",
        "communication": profile.get("communication") or {},
        "cognitive": profile.get("cognitive_style") or {},
    }

    rows = db.fetchall(
        "SELECT id, display_name, organization, role, relation_to_user, relation_type, "
        "communication, cognitive, tags FROM persons WHERE active = 1 "
        "ORDER BY display_name COLLATE NOCASE"
    )
    nodes: list[dict] = []
    for r in rows:
        node = _row_to_dict(r)
        nodes.append(
            {
                "id": node["id"],
                "display_name": node["display_name"],
                "organization": node.get("organization"),
                "role": node.get("role"),
                "relation_to_user": node.get("relation_to_user"),
                "relation_type": node.get("relation_type"),
                "communication": node.get("communication") or {},
                "cognitive": node.get("cognitive") or {},
                "tags": node.get("tags") or [],
            }
        )

    stats = translation_stats()
    # Intent is computed from the raw user_profile (includes communication +
    # cognitive_style), not the compacted `me` above — the diff needs all the
    # sender fields to see the deltas clearly.
    edges = [
        {
            "source": "me",
            "target": n["id"],
            "translation_count": stats.get(n["id"], {}).get("count", 0),
            "last_translated_at": stats.get(n["id"], {}).get("last_at"),
            "intent": translation_intent(profile, n),
        }
        for n in nodes
    ]

    return {"me": me, "nodes": nodes, "edges": edges}


# ── Graph layout ─────────────────────────────────────────────────────
#
# The People-graph arrangement used to live only in browser localStorage,
# so every new browser or machine opened to the default layout. These two
# routes are its server-side home.
#
# GROUPS ARE target_groups (migration 110). A cluster drawn here is the same
# entity the prism generator and the handover recipient picker consume — draw
# a board group on the canvas and it is immediately available as an audience
# lens, with resolve_audience merging its members' cognitive profiles. Only the
# GEOMETRY is graph-private, in people_graph_group_geometry; a target group is
# on the canvas exactly when it has a row there. Contract:
#
#   GET  /api/people/layout  -> {positions, groups, settings, saved_at}
#   PUT  /api/people/layout  <- the same shape (minus saved_at)
#
# `saved_at` is the hydration discriminator and the reason this is not a
# bare GET-returns-empty-dict. NULL means the server has NEVER been
# written, which the client answers by seeding the server from whatever is
# in its localStorage. Non-NULL means the server wins, even if a
# collection inside is empty — otherwise "user deleted their last group on
# laptop A" would read identically to "server never written" and laptop B
# would resurrect its stale local groups. It is derived from MAX(updated_at)
# rather than a sentinel row because every PUT writes at least one settings
# row (the client's connectionStyle always has a value, defaulted to
# "bezier" by readSettings), so the derivation can never be spuriously NULL.


def _layout_saved_at(db: Any) -> str | None:
    """Newest write across the three layout tables, or None if never written."""
    row = db.fetchone(
        "SELECT MAX(ts) AS saved_at FROM ("
        "  SELECT MAX(updated_at) AS ts FROM people_graph_positions"
        "  UNION ALL SELECT MAX(updated_at) FROM people_graph_group_geometry"
        "  UNION ALL SELECT MAX(updated_at) FROM people_graph_settings"
        ")"
    )
    return (row or {}).get("saved_at")


@router.get("/layout")
def get_people_graph_layout() -> dict:
    """Return the stored graph arrangement.

    Unauthed like the other read routes — same data any MCP agent can see,
    and it is UI geometry, not profile content.
    """
    from okuro.db import get_db

    db = get_db()

    positions = {
        r["node_id"]: {"x": r["x"], "y": r["y"]}
        for r in db.fetchall("SELECT node_id, x, y FROM people_graph_positions")
    }

    # Only groups WITH a geometry row are on the graph. The seeded standard
    # templates (board, engineers, …) and any MCP-created audience are real
    # target_groups but have no place on the canvas, and must not appear here
    # as bubbles the user never drew.
    from okuro.peer.target_groups import _member_ids

    groups: list[dict] = []
    for r in db.fetchall(
        "SELECT g.id, g.name, g.company_id, g.role_class, "
        "       gm.center_x, gm.center_y, gm.radius, gm.geom_version "
        "FROM people_graph_group_geometry gm "
        "JOIN target_groups g ON g.id = gm.group_id "
        "WHERE g.active = 1 "
        "ORDER BY gm.sort_order, g.id"
    ):
        group: dict[str, Any] = {
            "id": r["id"],
            "name": r["name"],
            # Resolved membership, not the raw override rows: an audience
            # defined by company + role_class stores NO explicit members, so
            # reading target_group_members directly would draw a company
            # audience on the canvas as an empty circle. _member_ids is the
            # same resolution the prism generator and handover picker use —
            # affiliation-derived ∪ explicit — so the bubble shows the
            # audience the rest of okuro means by that name.
            "memberIds": _member_ids(db, r),
        }
        # Emit center/radius/geomVersion only when stored. Sending nulls
        # would make the client's `g.center && g.radius != null` geometry
        # check pass a shape it must actually treat as missing.
        if r["center_x"] is not None and r["center_y"] is not None:
            group["center"] = {"x": r["center_x"], "y": r["center_y"]}
        if r["radius"] is not None:
            group["radius"] = r["radius"]
        if r["geom_version"] is not None:
            group["geomVersion"] = r["geom_version"]
        groups.append(group)

    settings: dict[str, Any] = {}
    for r in db.fetchall("SELECT key, value FROM people_graph_settings"):
        if r["key"] == "connection_style":
            settings["connectionStyle"] = r["value"]

    return {
        "positions": positions,
        "groups": groups,
        "settings": settings,
        "saved_at": _layout_saved_at(db),
    }


@router.put("/layout")
def put_people_graph_layout(payload: GraphLayoutIn, request: Request) -> dict:
    """Replace the whole stored arrangement.

    Localhost-only, matching every other mutating route in this module.
    Reachable from other machines in practice because the SPA is served
    through the same-host Caddy reverse proxy, which connects to the
    backend over loopback (uvicorn runs with proxy_headers=False, so the
    guard sees the proxy's address, not the browser's). Hitting the
    backend port directly from a LAN address still 403s.

    One transaction. Positions and settings are a straight whole-collection
    replace — the payload is authoritative, so anything absent was removed by
    the user. Groups are NOT, because they are target_groups shared with the
    prism generator and the handover picker: the replace is scoped to groups
    the graph owns (those with a geometry row), and removal degrades to
    "leaves the canvas" for any group carrying real audience semantics.
    """
    _require_localhost_pe(request)

    from okuro.db import get_db

    db = get_db()

    # NOTE: use the yielded raw connection, never db.execute/db.executemany.
    # SQLiteDB.executemany opens its own `BEGIN IMMEDIATE` via write(), which
    # inside this block raises "cannot start a transaction within a transaction".
    with db.transaction() as conn:
        conn.execute("DELETE FROM people_graph_positions")
        if payload.positions:
            conn.executemany(
                "INSERT INTO people_graph_positions (node_id, x, y) VALUES (?, ?, ?)",
                [(k, p.x, p.y) for k, p in payload.positions.items()],
            )

        # Groups are target_groups now, so "replace the collection" must be
        # scoped: delete ONLY groups the graph owns (those carrying a geometry
        # row). An unscoped DELETE would take the seeded standard templates and
        # every MCP-created audience with it — the client never sends those, so
        # they would read as "removed by the user" on the very first save.
        incoming = {g.id for g in payload.groups}
        graph_owned = {
            r["group_id"]
            for r in conn.execute(
                "SELECT group_id FROM people_graph_group_geometry"
            ).fetchall()
        }
        for gone in graph_owned - incoming:
            # Removing a bubble always takes it off the canvas, but it only
            # deletes the AUDIENCE when that audience is nothing more than the
            # bubble: kind='custom' with no company and no role_class, i.e. a
            # set of hand-picked people. A group carrying semantics (a company
            # audience, a role_class template) survives as a target_group and
            # merely leaves the graph — "I dragged this off my canvas" must
            # never silently destroy a lens the prism generator depends on.
            conn.execute(
                "DELETE FROM people_graph_group_geometry WHERE group_id = ?", (gone,)
            )
            conn.execute(
                "DELETE FROM target_groups WHERE id = ? AND kind = 'custom' "
                "  AND company_id IS NULL AND role_class IS NULL",
                (gone,),
            )

        for i, g in enumerate(payload.groups):
            # kind='custom' with company_id/role_class NULL is the shape
            # peer/target_groups.py::_member_ids resolves as "explicit members
            # only" — exactly the people dropped into the circle. Name is the
            # only field the graph owns; COALESCE leaves role_class, company_id
            # and notes alone so placing an existing audience on the canvas
            # cannot strip its semantics.
            conn.execute(
                "INSERT INTO target_groups (id, name, kind, notes) "
                "VALUES (?, ?, 'custom', 'Drawn on the People graph') "
                "ON CONFLICT(id) DO UPDATE SET "
                "  name = excluded.name, updated_at = datetime('now')",
                (g.id, g.name),
            )

            # Membership is a full replace per group — but ONLY for a group the
            # graph fully owns. An audience defined by company_id/role_class
            # derives its members from affiliations, and the client just echoes
            # back the RESOLVED list it was shown; writing that back as explicit
            # override rows would freeze a living membership, so a board member
            # who later leaves would linger forever as a manual override.
            # For those, membership stays server-derived and the canvas owns
            # only the name and the geometry.
            owned = conn.execute(
                "SELECT company_id, role_class FROM target_groups WHERE id = ?",
                (g.id,),
            ).fetchone()
            derives_members = bool(
                owned and (owned["company_id"] or owned["role_class"])
            )

            if not derives_members:
                # Filtered against persons because target_group_members.person_id
                # is a FK — a stale id from a browser that has not seen a
                # deletion yet must not 500 the whole save.
                conn.execute(
                    "DELETE FROM target_group_members WHERE group_id = ?", (g.id,)
                )
                for pid in dict.fromkeys(g.memberIds):
                    conn.execute(
                        "INSERT OR IGNORE INTO target_group_members "
                        "(id, group_id, person_id) "
                        "SELECT 'tgm_' || lower(hex(randomblob(6))), ?, id "
                        "  FROM persons WHERE id = ?",
                        (g.id, pid),
                    )

            conn.execute(
                "INSERT INTO people_graph_group_geometry "
                "(group_id, center_x, center_y, radius, geom_version, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET "
                "  center_x = excluded.center_x, center_y = excluded.center_y, "
                "  radius = excluded.radius, geom_version = excluded.geom_version, "
                "  sort_order = excluded.sort_order, updated_at = datetime('now')",
                (
                    g.id,
                    g.center.x if g.center else None,
                    g.center.y if g.center else None,
                    g.radius,
                    g.geomVersion,
                    i,
                ),
            )

        # Upsert rather than replace: unknown keys written by a NEWER
        # frontend must survive a save from an OLDER one still running in
        # another tab, which would otherwise silently drop them.
        if payload.settings.connectionStyle is not None:
            conn.execute(
                "INSERT INTO people_graph_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = datetime('now')",
                ("connection_style", payload.settings.connectionStyle),
            )

    return {
        "ok": True,
        "positions": len(payload.positions),
        "groups": len(payload.groups),
        "saved_at": _layout_saved_at(db),
    }


@router.get("/role-defaults")
def get_role_default_sliders(role: Optional[str] = None) -> dict:
    """Return the slider seed for a generic role label.

    Used by the Add Person flow + the SlidersPanel "reset to default"
    button. Role is matched case-insensitively against
    ``ROLE_DEFAULT_SLIDERS`` and common aliases (``CEO`` → ``ceo``,
    ``Software Engineer`` → ``engineer``). Unknown roles fall back to
    the neutral default vector.
    """
    from okuro.peer.cognitive_profile import role_default_sliders

    return {
        "role": role or "default",
        "sliders": role_default_sliders(role),
    }


@router.get("/slider-meta")
def get_slider_meta() -> dict:
    """Return the canonical slider metadata for UI rendering.

    Body shape: {sliders: [{key, left_label, right_label}, ...]} in the
    canonical order. Frontend reads this once and renders the panel
    deterministically — no labels hardcoded in TS.
    """
    from okuro.peer.cognitive_profile import SLIDER_NAMES, SLIDER_LABELS

    return {
        "sliders": [
            {
                "key": k,
                "left_label": SLIDER_LABELS[k][0],
                "right_label": SLIDER_LABELS[k][1],
            }
            for k in SLIDER_NAMES
        ],
    }


@router.put("/{person_id}/sliders")
def update_person_sliders(person_id: str, payload: SlidersIn, request: Request) -> dict:
    """Patch the slider vector on persons.cognitive.sliders.

    Partial: omitted axes preserve their stored value. New axes overwrite.
    Returns the full cognitive dict after the merge so the client can
    refresh its local state without a follow-up GET.
    """
    _require_localhost_pe(request)

    from okuro.db import get_db
    from okuro.peer.persons import person_update_sliders

    # Delegate to the single source of truth so the HTTP path stamps per-axis
    # provenance (source=user, confidence=1.0) exactly like the MCP path —
    # otherwise UI slider clicks would never confirm a role-seeded prior.
    incoming = payload.model_dump(exclude_none=True)
    result = person_update_sliders(person_id, **incoming)
    if isinstance(result, str) and result.startswith("Person not found"):
        raise HTTPException(404, f"Person not found: {person_id}")

    # Return the full cognitive dict (sliders + provenance) so the client can
    # refresh local state without a follow-up GET.
    db = get_db()
    row = db.fetchone("SELECT cognitive FROM persons WHERE id = ?", (person_id,))
    cog_raw = row["cognitive"] if row else "{}"
    if isinstance(cog_raw, str) and cog_raw:
        try:
            cog = json.loads(cog_raw)
        except json.JSONDecodeError:
            cog = {}
    else:
        cog = cog_raw or {}
    return {"person_id": person_id, "cognitive": cog}


@router.get("/presets")
def list_role_presets() -> dict:
    """Expose role-based starter presets for the Add Person dialog.

    Body shape: {presets: [{role_id, label, domain, description}, ...]}.
    Full preset bodies are fetched via GET /presets/{role_id}.
    """
    from okuro.peer.presets import list_preset_roles

    return {"presets": list_preset_roles()}


@router.get("/presets/{role_id}")
def get_role_preset(role_id: str) -> dict:
    """Full preset body for a role_id (communication + cognitive + hints)."""
    from okuro.peer.presets import resolve_preset
    from okuro.peer.survey_guard import UnreviewedLanguage, require_deliverable_language

    hit = resolve_preset(role_id)
    if not hit:
        raise HTTPException(404, f"No preset for role: {role_id}")
    return hit


@router.get("/{person_id}")
def get_person(person_id: str) -> dict:
    """Full record for one person (parsed JSON columns)."""
    return _fetch_person(person_id)


@router.post("", status_code=201)
def create_person(payload: PersonIn, request: Request) -> dict:
    """Create a new person. ``display_name`` is required."""
    _require_localhost_pe(request)
    if not payload.display_name or not payload.display_name.strip():
        raise HTTPException(400, "display_name is required")

    from okuro.peer.persons import person_add

    # person_add handles upsert — for explicit create we check for collisions first.
    from okuro.db import get_db

    db = get_db()
    pid = _slugify(payload.display_name)
    existing = db.fetchone("SELECT id FROM persons WHERE id = ?", (pid,))
    if existing:
        raise HTTPException(
            409, f"A person with id '{pid}' already exists — use PUT to update."
        )

    person_add(
        display_name=payload.display_name,
        organization=payload.organization,
        role=payload.role,
        relation_to_user=payload.relation_to_user,
        relation_type=payload.relation_type or "professional",
        communication=payload.communication or {},
        cognitive=payload.cognitive or {},
        contact=payload.contact or {},
        notes=payload.notes,
        tags=payload.tags or [],
        origin="web",
    )
    return _fetch_person(pid)


@router.put("/{person_id}")
def update_person(person_id: str, payload: PersonIn, request: Request) -> dict:
    """Full update — replaces all provided fields. Omitted fields are preserved."""
    _require_localhost_pe(request)

    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM persons WHERE id = ?", (person_id,))
    if not row:
        raise HTTPException(404, f"Person not found: {person_id}")
    current = _row_to_dict(row)

    def pick(new, old):
        return new if new is not None else old

    comm = payload.communication if payload.communication is not None else current.get("communication") or {}
    cog = payload.cognitive if payload.cognitive is not None else current.get("cognitive") or {}
    cont = payload.contact if payload.contact is not None else current.get("contact") or {}
    tags = payload.tags if payload.tags is not None else current.get("tags") or []

    display_name = pick(payload.display_name, current.get("display_name"))
    organization = pick(payload.organization, current.get("organization"))
    role = pick(payload.role, current.get("role"))
    relation_to_user = pick(payload.relation_to_user, current.get("relation_to_user"))
    relation_type = pick(payload.relation_type, current.get("relation_type")) or "professional"
    notes = pick(payload.notes, current.get("notes"))
    active = 1
    if payload.active is not None:
        active = 1 if payload.active else 0
    elif current.get("active") is not None:
        active = int(current["active"])

    db.execute(
        """UPDATE persons SET
               display_name = ?,
               organization = ?,
               role = ?,
               relation_to_user = ?,
               relation_type = ?,
               communication = ?,
               cognitive = ?,
               contact = ?,
               notes = ?,
               tags = ?,
               active = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (
            display_name,
            organization,
            role,
            relation_to_user,
            relation_type,
            json.dumps(comm),
            json.dumps(cog),
            json.dumps(cont),
            notes,
            json.dumps(tags),
            active,
            person_id,
        ),
    )
    from okuro.peer.persons import emit_person_event

    emit_person_event(db, person_id, "updated", "web")
    db.conn.commit()

    # Re-embed (best effort — matches person_add behaviour).
    try:
        from okuro.peer.persons import _embed_person

        _embed_person(
            person_id, display_name, organization, role, notes, tags if isinstance(tags, list) else []
        )
    except Exception:
        pass

    return _fetch_person(person_id)


@router.delete("/{person_id}", status_code=204, response_model=None)
def delete_person(person_id: str, request: Request, hard: bool = False) -> None:
    """Delete a person. Default is soft-delete (active=0); ``?hard=true`` removes the row."""
    _require_localhost_pe(request)

    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT id FROM persons WHERE id = ?", (person_id,))
    if not row:
        raise HTTPException(404, f"Person not found: {person_id}")

    if hard:
        db.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        try:
            db.execute("DELETE FROM vec_persons WHERE id = ?", (person_id,))
        except Exception:
            pass
    else:
        db.execute(
            "UPDATE persons SET active = 0, updated_at = datetime('now') WHERE id = ?",
            (person_id,),
        )
    from okuro.peer.persons import emit_person_event

    emit_person_event(db, person_id, "deleted", "web")
    db.conn.commit()
    return None


@router.post("/match")
def match_people(req: MatchRequest) -> dict:
    """Fuzzy/semantic match — falls back to LIKE search if embeddings unavailable."""
    from okuro.db import get_db

    db = get_db()

    # Prefer vector search; mirror okuro.peer.persons.person_match but return JSON.
    results: list[dict] = []
    try:
        from okuro.embed.client import embed_query, to_bytes  # type: ignore

        vec = embed_query(req.query)
        vec_bytes = to_bytes(vec)
        matches = db.vec_search("vec_persons", vec_bytes, limit=req.limit)
        if matches:
            ids = [m["id"] for m in matches]
            distances = {m["id"]: m["distance"] for m in matches}
            placeholders = ", ".join("?" * len(ids))
            rows = db.fetchall(
                f"SELECT id, display_name, organization, role, relation_type, tags "
                f"FROM persons WHERE id IN ({placeholders}) AND active = 1",
                tuple(ids),
            )
            for r in rows:
                person = _row_to_dict(r)
                person["similarity"] = 1.0 - float(distances.get(r["id"], 1.0))
                results.append(person)
            results.sort(key=lambda p: p.get("similarity", 0.0), reverse=True)
    except Exception as exc:
        logger.debug("Vector match unavailable, falling back to LIKE: %s", exc)

    if not results:
        pattern = f"%{req.query.lower()}%"
        rows = db.fetchall(
            """SELECT id, display_name, organization, role, relation_type, tags
               FROM persons
               WHERE active = 1 AND (
                   LOWER(display_name) LIKE ? OR
                   LOWER(COALESCE(organization, '')) LIKE ? OR
                   LOWER(COALESCE(role, '')) LIKE ? OR
                   LOWER(COALESCE(notes, '')) LIKE ?
               )
               LIMIT ?""",
            (pattern, pattern, pattern, pattern, req.limit),
        )
        results = [_row_to_dict(r) for r in rows]

    return {"matches": results}


@router.post("/{person_id}/lens", response_model=LensResponse)
def person_lens_endpoint(person_id: str, req: LensRequest) -> LensResponse:
    """Generate communication guidance (markdown) for a person.

    Purely deterministic today — assembles profile fields into a prompt
    fragment. No LLM call, so ``bridge_invoke`` brokenness does not affect us.
    """
    from okuro.peer.persons import person_lens

    markdown = person_lens(person_id, context=req.context)
    if markdown.startswith("Person not found"):
        raise HTTPException(404, markdown)
    return LensResponse(person_id=person_id, markdown=markdown)


# ── Translate ────────────────────────────────────────────────────────


@router.post("/{person_id}/translate")
def translate_for_person(
    person_id: str, req: TranslateRequest, request: Request
) -> dict:
    """Rewrite `source` for the recipient, log the attempt, return the result.

    Localhost-only — translation hits the LLM bridge (cost + outbound CLI).
    Always logs a row to translation_log, even on failure, so edge stats
    on the graph reflect attempts.
    """
    _require_localhost_pe(request)
    if not req.source or not req.source.strip():
        raise HTTPException(400, "source is empty")

    from okuro.peer.translate import person_translate

    result = person_translate(
        person_id=person_id,
        source=req.source,
        context=req.context,
        provider=req.provider,
    )
    if result.get("error") and (result.get("error") or "").startswith("Person not found"):
        raise HTTPException(404, result["error"])
    return result


# ── Profile enrichment — preset, file drop, provenance ─────────────


@router.post("/{person_id}/apply-preset")
def apply_person_preset(
    person_id: str, payload: ApplyPresetRequest, request: Request
) -> dict:
    """Resolve a role-catalog preset and write it as a source on this person.

    Localhost-only. Idempotent-ish: re-applying the same preset writes new
    person_sources rows (newest wins) so the user can intentionally refresh
    a stale preset after catalog edits.
    """
    _require_localhost_pe(request)
    _fetch_person(person_id)  # 404 if missing

    from okuro.peer.sources import apply_preset

    result = apply_preset(person_id, payload.role_query)
    return result


@router.post("/{person_id}/sources")
async def ingest_person_source(
    person_id: str,
    request: Request,
    file: Optional[UploadFile] = File(default=None),
) -> dict:
    """Ingest a source — file upload OR JSON {text, source_type, source_ref}.

    Localhost-only. Routes the content through the extractor, then writes
    the delta via apply_profile_delta. The caller receives a summary of
    what fields changed and which provenance rows were written.
    """
    _require_localhost_pe(request)
    person = _fetch_person(person_id)

    from okuro.peer.extract import (
        extract_profile_delta_from_text,
        extract_text_from_bytes,
    )
    from okuro.peer.sources import apply_profile_delta

    source_type: str
    source_ref: str | None
    text: str

    if file is not None:
        if not file.filename:
            raise HTTPException(400, "filename required")
        data = await file.read()
        if len(data) > 10_000_000:
            raise HTTPException(400, "file too large (10MB max)")
        text, source_type = extract_text_from_bytes(file.filename, data)
        source_ref = file.filename
        if not text.strip():
            raise HTTPException(422, "no extractable text in file")
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "expected JSON body or multipart file")
        try:
            payload = IngestTextRequest(**(body or {}))
        except Exception as exc:
            raise HTTPException(400, f"invalid body: {exc}")
        text = payload.text
        source_type = payload.source_type
        source_ref = payload.source_ref

    delta = extract_profile_delta_from_text(text, person)
    if not delta:
        return {
            "sources_written": 0,
            "fields_changed": [],
            "apply": False,
            "note": "extractor returned no signal",
            "source_type": source_type,
            "source_ref": source_ref,
        }

    evidence = delta.pop("_evidence", None)
    result = apply_profile_delta(
        person_id,
        source_type=source_type,
        source_ref=source_ref,
        delta=delta,
        confidence=0.6,
    )
    if evidence:
        result["evidence"] = evidence
    result["source_type"] = source_type
    result["source_ref"] = source_ref
    return result


@router.get("/{person_id}/sources")
def list_person_sources(person_id: str, applied_only: bool = True) -> dict:
    """List provenance rows for a person — powers the chip row in the UI."""
    _fetch_person(person_id)
    from okuro.peer.sources import list_sources

    return {"sources": list_sources(person_id, applied_only=applied_only)}


@router.get("/{person_id}/source-groups")
def list_person_source_groups(person_id: str) -> dict:
    """Grouped provenance — one entry per uploaded file / preset / survey.

    Powers the removable source list in the Enrich panel.
    """
    _fetch_person(person_id)
    from okuro.peer.sources import list_source_groups

    return {"groups": list_source_groups(person_id)}


@router.delete("/{person_id}/sources")
def remove_person_source(
    person_id: str, payload: RemoveSourceRequest, request: Request
) -> dict:
    """Remove one source group and revert the fields it set.

    Deletes every row for (source_type, source_ref) and rebuilds each affected
    field from the remaining applied sources (latest wins; orphaned fields are
    cleared). Localhost-only, mirroring the ingest route.
    """
    _require_localhost_pe(request)
    _fetch_person(person_id)
    from okuro.peer.sources import remove_source

    result = remove_source(person_id, payload.source_type, payload.source_ref)
    if result["removed_rows"] == 0:
        raise HTTPException(404, "No matching source to remove")
    return result


# ── Questionnaires — mint only here; public consume lives in api/questionnaires.py ─


def _legacy_freetext_questions(hints: list[str]) -> list[dict]:
    """Baseline free-text questions + role hints (legacy, LLM-parsed path)."""
    questions: list[dict] = [
        {
            "id": "greeting_preference",
            "prompt": "How should I address you in messages? (e.g. 'Hi Alex' vs 'Dear Ms. Morgan')",
        },
        {
            "id": "preferred_channel",
            "prompt": "When I have something for you, where should it land? (email, Slack, SMS, etc.)",
        },
        {
            "id": "length_tolerance",
            "prompt": "When you open a message, how long are you willing to read before you bail?",
        },
        {
            "id": "worst_habit",
            "prompt": "What communication habit of mine do you find hardest to work with?",
        },
    ]
    for i, hint in enumerate(hints):
        questions.append({"id": f"role_hint_{i}", "prompt": hint})
    return questions


@router.post("/{person_id}/questionnaires")
def mint_questionnaire(
    person_id: str, payload: QuestionnaireMintRequest, request: Request
) -> dict:
    """Mint a signed magic-link token + question set for self-report.

    Localhost-only (the mint). The token's consume endpoint is public —
    see api/questionnaires.py. Questions come from the role preset's
    ``questionnaire_hints`` plus a small set of role-agnostic baseline
    questions so the recipient always has something to fill in.
    """
    _require_localhost_pe(request)
    person = _fetch_person(person_id)

    import secrets
    import uuid
    from datetime import datetime, timedelta
    from okuro.db import get_db
    from okuro.peer.presets import resolve_preset

    role_q = (payload.role_hint or person.get("role") or "").strip()
    hit = resolve_preset(role_q) if role_q else None
    hints: list[str] = []
    preset_key: str | None = None
    if hit:
        preset = hit.get("person_preset") or {}
        hints = list(preset.get("questionnaire_hints") or [])
        preset_key = hit.get("role_id")

    # Structured questionnaires carry NO free-text questions — the recipient
    # form renders survey_form() and the answers are scored deterministically.
    # Empty ``questions`` is the discriminator the consume endpoint reads.
    if payload.structured:
        questions: list[dict] = []
    else:
        questions = _legacy_freetext_questions(hints)

    token = secrets.token_urlsafe(32)
    qid = uuid.uuid4().hex[:12]
    expires_at = (
        datetime.utcnow() + timedelta(hours=int(payload.expires_hours))
    ).strftime("%Y-%m-%d %H:%M:%S")

    # Guard at CREATION, exactly as the offline path does. Once a link is in
    # an inbox it is too late to reconsider the translation.
    try:
        lang = require_deliverable_language(
            payload.lang, allow_unreviewed=payload.allow_unreviewed,
            context="mint_token",
        )
    except UnreviewedLanguage as exc:
        raise HTTPException(409, str(exc)) from None

    db = get_db()
    db.execute(
        """INSERT INTO person_questionnaires (
               id, person_id, token, preset_key, questions, status,
               expires_at, lang
           ) VALUES (?, ?, ?, ?, ?, 'sent', ?, ?)""",
        (qid, person_id, token, preset_key, json.dumps(questions), expires_at,
         lang),
    )
    db.conn.commit()

    share_url = f"/q/{token}"
    return {
        "id": qid,
        "token": token,
        "share_url": share_url,
        "expires_at": expires_at,
        "question_count": len(questions),
        "preset_key": preset_key,
    }


@router.get("/{person_id}/questionnaires")
def list_questionnaires(person_id: str, request: Request) -> dict:
    """Localhost-only: this person's surveys and how long each one took.

    The reader half of migration 125. P3.1's acceptance is a duration, so
    ``answered_at - started_at`` has to be reachable from the running system
    and not only from a test — otherwise ``started_at`` is a stamp nothing
    consumes. ``completion_seconds`` is None when either end is missing;
    ``sent_at`` is never substituted for a missing ``started_at`` (see
    peer.persons.questionnaire_timings).

    Returns no token and no responses — this is the timing view, not a way
    to read what the recipient said.
    """
    from okuro.peer.persons import questionnaire_timings

    _require_localhost_pe(request)
    _fetch_person(person_id)
    rows = [
        {k: v for k, v in r.items() if k != "token"}
        for r in questionnaire_timings(person_id)
    ]
    measured = [r["completion_seconds"] for r in rows if r["completion_seconds"]]
    return {
        "person_id": person_id,
        "questionnaires": rows,
        "measured_count": len(measured),
        "slowest_seconds": max(measured) if measured else None,
    }


# ── Offline survey — universal delivery (no cloud, no exposure) ────────


@router.get("/{person_id}/survey.html")
def download_offline_survey(
    person_id: str,
    request: Request,
    lang: str = "en",
    allow_unreviewed: bool = False,
):
    """Localhost-only: download a self-contained offline survey HTML.

    The user emails/hands this file to the recipient, who fills it in any
    browser (no network) and gets a {person_id, survey} JSON back to import
    via POST /{person_id}/survey-import. Scored by the same deterministic
    scorer as the online path.
    """
    from fastapi.responses import HTMLResponse
    from okuro.peer.offline_survey import build_offline_survey_html
    from okuro.peer.survey import survey_form

    from okuro.peer.survey_guard import UnreviewedLanguage, require_deliverable_language

    _require_localhost_pe(request)
    person = _fetch_person(person_id)
    # Refuse at CREATION, never at consumption: a link already in someone's
    # inbox must not break because of a decision the sender made.
    try:
        lang = require_deliverable_language(
            lang, allow_unreviewed=allow_unreviewed, context="offline_html",
        )
    except UnreviewedLanguage as exc:
        raise HTTPException(409, str(exc)) from None
    html = build_offline_survey_html(
        survey_form(lang),
        person={
            "id": person_id,
            "display_name": person.get("display_name") or person_id,
            "role": person.get("role"),
        },
    )
    return HTMLResponse(
        content=html,
        headers={
            "Content-Disposition": f'attachment; filename="okuro-survey-{person_id}-{lang}.html"'
        },
    )


class SurveyImportRequest(BaseModel):
    survey: dict[str, Any] = Field(default_factory=dict)


@router.post("/{person_id}/survey-import")
def import_offline_survey(
    person_id: str, payload: SurveyImportRequest, request: Request
) -> dict:
    """Localhost-only: apply an offline-filled survey JSON to this person.

    Mirrors the online structured path (POST /api/q/{token}) but for the file
    the recipient sent back. ``person_id`` comes from the URL — the user picks
    who; any person_id embedded in the file is ignored (trust the chooser).
    """
    from okuro.orchestrator.api.questionnaires import _apply_survey, _sanitize_survey

    _require_localhost_pe(request)
    _fetch_person(person_id)  # 404 if missing
    survey = _sanitize_survey(payload.survey)
    if not survey:
        raise HTTPException(400, "empty survey payload")
    applied = _apply_survey(person_id, "offline-import", survey)
    return {
        "ok": True,
        "fields_updated": applied.get("fields_changed") or [],
        "measured_axes": applied.get("measured_axes") or [],
        "over_claim_flags": applied.get("over_claim_flags", 0),
    }


# ── Blank self-identify survey (one file -> many recipients -> create on import) ──


@router.get("/survey/languages")
def list_survey_languages(request: Request) -> dict:
    """What a language picker needs, including which translations are checked.

    ``reviewed`` travels so the UI can warn BEFORE the download is attempted
    rather than the user meeting a 409 after choosing.
    """
    from okuro.peer.survey_i18n import language_choices

    _require_localhost_pe(request)
    return {"languages": language_choices()}


@router.get("/offline/blank.html")
def download_blank_offline_survey(
    request: Request, lang: str = "en", allow_unreviewed: bool = False
):
    """Localhost-only: download a BLANK self-identifying offline survey.

    Send the SAME file to many people. Each enters their own name/role/email,
    fills the survey, and returns a {identity, survey} JSON. Importing it
    CREATES the person and applies the profile in one step (POST /offline/import).

    Takes ``lang`` and the review guard like every other survey exit. This is
    the endpoint the People page's download button calls, so it is the one a
    real send is most likely to go through — it was the LAST of the three
    offline exits to get either, and English-only by omission is not a
    decision anybody made.
    """
    from fastapi.responses import HTMLResponse
    from okuro.peer.offline_survey import build_offline_survey_html
    from okuro.peer.survey import survey_form
    from okuro.peer.survey_guard import UnreviewedLanguage, require_deliverable_language

    _require_localhost_pe(request)
    try:
        lang = require_deliverable_language(
            lang, allow_unreviewed=allow_unreviewed, context="offline_blank",
        )
    except UnreviewedLanguage as exc:
        raise HTTPException(409, str(exc)) from None
    html = build_offline_survey_html(survey_form(lang), person=None)
    return HTMLResponse(
        content=html,
        headers={
            "Content-Disposition": f'attachment; filename="okuro-survey-{lang}.html"'
        },
    )


class OfflineImportRequest(BaseModel):
    identity: dict[str, Any] = Field(default_factory=dict)
    survey: dict[str, Any] = Field(default_factory=dict)


@router.post("/offline/import")
def import_self_identified_survey(
    payload: OfflineImportRequest, request: Request
) -> dict:
    """Localhost-only: create-or-update a person from a self-identified survey.

    Recipient-entered identity drives the person; the deterministic scorer
    applies the profile. Existing person (same name slug) is updated, not
    duplicated, and their non-survey data is preserved. Same-name distinct
    people collide on the slug — known v1 limitation (mitigate via email later).
    """
    from okuro.db import get_db
    from okuro.orchestrator.api.questionnaires import _apply_survey, _sanitize_survey
    from okuro.peer.persons import person_add

    _require_localhost_pe(request)
    identity = payload.identity or {}
    name = str(identity.get("display_name") or "").strip()
    if not name:
        raise HTTPException(400, "identity.display_name required")
    survey = _sanitize_survey(payload.survey)
    if not survey:
        raise HTTPException(400, "empty survey payload")

    pid = _slugify(name)
    existed = get_db().fetchone("SELECT 1 FROM persons WHERE id = ?", (pid,)) is not None
    if not existed:
        # v2 identity uses profession/function (no separate "role"); contact optional.
        role = (str(identity.get("profession") or "").strip()
                or str(identity.get("function") or "").strip() or None)
        contact: dict[str, str] = {}
        for key in ("email", "linkedin"):
            if str(identity.get(key) or "").strip():
                contact[key] = str(identity[key]).strip()
        person_add(name, role=role, person_id=pid, contact=contact or None)
    applied = _apply_survey(pid, "offline-import", survey)
    return {
        "ok": True,
        "person_id": pid,
        "display_name": name,
        "created": not existed,
        "fields_updated": applied.get("fields_changed") or [],
        "measured_axes": applied.get("measured_axes") or [],
        "over_claim_flags": applied.get("over_claim_flags", 0),
    }
