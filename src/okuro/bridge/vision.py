# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The bridge's VISION seam — image INPUT (image -> text), the mirror of media.py.
# index: imports | def vision_config | def providers_with_vision | def normalize_images | def build_image_argv | def apply_path_in_prompt | def to_content_parts
# AGENT_HEADER_END -->
"""The bridge's VISION seam — image INPUT, the mirror of :mod:`okuro.bridge.media`.

``media.py`` is generation: a prompt goes in, image FILES come out.
This module is the other direction: image files go in, text comes out.
The two never share a code path and must not be confused.

THREE MECHANISMS, one per provider shape, declared in the provider's
``vision`` block in :mod:`okuro.bridge.config`:

``flag``
    The CLI has a real image-attach flag. Only ``codex`` does
    (``-i/--image <FILE>...``). **That flag is VARIADIC**, so a trailing
    positional prompt is swallowed as another filename unless ``--``
    separates them — measured 2026-09-06, the failure is silent and reads
    as "No prompt provided via stdin".

``path-in-prompt``
    The CLI has no image flag but its agent can read a local file with its
    built-in read tool (``claude``, ``antigravity``). We name absolute paths
    in the prompt and instruct it to look. Verified end to end against both,
    including under ``invoke(tool=True)`` — the read tool is built in, not
    MCP, so ``--strict-mcp-config`` does not remove it.

``content-parts``
    OpenAI-compatible ``image_url`` parts with a base64 data URI, for
    ``local-http`` providers serving a VLM.

**A NOTE ON FLAG NAMES.** ``-i`` means ``--image`` on codex and
``--prompt-interactive`` on antigravity. Nothing here may assume a flag
spelling is portable; every one is read from the provider's own block.
"""

import base64
import mimetypes
from pathlib import Path

from .config import get_provider, load_config

# Extensions every target accepts. Deliberately conservative — an
# unsupported file reaching a CLI produces a provider-specific error that
# is far harder to read than this one.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

# Per-image ceiling. Above this the CLIs get slow and the local-http
# base64 payload stops being reasonable to hold in memory.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

DEFAULT_MAX_IMAGES = 8


class VisionUnsupported(Exception):
    """Raised when images are passed to a provider that declares no vision block.

    Never degrade this to "send the prompt without the images". A judge that
    silently scores a picture it never saw returns confident noise, which is
    worse than an error.
    """


def vision_config(provider_id: str) -> dict | None:
    """The provider's ``vision`` block, or None when it declares none."""
    provider = get_provider(provider_id)
    if not provider:
        return None
    block = provider.get("vision")
    return block if isinstance(block, dict) else None


def providers_with_vision() -> list[str]:
    """Provider ids declaring a vision block, in config order."""
    config = load_config()
    out = []
    for pid, provider in (config.get("providers") or {}).items():
        if isinstance(provider.get("vision"), dict):
            out.append(pid)
    return out


def normalize_images(images) -> list[str]:
    """Validate and absolutise image paths.

    Raises ValueError naming the offending path. Callers surface it in the
    standard ``{success: False, error: ...}`` dict — a bad path must never
    reach a CLI, where it becomes an opaque provider error.
    """
    if images is None:
        return []
    if isinstance(images, (str, Path)):
        images = [images]
    try:
        candidates = list(images)
    except TypeError:
        raise ValueError(f"images must be a path or a list of paths, got {type(images).__name__}")

    out: list[str] = []
    for raw in candidates:
        if not isinstance(raw, (str, Path)):
            raise ValueError(f"image entry must be a path, got {type(raw).__name__}")
        path = Path(str(raw)).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise ValueError(f"image not found: {raw}")
        if not path.is_file():
            raise ValueError(f"image is not a file: {path}")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError(
                f"unsupported image type {path.suffix!r}: {path} "
                f"(supported: {', '.join(sorted(IMAGE_SUFFIXES))})"
            )
        size = path.stat().st_size
        if size == 0:
            raise ValueError(f"image is empty: {path}")
        if size > MAX_IMAGE_BYTES:
            raise ValueError(
                f"image is {size / 1e6:.1f} MB, over the "
                f"{MAX_IMAGE_BYTES / 1e6:.0f} MB per-image limit: {path}"
            )
        out.append(str(path))
    return out


def check_supported(provider_id: str, images: list[str]) -> dict | None:
    """Return the vision block for ``provider_id``, or raise VisionUnsupported.

    No-op (returns None) when ``images`` is empty, so every call site can
    invoke it unconditionally.
    """
    if not images:
        return None
    block = vision_config(provider_id)
    if not block:
        able = providers_with_vision()
        raise VisionUnsupported(
            f"provider {provider_id!r} declares no vision block, so it cannot "
            f"accept images. Providers that can: {', '.join(able) or 'none'}. "
            f"Route with capability='vision' or provider=<one of those>."
        )
    limit = int(block.get("max_images", DEFAULT_MAX_IMAGES))
    if len(images) > limit:
        raise VisionUnsupported(
            f"{len(images)} images passed but provider {provider_id!r} accepts "
            f"at most {limit}"
        )
    return block


def build_image_argv(block: dict, images: list[str]) -> list[str]:
    """Argv fragment attaching ``images`` for a ``flag`` mechanism provider."""
    flag = block.get("flag")
    if not flag:
        raise ValueError("vision mechanism 'flag' requires a 'flag' key")
    if block.get("repeat_flag"):
        out: list[str] = []
        for image in images:
            out.extend([flag, image])
        return out
    return [flag, *images]


def apply_path_in_prompt(prompt: str, images: list[str], block: dict | None = None) -> str:
    """Prefix ``prompt`` with an instruction to open each image path.

    Deliberately explicit and first: a path mentioned only in passing, or
    buried after a long prompt, gets skipped by an agent optimising for the
    task it was given.
    """
    if not images:
        return prompt
    template = (block or {}).get("instruction")
    listing = "\n".join(f"- {p}" for p in images)
    noun = "image" if len(images) == 1 else "images"
    if template:
        header = template.format(paths=listing, count=len(images), noun=noun)
    else:
        header = (
            f"First, open and look at the following {noun} using your file-read "
            f"tool. Do this before answering — the answer depends on what they "
            f"show.\n{listing}\n"
        )
    return f"{header}\n{prompt}"


def to_content_parts(prompt: str, images: list[str]) -> list[dict]:
    """OpenAI-compatible multimodal ``content`` array for a local-http provider."""
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for image in images:
        mime = mimetypes.guess_type(image)[0] or "image/png"
        encoded = base64.b64encode(Path(image).read_bytes()).decode("ascii")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        })
    return parts
