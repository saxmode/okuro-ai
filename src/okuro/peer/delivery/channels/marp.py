# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.channels.marp — Marp slides channel.
#   Renders an Outline as Marp markdown with embedded brand CSS and
#   (best-effort) shells out to marp-cli to materialise a PDF.
#   When marp-cli is missing the body alone is returned; the
#   delivery row stays valid (HR-C3). PDF only — no PPTX.
# index: imports | def render | def _to_marp_md | def _try_marp_cli
# AGENT_HEADER_END -->
"""Marp slides renderer (P1).

Marp is a markdown -> slide deck tool. okuro restricts Marp output to
PDF (and the source markdown). PPTX is BANNED — okuro never emits
Microsoft proprietary formats (convention memory 50b49e1c). Recipients
who insist on .pptx convert from PDF on their side.

The renderer emits Marp-flavoured markdown with the brand's CSS
embedded in frontmatter so a downstream `marp <file>` invocation can
render deterministically. We attempt to invoke `marp-cli` via
subprocess to attach a PDF blob; missing-binary degrades to body-only.

slides_per_section is the only structural choice that differs from
the markdown channel: each Outline section becomes its own slide.
TLDR + title get their own intro slide.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)


_MARP_BIN_CANDIDATES = ("marp", "marp-cli")
_MARP_TIMEOUT_S = 30.0


def render(outline, theme) -> "ChannelOutput":  # noqa: F821
    """Render Outline -> Marp markdown (+ optional PDF blob).

    PDF only — PPTX is banned (no Microsoft proprietary formats).
    """
    from okuro.peer.delivery.channels import ChannelOutput, register  # noqa: F401

    start = time.monotonic()
    md = _to_marp_md(outline, theme)

    blob, blob_media_type, marp_error = _try_marp_cli(md)

    duration_ms = int((time.monotonic() - start) * 1000)
    return ChannelOutput(
        body=md,
        body_blob=blob,
        media_type="text/markdown" if blob is None else blob_media_type,
        duration_ms=duration_ms,
        provider="marp" if blob else "local",
        model="marp-cli" if blob else "okuro-delivery-marp-md",
        error=marp_error,
        extras={
            "marp_md_bytes": len(md.encode("utf-8")),
            "blob_bytes": len(blob) if blob else 0,
            "marp_cli_available": blob is not None or marp_error is None,
        },
    )


# ----------------------------------------------------------------------
# Marp markdown emitter
# ----------------------------------------------------------------------


def _to_marp_md(outline, theme) -> str:
    css = ""
    if isinstance(theme.channel_specific, dict):
        css = theme.channel_specific.get("marp_css") or ""

    bg = (theme.tokens.get("color", {}) or {}).get("background", "#0a0a0a")
    fg = (theme.tokens.get("color", {}) or {}).get("foreground", "#e5e5e5")
    primary = (theme.tokens.get("color", {}) or {}).get("primary", "#22c55e")

    fm = [
        "---",
        "marp: true",
        f"theme: okuro-delivery",
        "paginate: true",
        f"backgroundColor: {bg}",
        f"color: {fg}",
        "---",
    ]
    if css:
        fm.append("")
        fm.append("<style>")
        fm.append(css)
        fm.append("</style>")
        fm.append("")

    out: list[str] = ["\n".join(fm), ""]

    # Slide 1 — title
    title = outline.title or "Untitled"
    out.append(f"# {title}")
    if outline.subtitle:
        out.append("")
        out.append(f"_{outline.subtitle}_")

    if outline.tldr:
        out.append("")
        out.append("---")
        out.append("")
        out.append("## TL;DR")
        out.append("")
        out.append(outline.tldr)

    for section in outline.sections:
        out.append("")
        out.append("---")
        out.append("")
        out.append(f"## {section.heading}")
        out.append("")
        for block in section.blocks:
            chunk = _block_to_marp(block, primary=primary)
            if chunk:
                out.append(chunk)
                out.append("")

    if outline.next_steps:
        out.append("")
        out.append("---")
        out.append("")
        out.append("## Next Steps")
        out.append("")
        for step in outline.next_steps:
            out.append(f"- {step}")

    return ("\n".join(out)).rstrip() + "\n"


def _block_to_marp(block, *, primary: str) -> str:
    """Same dialect as markdown channel; Marp accepts standard md."""
    if block.type == "bullets":
        rows = [str(x).strip() for x in (block.data or []) if str(x).strip()]
        if not rows:
            return ""
        return "\n".join(f"- {r}" for r in rows)
    if block.type == "callout":
        return f"> **{(block.content or '').strip()}**"
    if block.type == "cta":
        text = (block.content or "").strip()
        return f"<p style=\"color:{primary};font-weight:bold\">→ {text}</p>" if text else ""
    if block.type == "kpi":
        rows = block.data or []
        if not rows:
            return ""
        lines = ["| Metric | Value |", "|--------|-------|"]
        for entry in rows:
            if isinstance(entry, dict):
                label = str(entry.get("label", "")).strip()
                value = str(entry.get("value", "")).strip()
                if label or value:
                    lines.append(f"| {label} | {value} |")
        return "\n".join(lines) if len(lines) > 2 else ""
    if block.type == "table":
        rows = block.data or []
        if not rows:
            return ""
        headers: list[str] = []
        for r in rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in headers:
                        headers.append(k)
        if not headers:
            return ""
        lines = ["| " + " | ".join(headers) + " |",
                 "|" + "|".join(["---"] * len(headers)) + "|"]
        for r in rows:
            if not isinstance(r, dict):
                continue
            lines.append("| " + " | ".join(str(r.get(h, "")).strip() for h in headers) + " |")
        return "\n".join(lines)
    return (block.content or "").strip()


# ----------------------------------------------------------------------
# marp-cli subprocess (best-effort)
# ----------------------------------------------------------------------


def _find_marp_bin() -> str | None:
    for name in _MARP_BIN_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _try_marp_cli(md: str) -> tuple[bytes | None, str, str | None]:
    """Render md -> PDF bytes via marp-cli. Returns (blob, media_type, error).

    PDF only. PPTX is banned by convention (no Microsoft proprietary
    formats — see memory 50b49e1c). Skipped silently when the binary
    is absent — caller still gets the md body. Per HR-C3 we never raise.
    """
    binary = _find_marp_bin()
    if not binary:
        return None, "application/pdf", None

    import tempfile
    with tempfile.TemporaryDirectory(prefix="okuro-marp-") as tmp:
        tmp_dir = Path(tmp)
        in_path = tmp_dir / "in.md"
        out_path = tmp_dir / "out.pdf"
        in_path.write_text(md, encoding="utf-8")

        cmd = [binary, "--pdf", "--allow-local-files",
               "-o", str(out_path), str(in_path)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=_MARP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return None, "", f"marp-cli timed out after {_MARP_TIMEOUT_S}s"
        except Exception as exc:
            return None, "", f"marp-cli invocation failed: {exc}"

        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", errors="replace")[:500]
            return None, "", f"marp-cli exited {result.returncode}: {stderr}"

        if not out_path.exists():
            return None, "", "marp-cli produced no output file"
        blob = out_path.read_bytes()
        return blob, "application/pdf", None


# Register on import.
from okuro.peer.delivery.channels import register  # noqa: E402

register("marp", render)
