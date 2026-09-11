# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Stack registry readers + lazy seeder. Mirrors sense/principles pattern.
# index:
#   imports
#   def seed_registry
#   def list_layers
#   def get_layer
#   def list_entries
#   def get_entry
#   def match_entries
#   def list_profiles
#   def get_profile
#   def resolve_profile
#   def active_profile_for
#   def list_slot_kinds
#   def list_brands
#   def get_brand
#   def resolve_brand
#   def active_brand_for
# AGENT_HEADER_END -->
"""Stack registry readers + lazy seeder.

Data lives in SQLite (`stack_*` tables, see migration 010). Seed source of
truth is `data/stack.yaml`, bundled with the package. Call any read fn on an
empty DB and it will auto-seed — same pattern as `sense.principles`.

Reads return plain dicts (JSON-safe). Writes live in `writer.py`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("okuro.stack")

DATA_DIR = Path(__file__).parent / "data"
SEED_YAML = DATA_DIR / "stack.yaml"


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

def seed_registry() -> dict[str, int]:
    """Seed layers, entries, deps, alternatives, profiles from YAML.

    Uses INSERT OR REPLACE so it's safe to re-run. Hand edits to stack.yaml
    propagate on next seed (triggered by any empty read or by
    `okuro.cli` init). Returns counts per table.
    """
    from okuro.db import get_db

    if not SEED_YAML.exists():
        return {
            "layers": 0, "entries": 0, "deps": 0, "alternatives": 0,
            "profiles": 0, "slot_kinds": 0, "brands": 0, "asset_profiles": 0,
            "voice_profiles": 0,
        }

    data = yaml.safe_load(SEED_YAML.read_text()) or {}
    db = get_db()

    counts = {
        "layers": 0, "entries": 0, "deps": 0, "alternatives": 0,
        "profiles": 0, "slot_kinds": 0, "brands": 0, "asset_profiles": 0,
        "voice_profiles": 0,
    }

    # Layers first (entries reference them via FK).
    for layer in data.get("layers", []):
        db.execute(
            "INSERT OR REPLACE INTO stack_layers "
            "(id, name, description, category, cardinality, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                layer["id"],
                layer["name"],
                layer.get("description", ""),
                layer["category"],
                layer.get("cardinality", "single"),
                int(layer.get("sort_order", 0)),
            ),
        )
        counts["layers"] += 1

    # Entries. We seed all entries first without `replaces`, then a second
    # pass sets `replaces` (self-FK) so forward references work regardless
    # of declaration order in YAML.
    entries = data.get("entries", [])
    for e in entries:
        db.execute(
            "INSERT OR REPLACE INTO stack_entries "
            "(id, layer, name, version, status, rationale, use_when, avoid_when, "
            " docs_url, owner, replaces, last_reviewed, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, datetime('now'))",
            (
                e["id"],
                e["layer"],
                e["name"],
                e.get("version"),
                e.get("status", "trial"),
                e.get("rationale", ""),
                json.dumps(e.get("use_when", [])),
                json.dumps(e.get("avoid_when", [])),
                e.get("docs_url"),
                e.get("owner"),
                e.get("last_reviewed"),
            ),
        )
        counts["entries"] += 1

    for e in entries:
        if e.get("replaces"):
            db.execute(
                "UPDATE stack_entries SET replaces = ? WHERE id = ?",
                (e["replaces"], e["id"]),
            )

    # Deps — wipe + re-insert (authoritative from YAML).
    db.execute("DELETE FROM stack_entry_deps")
    for e in entries:
        for dep in e.get("depends_on", []) or []:
            try:
                db.execute(
                    "INSERT INTO stack_entry_deps (entry_id, depends_on) VALUES (?, ?)",
                    (e["id"], dep),
                )
                counts["deps"] += 1
            except Exception as exc:
                logger.warning("Failed to seed dep %s → %s: %s", e["id"], dep, exc)

    # Alternatives — wipe + re-insert.
    db.execute("DELETE FROM stack_entry_alternatives")
    for e in entries:
        for alt in e.get("alternatives_considered", []) or []:
            if not isinstance(alt, dict):
                continue
            alt_id = alt.get("id") or alt.get("alternative_id")
            if not alt_id:
                continue
            db.execute(
                "INSERT INTO stack_entry_alternatives "
                "(entry_id, alternative_id, alternative_name, reason_rejected) "
                "VALUES (?, ?, ?, ?)",
                (
                    e["id"],
                    alt_id,
                    alt.get("name"),
                    alt.get("reason_rejected"),
                ),
            )
            counts["alternatives"] += 1

    # Profiles — wipe + re-insert.
    db.execute("DELETE FROM stack_profile_entries")
    for p in data.get("profiles", []):
        db.execute(
            "INSERT OR REPLACE INTO stack_profiles "
            "(name, label, description, scope, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (
                p["name"],
                p.get("label", p["name"]),
                p.get("description", ""),
                p.get("scope", "fullstack"),
                p.get("status", "active"),
            ),
        )
        for pe in p.get("entries", []):
            if isinstance(pe, str):
                entry_id, role = pe, "primary"
            else:
                entry_id = pe.get("id")
                role = pe.get("role", "primary")
            if not entry_id:
                continue
            db.execute(
                "INSERT OR REPLACE INTO stack_profile_entries "
                "(profile_name, entry_id, role) VALUES (?, ?, ?)",
                (p["name"], entry_id, role),
            )
        counts["profiles"] += 1

    # Brand slot kinds — wipe + re-insert (authoritative from YAML).
    # brand_slots has a FK → brand_slot_kinds(kind); clear the children first so
    # a re-seed over an already-populated DB doesn't trip the constraint (the
    # brands loop below repopulates brand_slots).
    db.execute("DELETE FROM brand_slots")
    db.execute("DELETE FROM brand_slot_kinds")
    for sk in data.get("brand_slot_kinds", []):
        db.execute(
            "INSERT OR REPLACE INTO brand_slot_kinds "
            "(kind, registry, required, cardinality, scope_filter, sort_order, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sk["kind"],
                sk["registry"],
                int(bool(sk.get("required", False))),
                sk.get("cardinality", "single"),
                sk.get("scope_filter"),
                int(sk.get("sort_order", 0)),
                sk.get("description", ""),
            ),
        )
        counts["slot_kinds"] += 1

    # Asset profiles — wipe sets + upsert profiles (authoritative from YAML).
    # The icon library itself is a separate licensed data-dir DB; this only
    # seeds the small per-brand binding (provider, allowed sets, license, delivery).
    db.execute("DELETE FROM asset_profile_sets")
    for ap in data.get("asset_profiles", []):
        db.execute(
            "INSERT OR REPLACE INTO asset_profiles "
            "(id, name, description, icon_provider, license_tier, delivery, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                ap["id"],
                ap["name"],
                ap.get("description", ""),
                ap.get("icon_provider", "okuro-assets.icons"),
                ap.get("license_tier", "permissive"),
                ap.get("delivery", "npm-name"),
                ap.get("status", "active"),
            ),
        )
        for pos, set_id in enumerate(ap.get("allowed_sets", []) or []):
            db.execute(
                "INSERT OR REPLACE INTO asset_profile_sets "
                "(profile_id, set_id, sort_order) VALUES (?, ?, ?)",
                (ap["id"], set_id, pos),
            )
        counts["asset_profiles"] = counts.get("asset_profiles", 0) + 1

    # Voice profiles — the brand's VOICE, seeded like every other registry.
    #
    # Migration 146 creates `voice_profiles` and fills NOTHING: the one shipped
    # voice was written into one machine's store by a one-off data migration
    # that never entered the repo. Measured 2026-09-05 — the id appeared nowhere
    # in the tree — so a fresh install got the tables empty and a brand naming a
    # voice slot had a dangling ref the validator would flag.
    #
    # RULES ARE TOLD APART BY TYPE, not by a hard-coded list of kind names: a
    # MAP is (key, value) rows, a LIST is (key, null) rows. That keeps the
    # promise the table was built on — a sixth kind is a row, not a migration —
    # and it holds here too, where a sixth kind is a YAML block.
    db.execute("DELETE FROM voice_profile_rules")
    for vp in data.get("voice_profiles", []):
        db.execute(
            "INSERT OR REPLACE INTO voice_profiles "
            "(id, name, description, tone, sentences, casing, locale, "
            " reference, owner, domain, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                vp["id"],
                vp["name"],
                vp.get("description", ""),
                vp.get("tone"),
                vp.get("sentences"),
                vp.get("casing"),
                vp.get("locale"),
                vp.get("reference"),
                vp.get("owner"),
                vp.get("domain"),
                vp.get("status", "active"),
            ),
        )
        for kind, rows in (vp.get("rules") or {}).items():
            pairs = rows.items() if isinstance(rows, dict) else (
                (key, None) for key in rows
            )
            for pos, (key, value) in enumerate(pairs):
                db.execute(
                    "INSERT OR REPLACE INTO voice_profile_rules "
                    "(profile_id, kind, key, value, sort_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (vp["id"], kind, str(key), value, pos),
                )
        counts["voice_profiles"] = counts.get("voice_profiles", 0) + 1

    # Brands + slot assignments — wipe slots, upsert brands.
    # Keeping brands row across re-seed preserves project bindings; only
    # slot assignments are fully authoritative-from-YAML.
    db.execute("DELETE FROM brand_slots")
    for b in data.get("brands", []):
        db.execute(
            "INSERT OR REPLACE INTO brands "
            "(id, name, description, status, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (
                b["id"],
                b["name"],
                b.get("description", ""),
                b.get("status", "active"),
            ),
        )
        slots = b.get("slots", {}) or {}
        for slot_kind, ref in slots.items():
            if ref is None:
                continue
            refs = ref if isinstance(ref, list) else [ref]
            for pos, rid in enumerate(refs):
                if not rid:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO brand_slots "
                    "(brand_id, slot_kind, ref_id, position) "
                    "VALUES (?, ?, ?, ?)",
                    (b["id"], slot_kind, rid, pos),
                )
        counts["brands"] += 1

    db.conn.commit()
    return counts


def _ensure_seeded() -> None:
    """Trigger lazy seed if the layers table is empty."""
    from okuro.db import get_db
    db = get_db()
    row = db.fetchone("SELECT COUNT(*) AS n FROM stack_layers")
    if not row or row["n"] == 0:
        seed_registry()


# ---------------------------------------------------------------------------
# Layer readers
# ---------------------------------------------------------------------------

def list_layers(category: str | None = None) -> list[dict]:
    """List layers, optionally filtered by category."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    sql = "SELECT * FROM stack_layers"
    params: list[Any] = []
    if category:
        sql += " WHERE category = ?"
        params.append(category)
    sql += " ORDER BY sort_order, id"
    return db.fetchall(sql, tuple(params))


