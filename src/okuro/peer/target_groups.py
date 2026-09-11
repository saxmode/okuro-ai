# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Target-audience registry — first-class groups a prism deck is tailored
#   for (board / top-management / engineers / designers …). Thin entity on top of
#   migration 074: membership resolves via affiliations on role_class (+company),
#   plus optional explicit overrides. Empty/template group → generic deck from the
#   role_class slider preset.
# index:
#   def target_group_add
#   def target_group_get
#   def target_group_list
#   def target_group_add_member
#   def target_group_remove_member
#   def seed_standard_groups
#   def resolve_audience
# AGENT_HEADER_END -->
"""Target-group registry (peer 仲).

A **target group** is the audience a deck is built for. Sits on companies +
affiliations (migration 056). Two flavours:

- **standard template** (``company_id`` NULL) — reusable archetype (``board``,
  ``designers``) carrying only a slider seed. Renders a GENERIC deck.
- **concrete group** (``company_id`` set) — e.g. ``board-of-mobiliar``. Members
  are the company's active affiliations whose ``role_class`` matches, plus any
  explicit overrides, merged into one audience lens.

``role_class`` is validated at the writer against ``ROLE_DEFAULT_SLIDERS`` (same
choice as ``affiliation_add``). ``resolve_audience`` is the payoff the prism
generator consumes: a merged cognitive lens for the whole group, or the preset
lens + ``generic=True`` when there are no concrete members yet.
"""

import json
import re
import uuid
from typing import Optional

from okuro.peer.cognitive_profile import ROLE_DEFAULT_SLIDERS, role_default_sliders
from okuro.sense.retrieval import candidate_pool

# Standard groups seeded at install. company_id is NULL — these are reusable
# templates; a concrete per-company group is created by target_group_add with a
# company_id. label is the human name; role_class seeds the slider prior.
STANDARD_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("board", "Board / Verwaltungsrat", "board"),
    ("top-management", "Top Management", "top_management"),
    ("engineers", "Engineers", "engineer"),
    ("designers", "Designers", "designer"),
    ("product", "Product", "product_manager"),
    ("sales", "Sales", "sales"),
    ("marketing", "Marketing", "marketer"),
)


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ── writers ─────────────────────────────────────────────────────────────────


def target_group_add(name: str, role_class: str = None, company_id: str = None,
                     kind: str = "custom", notes: str = None,
                     group_id: str = None) -> str:
    """Add or upsert a target group.

    ``role_class`` (validated against ROLE_DEFAULT_SLIDERS) seeds the slider
    prior and drives affiliation-based membership. ``company_id`` NULL makes a
    reusable template; set it for a concrete group like ``board-of-mobiliar``.
    """
    from okuro.db import get_db

    db = get_db()
    gid = group_id or _slugify(name)

    if kind not in ("standard", "custom"):
        return "kind must be 'standard' or 'custom'."
    if role_class and role_class.lower() not in ROLE_DEFAULT_SLIDERS:
        allowed = ", ".join(sorted(ROLE_DEFAULT_SLIDERS))
        return f"Unknown role_class `{role_class}`. Allowed: {allowed}."
    if company_id and not db.fetchone("SELECT id FROM companies WHERE id = ?", (company_id,)):
        return f"Company not found: {company_id}. Add it first with company_add."

    rc = role_class.lower() if role_class else None
    seed = json.dumps(role_default_sliders(rc)) if rc else None

    db.execute(
        """INSERT INTO target_groups
           (id, name, kind, company_id, role_class, seed_sliders, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (id) DO UPDATE SET
               name = excluded.name,
               kind = excluded.kind,
               company_id = COALESCE(excluded.company_id, target_groups.company_id),
               role_class = COALESCE(excluded.role_class, target_groups.role_class),
               seed_sliders = COALESCE(excluded.seed_sliders, target_groups.seed_sliders),
               notes = COALESCE(excluded.notes, target_groups.notes),
               updated_at = datetime('now')""",
        (gid, name, kind, company_id, rc, seed, notes),
    )
    db.conn.commit()
    reembed_group(gid)
    scope = f"@ {company_id}" if company_id else "(template)"
    return f"Target group added: **{name}** (`{gid}`) {scope}" + (
        f" · class `{rc}`" if rc else "")


