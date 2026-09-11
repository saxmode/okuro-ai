# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Stack registry mutations — entry upsert, status transitions, profile binding, proposals, brands.
# index:
#   imports
#   def upsert_entry
#   def set_entry_status
#   def upsert_profile
#   def assign_project_profile
#   def propose
#   def decide_proposal
#   def list_proposals
#   def upsert_brand
#   def set_brand_slot
#   def delete_brand_slot
#   def assign_project_brand
#   def delete_brand
# AGENT_HEADER_END -->
"""Stack registry mutations.

Kept separate from `registry.py` so the read path stays import-cheap and
testable in isolation. All mutations commit immediately — there's no
transactional batch API by design (the registry is small enough that
per-call commits are fine).

Lifecycle transitions are enforced here:
    trial → approved | deprecated | banned
    approved → deprecated | banned
    deprecated → approved (reinstate) | banned
    banned → (terminal; only admin reinstate via direct DB)
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from .registry import _ensure_seeded

logger = logging.getLogger("okuro.stack")


_ALLOWED_TRANSITIONS = {
    "trial":       {"approved", "deprecated", "banned"},
    "approved":    {"deprecated", "banned"},
    "deprecated":  {"approved", "banned"},
    "banned":      set(),
}


# ---------------------------------------------------------------------------
# Entry mutations
# ---------------------------------------------------------------------------

def upsert_entry(
    entry_id: str,
    *,
    layer: str,
    name: str,
    version: str | None = None,
    status: str = "trial",
    rationale: str = "",
    use_when: list[str] | None = None,
    avoid_when: list[str] | None = None,
    depends_on: list[str] | None = None,
    docs_url: str | None = None,
    owner: str | None = None,
    replaces: str | None = None,
) -> dict:
    """Create or update an entry.

    Dep lists are authoritative — passing an empty list clears deps.
    Pass `depends_on=None` to leave existing deps untouched.
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    db.execute(
        "INSERT INTO stack_entries "
        "(id, layer, name, version, status, rationale, use_when, avoid_when, "
        " docs_url, owner, replaces, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  layer = excluded.layer, "
        "  name = excluded.name, "
        "  version = excluded.version, "
        "  status = excluded.status, "
        "  rationale = excluded.rationale, "
        "  use_when = excluded.use_when, "
        "  avoid_when = excluded.avoid_when, "
        "  docs_url = excluded.docs_url, "
        "  owner = excluded.owner, "
        "  replaces = excluded.replaces, "
        "  updated_at = datetime('now')",
        (
            entry_id, layer, name, version, status, rationale,
            json.dumps(use_when or []),
            json.dumps(avoid_when or []),
            docs_url, owner, replaces,
        ),
    )

    if depends_on is not None:
        db.execute("DELETE FROM stack_entry_deps WHERE entry_id = ?", (entry_id,))
        for dep in depends_on:
            try:
                db.execute(
                    "INSERT INTO stack_entry_deps (entry_id, depends_on) VALUES (?, ?)",
                    (entry_id, dep),
                )
            except Exception as exc:
                logger.warning("Skipping invalid dep %s → %s: %s", entry_id, dep, exc)

    db.conn.commit()

    from .registry import get_entry
    return get_entry(entry_id) or {}