def get_layer(layer_id: str) -> dict | None:
    from okuro.db import get_db
    _ensure_seeded()
    return get_db().fetchone(
        "SELECT * FROM stack_layers WHERE id = ?", (layer_id,)
    )


# ---------------------------------------------------------------------------
# Entry readers
# ---------------------------------------------------------------------------

def _hydrate_entry(row: dict) -> dict:
    """Parse JSON arrays + attach deps/alternatives."""
    from okuro.db import get_db
    db = get_db()

    row = dict(row)
    row["use_when"] = json.loads(row.get("use_when") or "[]")
    row["avoid_when"] = json.loads(row.get("avoid_when") or "[]")

    deps = db.fetchall(
        "SELECT depends_on FROM stack_entry_deps WHERE entry_id = ? ORDER BY depends_on",
        (row["id"],),
    )
    row["depends_on"] = [d["depends_on"] for d in deps]

    alts = db.fetchall(
        "SELECT alternative_id, alternative_name, reason_rejected "
        "FROM stack_entry_alternatives WHERE entry_id = ? ORDER BY alternative_id",
        (row["id"],),
    )
    row["alternatives_considered"] = [
        {
            "id": a["alternative_id"],
            "name": a["alternative_name"],
            "reason_rejected": a["reason_rejected"],
        }
        for a in alts
    ]
    return row