def target_group_add_member(group_id: str, person_id: str) -> str:
    """Explicitly pull a person into a group (override; not affiliation-derived)."""
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM target_groups WHERE id = ?", (group_id,)):
        return f"Target group not found: {group_id}"
    if not db.fetchone("SELECT id FROM persons WHERE id = ?", (person_id,)):
        return f"Person not found: {person_id}"
    db.execute(
        """INSERT INTO target_group_members (id, group_id, person_id)
           VALUES (?, ?, ?)
           ON CONFLICT (group_id, person_id) DO NOTHING""",
        (_gen_id("tgm"), group_id, person_id),
    )
    db.conn.commit()
    reembed_group(group_id)
    return f"Added {person_id} to `{group_id}`."


def target_group_remove_member(group_id: str, person_id: str) -> str:
    """Remove an explicit member override (does not affect affiliation-derived)."""
    from okuro.db import get_db

    db = get_db()
    db.execute(
        "DELETE FROM target_group_members WHERE group_id = ? AND person_id = ?",
        (group_id, person_id),
    )
    db.conn.commit()
    reembed_group(group_id)
    return f"Removed {person_id} from `{group_id}`."


# ── membership + audience resolution ────────────────────────────────────────


def _member_ids(db, group: dict) -> list[str]:
    """Resolve members: affiliation-derived (role_class within company) ∪ explicit."""
    ids: list[str] = []
    company_id = group["company_id"]
    role_class = group["role_class"]
    if company_id and role_class:
        rows = db.fetchall(
            """SELECT DISTINCT a.person_id FROM affiliations a
               JOIN persons p ON p.id = a.person_id
               WHERE a.company_id = ? AND a.role_class = ?
                 AND a.status = 'active' AND p.active = 1""",
            (company_id, role_class),
        )
        ids.extend(r["person_id"] for r in rows)
    explicit = db.fetchall(
        """SELECT m.person_id FROM target_group_members m
           JOIN persons p ON p.id = m.person_id
           WHERE m.group_id = ? AND p.active = 1""",
        (group["id"],),
    )
    for r in explicit:
        if r["person_id"] not in ids:
            ids.append(r["person_id"])
    return ids


def resolve_audience(group_id: str) -> dict:
    """THE payoff the prism generator consumes.

    Returns a structured audience lens for the whole group:
      - ``members``: [{person_id, display_name}]
      - ``sliders``: merged (mean, rounded) across member cognitive lenses, OR
        the role_class preset when there are no concrete members
      - ``topic_interests``: union of member interests (weight = max)
      - ``generic``: True when built from the preset (no live members) — the
        caller MUST tell the user the deck is a general one for the archetype
      - ``role_class``, ``company_id``, ``name``
    """
    from okuro.db import get_db
    from okuro.peer.cognitive_profile import cognitive_profile_for_llm

    db = get_db()
    g = db.fetchone("SELECT * FROM target_groups WHERE id = ? AND active = 1", (group_id,))
    if not g:
        return {"error": f"Target group not found: {group_id}"}

    member_ids = _member_ids(db, g)
    members = []
    lenses = []
    for pid in member_ids:
        row = db.fetchone("SELECT display_name FROM persons WHERE id = ?", (pid,))
        members.append({"person_id": pid, "display_name": row["display_name"] if row else pid})
        lens = cognitive_profile_for_llm(pid)
        if lens:
            lenses.append(lens)

    result = {
        "group_id": g["id"], "name": g["name"], "role_class": g["role_class"],
        "company_id": g["company_id"], "members": members,
    }

    if not lenses:
        # Generic: no live members (or none with a profile) → preset lens.
        seed = json.loads(g["seed_sliders"]) if g["seed_sliders"] else (
            role_default_sliders(g["role_class"]))
        result.update({
            "generic": True, "sliders": seed, "topic_interests": [],
            "note": ("no profiled members yet — this is a GENERIC deck for the "
                     f"'{g['role_class'] or 'default'}' archetype"),
        })
        return result

    # Merge via THE canonical policy. This used to mean-round every axis,
    # which prism.audience.group_policy's own docstring calls wrong: the
    # mean of a novice and an expert is a deck that loses the novice.
    # `sliders` stays a vector — four consumers depend on that shape.
    from okuro.prism.audience import aggregate_members

    aggregated = aggregate_members(
        [lens.get("sliders") or {} for lens in lenses],
        [lens.get("value_usage") or {} for lens in lenses],
    )
    merged = aggregated["sliders"]

    # Union topic_interests, keeping the max weight per topic.
    interests: dict[str, float] = {}
    for lens in lenses:
        for ti in (lens.get("topic_interests") or []):
            topic = ti.get("topic") if isinstance(ti, dict) else None
            if topic:
                w = ti.get("weight", 1) if isinstance(ti, dict) else 1
                interests[topic] = max(interests.get(topic, 0), w)

    result.update({
        "generic": False, "sliders": merged,
        # The categorical knobs that a merged vector cannot express — floors,
        # dual-construal, split-regulatory. {} for a single profiled member.
        "group_policy": aggregated["policy"],
        "topic_interests": [{"topic": t, "weight": w} for t, w in
                            sorted(interests.items(), key=lambda kv: -kv[1])],
        "member_count": len(members),
    })
    return result


