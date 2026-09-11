# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Speech-to-text — provider-agnostic ABC plus DeepgramSTT adapter.
# index:
#   imports
#   class TranscriptEvent
#   class STTClient
#   class FakeSTT
#   class DeepgramSTT
#   def build_stt_client
# AGENT_HEADER_END -->
"""Speech-to-text adapters.

The rest of the voice pipeline talks to ``STTClient`` — an ABC. Two
implementations ship:

- ``DeepgramSTT`` — production, Nova-3 streaming over WebSocket.
- ``FakeSTT`` — injected by tests; returns pre-baked transcripts without
  any network traffic.

Both expose the same contract:

    stt.open()
    stt.send_audio(pcm_bytes)
    stt.finalize() -> str      # blocks until final transcript is ready
    stt.close()

The ABC also publishes a ``transcript_events()`` generator for callers
that want partial results (useful later for barge-in / live captions).

Deepgram auth + URL
-------------------
The Deepgram adapter reads ``deepgram_api_key`` from the okuro keyring
**once at construction time**, never from env vars or config files.
Missing key -> ``MissingKeyError``.
"""

from __future__ import annotations

import json
import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Optional

from .config import STTConfig, VoiceConfig
from .errors import MissingKeyError, STTError


@dataclass
class TranscriptEvent:
    """One transcript update from the STT stream."""

    text: str
    is_final: bool
    confidence: float = 0.0


