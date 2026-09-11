# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.voice — public entry points for voice turns/sessions.
# index:
#   imports
#   def session
#   def run_session
#   def run_session
# AGENT_HEADER_END -->
"""okuro.voice — talk to okuro, listen to okuro.

Primary entry points
--------------------
``okuro.voice.session()``
    Run a single turn: listen → transcribe → brain → speak → return.

``okuro.voice.run_session(max_turns=...)``
    Continuous mode — loops ``session()`` until ``max_turns`` or Ctrl-C.

Both are thin wrappers over ``orchestrator.TurnOrchestrator``.

Everything under ``okuro.voice`` is provider-agnostic above the adapter
boundary (``stt.py``, ``tts.py``). Tests inject fake providers; production
auto-resolves Deepgram + ElevenLabs from config.
"""

from __future__ import annotations

from typing import Optional

from .config import VoiceConfig, load_voice_config
from .errors import (
    BridgeError,
    MicPermissionError,
    MissingKeyError,
    NoDeviceError,
    STTError,
    TTSError,
    VADError,
    VoiceError,
)

__all__ = [
    "session",
    "run_turn",
    "run_session",
    "VoiceConfig",
    "load_voice_config",
    "VoiceError",
    "MissingKeyError",
    "NoDeviceError",
    "MicPermissionError",
    "STTError",
    "TTSError",
    "VADError",
    "BridgeError",
]


def session(
    config: Optional[VoiceConfig] = None,
    *,
    stt_client=None,
    tts_client=None,
    bridge_invoke=None,
    audio_in=None,
    audio_out=None,
) -> dict:
    """Run a single voice turn and return its result.

    This is the single public entry point called out in the subtask brief:
    ``okuro.voice.session()`` runs one turn (listen → transcribe →
    ``bridge.invoke`` → speak).

    All provider/audio arguments are injectable for tests. Production
    callers pass no arguments — defaults resolve from config + keyring.

    Returns
    -------
    dict
        ``{"transcript": str, "reply": str, "duration": float, "turn_index": int}``
    """
    from .orchestrator import TurnOrchestrator

    cfg = config or load_voice_config()
    orch = TurnOrchestrator(
        cfg,
        stt_client=stt_client,
        tts_client=tts_client,
        bridge_invoke=bridge_invoke,
        audio_in=audio_in,
        audio_out=audio_out,
    )
    return orch.run_turn()


# Alias kept for ADR §3 module-layout compatibility — the ADR lists both
# ``run_turn()`` and ``run_session()`` in the public API.
run_turn = session


def run_session(
    config: Optional[VoiceConfig] = None,
    *,
    max_turns: Optional[int] = None,
    stt_client=None,
    tts_client=None,
    bridge_invoke=None,
    audio_in=None,
    audio_out=None,
) -> list[dict]:
    """Run multiple turns in a row (continuous mode).

    Stops when ``max_turns`` is reached, on KeyboardInterrupt, or when the
    orchestrator returns an empty transcript (user hung up).
    """
    from .orchestrator import TurnOrchestrator

    cfg = config or load_voice_config()
    orch = TurnOrchestrator(
        cfg,
        stt_client=stt_client,
        tts_client=tts_client,
        bridge_invoke=bridge_invoke,
        audio_in=audio_in,
        audio_out=audio_out,
    )
    limit = max_turns or cfg.llm.max_turns
    results: list[dict] = []
    try:
        for _ in range(limit):
            result = orch.run_turn()
            results.append(result)
            if not result.get("transcript", "").strip():
                break
    except KeyboardInterrupt:
        pass
    return results
