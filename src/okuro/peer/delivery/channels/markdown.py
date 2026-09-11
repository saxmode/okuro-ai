# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.channels.markdown — text baseline channel.
#   Always available; produces a recipient-adapted markdown document
#   from the Outline. Other channels can fall back here when their
#   external dependency (marp-cli / Astro / Kokoro) is missing.
# index: imports | def render | def _render_block
# AGENT_HEADER_END -->
"""Markdown channel renderer (P1, baseline).

Consumes an Outline (already adapted by outline_for_recipient via
person_translate) plus a ThemeBundle (mostly cosmetic for markdown —
brand color appears in a header note only) and produces a clean
markdown document.

Output is text/markdown, always returned in ``body``. No subprocess,
no external dep — markdown is the safe fallback every host environment
supports.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


def render(outline, theme) -> "ChannelOutput":  # noqa: F821
    """Render Outline -> markdown text. Always succeeds (no external dep)."""
    from okuro.peer.delivery.channels import ChannelOutput, register

    start = time.monotonic()
    parts: list[str] = []

    title = outline.title or "Untitled"
    parts.append(f"# {title}")
    if outline.subtitle:
        parts.append(f"_{outline.subtitle}_")
        parts.append("")

    if outline.tldr:
        parts.append("## TL;DR")
        parts.append("")
        parts.append(outline.tldr)
        parts.append("")

    for section in outline.sections:
        parts.append(f"## {section.heading}")
        parts.append("")
        for block in section.blocks:
            chunk = _render_block(block)
            if chunk:
                parts.append(chunk)
                parts.append("")

    if outline.next_steps:
        parts.append("## Next Steps")
        parts.append("")
        for step in outline.next_steps:
            parts.append(f"- {step}")
        parts.append("")

    body = "\n".join(parts).rstrip() + "\n"
    duration_ms = int((time.monotonic() - start) * 1000)

    return ChannelOutput(
        body=body,
        media_type="text/markdown",
        duration_ms=duration_ms,
        provider="local",
        model="okuro-delivery-markdown",
    )


def _render_block(block) -> str:
    """Render one OutlineBlock as a markdown chunk."""
    btype = block.type
    if btype == "bullets":
        rows = [str(x).strip() for x in (block.data or []) if str(x).strip()]
        if not rows:
            return ""
        return "\n".join(f"- {r}" for r in rows)
    if btype == "prose":
        return (block.content or "").strip()
    if btype == "callout":
        return f"> **{(block.content or '').strip()}**"
    if btype == "quote":
        text = (block.content or "").strip()
        return "\n".join(f"> {l}" for l in text.splitlines() if l.strip())
    if btype == "kpi":
        rows = block.data or []
        if not rows:
            return ""
        # Render as a 2-column table: label | value
        lines = ["| Metric | Value |", "|--------|-------|"]
        for entry in rows:
            if isinstance(entry, dict):
                label = str(entry.get("label", "")).strip()
                value = str(entry.get("value", "")).strip()
                if label or value:
                    lines.append(f"| {label} | {value} |")
        return "\n".join(lines) if len(lines) > 2 else ""
    if btype == "table":
        rows = block.data or []
        if not rows or not isinstance(rows, list):
            return ""
        # data is a list of dicts (homogeneous keys preferred)
        headers: list[str] = []
        for r in rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in headers:
                        headers.append(k)
        if not headers:
            return ""
        out = ["| " + " | ".join(headers) + " |",
               "|" + "|".join(["---"] * len(headers)) + "|"]
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append("| " + " | ".join(str(r.get(h, "")).strip() for h in headers) + " |")
        return "\n".join(out)
    if btype == "cta":
        text = (block.content or "").strip()
        return f"**→ {text}**" if text else ""
    # Unknown block types degrade gracefully to their content string.
    return (block.content or "").strip()


# Register on import so `from okuro.peer.delivery import channels` triggers it.
from okuro.peer.delivery.channels import register  # noqa: E402

register("markdown", render)
