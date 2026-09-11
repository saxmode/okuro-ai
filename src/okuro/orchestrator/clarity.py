# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Orchestrator intake gate — assess task clarity before decomposition.
# index:
#   imports
#   def assess_task_clarity
#   def _build_assessment_prompt
#   def _parse_assessment
#   def _fallback_question
# AGENT_HEADER_END -->
"""Orchestrator intake gate — assess task clarity before decomposition.

When `okuro orchestrator "..."` is invoked with a vague description, the
decomposer wastes a strategic-tier LLM call producing a low-quality plan
and the user wastes turns iterating. This module short-circuits that:
ask a fast/cheap LLM whether the description is concrete enough; if not,
return targeted questions to the caller (HTTP API → frontend / MCP).

Parallel to `okuro.sense.bootstrap.intake` — that one runs at agent-session
startup (Claude Code, Codex). This one runs at orchestrator task creation.
Both follow the same P0 question taxonomy (target file/module, success
criteria, scope, constraints).

Cache by `hash(description)` so retries / preview round-trips don't re-pay
the bridge_invoke cost.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from functools import lru_cache
from typing import Optional

logger = logging.getLogger("okuro.orchestrator.clarity")


# Ask-by-default policy. Confidence < 0.9 → intake gate (return clarifying
# questions); only a near-perfect spec (>= 0.9) skips straight to auto-execute.
# Raised from 0.6 so nearly every real task gets clarified before planning.
CONFIDENCE_THRESHOLD = 0.9
# Smart-gate deliberation band RETIRED. The band was [CONFIDENCE_THRESHOLD,
# DELIBERATE_CONFIDENCE_CEILING) — clear-enough-to-plan-but-fuzzy tasks routed
# to multiperspective deliberation before planning. In practice real clarity
# scores are bimodal (<=0.58 or >=0.75), so the band never fired. Setting
# ceiling == threshold (0.9) makes the band empty: confidence < 0.9 → intake
# (ask questions), >= 0.9 → auto-execute. No deliberate-by-band routing.
DELIBERATE_CONFIDENCE_CEILING = 0.9
# Up to 10 clarifying questions per intake (was 5) — pairs with the aggressive
# ask-by-default threshold so a fuzzy task can be fully pinned down in one pass.
MAX_QUESTIONS = 10
# bridge_invoke timeout. MUST cover a COLD CLI-subprocess spawn (claude/codex/
# gemini `-p` fast-draft), which routinely exceeds 10s. At 10s the call timed
# out on EVERY invocation → assess_task_clarity fail-opened to confidence=1.0,
# silently disabling the intake gate AND the smart-gate deliberation band
# (both became unreachable in practice). 45s lets the cold spawn complete so
# the gate returns a real confidence; fail-open remains only for a genuine
# bridge outage, which is now rare rather than universal.
ASSESSMENT_TIMEOUT_SECONDS = 45


_ASSESSMENT_PROMPT = """You are a task-intake auditor for an autonomous code orchestrator.

A user submitted this task description for autonomous execution. Your job: decide
if the description is CONCRETE enough for an LLM planner to produce a useful plan,
or if critical context is missing.

Task description:
\"\"\"{description}\"\"\"
{project_context_block}
Score CONFIDENCE 0.0-1.0:
- 1.0 = target files/modules named, success criteria explicit, scope bounded
- 0.6 = enough to plan but some details fuzzy
- 0.3 = ambiguous goal, no specific files, unclear scope
- 0.0 = single vague verb, no nouns, no scope

If confidence < 0.6, list up to 5 SPECIFIC missing pieces. Pick from:
- target file/module (which file or directory?)
- success criteria (what does "done" look like?)
- scope (one file, one module, the whole project?)
- tool/CLI constraint (any required tool or framework?)
- deadline or priority
- inputs/outputs expected

Output ONLY a JSON object on a single line, no prose:
{{"confidence": 0.X, "missing": ["...", "..."]}}
"""


def _build_assessment_prompt(description: str, project_context: Optional[dict]) -> str:
    """Render the assessment prompt with optional project context."""
    if project_context:
        bits = []
        if project_context.get("project"):
            bits.append(f"Project slug: {project_context['project']}")
        if project_context.get("recent_progress"):
            bits.append(f"Recent progress: {project_context['recent_progress'][:200]}")
        if project_context.get("path"):
            bits.append(f"Project root: {project_context['path']}")
        ctx_block = "\nProject context:\n" + "\n".join(bits) + "\n" if bits else ""
    else:
        ctx_block = ""
    return _ASSESSMENT_PROMPT.format(
        description=description.strip(),
        project_context_block=ctx_block,
    )


def _parse_assessment(raw: str) -> dict:
    """Parse the model's JSON response. Tolerant of code-fence wrappers."""
    if not raw:
        raise ValueError("empty response")

    # Try to peel a fenced JSON block first
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()

    # Find first { ... } substring (model often wraps with prose)
    obj_match = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not obj_match:
        raise ValueError("no JSON object in response")

    payload = json.loads(obj_match.group(0))
    confidence = float(payload.get("confidence", 0.0))
    missing = payload.get("missing") or []
    if not isinstance(missing, list):
        missing = []
    # Coerce + truncate
    missing = [str(m).strip() for m in missing if str(m).strip()][:MAX_QUESTIONS]
    confidence = max(0.0, min(1.0, confidence))
    return {"confidence": confidence, "missing": missing}


