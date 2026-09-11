# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Thought capture, search, lifecycle, and daily digest.
# index:
#   imports
#   def capture_thought
#   def upsert_obsidian_thought
#   def search_thoughts
#   def update_thought
#   def daily_digest
#   def surface_relevant
#   def _format_thought_rows
#   def _guess_category
#   def _extract_tags
#   def _extract_metadata_llm
# AGENT_HEADER_END -->
"""Thought capture, search, lifecycle, and daily digest.

Ported from tm-launcher brain/thoughts.py.
Postgres vector ops replaced with okuro.db vec_search + sqlite-vec binary.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from okuro.sense.surface import log_thought_surface
from okuro.sense.retrieval import candidate_pool

log = logging.getLogger("okuro.sense.thoughts")

# Minimum cosine similarity for "relevant" vector matches. vec_thoughts
# declares distance_metric=cosine, so `1 - distance` IS cosine (enforced by
# okuro.embed.repair — before 2026-07-15 the table was L2 and this score was
# neither cosine nor bounded, which silently starved every caller below).
#
# CALIBRATED, not guessed — 2026-07-15, against this store with Qwen3-0.6B:
#   in-domain queries : cosine 0.571 - 0.746  (min 0.571)
#   off-domain queries: cosine 0.424 - 0.522  (max 0.522)
# Clean gap; 0.55 keeps every real match and admits no junk.
#
# Deliberately calibrated apart from memory._SIM_FLOOR — the two are set by
# separate measurement against separate corpora, not by symmetry. memory's
# current value is deliberately NOT quoted here; read it from memory.py.
#
# (This cross-reference has now rotted TWICE. It cited 0.62 until 2026-07-16,
# when an acceptance-test agent caught that memory's floor had moved
# 0.62 -> 0.60 -> 0.55. It was then rewritten to say "also 0.55" — and rotted
# again, found on 2026-07-28 by a memory-staleness audit after memory moved to
# 0.45. Twice is the pattern, not the accident: a number quoted from another
# module is a copy that no test covers, and re-quoting the corrected value
# just resets the clock. So the value is gone rather than updated. Cite the
# reasoning, not the value — which is what the previous version of this
# comment already said, one sentence after quoting a value.) Different
# corpora:
# thoughts are short, intent-shaped fragments and score lower than memory
# prose for the same relevance, so memory's floor would drop real matches
# here. The old code asserted the two were symmetric and pinned both at 0.4;
# the symmetry was never measured.
#
# 2026-07-16 — switched queries to the asymmetric embed_query wrapper (see
# memory.py for the why; documents stay raw). Recalibrated (n=60 round-trip):
# real self-sim min 0.325 / median 0.733; junk max 0.379. The distributions
# OVERLAP (real reaches below junk's ceiling), so this is a recall/precision
# trade, not a separation point — same as memory. 0.40 is the knee: junk
# rejection 1.000, recall@10 0.583; 0.42 buys nothing on junk and costs recall.
# Retune by re-running the measurement, never by copying memory's number.
_SIM_FLOOR = 0.40
_QUERY_INSTRUCTION = (
    "Given a task or focus, retrieve related ideas, plans, questions, and "
    "captured thoughts."
)


# Dedup window for capture_thought. Two captures with the same normalized
# content + project + source within this window collapse into one — the
# existing thought's surface_count is bumped instead of inserting a new
# row. Set per the daemon's polling cadence: orchestrator sentinel emits
# observations every 5 min, so a 24h window catches even slow-running
# repeated stalls without merging legitimately distinct daily updates.
_DEDUP_WINDOW_HOURS = 24

# Volatile tokens stripped before computing the dedup key. Pattern is
# DELIBERATELY narrow — it only erases time-shaped tokens that change
# between otherwise-identical observations from a polling watcher.
#
# Earlier this regex also stripped every bare integer, which collapsed
# legitimately distinct numeric facts (e.g. "Q1 revenue 4M" ≡ "Q2
# revenue 8M" → same dedup key, second capture lost). The bare-integer
# strip was insurance; the duration/date/time patterns alone are
# sufficient to neutralise the polling-spam case in
# tests/sense/test_thoughts_dedup.py.
#
# Preserved verbatim (NOT stripped): currency amounts, version strings,
# percentages, identifier-style numbers, ordinals — all of which carry
# information that distinguishes one captured thought from another.
_DEDUP_VOLATILE_TOKENS = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"  # ISO ts
    r"|\b\d{4}-\d{2}-\d{2}\b"                                 # ISO date
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"                          # HH:MM(:SS)
    r"|\b\d+\s*(?:min|sec|s|h|ms|hour|hours|minute|minutes|second|seconds)\b"  # durations
    r"|\b\d+\s*(?:ago|left)\b"                                # "5min ago", "3 left"
    , re.IGNORECASE,
)


def _dedup_key(content: str) -> str:
    """Normalize content for dedup. Strips volatile time tokens + whitespace."""
    if not isinstance(content, str):
        return ""
    normalized = _DEDUP_VOLATILE_TOKENS.sub(" ", content)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def capture_thought(content: str, source: str = "agent",
                    category: str = None, project: str = None) -> str:
    """Capture a thought with embedding and metadata extraction.

    Dedupes within a 24h window: when an incoming thought normalizes to
    the same key (same project + same source) as a recent thought, the
    existing row is reused — its ``surface_count`` is incremented and
    ``updated_at`` refreshed. Returns the existing thought id without
    inserting. Daemon polling emitters (orchestrator sentinel, watchers)
    can call this repeatedly without flooding the daily digest.

    Args:
        content: Thought text.
        source: Origin — "agent", "obsidian", "cli", "user".
        category: Optional — "idea", "observation", "note", "decision", "question".
        project: Optional project slug.
    """
    from okuro.db import get_db

    db = get_db()

    # Inherit the project before the dedup pass, not after: the dedup query
    # below scopes on `project`, so resolving later would compare against NULL
    # and then insert a slug — two rows that should have collapsed into one.
    from okuro.sense.progress import resolve_write_project

    project = resolve_write_project(db, project)

    # Dedup pass — return existing id if a normalized-equivalent thought
    # was emitted from the same project + same source within the window.
    # Scoped to status='open' so a deliberately dismissed thought doesn't
    # silently re-resurrect by surface bump.
    norm_key = _dedup_key(content)
    if norm_key:
        recent = db.fetchall(
            "SELECT id, content, metadata, surface_count "
            "FROM thoughts "
            "WHERE status = 'open' "
            "  AND COALESCE(project, '') = COALESCE(?, '') "
            "  AND created_at > datetime('now', ?)",
            (project, f"-{_DEDUP_WINDOW_HOURS} hours"),
        )
        for row in recent or []:
            try:
                meta = row["metadata"]
                if isinstance(meta, str):
                    meta = json.loads(meta) if meta else {}
                if (meta or {}).get("source") != source:
                    continue
            except Exception:
                continue
            if _dedup_key(row["content"]) == norm_key:
                # Bump dedup_count, NOT surface_count. surface_count is
                # reserved for "actually shown to a consumer" signals
                # (bootstrap, daily_digest, search_thoughts) — see
                # migration 037. Polluting surface_count here used to
                # silently push thoughts out of the forgotten-ideas
                # bucket every time a polling watcher re-emitted the
                # same observation, even though no human had ever seen it.
                db.execute(
                    "UPDATE thoughts SET dedup_count = "
                    "  COALESCE(dedup_count, 0) + 1, "
                    "  updated_at = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                db.conn.commit()
                bumped = (
                    db.fetchone(
                        "SELECT dedup_count FROM thoughts WHERE id = ?",
                        (row["id"],),
                    )["dedup_count"]
                    or 0
                )
                return (
                    f"Thought deduped (id={row['id']}, dedup_count={bumped})"
                )

    thought_id = str(uuid.uuid4())

    metadata = {
        "source": source,
        "category": category or _guess_category(content),
        "tags": _extract_tags(content),
    }

    # Try LLM metadata extraction via bridge (best-effort)
    try:
        enhanced = _extract_metadata_llm(content)
        if enhanced:
            metadata.update(enhanced)
    except Exception:
        pass

    db.execute(
        """INSERT INTO thoughts (id, content, metadata, status, project)
           VALUES (?, ?, ?, 'open', ?)""",
        (thought_id, content, json.dumps(metadata), project),
    )

    # Store embedding
    try:
        from okuro.embed.client import embed_one, to_bytes
        vec_bytes = to_bytes(embed_one(content))
        db.execute(
            "INSERT INTO vec_thoughts (id, embedding) VALUES (?, ?)",
            (thought_id, vec_bytes),
        )
    except Exception:
        pass

    db.conn.commit()
    return f"Thought captured (id={thought_id}, category={metadata.get('category', 'unknown')})"


def upsert_obsidian_thought(content: str, source_path: str) -> str:
    """Upsert a thought from an Obsidian note.

    Each note (identified by source_path) maps to exactly one thought row.
    If the note was previously ingested, UPDATE the existing row. If new, INSERT.
    """
    from okuro.db import get_db

    db = get_db()
    metadata = {
        "source": "obsidian",
        "source_path": source_path,
        "category": _guess_category(content),
        "tags": _extract_tags(content),
    }

    # Check if this source_path already exists
    existing = db.fetchone(
        "SELECT id, status FROM thoughts WHERE json_extract(metadata, '$.source_path') = ?",
        (source_path,),
    )

    if existing:
        thought_id = existing["id"]
        # Merge metadata
        old_meta_raw = db.fetchone("SELECT metadata FROM thoughts WHERE id = ?", (thought_id,))
        old_meta = json.loads(old_meta_raw["metadata"]) if old_meta_raw and isinstance(old_meta_raw["metadata"], str) else {}
        old_meta.update(metadata)

        db.execute(
            "UPDATE thoughts SET content = ?, metadata = ?, updated_at = datetime('now') WHERE id = ?",
            (content, json.dumps(old_meta), thought_id),
        )

        # Update embedding
        try:
            from okuro.embed.client import embed_one, to_bytes
            vec_bytes = to_bytes(embed_one(content))
            db.execute("DELETE FROM vec_thoughts WHERE id = ?", (thought_id,))
            db.execute("INSERT INTO vec_thoughts (id, embedding) VALUES (?, ?)", (thought_id, vec_bytes))
        except Exception:
            # A thought with no vector is invisible to search_thoughts forever,
            # while this function still returns "Thought updated". write_memory
            # warns on the identical failure (memory.py); this path never got
            # the same treatment, so the loss was silent by omission.
            log.warning(
                "update_thought: embedding failed for %s — the thought is "
                "stored but will NOT be semantically searchable",
                thought_id, exc_info=True,
            )

        db.conn.commit()
        return f"Thought updated (id={thought_id}, path={source_path})"
    else:
        thought_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO thoughts (id, content, metadata, status) VALUES (?, ?, ?, 'open')",
            (thought_id, content, json.dumps(metadata)),
        )

        try:
            from okuro.embed.client import embed_one, to_bytes
            vec_bytes = to_bytes(embed_one(content))
            db.execute("INSERT INTO vec_thoughts (id, embedding) VALUES (?, ?)", (thought_id, vec_bytes))
        except Exception:
            # Same silent-loss class as update_thought above: "Thought
            # captured" is returned either way, but without a vector the idea
            # can never be recalled. The user's word for this is "ideas are
            # lost"; this is one of the paths that produced it.
            log.warning(
                "capture_thought: embedding failed for %s — the thought is "
                "stored but will NOT be semantically searchable",
                thought_id, exc_info=True,
            )

        db.conn.commit()
        return f"Thought captured (id={thought_id}, path={source_path})"


def search_thoughts(query: str = None, limit: int = 5,
                    category: str = None, status: str = "open",
                    since: str = None) -> str:
    """Search thoughts with optional filters.

    Args:
        query: Semantic search query.
        limit: Max results.
        category: Filter by category.
        status: Filter by status. Defaults to ``"open"`` — dropped/resolved
            thoughts are hidden by default. Pass ``status=""`` (empty
            string) to disable status filtering and return all statuses.
            ``None`` is treated the same as the default for backwards
            compatibility with callers that explicitly pass None.
        since: ISO date string.
    """
    from okuro.db import get_db

    db = get_db()

    # Empty string disables the status filter; None falls back to default
    # ("open") so accidental None-passers don't leak resolved/dropped rows.
    status_filter: Optional[str]
    if status == "":
        status_filter = None
    elif status is None:
        status_filter = "open"
    else:
        status_filter = status

    if query:
        try:
            from okuro.embed.client import embed_query, to_bytes
            vec_bytes = to_bytes(embed_query(query, instruction=_QUERY_INSTRUCTION))
            # status + category narrow this AFTER the scan, so a 1x pool
            # could be emptied entirely by either. NOT partitioned by
            # project: this surface BOOSTS by project rather than filtering,
            # so pushing it into the scan would silently drop other projects.
            matches = db.vec_search(
                "vec_thoughts", vec_bytes,
                limit=candidate_pool(limit, scoped=True),
            )

            if matches:
                ids = [m["id"] for m in matches]
                distances = {m["id"]: m["distance"] for m in matches}
                placeholders = ", ".join("?" * len(ids))

                sql = f"""SELECT id, content, metadata, status, project, surface_count, created_at
                          FROM thoughts WHERE id IN ({placeholders})"""
                params = list(ids)

                if status_filter:
                    sql += " AND status = ?"
                    params.append(status_filter)
                if category:
                    sql += " AND json_extract(metadata, '$.category') = ?"
                    params.append(category)

                rows = db.fetchall(sql, tuple(params))
                if rows:
                    return _format_thought_rows(rows, distances, is_query=True)
        except Exception:
            pass  # Fall through to direct query

    # Direct query
    sql = "SELECT id, content, metadata, status, project, surface_count, created_at FROM thoughts WHERE 1=1"
    params: list = []

    if status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
    if category:
        sql += " AND json_extract(metadata, '$.category') = ?"
        params.append(category)
    if since:
        sql += " AND created_at >= ?"
        params.append(since)

    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = db.fetchall(sql, tuple(params))
    if not rows:
        return "No thoughts found."
    return _format_thought_rows(rows, is_query=False)


def update_thought(thought_id: str, status: str = None,
                   metadata: dict = None) -> str:
    """Update thought status or metadata."""
    from okuro.db import get_db

    db = get_db()

    if status:
        db.execute(
            "UPDATE thoughts SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, thought_id),
        )

    if metadata:
        row = db.fetchone("SELECT metadata FROM thoughts WHERE id = ?", (thought_id,))
        if row:
            existing = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {})
            existing.update(metadata)
            db.execute(
                "UPDATE thoughts SET metadata = ?, updated_at = datetime('now') WHERE id = ?",
                (json.dumps(existing), thought_id),
            )

    db.conn.commit()
    return f"Thought {thought_id} updated"


def daily_digest(limit: int = 10) -> str:
    """Generate daily digest of thoughts."""
    from okuro.db import get_db

    db = get_db()
    sections = []
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    # Defensively dedupe surface logging in case a row qualifies for more
    # than one bucket (shouldn't happen with current filters, but cheap).
    logged_ids: set[str] = set()

    def _log_once(thought_id: str, context: str) -> None:
        if thought_id in logged_ids:
            return
        logged_ids.add(thought_id)
        log_thought_surface(thought_id, context=context)

    # Action items
    rows = db.fetchall(
        """SELECT id, content, metadata, created_at FROM thoughts
           WHERE status = 'open' AND json_extract(metadata, '$.action_items') IS NOT NULL
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )
    if rows:
        lines = ["### Action Items"]
        for r in rows:
            _log_once(r["id"], "daily_digest_action_items")
            meta = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {})
            items = meta.get("action_items", [])
            if items:
                for item in items:
                    lines.append(f"- {item}")
            else:
                lines.append(f"- {r['content'][:100]}")
        sections.append("\n".join(lines))

    # Recent ideas
    rows = db.fetchall(
        """SELECT id, content, metadata, created_at FROM thoughts
           WHERE status = 'open' AND created_at >= ?
           ORDER BY created_at DESC LIMIT ?""",
        (week_ago, limit),
    )
    if rows:
        lines = ["### Recent Ideas"]
        for r in rows:
            _log_once(r["id"], "daily_digest_recent")
            meta = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {})
            cat = meta.get("category", "?")
            lines.append(f"- [{cat}] {r['content'][:150]}")
        sections.append("\n".join(lines))

    # Forgotten ideas — DO NOT log surface here. The bucket filter is
    # `surface_count = 0` so logging the surface immediately ejects the
    # thought from the bucket (self-extinguishing). The whole point of
    # this bucket is to keep re-surfacing unengaged thoughts in every
    # digest until the user acts on them.
    rows = db.fetchall(
        """SELECT id, content, metadata, created_at FROM thoughts
           WHERE status = 'open' AND surface_count = 0 AND created_at < ?
           ORDER BY created_at ASC LIMIT ?""",
        (week_ago, limit),
    )
    if rows:
        lines = ["### Forgotten Ideas"]
        for r in rows:
            lines.append(f"- {r['content'][:150]}")
        sections.append("\n".join(lines))

    if not sections:
        return "No thoughts to digest."
    return "## Daily Digest\n\n" + "\n\n".join(sections)


