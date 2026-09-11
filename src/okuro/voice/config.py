# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Pydantic voice-config loader — reads 'voice:' block from ~/.okuro/config.yaml.
# index:
#   imports
#   class STTConfig
#   class TTSConfig
#   class AudioConfig
#   class VADConfig
#   class TriggerConfig
#   class LLMConfig
#   class LoggingConfig
#   class VoiceConfig
#   def load_voice_config
#   def default_config_path
# AGENT_HEADER_END -->
"""Voice configuration — Pydantic v2 models + YAML loader.

Matches ADR 1.3 §7 shape exactly. Every field has a sane default so a user
with no ``voice:`` block still gets ``VoiceConfig(enabled=False)`` rather
than a stack trace on config access.

The config lives in the SAME ``~/.okuro/config.yaml`` as the bridge — per
the ADR, voice does not add a new file (DP09 NO-BLOAT).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError
from okuro.db.engine import okuro_home


def default_config_path() -> Path:
    """Path to ``~/.okuro/config.yaml``. Callers can override for tests."""
    return okuro_home() / "config.yaml"


class STTConfig(BaseModel):
    provider: str = "deepgram"
    model: str = "nova-3"
    language: str = "en"
    endpointing_ms: int = Field(default=300, ge=0, le=5000)


class TTSConfig(BaseModel):
    provider: str = "elevenlabs"
    model: str = "eleven_flash_v2_5"
    voice_id: str = "rachel"
    output_format: str = "pcm_24000"


class AudioConfig(BaseModel):
    input_device: Optional[str] = None
    output_device: Optional[str] = None
    input_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    output_sample_rate: int = Field(default=24000, ge=8000, le=48000)
    block_size: int = Field(default=512, ge=64, le=8192)


class VADConfig(BaseModel):
    engine: Literal["silero", "none"] = "silero"
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    min_silence_ms: int = Field(default=500, ge=0, le=10000)
    min_speech_ms: int = Field(default=250, ge=0, le=10000)


class TriggerConfig(BaseModel):
    """Turn trigger mode.

    - ``cli``    — v1 default. Press Enter to start, VAD ends the turn.
    - ``vad``    — auto-start (VAD detects speech onset too) + auto-end.
    - ``ptt``    — push-to-talk — press-and-hold a key. Fallback when
                   VAD is unreliable (noisy environment, non-English).
    - ``hotkey`` — v2 daemon mode — global hotkey triggers a turn.
    """

    mode: Literal["cli", "vad", "ptt", "hotkey"] = "cli"
    hotkey: Optional[str] = None
    ptt_key: str = "space"


class LLMConfig(BaseModel):
    capability: str = "voice"
    max_turns: int = Field(default=20, ge=1, le=500)
    context_window_chars: int = Field(default=8000, ge=500, le=200000)


class LoggingConfig(BaseModel):
    transcripts: bool = True
    audio_debug: bool = False


class VoiceConfig(BaseModel):
    enabled: bool = False
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_voice_config(path: Optional[Path] = None) -> VoiceConfig:
    """Load ``voice:`` from ``~/.okuro/config.yaml``.

    Missing file → default config (``enabled=False``).
    Missing ``voice:`` key → default config.
    Invalid YAML or schema violation → ``ValidationError`` re-raised.

    The path argument is purely for unit testing — production callers
    pass nothing and get the standard location.
    """
    p = path or default_config_path()
    if not p.exists():
        return VoiceConfig()

    raw = yaml.safe_load(p.read_text()) or {}
    voice_block = raw.get("voice") or {}

    try:
        return VoiceConfig(**voice_block)
    except ValidationError:
        raise
