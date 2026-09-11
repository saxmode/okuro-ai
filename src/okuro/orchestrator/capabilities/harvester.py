# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Capability harvester — extracts reusable building blocks from completed tasks.
# index:
#   imports
#   def harvest_capabilities
#   def _read_task_description
#   def _read_artifact_summaries
#   def _read_code_diff
#   def _build_prompt
#   def _call_llm
#   def _parse_capabilities
# AGENT_HEADER_END -->
"""Capability harvester — extracts reusable building blocks from completed tasks."""
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

from .models import Capability, CAPABILITY_TYPES
from .registry import CapabilityRegistry
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger(__name__)


def harvest_capabilities(task_dir: str, config) -> list[Capability]:
    """Extract reusable capabilities from a completed task.

    Reads task artifacts, diffs code changes, asks an LLM to identify
    reusable building blocks, and persists them to the capability registry.

    Args:
        task_dir: Absolute path to the task directory (contains task.yaml, artifacts/)
        config: Okuro Config object (needs orchestrator_root, prompts_dir)

    Returns:
        List of Capability objects extracted (empty on failure).
    """
    task_path = Path(task_dir)
    task_id = task_path.name

    try:
        # 1. Gather context
        description = _read_task_description(task_path)
        if not description:
            logger.info("No task description found in %s, skipping harvest", task_dir)
            return []

        artifact_summaries = _read_artifact_summaries(task_path)
        code_diff = _read_code_diff(task_path)

        # 2. Build prompt from template
        prompt = _build_prompt(config.prompts_dir, description, artifact_summaries, code_diff)
        if not prompt:
            logger.warning("Could not build harvest prompt (template missing?)")
            return []

        # 3. Call LLM via okuro.bridge
        raw_response = _call_llm(prompt)
        if not raw_response:
            logger.warning("LLM returned empty response for capability harvest")
            return []

        # 4. Parse YAML response into Capability objects
        capabilities = _parse_capabilities(raw_response, task_id)
        if not capabilities:
            logger.info("No capabilities extracted from task %s", task_id)
            return []

        # 5. Persist to registry
        registry_path = config.orchestrator_root / "capabilities.yaml"
        registry = CapabilityRegistry(registry_path)
        new_count = registry.add_many(capabilities)
        logger.info("Harvested %d capabilities (%d new) from task %s",
                     len(capabilities), new_count, task_id)

        return capabilities

    except Exception as e:
        logger.error("Capability harvesting failed for %s: %s", task_dir, e)
        return []


def _read_task_description(task_path: Path) -> str:
    """Read task description from task.yaml."""
    task_yaml = task_path / "task.yaml"
    if not task_yaml.exists():
        return ""
    try:
        data = yload(task_yaml.read_text()) or {}
        return data.get("description", "")
    except Exception as e:
        logger.warning("Failed to read task.yaml: %s", e)
        return ""


def _read_artifact_summaries(task_path: Path) -> str:
    """Read first 500 chars from each artifact markdown file (max 10)."""
    artifacts_dir = task_path / "artifacts"
    if not artifacts_dir.exists():
        return "_No artifacts found._"

    summaries = []
    md_files = sorted(artifacts_dir.glob("*.md"))[:10]
    for md_file in md_files:
        try:
            content = md_file.read_text()[:500]
            summaries.append(f"### {md_file.name}\n{content}")
        except Exception as e:
            logger.debug("Could not read artifact %s: %s", md_file, e)

    return "\n\n".join(summaries) if summaries else "_No artifact summaries available._"


def _read_code_diff(task_path: Path) -> str:
    """Run git diff --stat on the task directory to find code changes."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1", "--", "."],
            capture_output=True, text=True, timeout=10,
            cwd=str(task_path),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        logger.debug("git diff failed for %s: %s", task_path, e)
    return "_No code diff available._"


def _build_prompt(prompts_dir: Path, description: str, artifact_summaries: str, code_diff: str) -> str:
    """Load prompt template and fill in placeholders."""
    template_path = prompts_dir / "harvest-capabilities.md"
    if not template_path.exists():
        logger.warning("Prompt template not found: %s", template_path)
        return ""
    try:
        template = template_path.read_text()
        return template.format(
            task_description=description,
            artifact_summaries=artifact_summaries,
            code_diff_summary=code_diff,
        )
    except KeyError as e:
        logger.error("Prompt template has unknown placeholder: %s", e)
        return ""


def _call_llm(prompt: str) -> str:
    """Call okuro.bridge to get LLM analysis."""
    try:
        from okuro.bridge import invoke
        result = invoke(prompt=prompt, capability="fast-draft")
        if isinstance(result, dict):
            if not result.get("success"):
                logger.error("okuro.bridge failure: %s", result.get("error"))
                return ""
            result = result.get("output", "")
        return (result or "").strip()
    except Exception as e:
        logger.error("okuro.bridge call failed: %s", e)
        return ""


def _parse_capabilities(raw_yaml: str, task_id: str) -> list[Capability]:
    """Parse LLM YAML response into Capability objects."""
    # Strip markdown code fences if present
    text = raw_yaml.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```yaml) and last line (```)
        lines = [l for l in lines[1:] if l.strip() != "```"]
        text = "\n".join(lines)

    # If the LLM led with conversational text ("Provide those and I can..."),
    # try to locate the first YAML-shaped line and parse from there. Same
    # rescue pattern the decomposer uses (see decomposer.parse_plan).
    try:
        data = yload(text)
    except yaml.YAMLError:
        rescued = None
        for i, line in enumerate(text.split("\n")):
            stripped = line.strip()
            if (
                stripped.startswith("capabilities:")
                or stripped.startswith("- type:")
                or stripped.startswith("- id:")
            ):
                rescued_text = "\n".join(text.split("\n")[i:])
                try:
                    rescued = yload(rescued_text)
                    break
                except yaml.YAMLError:
                    continue
        if rescued is None:
            # Downgrade to debug — the harvester is side-effect-only and
            # an empty return is a graceful no-op. Logging warnings on
            # every conversational LLM reply was alarming noise without
            # affecting task outcomes.
            logger.debug(
                "harvester LLM response was not valid YAML (graceful no-op)"
            )
            return []
        data = rescued

    if not data:
        return []

    # Handle both list and dict-with-capabilities-key formats
    if isinstance(data, dict):
        items = data.get("capabilities", [])
    elif isinstance(data, list):
        items = data
    else:
        logger.warning("Unexpected YAML structure: %s", type(data))
        return []

    capabilities = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            # Validate type
            cap_type = item.get("type", "")
            if cap_type not in CAPABILITY_TYPES:
                logger.debug("Skipping capability with unknown type: %s", cap_type)
                continue

            # Validate required fields
            if not item.get("id") or not item.get("name"):
                logger.debug("Skipping capability missing id or name")
                continue

            cap = Capability(
                id=item["id"],
                type=cap_type,
                name=item.get("name", item["id"]),
                description=item.get("description", ""),
                origin_task=task_id,
                origin_project=item.get("origin_project", ""),
                interface=item.get("interface", ""),
                constraints=item.get("constraints", []),
                reusable_for=item.get("reusable_for", []),
                tags=item.get("tags", []),
                confidence=float(item.get("confidence", 0.5)),
            )
            capabilities.append(cap)
        except Exception as e:
            logger.debug("Failed to parse capability item: %s", e)
            continue

    return capabilities