def list_entries(
    layer: str | None = None,
    status: str | None = None,
    profile: str | None = None,
) -> list[dict]:
    """List entries with optional filters.

    Profile filter resolves to the entries bound to that profile.
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    clauses = []
    params: list[Any] = []

    base = "SELECT e.* FROM stack_entries e"
    if profile:
        base += " JOIN stack_profile_entries pe ON pe.entry_id = e.id"
        clauses.append("pe.profile_name = ?")
        params.append(profile)

    if layer:
        clauses.append("e.layer = ?")
        params.append(layer)
    if status:
        clauses.append("e.status = ?")
        params.append(status)

    sql = base
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY e.layer, e.status, e.id"

    rows = db.fetchall(sql, tuple(params))
    return [_hydrate_entry(r) for r in rows]


def get_entry(entry_id: str) -> dict | None:
    from okuro.db import get_db
    _ensure_seeded()
    row = get_db().fetchone("SELECT * FROM stack_entries WHERE id = ?", (entry_id,))
    return _hydrate_entry(row) if row else None


def match_entries(need: str, limit: int = 5) -> list[dict]:
    """Naive keyword match against entry name, id, layer, rationale, use_when.

    No embeddings — stack is small (~30 entries). If the registry grows past
    ~500 entries, swap in vec_search over a new vec table.
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    q = f"%{need.lower()}%"
    rows = db.fetchall(
        "SELECT * FROM stack_entries "
        "WHERE lower(id) LIKE ? OR lower(name) LIKE ? "
        "   OR lower(layer) LIKE ? OR lower(rationale) LIKE ? "
        "   OR lower(use_when) LIKE ? "
        "ORDER BY "
        "  CASE status WHEN 'approved' THEN 0 WHEN 'trial' THEN 1 "
        "              WHEN 'deprecated' THEN 2 ELSE 3 END, "
        "  layer, id "
        "LIMIT ?",
        (q, q, q, q, q, limit),
    )
    return [_hydrate_entry(r) for r in rows]


