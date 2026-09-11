# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Pre-fetch verifiable side-effect evidence for the Critic.
#   The Critic only sees deliverable TEXT — it cannot observe side
#   effects (okuro-memory writes, git commits) the subagent performed.
#   ACs like "ADR written to okuro memory" therefore always read as
#   UNPROVEN even when satisfied, forcing retry exhaustion. This module
#   reads those side effects directly (okuro.db, git log) and renders an
#   authoritative evidence block injected into the Critic prompt.
# index: imports | _is_memory_ac | _is_git_ac | detect_side_effect_acs
#        | _gather_memory_evidence | _gather_git_evidence
#        | build_side_effect_evidence
# AGENT_HEADER_END -->
"""Orchestrator-side verification of side-effect acceptance criteria.

The Critic (``critic.py``) is a single-shot LLM that receives only the
deliverable bodies. Acceptance criteria that assert an external side
effect — "ADR written to okuro memory", "committed to git" — cannot be
verified from the deliverable text alone, so the Critic flags them
``load_bearing`` (unproven) on every attempt. The subagent really did
perform the side effect; the proof just lives in okuro.db / the git log,
not in the artifact body.

Rather than turn the Critic into a tool-calling agent, the orchestrator
reads the side effects HERE (deterministic, cheap) and injects a
``ORCHESTRATOR-VERIFIED SIDE-EFFECTS`` block the Critic treats as ground
truth. The Critic still judges whether a given memory / commit actually
satisfies the AC — it just can no longer fail a satisfied AC for lack of
visibility.

Everything is best-effort: any failure (embed service down, not a git
repo, malformed timestamps) yields an empty block and the Critic falls
back to its prior text-only judgement. A reviewer stage must never crash
because side-effect lookup failed.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)


# An AC is memory-shaped when it names "memory" (or write_memory) AND a
# persistence verb. "memory" alone is too broad (e.g. "memory usage < 2GB");
# the verb gate keeps it to persistence assertions.
_MEMORY_NOUN_RE = re.compile(r"\b(write_memory|okuro\s+memory|memory)\b", re.I)
_MEMORY_VERB_RE = re.compile(
    r"\b(writ|persist|record|sav|stor|captur|log(?:g)?|adr|decision)\w*", re.I
)
# Git-shaped: a commit assertion, or an explicit git-log reference.
_GIT_AC_RE = re.compile(
    r"\b(git\s+commit|committed|commit(?:ted|s|ation)?|git\s+log|pushed\s+to)\b",
    re.I,
)


def _is_memory_ac(text: str) -> bool:
    return bool(_MEMORY_NOUN_RE.search(text) and _MEMORY_VERB_RE.search(text))


def _is_git_ac(text: str) -> bool:
    # Guard against "GitHub issue" / "git history" false hits being treated as
    # commit assertions — those still pass the regex, which is fine: the
    # rendered git-log block is harmless context even when slightly over-broad.
    return bool(_GIT_AC_RE.search(text))


def detect_side_effect_acs(phase: Any) -> dict[str, list[str]]:
    """Scan a phase's subtask ACs, bucketing side-effect criteria by kind.

    Returns ``{"memory": [...], "git": [...]}`` — the verbatim AC strings
    that assert each side-effect class. Empty lists when none match.
    """
    memory: list[str] = []
    git: list[str] = []
    for st in getattr(phase, "subtasks", []) or []:
        for ac in getattr(st, "acceptance_criteria", None) or []:
            text = str(ac)
            if _is_memory_ac(text):
                memory.append(text)
            if _is_git_ac(text):
                git.append(text)
    return {"memory": memory, "git": git}


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Best-effort ISO-8601 parse. Returns None when unparseable."""
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Trailing fractional/zone noise — retry on the date+time prefix.
        try:
            dt = datetime.fromisoformat(s[:19])
        except ValueError:
            return None
    # Normalize to naive UTC so created_at (often naive) and task.created_at
    # (often tz-aware) compare without raising.
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz=None).replace(tzinfo=None)
    return dt


