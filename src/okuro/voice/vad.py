# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Voice Activity Detection — silero-vad (ONNX) turn-end detector.
# index:
#   imports
#   class VAD
#   class SileroVAD
#   class SilenceVAD
#   class TurnDetector
# AGENT_HEADER_END -->
"""Voice activity detection and turn-end decisions.

Silero-vad gives us a 0..1 speech-probability for each ~30 ms frame.
``TurnDetector`` folds those probabilities into three discrete events:

- ``speech_start`` — first frame over threshold after a silence run.
- ``speech_continue`` — a speech frame mid-utterance.
- ``turn_end`` — ``min_silence_ms`` of sub-threshold frames after at least
  ``min_speech_ms`` of speech has been observed.

Two policies:

- **Auto mode (``vad`` trigger)** — the orchestrator lets ``TurnDetector``
  decide when the user started AND ended talking.
- **CLI / PTT mode** — the orchestrator controls start/end from the
  keyboard; the VAD is still consulted for the *end* event as a safety
  net so the user doesn't have to hold Enter forever.

The ONNX model is loaded on first ``SileroVAD`` construction and cached
at a module-level, so repeat calls in a continuous session don't re-parse
the ~2 MB weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

import numpy as np

from .config import VADConfig
from .errors import VADError
from okuro.db.engine import okuro_home


SILERO_FRAME_MS = 32  # silero expects 512 samples @ 16 kHz = 32 ms.
SILERO_SAMPLES_16K = 512


class VAD(Protocol):
    """Minimal interface every VAD implementation must satisfy."""

    sample_rate: int

    def score(self, pcm_frame: np.ndarray) -> float:
        """Return speech probability for a single frame, 0..1."""
        ...

    def reset(self) -> None:
        """Clear internal LSTM state between sessions."""
        ...


class SilenceVAD:
    """Zero-crossings / RMS fallback VAD.

    Used when silero-vad isn't installed (e.g. minimal install) or in
    tests that don't want onnxruntime on the import path. It's crude
    but good enough that the rest of the pipeline behaves — the
    orchestrator's PTT mode doesn't need a great VAD.
    """

    sample_rate = 16000

    def __init__(self, rms_threshold: float = 300.0) -> None:
        self.rms_threshold = rms_threshold

    def score(self, pcm_frame: np.ndarray) -> float:
        if pcm_frame.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(pcm_frame.astype(np.float32) ** 2)))
        # Normal speech sits rms 200-500; divisor must put that range over
        # 0.5 so the default threshold fires. Using rms_threshold (300) maps
        # rms=150→0.5, rms>=300→1.0 — fallback path now ends turns reliably.
        return min(1.0, rms / self.rms_threshold)

    def reset(self) -> None:  # noqa: D401
        """No-op — this VAD is stateless."""
        return


_SILERO_MODEL = None


def _load_silero(model_path: Optional[Path] = None):
    """Import + load the silero-vad ONNX model (cached)."""
    global _SILERO_MODEL
    if _SILERO_MODEL is not None:
        return _SILERO_MODEL
    try:
        import onnxruntime as ort  # type: ignore
    except ImportError as e:
        raise VADError(
            "onnxruntime not installed.",
            hint="pip install onnxruntime  (bundled with okuro[voice] extra)",
        ) from e

    # Resolution order:
    # 1. Explicit path passed in.
    # 2. ~/.okuro/models/silero_vad.onnx (bundled by onboarding).
    # 3. Package-adjacent models/silero_vad.onnx (dev install).
    candidates: list[Path] = []
    if model_path is not None:
        candidates.append(Path(model_path))
    candidates.append(okuro_home() / "models" / "silero_vad.onnx")
    candidates.append(Path(__file__).parent / "models" / "silero_vad.onnx")

    for c in candidates:
        if c.exists():
            _SILERO_MODEL = ort.InferenceSession(
                str(c), providers=["CPUExecutionProvider"]
            )
            return _SILERO_MODEL

    raise VADError(
        "silero_vad.onnx not found.",
        hint="Run: okuro voice doctor  — it will download the model (~2 MB).",
    )


class SileroVAD:
    """Silero-vad ONNX wrapper — 16 kHz mono, 512-sample frames.

    Keeps the LSTM hidden state between frames inside a single turn. Call
    ``reset()`` at the top of each new turn.
    """

    sample_rate = 16000

    def __init__(self, model_path: Optional[Path] = None) -> None:
        self._session = _load_silero(model_path)
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._sr = np.array(self.sample_rate, dtype=np.int64)

    def reset(self) -> None:
        self._h[:] = 0.0
        self._c[:] = 0.0

    def score(self, pcm_frame: np.ndarray) -> float:
        """Score a 512-sample int16 frame. Returns speech probability 0..1."""
        if pcm_frame.size != SILERO_SAMPLES_16K:
            raise VADError(
                f"SileroVAD expects frames of {SILERO_SAMPLES_16K} samples, "
                f"got {pcm_frame.size}."
            )
        audio = pcm_frame.astype(np.float32) / 32768.0
        audio = audio.reshape(1, -1)
        out = self._session.run(
            None,
            {
                "input": audio,
                "h": self._h,
                "c": self._c,
                "sr": self._sr,
            },
        )
        prob, self._h, self._c = out
        return float(prob.squeeze())


@dataclass
class TurnEvent:
    kind: str  # "speech_start" | "speech_continue" | "silence" | "turn_end"
    score: float


class TurnDetector:
    """Folds frame-level VAD scores into turn-level events.

    State machine — three states, one event per frame:

        SILENCE --speech--> SPEAKING
        SPEAKING --speech--> SPEAKING
        SPEAKING --silence--> TRAILING
        TRAILING --silence (>= min_silence_ms)--> DONE (emits turn_end)
        TRAILING --speech--> SPEAKING (silence run reset)

    Frame size is fixed by the VAD implementation (512 samples @ 16 kHz =
    32 ms for silero). Timing thresholds are converted to frame counts on
    construction.
    """

    def __init__(
        self,
        vad: VAD,
        cfg: VADConfig,
    ) -> None:
        self.vad = vad
        self.cfg = cfg
        frame_ms = (SILERO_FRAME_MS if vad.sample_rate == 16000 else
                    int(1000 * SILERO_SAMPLES_16K / vad.sample_rate))
        self._frame_ms = frame_ms
        self._min_silence_frames = max(1, cfg.min_silence_ms // frame_ms)
        self._min_speech_frames = max(1, cfg.min_speech_ms // frame_ms)
        self._state = "SILENCE"
        self._speech_frames = 0
        self._silence_run = 0

    def reset(self) -> None:
        self.vad.reset()
        self._state = "SILENCE"
        self._speech_frames = 0
        self._silence_run = 0

    def feed(self, pcm_frame: np.ndarray) -> TurnEvent:
        score = self.vad.score(pcm_frame)
        is_speech = score >= self.cfg.threshold

        if self._state == "SILENCE":
            if is_speech:
                self._state = "SPEAKING"
                self._speech_frames = 1
                self._silence_run = 0
                return TurnEvent("speech_start", score)
            return TurnEvent("silence", score)

        if self._state == "SPEAKING":
            if is_speech:
                self._speech_frames += 1
                self._silence_run = 0
                return TurnEvent("speech_continue", score)
            # first silence after speech
            self._silence_run = 1
            self._state = "TRAILING"
            return TurnEvent("silence", score)

        # TRAILING
        if is_speech:
            self._state = "SPEAKING"
            self._speech_frames += 1
            self._silence_run = 0
            return TurnEvent("speech_continue", score)
        self._silence_run += 1
        if (
            self._silence_run >= self._min_silence_frames
            and self._speech_frames >= self._min_speech_frames
        ):
            ev = TurnEvent("turn_end", score)
            # reset for the next turn — caller expected to restart.
            self.reset()
            return ev
        return TurnEvent("silence", score)