def surface_relevant(task_hint: str, limit: int = 3,
                     project: Optional[str] = None) -> str:
    """Find thoughts relevant to a task. Returns formatted string or empty.

    Args:
        task_hint: Natural-language task description to match against.
        limit: Max number of thoughts to return.
        project: Optional project slug — same-project matches get a
            1.2x similarity boost after the relevance floor filter.
    """
    if not task_hint:
        return ""

    from okuro.db import get_db

    db = get_db()

    try:
        from okuro.embed.client import embed_query, to_bytes
        vec_bytes = to_bytes(embed_query(task_hint, instruction=_QUERY_INSTRUCTION))
        # Widen pre-filter pool so floor + project boost have real material
        # to re-rank against; narrowing to `limit` pre-filter over-prunes.
        matches = db.vec_search(
            "vec_thoughts", vec_bytes,
            limit=candidate_pool(limit, scoped=True),
        )

        if not matches:
            return ""

        # Apply project boost FIRST so a same-project hit at sim 0.39 (which
        # becomes 0.47 after ×1.2) survives the floor instead of being
        # dropped before the boost can rescue it. Previous order applied the
        # boost only after the floor, undermining the boost for
        # near-threshold matches.
        boosted_pre: list[tuple[str, float]] = []
        for m in matches:
            base = 1 - m["distance"]
            sim = base * 1.2 if project else base
            boosted_pre.append((m["id"], sim))
        relevant = [b for b in boosted_pre if b[1] > _SIM_FLOOR]
        if not relevant:
            return ""

        ids = [r[0] for r in relevant]
        # Track BOTH the post-boost score (for ranking) and the base
        # similarity (for honest display). Showing the inflated boosted
        # score under "relevance:" would mislead the agent.
        boosted_sims = {r[0]: r[1] for r in relevant}
        base_sims = {m["id"]: 1 - m["distance"] for m in matches}
        placeholders = ", ".join("?" * len(ids))

        rows = db.fetchall(
            f"SELECT id, content, project FROM thoughts "
            f"WHERE id IN ({placeholders}) AND status = 'open'",
            tuple(ids),
        )

        # Final ranking: keep the project-conditional boost only for rows
        # that actually match the active project. Cross-project rows fall
        # back to their unboosted similarity.
        boosted: list[tuple[dict, float]] = []
        for r in rows:
            if project and r.get("project") == project:
                sim = boosted_sims.get(r["id"], 0.0)
            else:
                sim = base_sims.get(r["id"], 0.0)
            boosted.append((r, sim))
        boosted.sort(key=lambda pair: pair[1], reverse=True)
        boosted = boosted[:limit]

        lines = []
        for r, sim in boosted:
            # Increment surface count inside the same try/except so a
            # log-write failure never breaks the surfacing itself.
            log_thought_surface(r["id"], context="bootstrap_relevant")
            lines.append(f"- (relevance: {sim:.2f}) {r['content'][:200]}")
        return "\n".join(lines)

    except Exception:
        return ""


