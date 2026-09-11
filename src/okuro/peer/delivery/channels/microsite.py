# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.channels.microsite — Astro+Tailwind static site.
#   Emits a self-contained Astro project tree (package.json + astro.config
#   + tailwind config + index.astro + content) as a tar.gz, written to
#   ``OKURO_DELIVERY_STORAGE`` (default ~/.okuro/deliveries) and recorded
#   in body_path.
# index: imports | def render | def _build_tarball | def _astro_index |
#   def _package_json | def _astro_config
# AGENT_HEADER_END -->
"""Microsite renderer (P1).

Per the Stream C plan (artifact 7243a962): a brand-themed static site
generated per-recipient, deliverable as a tarball or deployable folder.

Tarball layout (paths inside the archive):
  package.json
  astro.config.mjs
  tailwind.config.mjs
  src/pages/index.astro
  src/styles/global.css
  README.md

The tarball lands on disk because Astro builds need ``npm install`` —
storing the rendered HTML directly would lock us to one bundling
choice. Recipients (or downstream channels) run `npm i && npm run
build` to produce the static output.

HR-C4: tarballs go to ``body_path`` (not body / body_blob).
"""

from __future__ import annotations

import io
import json
import logging
import os
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


def _delivery_storage_root() -> Path:
    """Root dir for tarball storage. Honours OKURO_DELIVERY_STORAGE env."""
    override = os.environ.get("OKURO_DELIVERY_STORAGE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".okuro" / "deliveries"


# ----------------------------------------------------------------------
# Public renderer
# ----------------------------------------------------------------------


def render(outline, theme) -> "ChannelOutput":  # noqa: F821
    from okuro.peer.delivery.channels import ChannelOutput

    start = time.monotonic()
    try:
        path, summary_md = _build_tarball(outline, theme)
    except Exception as exc:
        log.warning("microsite render failed: %s", exc)
        return ChannelOutput(
            error=f"microsite render failed: {exc}",
            duration_ms=int((time.monotonic() - start) * 1000),
            provider="local",
            model="okuro-delivery-microsite",
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    return ChannelOutput(
        body=summary_md,                # human-readable site summary
        body_path=str(path),            # tarball on disk (HR-C4)
        media_type="application/gzip",
        duration_ms=duration_ms,
        provider="local",
        model="okuro-delivery-microsite",
        extras={
            "tarball_bytes": path.stat().st_size,
            "tarball_path": str(path),
        },
    )


# ----------------------------------------------------------------------
# Tarball build
# ----------------------------------------------------------------------


def _build_tarball(outline, theme):
    storage = _delivery_storage_root()
    storage.mkdir(parents=True, exist_ok=True)
    site_id = uuid.uuid4().hex[:12]
    out_path = storage / f"microsite-{site_id}.tar.gz"

    # Build everything in memory then flush to disk in one go.
    files: dict[str, str] = {
        "package.json":         _package_json(outline.title or "delivery"),
        "astro.config.mjs":     _astro_config(),
        "tailwind.config.mjs":  _tailwind_config(theme),
        "src/styles/global.css": _global_css(theme),
        "src/pages/index.astro": _astro_index(outline, theme),
        "README.md":            _readme(outline.title or "delivery"),
    }

    with tarfile.open(out_path, "w:gz") as tar:
        for arcname, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))

    summary_md = (
        f"# Microsite — {outline.title or 'delivery'}\n\n"
        f"Generated Astro+Tailwind static site for recipient delivery.\n"
        f"\n"
        f"## Tarball\n\n"
        f"`{out_path}`\n\n"
        f"## Build\n\n"
        f"```bash\n"
        f"tar -xzf {out_path.name}\n"
        f"cd microsite-{site_id} || cd .\n"
        f"npm install\n"
        f"npm run build\n"
        f"```\n"
        f"\n"
        f"Output: `dist/` — static HTML/CSS, ready to host on any CDN.\n"
    )
    return out_path, summary_md


# ----------------------------------------------------------------------
# File contents
# ----------------------------------------------------------------------


def _package_json(title: str) -> str:
    pkg = {
        "name": _slug(title),
        "version": "0.1.0",
        "type": "module",
        "scripts": {
            "dev": "astro dev",
            "start": "astro dev",
            "build": "astro build",
            "preview": "astro preview",
        },
        "dependencies": {
            "astro": "^5.0.0",
            "@astrojs/tailwind": "^5.1.0",
            "tailwindcss": "^4.0.0",
        },
    }
    return json.dumps(pkg, indent=2) + "\n"


def _astro_config() -> str:
    return (
        "import { defineConfig } from 'astro/config';\n"
        "import tailwind from '@astrojs/tailwind';\n"
        "\n"
        "export default defineConfig({\n"
        "  integrations: [tailwind({ applyBaseStyles: false })],\n"
        "});\n"
    )