def set_entry_status(entry_id: str, new_status: str, actor: str | None = None) -> dict:
    """Transition an entry's lifecycle status, enforcing the state machine.

    Records an audit row in stack_proposals with kind=<transition> and
    outcome=accepted.
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    row = db.fetchone("SELECT status FROM stack_entries WHERE id = ?", (entry_id,))
    if not row:
        return {"ok": False, "error": f"Entry not found: {entry_id}"}

    current = row["status"]
    if new_status == current:
        return {"ok": True, "entry_id": entry_id, "status": current, "noop": True}

    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        return {
            "ok": False,
            "error": f"Illegal transition {current} → {new_status}",
            "allowed_next": sorted(allowed),
        }

    db.execute(
        "UPDATE stack_entries SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (new_status, entry_id),
    )

    kind = {
        "approved":   "promote",
        "deprecated": "deprecate",
        "banned":     "ban",
    }.get(new_status, "new")

    db.execute(
        "INSERT INTO stack_proposals "
        "(id, entry_id, kind, proposed_by, outcome, decided_by, decided_at) "
        "VALUES (?, ?, ?, ?, 'accepted', ?, datetime('now'))",
        (str(uuid.uuid4()), entry_id, kind, actor, actor),
    )
    db.conn.commit()

    return {"ok": True, "entry_id": entry_id, "status": new_status, "from": current}


# ---------------------------------------------------------------------------
# Profile mutations
# ---------------------------------------------------------------------------

_ALLOWED_SCOPES = {"frontend", "backend", "fullstack", "agent", "other"}


def upsert_profile(
    name: str,
    *,
    label: str,
    description: str = "",
    scope: str = "fullstack",
    status: str = "active",
    entries: list[dict] | None = None,
) -> dict:
    """Create/update a profile. Passing `entries=None` leaves entries untouched;
    passing a list (possibly empty) replaces them.

    Each entry dict: ``{"id": "...", "role": "primary"|"secondary"}``.
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    if scope not in _ALLOWED_SCOPES:
        raise ValueError(
            f"Invalid scope '{scope}'. Allowed: {sorted(_ALLOWED_SCOPES)}"
        )

    db.execute(
        "INSERT INTO stack_profiles (name, label, description, scope, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(name) DO UPDATE SET "
        "  label = excluded.label, "
        "  description = excluded.description, "
        "  scope = excluded.scope, "
        "  status = excluded.status, "
        "  updated_at = datetime('now')",
        (name, label, description, scope, status),
    )

    if entries is not None:
        db.execute(
            "DELETE FROM stack_profile_entries WHERE profile_name = ?", (name,)
        )
        for pe in entries:
            eid = pe.get("id")
            role = pe.get("role", "primary")
            if not eid:
                continue
            try:
                db.execute(
                    "INSERT INTO stack_profile_entries (profile_name, entry_id, role) "
                    "VALUES (?, ?, ?)",
                    (name, eid, role),
                )
            except Exception as exc:
                logger.warning("Skipping invalid profile entry %s: %s", eid, exc)

    db.conn.commit()

    from .registry import get_profile
    return get_profile(name) or {}


def assign_project_profile(project_slug: str, profile_name: str | None) -> dict:
    """Bind (or unbind, if profile_name is None) a project to a profile."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    if profile_name is None:
        db.execute(
            "DELETE FROM stack_project_profile WHERE project_slug = ?",
            (project_slug,),
        )
        db.conn.commit()
        return {"ok": True, "project_slug": project_slug, "profile_name": None}

    profile = db.fetchone(
        "SELECT name FROM stack_profiles WHERE name = ?", (profile_name,)
    )
    if not profile:
        return {"ok": False, "error": f"Profile not found: {profile_name}"}

    db.execute(
        "INSERT INTO stack_project_profile (project_slug, profile_name, assigned_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(project_slug) DO UPDATE SET "
        "  profile_name = excluded.profile_name, "
        "  assigned_at = datetime('now')",
        (project_slug, profile_name),
    )
    db.conn.commit()
    return {"ok": True, "project_slug": project_slug, "profile_name": profile_name}


# ---------------------------------------------------------------------------
# Proposals (lifecycle workflow)
# ---------------------------------------------------------------------------

def propose(
    entry_id: str,
    *,
    kind: str = "new",
    proposed_by: str | None = None,
    rationale: str = "",
    payload: dict[str, Any] | None = None,
) -> dict:
    """File a proposal for human review.

    Kinds:
        new         — create a new entry (payload carries the draft fields)
        promote     — trial → approved
        deprecate   — approved → deprecated
        ban         — any → banned
        reinstate   — deprecated → approved
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    pid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO stack_proposals "
        "(id, entry_id, kind, proposed_by, rationale, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, entry_id, kind, proposed_by, rationale, json.dumps(payload or {})),
    )
    db.conn.commit()
    return {"ok": True, "proposal_id": pid, "entry_id": entry_id, "kind": kind}