def _gather_memory_evidence(
    task: Any, memory_acs: list[str], *, limit: int = 6
) -> list[dict]:
    """Semantic-search okuro memory for writes that could satisfy the ACs.

    Scopes to memories created at/after the task started (with a small skew
    tolerance) so unrelated historical memories don't masquerade as this
    task's ADR. Returns row dicts (topic/content/created_at/_similarity).
    """
    try:
        from okuro.sense.memory import _read_memory_rows
    except Exception as exc:  # pragma: no cover - import guard
        log.warning("side_effect_evidence: memory module unavailable: %s", exc)
        return []

    desc = (getattr(task, "description", "") or "")[:200]
    query = " ".join([desc] + memory_acs)[:400]

    # Floor for "created during this task". 5-minute skew absorbs clock drift
    # between the orchestrator host and the DB write path.
    started = _parse_ts(getattr(task, "created_at", None))
    floor = (started - timedelta(minutes=5)) if started else None

    try:
        rows = _read_memory_rows(query=query, limit=limit * 3, min_confidence=0.0)
    except Exception as exc:
        log.warning("side_effect_evidence: memory query failed: %s", exc)
        return []

    out: list[dict] = []
    for r in rows:
        if floor is not None:
            created = _parse_ts(r.get("created_at"))
            if created is not None and created < floor:
                continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _gather_git_evidence(task: Any, *, limit: int = 15) -> list[str]:
    """`git log` the task's project_path for commits since the task started.

    Returns one ``<sha> <subject>`` line per commit. Empty when project_path
    is unset / not a repo / git missing.
    """
    project_path = (getattr(task, "project_path", "") or "").strip()
    if not project_path:
        return []
    started = _parse_ts(getattr(task, "created_at", None))
    args = [
        "git", "-C", project_path, "log",
        f"-n{limit}", "--no-merges", "--pretty=format:%h %s",
    ]
    if started:
        # Give a 5-min lead so a commit racing the task-create timestamp is
        # still captured.
        since = (started - timedelta(minutes=5)).isoformat()
        args.append(f"--since={since}")
    try:
        res = subprocess.run(
            args, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("side_effect_evidence: git log failed (%s): %s", project_path, exc)
        return []
    if res.returncode != 0:
        return []
    return [ln for ln in res.stdout.splitlines() if ln.strip()][:limit]


def build_side_effect_evidence(task: Any, phase: Any) -> str:
    """Render the ORCHESTRATOR-VERIFIED evidence block for the Critic prompt.

    Returns a markdown string. When no side-effect ACs are declared (the
    common case) returns a single ``_(none)_`` sentinel so the prompt slot
    stays inert — no behavioural change for the vast majority of phases.
    """
    acs = detect_side_effect_acs(phase)
    if not acs["memory"] and not acs["git"]:
        return "_(no side-effect acceptance criteria — nothing to pre-verify)_"

    sections: list[str] = []

    if acs["memory"]:
        rows = _gather_memory_evidence(task, acs["memory"])
        if rows:
            lines = ["## okuro memory writes (read directly from okuro.db)"]
            for r in rows:
                topic = (r.get("topic") or "?").strip()
                created = (r.get("created_at") or "?")
                sim = r.get("_similarity")
                sim_s = f" · sim {sim:.2f}" if isinstance(sim, (int, float)) else ""
                body = " ".join(str(r.get("content") or "").split())[:500]
                lines.append(f"- **{topic}** ({created}{sim_s}): {body}")
            sections.append("\n".join(lines))
        else:
            sections.append(
                "## okuro memory writes\n- _(no memory rows found for this task "
                "since it started — the persistence AC is genuinely unproven)_"
            )

    if acs["git"]:
        commits = _gather_git_evidence(task)
        if commits:
            lines = ["## git commits (read directly from the project repo)"]
            lines.extend(f"- `{c}`" for c in commits)
            sections.append("\n".join(lines))
        else:
            sections.append(
                "## git commits\n- _(no commits found in project_path since the "
                "task started — the commit AC is genuinely unproven)_"
            )

    return "\n\n".join(sections)
