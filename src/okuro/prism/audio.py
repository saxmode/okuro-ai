# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Audio-brief — prism's first MULTIMODAL block. Synthesize a facet's
#   point into a short spoken briefing on-box (Orpheus/Kokoro TTS) and embed it
#   as a self-contained data-URI `audiobrief` block. A board pre-read that talks,
#   generated locally — nothing leaves the machine.
# index:
#   def audio_brief
# AGENT_HEADER_END -->
"""okuro·prism audio-brief — the deck that speaks.

The first block in prism's multimodal class: it taps okuro's local TTS seam
(``peer.delivery.tts.synth`` — Orpheus when available, Kokoro floor otherwise,
no API key, no GPU required) to turn a facet's takeaway into a ~60-90s spoken
briefing. The audio is embedded as a base64 ``data:`` URI so the block is
self-contained — it survives the Resonance website export and needs no serving
route or auth token, matching prism's portable-block ethos. Board-confidential:
the synthesis runs on the RTX box, the bytes never touch a cloud.

Graceful: any TTS/encode fault returns ``{}`` (no ``src``) so the caller simply
skips attaching it — a deck is never blocked over optional audio.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Optional

log = logging.getLogger("okuro.prism.audio")

# A spoken exec brief is ~130-200 words (≈60-90s). Cap so a data-URI stays light
# (~1 MB) and the clip stays board-attention-sized.
_MAX_WORDS = 190


def audio_brief(
    text: str,
    *,
    voice: Optional[str] = None,
    role: str = "narrator",
    max_words: int = _MAX_WORDS,
    caption: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Synthesize ``text`` into a self-contained ``audiobrief`` block.

    Trims to ``max_words`` (a board-sized clip), synthesizes via the local TTS
    engine, and embeds the MP3 as a ``data:`` URI. Returns ``{}`` when the text
    is empty or synthesis fails — the caller then attaches nothing.
    """
    words = (text or "").split()
    if not words:
        return {}
    spoken = " ".join(words[:max_words])

    try:
        from okuro.peer.delivery import tts, voice_drone
        samples, sr = tts.synth(spoken, voice=voice, role=role)
        # okuro brand chord-drone bed. Before the write, so `duration` below
        # counts the bed rather than under-reporting the clip.
        samples = voice_drone.brand(samples, sr)
        import soundfile as sf
        buf = io.BytesIO()
        sf.write(buf, samples, sr, format="MP3")
        mp3 = buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — audio is optional, never block a deck
        log.warning("audio_brief synthesis failed: %s", exc)
        return {}
    if not mp3:
        return {}

    duration = round(len(samples) / float(sr or 24_000), 1)
    block: dict[str, Any] = {
        "type": "audiobrief",
        # MIME must be audio/mpeg (the registered type) — Chrome rejects the
        # non-standard audio/mp3 on a data URI with MEDIA_ERR_SRC_NOT_SUPPORTED.
        "src": "data:audio/mpeg;base64," + base64.b64encode(mp3).decode("ascii"),
        "duration": duration,
        "transcript": spoken,
    }
    try:
        block["voice"] = voice or tts.narrator_voice()
    except Exception:  # noqa: BLE001
        pass
    if caption:
        block["caption"] = caption
    if source_ref:
        block["source_ref"] = source_ref
    return block


__all__ = ["audio_brief"]
