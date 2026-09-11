# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Intake — context confidence scoring and question generation.
# index: def score_confidence | def generate_questions | def build_intake
# AGENT_HEADER_END -->
"""Intake — context confidence scoring and question generation.

Analyzes the task_hint against available signals (project match, progress,
open thoughts, memories) and generates targeted questions when the human
likely forgot to provide important context.
"""

from __future__ import annotations

from ._safe import _safe

# Confidence thresholds
THRESHOLD_ASK = 0.6      # Below this: generate questions
THRESHOLD_WARN = 0.4     # Below this: mark as P0 (must-ask)
MAX_QUESTIONS = 3


def score_confidence(
    task_hint: str | None,
    project_slug: str | None,
    progress: dict | None,
    thoughts: list | None,
    memories: list | None,
) -> tuple[float, list[str]]:
    """Score how complete the human's task context is.

    Returns (score 0.0-1.0, list of missing signal names).
    """
    if not task_hint:
        return 0.0, ["task_hint", "project", "outcome", "specifics", "scope"]

    score = 0.0
    missing = []
    task_lower = task_hint.lower()
    words = task_lower.split()

    # Signal 1: Has project reference? (+0.2)
    if project_slug:
        score += 0.2
    else:
        missing.append("project")

    # Signal 2: Has concrete outcome/verb? (+0.2)
    action_verbs = {
        "fix", "build", "add", "remove", "refactor", "implement", "create",
        "update", "migrate", "deploy", "debug", "investigate", "design",
        "test", "review", "configure", "optimize", "integrate", "wire",
        "port", "continue", "continuing", "finish",
    }
    has_verb = any(w in action_verbs for w in words)
    if has_verb:
        score += 0.2
    else:
        missing.append("outcome")

    # Signal 3: Has specifics — files, components, or technical terms? (+0.2)
    has_specifics = (
        any(c in task_hint for c in [".", "/", "_", ":"])
        or len(words) >= 10
        or any(w.endswith((".py", ".ts", ".js", ".yaml", ".md")) for w in words)
    )
    if has_specifics:
        score += 0.2
    else:
        missing.append("specifics")

    # Signal 4: Matches recent progress — continuation? (+0.2)
    if progress and project_slug:
        score += 0.2
    elif progress:
        score += 0.1
        missing.append("continuation_unclear")
    else:
        missing.append("no_prior_context")

    # Signal 5: Scope clarity (+0.2)
    scope_words = {
        "only", "just", "specifically", "scope", "done when", "goal is",
        "should", "must", "needs to", "so that", "because", "in order to",
    }
    has_scope = any(phrase in task_lower for phrase in scope_words)
    if has_scope:
        score += 0.2
    else:
        missing.append("scope")

    return min(score, 1.0), missing


def generate_questions(
    task_hint: str | None,
    project_slug: str | None,
    confidence: float,
    missing: list[str],
    progress: dict | None = None,
    thoughts: list[tuple] | None = None,
) -> list[dict]:
    """Generate targeted intake questions based on what's missing.

    Returns list of {priority: "P0"|"P1", question: str, reason: str}.
    """
    if confidence >= THRESHOLD_ASK:
        return []

    questions = []

    # P0: Things we really need to know
    if "project" in missing:
        if progress:
            proj = progress.get("project", "unknown")
            summary = progress.get("summary", "")
            questions.append({
                "priority": "P0",
                "question": f"Which project is this for? Last active was **{proj}**: _{summary}_",
                "reason": "no_project_match",
            })
        else:
            questions.append({
                "priority": "P0",
                "question": "Which project does this belong to?",
                "reason": "no_project_match",
            })

    if "continuation_unclear" in missing and progress:
        proj = progress.get("project", "?")
        next_steps = progress.get("next_steps")
        blockers = progress.get("blockers")

        if blockers:
            questions.append({
                "priority": "P0",
                "question": f"Last session on **{proj}** was blocked on: _{blockers}_. Is this related?",
                "reason": "open_blocker",
            })
        elif next_steps:
            questions.append({
                "priority": "P0",
                "question": f"Last session on **{proj}** planned next: _{next_steps}_. Continuing this or something new?",
                "reason": "session_continuity",
            })

    # P1: Things that would help
    if "outcome" in missing and confidence < THRESHOLD_WARN:
        questions.append({
            "priority": "P1",
            "question": "What does 'done' look like for this?",
            "reason": "no_outcome",
        })

    if "specifics" in missing:
        questions.append({
            "priority": "P1",
            "question": "Any specific files, components, or areas to focus on?",
            "reason": "no_specifics",
        })

    if "scope" in missing and confidence < THRESHOLD_WARN:
        questions.append({
            "priority": "P1",
            "question": "Any boundaries? (e.g. 'only touch X', 'don't change Y', 'keep it minimal')",
            "reason": "no_scope",
        })

    # Surface relevant thoughts
    if thoughts:
        thought_texts = [
            t[1][:80] if isinstance(t, (list, tuple)) else str(t)[:80]
            for t in thoughts[:2]
        ]
        if thought_texts:
            formatted = "; ".join(f"_{t}_" for t in thought_texts)
            questions.append({
                "priority": "P1",
                "question": f"You have open thoughts that might be relevant: {formatted} — any of these connected?",
                "reason": "related_thoughts",
            })

    return questions[:MAX_QUESTIONS]


