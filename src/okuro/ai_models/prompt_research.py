# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Install-time prompting research — when a model is ingested and its
#          family has no curated conventions, ask an LLM for that model's
#          prompting conventions (quality/score tags, negatives, params) and
#          merge them INTO the model's okuro.prompting/v1 block, so everything
#          downstream (resolve_prompting -> family.inherit -> optimize) applies
#          them at generation with no further research. Realises the "prompting
#          knowledge updated on model install" contract (one model-prompting-
#          expert role + knowledge-as-data). Best-effort + non-fatal + opt-in
#          (an LLM call per install has a cost — only runs for unknown families).
# index:
#   research_convention   (LLM -> partial convention dict; {} on failure)
#   enrich_prompting_block (merge researched conventions into a block)
# AGENT_HEADER_END -->
"""Install-time prompting-convention research.

The model-prompting-expert role's knowledge is DATA: family conventions
(``family.py``) + the per-model ingested block. For a model whose family okuro
already knows (Pony/SDXL/Flux/…) the curated conventions suffice — no research.
For an UNKNOWN family, this asks an LLM once, at install, for the model's
prompting conventions and bakes them into the saved block. Injectable invoke so
it is testable without an LLM; returns the block unchanged on any failure.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger("okuro.ai_models.prompt_research")

_SYSTEM = (
    "You are a diffusion-model prompting expert. Given a model, output ONLY a JSON "
    "object with its prompting conventions. Keys (all optional): "
    "prompt_syntax ('booru_tags'|'natural_language'), "
    "quality_tags {prefix:[...], rating:[...]}, negative_defaults:[...], "
    "recommended_params {cfg:number, steps:int, sampler:string}, notes:string. "
    "Base it on what the model was trained on. No prose, JSON only."
)


def _default_invoke(prompt: str, system: Optional[str] = None) -> str:
    from okuro.bridge.invoke import invoke
    r = invoke(prompt, system_prompt=system, role="model-prompting-expert",
               temperature=0.2, response_format="json")
    return r.get("output", "") if r.get("success") else ""


def _parse(text: str) -> dict:
    """Extract a JSON object from LLM output (tolerant of surrounding prose)."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


_ALLOWED = ("prompt_syntax", "quality_tags", "negative_defaults",
            "recommended_params", "constraints", "notes")


def research_convention(
    name: str,
    family: Optional[str],
    base_model: Optional[str],
    *,
    invoke: Optional[Callable[..., str]] = None,
) -> dict:
    """Research a model's prompting conventions via an LLM. Returns a partial
    convention dict (subset of :data:`_ALLOWED`) or ``{}`` on any failure."""
    inv = invoke or _default_invoke
    q = (f"Model: {name or '(unknown)'}\nFamily: {family or '(unknown)'}\n"
         f"Base model: {base_model or '(unknown)'}\n"
         "Give this model's prompting conventions as JSON.")
    try:
        conv = _parse(inv(q, _SYSTEM))
    except Exception as exc:  # pragma: no cover - invoke is best-effort
        logger.warning("prompt_research: invoke failed for %s: %s", name, exc)
        return {}
    if not isinstance(conv, dict):
        return {}
    return {k: v for k, v in conv.items() if k in _ALLOWED and v}


def enrich_prompting_block(
    block: dict,
    *,
    name: Optional[str] = None,
    invoke: Optional[Callable[..., str]] = None,
    force: bool = False,
) -> dict:
    """Merge researched conventions into a prompting ``block`` when its family
    has no curated conventions (or ``force``). Non-fatal — returns ``block``
    unchanged when the family is already known, research fails, or yields nothing.
    """
    from okuro.ai_models.family import conventions_for

    family = block.get("family")
    if not force and conventions_for(family):
        return block  # curated family conventions already cover it — no LLM cost
    conv = research_convention(
        name or block.get("name") or family or "", family,
        block.get("base_model"), invoke=invoke)
    if not conv:
        return block
    merged = dict(block)
    for k, v in conv.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    prov = list(merged.get("provenance", []))
    prov.append({"field": ",".join(sorted(conv)),
                 "source": "okuro:llm_research", "confidence": 0.6})
    merged["provenance"] = prov
    logger.info("prompt_research: enriched %s with %s", name or family, sorted(conv))
    return merged