# ---------------------------------------------------------------------------
# Profile readers
# ---------------------------------------------------------------------------

def list_profiles(
    status: str | None = None,
    scope: str | None = None,
) -> list[dict]:
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    sql = "SELECT * FROM stack_profiles"
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY scope, name"

    rows = db.fetchall(sql, tuple(params))
    # Attach entry counts (cheap roll-up).
    for r in rows:
        n = db.fetchone(
            "SELECT COUNT(*) AS n FROM stack_profile_entries WHERE profile_name = ?",
            (r["name"],),
        )
        r["entry_count"] = n["n"] if n else 0
    return rows


def get_profile(name: str) -> dict | None:
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    p = db.fetchone("SELECT * FROM stack_profiles WHERE name = ?", (name,))
    if not p:
        return None

    p = dict(p)
    p["entries"] = db.fetchall(
        "SELECT pe.entry_id AS id, pe.role, e.layer, e.name, e.status, e.version "
        "FROM stack_profile_entries pe "
        "JOIN stack_entries e ON e.id = pe.entry_id "
        "WHERE pe.profile_name = ? "
        "ORDER BY e.layer, pe.role, pe.entry_id",
        (name,),
    )

    # Which projects have this profile bound?
    p["projects"] = [
        r["project_slug"] for r in db.fetchall(
            "SELECT project_slug FROM stack_project_profile WHERE profile_name = ? "
            "ORDER BY project_slug",
            (name,),
        )
    ]
    return p


