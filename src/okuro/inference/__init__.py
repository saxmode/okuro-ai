# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.inference — local inference broker (single VRAM authority).
# index: none
# AGENT_HEADER_END -->
"""okuro.inference — the single VRAM authority between okuro and every model
it runs (embeddings, LLM, TTS, media). See broker.Broker for admission and
engine.EngineRunner for serving a bundle under a lease."""

from .broker import Broker, BrokerRejection, Lease
from .engine import (
    LLAMA_CPP_PYTHON,
    LLAMA_SERVER,
    VLLM,
    EngineCommand,
    EngineRunner,
    EngineStartError,
    RunningEngine,
    build_command,
    default_tier,
)
from .engine_registry import EngineRegistry

__all__ = [
    "Broker",
    "BrokerRejection",
    "Lease",
    "EngineRunner",
    "RunningEngine",
    "EngineCommand",
    "EngineStartError",
    "EngineRegistry",
    "build_command",
    "default_tier",
    "VLLM",
    "LLAMA_SERVER",
    "LLAMA_CPP_PYTHON",
]