# --- Helpers ---

def _format_thought_rows(rows: list[dict], distances: dict = None,
                         is_query: bool = False) -> str:
    lines = []
    for r in rows:
        meta = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {})
        cat = meta.get("category", "?")
        scope = f" [{r['project']}]" if r.get("project") else ""
        sim = ""
        if is_query and distances:
            s = 1 - distances.get(r["id"], 1)
            sim = f" (relevance: {s:.2f})"
        date_str = r.get("created_at", "")[:10] if r.get("created_at") else ""
        lines.append(f"- **{cat}**{scope}{sim} ({r['status']}, {date_str}): {r['content'][:200]}")
    return "\n".join(lines)


def _guess_category(content: str) -> str:
    """Keyword-based category guess from first line.

    English + minimal German cues (user is Switzerland-based and captures
    some German content). Order: most-specific wins — todo/decision prefix
    markers beat loose idea/observation phrasing.
    """
    first_line = ""
    for line in content.split("\n"):
        stripped = line.strip().lstrip("#").strip()
        if stripped and not stripped.startswith("---"):
            first_line = stripped
            break
    probe = (first_line or content[:200]).lower()

    # Explicit prefixes win first (most specific).
    if any(w in probe for w in ["todo:", "- [ ]", "task:", "aufgabe:"]):
        return "todo"
    if any(w in probe for w in [
        "decided:", "decision:", "we will", "let's",
        "entschieden:", "entscheidung:", "wir werden",
    ]):
        return "decision"
    if "question:" in probe or "frage:" in probe:
        return "question"
    if any(w in probe for w in [
        "idea:", "what if", "could we", "maybe we", "how about",
        "idee:", "was wäre wenn", "wir könnten", "vielleicht",
    ]):
        return "idea"
    if any(w in probe for w in ["noticed", "interesting:", "observation:", "realized"]):
        return "observation"
    if first_line.rstrip().endswith("?"):
        return "question"
    return "note"


