# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery — Stream C of the role-handover rewrite.
#   Audience-adapted renders of Stream B artifacts via 5-stage pipeline:
#   SourceDocument -> outline_for_recipient -> tokens_to_theme ->
#   channel.render -> deliveries.insert.
# index: imports | __all__
# AGENT_HEADER_END -->
"""peer.delivery — audience-adapted rendering of artifacts for recipients.

Reuses the cognitive side (peer/translate.py + peer/cognitive_profile.py)
and the brand side (stack_brand_resolve) that already exist. This package
adds the multi-modal delivery layer that connects them: outline-aware,
brand-themed, channel-rendered output stored in the new ``deliveries``
table (migration 036).

Public surface:

  delivery.send(artifact_id, person_id, channel, brand_id?) -> {delivery_id, ...}
  delivery.list(person_id?, artifact_id?, channel?, limit?) -> rows
  delivery.get(delivery_id, include_body?) -> row | None

  channels.register(name, renderer)
  channels.get(name) -> renderer

P1 channels: markdown, marp (PDF only — no PPTX), microsite.
P2 channels: tts (Kokoro local + ElevenLabs cloud).
P3 channels: podcast (Podcastfy multi-speaker).

okuro NEVER emits Microsoft proprietary formats (PPTX/DOCX/XLSX).
Open formats only — see convention memory 50b49e1c.

Hard rules:
  HR-C1: deliveries are NOT artifacts (parallel table; never artifact_write).
  HR-C2: peer.translate.person_translate is the only LLM hop in the
         pipeline. Channel renderers consume Outline + ThemeBundle and
         apply structural/format transforms only.
  HR-C3: pipeline.send is best-effort. Failures log + return error
         envelope, never raise into the orchestrator.
  HR-C4: body_blob for binaries; body for text; body_path only when
         blob > 1 MB (microsite tarballs, audio).
  HR-C5: voice_preset belongs to Brand, not Person.
"""

from okuro.peer.delivery.store import (
    delivery_write,
    delivery_get,
    delivery_list,
    delivery_delete,
)


def send(*args, **kwargs):
    """Lazy import so the package loads even before pipeline.py is wired
    in (channel modules import this package on registration)."""
    from okuro.peer.delivery.pipeline import send as _send
    return _send(*args, **kwargs)


__all__ = [
    "delivery_write",
    "delivery_get",
    "delivery_list",
    "delivery_delete",
    "send",
]