def resolve_profile(name: str) -> dict | None:
    """Resolve a profile into a per-layer view — one entry per layer,
    grouped by category, with dependency closure.

    Used by bootstrap and scaffolding: "given this profile, what does the
    stack actually look like?"
    """
    from okuro.db import get_db
    _ensure_seeded()

    profile = get_profile(name)
    if not profile:
        return None

    db = get_db()

    # Build dep closure over entries in the profile.
    picked_ids = {e["id"] for e in profile["entries"]}
    closure: set[str] = set(picked_ids)
    frontier = set(picked_ids)
    while frontier:
        next_frontier: set[str] = set()
        for eid in frontier:
            deps = db.fetchall(
                "SELECT depends_on FROM stack_entry_deps WHERE entry_id = ?", (eid,)
            )
            for d in deps:
                if d["depends_on"] not in closure:
                    closure.add(d["depends_on"])
                    next_frontier.add(d["depends_on"])
        frontier = next_frontier

    # Group by layer → category.
    layers = {layer["id"]: layer for layer in list_layers()}
    by_category: dict[str, list[dict]] = {}
    transitive: list[dict] = []

    for eid in sorted(closure):
        entry = db.fetchone("SELECT * FROM stack_entries WHERE id = ?", (eid,))
        if not entry:
            continue
        layer = layers.get(entry["layer"])
        if not layer:
            continue
        enriched = {
            "id": entry["id"],
            "name": entry["name"],
            "version": entry["version"],
            "status": entry["status"],
            "layer": entry["layer"],
            "layer_name": layer["name"],
            "role": (
                "primary"
                if eid in picked_ids
                and next(
                    (pe for pe in profile["entries"] if pe["id"] == eid), {}
                ).get("role") == "primary"
                else "secondary" if eid in picked_ids else "transitive"
            ),
        }
        if enriched["role"] == "transitive":
            transitive.append(enriched)
        else:
            by_category.setdefault(layer["category"], []).append(enriched)

    return {
        "name": profile["name"],
        "label": profile["label"],
        "description": profile["description"],
        "status": profile["status"],
        "by_category": by_category,
        "transitive": transitive,
        "projects": profile["projects"],
    }


def active_profile_for(project_slug: str) -> dict | None:
    """Return the resolved profile currently bound to a project, if any."""
    from okuro.db import get_db
    _ensure_seeded()

    row = get_db().fetchone(
        "SELECT profile_name FROM stack_project_profile WHERE project_slug = ?",
        (project_slug,),
    )
    if not row:
        return None
    return resolve_profile(row["profile_name"])


# ---------------------------------------------------------------------------
# Brand slot kinds + brands
# ---------------------------------------------------------------------------

def list_slot_kinds() -> list[dict]:
    """List all brand slot kinds (the configurable plug points)."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()
    rows = db.fetchall(
        "SELECT * FROM brand_slot_kinds ORDER BY sort_order, kind"
    )
    # Cast the SQLite INTEGER `required` back to a bool for JSON consumers.
    for r in rows:
        r["required"] = bool(r.get("required"))
    return rows


def _get_slot_kind(kind: str) -> dict | None:
    from okuro.db import get_db
    row = get_db().fetchone(
        "SELECT * FROM brand_slot_kinds WHERE kind = ?", (kind,)
    )
    if not row:
        return None
    row = dict(row)
    row["required"] = bool(row.get("required"))
    return row


def list_brands(status: str | None = None) -> list[dict]:
    """List brands (optionally filtered by status). Slot count + project count
    are attached as cheap roll-ups so the UI can render cards without N+1s."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    sql = "SELECT * FROM brands"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY name"

    rows = db.fetchall(sql, tuple(params))
    for r in rows:
        slots = db.fetchone(
            "SELECT COUNT(*) AS n FROM brand_slots WHERE brand_id = ?", (r["id"],)
        )
        r["slot_count"] = slots["n"] if slots else 0

        projects = db.fetchone(
            "SELECT COUNT(*) AS n FROM stack_project_brand WHERE brand_id = ?",
            (r["id"],),
        )
        r["project_count"] = projects["n"] if projects else 0
    return rows


