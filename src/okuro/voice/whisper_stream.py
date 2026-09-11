# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Real-time streaming speech-to-text for okuro-notes voice capture.
#   LocalAgreement-2 over faster-whisper (CTranslate2 int8) on CPU — no GPU, no
#   cloud, stays on LAN (SYS-LAN). Emits a committed prefix as words stabilise
#   plus a live interim tail, so notes get dictated *as the user speaks* instead
#   of one blob after stop. Consumer-laptop friendly: language-aware int8 model
#   (base for en/auto, small for non-English where tiny/base collapse), rolling
#   buffer trim, real-time auto-downgrade with a per-language floor so German
#   never drops to tiny. No GPU, LAN-local.
# index: class HypothesisBuffer | class StreamingTranscriber | def is_available
# AGENT_HEADER_END -->
"""CPU streaming Whisper via LocalAgreement-2.

The engine keeps a rolling audio buffer and re-transcribes it every ~0.5s of
new audio. A word is *committed* only once two consecutive hypotheses agree on
it (LocalAgreement-n, n=2 — the ÚFAL whisper_streaming policy). Committed audio
is trimmed off the front so decode cost stays bounded and near real-time on a
laptop CPU.

Protocol (per WebSocket connection):
  * client streams 16 kHz mono PCM as Int16LE binary frames
  * `feed(pcm)` accumulates, `process()` returns (committed_delta, interim_tail)
  * on stop, `finish()` flushes the remaining tail as committed

Model is language-aware (Whisper's small sizes are English-biased and collapse
on other languages): en/auto default to `base`, non-English (de/fr/it/…) to
`small`. OKURO_STT_STREAM_MODEL overrides both. A session that can't keep pace
auto-downgrades toward a per-language floor — en/auto to `tiny`, non-English no
lower than `base`. Pin the language via the WS `?lang=` query for best results.
Latency ≈ 0.5–1.5s behind live.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# tiny→large, ascending accuracy / descending speed. Used for downgrade steps.
_MODEL_ORDER = ["tiny", "base", "small", "medium", "large-v3"]

# Language-aware defaults (when OKURO_STT_STREAM_MODEL is unset). English-biased
# small models fall off a cliff on other languages, so pin a bigger floor for
# non-English: `small` is the practical CPU floor for good German, and a slow
# `de` session must never drop below `base` (tiny German is unusable). Auto/en
# defaults to `base` — real-time on a decent CPU, auto-downgrades to tiny on weak
# ones. Add languages here as needed.
_NON_ENGLISH_LIKE = {"de", "fr", "it", "es", "nl", "pt", "pl", "ru", "de-ch"}

_models: dict[str, object] = {}  # size -> cached model (per-size, avoids reload churn)
_lock = threading.Lock()


def _default_model_for(language: str | None) -> str:
    """Pick a default size by language. Env override wins over this."""
    env = os.environ.get("OKURO_STT_STREAM_MODEL")
    if env:
        return env
    if language and language.lower() in _NON_ENGLISH_LIKE:
        return "small"
    return "base"


def _floor_for(language: str | None) -> str:
    """Smallest model the real-time auto-downgrade may fall to for a language.
    Non-English never drops to tiny — accuracy would collapse; accept a little
    lag instead."""
    if language and language.lower() in _NON_ENGLISH_LIKE:
        return "base"
    return "tiny"


def is_available() -> bool:
    """True when faster-whisper is importable (CPU streaming possible)."""
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def _get_model(size: str, device: str = "cpu", compute_type: str = "int8"):
    """Lazy-load + process-wide cache of a CTranslate2 Whisper model.

    Cache key is (size, device, compute_type) so the same size on CPU vs GPU are
    distinct entries and never clobber each other. faster-whisper wants a bare
    device family ("cuda"/"cpu") plus device_index, so a "cuda:N" string is split
    into device="cuda", device_index=N.
    """
    key = f"{size}@{device}/{compute_type}"
    m = _models.get(key)
    if m is None:
        with _lock:
            m = _models.get(key)
            if m is None:
                from faster_whisper import WhisperModel

                dev, index = _split_device(device)
                logger.info(
                    "loading faster-whisper '%s' (%s/%s, streaming)", size, device, compute_type
                )
                kwargs: dict = {"device": dev, "compute_type": compute_type}
                if index is not None:
                    kwargs["device_index"] = index
                m = WhisperModel(size, **kwargs)
                _models[key] = m
    return m


def _split_device(device: str) -> tuple[str, int | None]:
    """'cuda:1' -> ('cuda', 1); 'cpu'/'mps' -> (device, None)."""
    if device.startswith("cuda:"):
        return "cuda", int(device.split(":", 1)[1])
    return device, None


class HypothesisBuffer:
    """LocalAgreement-2 prefix commit.

    Holds the tail of the *previous* hypothesis (`buffer`) and commits the
    longest common prefix it shares with the *current* hypothesis (`new`).
    A word survives only when two consecutive decodes agree on it — this is what
    kills Whisper's mid-stream rewrites. Times are absolute (seconds).
    """

    def __init__(self) -> None:
        self.buffer: list[tuple[float, float, str]] = []  # prev hypothesis tail
        self.committed: list[tuple[float, float, str]] = []
        self.last_committed_time = 0.0

    def insert(self, words: list[tuple[float, float, str]], offset: float) -> None:
        """Register a fresh hypothesis. `words` times are buffer-relative; shift
        by `offset` to absolute, and drop anything already behind the commit."""
        shifted = [(s + offset, e + offset, w) for s, e, w in words]
        kept = [t for t in shifted if t[1] > self.last_committed_time]
        # Boundary de-dup: after a trim, the decoder re-hears the just-committed
        # word (its audio sits at the new buffer start) and would commit it again
        # — every commit's last word reappeared as the next commit's first word
        # ("five five", "and and"). Drop leading words that repeat the last
        # committed word right at the commit point.
        while (
            kept
            and self.committed
            and kept[0][2] == self.committed[-1][2]
            and kept[0][0] < self.last_committed_time + 0.5
        ):
            kept.pop(0)
        self.new = kept

    def flush(self) -> list[tuple[float, float, str]]:
        """Commit the common prefix of previous (`buffer`) and current (`new`)."""
        commit: list[tuple[float, float, str]] = []
        new = getattr(self, "new", [])
        while new and self.buffer:
            if new[0][2] == self.buffer[0][2]:
                commit.append(new[0])
                self.last_committed_time = new[0][1]
                self.buffer.pop(0)
                new.pop(0)
            else:
                break
        self.buffer = new  # current becomes the reference for the next round
        self.new = []
        self.committed.extend(commit)
        return commit


class StreamingTranscriber:
    """Rolling-buffer streaming transcriber for one dictation session."""

    # re-decode once this much *new* audio has arrived (latency vs CPU tradeoff).
    # 1.0s keeps steady-state decode cost under the real-time budget on laptop
    # CPUs while staying within the 0.5–2s streaming-latency band.
    MIN_CHUNK_S = 1.0
    # hard cap on buffer length; force-trim beyond this so a long monologue on a
    # weak CPU can't blow decode time unboundedly
    MAX_BUFFER_S = 15.0
    # rolling real-time-factor above which we downgrade the model once
    RTF_DOWNGRADE = 1.15
    RTF_WINDOW = 4

    def __init__(self, language: str | None = None) -> None:
        # Default to a pinned language when the client sends none. Auto-detect on
        # short VAD-trimmed streaming buffers is unreliable — it misfired to 'nn'
        # (Norwegian, prob 0.61) on English speech and then locked it. OKURO_STT_LANG
        # sets the default (default "en"); set it to "" to force true auto-detect.
        if language is None:
            language = os.environ.get("OKURO_STT_LANG", "en") or None
        self.language = language
        # tier authority picks model size + device; it already honours the
        # OKURO_STT_STREAM_MODEL pin, so _default_model_for is only the floor ref.
        from okuro.voice import tier as _tier

        backend = _tier.resolve_stt("stream", language)
        self.model_size = backend.model_size
        self._device = backend.device
        self._compute = backend.compute_type
        self.floor = _floor_for(language)
        if backend.use_broker:
            # pro/GPU: VRAM is reserved through the shared inference broker in a
            # follow-up; today we select the GPU via capability.recommended_device
            # (the same authority the broker uses, and the one that refuses the
            # display GPU). Logged so the seam is visible.
            logger.info("STT tier=pro on %s (broker lease: pending integration)", backend.device)
        # honour an explicit env override literally: if the user pinned a model,
        # don't second-guess it with the real-time auto-downgrade.
        self._allow_downgrade = os.environ.get("OKURO_STT_STREAM_MODEL") is None
        # Lock the language after the first decode so we don't re-detect (and
        # possibly flip) on every rolling-buffer pass. Starts as the pinned
        # ?lang= (or None → auto-detect once, then locked).
        self._decode_lang = self.language
        # Re-decode cadence. Kept at MIN_CHUNK_S (1.0s): a 0.5s cadence on GPU
        # pushed decode-per-new-audio over the RTF_DOWNGRADE threshold as the
        # rolling buffer grew, spuriously demoting large-v3 → tiny mid-stream.
        self._min_chunk_s = self.MIN_CHUNK_S
        self.model = _get_model(self.model_size, self._device, self._compute)
        self.audio = np.zeros(0, dtype=np.float32)
        self.buffer_time_offset = 0.0
        self.hyp = HypothesisBuffer()
        self._new_since_run = 0
        self._rtf: list[float] = []
        self._downgraded = False

    # -- audio intake -----------------------------------------------------
    def feed(self, pcm: np.ndarray) -> None:
        """Append float32 [-1,1] mono @16k samples."""
        self.audio = np.append(self.audio, pcm)
        self._new_since_run += len(pcm)

    def _decode(self, audio: np.ndarray):
        """Transcribe `audio` with anti-hallucination decode params.

        No `initial_prompt`: feeding the committed tail back made the model
        (large-v3 especially) *continue the sentence* and invent words on pauses
        or weak audio. Greedy `temperature=0` + no-speech / low-logprob /
        compression-ratio / hallucination-silence thresholds suppress the rest.
        Language locks after the first decode so it can't flip mid-stream.
        """
        segments, info = self.model.transcribe(
            audio,
            language=self._decode_lang,
            beam_size=1,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=True,
            temperature=0.0,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            hallucination_silence_threshold=0.5,
        )
        segs = list(segments)
        # Only lock an auto-detected language once confident — a low-probability
        # guess (English misreads as 'nn'/'da' at ~0.6) must never cement for the
        # whole session.
        if self._decode_lang is None and getattr(info, "language_probability", 0.0) >= 0.85:
            self._decode_lang = info.language
        return segs, info

    # -- core loop --------------------------------------------------------
    def process(self) -> tuple[str, str]:
        """Decode the current buffer if enough new audio arrived.

        Returns (committed_delta_text, interim_tail_text). Both may be empty.
        """
        if self._new_since_run < int(SAMPLE_RATE * self._min_chunk_s):
            return "", ""
        chunk_s = self._new_since_run / SAMPLE_RATE  # real audio arrived since last run
        self._new_since_run = 0

        t0 = time.monotonic()
        segments, _info = self._decode(self.audio)
        words: list[tuple[float, float, str]] = []
        for seg in segments:
            for w in seg.words or []:
                words.append((w.start, w.end, w.word))
        self._track_rtf(time.monotonic() - t0, chunk_s)

        self.hyp.insert(words, self.buffer_time_offset)
        committed = self.hyp.flush()
        self._trim()

        committed_text = "".join(w for _, _, w in committed)
        interim_text = "".join(w for _, _, w in self.hyp.buffer)
        return committed_text, interim_text

    def finish(self) -> str:
        """Flush on stop: commit whatever interim tail remains, uncorroborated."""
        segments, _info = self._decode(self.audio)
        words: list[tuple[float, float, str]] = []
        for seg in segments:
            for w in seg.words or []:
                if (w.start + self.buffer_time_offset) > self.hyp.last_committed_time - 0.1:
                    words.append(w.word)
        return "".join(words)

    # -- buffer management ------------------------------------------------
    def _trim(self) -> None:
        """Drop committed audio off the front; hard-cap runaway buffers."""
        cut_time = self.hyp.last_committed_time
        # hard cap: if the buffer outgrew MAX_BUFFER_S without a commit, force it
        buffer_len_s = len(self.audio) / SAMPLE_RATE
        if buffer_len_s > self.MAX_BUFFER_S:
            forced = self.buffer_time_offset + (buffer_len_s - self.MAX_BUFFER_S)
            cut_time = max(cut_time, forced)
        cut_samples = int((cut_time - self.buffer_time_offset) * SAMPLE_RATE)
        if 0 < cut_samples < len(self.audio):
            self.audio = self.audio[cut_samples:]
            self.buffer_time_offset = cut_time

    def _track_rtf(self, decode_s: float, chunk_s: float) -> None:
        """One-shot downgrade toward the language floor when decoding can't keep
        pace with the *incoming audio rate* (decode_s per second of new audio >
        1) — the true real-time keep-up metric, not decode-vs-buffer. The floor
        respects language: en/auto may fall to `tiny`, but non-English stops at
        `base` so German accuracy never collapses. Bounds latency on weak
        laptops."""
        if self._downgraded or not self._allow_downgrade:
            return
        if _MODEL_ORDER.index(self.model_size) <= _MODEL_ORDER.index(self.floor):
            self._downgraded = True  # already at/below floor, nothing to do
            return
        self._rtf.append(decode_s / max(chunk_s, 0.1))
        self._rtf = self._rtf[-self.RTF_WINDOW :]
        if len(self._rtf) >= self.RTF_WINDOW and (
            sum(self._rtf) / len(self._rtf)
        ) > self.RTF_DOWNGRADE:
            avg = sum(self._rtf) / len(self._rtf)
            logger.warning(
                "streaming STT slower than real time (rtf~%.2f) — downgrading '%s'→'%s'",
                avg,
                self.model_size,
                self.floor,
            )
            self.model = _get_model(self.floor, self._device, self._compute)
            self.model_size = self.floor
            self._downgraded = True
