# SPDX-License-Identifier: Apache-2.0
"""Hatchling build hook — vendors the embedding model into the wheel.

Downloads ``Alibaba-NLP/gte-modernbert-base`` (the LOW / laptop-safe tier,
768d, ~150MB) into ``src/okuro/embed/models/gte-modernbert-base`` before wheel
assembly so the bundled model matches ``embed.config.TIERS['low']`` and ships
inside the package. Users never hit the HuggingFace Hub for the default tier,
nothing lands in ``~/.cache/huggingface/``, and ``rm -rf ~/.okuro`` leaves
zero residue.

Pre-2026-06 this vendored the long-dead ``BAAI/bge-small-en-v1.5``, which is no
longer a registered tier — so NEITHER the low nor high model was bundled and
GPU-less machines tried to pull the model from HF at runtime (Qwen3-0.6B,
1.2GB), which failed and left okuro-embed inactive. The HIGH tier (Qwen3) is
intentionally NOT vendored — it targets GPU boxes that download it on demand.

The download is cached between builds: if ``config.json`` already exists in
the target dir the hook is a no-op. Set ``OKURO_SKIP_MODEL_VENDOR=1`` to
skip entirely (useful for offline CI where the model is vendored by some
other means).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

MODEL_ID = "Alibaba-NLP/gte-modernbert-base"
MODEL_REL = "src/okuro/embed/models/gte-modernbert-base"

# Only ship what SentenceTransformer needs to load the model offline.
# Excludes pytorch_model.bin (legacy, redundant with safetensors), ONNX
# (separate extras path), and README.md. Verified 2026-06: gte-modernbert
# loads offline from exactly these files (it has no vocab.txt — ModernBERT
# uses tokenizer.json; the entry is harmless and kept for BERT-style models).
_ALLOW_PATTERNS = [
    "config.json",
    "config_sentence_transformers.json",
    "sentence_bert_config.json",
    "modules.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "model.safetensors",
    "1_Pooling/config.json",
]


class VendorModelHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if os.environ.get("OKURO_SKIP_MODEL_VENDOR") == "1":
            self.app.display_info("[okuro] OKURO_SKIP_MODEL_VENDOR=1 — skipping model vendor step")
            return

        dest = Path(self.root) / MODEL_REL
        marker = dest / "config.json"
        if not marker.exists():
            dest.mkdir(parents=True, exist_ok=True)
            from huggingface_hub import snapshot_download

            self.app.display_info(f"[okuro] vendoring {MODEL_ID} → {dest}")
            snapshot_download(
                repo_id=MODEL_ID,
                local_dir=str(dest),
                allow_patterns=_ALLOW_PATTERNS,
            )
            self.app.display_info("[okuro] model vendored")
        else:
            self.app.display_info(f"[okuro] model already vendored at {dest}")

        # huggingface_hub drops its own bookkeeping dir — we don't need it at
        # runtime and don't want 14 .metadata files bloating the wheel.
        cache_leak = dest / ".cache"
        if cache_leak.exists():
            shutil.rmtree(cache_leak)
