# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Provider inventory — auto-detect the local inference servers on this
#          box (Ollama, ComfyUI, vLLM, A1111, llama-server, llama-cpp-python):
#          installed? running? holding VRAM? how to reclaim it? One structure
#          feeds the contention modal, the reclaim buttons, and the "what local
#          inference exists here" view.
# index:
#   class ProviderInfo
#   KNOWN_PROVIDERS
#   def _http_json / _which / _has_pkg
#   def detect_providers
# AGENT_HEADER_END -->
"""Deterministic multi-signal discovery of installed inference providers.

Provider identity is a SIGNATURE MATCH, not a reasoning task, so we probe rather
than ask a model: a GET to :11434 that answers Ollama's API is proof, cheaper
and surer than any inference. Three signals, unioned:
  - port probe  → running servers (+ their loaded models, + control endpoint)
  - binary/pkg  → installed-but-idle (which ollama, importable vllm/llama_cpp)
The ambiguous long tail (a bare ``python`` holding VRAM that matches nothing) is
left to a fast-model classifier elsewhere — this module only does the certain.

All I/O (http_get / which / has_pkg) is injectable so detection is unit-testable
without live servers.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

# reclaim methods
GRACEFUL_API = "graceful_api"   # server has an unload endpoint; keeps running
PROCESS_STOP = "process_stop"   # no unload → must stop the process (destructive)
NONE = "none"


@dataclass
class ProviderInfo:
    provider: str
    installed: bool = False
    running: bool = False
    endpoint: Optional[str] = None
    loaded_models: list = field(default_factory=list)
    reclaim: str = NONE
    detected_via: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "installed": self.installed,
            "running": self.running,
            "endpoint": self.endpoint,
            "loaded_models": self.loaded_models,
            "reclaim": self.reclaim,
            "detected_via": self.detected_via,
        }


# Known providers + how to recognise and reclaim each. ``models_key`` is the dotted
# path into the probe JSON that lists loaded/available models (best-effort).
KNOWN_PROVIDERS = [
    {"name": "ollama", "port": 11434, "probe": "/api/tags",
     "models_key": "models[].name", "reclaim": GRACEFUL_API,
     "binaries": ["ollama"], "packages": []},
    {"name": "comfyui", "port": 8188, "probe": "/system_stats",
     "models_key": None, "reclaim": GRACEFUL_API,
     "binaries": [], "packages": []},
    {"name": "vllm", "port": 8000, "probe": "/v1/models",
     "models_key": "data[].id", "reclaim": PROCESS_STOP,
     "binaries": ["vllm"], "packages": ["vllm"]},
    {"name": "automatic1111", "port": 7860, "probe": "/sdapi/v1/sd-models",
     "models_key": "[].model_name", "reclaim": GRACEFUL_API,
     "binaries": [], "packages": []},
    {"name": "llama-server", "port": None, "probe": None,
     "models_key": None, "reclaim": PROCESS_STOP,
     "binaries": ["llama-server"], "packages": []},
    {"name": "llama-cpp-python", "port": None, "probe": None,
     "models_key": None, "reclaim": PROCESS_STOP,
     "binaries": [], "packages": ["llama_cpp"]},
]


# Tenant name → base reclaim capability. Graceful unload needs a REACHABLE
# endpoint (to call /free etc.); without one we fall back to stopping the
# process — so a comfyui holding VRAM is still reclaimable even when its server
# port didn't probe. okuro's own engines are broker-managed (evicted, not
# reclaimed here); an unknown raw process is inform-only.
_GRACEFUL_TENANTS = {"comfyui", "ollama", "automatic1111"}
_PROCESS_TENANTS = {"vllm", "llama-server", "llama-cpp-python"}


def reclaim_for_tenant(tenant: Optional[str], endpoint: Optional[str] = None) -> str:
    """Reclaim method for a named tenant given whether we have a live endpoint."""
    if tenant in _GRACEFUL_TENANTS:
        return GRACEFUL_API if endpoint else PROCESS_STOP
    if tenant in _PROCESS_TENANTS:
        return PROCESS_STOP
    return NONE


def _http_json(url: str, timeout: float = 1.5) -> Optional[dict]:
    """GET JSON from a local URL, or None (server down / not this provider)."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status and 200 <= resp.status < 300:
                return json.loads(resp.read())
    except Exception:
        return None
    return None


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _has_pkg(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _extract_models(payload, models_key: Optional[str]) -> list:
    """Pull a model-name list from a probe response via a tiny grammar:
    ``<container>[].<leaf>`` — e.g. ``models[].name`` / ``data[].id`` /
    ``[].model_name`` (empty container = the payload is itself the list)."""
    if payload is None or not models_key:
        return []
    left, _, right = models_key.partition("[]")
    container = payload
    for part in [p for p in left.strip(".").split(".") if p]:
        container = container.get(part) if isinstance(container, dict) else None
        if container is None:
            return []
    if not isinstance(container, list):
        return []
    leaf = right.strip(".")
    if not leaf:
        return []
    return [e.get(leaf) for e in container if isinstance(e, dict) and e.get(leaf)]


def detect_providers(
    *,
    http_get: Optional[Callable[[str], Optional[dict]]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    has_pkg: Optional[Callable[[str], bool]] = None,
    host: str = "127.0.0.1",
) -> list[ProviderInfo]:
    """Detect installed/running inference providers on this box.

    Returns one ProviderInfo per KNOWN provider that is installed OR running.
    Injected ``http_get`` / ``which`` / ``has_pkg`` make it testable offline.
    """
    http_get = http_get or _http_json
    which = which or _which
    has_pkg = has_pkg or _has_pkg

    out: list[ProviderInfo] = []
    for spec in KNOWN_PROVIDERS:
        via: list[str] = []
        installed = False
        for b in spec["binaries"]:
            if which(b):
                installed = True
                via.append(f"binary:{b}")
        for p in spec["packages"]:
            if has_pkg(p):
                installed = True
                via.append(f"package:{p}")

        running = False
        endpoint = None
        models: list = []
        if spec["port"] and spec["probe"]:
            url = f"http://{host}:{spec['port']}{spec['probe']}"
            payload = http_get(url)
            if payload is not None:
                running = True
                endpoint = f"http://{host}:{spec['port']}"
                via.append(f"port:{spec['port']}")
                models = [m for m in _extract_models(payload, spec["models_key"]) if m]

        if installed or running:
            out.append(ProviderInfo(
                provider=spec["name"],
                installed=installed or running,
                running=running,
                endpoint=endpoint,
                loaded_models=models,
                reclaim=spec["reclaim"],
                detected_via=via,
            ))
    return out
