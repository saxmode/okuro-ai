# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.channels.podcast — two-host audio podcast (P3).
#   Outline -> 2-host dialogue (LLM) -> choreographed timing track
#   (over-blend / ducking / holds) -> per-line Kokoro synth -> numpy mix
#   -> WAV on disk (body_path). The choreography is okuro-owned IP and is
#   backend-agnostic (renders identically on any tts backend).
# index: imports | dataclass DialogueLine | def render | def _generate_dialogue |
#   def _fallback_dialogue | def _outline_text | def _voices_for | def _choreograph
#   def _mix | def _normalize | def _transcript | def _storage_root | def _extract_json
# AGENT_HEADER_END -->
"""Podcast channel renderer (P3).

Turns a recipient-adapted Outline into a natural-sounding two-host
conversation. The Outline is ALREADY lens-adapted upstream
(outline_for_recipient → person_translate), so this channel's only job is
to dramatise it as dialogue and choreograph the audio — the recipient's
"words" are baked in before we get here.

Pipeline (all local, no GPU, no key):
  1. _generate_dialogue   — one LLM hop (bridge) → tagged 2-host script.
                            Deterministic fallback if the bridge is absent,
                            so audio is produced even offline (HR-C3).
  2. _voices_for          — host→voice from brand voice_preset (or defaults).
  3. tts.synth (per line) — Kokoro fp32 CPU floor; one short clip per turn.
  4. _choreograph         — the timing track: gaps, holds after a landing
                            beat, latched fast exchanges, and the over-blend
                            (a backchannel ducked under the speaker's tail).
  5. _mix / _normalize    — sum clips on the timeline, RMS-normalise.
  6. WAV → ~/.okuro/deliveries, recorded in body_path (HR-C4, >1 MB).

Never raises into the caller — failures land on ChannelOutput.error.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_VALID_INTENTS = {
    "assert", "affirm", "question", "tangent", "land", "backchannel",
    "challenge", "reveal",
}
_BACKCHANNELS = ("Right.", "Exactly.", "Mm-hm.", "Yeah.", "That's the thing.")


@dataclass
class DialogueLine:
    host: str            # "A" | "B"
    text: str
    intent: str = "assert"  # assert|affirm|question|tangent|land|backchannel


# ----------------------------------------------------------------------
# Public renderer
# ----------------------------------------------------------------------


def render(outline, theme) -> "ChannelOutput":  # noqa: F821
    from okuro.peer.delivery.channels import ChannelOutput

    start = time.monotonic()
    try:
        from okuro.peer.delivery import tts
    except Exception as exc:
        return ChannelOutput(
            error=f"tts module unavailable: {exc}",
            duration_ms=int((time.monotonic() - start) * 1000),
            provider="local", model="okuro-delivery-podcast",
        )

    try:
        import numpy as np
        import soundfile as sf
    except Exception as exc:
        return ChannelOutput(
            error=f"audio deps missing (numpy/soundfile): {exc}",
            duration_ms=int((time.monotonic() - start) * 1000),
            provider="local", model="okuro-delivery-podcast",
        )

    try:
        from okuro.peer.delivery import progress

        lines = _generate_dialogue(outline, theme)
        if not lines:
            raise RuntimeError("empty dialogue")
        progress.report("writing conversation", 0.28)

        voice_a, voice_b, speed = _voices_for(theme)
        engine = tts.active_engine()
        # A two-host show needs two voices. active_engine() answers "what is the
        # best voice on this box", which is not the same question — an engine
        # that renders one speaker for every role turns this channel into one
        # voice talking to itself, and does it silently. Fall back to the floor,
        # which always has distinct host voices. Correctness beats tier.
        if not tts.role_voices_distinct(engine):
            log.warning(
                "engine %s renders one voice for every role — podcast falls back "
                "to kokoro so the two hosts stay two people", engine,
            )
            engine = "kokoro"
        sr = tts.SAMPLE_RATE
        mix = None

        # MOSS (pro edition) is dialogue-native — render the WHOLE conversation
        # in one model load with [S1]/[S2] turn-taking, not clip-by-clip. Any
        # fault degrades to the per-line loop below (HR-C3).
        if engine == "moss":
            try:
                progress.report("render conversation", 0.35)
                samples, sr = tts.synth_dialogue(_moss_script(lines))
                if len(samples):
                    mix = _normalize(samples, np)
            except Exception as exc:
                log.warning("moss dialogue render failed (%s) — per-line fallback", exc)
                mix, engine = None, "kokoro"

        if mix is None:
            # Per-turn clips — the choreography needs each line as its own array
            # to over-blend, duck and hold, so this cannot be one long render.
            # synth_many hands the whole list to the engine and lets IT decide how
            # to batch: Qwen amortises its one model load across every line (it
            # used to pay that load per line), Kokoro/Orpheus just loop.
            progress.report("render conversation", 0.35)
            items = [
                {
                    "text": line.text,
                    "voice": voice_a if line.host == "A" else voice_b,
                    "role": "host_a" if line.host == "A" else "host_b",
                    "speed": min(1.5, max(0.7, speed * _speed_factor(line.intent))),
                }
                for line in lines
            ]
            rendered = tts.synth_many(items, engine=engine)
            clips: list[tuple[DialogueLine, "np.ndarray"]] = []
            for line, (samples, line_sr) in zip(lines, rendered):
                if len(samples):
                    clips.append((line, samples))
                    sr = line_sr
            progress.report("render conversation", 0.90)
            if not clips:
                raise RuntimeError("no audio produced")
            placed = _choreograph(clips, sr, np)
            mix = _normalize(_mix(placed, np), np)

        progress.report("conversion to mp3", 0.93)

        # okuro brand chord-drone bed, under the finished choreography rather than
        # inside it — the bed is branding, not part of the two-host timing track.
        # Before the write, so audio_seconds counts it.
        from okuro.peer.delivery import voice_drone
        mix = voice_drone.brand(mix, sr)

        out_path = _storage_root() / f"podcast-{uuid.uuid4().hex[:12]}.mp3"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # MP3 via libsndfile (>=1.1) — no ffmpeg/lameenc dependency. ~10x
        # smaller than WAV, and the format stakeholders expect.
        sf.write(str(out_path), mix, sr, format="MP3")
        try:  # index into the assets media bucket (never block a delivery)
            from okuro.assets import store as _assets
            _assets.register_delivery_file(out_path)
        except Exception:  # noqa: BLE001
            pass

        audio_seconds = round(len(mix) / sr, 1)
        duration_ms = int((time.monotonic() - start) * 1000)
        return ChannelOutput(
            body=_transcript(outline, lines),     # searchable transcript
            body_path=str(out_path),              # audio on disk (HR-C4)
            media_type="audio/mpeg",
            duration_ms=duration_ms,
            provider="local",
            model={"moss": "moss-ttsd-v1.0", "orpheus": "orpheus-3b-q8"}.get(
                engine, "kokoro-v1.0-fp32"),
            extras={
                "audio_seconds": audio_seconds,
                "turns": len(lines),
                "voices": {"A": voice_a, "B": voice_b},
                "wav_path": str(out_path),
                "wav_bytes": out_path.stat().st_size,
            },
        )
    except Exception as exc:
        log.warning("podcast render failed: %s", exc)
        return ChannelOutput(
            error=f"podcast render failed: {exc}",
            duration_ms=int((time.monotonic() - start) * 1000),
            provider="local", model="okuro-delivery-podcast",
        )


# ----------------------------------------------------------------------
# Stage 1 — dialogue generation
# ----------------------------------------------------------------------


_SYSTEM = (
    "You are a broadcast news scriptwriter. Write a two-anchor news segment: two "
    "professional newsreaders present the material plainly and factually, like a "
    "network news desk.\n"
    "REGISTER:\n"
    "- Neutral, measured, professional. State the facts clearly and let them stand.\n"
    "- NO hype, no opinions, no salesmanship, no personality or banter, no jokes, no "
    "manufactured disagreement, no 'thinking out loud'.\n"
    "- The two anchors share the read and hand off cleanly — one presents a point, "
    "the other takes the next. They do not argue or react emotionally.\n"
    "STRUCTURE:\n"
    "- Open with a one-line summary of what the segment covers. Present the key "
    "points in a logical order. Close with a brief, factual takeaway.\n"
    "- Alternate anchors at natural boundaries. Short, clear, complete sentences.\n"
    "DELIVERY:\n"
    "- Plain spoken prose. NO disfluencies, interjections or filler ('wait', 'okay "
    "but', 'hold on', 'I mean', 'right,', 'huh'). NO em-dash interruptions or "
    "trailing ellipses. Clean, professional phrasing.\n"
    "Preserve every fact; invent nothing.\n"
    'Output ONLY a JSON array of {"host":"A"|"B","text":"...","intent":"assert"}. '
    'Use intent "assert" for statements and "affirm" only for a brief neutral handoff '
    "acknowledgement; do not use challenge, reveal, tangent, or backchannel."
)

# A second editor pass (NotebookLM-style) that rewrites the draft to sound more
# natural spoken. Best-effort; skipped via OKURO_PODCAST_REVIEW=0 or if the
# bridge is unavailable.
_REVIEW_SYSTEM = (
    "You are a script editor for broadcast news read aloud. Given a JSON array of "
    "dialogue turns, return an IMPROVED JSON array (identical schema) that reads "
    "cleanly and professionally on air: tighten wordy phrasing and break any turn "
    "that reads like dense written prose into clear spoken sentences, while keeping "
    "the tone strictly neutral and factual. Do NOT add disfluencies, filler, "
    "opinions, humour, or manufactured tension, and do NOT change any intent away "
    "from 'assert'/'affirm'. Keep every fact intact, add no new claims. Output ONLY "
    "the JSON array."
)
_REVIEW_PASS = os.environ.get("OKURO_PODCAST_REVIEW", "1") != "0"


def _generate_dialogue(outline, theme) -> list[DialogueLine]:
    """One LLM hop → tagged script. Falls back to a deterministic script."""
    briefing = _outline_text(outline)
    prompt = (
        f"Briefing to present as a two-anchor news segment:\n\n{briefing}\n\n"
        "Write 12-20 turns. Open with a one-line summary, close with a brief factual "
        "takeaway. Return ONLY the JSON array."
    )
    try:
        from okuro.bridge.invoke import invoke
        result = invoke(prompt, capability="translate", system_prompt=_SYSTEM, timeout=120)
    except Exception as exc:
        log.info("podcast: bridge invoke failed (%s); using fallback script", exc)
        return _fallback_dialogue(outline)

    if not isinstance(result, dict) or not result.get("success"):
        log.info("podcast: bridge returned no output; using fallback script")
        return _fallback_dialogue(outline)

    raw = _extract_json(result.get("output") or "")
    if not raw:
        return _fallback_dialogue(outline)

    lines: list[DialogueLine] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        host = "B" if str(item.get("host", "A")).strip().upper() == "B" else "A"
        intent = str(item.get("intent", "assert")).strip().lower()
        if intent not in _VALID_INTENTS:
            intent = "assert"
        lines.append(DialogueLine(host=host, text=text, intent=intent))

    if not lines:
        return _fallback_dialogue(outline)
    if _REVIEW_PASS:
        lines = _review_dialogue(lines) or lines
    return lines


def _review_dialogue(lines: list[DialogueLine]) -> list[DialogueLine] | None:
    """Second editor hop: rewrite the draft to sound more natural. Best-effort."""
    draft = json.dumps(
        [{"host": l.host, "text": l.text, "intent": l.intent} for l in lines],
        ensure_ascii=False,
    )
    try:
        from okuro.bridge.invoke import invoke
        result = invoke(
            f"Improve this podcast script:\n\n{draft}",
            capability="translate", system_prompt=_REVIEW_SYSTEM, timeout=120,
        )
    except Exception as exc:
        log.info("podcast: review pass skipped (%s)", exc)
        return None
    if not isinstance(result, dict) or not result.get("success"):
        return None
    raw = _extract_json(result.get("output") or "")
    if not raw:
        return None
    reviewed: list[DialogueLine] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        host = "B" if str(item.get("host", "A")).strip().upper() == "B" else "A"
        intent = str(item.get("intent", "assert")).strip().lower()
        if intent not in _VALID_INTENTS:
            intent = "assert"
        reviewed.append(DialogueLine(host=host, text=text, intent=intent))
    return reviewed or None


def _fallback_dialogue(outline) -> list[DialogueLine]:
    """Deterministic 2-host script straight from the Outline — no LLM.

    Guarantees a podcast even offline: hosts alternate through the TL;DR and
    section bullets, with the partner dropping a backchannel after each
    section's landing point.
    """
    lines: list[DialogueLine] = []
    title = outline.title or "today's topic"
    lines.append(DialogueLine("A", f"Welcome in. Today we're getting into {title}.", "assert"))
    if outline.tldr:
        lines.append(DialogueLine("B", f"The short version: {outline.tldr}", "land"))
        lines.append(DialogueLine("A", "Right.", "backchannel"))

    host = "A"
    for section in outline.sections:
        lines.append(DialogueLine(host, f"Let's talk about {section.heading}.", "question"))
        host = "B" if host == "A" else "A"
        points = _section_points(section)
        for j, point in enumerate(points):
            intent = "land" if j == len(points) - 1 else "assert"
            lines.append(DialogueLine(host, point, intent))
            host = "B" if host == "A" else "A"
        if points:
            lines.append(DialogueLine(host, _BACKCHANNELS[len(lines) % len(_BACKCHANNELS)], "backchannel"))
    lines.append(DialogueLine(host, "That's a good place to leave it. Thanks for listening.", "assert"))
    return lines


def _section_points(section) -> list[str]:
    out: list[str] = []
    for block in section.blocks:
        if block.type == "bullets":
            out.extend(str(x).strip() for x in (block.data or []) if str(x).strip())
        elif block.content and block.content.strip():
            out.append(block.content.strip())
    return out[:4]


def _outline_text(outline) -> str:
    parts: list[str] = [outline.title or "Untitled"]
    if outline.subtitle:
        parts.append(outline.subtitle)
    if outline.tldr:
        parts.append(f"Summary: {outline.tldr}")
    for section in outline.sections:
        parts.append(f"\n## {section.heading}")
        for point in _section_points(section):
            parts.append(f"- {point}")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Stage 2 — voice assignment
# ----------------------------------------------------------------------


def _voices_for(theme) -> tuple[str, str, float]:
    """Resolve (host_a_voice, host_b_voice, speed), engine-aware.

    Defaults follow the ACTIVE tts engine: Orpheus host ids (leo/jess) when the
    expressive backend is live, else Kokoro warm blends (its one timbre lever).
    A brand voice_preset overrides with engine-native ids.
    """
    from okuro.peer.delivery import tts
    preset = {}
    try:
        preset = (theme.channel_specific or {}).get("voice_preset") or {}
    except Exception:
        preset = {}
    if tts.active_engine() == "orpheus":
        def_a, def_b = tts.host_voices("orpheus")
    else:
        def_a, def_b = "af_heart:70,af_bella:30", "am_michael:70,am_fenrir:30"
    voice_a = str(preset.get("host_a") or preset.get("voice") or def_a)
    voice_b = str(preset.get("host_b") or def_b)
    try:
        speed = float(preset.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    speed = min(1.5, max(0.7, speed))
    return voice_a, voice_b, speed


# ----------------------------------------------------------------------
# Stage 3 — choreography (the timing track / IP)
# ----------------------------------------------------------------------


def _db(gain_db: float) -> float:
    return float(10.0 ** (gain_db / 20.0))


# Per-intent speaking-rate factor: slow down to land a point, speed up for
# quick reactions. Kokoro has no prosody tags, so rate + punctuation are the
# in-model expressiveness levers (the rest is choreography).
_SPEED_FACTOR = {
    "reveal": 0.90,       # the payoff — slow, weighty
    "land": 0.93,         # emphasis — slower
    "question": 0.98,
    "assert": 1.0,
    "tangent": 1.03,
    "challenge": 1.04,    # pushback — a touch faster, assertive
    "affirm": 1.05,
    "backchannel": 1.08,  # quick reaction
}


def _speed_factor(intent: str) -> float:
    return _SPEED_FACTOR.get(intent, 1.0)


def _moss_script(lines: list[DialogueLine]) -> str:
    """Flatten dialogue turns into a MOSS ``[S1]/[S2]`` script (host A→S1, B→S2).

    MOSS renders the whole conversation in one pass, so intents/choreography are
    dropped here — turn-taking and pacing come from the model, not from clip
    placement.
    """
    tag = {"A": "[S1]", "B": "[S2]"}
    return "\n".join(
        f"{tag.get(ln.host, '[S1]')} {ln.text.strip()}"
        for ln in lines if (ln.text or "").strip()
    )


def _choreograph(clips, sr: int, np):
    """Place each clip on a global timeline. Returns [(start_sample, samples, gain)].

    Rules:
      - default     : 220 ms breath between turns
      - land        : 600 ms hold after a landing beat (emphasis)
      - latch       : short assert/affirm turns butt up with no gap
      - backchannel : ducked (-10 dB) and slid UNDER the previous speaker's
                      tail — the "over-blend" that reads as spontaneous but is
                      fully pre-computed. Does not advance the main timeline.
    """
    placed: list[tuple[int, "np.ndarray", float]] = []
    cursor = 0
    prev_main_start = 0
    prev_main_len = 0

    for line, samples in clips:
        n = len(samples)
        if line.intent == "backchannel" and prev_main_len > 0:
            overlap = min(int(0.8 * sr), int(0.45 * prev_main_len))
            start = max(0, prev_main_start + prev_main_len - overlap)
            placed.append((start, samples, _db(-10.0)))
            continue  # backchannel never pushes the main cursor

        if line.intent == "challenge" and prev_main_len > 0:
            # interrupt: cut in ~250 ms before the previous turn ends, at FULL
            # volume (a real turn, not a ducked backchannel). This is what
            # breaks the rigid A/B/A/B feel acoustically.
            overlap = min(int(0.25 * sr), int(0.30 * prev_main_len))
            start = max(0, prev_main_start + prev_main_len - overlap)
            placed.append((start, samples, 1.0))
            prev_main_start, prev_main_len = start, n
            cursor = start + n + int(0.18 * sr)
            continue

        # normal main turn — 'reveal' gets a suspense beat before it lands.
        lead = int(0.45 * sr) if line.intent == "reveal" else 0
        start = cursor + lead
        placed.append((start, samples, 1.0))
        prev_main_start, prev_main_len = start, n

        gap = 0.22
        if line.intent in ("land", "reveal"):
            gap = 0.60
        elif n < int(1.2 * sr) and line.intent in ("assert", "affirm"):
            gap = 0.0
        cursor = start + n + int(gap * sr)

    return placed


def _mix(placed, np):
    """Sum placed clips into one mono float32 buffer."""
    if not placed:
        return np.zeros(0, dtype="float32")
    total = max(start + len(s) for start, s, _g in placed)
    buf = np.zeros(total, dtype="float32")
    for start, s, gain in placed:
        end = start + len(s)
        buf[start:end] += s.astype("float32") * gain
    return buf


def _normalize(buf, np, target_rms: float = 0.12, peak_ceiling: float = 0.97):
    """RMS-normalise toward target, then guard the peak. No external dep."""
    if buf.size == 0:
        return buf
    rms = float(np.sqrt(np.mean(np.square(buf))))
    if rms > 1e-6:
        buf = buf * (target_rms / rms)
    peak = float(np.max(np.abs(buf))) if buf.size else 0.0
    if peak > peak_ceiling:
        buf = buf * (peak_ceiling / peak)
    return np.clip(buf, -1.0, 1.0).astype("float32")


# ----------------------------------------------------------------------
# Transcript + storage helpers
# ----------------------------------------------------------------------


def _transcript(outline, lines: list[DialogueLine]) -> str:
    parts = [f"# {outline.title or 'Podcast'}", ""]
    if outline.subtitle:
        parts += [f"_{outline.subtitle}_", ""]
    for line in lines:
        if line.intent == "backchannel":
            continue
        parts.append(f"**{line.host}:** {line.text}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _storage_root() -> Path:
    override = os.environ.get("OKURO_DELIVERY_STORAGE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".okuro" / "deliveries"


def _extract_json(text: str):
    """Pull the first JSON array out of an LLM response (fence-tolerant)."""
    if not text:
        return None
    lo = text.find("[")
    hi = text.rfind("]")
    if lo == -1 or hi == -1 or hi <= lo:
        return None
    try:
        data = json.loads(text[lo:hi + 1])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


# Register on import.
from okuro.peer.delivery.channels import register  # noqa: E402

register("podcast", render)