def _extract_tags(content: str) -> list[str]:
    return re.findall(r"#(\w+)", content)


def _extract_metadata_llm(content: str) -> dict | None:
    """Try to extract rich metadata via okuro.bridge.

    LLMs wrap JSON in code fences or preface it with chatter ("Here's the
    JSON: {...}"). Strip markdown fences and fall back to extracting the
    first ``{...}`` block with a regex before giving up. Still returns
    None on unrecoverable failure — public behavior is unchanged; we only
    add observability via logger.warning when parsing falls through.
    """
    result = None
    try:
        from okuro.bridge import invoke

        result = invoke(
            prompt=(
                "Extract metadata from this thought. Return JSON with: "
                "category (idea/observation/note/decision/question), "
                "tags (list of keywords), people (mentioned names), "
                "action_items (list of todos). Return ONLY valid JSON.\n\n"
                f"{content}"
            ),
            capability="fast-draft",
        )
        # okuro.bridge.invoke returns dict {success, output, error, ...};
        # older tests may still stub it to a raw string.
        if isinstance(result, dict):
            if not result.get("success"):
                log.warning(
                    "_extract_metadata_llm: bridge returned failure: %s",
                    result.get("error"),
                )
                return None
            result = result.get("output", "")
        if not isinstance(result, str) or not result.strip():
            return None

        cleaned = result.strip()
        # Strip markdown code fences (```json ... ``` or ``` ... ```).
        if cleaned.startswith("```"):
            # Drop opening fence line (may be ```json).
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            # Drop trailing fence.
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3]
            cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Fallback: first balanced-ish {...} block in mixed output.
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            log.warning(
                "_extract_metadata_llm: unparseable response (first 200 chars): %r",
                (result[:200] if result else ""),
            )
            return None
    except Exception as exc:
        log.warning(
            "_extract_metadata_llm: bridge invocation failed: %s; response head=%r",
            exc,
            (result[:200] if isinstance(result, str) else None),
        )
        return None