def decide_proposal(proposal_id: str, outcome: str, decided_by: str | None = None) -> dict:
    """Accept or reject a proposal.

    Accepting a `new` proposal with a draft payload upserts the entry.
    Accepting lifecycle proposals (promote/deprecate/ban/reinstate) applies
    the transition.
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    if outcome not in ("accepted", "rejected"):
        return {"ok": False, "error": f"Invalid outcome: {outcome}"}

    row = db.fetchone(
        "SELECT id, entry_id, kind, payload, outcome FROM stack_proposals WHERE id = ?",
        (proposal_id,),
    )
    if not row:
        return {"ok": False, "error": f"Proposal not found: {proposal_id}"}
    if row["outcome"] != "pending":
        return {"ok": False, "error": f"Proposal already decided: {row['outcome']}"}

    if outcome == "accepted":
        kind = row["kind"]
        payload = json.loads(row["payload"] or "{}")

        if kind == "new":
            # payload should carry all upsert_entry kwargs.
            upsert_entry(
                row["entry_id"],
                layer=payload.get("layer", ""),
                name=payload.get("name", row["entry_id"]),
                version=payload.get("version"),
                status=payload.get("status", "trial"),
                rationale=payload.get("rationale", ""),
                use_when=payload.get("use_when"),
                avoid_when=payload.get("avoid_when"),
                depends_on=payload.get("depends_on"),
                docs_url=payload.get("docs_url"),
                owner=payload.get("owner"),
                replaces=payload.get("replaces"),
            )
        else:
            target = {
                "promote":   "approved",
                "deprecate": "deprecated",
                "ban":       "banned",
                "reinstate": "approved",
            }.get(kind)
            if target:
                set_entry_status(row["entry_id"], target, actor=decided_by)

    db.execute(
        "UPDATE stack_proposals SET outcome = ?, decided_by = ?, "
        "decided_at = datetime('now') WHERE id = ?",
        (outcome, decided_by, proposal_id),
    )
    db.conn.commit()
    return {"ok": True, "proposal_id": proposal_id, "outcome": outcome}


def list_proposals(outcome: str | None = None, limit: int = 50) -> list[dict]:
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    sql = "SELECT * FROM stack_proposals"
    params: list[Any] = []
    if outcome:
        sql += " WHERE outcome = ?"
        params.append(outcome)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = db.fetchall(sql, tuple(params))
    for r in rows:
        r["payload"] = json.loads(r.get("payload") or "{}")
    return rows


# ---------------------------------------------------------------------------
# Brand mutations
# ---------------------------------------------------------------------------

def upsert_brand(
    brand_id: str,
    *,
    name: str,
    description: str = "",
    status: str = "active",
    slots: dict[str, Any] | None = None,
    logo_corner: str | None = None,
) -> dict:
    """Create or update a brand.

    ``slots`` is a dict ``{slot_kind: ref_id | [ref_id, ...]}``. Passing
    ``slots=None`` leaves slots untouched; passing ``{}`` (or any dict)
    **replaces** the brand's slot assignments — no partial merges, matching
    YAML-round-trip semantics.

    Validation (slot_kind existence, cardinality, scope_filter) lives in
    ``validator.lint_brand`` — this writer is permissive so drafts can be
    saved incrementally. Call ``lint_brand`` before promoting to active.
    """
    from okuro.db import get_db
    from .registry import get_brand, list_slot_kinds
    _ensure_seeded()
    db = get_db()

    # Normalise the optional corner; None ⇒ leave untouched (insert defaults to
    # 'tl', update keeps the existing value via COALESCE).
    corner = (logo_corner or "").strip().lower() or None
    if corner is not None and corner not in {"tl", "tr", "bl", "br"}:
        corner = None

    db.execute(
        "INSERT INTO brands (id, name, description, status, logo_corner, updated_at) "
        "VALUES (?, ?, ?, ?, COALESCE(?, 'tl'), datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  name = excluded.name, "
        "  description = excluded.description, "
        "  status = excluded.status, "
        "  logo_corner = COALESCE(?, brands.logo_corner), "
        "  updated_at = datetime('now')",
        (brand_id, name, description, status, corner, corner),
    )

    if slots is not None:
        kind_meta = {sk["kind"]: sk for sk in list_slot_kinds()}
        db.execute("DELETE FROM brand_slots WHERE brand_id = ?", (brand_id,))
        for slot_kind, ref in slots.items():
            if ref is None:
                continue
            # Multi-cardinality slots accept a list; single cards are coerced.
            meta = kind_meta.get(slot_kind, {})
            if meta.get("cardinality") == "multi" and isinstance(ref, list):
                refs = ref
            elif isinstance(ref, list):
                # Caller passed a list for a single-cardinality slot — take
                # the first non-empty entry, log the rest.
                refs = ref[:1]
                if len(ref) > 1:
                    logger.warning(
                        "Slot %s is single-cardinality; dropped extras: %s",
                        slot_kind, ref[1:],
                    )
            else:
                refs = [ref]
            for pos, rid in enumerate(refs):
                if not rid:
                    continue
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO brand_slots "
                        "(brand_id, slot_kind, ref_id, position) "
                        "VALUES (?, ?, ?, ?)",
                        (brand_id, slot_kind, rid, pos),
                    )
                except Exception as exc:
                    logger.warning(
                        "Skipping invalid brand slot %s/%s=%s: %s",
                        brand_id, slot_kind, rid, exc,
                    )

    db.conn.commit()
    return get_brand(brand_id) or {}


def set_brand_slot(
    brand_id: str,
    slot_kind: str,
    ref_id: str | None,
    *,
    position: int = 0,
) -> dict:
    """Set a single slot on a brand (finer-grained than upsert_brand(slots=)).

    Passing ``ref_id=None`` clears the slot at the given position.
    """
    from okuro.db import get_db
    from .registry import get_brand
    _ensure_seeded()
    db = get_db()

    if ref_id is None:
        db.execute(
            "DELETE FROM brand_slots "
            "WHERE brand_id = ? AND slot_kind = ? AND position = ?",
            (brand_id, slot_kind, position),
        )
    else:
        db.execute(
            "INSERT OR REPLACE INTO brand_slots "
            "(brand_id, slot_kind, ref_id, position) VALUES (?, ?, ?, ?)",
            (brand_id, slot_kind, ref_id, position),
        )

    db.execute(
        "UPDATE brands SET updated_at = datetime('now') WHERE id = ?",
        (brand_id,),
    )
    db.conn.commit()
    return get_brand(brand_id) or {}


def delete_brand_slot(brand_id: str, slot_kind: str) -> dict:
    """Remove all positions of a slot on a brand."""
    return set_brand_slot(brand_id, slot_kind, None)


def assign_project_brand(project_slug: str, brand_id: str | None) -> dict:
    """Bind (or unbind, if brand_id is None) a project to a brand.

    Coexists with ``assign_project_profile``. At resolve time, a brand takes
    precedence over a profile binding.
    """
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    if brand_id is None:
        db.execute(
            "DELETE FROM stack_project_brand WHERE project_slug = ?",
            (project_slug,),
        )
        db.conn.commit()
        return {"ok": True, "project_slug": project_slug, "brand_id": None}

    exists = db.fetchone("SELECT id FROM brands WHERE id = ?", (brand_id,))
    if not exists:
        return {"ok": False, "error": f"Brand not found: {brand_id}"}

    db.execute(
        "INSERT INTO stack_project_brand (project_slug, brand_id, assigned_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(project_slug) DO UPDATE SET "
        "  brand_id = excluded.brand_id, "
        "  assigned_at = datetime('now')",
        (project_slug, brand_id),
    )
    db.conn.commit()
    return {"ok": True, "project_slug": project_slug, "brand_id": brand_id}


def delete_brand(brand_id: str) -> dict:
    """Delete a brand and its slot assignments. Project bindings cascade via
    FK (ON DELETE CASCADE on stack_project_brand → brands would be ideal;
    schema uses a plain REFERENCES so we clean bindings manually)."""
    from okuro.db import get_db
    _ensure_seeded()
    db = get_db()

    db.execute(
        "DELETE FROM stack_project_brand WHERE brand_id = ?", (brand_id,)
    )
    # brand_slots has ON DELETE CASCADE, so it clears automatically.
    cur = db.execute("DELETE FROM brands WHERE id = ?", (brand_id,))
    db.conn.commit()
    return {
        "ok": True,
        "brand_id": brand_id,
        "deleted": (cur.rowcount if cur is not None else 0) > 0,
    }