class STTClient(ABC):
    """Abstract speech-to-text adapter."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def send_audio(self, pcm: bytes) -> None: ...

    @abstractmethod
    def finalize(self, timeout: float = 5.0) -> str:
        """Signal end-of-utterance, block for the final transcript."""
        ...

    @abstractmethod
    def close(self) -> None: ...

    def transcript_events(self) -> Iterator[TranscriptEvent]:
        """Yield interim + final events as the stream produces them.

        Default implementation returns no events — concrete classes
        override. The orchestrator doesn't rely on this in v1 (it only
        uses ``finalize()``) but it keeps the API future-proof for
        live captions.
        """
        return iter(())


class FakeSTT(STTClient):
    """In-memory STT for tests.

    Construct with ``FakeSTT(final="hello there")`` to have ``finalize()``
    return that string. Tracks ``audio_bytes_received`` so tests can
    assert the pipeline actually fed audio through.
    """

    def __init__(
        self,
        final: str = "",
        interim: Optional[list[str]] = None,
    ) -> None:
        self._final = final
        self._interim = interim or []
        self.audio_bytes_received: int = 0
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def send_audio(self, pcm: bytes) -> None:
        self.audio_bytes_received += len(pcm)

    def finalize(self, timeout: float = 5.0) -> str:  # noqa: ARG002
        return self._final

    def close(self) -> None:
        self.closed = True

    def transcript_events(self) -> Iterator[TranscriptEvent]:
        for t in self._interim:
            yield TranscriptEvent(text=t, is_final=False, confidence=0.9)
        yield TranscriptEvent(text=self._final, is_final=True, confidence=1.0)


class DeepgramSTT(STTClient):
    """Deepgram Nova-3 streaming STT over WebSocket.

    Uses the official ``deepgram-sdk`` package when available and falls back
    to a raw ``websockets`` client if the SDK isn't importable. Production
    path is always the SDK — the fallback exists to keep ``pip install
    okuro[voice-lite]`` an option.
    """

    WS_URL_TEMPLATE = (
        "wss://api.deepgram.com/v1/listen"
        "?encoding=linear16&sample_rate={sample_rate}&channels=1"
        "&model={model}&language={language}&endpointing={endpointing_ms}"
        "&interim_results=true&smart_format=true"
    )

    def __init__(
        self,
        cfg: STTConfig,
        api_key: str,
        sample_rate: int = 16000,
        ws_factory=None,  # for tests — callable(url, headers) -> fake ws
    ) -> None:
        if not api_key:
            raise MissingKeyError("deepgram_api_key")
        self.cfg = cfg
        self.api_key = api_key
        self.sample_rate = sample_rate
        self._ws_factory = ws_factory
        self._ws = None
        self._events: "queue.Queue[TranscriptEvent]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._final_text: list[str] = []
        self._finalized = threading.Event()
        self._closed = False

    # ------- lifecycle -------
    def _build_url(self) -> str:
        return self.WS_URL_TEMPLATE.format(
            sample_rate=self.sample_rate,
            model=self.cfg.model,
            language=self.cfg.language,
            endpointing_ms=self.cfg.endpointing_ms,
        )

    def open(self) -> None:
        headers = [("Authorization", f"Token {self.api_key}")]
        if self._ws_factory is not None:
            self._ws = self._ws_factory(self._build_url(), headers)
        else:
            try:
                # Prefer the sync WS in `websockets` 12+ (simple connect()).
                from websockets.sync.client import connect  # type: ignore
            except ImportError as e:
                raise STTError(
                    "websockets client not installed.",
                    hint="pip install websockets  (bundled with okuro[voice])",
                ) from e
            try:
                self._ws = connect(
                    self._build_url(),
                    additional_headers=dict(headers),
                    max_size=None,
                )
            except Exception as e:  # noqa: BLE001
                raise STTError(f"Deepgram WS connect failed: {e}") from e

        # Start a background reader — Deepgram sends interim results
        # asynchronously; we buffer them into a queue + accumulate finals.
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="deepgram-reader"
        )
        self._reader_thread.start()

    def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg_type = msg.get("type")
                if msg_type == "Results":
                    alt = (
                        msg.get("channel", {})
                        .get("alternatives", [{}])[0]
                    )
                    text = alt.get("transcript", "").strip()
                    confidence = float(alt.get("confidence", 0.0))
                    is_final = bool(msg.get("is_final", False))
                    speech_final = bool(msg.get("speech_final", False))
                    if text:
                        self._events.put(
                            TranscriptEvent(text, is_final, confidence)
                        )
                    if is_final and text:
                        self._final_text.append(text)
                    if speech_final:
                        self._finalized.set()
                elif msg_type == "UtteranceEnd":
                    self._finalized.set()
                elif msg_type in ("Error", "Warning"):
                    # Surface via events; orchestrator decides what to do.
                    self._events.put(
                        TranscriptEvent(
                            text=f"[deepgram:{msg_type}] {msg}",
                            is_final=True,
                            confidence=0.0,
                        )
                    )
        except Exception:  # noqa: BLE001
            # Connection dropped — finalize() still returns whatever
            # we accumulated.
            self._finalized.set()

    # ------- streaming -------
    def send_audio(self, pcm: bytes) -> None:
        if self._ws is None:
            raise STTError("DeepgramSTT.send_audio called before open().")
        try:
            self._ws.send(pcm)
        except Exception as e:  # noqa: BLE001
            raise STTError(f"Deepgram send failed: {e}") from e

    def finalize(self, timeout: float = 5.0) -> str:
        # Per Deepgram docs — sending `{type: "CloseStream"}` flushes the
        # final transcript, then the server closes the connection.
        try:
            if self._ws is not None:
                self._ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:  # noqa: BLE001
            pass
        self._finalized.wait(timeout=timeout)
        return " ".join(self._final_text).strip()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)

    def transcript_events(self) -> Iterator[TranscriptEvent]:
        while True:
            try:
                yield self._events.get(timeout=0.1)
            except queue.Empty:
                if self._finalized.is_set() and self._events.empty():
                    return


def build_stt_client(
    cfg: VoiceConfig,
    api_key_getter=None,
) -> STTClient:
    """Factory — map ``cfg.stt.provider`` to a concrete adapter.

    ``api_key_getter`` is a callable ``(name) -> str | None`` — defaults to
    ``okuro.keyring.storage.KeyringStorage().get_key`` in production and
    can be swapped in tests.
    """
    provider = cfg.stt.provider
    getter = api_key_getter or _default_api_key_getter

    if provider == "deepgram":
        key = getter("deepgram_api_key")
        if not key:
            raise MissingKeyError("deepgram_api_key")
        return DeepgramSTT(
            cfg=cfg.stt,
            api_key=key,
            sample_rate=cfg.audio.input_sample_rate,
        )
    if provider == "fake":
        return FakeSTT(final="")
    raise STTError(f"Unknown STT provider: {provider!r}")


def _default_api_key_getter(name: str) -> Optional[str]:
    from okuro.keyring.storage import KeyringStorage  # lazy import

    return KeyringStorage().get_key(name)
