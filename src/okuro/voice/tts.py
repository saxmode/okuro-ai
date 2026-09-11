# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Text-to-speech — provider-agnostic ABC plus ElevenLabsTTS adapter.
# index:
#   imports
#   class TTSClient
#   class FakeTTS
#   class ElevenLabsTTS
#   def build_tts_client
# AGENT_HEADER_END -->
"""Text-to-speech adapters.

Same contract as STT — an ABC plus a ``FakeTTS`` for tests and
``ElevenLabsTTS`` for production.

Streaming contract
------------------
``stream(text) -> Iterator[bytes]`` yields PCM frames as they arrive so
the orchestrator can start playback before the full reply has been
synthesised. ElevenLabs Flash v2.5 at TTFB ~75 ms means first audio out
of the speaker is well under the 1 s end-to-end budget in the ADR.

Frames are ``pcm_24000`` (16-bit little-endian @ 24 kHz mono) by default —
matches the ``AudioOut`` default sample rate. If the user picks a
different output format in config we still hand raw PCM to the audio
layer; no re-encoding here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, Optional

from .config import TTSConfig, VoiceConfig
from .errors import MissingKeyError, TTSError


class TTSClient(ABC):
    """Abstract text-to-speech adapter."""

    @abstractmethod
    def stream(self, text: str) -> Iterator[bytes]:
        """Yield PCM frames for ``text``. Order matters — first chunk first."""
        ...

    def close(self) -> None:  # noqa: D401
        """Release any pooled HTTP connections. Safe default: no-op."""
        return


class FakeTTS(TTSClient):
    """In-memory TTS for tests.

    Construct with ``FakeTTS(frames=[b"\\x00\\x00" * 480])`` to yield
    a specific PCM stream; default yields a single silent 20 ms frame
    per spoken character so the orchestrator sees non-empty output.
    """

    def __init__(self, frames: Optional[list[bytes]] = None) -> None:
        self.frames = frames
        self.last_text: Optional[str] = None
        self.calls: int = 0

    def stream(self, text: str) -> Iterator[bytes]:
        self.last_text = text
        self.calls += 1
        if self.frames is not None:
            yield from self.frames
            return
        # One 20 ms silent chunk per spoken character — deterministic,
        # audible only if actually piped to speakers.
        frame = b"\x00\x00" * 480
        for _ in text:
            yield frame


class ElevenLabsTTS(TTSClient):
    """ElevenLabs Flash v2.5 streaming TTS.

    Uses the ``elevenlabs`` SDK when available and falls back to a raw
    ``httpx`` stream against ``POST /v1/text-to-speech/{voice_id}/stream``.
    Both paths return a bytes iterator.
    """

    _STREAM_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

    def __init__(
        self,
        cfg: TTSConfig,
        api_key: str,
        http_factory=None,  # for tests — callable(url, headers, json) -> iter[bytes]
    ) -> None:
        if not api_key:
            raise MissingKeyError("elevenlabs_api_key")
        self.cfg = cfg
        self.api_key = api_key
        self._http_factory = http_factory

    def _format_query(self) -> str:
        # "pcm_24000" in our config → "pcm_24000" on the API.
        return self.cfg.output_format

    def stream(self, text: str) -> Iterator[bytes]:
        if not text.strip():
            return
        if self._http_factory is not None:
            yield from self._http_factory(
                self._STREAM_URL.format(voice_id=self.cfg.voice_id),
                {"xi-api-key": self.api_key},
                {
                    "text": text,
                    "model_id": self.cfg.model,
                    "output_format": self._format_query(),
                },
            )
            return

        try:
            import httpx  # type: ignore
        except ImportError as e:
            raise TTSError(
                "httpx not installed.",
                hint="pip install httpx  (bundled with okuro[voice])",
            ) from e

        url = self._STREAM_URL.format(voice_id=self.cfg.voice_id)
        params = {"output_format": self._format_query()}
        headers = {
            "xi-api-key": self.api_key,
            "Accept": "audio/pcm",
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": self.cfg.model,
        }
        try:
            with httpx.stream(
                "POST", url, params=params, headers=headers, json=body,
                timeout=30.0,
            ) as resp:
                if resp.status_code >= 400:
                    detail = resp.read().decode("utf-8", errors="ignore")[:400]
                    raise TTSError(
                        f"ElevenLabs HTTP {resp.status_code}: {detail}"
                    )
                for chunk in resp.iter_bytes():
                    if chunk:
                        yield chunk
        except TTSError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"ElevenLabs stream failed: {e}") from e


def build_tts_client(
    cfg: VoiceConfig,
    api_key_getter=None,
) -> TTSClient:
    """Factory — map ``cfg.tts.provider`` to a concrete adapter."""
    provider = cfg.tts.provider
    getter = api_key_getter or _default_api_key_getter

    if provider == "elevenlabs":
        key = getter("elevenlabs_api_key")
        if not key:
            raise MissingKeyError("elevenlabs_api_key")
        return ElevenLabsTTS(cfg=cfg.tts, api_key=key)
    if provider == "fake":
        return FakeTTS()
    raise TTSError(f"Unknown TTS provider: {provider!r}")


def _default_api_key_getter(name: str):
    from okuro.keyring.storage import KeyringStorage

    return KeyringStorage().get_key(name)
