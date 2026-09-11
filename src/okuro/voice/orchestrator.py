# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: TurnOrchestrator — stitches mic → VAD → STT → bridge → TTS → speaker.
# index:
#   imports
#   def _load_voice_system_prompt
#   class TurnOrchestrator
# AGENT_HEADER_END -->
"""Turn orchestrator — one voice turn, start to finish.

A single ``TurnOrchestrator`` instance owns the shared state that a
continuous session wants to keep warm: audio devices, VAD model,
keyring-resolved API keys. Each call to ``run_turn()`` executes one
pipeline pass and returns its result.

Trigger modes
-------------
``cli``    — prints "listening…" and waits; VAD ends the turn.
``vad``    — same as cli but speech onset also starts the turn (no prompt).
``ptt``    — push-to-talk. Holds while a key is down (implemented via a
             callback — the CLI wires it to ``click.getchar`` or the
             ``keyboard`` library). The ABC here lets tests inject an
             iterator of booleans so there's no real keyboard dependency
             in unit tests.
``hotkey`` — v2. Not implemented in the orchestrator; the daemon loop will.

Separation of concerns
----------------------
This module does NOT import ``sounddevice``, ``onnxruntime``,
``websockets``, ``httpx``, or the Deepgram/ElevenLabs SDKs directly.
Everything is reached through the adapters (``audio.AudioIn``,
``audio.AudioOut``, ``vad.TurnDetector``, ``stt.STTClient``,
``tts.TTSClient``). That is what makes it unit-testable with fakes.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .config import VoiceConfig
from .errors import BridgeError, VoiceError
from .vad import (
    SILERO_SAMPLES_16K,
    SileroVAD,
    SilenceVAD,
    TurnDetector,
)


def _load_voice_system_prompt() -> str:
    """Read ``prompts/voice_system.txt`` adjacent to this module."""
    here = Path(__file__).parent / "prompts" / "voice_system.txt"
    if here.exists():
        return here.read_text()
    return "You are okuro speaking aloud. Keep replies under 40 spoken words."


def _chunk_to_frames(chunk: np.ndarray, frame_size: int = SILERO_SAMPLES_16K):
    """Split a mic block into VAD-sized frames, dropping ragged tails."""
    n = (chunk.size // frame_size) * frame_size
    if n == 0:
        return
    for start in range(0, n, frame_size):
        yield chunk[start : start + frame_size]


class TurnOrchestrator:
    """One-turn-at-a-time voice loop.

    Dependencies are all injectable for tests. Production callers pass
    none — defaults are built on first ``run_turn()`` from config +
    keyring.
    """

    def __init__(
        self,
        config: VoiceConfig,
        *,
        stt_client=None,
        tts_client=None,
        bridge_invoke: Optional[Callable] = None,
        audio_in=None,
        audio_out=None,
        vad=None,
        ptt_source: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.cfg = config
        self._stt = stt_client
        self._tts = tts_client
        self._bridge_invoke = bridge_invoke
        self._audio_in = audio_in
        self._audio_out = audio_out
        self._vad = vad
        self._ptt_source = ptt_source
        self._turn_index: int = 0
        self._system_prompt = _load_voice_system_prompt()

    # ---------- dependency resolution ----------
    def _resolve_audio_in(self):
        if self._audio_in is not None:
            return self._audio_in
        from .audio import AudioIn
        return AudioIn(self.cfg.audio)

    def _resolve_audio_out(self):
        if self._audio_out is not None:
            return self._audio_out
        from .audio import AudioOut
        return AudioOut(self.cfg.audio)

    def _resolve_stt(self):
        if self._stt is not None:
            return self._stt
        from .stt import build_stt_client
        return build_stt_client(self.cfg)

    def _resolve_tts(self):
        if self._tts is not None:
            return self._tts
        from .tts import build_tts_client
        return build_tts_client(self.cfg)

    def _resolve_vad(self) -> TurnDetector:
        if self._vad is not None:
            return self._vad
        if self.cfg.vad.engine == "silero":
            try:
                v = SileroVAD()
            except VoiceError:
                # Silero not available — graceful degrade to RMS.
                v = SilenceVAD()
        else:
            v = SilenceVAD()
        return TurnDetector(v, self.cfg.vad)

    def _resolve_bridge(self) -> Callable:
        if self._bridge_invoke is not None:
            return self._bridge_invoke
        from okuro.bridge.invoke import invoke  # type: ignore
        return invoke

    # ---------- main loop ----------
    def run_turn(self) -> dict:
        """Execute one listen → transcribe → think → speak cycle.

        Returns
        -------
        dict
            ``{"transcript": str, "reply": str, "duration": float,
               "turn_index": int, "provider": str|None}``
        """
        started = time.monotonic()
        self._turn_index += 1
        detector = self._resolve_vad()
        audio_in = self._resolve_audio_in()
        audio_out = self._resolve_audio_out()
        stt = self._resolve_stt()
        tts = self._resolve_tts()

        transcript = ""
        try:
            with audio_in as mic, audio_out as spk:
                stt.open()
                transcript = self._listen(mic, detector, stt)

            if not transcript:
                return self._result(transcript, "", started, provider=None)

            reply, provider = self._think(transcript)
            if not reply:
                return self._result(transcript, "", started, provider=provider)

            # Re-open the output stream for playback. Using the same
            # `audio_out` instance is deliberate — tests expect a single
            # open/close pair.
            with self._resolve_audio_out() as spk2:
                for chunk in tts.stream(reply):
                    spk2.write(chunk)

            return self._result(transcript, reply, started, provider=provider)
        finally:
            try:
                stt.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                tts.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- per-stage helpers ----------
    def _listen(self, mic, detector: TurnDetector, stt) -> str:
        """Drive mic → VAD → STT until a turn-end is detected."""
        mode = self.cfg.trigger.mode
        saw_speech = False
        mic_frames = 0

        # PTT mode: gate audio by the push-to-talk source; VAD is skipped
        # for start detection but still honoured for end safety-net.
        ptt_gate = self._ptt_source if mode == "ptt" else None

        for chunk in mic.chunks():
            if chunk.size == 0:
                continue
            # PTT: if key not held, stop pushing audio to STT. If we
            # already saw speech, that absence means end-of-turn.
            if ptt_gate is not None and not ptt_gate():
                if saw_speech:
                    break
                continue

            mic_frames += 1
            stt.send_audio(chunk.astype(np.int16).tobytes())

            for frame in _chunk_to_frames(chunk):
                ev = detector.feed(frame)
                if ev.kind == "speech_start":
                    saw_speech = True
                elif ev.kind == "turn_end":
                    return stt.finalize()

            # Safety cap — don't listen forever if silero misfires.
            # 60 s @ 16 kHz / block_size ≈ a lot of frames; we use a
            # simple wall-clock-ish heuristic from mic_frames.
            if mic_frames * self.cfg.audio.block_size > 60 * self.cfg.audio.input_sample_rate:
                break

        return stt.finalize()

    def _think(self, transcript: str) -> tuple[str, Optional[str]]:
        """Route the transcript through ``okuro.bridge.invoke``."""
        bridge = self._resolve_bridge()
        try:
            result = bridge(
                transcript,
                capability=self.cfg.llm.capability,
                system_prompt=self._system_prompt,
            )
        except Exception as e:  # noqa: BLE001
            raise BridgeError(f"bridge.invoke crashed: {e}") from e

        if not result.get("success", False):
            err = result.get("error") or "bridge returned success=false"
            raise BridgeError(err)

        reply = (result.get("output") or "").strip()
        provider = result.get("provider")
        # Trim to the configured context window — paranoid, TTS will
        # otherwise cost per character.
        if len(reply) > self.cfg.llm.context_window_chars:
            reply = reply[: self.cfg.llm.context_window_chars]
        return reply, provider

    def _result(
        self,
        transcript: str,
        reply: str,
        started: float,
        provider: Optional[str],
    ) -> dict:
        return {
            "transcript": transcript,
            "reply": reply,
            "duration": round(time.monotonic() - started, 3),
            "turn_index": self._turn_index,
            "provider": provider,
        }
