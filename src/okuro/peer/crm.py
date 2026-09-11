# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Relational person CRM — companies, affiliations (hats), connections
#   (user edges), engagements (project x hat x company) + engagement_resolve.
# index:
#   def _gen_id
#   def company_add
#   def company_get
#   def company_list
#   def company_set_brand
#   def affiliation_add
#   def affiliation_list
#   def affiliation_set_primary
#   def affiliation_remove
#   def connection_add
#   def connection_list
#   def connection_remove
#   def engagement_add
#   def engagement_list
#   def engagement_resolve
# AGENT_HEADER_END -->
"""Relational person CRM (peer 仲, phase 2/3).

Sits on top of migration 056. The flat ``persons`` row stays as identity +
global cognitive baseline; this module adds the relations that make a contact
real:

- **companies**   — entity that owns a design identity (``brand_id``).
- **affiliations**— person→company *hats* (CEO, board member, stakeholder…).
- **connections** — person→user edges (co-shareholder, customer, vendor…).
- **engagements** — project × hat × company; the join ``engagement_resolve``
  reads to deliver, for one project: information architecture from the PERSON's
  cognitive profile + visual design from the COMPANY's brand.

Enum-like columns (``affiliation.role_class``, ``connection.connection_type``)
are validated here at the writer, mirroring the schema's "document the set in
code" choice. ``role_class`` is checked against ROLE_DEFAULT_SLIDERS so a hat
can seed slider priors.
"""

import json
import re
import uuid

from okuro.peer.cognitive_profile import ROLE_DEFAULT_SLIDERS

# connection_type is open-ended but we validate against a known set to catch
# typos; unknown values are accepted with a hint rather than rejected, so the
# vocabulary can grow without a code change blocking the user.
KNOWN_CONNECTION_TYPES = (
    "co-shareholder", "business-partner", "customer", "client", "vendor",
    "supplier", "board-peer", "investor", "advisor", "colleague",
    "friend", "family", "acquaintance", "other",
)