# ── readers ─────────────────────────────────────────────────────────────────


def target_group_get(group_id: str) -> str:
    """Group profile: members (resolved) + merged audience lens, or generic note."""
    aud = resolve_audience(group_id)
    if aud.get("error"):
        return aud["error"]

    lines = [f"# {aud['name']}  (`{aud['group_id']}`)"]
    scope = f"company `{aud['company_id']}`" if aud["company_id"] else "template (no company)"
    lines.append(f"- **Scope:** {scope}  ·  **Class:** {aud['role_class'] or '—'}")

    if aud.get("generic"):
        lines.append(f"- ⚠️ **Generic deck** — {aud['note']}")
    else:
        lines.append(f"- **Members:** {aud.get('member_count', 0)} (merged lens)")
        lines.append("\n## Members")
        lines.append("| Person | ID |")
        lines.append("|---|---|")
        for m in aud["members"]:
            lines.append(f"| {m['display_name']} | {m['person_id']} |")

    sliders = aud.get("sliders") or {}
    if sliders:
        from okuro.peer.cognitive_profile import format_sliders_for_prompt

        lines.append("\n## Audience lens (sliders)")
        lines.append(format_sliders_for_prompt(aud))
    interests = aud.get("topic_interests") or []
    if interests:
        lines.append("\n## Shared interests")
        lines.append(", ".join(f"{i['topic']} ({i['weight']})" for i in interests))
    return "\n".join(lines)


def target_group_list() -> str:
    """All target groups with member counts (affiliation-derived + explicit)."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT * FROM target_groups WHERE active = 1 ORDER BY company_id IS NULL DESC, name")
    if not rows:
        return "No target groups yet. Seed standards with seed_standard_groups()."
    lines = ["| ID | Name | Kind | Company | Class | Members |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        n = len(_member_ids(db, r))
        lines.append(
            f"| {r['id']} | {r['name']} | {r['kind']} | {r['company_id'] or '—'} "
            f"| {r['role_class'] or '—'} | {n} |")
    return "\n".join(lines)


def list_groups() -> list[dict]:
    """Structured group list for the API (JSON, not markdown). Mirrors what
    ``target_group_list`` renders, with a resolved ``member_count`` per group."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT * FROM target_groups WHERE active = 1 "
        "ORDER BY company_id IS NULL DESC, name")
    out: list[dict] = []
    for r in rows:
        # Resolved once and reported both ways. member_ids is what the People
        # graph needs to place an existing audience on the canvas without
        # having to re-resolve membership client-side; member_count stays for
        # callers that only want the number (the handover picker).
        ids = _member_ids(db, r)
        out.append({
            "id": r["id"], "name": r["name"], "kind": r["kind"],
            "company_id": r["company_id"], "role_class": r["role_class"],
            "member_ids": ids,
            "member_count": len(ids),
        })
    return out


def seed_standard_groups() -> str:
    """Idempotently create the standard template groups (board, engineers, …)."""
    created = []
    for gid, name, rc in STANDARD_GROUPS:
        target_group_add(name=name, role_class=rc, kind="standard", group_id=gid)
        created.append(gid)
    return f"Seeded {len(created)} standard groups: {', '.join(created)}."


# ── Embedding (RAG over audiences) ──────────────────────────────────────────
#
# A target group is embedded as PROSE describing three things: who it is, who
# is in it, and how it wants to be addressed. The third is the reason this
# exists — "which audience wants an exec summary?" is a question no relational
# query answers, while "which group contains Alex" is one SQL already answers
# better. The shape vocabulary is shared with the People-graph edge labels
# (translate.audience_focus_phrases) so a group's stored description and a
# person's label speak the same language, and a query phrased in that language
# matches both.
#
# The vec table itself needs no migration: embed/repair.py's SPECS registry
# creates any missing vec_* table at the active tier's dim + cosine metric and
# backfills it by re-embedding the source rows.

_EMBED_MAX_MEMBERS = 12  # keep the text bounded for a large company audience


