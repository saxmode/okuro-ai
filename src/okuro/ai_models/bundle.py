# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bundle model store — self-contained model dirs + okuro.bundle/v1 manifest.
# index:
#   imports
#   constants
#   class BundleFile
#   class Bundle
#   def bundles_root
#   def load_bundle
#   def scan_bundles
#   def write_bundle
# AGENT_HEADER_END -->
"""Bundle model store — one self-contained servable dir per inference target.

A *bundle* holds everything one model needs to be served (weights + tokenizer
+ chat template + any projector/vae) under a single id, accepting file
duplication over the old type-split store. Layout::

    <bundles_root>/<id>/
        bundle.json        # okuro.bundle/v1 manifest
        files/             # weights + tokenizer + template + ...

Distinct from the catalog (`okuro.catalog/v1`, discoverable-but-not-downloaded):
a bundle is downloaded + servable. This module is the read/write/scan core the
catalog, acquisition (`okuro models pull`), broker, and Models page consume.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .fitting import estimate_vram

SCHEMA = "okuro.bundle/v1"


def bundles_root() -> Path:
    """Root dir for the bundle store.

    Resolution: OKURO_BUNDLES_ROOT env → ``ai_models.bundles_root``
    convention → ``~/.okuro/models/bundles``.
    """
    env = os.environ.get("OKURO_BUNDLES_ROOT")
    if env:
        return Path(env)
    from okuro.yu.conventions import get_convention

    return Path(
        get_convention("ai_models.bundles_root", "~/.okuro/models/bundles")
    ).expanduser()


@dataclass
class BundleFile:
    """One file inside a bundle's files/ dir."""

    name: str
    role: str = "weights"  # weights | tokenizer | template | projector | vae | ...
    size_bytes: int = 0
    sha256: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "BundleFile":
        return cls(
            name=d["name"],
            role=d.get("role", "weights"),
            size_bytes=int(d.get("size_bytes", 0) or 0),
            sha256=d.get("sha256"),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass
class Bundle:
    """A downloaded, servable model bundle (okuro.bundle/v1)."""

    id: str
    capability: str  # llm | embed | image | tts | vlm | video | music | ...
    path: Path
    display_name: str = ""
    engine: Optional[str] = None  # llama-server | vllm | ollama | ...
    engine_args: dict = field(default_factory=dict)
    format: Optional[str] = None  # gguf | safetensors | ...
    quant: Optional[str] = None
    parameters: Optional[str] = None  # "32B"
    files: list[BundleFile] = field(default_factory=list)
    chat_template: Optional[dict] = None
    context_length: Optional[int] = None
    vram_estimate_gb: Optional[float] = None
    source: dict = field(default_factory=dict)
    license: Optional[dict] = None
    # Forward-compat blocks (stream F prompting, tier qualification) — kept raw
    # so this core doesn't have to know their schema yet.
    prompting: Optional[dict] = None
    qualification: Optional[dict] = None

    @property
    def size_gb(self) -> float:
        total = sum(f.size_bytes for f in self.files)
        return round(total / (1024 ** 3), 2) if total else 0.0

    @property
    def vram_gb(self) -> float:
        """VRAM requirement. Prefers the EXACT GQA-aware estimate when the
        ingested prompting block carries attention dims (n_layers/n_kv_heads/
        head_dim); else the manifest override; else the coarse params+quant
        estimate. Uses the model's own context_length — the KV cache at full
        context is exactly where the old estimate under-counted into OOM."""
        ctx = self.context_length or 8192
        dims = (self.prompting or {}).get("constraints", {}) if self.prompting else {}
        if dims.get("n_layers") and dims.get("head_dim"):
            import re
            from .fitting import estimate_vram_detailed

            m = re.match(r"(\d+(?:\.\d+)?)", self.parameters or "")
            param_b = float(m.group(1)) if m else 0.0
            return estimate_vram_detailed(
                param_b, self.quant,
                n_layers=dims.get("n_layers"),
                n_kv_heads=dims.get("n_kv_heads"),
                head_dim=dims.get("head_dim"),
                context_length=ctx,
            )["total"]
        if self.vram_estimate_gb is not None:
            return float(self.vram_estimate_gb)
        return estimate_vram(self.parameters, self.quant, ctx)

    def to_dict(self) -> dict:
        """Superset of the registry ModelInfo shape so the Models page can
        render bundles alongside legacy GGUF scan results."""
        return {
            "name": self.display_name or self.id,
            "alias": self.id,
            "source": "bundle",
            "path": str(self.path),
            "size_gb": self.size_gb,
            "parameters": self.parameters,
            "quantization": self.quant,
            "capabilities": [self.capability] if self.capability else [],
            # bundle-specific extras (ignored by legacy consumers)
            "id": self.id,
            "capability": self.capability,
            "engine": self.engine,
            "format": self.format,
            "context_length": self.context_length,
            "vram_estimate_gb": self.vram_gb,
            "license": self.license,
            "prompt_ready": self.prompting is not None,
            "prompt_syntax": (self.prompting or {}).get("prompt_syntax"),
            "qualified": self.qualification is not None,
        }

    @classmethod
    def from_manifest(cls, data: dict, path: Path) -> "Bundle":
        return cls(
            id=data["id"],
            capability=data.get("capability", "llm"),
            path=path,
            display_name=data.get("display_name", ""),
            engine=data.get("engine"),
            engine_args=data.get("engine_args", {}) or {},
            format=data.get("format"),
            quant=data.get("quant"),
            parameters=data.get("parameters"),
            files=[BundleFile.from_dict(f) for f in data.get("files", [])],
            chat_template=data.get("chat_template"),
            context_length=data.get("context_length"),
            vram_estimate_gb=data.get("vram_estimate_gb"),
            source=data.get("source", {}) or {},
            license=data.get("license"),
            prompting=data.get("prompting"),
            qualification=data.get("qualification"),
        )

    def to_manifest(self) -> dict:
        """Serializable okuro.bundle/v1 manifest (path is not persisted)."""
        m: dict[str, Any] = {
            "schema": SCHEMA,
            "id": self.id,
            "display_name": self.display_name or self.id,
            "capability": self.capability,
            "engine": self.engine,
            "engine_args": self.engine_args,
            "format": self.format,
            "quant": self.quant,
            "parameters": self.parameters,
            "files": [f.to_dict() for f in self.files],
            "chat_template": self.chat_template,
            "context_length": self.context_length,
            "vram_estimate_gb": self.vram_estimate_gb,
            "source": self.source,
            "license": self.license,
        }
        if self.prompting is not None:
            m["prompting"] = self.prompting
        if self.qualification is not None:
            m["qualification"] = self.qualification
        return m


def load_bundle(bundle_dir: str | os.PathLike) -> Optional[Bundle]:
    """Load one bundle from its dir. Returns None if absent/invalid."""
    path = Path(bundle_dir)
    manifest = path / "bundle.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text())
    except (ValueError, OSError):
        return None
    if not str(data.get("schema", "")).startswith("okuro.bundle/"):
        return None
    if not data.get("id"):
        return None
    return Bundle.from_manifest(data, path)


def scan_bundles(root: str | os.PathLike | None = None) -> list[Bundle]:
    """Discover all bundles under the store root (missing root → [])."""
    base = Path(root) if root is not None else bundles_root()
    if not base.is_dir():
        return []
    found: list[Bundle] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        bundle = load_bundle(child)
        if bundle is not None:
            found.append(bundle)
    return found


def write_bundle(bundle: Bundle, root: str | os.PathLike | None = None) -> Path:
    """Persist a bundle's manifest under <root>/<id>/bundle.json.

    Creates the bundle dir (and files/ subdir) if missing. Weight files are
    materialized separately by the acquisition layer; this only writes the
    manifest. Returns the bundle dir.
    """
    base = Path(root) if root is not None else bundles_root()
    target = base / bundle.id
    (target / "files").mkdir(parents=True, exist_ok=True)
    bundle.path = target
    (target / "bundle.json").write_text(json.dumps(bundle.to_manifest(), indent=2))
    return target
