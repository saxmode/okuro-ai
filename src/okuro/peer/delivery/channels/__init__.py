# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.channels — registry of channel renderers.
#   Renderers consume (Outline, ThemeBundle) and return ChannelOutput
#   (body / body_blob / body_path / media_type). Pluggable: P2/P3
#   channels register themselves on import without modifying core.
# index: imports | dataclass ChannelOutput | def register | def get |
#   def list_channels | _REGISTRY
# AGENT_HEADER_END -->
"""Channel renderer registry.

Each channel is a callable ``render(outline, theme) -> ChannelOutput``.
Renderers must:
  - Be pure-ish: deterministic given the same inputs (subprocess
    timeouts and cost are the only allowed sources of variance).
  - Never raise into the caller. Errors land on ``ChannelOutput.error``.
  - Honour HR-C4: text payloads in ``body``, binaries in ``body_blob``,
    files >1MB on disk via ``body_path``.

P1 registers: markdown, marp (PDF — no PPTX), microsite. P2: tts.
P3: podcast. PPTX/DOCX/XLSX are banned (memory 50b49e1c).

The registry is intentionally process-local. Reloading the package
re-registers the built-ins; tests can call ``register`` to swap in a
fake channel without monkeypatching internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ChannelOutput:
    """What a channel renderer produces. One of body / body_blob /
    body_path will be set, the other two empty.
    """

    body: str | None = None
    body_blob: bytes | None = None
    body_path: str | None = None
    media_type: str = "text/plain"
    duration_ms: int | None = None
    cost_usd: float | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return not self.error and (
            (self.body is not None and self.body != "")
            or self.body_blob is not None
            or self.body_path is not None
        )


Renderer = Callable[[Any, Any], ChannelOutput]
# Effective signature: render(outline: Outline, theme: ThemeBundle) -> ChannelOutput
# We type the args as Any to avoid a circular import; renderers cast.


_REGISTRY: dict[str, Renderer] = {}


def register(name: str, renderer: Renderer) -> None:
    """Register a renderer for ``name``. Last-write wins (test-friendly)."""
    _REGISTRY[name] = renderer


def get(name: str) -> Optional[Renderer]:
    """Resolve a channel name to its renderer (None if unknown)."""
    return _REGISTRY.get(name)


def list_channels() -> list[str]:
    """Sorted list of currently-registered channel names."""
    return sorted(_REGISTRY.keys())


# ── Built-in registrations ──────────────────────────────────────────
# Imports below register on module load. Order matters only for the
# list_channels() output (sorted post-hoc anyway).

from okuro.peer.delivery.channels import markdown as _markdown_channel  # noqa: E402,F401
from okuro.peer.delivery.channels import marp as _marp_channel  # noqa: E402,F401
from okuro.peer.delivery.channels import microsite as _microsite_channel  # noqa: E402,F401
from okuro.peer.delivery.channels import podcast as _podcast_channel  # noqa: E402,F401
from okuro.peer.delivery.channels import summary as _summary_channel  # noqa: E402,F401
