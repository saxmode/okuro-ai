# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Family-level prompting conventions — the INHERIT tier. A per-model
#          bundle block holds only what is genuinely model-specific; the shared
#          rules ("Pony needs score tags", "Flux is natural-language only",
#          "Qwen3 samples at temp 0.6") live here, written once per family and
#          inherited by every current + future merge of that family.
# index:
#   FAMILY_CONVENTIONS               (curated seed — proof-cited)
#   def conventions_for              (lookup)
#   def inherit                      (pure merge — family under model-specific)
#   def record_family                (persist KG edges + memory, non-fatal)
# AGENT_HEADER_END -->
"""Family conventions — the middle tier of the three-tier model-knowledge store
(per-model bundle block · family conventions · KG relations).

Rationale (design report "Local inference — model knowledge ingestion + model-
conditioned prompt optimization"): a fresh Pony merge must inherit "score tags
mandatory" WITHOUT re-scraping. Encoding the rule once per family and merging it
under the model-specific block at apply time solves the root problem once, not
once per checkpoint (DP10).

``FAMILY_CONVENTIONS`` is the offline seed for families we have proof for.
Researched-at-ingest conventions (image families pulled from Civitai) will be
persisted via ``record_family`` and layered on top in a later slice; ``inherit``
already accepts an ``extra`` override so that path plugs in without a rewrite.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("okuro.ai_models.family")

# Curated family conventions. Every value is proof-cited in the design report's
# per-modality table; nothing here is guessed. Keys mirror okuro.prompting/v1
# field names so a family block merges structurally with a model block.
FAMILY_CONVENTIONS: dict[str, dict] = {
    # --- Image: booru-tag families --------------------------------------
    "pony": {
        "prompt_syntax": "booru_tags",
        "constraints": {
            "clip_skip": 2,
            "requires_quality_prefix": True,
            "negative_prompt": "weak_effect",
            "no_weight_syntax": False,
        },
        "quality_tags": {
            "prefix": ["score_9", "score_8_up", "score_7_up", "score_6_up"],
            "rating": ["rating_safe", "rating_questionable", "rating_explicit"],
        },
        "negative_defaults": ["score_4", "score_3", "worst quality", "low quality", "blurry"],
        "recommended_params": {
            "cfg": 7.0, "steps": 26, "sampler": "Euler a", "clip_skip": 2,
            "resolution_buckets": ["1024x1024", "832x1216", "1216x832"],
        },
    },
    "illustrious": {
        "prompt_syntax": "booru_tags",
        "constraints": {"requires_quality_prefix": True, "negative_prompt": "strong"},
        "quality_tags": {
            "prefix": ["masterpiece", "best quality", "amazing quality", "very aesthetic", "newest"],
        },
        "negative_defaults": ["worst quality", "low quality", "blurry"],
        "recommended_params": {
            "cfg": 5.5, "steps": 24, "sampler": "Euler a",
            "resolution_buckets": ["1024x1024", "832x1216", "1216x832"],
        },
    },
    "sdxl": {
        "prompt_syntax": "booru_tags",
        "constraints": {"requires_quality_prefix": True, "negative_prompt": "recommended"},
        "quality_tags": {"prefix": ["masterpiece", "best quality"]},
        "negative_defaults": ["worst quality", "low quality", "blurry", "bad anatomy"],
        "recommended_params": {
            "cfg": 6.5, "steps": 28, "sampler": "DPM++ 2M",
            "resolution_buckets": ["1024x1024", "832x1216", "1216x832"],
        },
    },
    "sd15": {
        "prompt_syntax": "booru_tags",
        "constraints": {"requires_quality_prefix": True, "negative_prompt": "recommended"},
        "quality_tags": {"prefix": ["masterpiece", "best quality"]},
        "negative_defaults": ["worst quality", "low quality", "blurry", "bad anatomy", "extra limbs"],
        "recommended_params": {
            "cfg": 7.0, "steps": 25, "sampler": "DPM++ 2M",
            "resolution_buckets": ["512x512", "512x768", "768x512"],
        },
    },
    # --- Image: natural-language family ---------------------------------
    "flux": {
        "prompt_syntax": "natural_language",
        "constraints": {"no_weight_syntax": True, "negative_prompt": "unsupported"},
        "negative_defaults": [],
        "recommended_params": {
            "guidance": 3.5, "cfg": 1.0, "steps": 28, "resolution_buckets": ["1024x1024"],
        },
    },
    # --- LLM: chat-template family (live path — slice 1 tags qwen3) ------
    "qwen3": {
        "prompt_syntax": "chat_template",
        "constraints": {"thinking_mode": True},
        "system_prompt_style": "concise_instruction",
        "recommended_params": {
            "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0,
            "note": "thinking mode; do NOT use greedy decoding — repetition risk",
        },
    },
}

# okuro.prompting/v1 fields that are dicts → merged key-by-key (model wins per
# key). Everything else is a scalar/list → model value wins wholesale.
_DICT_FIELDS = ("constraints", "quality_tags", "recommended_params")


def conventions_for(family: Optional[str]) -> dict:
    """Curated conventions for a family slug, or {} if none are seeded."""
    if not family:
        return {}
    return FAMILY_CONVENTIONS.get(family.lower(), {})


def inherit(block: Optional[dict], *, extra: Optional[dict] = None) -> dict:
    """Merge family conventions UNDER a model-specific prompting block.

    Precedence (low → high): seeded family conventions → ``extra`` (researched
    family conventions, layered by a future slice) → the model block itself.
    Model-specific values always win — inheritance never overrides what the
    checkpoint actually declares. Pure: returns a new dict, mutates nothing.
    A ``provenance`` note records which fields came from the family layer.
    """
    block = dict(block or {})
    conv = conventions_for(block.get("family"))
    if extra:
        conv = _merge(conv, extra)
    if not conv:
        return block

    merged = _merge(conv, block)
    inherited = sorted(k for k in conv if k not in ("provenance",) and k in merged)
    if inherited:
        prov = list(merged.get("provenance", []))
        prov.append({
            "field": ",".join(inherited),
            "source": "okuro:family_conventions",
            "family": block.get("family"),
        })
        merged["provenance"] = prov
    return merged


def _merge(base: dict, over: dict) -> dict:
    """Shallow merge with per-key recursion on the known dict fields.

    ``over`` wins. For a dict field present in both, merge keys (over wins per
    key) so a model can override one param without dropping the family's rest.
    """
    out = dict(base)
    for k, v in over.items():
        if k in _DICT_FIELDS and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def record_family(
    model_id: str,
    family: Optional[str],
    base_model: Optional[str] = None,
    *,
    project: str = "okuro",
) -> None:
    """Persist the relations + family tier at ingest — never fatal.

    - KG edges (the relations tier): ``model -is_a-> family`` and, when known,
      ``model -based_on-> base_model``. These are what an inheritance graph walk
      (``kg_query(model, 'is_a')``) reads.
    - Family convention memory (the family tier): the seeded rule set, written
      once per family. ``write_memory`` dedups on embedding, so repeat ingests
      of the same family are idempotent.

    Any failure (no db, import error) is logged and swallowed — a knowledge
    write must never break a model pull.
    """
    if not family:
        return
    try:
        from okuro.sense.kg import kg_add

        kg_add(model_id, "is_a", family, subject_type="model",
               object_type="family", project=project, confidence=0.95)
        if base_model:
            kg_add(model_id, "based_on", base_model, subject_type="model",
                   object_type="model", project=project, confidence=0.9)
    except Exception as exc:  # pragma: no cover - infra-dependent
        logger.warning("record_family: kg_add failed for %s: %s", model_id, exc)

    conv = conventions_for(family)
    if not conv:
        return
    try:
        from okuro.sense.memory import write_memory

        syntax = conv.get("prompt_syntax", "?")
        write_memory(
            topic="convention",
            content=(
                f"Model family '{family}' prompting convention: prompt_syntax="
                f"{syntax}. Applied at inference by ai_models/optimize.py via "
                f"family.inherit(); model-specific bundle fields override. Seed "
                f"in ai_models/family.py:FAMILY_CONVENTIONS."
            ),
            project=project,
            confidence=0.9,
            source_agent="ai_models.ingest",
        )
    except Exception as exc:  # pragma: no cover - infra-dependent
        logger.warning("record_family: write_memory failed for %s: %s", family, exc)
