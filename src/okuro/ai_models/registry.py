# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Model catalog: find, check, list AI models from multiple sources.
# index:
#   imports
#   class ModelInfo
#   def scan_models
#   def list_models
#   def get_model
#   def _generate_alias
#   def _extract_params
#   def _extract_quantization
#   def _get_subscription_models
# AGENT_HEADER_END -->
"""Model catalog: find, check, list AI models from multiple sources."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ModelInfo:
    """Information about an AI model."""

    name: str
    alias: str
    source: str  # "local-gguf", "subscription", "api"
    path: Optional[str] = None
    size_gb: float = 0.0
    parameters: Optional[str] = None  # e.g. "7B", "32B"
    quantization: Optional[str] = None  # e.g. "Q4_K_M", "Q8_0"
    capabilities: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "alias": self.alias,
            "source": self.source,
            "path": self.path,
            "size_gb": self.size_gb,
            "parameters": self.parameters,
            "quantization": self.quantization,
            "capabilities": self.capabilities or [],
        }


# Default GGUF scan directories: the HF cache plus whatever the user's
# ``ai_models.scan_dirs`` convention names (their local model store).
# NOTE: scan only TEXT-model roots — the hardcoded
# capabilities=["text-generation"] label must stay honest (image/video/vision
# GGUFs in sibling dirs must not be picked up). Superseded by the bundle
# store scan in P1.
def _default_scan_dirs() -> list[str]:
    from okuro.yu.conventions import get_convention

    extra = get_convention("ai_models.scan_dirs", []) or []
    return [str(Path(p).expanduser()) for p in extra] + [
        str(Path.home() / ".cache" / "huggingface" / "hub"),
    ]


_DEFAULT_SCAN_DIRS = _default_scan_dirs()


def scan_models(
    scan_dirs: list[str] | None = None,
) -> list[ModelInfo]:
    """Scan directories for GGUF model files.

    Args:
        scan_dirs: Directories to scan. Defaults to common locations.

    Returns:
        List of discovered ModelInfo objects.
    """
    dirs = scan_dirs or _DEFAULT_SCAN_DIRS
    models = []

    for dir_path in dirs:
        path = Path(dir_path)
        if not path.exists():
            continue

        for gguf_file in path.rglob("*.gguf"):
            try:
                size_gb = round(gguf_file.stat().st_size / (1024**3), 2)
            except OSError:
                size_gb = 0.0

            name = gguf_file.stem
            alias = _generate_alias(name)
            params = _extract_params(name)
            quant = _extract_quantization(name)

            models.append(
                ModelInfo(
                    name=name,
                    alias=alias,
                    source="local-gguf",
                    path=str(gguf_file),
                    size_gb=size_gb,
                    parameters=params,
                    quantization=quant,
                    capabilities=["text-generation"],
                )
            )

    return sorted(models, key=lambda m: m.name)


def list_models(
    scan_dirs: list[str] | None = None,
    include_subscriptions: bool = True,
    include_bundles: bool = True,
) -> list[dict]:
    """List all known models (bundles + local GGUF scan + subscription).

    Args:
        scan_dirs: Directories to scan for legacy local GGUF models.
        include_subscriptions: Include subscription models from bridge config.
        include_bundles: Include models from the bundle store (source="bundle").
    """
    results: list[dict] = []

    if include_bundles:
        from .bundle import scan_bundles

        results.extend(b.to_dict() for b in scan_bundles())

    results.extend(m.to_dict() for m in scan_models(scan_dirs))

    if include_subscriptions:
        results.extend(_get_subscription_models())

    return results


def get_model(name_or_alias: str, scan_dirs: list[str] | None = None) -> dict | None:
    """Find a model by name or alias."""
    name_lower = name_or_alias.lower()
    for m in scan_models(scan_dirs):
        if m.name.lower() == name_lower or m.alias.lower() == name_lower:
            return m.to_dict()
    return None


def _generate_alias(filename: str) -> str:
    """Generate a short alias from a GGUF filename.

    e.g. 'Qwen2.5-Coder-7B-Instruct-Q4_K_M' → 'qwen2-5-coder-7b'
    """
    name = filename.lower()
    # Remove quantization suffix
    name = re.sub(r"[-_]q\d+_\w+.*$", "", name)
    name = re.sub(r"[-_]f\d+$", "", name)
    # Remove instruct/chat suffixes
    name = re.sub(r"[-_](instruct|chat|it|base)$", "", name)
    # Clean up
    name = re.sub(r"[._]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name


def _extract_params(filename: str) -> Optional[str]:
    """Extract parameter count from filename (e.g. '7B', '32B')."""
    m = re.search(r"(\d+(?:\.\d+)?)[bB]", filename)
    return f"{m.group(1)}B" if m else None


def _extract_quantization(filename: str) -> Optional[str]:
    """Extract quantization from filename (e.g. 'Q4_K_M', 'Q8_0')."""
    m = re.search(r"(Q\d+_\w+|F\d+)", filename, re.IGNORECASE)
    return m.group(1).upper() if m else None


def _get_subscription_models() -> list[dict]:
    """Get subscription models from bridge config."""
    try:
        from okuro.bridge.config import load_config

        config = load_config()
        results = []
        for provider_id, prov in config.get("providers", {}).items():
            ptype = prov.get("type", "cli")
            if ptype in ("local-http",):
                continue  # Skip local providers (handled by scan)
            for tier, model_name in prov.get("models", {}).items():
                results.append(
                    {
                        "name": model_name,
                        "alias": f"{provider_id}/{tier}",
                        "source": "subscription",
                        "path": None,
                        "size_gb": 0.0,
                        "parameters": None,
                        "quantization": None,
                        "capabilities": prov.get("capabilities", []),
                    }
                )
        return results
    except Exception:
        return []