def _gen_id(prefix: str) -> str:
    """Short surrogate id, e.g. ``aff_3f9a1c2b4d5e``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ── companies ──────────────────────────────────────────────────────────────


def company_add(name: str, brand_id: str = None, domain: str = None,
                 notes: str = None, company_id: str = None) -> str:
    """Add or upsert a company. ``brand_id`` (design profile) may be NULL until
    brands mature — the visual-design slot degrades gracefully."""
    from okuro.db import get_db

    db = get_db()
    cid = company_id or _slugify(name)

    if brand_id:
        row = db.fetchone("SELECT id FROM brands WHERE id = ?", (brand_id,))
        if not row:
            return f"Brand not found: `{brand_id}`. Create it first or omit brand_id."

    db.execute(
        """INSERT INTO companies (id, name, brand_id, domain, notes)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (id) DO UPDATE SET
               name = excluded.name,
               brand_id = COALESCE(excluded.brand_id, companies.brand_id),
               domain = COALESCE(excluded.domain, companies.domain),
               notes = COALESCE(excluded.notes, companies.notes),
               updated_at = datetime('now')""",
        (cid, name, brand_id, domain, notes),
    )
    db.conn.commit()
    return f"Company added: **{name}** (`{cid}`)" + (f" → brand `{brand_id}`" if brand_id else " (no brand yet)")


def company_get(company_id: str) -> str:
    """Company profile + its people (affiliations)."""
    from okuro.db import get_db

    db = get_db()
    c = db.fetchone("SELECT * FROM companies WHERE id = ?", (company_id,))
    if not c:
        return f"Company not found: {company_id}"

    lines = [f"# {c['name']}  (`{c['id']}`)"]
    lines.append(f"- **Brand:** {c['brand_id'] or '— (none yet)'}")
    if c.get("domain"):
        lines.append(f"- **Domain:** {c['domain']}")
    if c.get("notes"):
        lines.append(f"- **Notes:** {c['notes']}")

    members = db.fetchall(
        """SELECT a.id, a.role, a.role_class, a.is_primary, p.display_name
           FROM affiliations a JOIN persons p ON p.id = a.person_id
           WHERE a.company_id = ? AND a.status = 'active'
           ORDER BY a.is_primary DESC, p.display_name""",
        (company_id,),
    )
    if members:
        lines.append("\n## People")
        lines.append("| Person | Role | Primary |")
        lines.append("|---|---|---|")
        for m in members:
            star = "★" if m["is_primary"] else ""
            lines.append(f"| {m['display_name']} | {m['role'] or '-'} | {star} |")
    return "\n".join(lines)


def company_list() -> str:
    """All companies with member counts."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """SELECT c.id, c.name, c.brand_id, c.domain,
                  (SELECT COUNT(*) FROM affiliations a
                   WHERE a.company_id = c.id AND a.status = 'active') AS members
           FROM companies c WHERE c.active = 1 ORDER BY c.name"""
    )
    if not rows:
        return "No companies yet."
    lines = ["| ID | Name | Brand | Domain | People |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['name']} | {r['brand_id'] or '-'} "
            f"| {r['domain'] or '-'} | {r['members']} |"
        )
    return "\n".join(lines)


def company_set_brand(company_id: str, brand_id: str) -> str:
    """Bind a company to a design profile (brand). Pass empty string to clear."""
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM companies WHERE id = ?", (company_id,)):
        return f"Company not found: {company_id}"
    bid = brand_id or None
    if bid and not db.fetchone("SELECT id FROM brands WHERE id = ?", (bid,)):
        return f"Brand not found: `{bid}`."
    db.execute(
        "UPDATE companies SET brand_id = ?, updated_at = datetime('now') WHERE id = ?",
        (bid, company_id),
    )
    db.conn.commit()
    return f"`{company_id}` → brand {('`' + bid + '`') if bid else 'cleared'}."


# ── affiliations (person → company hats) ────────────────────────────────────


def affiliation_add(person_id: str, company_id: str, role: str = None,
                    role_class: str = None, is_primary: bool = False,
                    status: str = "active", started_at: str = None,
                    ended_at: str = None) -> str:
    """Add a hat: this person holds ``role`` at ``company``.

    ``role_class`` (optional) maps to ROLE_DEFAULT_SLIDERS so the hat can seed
    slider priors; unknown classes are rejected to catch typos. ``is_primary``
    makes this the headline hat — any existing primary for the person is
    demoted (the schema's partial unique index would otherwise reject it).
    """
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM persons WHERE id = ?", (person_id,)):
        return f"Person not found: {person_id}"
    if not db.fetchone("SELECT id FROM companies WHERE id = ?", (company_id,)):
        return f"Company not found: {company_id}. Add it first with company_add."
    if status not in ("active", "past"):
        return "status must be 'active' or 'past'."
    if role_class and role_class.lower() not in ROLE_DEFAULT_SLIDERS:
        allowed = ", ".join(sorted(ROLE_DEFAULT_SLIDERS))
        return f"Unknown role_class `{role_class}`. Allowed: {allowed}."

    aid = _gen_id("aff")
    with db.write():
        if is_primary:
            db.conn.execute(
                "UPDATE affiliations SET is_primary = 0 WHERE person_id = ?",
                (person_id,),
            )
        db.conn.execute(
            """INSERT INTO affiliations
               (id, person_id, company_id, role, role_class, status,
                is_primary, started_at, ended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (aid, person_id, company_id, role,
             role_class.lower() if role_class else None,
             status, 1 if is_primary else 0, started_at, ended_at),
        )
    star = " ★primary" if is_primary else ""
    return f"Hat added (`{aid}`): {person_id} → {role or '?'} @ {company_id}{star}"


def affiliation_list(person_id: str = None, company_id: str = None) -> str:
    """List hats, filtered by person and/or company."""
    from okuro.db import get_db

    db = get_db()
    q = ("""SELECT a.id, a.role, a.role_class, a.status, a.is_primary,
                   p.display_name AS person, c.name AS company
            FROM affiliations a
            JOIN persons p ON p.id = a.person_id
            JOIN companies c ON c.id = a.company_id WHERE 1=1""")
    params = []
    if person_id:
        q += " AND a.person_id = ?"
        params.append(person_id)
    if company_id:
        q += " AND a.company_id = ?"
        params.append(company_id)
    q += " ORDER BY a.is_primary DESC, p.display_name"
    rows = db.fetchall(q, tuple(params))
    if not rows:
        return "No affiliations found."
    lines = ["| ID | Person | Role | Company | Class | Status | Primary |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        star = "★" if r["is_primary"] else ""
        lines.append(
            f"| {r['id']} | {r['person']} | {r['role'] or '-'} | {r['company']} "
            f"| {r['role_class'] or '-'} | {r['status']} | {star} |"
        )
    return "\n".join(lines)


def affiliation_set_primary(affiliation_id: str) -> str:
    """Make this hat the person's headline hat; demote any other primary."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT person_id FROM affiliations WHERE id = ?", (affiliation_id,))
    if not row:
        return f"Affiliation not found: {affiliation_id}"
    with db.write():
        db.conn.execute(
            "UPDATE affiliations SET is_primary = 0 WHERE person_id = ?",
            (row["person_id"],),
        )
        db.conn.execute(
            "UPDATE affiliations SET is_primary = 1, updated_at = datetime('now') WHERE id = ?",
            (affiliation_id,),
        )
    return f"`{affiliation_id}` is now the primary hat."


def affiliation_remove(affiliation_id: str) -> str:
    """Delete a hat."""
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM affiliations WHERE id = ?", (affiliation_id,)):
        return f"Affiliation not found: {affiliation_id}"
    db.execute("DELETE FROM affiliations WHERE id = ?", (affiliation_id,))
    db.conn.commit()
    return f"Removed affiliation `{affiliation_id}`."


# ── connections (person → user) ─────────────────────────────────────────────


def connection_add(person_id: str, connection_type: str, context: str = None,
                   company_id: str = None, notes: str = None) -> str:
    """Record YOUR relationship to a person (e.g. co-shareholder @ the joint venture)."""
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM persons WHERE id = ?", (person_id,)):
        return f"Person not found: {person_id}"
    if company_id and not db.fetchone("SELECT id FROM companies WHERE id = ?", (company_id,)):
        return f"Company not found: {company_id}."

    hint = ""
    if connection_type not in KNOWN_CONNECTION_TYPES:
        hint = f" (note: `{connection_type}` is not in the known set — accepted anyway)"

    cid = _gen_id("con")
    db.execute(
        """INSERT INTO connections
           (id, person_id, connection_type, context, company_id, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cid, person_id, connection_type, context, company_id, notes),
    )
    db.conn.commit()
    ctx = f" ({context})" if context else ""
    return f"Connection added (`{cid}`): you ↔ {person_id} — {connection_type}{ctx}{hint}"


def connection_list(person_id: str = None) -> str:
    """List your connection edges, optionally for one person."""
    from okuro.db import get_db

    db = get_db()
    q = ("""SELECT cn.id, cn.connection_type, cn.context, cn.company_id,
                   p.display_name AS person
            FROM connections cn JOIN persons p ON p.id = cn.person_id WHERE 1=1""")
    params = []
    if person_id:
        q += " AND cn.person_id = ?"
        params.append(person_id)
    q += " ORDER BY p.display_name"
    rows = db.fetchall(q, tuple(params))
    if not rows:
        return "No connections found."
    lines = ["| ID | Person | Type | Context | Company |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['person']} | {r['connection_type']} "
            f"| {r['context'] or '-'} | {r['company_id'] or '-'} |"
        )
    return "\n".join(lines)


def connection_remove(connection_id: str) -> str:
    """Delete a connection edge."""
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM connections WHERE id = ?", (connection_id,)):
        return f"Connection not found: {connection_id}"
    db.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
    db.conn.commit()
    return f"Removed connection `{connection_id}`."


# ── engagements (project × hat × company) ───────────────────────────────────


def engagement_add(project_slug: str, person_id: str, affiliation_id: str = None,
                   company_id: str = None, notes: str = None) -> str:
    """Bind a person (wearing a hat) to a project under a company's design.

    If ``company_id`` is omitted it's inferred from the affiliation. One
    engagement per (project, person) — re-adding updates the hat/company.
    """
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM projects WHERE id = ?", (project_slug,)):
        return f"Project not found: {project_slug}"
    if not db.fetchone("SELECT id FROM persons WHERE id = ?", (person_id,)):
        return f"Person not found: {person_id}"

    if affiliation_id:
        aff = db.fetchone(
            "SELECT id, person_id, company_id FROM affiliations WHERE id = ?",
            (affiliation_id,),
        )
        if not aff:
            return f"Affiliation not found: {affiliation_id}"
        if aff["person_id"] != person_id:
            return f"Affiliation `{affiliation_id}` does not belong to {person_id}."
        company_id = company_id or aff["company_id"]
    if company_id and not db.fetchone("SELECT id FROM companies WHERE id = ?", (company_id,)):
        return f"Company not found: {company_id}."

    eid = _gen_id("eng")
    db.execute(
        """INSERT INTO engagements
           (id, project_slug, person_id, affiliation_id, company_id, notes)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (project_slug, person_id) DO UPDATE SET
               affiliation_id = excluded.affiliation_id,
               company_id = excluded.company_id,
               notes = COALESCE(excluded.notes, engagements.notes),
               updated_at = datetime('now')""",
        (eid, project_slug, person_id, affiliation_id, company_id, notes),
    )
    db.conn.commit()
    return f"Engagement set: {person_id} on `{project_slug}`" + (
        f" as hat `{affiliation_id}`" if affiliation_id else "")


def engagement_list(project_slug: str = None, person_id: str = None) -> str:
    """List engagements, filtered by project and/or person."""
    from okuro.db import get_db

    db = get_db()
    q = ("""SELECT e.id, e.project_slug, p.display_name AS person,
                   a.role AS hat, c.name AS company
            FROM engagements e
            JOIN persons p ON p.id = e.person_id
            LEFT JOIN affiliations a ON a.id = e.affiliation_id
            LEFT JOIN companies c ON c.id = e.company_id WHERE 1=1""")
    params = []
    if project_slug:
        q += " AND e.project_slug = ?"
        params.append(project_slug)
    if person_id:
        q += " AND e.person_id = ?"
        params.append(person_id)
    q += " ORDER BY e.project_slug, person"
    rows = db.fetchall(q, tuple(params))
    if not rows:
        return "No engagements found."
    lines = ["| ID | Project | Person | Hat | Company |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['project_slug']} | {r['person']} "
            f"| {r['hat'] or '-'} | {r['company'] or '-'} |"
        )
    return "\n".join(lines)


def engagement_resolve(project_slug: str, person_id: str) -> str:
    """THE payoff: for a project + person, return the delivery context —

    - **IA**   : the person's anonymized cognitive lens (+ company overlay) —
      drives structure and wording.
    - **design**: the company's brand composition — drives visual language.
    - **hat**  : which role is in play (disambiguates a multi-hat person).

    Resolution is tolerant: a missing engagement falls back to the person's
    primary hat; a company with no brand returns design=None with a note.
    """
    from okuro.db import get_db
    from okuro.peer.cognitive_profile import cognitive_profile_for_llm

    db = get_db()
    person = db.fetchone(
        "SELECT id, display_name FROM persons WHERE id = ? AND active = 1",
        (person_id,),
    )
    if not person:
        return f"Person not found: {person_id}"

    eng = db.fetchone(
        "SELECT affiliation_id, company_id FROM engagements "
        "WHERE project_slug = ? AND person_id = ?",
        (project_slug, person_id),
    )
    affiliation_id = eng["affiliation_id"] if eng else None
    company_id = eng["company_id"] if eng else None
    source = "engagement"

    # Fallback: no engagement → use the person's primary hat.
    if not affiliation_id:
        prim = db.fetchone(
            "SELECT id, company_id FROM affiliations "
            "WHERE person_id = ? AND is_primary = 1",
            (person_id,),
        )
        if prim:
            affiliation_id = prim["id"]
            company_id = company_id or prim["company_id"]
            source = "primary-hat fallback (no engagement for this project)"

    hat_role = None
    if affiliation_id:
        hat = db.fetchone("SELECT role FROM affiliations WHERE id = ?", (affiliation_id,))
        hat_role = hat["role"] if hat else None

    company = None
    if company_id:
        company = db.fetchone("SELECT id, name, brand_id FROM companies WHERE id = ?", (company_id,))

    # IA — cognitive lens, with optional per-company overlay applied.
    # The lens is resolved INSIDE the firewall. This used to re-read
    # persons.cognitive raw — a second read path around the very function
    # that exists to be the only one — and it silently dropped any override
    # for an axis not already present in the base vector.
    ia = cognitive_profile_for_llm(person_id, lens_key=company_id)
    base = cognitive_profile_for_llm(person_id) if company_id else ia
    overlay_applied = bool(
        company_id and ia and base.get("sliders") != ia.get("sliders")
    )

    # design — resolve the company's brand composition.
    design = None
    design_note = None
    if company and company["brand_id"]:
        try:
            from okuro.stack.registry import resolve_brand
            design = resolve_brand(company["brand_id"])
        except Exception as exc:  # noqa: BLE001
            design_note = f"brand resolve failed: {exc}"
    elif company:
        design_note = f"company `{company['id']}` has no brand yet — visual layer not bound"
    else:
        design_note = "no company resolved — cannot bind a design profile"

    # ── render ──
    lines = [f"# Engagement resolve — {person['display_name']} on `{project_slug}`",
             f"*resolution source: {source}*", ""]
    lines.append(f"- **Hat:** {hat_role or '— (no hat)'}"
                 + (f"  ·  company: {company['name']}" if company else ""))
    lines.append("")
    lines.append("## Information architecture (from PERSON)")
    if ia:
        sliders = ia.get("sliders") or {}
        if sliders:
            from okuro.peer.cognitive_profile import format_sliders_for_prompt

            lines.append("- **Sliders" + (" + overlay" if overlay_applied else "") + ":** "
                         + format_sliders_for_prompt(ia))
            conf = ia.get("slider_confidence") or {}
            low = [k for k, c in conf.items() if isinstance(c, (int, float)) and c < 0.6]
            if low:
                lines.append(f"- **Unconfirmed axes (priors, not observed):** {', '.join(low)}")
        for key in ("formality_baseline", "jargon_tolerance", "decision_style",
                    "attention_span", "learning_style"):
            if ia.get(key):
                lines.append(f"- **{key.replace('_', ' ').title()}:** {ia[key]}")
        fmt = ia.get("format_preferences") or []
        if fmt:
            lines.append(f"- **Format preferences:** {', '.join(map(str, fmt))}")
    else:
        lines.append("- *No cognitive profile yet — translate with sender defaults.*")

    lines.append("")
    lines.append("## Visual design (from COMPANY)")
    if design:
        brand_id = company["brand_id"]
        slots = design.get("slots") or design.get("resolved_slots") or {}
        lines.append(f"- **Brand:** `{brand_id}` ({company['name']})")
        if isinstance(slots, dict) and slots:
            lines.append(f"- **Slots:** {', '.join(slots.keys())}")
        lines.append("- *(full composition via `stack_brand_get`)*")
    else:
        lines.append(f"- *{design_note}*")

    return "\n".join(lines)
