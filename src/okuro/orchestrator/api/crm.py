# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: CRM API — companies + affiliations (hats) + connections + engagements
#   + engagement_resolve. HTTP surface for the relational person model (mig 056).
# index:
#   imports
#   router
#   models
#   def _localhost
#   def _crm_result
#   def list_companies / create_company / get_company / set_company_brand
#   def list_brands / list_projects
#   def list_affiliations / add_affiliation / set_primary_affiliation / remove_affiliation
#   def list_connections / add_connection / remove_connection
#   def list_engagements / add_engagement / remove_engagement
#   def resolve_engagement
# AGENT_HEADER_END -->
"""CRM API — relational person model (migration 056) over HTTP.

Deliberately under its OWN prefix ``/api/crm`` and in its own file so it adds
zero risk to the working ``/api/people`` surface: no route-capture overlap with
``/api/people/{person_id}``, no edits to people.py.

Split of concerns, mirroring people.py:
- **reads**  query the DB directly and return JSON (idiomatic here).
- **writes** delegate to ``okuro.peer.crm`` so validation stays single-sourced;
  the friendly result string is mapped to HTTP via ``_crm_result``, then the
  affected list is re-queried so the UI always renders real persisted state.

Mutating endpoints are localhost-only, matching people.py / keyring / cortex.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/crm", tags=["crm"])


# ── helpers ──────────────────────────────────────────────────────────


def _localhost(request: Request) -> None:
    """Reuse main's loopback guard — same pattern as people.py."""
    from okuro.orchestrator.api.main import _require_loopback

    _require_loopback(request)


# Substrings that mark a peer.crm function's friendly return as a failure.
# The crm functions never raise — they return a human string — so the API maps
# those to HTTP. State is never corrupted on a miss: writes re-query and return
# the real list, so a mis-classified message can only mislabel, not mutate.
_NOT_FOUND_MARKERS = ("not found",)
_BAD_REQUEST_MARKERS = ("Unknown ", "must be", "do not belong", "does not belong",
                        "Create it first", "Add it first", "nothing to merge")


def _crm_result(msg: str) -> str:
    """Raise HTTPException if a peer.crm result string signals failure."""
    low = msg.lower()
    if any(m in low for m in _NOT_FOUND_MARKERS):
        raise HTTPException(404, msg)
    if any(m.lower() in low for m in _BAD_REQUEST_MARKERS):
        raise HTTPException(400, msg)
    return msg


# ── models ───────────────────────────────────────────────────────────


class CompanyIn(BaseModel):
    name: str = Field(..., min_length=1)
    brand_id: Optional[str] = None
    domain: Optional[str] = None
    notes: Optional[str] = None
    company_id: Optional[str] = None


class CompanyBrandIn(BaseModel):
    brand_id: str = Field(default="", description="Brand id; empty string clears.")


class AffiliationIn(BaseModel):
    company_id: str = Field(..., min_length=1)
    role: Optional[str] = None
    role_class: Optional[str] = None
    is_primary: bool = False
    status: str = "active"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


class ConnectionIn(BaseModel):
    connection_type: str = Field(..., min_length=1)
    context: Optional[str] = None
    company_id: Optional[str] = None
    notes: Optional[str] = None


class EngagementIn(BaseModel):
    project_slug: str = Field(..., min_length=1)
    affiliation_id: Optional[str] = None
    company_id: Optional[str] = None
    notes: Optional[str] = None


# ── companies ────────────────────────────────────────────────────────


@router.get("/companies")
def list_companies() -> dict:
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """SELECT c.id, c.name, c.brand_id, c.domain, c.notes,
                  (SELECT COUNT(*) FROM affiliations a
                   WHERE a.company_id = c.id AND a.status = 'active') AS members
           FROM companies c WHERE c.active = 1 ORDER BY c.name COLLATE NOCASE"""
    )
    return {"companies": [dict(r) for r in rows]}


@router.post("/companies", status_code=201)
def create_company(payload: CompanyIn, request: Request) -> dict:
    _localhost(request)
    from okuro.peer.crm import company_add

    msg = _crm_result(company_add(
        name=payload.name, brand_id=payload.brand_id or None,
        domain=payload.domain, notes=payload.notes,
        company_id=payload.company_id or None,
    ))
    return {"message": msg, **list_companies()}


@router.get("/companies/{company_id}")
def get_company(company_id: str) -> dict:
    from okuro.db import get_db

    db = get_db()
    c = db.fetchone("SELECT * FROM companies WHERE id = ?", (company_id,))
    if not c:
        raise HTTPException(404, f"Company not found: {company_id}")
    members = db.fetchall(
        """SELECT a.id, a.role, a.role_class, a.is_primary, a.person_id,
                  p.display_name
           FROM affiliations a JOIN persons p ON p.id = a.person_id
           WHERE a.company_id = ? AND a.status = 'active'
           ORDER BY a.is_primary DESC, p.display_name""",
        (company_id,),
    )
    return {"company": dict(c), "members": [dict(m) for m in members]}


@router.put("/companies/{company_id}/brand")
def set_company_brand(company_id: str, payload: CompanyBrandIn, request: Request) -> dict:
    _localhost(request)
    from okuro.peer.crm import company_set_brand

    msg = _crm_result(company_set_brand(company_id, payload.brand_id))
    return {"message": msg}