def get_brand(brand_id: str) -> dict | None:
    """Get a brand with its slot assignments (raw ref_ids, not resolved)."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    brand = db.fetchone("SELECT * FROM brands WHERE id = ?", (brand_id,))
    if not brand:
        return None

    brand = dict(brand)
    slots = db.fetchall(
        "SELECT slot_kind, ref_id, position FROM brand_slots "
        "WHERE brand_id = ? ORDER BY slot_kind, position",
        (brand_id,),
    )

    # Group by slot_kind; single-cardinality slots collapse to a scalar,
    # multi stay as lists. Matches YAML shape for round-trip friendliness.
    kind_meta = {sk["kind"]: sk for sk in list_slot_kinds()}
    grouped: dict[str, Any] = {}
    for s in slots:
        kind = s["slot_kind"]
        grouped.setdefault(kind, []).append(s["ref_id"])
    brand["slots"] = {
        kind: (refs[0] if kind_meta.get(kind, {}).get("cardinality") != "multi" else refs)
        for kind, refs in grouped.items()
    }

    brand["projects"] = [
        r["project_slug"] for r in db.fetchall(
            "SELECT project_slug FROM stack_project_brand WHERE brand_id = ? "
            "ORDER BY project_slug",
            (brand_id,),
        )
    ]
    return brand


def _resolve_design_profile(design_id: str) -> dict | None:
    """The design slot, resolved from the ENGINE. There is no second source.

    THE NINE SILENT DEGRADATIONS HANG OFF THIS ONE FUNCTION. It imported
    okuro.design.profiles and swallowed ImportError with `return None`, so
    retiring v0 would have left the delivery chain, the slides generator, prism,
    bootstrap's design block, the CRM's engagement resolver and three stack
    tools each resolving to nothing -- and every one of them fails SILENTLY,
    rendering unbranded rather than raising. That is why the fix belonged here
    and not at nine call sites.

    THE v0 FALLBACK IS GONE (p10, 2026-09-06). It was a migration affordance
    and it said so in this docstring: "when the last v0 profile is gone the
    fallback is dead code and goes with the package." Both okuro brands were
    repointed at `okuro-ds` on 2026-09-05, the package is deleted, and a slot
    naming something the engine cannot open now returns None -- which the
    validator flags, exactly as it does for a missing asset or principle set.
    A miss is LOGGED rather than swallowed: it means somebody has a slot to fix.

    The projection lives in design_engine.profile_view -- see that module for
    why a projection is not a shim, and for the field-by-field trace to the
    lines that read each one.
    """
    from okuro.design_engine.profile_view import profile_view

    try:
        view = profile_view(design_id)
    except Exception:
        logger.exception("design kit %r failed to project", design_id)
        return None
    if view is None:
        logger.info(
            "design slot %r names no design system the engine can open; "
            "repoint it at a kit (see `okuro design list`)",
            design_id,
        )
    return view


def _resolve_principle_set(set_id: str) -> dict | None:
    """Expand a principle_set into its member principles. Returns None if the
    set doesn't exist (caller / validator flags this)."""
    try:
        from okuro.sense.principle_sets import get_principle_set
    except ImportError:
        return None
    return get_principle_set(set_id)


def _resolve_asset_profile(profile_id: str) -> dict | None:
    """Resolve an asset profile into provider + allowed sets + license + delivery.
    Returns None if the profile doesn't exist (validator flags it)."""
    from okuro.db import get_db
    db = get_db()
    row = db.fetchone("SELECT * FROM asset_profiles WHERE id = ?", (profile_id,))
    if not row:
        return None
    row = dict(row)
    row["allowed_sets"] = [
        r["set_id"] for r in db.fetchall(
            "SELECT set_id FROM asset_profile_sets WHERE profile_id = ? ORDER BY sort_order",
            (profile_id,),
        )
    ]
    return row


