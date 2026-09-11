# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Post-task continuation suggestions — shared by engine completion hook and on-demand API.
# index:
#   imports
#   def _artifact_summaries
#   def _format_prompt
#   def _parse_response
#   def generate_continuation_suggestions
# AGENT_HEADER_END -->
"""Post-task continuation suggestions — shared generator.

Both the engine's completion hook (orchestrator/engine.py) and the
on-demand POST /api/tasks/{task_id}/suggest endpoint call this one
function. That guarantees:

- One prompt (owned by the ``workforce-reviewer`` role YAML)
- One output schema (validated here)
- One set of bugs to fix

The role's prompt lives in ``src/okuro/roles/catalog/workforce-reviewer.yaml``
and is loaded via the roles registry at call-time — editing YAML tunes the
output without a code change or redeploy.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger("okuro.orchestrator.suggestions")

ROLE_ID = "workforce-reviewer"


def _artifact_summaries(artifacts_dir: Path, limit: int = 10) -> str:
    """Read the head of each artifact markdown file into a compact summary block."""
    if not artifacts_dir.exists():
        return "(none)"
    lines: list[str] = []
    for path in sorted(artifacts_dir.iterdir()):
        if not (path.is_file() and path.suffix == ".md"):
            continue
        if path.name.endswith(".stdout.md"):
            continue
        try:
            head = path.read_text()[:500]
        except Exception:
            lines.append(f"- {path.name}")
            continue
        lines.append(f"- {path.name}: {head[:200]}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) or "(none)"


def _format_prompt(role_prompt: str, task_description: str,
                   phase_count: int, artifacts: str) -> str:
    """Append the task-specific context block to the role's prompt."""
    return (
        f"{role_prompt}\n\n"
        f"---\n\n"
        f"## Task just completed\n\n"
        f"**Description:** {task_description}\n\n"
        f"**Phases completed:** {phase_count}\n\n"
        f"**Artifacts:**\n{artifacts}\n\n"
        f"Emit exactly 3 suggestions per the CONTRACT + OUTPUT FORMAT above. "
        f"YAML only."
    )


_VALID_CATEGORIES = {"followup", "build", "business", "research", "validate"}
_VALID_EFFORT = {"small", "medium", "large"}


def _parse_response(raw: str, task_id: str) -> list[dict]:
    """Parse the role's YAML response into the structured suggestion shape.

    Structured shape (matches engine.py + api/main.py legacy readers):
        {id, suggestion_text, source_role, category, effort, timestamp, dismissed}
    """
    text = (raw or "").strip()

    # Defence in depth — role prompt forbids fences but strip anyway
    if text.startswith("```"):
        body = text.split("\n")
        text = "\n".join(line for line in body if not line.startswith("```"))

    try:
        parsed = yload(text)
    except yaml.YAMLError as exc:
        logger.warning("suggestion YAML parse failed: %s — raw head: %r",
                       exc, text[:200])
        return []

    if isinstance(parsed, dict) and "ideas" in parsed:
        parsed = parsed["ideas"]
    if not isinstance(parsed, list):
        logger.warning("suggestion parse: expected list, got %s", type(parsed))
        return []

    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []
    for idx, item in enumerate(parsed[:3]):
        if not isinstance(item, dict):
            continue
        suggestion_text = str(
            item.get("suggestion_text")
            or item.get("description")
            or item.get("title")
            or ""
        ).strip()
        if not suggestion_text:
            continue

        category = str(item.get("category", "followup")).strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "followup"

        effort = str(item.get("effort", "small")).strip().lower()
        if effort not in _VALID_EFFORT:
            effort = "small"

        short_hash = hashlib.sha1(
            f"{task_id}-{idx}-{suggestion_text}".encode()
        ).hexdigest()[:6]

        out.append({
            "id": f"sugg-{short_hash}",
            "suggestion_text": suggestion_text,
            "source_role": ROLE_ID,
            "category": category,
            "effort": effort,
            "timestamp": now_iso,
            "dismissed": False,
        })

    return out


def generate_continuation_suggestions(
    task_id: str,
    task_description: str,
    phase_count: int,
    artifacts_dir: Path,
    config,
) -> list[dict]:
    """Produce exactly up-to-3 structured continuation suggestions.

    Invokes the ``workforce-reviewer`` role via a one-shot LLM call. The
    caller is responsible for persisting the returned list to
    ``task.yaml``. Returns an empty list on any failure (role missing,
    LLM error, parse failure) — callers should treat empty as "no
    suggestions generated" and surface that state in the UI.
    """
    from okuro.orchestrator.decomposer import call_llm_api
    from okuro.roles.registry import get_role

    role = get_role(ROLE_ID, level="full")
    if not role or not role.get("content"):
        logger.warning(
            "workforce-reviewer role not seeded — re-run role catalog seed",
        )
        return []

    prompt = _format_prompt(
        role_prompt=role["content"],
        task_description=task_description,
        phase_count=phase_count,
        artifacts=_artifact_summaries(artifacts_dir),
    )

    try:
        raw = call_llm_api(prompt, config)
    except Exception as exc:
        logger.warning("workforce-reviewer LLM call failed: %s", exc)
        return []

    return _parse_response(raw, task_id)