def group_embed_text(group_id: str) -> Optional[str]:
    """Prose describing an audience, for embedding. None if it cannot resolve.

    Deliberately NOT built from the raw row: membership for a company +
    role_class audience is derived from affiliations, and the merged slider
    shape only exists once members are resolved. Both come from
    resolve_audience, so the embedded text describes the audience the rest of
    okuro means by that name.
    """
    from okuro.db import get_db

    db = get_db()
    g = db.fetchone(
        "SELECT id, name, kind, role_class, company_id, notes "
        "FROM target_groups WHERE id = ? AND active = 1",
        (group_id,),
    )
    if not g:
        return None

    parts: list[str] = [g["name"]]
    if g["role_class"]:
        parts.append(f"role class {g['role_class']}")
    if g["company_id"]:
        company = db.fetchone(
            "SELECT name FROM companies WHERE id = ?", (g["company_id"],)
        )
        parts.append(f"at {company['name']}" if company else g["company_id"])
    if g["notes"]:
        parts.append(str(g["notes"])[:200])

    try:
        audience = resolve_audience(group_id)
    except Exception:
        audience = {}

    members = audience.get("members") or []
    if members:
        names = ", ".join(
            str(m.get("display_name") or m.get("person_id"))
            for m in members[:_EMBED_MAX_MEMBERS]
        )
        parts.append(f"members: {names}")

    # The payoff: the merged cognitive shape as searchable words. Only for a
    # group with real members — a template's sliders are a role preset, not a
    # measured audience, and embedding those would make every empty archetype
    # match every shape query.
    if members and not audience.get("generic"):
        from okuro.peer.translate import audience_focus_phrases

        phrases = audience_focus_phrases(audience.get("sliders"), limit=6)
        if phrases:
            parts.append("prefers: " + ", ".join(phrases))

    return " | ".join(p for p in parts if p)


def reembed_group(group_id: str) -> bool:
    """Refresh one group's vector. Never raises — embedding is not CRUD.

    Mirrors peer/persons.py: a missing or slow embed service must not fail the
    write that triggered it. A skipped vector is repaired by the daemon tick or
    by embed/repair.ensure_vec_dims().
    """
    try:
        text = group_embed_text(group_id)
        if not text:
            return False
        from okuro.embed.client import embed_one, to_bytes
        from okuro.db import get_db

        vec_bytes = to_bytes(embed_one(text))
        db = get_db()
        db.execute("DELETE FROM vec_target_groups WHERE id = ?", (group_id,))
        db.execute(
            "INSERT INTO vec_target_groups (id, embedding) VALUES (?, ?)",
            (group_id, vec_bytes),
        )
        db.conn.commit()
        return True
    except Exception:
        return False


def reembed_all_groups() -> dict:
    """Daemon handler — refresh every active group's vector.

    Why a sweep rather than only write-hooks: a company + role_class audience
    changes membership when an AFFILIATION changes, which never touches
    target_groups, so a write-hook there would never fire and the vector would
    silently rot. Sweeping is trivially cheap at the current scale (single
    digits); if groups ever reach the hundreds, trade this for hooks on
    affiliation_add/_remove.
    """
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall("SELECT id FROM target_groups WHERE active = 1")
    ok = sum(1 for r in rows if reembed_group(r["id"]))
    return {"groups": len(rows), "embedded": ok}


def match_groups(query: str, limit: int = 5) -> list[dict]:
    """Semantic search over audiences.

    Answers the questions the relational surface cannot: "who wants a
    technical deep-dive", "which audience needs an exec summary". Returns
    [{id, name, kind, role_class, company_id, member_count, similarity}].
    """
    if not query or not query.strip():
        return []
    from okuro.db import get_db
    # embed_QUERY, not embed_one. Qwen3-Embedding is asymmetric: a retrieval
    # query must carry the instruction prefix while documents stay raw.
    # Embedding a query raw collapses discrimination to cos~0.6 for everything
    # — the regression that once made roles_match rank by noise.
    from okuro.embed.client import embed_query, to_bytes

    db = get_db()
    try:
        matches = db.vec_search(
            "vec_target_groups", to_bytes(embed_query(query)),
            limit=candidate_pool(limit, scoped=True),
        )
    except Exception:
        return []

    out: list[dict] = []
    for m in matches:
        g = db.fetchone(
            "SELECT id, name, kind, role_class, company_id FROM target_groups "
            "WHERE id = ? AND active = 1",
            (m["id"],),
        )
        if not g:
            continue  # vector outlived its group; the next sweep prunes it
        row = dict(g)
        row["member_count"] = len(_member_ids(db, g))
        # vec_search returns raw distance. `1 - distance` is cosine similarity
        # ONLY because the table declares distance_metric=cosine (see
        # embed/repair.py — the vec tables that omitted it silently scored L2
        # through this same formula and broke recall for months).
        row["similarity"] = 1 - m["distance"]
        out.append(row)
    return out