def _resolve_voice_profile(profile_id: str) -> dict | None:
    """Resolve a voice profile into tone + casing + locale + terminology.

    THE BRAND'S VOICE, which used to live inside its DESIGN profile. v0 fused
    the two: `language` (tone, locale, casing, terminology) and `brand.owner` /
    `brand.domain` sat in the same YAML as the palette, because that was the
    file that existed. The owner settled it on 2026-09-03 -- "language belongs to
    the brand not the design" -- and this is the registry that follows from it.

    Returns None if the profile does not exist; the validator flags it, exactly
    as it does for a missing design or asset profile.
    """
    from okuro.db import get_db
    db = get_db()
    row = db.fetchone("SELECT * FROM voice_profiles WHERE id = ?", (profile_id,))
    if not row:
        return None
    row = dict(row)
    # Rules come back GROUPED BY KIND, because every consumer wants one kind at
    # a time -- a copy reviewer wants the banned phrases, a formatter wants the
    # date pattern. Returning one flat list would make each of them filter.
    grouped: dict[str, list[dict]] = {}
    for r in db.fetchall(
        "SELECT kind, key, value, note FROM voice_profile_rules "
        "WHERE profile_id = ? ORDER BY kind, sort_order, key",
        (profile_id,),
    ):
        r = dict(r)
        grouped.setdefault(r.pop("kind"), []).append(r)
    row["rules"] = grouped
    return row


def resolve_brand(brand_id: str) -> dict | None:
    """Resolve a brand into its full composition — design tokens + fe stack +
    be stack + principles — for the UI and bootstrap.

    Every slot is resolved via its registry:
        design_profile  → YAML file on disk
        stack_profile   → resolve_profile(name)
        principle_set   → members expanded to full principle rows
        asset_profile   → provider + allowed sets + license tier + delivery
        voice_profile   → tone + casing + locale + terminology pairs

    Missing refs are returned as {kind, ref_id, missing: true} so the UI can
    show "broken slot" state without crashing the render path.
    """
    brand = get_brand(brand_id)
    if not brand:
        return None

    kind_meta = {sk["kind"]: sk for sk in list_slot_kinds()}
    resolved_slots: dict[str, Any] = {}

    for kind, raw in brand.get("slots", {}).items():
        meta = kind_meta.get(kind)
        if not meta:
            # Unknown slot kind — surface raw so nothing is silently dropped.
            resolved_slots[kind] = {"raw": raw, "unknown_kind": True}
            continue

        refs = raw if isinstance(raw, list) else [raw]
        registry = meta["registry"]
        expanded: list[dict] = []
        for ref_id in refs:
            if registry == "design_profile":
                data = _resolve_design_profile(ref_id)
            elif registry == "stack_profile":
                data = resolve_profile(ref_id)
            elif registry == "principle_set":
                data = _resolve_principle_set(ref_id)
            elif registry == "asset_profile":
                data = _resolve_asset_profile(ref_id)
            elif registry == "voice_profile":
                data = _resolve_voice_profile(ref_id)
            else:
                data = None
            if data is None:
                expanded.append({"ref_id": ref_id, "missing": True})
            else:
                expanded.append({"ref_id": ref_id, "data": data})

        resolved_slots[kind] = (
            expanded if meta["cardinality"] == "multi" else expanded[0]
        )

    return {
        "id": brand["id"],
        "name": brand["name"],
        "description": brand["description"],
        "status": brand["status"],
        "logo_corner": brand.get("logo_corner") or "tl",
        "slots": resolved_slots,
        "projects": brand["projects"],
    }


def design_profile_for_brand(brand_id: str) -> str | None:
    """Return the design_profile ref_id bound to a brand's design slot, or None.

    Lightweight companion to `resolve_brand`: reads only the design slot's raw
    ref_id (a design-profile id consumable by `okuro.design.tokens.generate_css`)
    without expanding the stack / principle / asset slots. Used by the web
    theming path (`/tokens.css`) so selecting a brand re-themes the whole UI.
    """
    brand = get_brand(brand_id)
    if not brand:
        return None
    design = brand.get("slots", {}).get("design")
    if isinstance(design, list):
        design = design[0] if design else None
    return design or None


def active_brand_for(project_slug: str) -> dict | None:
    """Return the resolved brand bound to a project, if any.

    Brand takes precedence over a legacy project→profile binding; callers
    can still fall back to `active_profile_for` if this returns None.
    """
    from okuro.db import get_db
    _ensure_seeded()

    row = get_db().fetchone(
        "SELECT brand_id FROM stack_project_brand WHERE project_slug = ?",
        (project_slug,),
    )
    if not row:
        return None
    return resolve_brand(row["brand_id"])
