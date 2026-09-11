# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Content IR for okuro-handover — the ONE normalized payload every
#   content source (notes selection, flow subgraph, prism facet) emits and every
#   target (notes, flow, prism, slides) consumes. Source-agnostic on the way in,
#   target-agnostic on the way out.
# index:
#   class ContentIR
#   class ContentIR
#   def _snapshot_to_md
# AGENT_HEADER_END -->
"""okuro·handover — Content IR.

A selection in any tool is lifted into a single ``ContentIR`` before it is handed
to another tool. Sources differ (a markdown span, a set of flow nodes, a prism
facet) but every target only ever sees this shape, so adding a target is one
registry entry — never a new code path per source.

``kind`` records the richest shape preserved:
  - ``text``     — a markdown span (notes selection, plain text)
  - ``subgraph`` — flow nodes+edges (``structured = {nodes, edges}``)
  - ``facet``    — a prism facet subtree (``structured = {facets: {...}}``)
  - ``snapshot`` — a captured view (``structured = {screenshot, context}``): the
                   fallback source when a page declares no explicit selection

``body_md`` is ALWAYS populated (targets like prism/slides need a topic/body even
when the source was structured), and ``to_graph`` / ``to_markdown`` down-convert
for targets that want a specific shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

VALID_KINDS = ("text", "subgraph", "facet", "asset", "snapshot")

_MAX_TOPIC = 4000  # cap the seed we feed the prism/slides LLM


@dataclass
class ContentIR:
    kind: str
    title: str
    body_md: str
    structured: Optional[dict[str, Any]] = None
    project: Optional[str] = None
    source: Optional[dict[str, str]] = None  # {tool, id, label}

    # ── builders ─────────────────────────────────────────────────────────────

    @classmethod
    def from_text(cls, *, body_md: str, title: str = "", project: str = None,
                  source: dict = None) -> "ContentIR":
        body_md = (body_md or "").strip()
        return cls(
            kind="text",
            title=(title or _first_line(body_md) or "Untitled"),
            body_md=body_md,
            project=project,
            source=source,
        )

    @classmethod
    def from_subgraph(cls, *, nodes: list, edges: list, title: str = "",
                      project: str = None, source: dict = None) -> "ContentIR":
        nodes = nodes or []
        edges = edges or []
        return cls(
            kind="subgraph",
            title=(title or "Flow selection"),
            body_md=_subgraph_to_md(nodes),
            structured={"nodes": nodes, "edges": edges},
            project=project,
            source=source,
        )

    @classmethod
    def from_facet(cls, *, facets: dict, title: str = "", body_md: str = "",
                   project: str = None, source: dict = None) -> "ContentIR":
        return cls(
            kind="facet",
            title=(title or "Prism facet"),
            body_md=(body_md or title or "").strip(),
            structured={"facets": facets or {}},
            project=project,
            source=source,
        )

    @classmethod
    def from_asset(cls, *, assets: list, title: str = "", body_md: str = "",
                   project: str = None, source: dict = None) -> "ContentIR":
        """Media handed between tools. ``assets`` = list of
        ``{id, kind, mime, url, title}``. ``body_md`` carries markdown embeds so
        text-first targets (a note) render the media inline."""
        assets = assets or []
        md = (body_md or "").strip() or _assets_to_md(assets)
        first_title = (assets[0].get("title") if assets else "") or ""
        return cls(
            kind="asset",
            title=(title or first_title or "Asset"),
            body_md=md,
            structured={"assets": assets},
            project=project,
            source=source,
        )

    @classmethod
    def from_snapshot(cls, *, screenshot: str, context: dict = None, title: str = "",
                      project: str = None, source: dict = None) -> "ContentIR":
        """A captured view — the fallback source when a page declares no explicit
        selection. ``screenshot`` is a PNG data URL (``data:image/png;base64,…``)
        of what the user saw; ``context`` is the underlying page data
        (``{url, route, params, title, visibleText, entity?}``). Hands both the
        pixels AND the data to a target, so a new task can act on the real record,
        not just the image."""
        context = context or {}
        return cls(
            kind="snapshot",
            title=(title or context.get("title") or "Snapshot"),
            body_md=_snapshot_to_md(screenshot, context),
            structured={"screenshot": screenshot, "context": context},
            project=project,
            source=source,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "ContentIR":
        """Rehydrate an IR from an MCP/REST payload. Tolerant of missing keys."""
        if not isinstance(data, dict):
            raise ValueError("content IR must be an object")
        kind = (data.get("kind") or "text").strip()
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {VALID_KINDS}, got '{kind}'")
        structured = data.get("structured")
        if kind == "subgraph" and not isinstance(structured, dict):
            structured = {
                "nodes": data.get("nodes") or [],
                "edges": data.get("edges") or [],
            }
        if kind == "asset" and not isinstance(structured, dict):
            structured = {"assets": data.get("assets") or []}
        if kind == "snapshot" and not isinstance(structured, dict):
            structured = {
                "screenshot": data.get("screenshot") or "",
                "context": data.get("context") or {},
            }
        body_md = (data.get("body_md") or data.get("body") or "").strip()
        if kind == "asset" and not body_md and isinstance(structured, dict):
            body_md = _assets_to_md(structured.get("assets") or [])
        if kind == "snapshot" and not body_md and isinstance(structured, dict):
            body_md = _snapshot_to_md(
                structured.get("screenshot") or "", structured.get("context") or {})
        return cls(
            kind=kind,
            title=(data.get("title") or "").strip(),
            body_md=body_md,
            structured=structured,
            project=(data.get("project") or None),
            source=(data.get("source") or None),
        )

    # ── down-converters (target adapters call these) ─────────────────────────

    def as_topic(self, audience_hint: str = None) -> str:
        """A single-string seed for the prism/slides generator. The selection's
        title + body become the subject to expand; an optional audience hint is
        prepended so the generator frames the whole doc for that reader."""
        parts = []
        if audience_hint:
            parts.append(f"[Audience: {audience_hint}]")
        if self.title and self.title not in self.body_md:
            parts.append(self.title)
        if self.body_md:
            parts.append(self.body_md)
        topic = "\n\n".join(p for p in parts if p).strip()
        return topic[:_MAX_TOPIC]

    def to_markdown(self) -> str:
        """Markdown body for a note target, with a provenance footer."""
        body = self.body_md or ""
        foot = self._provenance_line()
        return f"{body}\n\n{foot}".strip() if foot else body

    def to_graph(self) -> dict[str, Any]:
        """A flow graph for the flow target. A subgraph passes through; text /
        facet collapse to a single markdown-note node."""
        if self.kind == "subgraph" and isinstance(self.structured, dict):
            return {
                "nodes": self.structured.get("nodes") or [],
                "edges": self.structured.get("edges") or [],
            }
        return {
            "nodes": [{
                "id": "n-source",
                "type": "node",
                "position": {"x": 0, "y": 0},
                "data": {"cat": "mdnote", "tag": "NOTE",
                         "title": self.title, "body": self.to_markdown()},
            }],
            "edges": [],
        }

    def _provenance_line(self) -> str:
        if not self.source:
            return ""
        tool = self.source.get("tool", "")
        label = self.source.get("label") or self.source.get("id") or ""
        if not tool:
            return ""
        return f"> ↩ handed over from **{tool}**" + (f" · {label}" if label else "")

    def public(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "title": self.title, "body_md": self.body_md,
            "structured": self.structured, "project": self.project,
            "source": self.source,
        }

    def snapshot_png_bytes(self) -> bytes:
        """Decode a snapshot's screenshot (a ``data:`` URL or raw base64) to PNG
        bytes. Empty bytes when there is no screenshot or it cannot be decoded —
        callers (asset register, task artifact write) treat empty as "no image"."""
        import base64

        if not isinstance(self.structured, dict):
            return b""
        raw = (self.structured.get("screenshot") or "").strip()
        if not raw:
            return b""
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            return base64.b64decode(raw)
        except (ValueError, TypeError):
            return b""


# ── helpers ──────────────────────────────────────────────────────────────────


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.lstrip("#").strip()
        if s:
            return s[:120]
    return ""


def _assets_to_md(assets: list) -> str:
    """Render asset refs as markdown embeds — images inline, other media as
    links — so a text-first target (a note) carries the media in its body."""
    lines: list[str] = []
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        url = (a.get("url") or "").strip()
        if not url:
            continue
        title = a.get("title") or a.get("id") or "asset"
        is_image = (a.get("kind") == "image") or (a.get("mime") or "").startswith("image/")
        lines.append(f"![{title}]({url})" if is_image else f"[{title}]({url})")
    return "\n\n".join(lines).strip()


def _snapshot_to_md(screenshot: str, context: dict) -> str:
    """Render a captured view as markdown — the image embed followed by a fenced
    context summary — so a text-first target (a note) carries both the pixels and
    the underlying page data inline."""
    import json

    lines: list[str] = []
    if screenshot:
        lines.append(f"![snapshot]({screenshot})")
    ctx = {k: v for k, v in (context or {}).items() if k != "screenshot"}
    if ctx:
        lines.append("```json\n" + json.dumps(ctx, indent=2, default=str) + "\n```")
    return "\n\n".join(lines).strip()


def _subgraph_to_md(nodes: list) -> str:
    """Flatten selected flow nodes into a readable markdown outline so structured
    selections still carry a body for text-first targets."""
    lines: list[str] = []
    for n in nodes:
        data = (n or {}).get("data", {}) if isinstance(n, dict) else {}
        title = data.get("title") or data.get("tag") or ""
        sub = data.get("sub") or ""
        body = data.get("body") or ""
        if data.get("cat") in ("title", "mdnote") and body:
            lines.append(body)
            continue
        if title:
            lines.append(f"- **{title}**" + (f" — {sub}" if sub else ""))
    return "\n".join(lines).strip()
