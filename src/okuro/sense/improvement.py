# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Self-improvement engine — detect patterns, suggest improvements.
# index:
#   def run_improvement
#   def _recommend_promotion
#   def _detect_recurring_patterns
#   def _detect_corrections
#   def _detect_cross_project_patterns
#   def run_maintenance
# AGENT_HEADER_END -->
"""Self-improvement engine — detect patterns, suggest improvements.

Ported from tm-launcher improvement.py.
Postgres vector ops replaced with okuro.db vec_search + sqlite-vec.
"""

from datetime import datetime, timezone


def run_improvement() -> str:
    """Detect patterns and generate improvement suggestions."""
    results = []

    for name, func in [
        ("Patterns", _detect_recurring_patterns),
        ("Corrections", _detect_corrections),
        ("Cross-project", _detect_cross_project_patterns),
    ]:
        try:
            results.append(func())
        except Exception as e:
            results.append(f"{name}: FAILED ({e})")

    return "## Improvement Report\n\n" + "\n".join(f"- {r}" for r in results)


def _recommend_promotion(topic: str | None, count: int, project: str | None) -> str:
    """Curation advice for the OWNER, never for an agent.

    Every branch names an action only the owner can take: editing
    conventions.yaml, TOOL-PROTOCOL.md, a project CLAUDE.md, the principle
    set, or the user profile. Three of them are things okuro's own rules
    forbid an agent from doing at all — TOOL-PROTOCOL.md and CLAUDE.md are
    the owner's files, and "NEVER create .md spec/architecture files" is in
    every provider instruction file.

    MEASURED 2026-09-09: this text reached a live bootstrap packet, appended to
    thought rows in `## Relevant Thoughts` as a bare imperative —
    "→ Add to TOOL-PROTOCOL.md — every agent needs this context." An agent
    reading its own packet has no way to tell that sentence is addressed to
    someone else.

    The wording is therefore an explicit vocative to the owner — by display
    name, resolved from the profile at call time — so the same string cannot
    be mistaken for an agent directive if a renderer surfaces it again;
    `_promotion_for_agent_packet` is what a packet may show.
    """
    from okuro.yu.profile import owner_display_name

    owner = owner_display_name("Owner")
    topic = (topic or "").lower()

    if topic in ("learning", "convention"):
        return f"{owner}: promote to conventions.yaml — agents keep rediscovering this."
    if topic == "decision":
        return f"{owner}: promote to a design principle — a repeated architectural choice."
    if topic == "architecture":
        if count >= 4:
            return f"{owner}: fold into the tool protocol — every agent needs this context."
        return f"{owner}: add to the project charter so agents stop re-learning it."
    if topic == "gotcha":
        return f"{owner}: add to the conventions gotchas — agents keep hitting this."
    if topic == "correction":
        return f"{owner}: add to the profile's behavioral overrides — a user preference."

    if count >= 5:
        return f"{owner}: appeared 5+ times — promote to a convention, or dismiss if obsolete."
    return f"{owner}: promote to conventions, fold into the protocol, or dismiss if stale."


def _detect_recurring_patterns() -> str:
    """Find memory clusters with 3+ similar entries via vector search."""
    from okuro.db import get_db

    db = get_db()

    memories = db.fetchall(
        """SELECT id, topic, content, project, source_agent
           FROM agent_memory
           WHERE confidence > 0.3
           ORDER BY created_at DESC LIMIT 100"""
    )

    if len(memories) < 3:
        return "Patterns: not enough memories to analyze"

    clusters = []
    seen: set[str] = set()

    for mem in memories:
        mid = mem["id"]
        if mid in seen:
            continue

        try:
            from okuro.embed.client import embed_one, to_bytes
            vec_bytes = to_bytes(embed_one(mem["content"]))
            matches = db.vec_search("vec_memory", vec_bytes, limit=6)
            similar = [m for m in matches if m["id"] != mid and (1 - m["distance"]) > 0.7]
        except Exception:
            continue

        if len(similar) >= 2:
            cluster_ids = {mid} | {s["id"] for s in similar}
            seen.update(cluster_ids)

            count = len(similar) + 1
            # THE CURATION ADVICE DOES NOT GO INTO THE THOUGHT.
            #
            # This line used to append `→ {reco}` — "Add to TOOL-PROTOCOL.md",
            # "Promote to conventions.yaml", "Add to project CLAUDE.md". Those
            # are actions only the owner can take, and three of them are actions
            # okuro's own rules forbid an agent from taking at all. Captured
            # into `thoughts.content`, they surface in the PROJECT half's
            # `## Relevant Thoughts` as a bare imperative, and an agent reading
            # its own packet cannot tell the sentence is addressed to someone
            # else. MEASURED 2026-09-09: 349 stored rows carry one.
            #
            # The OBSERVATION is kept — a memory that recurred 6x is worth an
            # agent's attention. `_recommend_promotion` still runs for the
            # improvement report, which is where the owner reads it.
            suggestion = (
                f"[{mem['topic'] or 'general'}] {mem['content'][:100]} "
                f"(recurs {count}x)"
            )

            try:
                from okuro.sense.thoughts import capture_thought
                capture_thought(content=suggestion, source="improvement", category="decision",
                                project=mem["project"])
                clusters.append(mem["content"][:50])
            except Exception:
                pass

    return f"Patterns: {len(clusters)} clusters detected" if clusters else "Patterns: no recurring patterns"