def build_intake(
    task_hint: str | None,
    project_slug: str | None,
) -> tuple[str, str, int]:
    """Build the intake section for the bootstrap packet.

    Returns (section_name, content, token_estimate).
    """
    from .budget import estimate_tokens

    progress = None
    thoughts_raw = None
    memories = None

    # Gather progress
    def _gather_progress():
        from okuro.db import get_db
        db = get_db()

        if project_slug:
            row = db.fetchone(
                "SELECT project, status, summary, next_steps, blockers "
                "FROM progress WHERE project = ? ORDER BY updated_at DESC LIMIT 1",
                (project_slug,),
            )
        else:
            row = db.fetchone(
                "SELECT project, status, summary, next_steps, blockers "
                "FROM progress ORDER BY updated_at DESC LIMIT 1"
            )
        return dict(row) if row else None

    progress = _safe("intake.build_intake.progress", _gather_progress)

    # Gather relevant thoughts
    def _gather_thoughts():
        if not task_hint:
            return None
        from okuro.embed.client import embed_query, to_bytes
        from okuro.db import get_db
        db = get_db()
        vec_bytes = to_bytes(embed_query(task_hint))
        matches = db.vec_search("vec_thoughts", vec_bytes, limit=3)
        if not matches:
            return None
        ids = [m["id"] for m in matches]
        distances = {m["id"]: m["distance"] for m in matches}
        placeholders = ", ".join("?" * len(ids))
        rows = db.fetchall(
            f"SELECT id, content FROM thoughts WHERE id IN ({placeholders}) AND status = 'open'",
            tuple(ids),
        )
        # Reuse thoughts' calibrated floor rather than a local magic number.
        # This queries vec_thoughts, so it must answer to the same threshold
        # its own module measured (0.55, junk max 0.522 / real min 0.571).
        # It carried a bare 0.4 — set before the metric was declared, when
        # `1 - distance` was not cosine at all, and never re-derived. A
        # second uncalibrated floor over the same corpus is how the original
        # outage started; an acceptance-test agent found this one.
        from okuro.sense.thoughts import _SIM_FLOOR as _THOUGHTS_FLOOR

        return [
            (r["id"], r["content"], 1 - distances.get(r["id"], 1))
            for r in rows
            if (1 - distances.get(r["id"], 1)) > _THOUGHTS_FLOOR
        ]

    thoughts_raw = _safe("intake.build_intake.thoughts", _gather_thoughts)

    # Gather memories for scoring
    def _gather_memories():
        if not task_hint:
            return None
        from okuro.sense.memory import read_memory
        mem_content = read_memory(query=task_hint, limit=2)
        # read_memory has TWO no-match returns: "No memories found." (empty
        # store / browse) and "No memories found matching this query …" (a
        # query that cleared no floor). The prefix covers both. An exact-string
        # check silently regressed on 2026-07-16 when the query-specific
        # message was added — it stopped matching, so the "no memory on this
        # topic" sentence was wrapped as task-relevant memory evidence on every
        # novel-topic bootstrap. Match the shape, not one literal.
        if mem_content and not mem_content.startswith("No memories found"):
            return [{"content": mem_content}]
        return None

    memories = _safe("intake.build_intake.memories", _gather_memories)

    # Score confidence
    confidence, missing_signals = score_confidence(
        task_hint, project_slug, progress, thoughts_raw, memories
    )

    # Generate questions
    questions = generate_questions(
        task_hint, project_slug, confidence, missing_signals,
        progress=progress, thoughts=thoughts_raw,
    )

    if not questions:
        return ("intake", "", 0)

    lines = ["## Intake"]
    lines.append(f"_Context confidence: {confidence:.1%} — {', '.join(missing_signals)}_")
    lines.append("")

    p0 = [q for q in questions if q["priority"] == "P0"]
    p1 = [q for q in questions if q["priority"] == "P1"]

    if p0:
        lines.append("**Ask before starting:**")
        for q in p0:
            lines.append(f"- {q['question']}")
        lines.append("")

    if p1:
        lines.append("**Worth clarifying:**")
        for q in p1:
            lines.append(f"- {q['question']}")
        lines.append("")

    lines.append('_User can say "just go" to skip._')

    content = "\n".join(lines)
    return ("intake", content, estimate_tokens(content))
