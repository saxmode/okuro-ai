# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Emit .claude/skills/<role_id>/SKILL.md per role for Claude Code
#   native progressive disclosure. L3 of the M4 Skills architecture.
# index:
#   imports
#   SKILL_MD_FRONTMATTER_MAX_BYTES
#   def render_skill_md
#   def emit_skill_for_role
#   def emit_all_skills
#   def main_cli
# AGENT_HEADER_END -->
"""SKILL.md emitter — okuro registry → Claude Code native Skills.

Claude Code's native progressive disclosure scans `.claude/skills/`
under the project root at session start. Each SKILL.md ships with a
YAML frontmatter block (name + description; ≤500 bytes by Anthropic
convention) that the model sees in its always-loaded layer. The body
loads only when Claude decides the skill is relevant.

This module is a one-way projection from the okuro `roles` table to
the Claude Code skill format. The registry stays the single source of
truth; this script regenerates the skill tree from it. Re-running is
idempotent (files overwritten in place, no orphan cleanup yet).

Why not auto-emit on every roles.write:
  Drift risk vs. cost trade-off. The roles table is the canonical store
  and the M4 metadata path serves all providers — Claude included. The
  SKILL.md tree is a bonus surface that lets Claude's native disclosure
  cut bootstrap further on Claude-Code sessions. Manual `okuro skills
  emit` keeps the projection auditable; auto-emit can land later when
  the frontmatter shape stabilises.
"""

from __future__ import annotations

import logging
from pathlib import Path

from okuro.roles.registry import get_role
from okuro.roles.skills import (
    SKILL_METADATA_BUDGET_BYTES,
    list_role_metadata,
)

log = logging.getLogger("okuro.roles.skill_md_emitter")

SKILL_MD_FRONTMATTER_MAX_BYTES = SKILL_METADATA_BUDGET_BYTES
"""Anthropic Skills convention — frontmatter must stay ≤500 bytes so
the always-loaded layer scales to dozens of skills without bloating
the system prompt."""


def render_skill_md(meta: dict, body: str) -> str:
    """Compose a SKILL.md file: YAML frontmatter + body.

    Frontmatter shape (Anthropic Skills v1):
        ---
        name: <role_id>
        description: <≤200-char single-line description>
        ---

    `tags` and `domain` are folded into the description so the frontmatter
    stays at the 2-field minimum. Anthropic's matcher reads `description`
    for relevance scoring.
    """
    description = meta.get("purpose") or ""
    domain = meta.get("domain") or ""
    tier = meta.get("tier") or "standard"
    tags = ", ".join(meta.get("tags") or [])

    # Squeeze metadata into description: "<domain/tier> · <tags> · <purpose>"
    parts: list[str] = []
    if domain or tier:
        parts.append(f"{domain}/{tier}")
    if tags:
        parts.append(tags)
    if description:
        parts.append(description)
    descline = " · ".join(parts)
    descline = descline.replace("\n", " ").strip()

    frontmatter = (
        "---\n"
        f"name: {meta['id']}\n"
        f"description: {descline}\n"
        "---\n"
    )
    fm_bytes = len(frontmatter.encode("utf-8"))
    if fm_bytes > SKILL_MD_FRONTMATTER_MAX_BYTES:
        # Truncate description so the frontmatter stays inside the
        # always-loaded budget. UTF-8 safe slice; reserves room for
        # the ellipsis byte cost so the rendered frontmatter ends up
        # at or below the budget, not 2-3 bytes over.
        ellipsis_bytes = len("…".encode("utf-8"))
        overshoot = fm_bytes - SKILL_MD_FRONTMATTER_MAX_BYTES + ellipsis_bytes
        descline_b = descline.encode("utf-8")
        descline = descline_b[: len(descline_b) - overshoot].decode(
            "utf-8", errors="ignore"
        ) + "…"
        frontmatter = (
            "---\n"
            f"name: {meta['id']}\n"
            f"description: {descline}\n"
            "---\n"
        )

    body = (body or "").rstrip()
    return f"{frontmatter}\n{body}\n"


def emit_skill_for_role(
    role_id: str,
    skills_root: Path,
    *,
    body_level: str = "lean",
) -> Path | None:
    """Write one SKILL.md under skills_root/<role_id>/. Returns path or None."""
    meta_list = [m for m in list_role_metadata() if m["id"] == role_id]
    if not meta_list:
        log.warning("no metadata card for %s — skipping", role_id)
        return None
    meta = meta_list[0]

    role = get_role(role_id, level=body_level)
    if not role or not (role.get("content") or "").strip():
        log.warning("no body at level=%s for %s — skipping", body_level, role_id)
        return None
    body = role["content"]

    target_dir = skills_root / role_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "SKILL.md"
    target.write_text(render_skill_md(meta, body), encoding="utf-8")
    return target


def emit_all_skills(
    skills_root: Path,
    *,
    body_level: str = "lean",
) -> dict:
    """Emit SKILL.md for every metadata card. Returns summary dict."""
    skills_root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    skipped: list[str] = []
    cards = list_role_metadata()
    for c in cards:
        path = emit_skill_for_role(c["id"], skills_root, body_level=body_level)
        if path:
            written.append(c["id"])
        else:
            skipped.append(c["id"])
    return {
        "skills_root": str(skills_root),
        "written": written,
        "skipped": skipped,
        "total_cards": len(cards),
    }


def main_cli() -> int:
    """`python -m okuro.roles.skill_md_emitter [skills_root]`"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Emit .claude/skills/<role>/SKILL.md from okuro registry."
    )
    parser.add_argument(
        "skills_root",
        nargs="?",
        default=".claude/skills",
        help="Target directory (default: .claude/skills)",
    )
    parser.add_argument(
        "--body-level",
        choices=("micro", "lean", "full"),
        default="lean",
        help="Which prompt tier to emit as the SKILL.md body (default: lean)",
    )
    args = parser.parse_args()

    skills_root = Path(args.skills_root).resolve()
    summary = emit_all_skills(skills_root, body_level=args.body_level)
    print(f"skills root:  {summary['skills_root']}")
    print(f"written:      {len(summary['written'])} / {summary['total_cards']}")
    if summary["skipped"]:
        print(f"skipped:      {len(summary['skipped'])} (no body at requested level)")
        for sid in summary["skipped"][:10]:
            print(f"  - {sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