def _detect_corrections() -> str:
    """Find high-confidence corrections that appear multiple times."""
    from okuro.db import get_db

    db = get_db()

    corrections = db.fetchall(
        """SELECT id, content FROM agent_memory
           WHERE confidence >= 0.8
           ORDER BY created_at DESC LIMIT 50"""
    )

    if len(corrections) < 2:
        return "Corrections: not enough data"

    duplicates = 0
    for mem in corrections:
        try:
            from okuro.embed.client import embed_one, to_bytes
            vec_bytes = to_bytes(embed_one(mem["content"]))
            matches = db.vec_search("vec_memory", vec_bytes, limit=3)
            similar = [m for m in matches if m["id"] != mem["id"] and (1 - m["distance"]) > 0.8]
            if similar:
                duplicates += 1
        except Exception:
            continue

    if duplicates > 0:
        return f"Corrections: {duplicates} repeated corrections found -- may need profile/principle update"
    return "Corrections: none repeated"


def _detect_cross_project_patterns() -> str:
    """Find same convention across 3+ projects."""
    from okuro.db import get_db

    db = get_db()

    project_memories = db.fetchall(
        """SELECT id, content, project FROM agent_memory
           WHERE project IS NOT NULL AND confidence > 0.3
           ORDER BY created_at DESC LIMIT 100"""
    )

    if len(project_memories) < 3:
        return "Cross-project: not enough project-scoped memories"

    promoted = 0
    seen: set[str] = set()

    for mem in project_memories:
        mid = mem["id"]
        if mid in seen:
            continue

        try:
            from okuro.embed.client import embed_one, to_bytes
            vec_bytes = to_bytes(embed_one(mem["content"]))
            matches = db.vec_search("vec_memory", vec_bytes, limit=10)
            similar_ids = [m["id"] for m in matches if m["id"] != mid and (1 - m["distance"]) > 0.8]
        except Exception:
            continue

        if not similar_ids:
            continue

        placeholders = ", ".join("?" * len(similar_ids))
        similar_rows = db.fetchall(
            f"SELECT DISTINCT project FROM agent_memory WHERE id IN ({placeholders}) AND project IS NOT NULL",
            tuple(similar_ids),
        )
        projects = [r["project"] for r in similar_rows if r["project"] != mem["project"]]

        if len(projects) >= 2:
            seen.add(mid)
            all_projects = [mem["project"]] + projects
            try:
                from okuro.sense.thoughts import capture_thought
                suggestion = (
                    f"[cross-project] {mem['content'][:100]} "
                    f"(found in {', '.join(all_projects)}). "
                    f"→ Promote to system-wide conventions — {len(all_projects)} projects share this."
                )
                capture_thought(content=suggestion, source="improvement", category="decision")
                promoted += 1
            except Exception:
                pass

    return f"Cross-project: {promoted} promotion candidates" if promoted else "Cross-project: no patterns"


def run_maintenance() -> str:
    """Full maintenance: hygiene + improvement."""
    from okuro.sense.maintenance import run_hygiene

    hygiene = run_hygiene()
    improvement = run_improvement()

    try:
        from okuro.sense.memory import write_memory
        report = f"Maintenance run: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        write_memory(topic="learning", content=report, confidence=0.3, source_agent="okuro-maintenance")
    except Exception:
        pass

    return f"{hygiene}\n\n{improvement}"