@router.get("/brands")
def list_brands() -> dict:
    """Brand options for the company → design-profile picker."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall("SELECT id, name FROM brands ORDER BY name COLLATE NOCASE")
    return {"brands": [dict(r) for r in rows]}


@router.get("/projects")
def list_projects() -> dict:
    """Active project slugs for the engagement picker."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT id, name FROM projects WHERE active = 1 ORDER BY id COLLATE NOCASE"
    )
    return {"projects": [dict(r) for r in rows]}


# ── affiliations (hats) ──────────────────────────────────────────────


@router.get("/people/{person_id}/affiliations")
def list_affiliations(person_id: str) -> dict:
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """SELECT a.id, a.company_id, a.role, a.role_class, a.status,
                  a.is_primary, a.started_at, a.ended_at, c.name AS company_name
           FROM affiliations a JOIN companies c ON c.id = a.company_id
           WHERE a.person_id = ?
           ORDER BY a.is_primary DESC, c.name COLLATE NOCASE""",
        (person_id,),
    )
    return {"affiliations": [dict(r) for r in rows]}


@router.post("/people/{person_id}/affiliations", status_code=201)
def add_affiliation(person_id: str, payload: AffiliationIn, request: Request) -> dict:
    _localhost(request)
    from okuro.peer.crm import affiliation_add

    msg = _crm_result(affiliation_add(
        person_id=person_id, company_id=payload.company_id, role=payload.role,
        role_class=payload.role_class, is_primary=payload.is_primary,
        status=payload.status, started_at=payload.started_at,
        ended_at=payload.ended_at,
    ))
    return {"message": msg, **list_affiliations(person_id)}


@router.put("/people/{person_id}/affiliations/{affiliation_id}/primary")
def set_primary_affiliation(person_id: str, affiliation_id: str, request: Request) -> dict:
    _localhost(request)
    from okuro.peer.crm import affiliation_set_primary

    msg = _crm_result(affiliation_set_primary(affiliation_id))
    return {"message": msg, **list_affiliations(person_id)}


@router.delete("/people/{person_id}/affiliations/{affiliation_id}")
def remove_affiliation(person_id: str, affiliation_id: str, request: Request) -> dict:
    _localhost(request)
    from okuro.peer.crm import affiliation_remove

    msg = _crm_result(affiliation_remove(affiliation_id))
    return {"message": msg, **list_affiliations(person_id)}


# ── connections (person → user) ──────────────────────────────────────


@router.get("/people/{person_id}/connections")
def list_connections(person_id: str) -> dict:
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """SELECT cn.id, cn.connection_type, cn.context, cn.company_id, cn.notes,
                  c.name AS company_name
           FROM connections cn LEFT JOIN companies c ON c.id = cn.company_id
           WHERE cn.person_id = ? ORDER BY cn.created_at""",
        (person_id,),
    )
    return {"connections": [dict(r) for r in rows]}


@router.post("/people/{person_id}/connections", status_code=201)
def add_connection(person_id: str, payload: ConnectionIn, request: Request) -> dict:
    _localhost(request)
    from okuro.peer.crm import connection_add

    msg = _crm_result(connection_add(
        person_id=person_id, connection_type=payload.connection_type,
        context=payload.context, company_id=payload.company_id or None,
        notes=payload.notes,
    ))
    return {"message": msg, **list_connections(person_id)}


@router.delete("/people/{person_id}/connections/{connection_id}")
def remove_connection(person_id: str, connection_id: str, request: Request) -> dict:
    _localhost(request)
    from okuro.peer.crm import connection_remove

    msg = _crm_result(connection_remove(connection_id))
    return {"message": msg, **list_connections(person_id)}


# ── engagements (project × hat × company) ────────────────────────────


@router.get("/people/{person_id}/engagements")
def list_engagements(person_id: str) -> dict:
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """SELECT e.id, e.project_slug, e.affiliation_id, e.company_id, e.notes,
                  a.role AS hat_role, c.name AS company_name, c.brand_id
           FROM engagements e
           LEFT JOIN affiliations a ON a.id = e.affiliation_id
           LEFT JOIN companies c ON c.id = e.company_id
           WHERE e.person_id = ? ORDER BY e.project_slug""",
        (person_id,),
    )
    return {"engagements": [dict(r) for r in rows]}


@router.post("/people/{person_id}/engagements", status_code=201)
def add_engagement(person_id: str, payload: EngagementIn, request: Request) -> dict:
    _localhost(request)
    from okuro.peer.crm import engagement_add

    msg = _crm_result(engagement_add(
        project_slug=payload.project_slug, person_id=person_id,
        affiliation_id=payload.affiliation_id or None,
        company_id=payload.company_id or None, notes=payload.notes,
    ))
    return {"message": msg, **list_engagements(person_id)}


@router.delete("/people/{person_id}/engagements/{engagement_id}")
def remove_engagement(person_id: str, engagement_id: str, request: Request) -> dict:
    _localhost(request)
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM engagements WHERE id = ?", (engagement_id,)):
        raise HTTPException(404, f"Engagement not found: {engagement_id}")
    db.execute("DELETE FROM engagements WHERE id = ?", (engagement_id,))
    db.conn.commit()
    return {"message": f"Removed engagement {engagement_id}.", **list_engagements(person_id)}


@router.get("/people/{person_id}/resolve")
def resolve_engagement(person_id: str, project: str) -> dict:
    """engagement_resolve preview — IA (person) + design (company) + hat."""
    from okuro.peer.crm import engagement_resolve

    markdown = engagement_resolve(project_slug=project, person_id=person_id)
    if markdown.startswith("Person not found"):
        raise HTTPException(404, markdown)
    return {"person_id": person_id, "project": project, "markdown": markdown}
