# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: M4 Agent Skills — metadata projection, slice-picker, lazy-load helper
# index:
#   imports
#   SKILL_METADATA_BUDGET_BYTES
#   def build_role_metadata
#   def list_role_metadata
#   def render_metadata_block
#   def pick_role_slice
#   def backfill_tags
#   def derive_tags_for_role
# AGENT_HEADER_END -->
"""M4 Agent Skills — progressive disclosure for okuro roles.

Three responsibilities:

1. **Metadata projection** — collapse a role row into a ≤500-byte skill
   metadata card (id, domain, tier, purpose, tags). This is what the
   dispatcher injects at spawn time instead of the full lean_prompt.

2. **Slice-picker** — given a task description, return the top-K most
   relevant role metadata cards. Deterministic-first (tag overlap with
   task keywords), semantic fallback (vec_roles cosine match) when tag
   intersection is empty.

3. **Lazy-load helper** — subagent calls `roles_get(role_id)` to fetch
   the full body only when it adopts a role. Mirrors Anthropic's Skills
   progressive disclosure semantically but works across all providers.

The role registry stays the single source of truth; SKILL.md files
(emitted by skill_generator.py for Claude Code) are projections, not a
fork.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from okuro.db import get_db
from okuro.roles.registry import list_roles

log = logging.getLogger("okuro.roles.skills")

SKILL_METADATA_BUDGET_BYTES = 500
"""Per-role metadata cap. Anthropic's published target is ~500B for the
always-loaded portion of a Skill — keep parity so the same projection
serves both the dispatcher slice and the Claude SKILL.md header."""

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "via", "expertise",
    "module", "role", "agent", "expert", "specialist", "designer",
    "engineer", "across", "based", "design", "domain", "system",
    "manage", "manager", "execute", "executes", "build", "builds",
    "implement", "implements", "configure", "configures", "ensure",
    "ensures", "provide", "provides", "create", "creates", "support",
    "supports", "use", "uses", "user", "users", "team", "teams",
    "process", "processes", "level", "tier", "modern", "fully",
    "general", "specific", "primary", "secondary", "complete",
    "comprehensive", "rigorous", "current", "active", "best",
    "practice", "practices", "standard", "standards",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def derive_tags_for_role(role: dict, limit: int = 6) -> list[str]:
    """Derive a deterministic 3-6 tag set for one role.

    Tags = {domain, tier, *kebab(id-tokens), *keywords-from-description}.
    Stable across runs: same role row in → same tags out.

    Trade-off: domain/tier are coarse and overlap heavily across roles.
    Description-keywords give the slice-picker finer-grained signal.
    """
    tags: list[str] = []
    seen: set[str] = set()

    def _push(tok: str) -> None:
        t = tok.strip().lower()
        if not t or t in seen or t in _STOPWORDS:
            return
        seen.add(t)
        tags.append(t)

    if role.get("domain"):
        _push(role["domain"])
    if role.get("tier") and role["tier"] != "standard":
        _push(role["tier"])

    # role_id tokens: "design-system-guardian" → design, system, guardian.
    # Skip the "design" duplicate if it's also the domain — already in seen.
    for part in re.split(r"[-_/]", role.get("id", "")):
        _push(part)
        if len(tags) >= limit:
            return tags

    # Description keywords: pull adjective-ish nouns ≥4 chars, skip stopwords.
    desc = (role.get("description") or "").lower()
    for tok in _TOKEN_RE.findall(desc):
        if len(tok) < 4:
            continue
        _push(tok)
        if len(tags) >= limit:
            break

    return tags


def backfill_tags(force: bool = False) -> int:
    """Populate roles.tags for every role row. Idempotent.

    `force=True` overwrites existing tag rows; default skips rows that
    already have a non-empty tag array. Returns count of rows updated.
    """
    db = get_db()
    roles = list_roles()
    updated = 0
    for r in roles:
        existing_raw = db.fetchone(
            "SELECT tags FROM roles WHERE role_id = ?", (r["id"],)
        )
        existing = []
        if existing_raw and existing_raw["tags"]:
            try:
                existing = json.loads(existing_raw["tags"])
            except (json.JSONDecodeError, TypeError):
                existing = []
        if existing and not force:
            continue
        tags = derive_tags_for_role(r)
        db.execute(
            "UPDATE roles SET tags = ? WHERE role_id = ?",
            (json.dumps(tags), r["id"]),
        )
        updated += 1
    return updated


def build_role_metadata(role_id: str) -> dict | None:
    """Project a role row into the ≤500B metadata card.

    Returns None if role not found. The caller is responsible for
    enforcing the byte budget at serialization time — `render_metadata_block`
    does that for the dispatcher path.
    """
    db = get_db()
    row = db.fetchone(
        "SELECT role_id, domain, tier, description, tags FROM roles "
        "WHERE role_id = ?",
        (role_id,),
    )
    if not row:
        return None
    tags = []
    if row["tags"]:
        try:
            tags = json.loads(row["tags"])
        except (json.JSONDecodeError, TypeError):
            tags = []
    return {
        "id": row["role_id"],
        "domain": row["domain"],
        "tier": row["tier"] or "standard",
        "purpose": (row["description"] or "").strip(),
        "tags": tags,
    }


def list_role_metadata() -> list[dict]:
    """Metadata cards for every non-draft role. Cheap — no prompt bodies."""
    db = get_db()
    rows = db.fetchall(
        "SELECT role_id, domain, tier, description, tags, maturity "
        "FROM roles ORDER BY domain, role_id"
    )
    out = []
    for r in rows:
        if (r["maturity"] or "active") == "draft":
            continue
        tags = []
        if r["tags"]:
            try:
                tags = json.loads(r["tags"])
            except (json.JSONDecodeError, TypeError):
                tags = []
        out.append({
            "id": r["role_id"],
            "domain": r["domain"],
            "tier": r["tier"] or "standard",
            "purpose": (r["description"] or "").strip(),
            "tags": tags,
        })
    return out


def render_metadata_block(meta: dict) -> str:
    """Serialize one metadata card to the dispatcher's compact form.

    Truncates purpose to keep the row ≤500B. Format:

        - <id> [<domain>/<tier>] tag1, tag2, tag3 — <purpose>
    """
    tags_str = ", ".join(meta.get("tags") or [])
    head = f"- {meta['id']} [{meta['domain']}/{meta['tier']}]"
    if tags_str:
        head += f" {tags_str}"
    purpose = meta.get("purpose") or ""
    # Final form: "{head} — {purpose}". The " — " separator is 5 bytes
    # in UTF-8 (' ' + '—' (3B) + ' '). The ellipsis when truncating is
    # 3 bytes. Reserve both so the rendered line never exceeds budget.
    sep_bytes = len(" — ".encode("utf-8"))
    room = SKILL_METADATA_BUDGET_BYTES - len(head.encode("utf-8")) - sep_bytes
    if room > 0 and purpose:
        purpose_bytes = purpose.encode("utf-8")
        if len(purpose_bytes) > room:
            ellipsis_bytes = len("…".encode("utf-8"))
            purpose = purpose_bytes[: room - ellipsis_bytes].decode(
                "utf-8", errors="ignore"
            ) + "…"
        return f"{head} — {purpose}"
    return head


def _task_keywords(text: str) -> set[str]:
    """Lowercase token set for tag-overlap matching. Drops stopwords."""
    out: set[str] = set()
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        out.add(tok)
    return out


# System-domain roles are okuro-internal (compressor, orchestrator,
# sys-researcher, role-researcher, etc.). They MUST NOT leak into user
# task slices — tag overlap on generic words like "task", "research",
# "system" would otherwise contaminate every user picker output.
# Filtered out unless the assigned role is itself a system role.
_SYSTEM_DOMAIN = "system"


def pick_role_slice(
    task_description: str,
    assigned_role: str | None = None,
    top_k: int = 5,
    *,
    cards: list[dict] | None = None,
) -> list[dict]:
    """Pick the top-K role metadata cards for a task.

    Algorithm:
      1. Always include `assigned_role` first (if present + valid).
      2. Score remaining roles by `len(tag_overlap(role.tags, task_keywords))`.
         Tie-breaker: domain match on a task keyword adds +0.5.
      3. If fewer than top_k roles have ≥1 tag overlap, fill the
         remainder by semantic fallback via `roles.resolver.match_role`
         (cosine over vec_roles).

    Deterministic-first by construction: when the same task description
    repeats, tag overlap returns the same scores in the same order.
    Semantic fallback fires ONLY when deterministic scoring runs out of
    candidates, never as the primary ranker (per M4 hard rule: pure-LLM
    pick forbidden).

    Domain scoping: system-domain roles are excluded from the candidate
    pool unless the assigned_role is itself a system role. This prevents
    orchestrator-internal roles (compressor / orchestrator / sys-researcher
    / role-researcher / role-designer / interface-specialist / etc.) from
    bleeding into user task picker slices via generic tag overlap.
    """
    if cards is None:
        cards = list_role_metadata()
    by_id = {c["id"]: c for c in cards}
    picked: list[dict] = []
    used: set[str] = set()

    # Domain gate — see _SYSTEM_DOMAIN comment above.
    assigned_card = by_id.get(assigned_role) if assigned_role else None
    assigned_is_system = (
        assigned_card is not None
        and assigned_card.get("domain") == _SYSTEM_DOMAIN
    )

    if assigned_role and assigned_role in by_id:
        picked.append(by_id[assigned_role])
        used.add(assigned_role)

    if len(picked) >= top_k:
        return picked

    kw = _task_keywords(task_description)
    scored: list[tuple[float, dict]] = []
    if kw:
        for c in cards:
            if c["id"] in used:
                continue
            # Domain gate — skip system roles unless the assigned role
            # is itself a system role.
            if (
                c.get("domain") == _SYSTEM_DOMAIN
                and not assigned_is_system
            ):
                continue
            tag_set = {t.lower() for t in c.get("tags") or []}
            overlap = len(tag_set & kw)
            if overlap == 0:
                continue
            score = float(overlap)
            if c["domain"] and c["domain"].lower() in kw:
                score += 0.5
            scored.append((score, c))
        scored.sort(key=lambda x: (-x[0], x[1]["id"]))
        for _, c in scored:
            if len(picked) >= top_k:
                break
            picked.append(c)
            used.add(c["id"])

    if len(picked) >= top_k:
        return picked

    # Semantic fallback — only when deterministic ranker yielded < top_k.
    try:
        from okuro.roles.resolver import match_role
        sem = match_role(task_description)
        alts: Iterable[dict] = []
        if sem.get("match_type") == "matched":
            alts = [{"id": sem["id"]}] + list(sem.get("alternatives") or [])
        elif sem.get("closest"):
            alts = [sem["closest"]]
        for alt in alts:
            if len(picked) >= top_k:
                break
            rid = alt.get("id")
            if not rid or rid in used or rid not in by_id:
                continue
            cand = by_id[rid]
            if (
                cand.get("domain") == _SYSTEM_DOMAIN
                and not assigned_is_system
            ):
                continue
            picked.append(cand)
            used.add(rid)
    except Exception as exc:
        log.warning("semantic fallback failed: %s", exc)

    return picked
