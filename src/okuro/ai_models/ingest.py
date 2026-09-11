# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Model-knowledge ingestion — build an okuro.prompting/v1 block for a
#          bundle from the artifact we already have (GGUF embedded metadata),
#          so the model's prompting manual travels with its weights (offline,
#          versioned, resolvable by bundle id). Feeds the model-conditioned
#          prompt optimizer and the Models-page "prompt-ready" badge.
# index:
#   def family_from_arch
#   def prompting_from_llm_metadata   (pure — unit-tested)
#   def read_gguf_metadata            (thin GGUF I/O)
#   def build_prompting_block         (dispatch by capability/engine)
# AGENT_HEADER_END -->
"""Ingest a downloaded model's prompting knowledge into its bundle.

The design (brain report "Local inference — model knowledge ingestion +
model-conditioned prompt optimization") calls for a ``prompting`` block on
``okuro.bundle/v1`` carrying the per-model prompting manual. This module builds
that block. For a GGUF LLM everything we need — chat template, context length,
stop token, tool support — is embedded in the GGUF KV metadata, so ingestion is
offline and authoritative (no base-repo guessing, no hallucination).

Family conventions (write_memory) and inheritance edges (kg_add), plus image /
TTS families, are follow-on slices; the schema here is model-agnostic so adding
a modality is one more branch, not a rewrite.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("okuro.ai_models.ingest")

PROMPTING_SCHEMA = "okuro.prompting/v1"

# GGUF general.architecture → okuro family slug (the KG/memory inheritance key).
# Fallback: the architecture string itself, so an unknown arch still groups.
_ARCH_FAMILY = {
    "qwen3moe": "qwen3",
    "qwen3": "qwen3",
    "qwen2moe": "qwen2",
    "qwen2": "qwen2",
    "llama": "llama",
    "mistral": "mistral",
    "mixtral": "mistral",
    "gemma2": "gemma",
    "gemma3": "gemma",
    "gemma": "gemma",
    "phi3": "phi",
    "phi2": "phi",
    "command-r": "command-r",
    "deepseek2": "deepseek",
}


def family_from_arch(arch: Optional[str], name: Optional[str] = None) -> Optional[str]:
    """Map a GGUF architecture (or model name) to an okuro family slug."""
    if arch:
        a = arch.lower()
        if a in _ARCH_FAMILY:
            return _ARCH_FAMILY[a]
        # strip a trailing "moe"/version digits and retry the base token
        base = a.rstrip("0123456789").removesuffix("moe")
        if base in _ARCH_FAMILY:
            return _ARCH_FAMILY[base]
        return a
    if name:
        return name.split("-")[0].split(".")[0].lower() or None
    return None


# Model name/tag → the domain it's specialised for. Ordered specific→general;
# first match wins. The bet: a growing fleet of small domain-expert models, and
# okuro routes a task to the best-fit LOCAL specialist (specialized_for ×
# qualified × fits-VRAM). "general" is the honest fallback for a plain chat model.
_SPECIALIZATION_RULES = [
    ("code", ("coder", "code", "codestral", "starcoder", "codegemma", "deepseek-coder")),
    ("math", ("math", "mathstral", "deepseek-math")),
    ("sql", ("sql", "text2sql", "text-to-sql")),
    ("vision", ("-vl-", "vl-", "-vl", "vision", "llava", "pixtral", "moondream", "internvl")),
    ("embedding", ("embed", "bge-", "gte-", "e5-", "nomic-embed")),
    ("reranking", ("rerank",)),
    ("translation", ("translate", "translation", "nllb", "opus-mt", "madlad", "tower")),
    ("tool-use", ("hermes", "functionary", "gorilla")),
    ("medical", ("medical", "biomed", "clinical", "meditron", "medllama")),
    ("legal", ("legal", "law-", "lawyer", "saul")),
    ("reasoning", ("-r1", "reasoner", "qwq", "deepseek-r")),
]


def specialization_from_name(name: Optional[str]) -> Optional[str]:
    """Domain a model is specialised for, from its name (or None if no name)."""
    if not name:
        return None
    n = name.lower()
    for domain, needles in _SPECIALIZATION_RULES:
        if any(k in n for k in needles):
            return domain
    return "general"


def prompting_from_llm_metadata(meta: dict) -> dict:
    """Build an okuro.prompting/v1 block from extracted LLM GGUF metadata.

    Pure function over a plain dict so it is unit-testable without a real GGUF.
    ``meta`` keys (any may be missing → the field is omitted, never guessed):
    architecture, name, base_model, context_length, chat_template, eos_token,
    bos_token.
    """
    arch = meta.get("architecture")
    template = meta.get("chat_template")
    ctx = meta.get("context_length")
    stop = [t for t in (meta.get("eos_token"),) if t]

    constraints: dict = {}
    if ctx:
        constraints["context_length"] = int(ctx)
    # A chat template that references a ``tools`` variable advertises tool-calling.
    if template:
        constraints["supports_tools"] = "tools" in template
    # Attention dims → exact GQA-aware KV-cache VRAM estimate (fitting.py).
    for k in ("n_layers", "n_kv_heads", "head_dim"):
        if meta.get(k):
            constraints[k] = int(meta[k])

    block: dict = {
        "schema": PROMPTING_SCHEMA,
        "family": family_from_arch(arch, meta.get("name")),
        "prompt_syntax": "chat_template",
        "constraints": constraints,
        "stop_tokens": stop,
        "provenance": [
            {
                "field": "*",
                "source": "gguf:embedded_metadata",
                "confidence": 0.95,
            }
        ],
    }
    spec = specialization_from_name(meta.get("name"))
    if spec:
        block["specialized_for"] = spec
    if template:
        block["chat_template"] = {"source": "embedded_gguf", "template": template}
    if meta.get("base_model"):
        block["base_model"] = meta["base_model"]
    return block


# Civitai baseModel string → okuro convention-family slug. Checked in order so
# a Pony/Illustrious checkpoint (both SDXL-derived) maps to its own family, not
# the generic sdxl bucket. NoobAI shares Illustrious conventions.
_IMAGE_FAMILY_RULES = [
    ("pony", "pony"),
    ("illustrious", "illustrious"),
    ("noob", "illustrious"),
    ("flux", "flux"),
    ("sd 3", "sd3"),
    ("sd3", "sd3"),
    ("sdxl", "sdxl"),
    ("sd 1", "sd15"),
    ("sd1", "sd15"),
    ("sd 2", "sd15"),
    ("sd2", "sd15"),
]
# Image families that take prose, not booru tags. Everything else image-side
# defaults to booru_tags (the SD-ecosystem norm) at lower confidence.
_NL_IMAGE_FAMILIES = {"flux", "sd3"}


def image_family(base_model: Optional[str], name: Optional[str] = None) -> Optional[str]:
    """Map a Civitai baseModel (or model name) to an okuro convention family."""
    for text in (base_model, name):
        if not text:
            continue
        t = text.lower()
        for needle, fam in _IMAGE_FAMILY_RULES:
            if needle in t:
                return fam
    return None


def prompting_from_image_metadata(meta: dict) -> dict:
    """Build an okuro.prompting/v1 block for an image checkpoint from Civitai
    discovery metadata (already fetched — no network here).

    Pure over a plain dict. ``meta`` keys: base_model, name, trigger_words
    (Civitai trainedWords — LoRA/checkpoint activation). Family conventions
    (quality prefix, negative, params) are inherited at apply time, NOT copied
    here — this block holds only what is model-specific.
    """
    base = meta.get("base_model")
    family = image_family(base, meta.get("name"))
    if family in _NL_IMAGE_FAMILIES:
        syntax, conf = "natural_language", 0.9
    elif family:
        syntax, conf = "booru_tags", 0.9
    else:
        # unknown image checkpoint → SD-ecosystem prior, flagged low-confidence
        syntax, conf = "booru_tags", 0.5

    triggers = [w.strip() for w in (meta.get("trigger_words") or []) if w and w.strip()]
    block: dict = {
        "schema": PROMPTING_SCHEMA,
        "family": family,
        "prompt_syntax": syntax,
        "constraints": {},
        "trigger_words": triggers,
        "specialized_for": "image-generation",
        "provenance": [
            {
                "field": "prompt_syntax,family",
                "source": "civitai:base_model",
                "confidence": conf,
            }
        ],
    }
    if base:
        block["base_model"] = base
    return block


def read_gguf_metadata(gguf_path: Path) -> dict:
    """Extract the prompting-relevant KV fields from a GGUF file.

    Thin I/O over gguf.GGUFReader. Context length is architecture-prefixed
    (``<arch>.context_length``); the eos token is stored as an id we resolve to
    a string via the embedded vocab.
    """
    import gguf

    reader = gguf.GGUFReader(str(gguf_path))

    def field(name: str):
        f = reader.get_field(name)
        return f.contents() if f is not None else None

    arch = field("general.architecture")
    meta: dict = {
        "architecture": arch,
        "name": field("general.name"),
        "base_model": field("general.base_model.0.name"),
        "chat_template": field("tokenizer.chat_template"),
    }
    if arch:
        meta["context_length"] = field(f"{arch}.context_length")
        # Attention dims for an EXACT, GQA-aware KV-cache VRAM estimate. head_dim
        # is stored explicitly on some archs (Qwen sets key_length); else derive
        # it from hidden/head_count. n_kv_heads may be a per-layer list — take
        # the max (worst case) so the estimate never under-counts.
        n_layers = field(f"{arch}.block_count")
        n_heads = field(f"{arch}.attention.head_count")
        n_kv_heads = field(f"{arch}.attention.head_count_kv")
        hidden = field(f"{arch}.embedding_length")
        key_length = field(f"{arch}.attention.key_length")
        if isinstance(n_kv_heads, (list, tuple)) and n_kv_heads:
            n_kv_heads = max(int(x) for x in n_kv_heads)
        head_dim = None
        if key_length:
            head_dim = int(key_length)
        elif hidden and n_heads:
            head_dim = int(hidden) // int(n_heads)
        if n_layers:
            meta["n_layers"] = int(n_layers)
        if n_kv_heads:
            meta["n_kv_heads"] = int(n_kv_heads)
        elif n_heads:
            meta["n_kv_heads"] = int(n_heads)  # no GQA → kv heads = attn heads
        if head_dim:
            meta["head_dim"] = head_dim

    eos_id = field("tokenizer.ggml.eos_token_id")
    tokens = field("tokenizer.ggml.tokens")
    if eos_id is not None and tokens is not None and 0 <= int(eos_id) < len(tokens):
        meta["eos_token"] = tokens[int(eos_id)]
    return meta


def build_prompting_block(bundle, *, entry=None, storage=None, research=False) -> Optional[dict]:
    """Dispatch by capability → an okuro.prompting/v1 block, or None.

    LLM/VLM: read the embedded GGUF metadata (offline, authoritative).
    Image: derive from the ``entry`` CatalogEntry's Civitai metadata (baseModel
    → family, trainedWords → trigger words) — no GGUF, so the entry is the
    source. TTS is a follow-on branch.
    """
    from okuro.ai_models.bundle import Bundle  # local import avoids a cycle

    assert isinstance(bundle, Bundle)

    from okuro.ai_models.family import record_family

    if bundle.capability == "image" and entry is not None:
        block = prompting_from_image_metadata({
            "base_model": getattr(entry, "base_model", None),
            "name": getattr(entry, "display_name", None) or bundle.display_name,
            # Civitai trainedWords are carried on the entry as capabilities.
            "trigger_words": list(getattr(entry, "capabilities", []) or []),
        })
        # Install-time research: for an UNKNOWN family, ask an LLM once for the
        # model's prompting conventions and bake them into the block (opt-in;
        # non-fatal). Known families already carry curated conventions.
        if research:
            from okuro.ai_models.prompt_research import enrich_prompting_block
            block = enrich_prompting_block(
                block, name=getattr(entry, "display_name", None) or bundle.display_name)
        record_family(bundle.id, block.get("family"), block.get("base_model"))
        return block

    gguf_files = [f for f in bundle.files if f.name.lower().endswith(".gguf")]
    if bundle.capability in ("llm", "vlm") and gguf_files:
        gguf_files.sort(key=lambda f: (f.role != "weights", f.name))
        path = bundle.path / "files" / gguf_files[0].name
        if not path.exists():
            logger.warning("ingest: gguf missing for %s at %s", bundle.id, path)
            return None
        try:
            meta = read_gguf_metadata(path)
        except Exception as exc:  # never fail a pull on ingest trouble
            logger.warning("ingest: gguf metadata read failed for %s: %s", bundle.id, exc)
            return None
        block = prompting_from_llm_metadata(meta)
        # Persist the relations + family tier (KG edges + convention memory) so
        # future merges of this family inherit without re-deriving. Non-fatal.
        record_family(bundle.id, block.get("family"), block.get("base_model"))
        return block
    return None
