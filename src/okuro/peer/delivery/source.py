# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.source — SourceDocument: ingest a Stream B
#   artifact (kind=report) and parse it into a section-shaped structure
#   the outline stage can transform.
# index: imports | class SourceDocument | def from_artifact_id |
#   def _parse_sections | def _split_intro
# AGENT_HEADER_END -->
"""Source-document ingest for the delivery pipeline.

Takes an artifact_id (post-035 brain row) and returns a SourceDocument
with title, summary, intro, and a parsed section tree. The outline
stage (outline.py) consumes this; channel renderers never see the raw
artifact body directly.

Intentionally lossy on purpose: we strip frontmatter (AGENT_HEADER) and
fold consecutive blank lines so downstream sliders measure structural
density rather than whitespace. Original body stays in the artifact —
this is a projection, not a copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_AGENT_HEADER_RE = re.compile(
    r"^<!--\s*AGENT_HEADER.*?AGENT_HEADER_END\s*-->\s*\n",
    re.DOTALL,
)
_SQL_AGENT_HEADER_RE = re.compile(
    r"^--\s*<!--\s*AGENT_HEADER.*?AGENT_HEADER_END\s*-->\s*\n",
    re.DOTALL | re.MULTILINE,
)


@dataclass
class SourceSection:
    """One <h2>+ region of the source. Hierarchical via children."""

    heading: str
    level: int
    body: str = ""
    children: list["SourceSection"] = field(default_factory=list)


@dataclass
class SourceDocument:
    """Section-tree projection of a Stream B artifact."""

    artifact_id: str
    title: str
    summary: str
    kind: str
    media_type: str
    intro: str
    sections: list[SourceSection]
    raw_body: str
    task_id: str | None = None
    subtask_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "title": self.title,
            "summary": self.summary,
            "kind": self.kind,
            "media_type": self.media_type,
            "intro": self.intro,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "sections": [_section_to_json(s) for s in self.sections],
        }


def _section_to_json(s: SourceSection) -> dict[str, Any]:
    return {
        "heading": s.heading,
        "level": s.level,
        "body": s.body,
        "children": [_section_to_json(c) for c in s.children],
    }


# ----------------------------------------------------------------------
# Public ingest
# ----------------------------------------------------------------------


# One declared file is a deliverable; ten are a repository. The cap is per
# document and generous enough for a page, a report or a spec, while keeping
# a stray build log or dataset out of somebody's inbox.
_EXTRA_DOC_CAP_CHARS = 40_000


def from_artifact_id(
    artifact_id: str,
    *,
    extra_documents: list[tuple[str, str]] | None = None,
) -> SourceDocument | None:
    """Load + parse an artifact into a SourceDocument.

    Returns None if the artifact does not exist or has no body.

    ``extra_documents`` is ``[(label, text), ...]`` folded into the body
    before parsing, so the outline stage sees one document. The caller passes
    CONTENT, never paths — this module serves any artifact from any source
    and does not know what an orchestrator subtask is.
    """
    try:
        from okuro.sense.artifacts import artifact_get
        row = artifact_get(artifact_id, include_body=True)
    except Exception:
        row = None
    if row is None:
        return None

    body = row.get("body") or ""
    if not isinstance(body, str):
        body = str(body)

    body = _AGENT_HEADER_RE.sub("", body, count=1)
    body = _SQL_AGENT_HEADER_RE.sub("", body, count=1)

    title = (row.get("title") or "").strip()
    summary = (row.get("summary") or "").strip()
    kind = row.get("kind") or "report"
    media_type = row.get("media_type") or "text/markdown"

    for label, text in (extra_documents or []):
        text = (text or "").strip()
        if not text:
            continue
        if len(text) > _EXTRA_DOC_CAP_CHARS:
            text = (text[:_EXTRA_DOC_CAP_CHARS]
                    + f"\n\n[truncated at {_EXTRA_DOC_CAP_CHARS} characters]")
        # A heading, so the parser gives it its own section and the outline
        # sliders measure it as structure rather than one long intro.
        body = f"{body.rstrip()}\n\n## {label}\n\n{text}\n"

    intro, sections = _parse(body)

    return SourceDocument(
        artifact_id=artifact_id,
        title=title or (artifact_id[:8]),
        summary=summary,
        kind=kind,
        media_type=media_type,
        intro=intro,
        sections=sections,
        raw_body=body,
        task_id=row.get("task_id"),
        subtask_id=row.get("subtask_id"),
    )


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


def _parse(body: str) -> tuple[str, list[SourceSection]]:
    """Split body into (intro, [SourceSection]).

    Intro = everything before the first heading. Subsequent headings
    open new sections; deeper headings nest under the most recent
    parent at the appropriate level.
    """
    lines = body.splitlines()
    intro_lines: list[str] = []
    sections: list[SourceSection] = []
    stack: list[SourceSection] = []  # current ancestry — stack[-1] is top of tree

    in_code_fence = False
    fence_marker = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code_fence:
                in_code_fence = True
                fence_marker = stripped
            elif stripped == "```" or stripped == fence_marker:
                in_code_fence = False
                fence_marker = ""
            _append_to_current(stack, intro_lines, line)
            continue
        if in_code_fence:
            _append_to_current(stack, intro_lines, line)
            continue

        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()
            section = SourceSection(heading=heading, level=level)
            while stack and stack[-1].level >= level:
                stack.pop()
            if stack:
                stack[-1].children.append(section)
            else:
                sections.append(section)
            stack.append(section)
            continue

        _append_to_current(stack, intro_lines, line)

    intro = _trim_blanks("\n".join(intro_lines)).strip()
    _trim_section_bodies(sections)
    return intro, sections


def _append_to_current(stack: list[SourceSection], intro_lines: list[str], line: str) -> None:
    if stack:
        # body accumulates; we'll trim trailing blanks at the end.
        cur = stack[-1]
        cur.body = cur.body + ("\n" if cur.body else "") + line
    else:
        intro_lines.append(line)


def _trim_section_bodies(sections: list[SourceSection]) -> None:
    for s in sections:
        s.body = _trim_blanks(s.body).strip()
        _trim_section_bodies(s.children)


def _trim_blanks(text: str) -> str:
    """Collapse 3+ consecutive blank lines into a single blank line."""
    return re.sub(r"\n{3,}", "\n\n", text)
