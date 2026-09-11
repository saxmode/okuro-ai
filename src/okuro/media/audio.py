# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro MEDIA — the studio AUDIO/narration capability. Turn text into a
#   spoken audio clip on-box (okuro's local TTS: Orpheus/Kokoro, no API key, no
#   GPU) and index it in the okuro asset store as a first-class STUDIO media asset
#   (source=studio, kind=audio) — the audio parallel of media.illustrate. So a
#   podcast/narration is a durable, reusable deliverable, not only a delivery
#   byproduct.
# index: def narrate
# AGENT_HEADER_END -->
"""okuro studio audio capability — text → spoken clip → asset store.

Audio synthesis already exists in okuro, but only as a DELIVERY byproduct
(``peer.delivery`` writes morning-brief MP3s, indexed source=delivery). This is
the STUDIO seam: an on-demand ``narrate(text)`` that produces a spoken clip via
okuro's own TTS (``peer.delivery.tts.synth`` — Orpheus when available, Kokoro
floor otherwise; no API key, no GPU) and indexes it as a studio media asset, so
it lands in the same asset bucket as images and illustrations. Graceful: any TTS
/encode fault returns ``{}``.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Optional

log = logging.getLogger("okuro.media.audio")

# A spoken clip is capped so a data-URI stays light and attention-sized.
_MAX_WORDS = 400


def _persist(data: bytes, *, title: str, meta: dict) -> Optional[str]:
    try:
        from okuro.assets.store import register_asset

        row = register_asset(
            kind="audio", source="studio", data=data, mime="audio/mpeg",
            folder="narrations", title=title[:120], meta=meta,
            tags=["audio", "narration", meta.get("voice") or "tts"],
        )
        return row.get("id")
    except Exception as exc:  # noqa: BLE001 — persistence never blocks the deliverable
        log.info("narrate: asset persist skipped: %s", exc)
        return None


def narrate(
    text: str,
    *,
    voice: Optional[str] = None,
    role: str = "narrator",
    title: Optional[str] = None,
    max_words: int = _MAX_WORDS,
    persist: bool = True,
) -> dict[str, Any]:
    """Synthesize ``text`` into a spoken MP3 via okuro's local TTS and (optionally)
    index it in the asset store as a studio audio asset. Returns ``{}`` on any
    failure, else ``{kind:"audio", src, duration, transcript, voice, asset_id?}``.
    Never raises."""
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
    except Exception as exc:  # noqa: BLE001 — audio is optional, never raise
        log.info("narrate: TTS synthesis unavailable: %s", exc)
        return {}
    if not mp3:
        return {}

    duration = round(len(samples) / float(sr or 24_000), 1)
    try:
        resolved_voice = voice or tts.narrator_voice()
    except Exception:  # noqa: BLE001
        resolved_voice = voice
    ttl = title or spoken[:60]

    block: dict[str, Any] = {
        "kind": "audio",
        # audio/mpeg (not audio/mp3) — Chrome rejects the non-standard mime on a data URI.
        "src": "data:audio/mpeg;base64," + base64.b64encode(mp3).decode("ascii"),
        "duration": duration,
        "transcript": spoken,
        "voice": resolved_voice,
        "caption": ttl,
    }
    if persist:
        aid = _persist(mp3, title=ttl, meta={"voice": resolved_voice, "duration": duration, "role": role})
        if aid:
            block["asset_id"] = aid
    return block


__all__ = ["narrate"]
