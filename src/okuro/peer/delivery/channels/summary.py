# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.channels.summary — single-voice audio summary (the
#   "tts" channel). Outline -> friendly spoken monologue (LLM, deterministic
#   fallback) -> per-segment synth via tts.synth(role="narrator") -> MP3.
#   The counterpart to the two-host `podcast` channel: one warm narrator
#   explaining what happened, not a chat.
# index: imports | def render | def _narration | def _fallback_narration |
#   def _segments | def _transcript
# AGENT_HEADER_END -->
"""Audio-summary channel (single-voice ``tts``).

Two audio modes share one voice engine (``tts.synth`` → Orpheus, Kokoro
fallback) but differ in FORM:
  - ``podcast``  — two hosts chatting about a topic (channels/podcast.py)
  - ``tts``      — one friendly narrator explaining what went on (this file)

The narrator monologue is generated with a single bridge hop (warm, spoken,
facts preserved) and degrades to a deterministic read of the Outline when the
bridge is absent (HR-C3). Audio is synthesised per segment so long briefs stay
within the model's token window, then concatenated with short breaths and
RMS-normalised (reusing the podcast channel's normaliser).

Never raises into the caller — failures land on ChannelOutput.error.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a warm, friendly host recording a short spoken audio summary — one "
    "voice, talking directly to the listener, explaining what happened. NOT a "
    "two-person chat, NOT a list of bullet points read aloud. Sound like a "
    "knowledgeable friend catching someone up: natural, clear, a little warm.\n"
    "- Open with a one-line hook, then walk through the key points in plain "
    "spoken language, then close with a short takeaway.\n"
    "- Preserve every fact; invent nothing. No headings, no bullet markers, no "
    "stage directions — just what you'd actually say.\n"
    "- Punctuation for spoken rhythm: commas for breath, em-dashes for asides, "
    "short sentences. Output ONLY the spoken text."
)


def render(outline, theme) -> "ChannelOutput":  # noqa: F821
    from okuro.peer.delivery.channels import ChannelOutput

    start = time.monotonic()
    try:
        from okuro.peer.delivery import tts
        from okuro.peer.delivery.channels.podcast import _normalize, _storage_root
        import numpy as np
        import soundfile as sf
    except Exception as exc:
        return ChannelOutput(
            error=f"audio deps missing: {exc}",
            duration_ms=int((time.monotonic() - start) * 1000),
            provider="local", model="okuro-delivery-summary",
        )

    try:
        from okuro.peer.delivery import progress

        # Narrator voice: brand preset override, else the active engine's default.
        preset = {}
        try:
            preset = (theme.channel_specific or {}).get("voice_preset") or {}
        except Exception:
            preset = {}
        voice = str(preset.get("narrator") or preset.get("voice") or "") or None

        text = _narration(outline)
        if not text.strip():
            raise RuntimeError("empty narration")
        progress.report("writing summary", 0.28)

        segments = _segments(text)
        engine = tts.active_engine()
        sr = tts.SAMPLE_RATE
        mix = None

        # MOSS (pro edition) renders the whole narration in one model load —
        # untagged text = single narrator voice. Fault → per-segment fallback.
        if engine == "moss":
            try:
                progress.report("render summary", 0.35)
                samples, sr = tts.synth_dialogue(text.strip())
                if len(samples):
                    mix = _normalize(np.asarray(samples, dtype="float32"), np)
            except Exception as exc:
                log.warning("moss summary render failed (%s) — per-segment fallback", exc)
                mix, engine = None, "kokoro"

        if mix is None:
            clips: list["np.ndarray"] = []
            total = len(segments) or 1
            gap = np.zeros(int(0.28 * sr), dtype="float32")
            for idx, seg in enumerate(segments):
                samples, sr = tts.synth(seg, voice, role="narrator", engine=engine)
                if len(samples):
                    clips.append(np.asarray(samples, dtype="float32"))
                    clips.append(gap)
                progress.report("render summary", 0.30 + 0.60 * ((idx + 1) / total))
            if not clips:
                raise RuntimeError("no audio produced")
            mix = _normalize(np.concatenate(clips), np)

        progress.report("conversion to mp3", 0.93)

        # okuro brand chord-drone bed. Before the write, so audio_seconds counts it.
        from okuro.peer.delivery import voice_drone
        mix = voice_drone.brand(mix, sr)

        out_path = _storage_root() / f"summary-{uuid.uuid4().hex[:12]}.mp3"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out_path), mix, sr, format="MP3")
        try:  # index into the assets media bucket (never block a delivery)
            from okuro.assets import store as _assets
            _assets.register_delivery_file(out_path)
        except Exception:  # noqa: BLE001
            pass

        audio_seconds = round(len(mix) / sr, 1)
        return ChannelOutput(
            body=_transcript(outline, text),
            body_path=str(out_path),
            media_type="audio/mpeg",
            duration_ms=int((time.monotonic() - start) * 1000),
            provider="local",
            model={"moss": "moss-ttsd-v1.0", "orpheus": "orpheus-3b-q8"}.get(
                engine, "kokoro-v1.0-fp32"),
            extras={
                "audio_seconds": audio_seconds,
                "segments": len(segments),
                "voice": voice or tts.narrator_voice(),
                "wav_path": str(out_path),
                "wav_bytes": out_path.stat().st_size,
                "mode": "summary",
            },
        )
    except Exception as exc:
        log.exception("summary channel failed")
        return ChannelOutput(
            error=f"summary render failed: {exc}",
            duration_ms=int((time.monotonic() - start) * 1000),
            provider="local", model="okuro-delivery-summary",
        )


def _narration(outline) -> str:
    """One bridge hop → friendly spoken monologue; deterministic fallback."""
    from okuro.peer.delivery.channels.podcast import _outline_text

    briefing = _outline_text(outline)
    prompt = (
        f"Turn this briefing into a short, friendly spoken audio summary "
        f"(roughly 150-300 words):\n\n{briefing}\n\nOutput ONLY the spoken text."
    )
    try:
        from okuro.bridge.invoke import invoke
        result = invoke(prompt, capability="translate", system_prompt=_SYSTEM, timeout=120)
        if isinstance(result, dict) and result.get("success"):
            out = str(result.get("output") or "").strip()
            if out:
                return out
    except Exception as exc:
        log.info("summary: bridge invoke failed (%s); using fallback", exc)
    return _fallback_narration(outline)


def _fallback_narration(outline) -> str:
    """Deterministic warm read straight from the Outline — no LLM."""
    from okuro.peer.delivery.channels.podcast import _section_points

    parts: list[str] = []
    title = outline.title or "today's update"
    parts.append(f"Here's a quick summary of {title}.")
    if getattr(outline, "tldr", ""):
        parts.append(str(outline.tldr))
    for section in outline.sections:
        pts = _section_points(section)
        if not pts:
            continue
        parts.append(f"On {section.heading}:")
        parts.extend(pts)
    parts.append("That's the summary — thanks for listening.")
    return " ".join(p.rstrip(".") + "." for p in parts if p.strip())


def _segments(text: str, max_chars: int = 320) -> list[str]:
    """Split narration into synth-sized chunks on sentence boundaries.

    Keeps each chunk under ~max_chars so Orpheus stays within its token window
    and progress reports are granular. Never splits mid-sentence.
    """
    sentences = re.split(r"(?<=[.!?…])\s+", text.strip())
    segments: list[str] = []
    buf = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if buf and len(buf) + 1 + len(s) > max_chars:
            segments.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        segments.append(buf)
    return segments


def _transcript(outline, text: str) -> str:
    head = f"# {getattr(outline, 'title', None) or 'Audio summary'}"
    return f"{head}\n\n{text.strip()}\n"


from okuro.peer.delivery.channels import register  # noqa: E402

register("tts", render)