def _tailwind_config(theme) -> str:
    cfg = (theme.channel_specific or {}).get("tailwind_config") or {}
    return (
        "/** @type {import('tailwindcss').Config} */\n"
        "export default "
        + json.dumps(
            {
                "content": ["./src/**/*.{astro,html,js,jsx,ts,tsx}"],
                **cfg,
            },
            indent=2,
        )
        + ";\n"
    )


def _global_css(theme) -> str:
    color = (theme.tokens or {}).get("color", {}) or {}
    return (
        "@tailwind base;\n"
        "@tailwind components;\n"
        "@tailwind utilities;\n"
        "\n"
        "@layer base {\n"
        "  :root {\n"
        f"    --brand-primary: {color.get('primary', '#22c55e')};\n"
        f"    --brand-bg:      {color.get('background', '#0a0a0a')};\n"
        f"    --brand-fg:      {color.get('foreground', '#e5e5e5')};\n"
        f"    --brand-muted:   {color.get('muted', '#737373')};\n"
        "  }\n"
        "  body {\n"
        "    background-color: var(--brand-bg);\n"
        "    color: var(--brand-fg);\n"
        "    font-family: 'JetBrains Mono', monospace;\n"
        "  }\n"
        "}\n"
    )


def _astro_index(outline, theme) -> str:
    parts = [
        "---",
        "import '../styles/global.css';",
        "---",
        "",
        "<html lang=\"en\">",
        "  <head>",
        "    <meta charset=\"utf-8\" />",
        "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />",
        f"    <title>{_html_escape(outline.title or 'Delivery')}</title>",
        "  </head>",
        "  <body class=\"min-h-screen\">",
        "    <main class=\"max-w-3xl mx-auto px-6 py-16 space-y-10\">",
        f"      <h1 class=\"text-4xl font-bold text-[color:var(--brand-primary)]\">{_html_escape(outline.title or 'Delivery')}</h1>",
    ]
    if outline.subtitle:
        parts.append(
            f"      <p class=\"text-lg italic text-[color:var(--brand-muted)]\">{_html_escape(outline.subtitle)}</p>"
        )
    if outline.tldr:
        parts.extend([
            "      <section class=\"border-l-4 border-[color:var(--brand-primary)] pl-4\">",
            "        <h2 class=\"text-xs uppercase tracking-widest text-[color:var(--brand-muted)]\">TL;DR</h2>",
            f"        <p class=\"mt-2\">{_html_escape(outline.tldr)}</p>",
            "      </section>",
        ])
    for section in outline.sections:
        parts.extend([
            "      <section class=\"space-y-3\">",
            f"        <h2 class=\"text-2xl font-semibold text-[color:var(--brand-primary)]\">{_html_escape(section.heading)}</h2>",
        ])
        for block in section.blocks:
            parts.append(_block_to_html(block))
        parts.append("      </section>")
    if outline.next_steps:
        parts.extend([
            "      <section class=\"space-y-3\">",
            "        <h2 class=\"text-2xl font-semibold text-[color:var(--brand-primary)]\">Next Steps</h2>",
            "        <ul class=\"list-disc pl-6 space-y-1\">",
        ])
        for step in outline.next_steps:
            parts.append(f"          <li>{_html_escape(step)}</li>")
        parts.append("        </ul>")
        parts.append("      </section>")

    parts.extend([
        "    </main>",
        "  </body>",
        "</html>",
        "",
    ])
    return "\n".join(parts)


def _block_to_html(block) -> str:
    indent = "        "
    if block.type == "bullets":
        rows = [str(x).strip() for x in (block.data or []) if str(x).strip()]
        if not rows:
            return ""
        out = [f"{indent}<ul class=\"list-disc pl-6 space-y-1\">"]
        for r in rows:
            out.append(f"{indent}  <li>{_html_escape(r)}</li>")
        out.append(f"{indent}</ul>")
        return "\n".join(out)
    if block.type == "prose":
        return f"{indent}<p>{_html_escape((block.content or '').strip())}</p>"
    if block.type == "callout":
        text = _html_escape((block.content or "").strip())
        return (
            f"{indent}<div class=\"border border-[color:var(--brand-muted)] "
            f"px-4 py-2 text-[color:var(--brand-primary)] font-semibold\">{text}</div>"
        )
    if block.type == "cta":
        text = _html_escape((block.content or "").strip())
        return (
            f"{indent}<p class=\"text-xl font-bold text-[color:var(--brand-primary)]\">→ {text}</p>"
        )
    return f"{indent}<p>{_html_escape((block.content or '').strip())}</p>"


def _readme(title: str) -> str:
    return (
        f"# {title}\n\n"
        "Astro + Tailwind static microsite generated by okuro Stream C.\n\n"
        "## Build\n\n"
        "```bash\n"
        "npm install\n"
        "npm run build\n"
        "```\n"
    )


def _slug(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("-")
    slug = "".join(out).strip("-")[:60] or "delivery"
    return slug


def _html_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Register on import.
from okuro.peer.delivery.channels import register  # noqa: E402

register("microsite", render)