def _missing_to_questions(missing: list[str]) -> list[str]:
    """Turn missing-piece labels into actionable user-facing questions.

    The model returns terse labels ("target file/module"); we expand to the
    same P0 phrasing used in src/okuro/sense/bootstrap/intake.py so the user
    sees one consistent intake voice across surfaces.
    """
    mapping = {
        "target file/module": "Which file or module should this touch?",
        "success criteria": "What does 'done' look like for this task?",
        "scope": "Is the scope one file, one module, or the whole project?",
        "tool/cli constraint": "Any required tool, CLI, or framework constraint?",
        "deadline or priority": "Is there a deadline or priority level?",
        "inputs/outputs expected": "What inputs and outputs do you expect?",
    }
    out: list[str] = []
    for m in missing:
        key = m.lower().strip().rstrip(".?!")
        # Try direct match first, then prefix match
        if key in mapping:
            out.append(mapping[key])
        else:
            matched = next(
                (v for k, v in mapping.items() if key.startswith(k) or k in key),
                None,
            )
            out.append(matched or _fallback_question(m))
    # Dedup preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for q in out:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped[:MAX_QUESTIONS]


def _fallback_question(label: str) -> str:
    """When the model returns a missing-piece label we don't know, keep it
    as a clarifying prompt rather than dropping it on the floor."""
    label = label.strip().rstrip(".?!")
    if not label:
        return "Can you provide more detail?"
    return f"Can you clarify: {label}?"


def _hash_key(description: str, project_slug: Optional[str]) -> str:
    """Stable cache key — same description + project = same answer."""
    raw = f"{(project_slug or '').strip()}::{description.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Module-level cache of (cache_key -> assessment dict). lru_cache decorates a
# private worker so callers can pass un-hashable dicts (project_context).
@lru_cache(maxsize=256)
def _cached_assessment(cache_key: str, prompt: str, capability: str) -> str:
    """Cached bridge_invoke call. Returns the raw JSON string from the model.

    Cache key is the description+project hash, NOT the prompt — so prompt
    template tweaks during the same process don't invalidate the cache, but
    a different description does. Returns the raw response string so parsing
    errors can be retried without re-paying the LLM cost.
    """
    from okuro.bridge.invoke import invoke as bridge_invoke

    logger.info(
        "[clarity] assessing description (cache_key=%s, cap=%s)",
        cache_key[:8], capability,
    )
    result = bridge_invoke(
        prompt=prompt,
        capability=capability,
        timeout=ASSESSMENT_TIMEOUT_SECONDS,
    )
    if not result.get("success"):
        # Don't cache failure — drop it so the next call re-tries
        logger.warning(
            "[clarity] bridge_invoke failed: %s — defaulting to PASS",
            result.get("error", "unknown"),
        )
        # Signal failure with a sentinel value the parent can handle
        raise RuntimeError(f"clarity bridge_invoke failed: {result.get('error')}")
    return result.get("output") or ""


def assess_task_clarity(
    description: str,
    project_context: Optional[dict] = None,
    capability: str = "fast-draft",
) -> dict:
    """Assess whether a task description is concrete enough to decompose.

    Args:
        description: User-supplied task description.
        project_context: Optional dict {project, recent_progress, path}.
        capability: Bridge capability route — default "fast-draft" for speed.

    Returns:
        {confidence: float 0.0-1.0, missing: list[str], questions: list[str]}

        - confidence ≥ 0.6 → caller should proceed with decomposition.
        - confidence < 0.6 → caller should surface `questions` to the user
          and skip decomposition (unless force/skip_intake override is set).

    On bridge failure, defaults to confidence=1.0 (FAIL OPEN — don't block
    the user because of an LLM availability hiccup; the decomposer will
    handle vague input as best it can, same as today).
    """
    description = (description or "").strip()
    if not description:
        # Empty description — caller should reject before this, but be safe
        return {
            "confidence": 0.0,
            "missing": ["task description is empty"],
            "questions": ["What would you like to do?"],
        }

    project_slug = (project_context or {}).get("project") if project_context else None
    cache_key = _hash_key(description, project_slug)
    prompt = _build_assessment_prompt(description, project_context)

    try:
        raw = _cached_assessment(cache_key, prompt, capability)
        parsed = _parse_assessment(raw)
    except RuntimeError as exc:
        # bridge failure — fail open
        logger.warning("[clarity] FAIL OPEN: %s", exc)
        return {"confidence": 1.0, "missing": [], "questions": []}
    except (ValueError, json.JSONDecodeError) as exc:
        # parse failure — fail open with a warning so we don't block the user
        logger.warning("[clarity] parse failure (%s) — FAIL OPEN", exc)
        return {"confidence": 1.0, "missing": [], "questions": []}

    questions = _missing_to_questions(parsed["missing"]) if parsed["missing"] else []
    return {
        "confidence": parsed["confidence"],
        "missing": parsed["missing"],
        "questions": questions,
    }


def clear_cache() -> None:
    """Test-only: reset the LRU cache between cases."""
    _cached_assessment.cache_clear()
