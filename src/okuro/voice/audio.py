# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: AudioIn/AudioOut — thin sounddevice wrappers for capture and playback.
# index:
#   imports
#   class AudioIn
#   class AudioOut
#   def list_devices
# AGENT_HEADER_END -->
"""Audio capture and playback — sounddevice + NumPy.

The adapter boundary is deliberately thin. Anything higher up the stack
(VAD, STT, orchestrator) sees bytes/ndarrays and never imports
``sounddevice`` directly. This lets the tests inject a fake that yields
pre-recorded PCM without needing a real sound card in CI.

Audio formats
-------------
- Mic in  : ``int16`` PCM, mono, ``cfg.audio.input_sample_rate`` Hz (16 kHz default).
- Speaker : ``int16`` PCM, mono, ``cfg.audio.output_sample_rate`` Hz (24 kHz default).
  (ElevenLabs ``pcm_24000`` output, sent straight to the output stream.)

Both are 16-bit little-endian to match what both Deepgram and ElevenLabs
agree on without a resampling step — matches ADR 1.3 §7.
"""

from __future__ import annotations

import queue
import threading
from typing import Iterator, Optional

import numpy as np

from .config import AudioConfig
from .errors import MicPermissionError, NoDeviceError


def _require_sounddevice():
    """Import sounddevice lazily — not needed at module import time."""
    try:
        import sounddevice as sd  # type: ignore
        return sd
    except OSError as e:
        # "PortAudio library not found" — Linux missing libportaudio2.
        raise NoDeviceError(
            f"sounddevice failed to initialise PortAudio: {e}",
            hint="Linux: sudo apt install libportaudio2 libasound2  (or `dnf install portaudio` / `pacman -S portaudio`)",
        ) from e


def list_devices() -> list[dict]:
    """Return the system's audio devices — used by ``okuro voice devices``.

    Returns a list of ``{index, name, kind, default}`` dicts. Pure
    introspection, no streams opened.
    """
    sd = _require_sounddevice()
    devices = sd.query_devices()
    default_in, default_out = sd.default.device
    result: list[dict] = []
    for i, d in enumerate(devices):
        kind = []
        if d.get("max_input_channels", 0) > 0:
            kind.append("in")
        if d.get("max_output_channels", 0) > 0:
            kind.append("out")
        result.append(
            {
                "index": i,
                "name": d.get("name", "?"),
                "kind": "/".join(kind) or "none",
                "default": i in (default_in, default_out),
                "sample_rate": d.get("default_samplerate"),
            }
        )
    return result


class AudioIn:
    """Microphone input — yields ``int16`` PCM chunks.

    Usage
    -----
    >>> cfg = AudioConfig()
    >>> with AudioIn(cfg) as mic:
    ...     for chunk in mic.chunks():
    ...         # chunk is np.ndarray, shape=(block_size,), dtype=int16
    ...         break

    The class is context-managed so the InputStream is always closed on
    exit — important on mac where the TCC indicator stays orange otherwise.

    For unit tests, callers construct a ``AudioIn(cfg, stream_factory=fake)``
    passing a callable that returns a fake stream — the chunk queue is
    populated by calling ``_push_chunk()`` directly.
    """

    def __init__(
        self,
        cfg: AudioConfig,
        stream_factory=None,  # callable(cfg) -> stream ctx. For tests.
    ) -> None:
        self.cfg = cfg
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self._stream = None
        self._stream_factory = stream_factory
        self._closed = False
        self._stopped = threading.Event()

    def __enter__(self) -> "AudioIn":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # Callback path — sounddevice pumps audio on a realtime thread.
    # We only copy into a thread-safe queue; no heavy work here.
    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            # overflow / underrun — let the caller see it via the status log,
            # but don't fail the stream.
            pass
        # indata is float32 by default — convert once to int16.
        pcm = (indata[:, 0] * 32767.0).astype(np.int16)
        self._queue.put(pcm.copy())

    def open(self) -> None:
        if self._stream_factory is not None:
            self._stream = self._stream_factory(self.cfg)
            return
        sd = _require_sounddevice()
        try:
            self._stream = sd.InputStream(
                samplerate=self.cfg.input_sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.cfg.block_size,
                device=self.cfg.input_device,
                callback=self._callback,
            )
            self._stream.start()
        except sd.PortAudioError as e:  # type: ignore[attr-defined]
            msg = str(e).lower()
            if "permission" in msg or "not permitted" in msg or "unavailable" in msg:
                raise MicPermissionError(str(e)) from e
            raise NoDeviceError(f"Cannot open input stream: {e}") from e

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stopped.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:  # noqa: BLE001
            # Closing a stream that never opened cleanly — don't mask the real error.
            pass

    def _push_chunk(self, chunk: np.ndarray) -> None:
        """Test hook — push a pre-recorded chunk into the queue."""
        self._queue.put(chunk.astype(np.int16))

    def stop(self) -> None:
        """Signal chunks() to exit its loop after draining the queue."""
        self._stopped.set()

    def chunks(self, timeout: float = 0.1) -> Iterator[np.ndarray]:
        """Yield mic chunks until ``stop()`` or ``close()`` is called.

        ``timeout`` is the per-poll queue timeout — keeps the loop
        responsive to ``stop()`` without busy-waiting.
        """
        while not self._stopped.is_set() or not self._queue.empty():
            try:
                yield self._queue.get(timeout=timeout)
            except queue.Empty:
                if self._closed:
                    return
                continue


class AudioOut:
    """Speaker output — accepts ``int16`` PCM chunks and plays them.

    Writes are blocking (call returns only when the frame is queued to
    PortAudio's internal ring buffer). This gives the orchestrator natural
    back-pressure — TTS generation is paced by playback.

    For tests, pass ``stream_factory=lambda cfg: FakeOut()`` and assert on
    ``FakeOut.written``.
    """

    def __init__(
        self,
        cfg: AudioConfig,
        stream_factory=None,
    ) -> None:
        self.cfg = cfg
        self._stream = None
        self._stream_factory = stream_factory
        self._closed = False

    def __enter__(self) -> "AudioOut":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if self._stream_factory is not None:
            self._stream = self._stream_factory(self.cfg)
            return
        sd = _require_sounddevice()
        try:
            self._stream = sd.OutputStream(
                samplerate=self.cfg.output_sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.cfg.block_size,
                device=self.cfg.output_device,
            )
            self._stream.start()
        except sd.PortAudioError as e:  # type: ignore[attr-defined]
            raise NoDeviceError(f"Cannot open output stream: {e}") from e

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:  # noqa: BLE001
            pass

    def write(self, pcm: np.ndarray | bytes) -> None:
        """Play a PCM chunk. Accepts int16 ndarray or raw little-endian bytes."""
        if isinstance(pcm, (bytes, bytearray)):
            pcm = np.frombuffer(pcm, dtype=np.int16)
        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        # Mono — reshape to (N, 1) for sounddevice's column convention.
        if pcm.ndim == 1:
            pcm = pcm.reshape(-1, 1)
        if self._stream is None:
            raise NoDeviceError("AudioOut is not open.")
        self._stream.write(pcm)
