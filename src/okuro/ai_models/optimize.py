# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Apply-time model-conditioned prompt optimizer. Given a target model's
#          okuro.prompting/v1 block (built at ingest, family conventions merged
#          in via family.inherit), shape a raw intent into the syntax that model
#          actually speaks — chat_template / booru_tags / natural_language —
#          injecting quality/score prefixes, building the negative, and emitting
#          the tuned params so the invoke call is conditioned on the model.
# index:
#   def optimize_prompt          (pure dispatch — unit-tested)
#   def _optimize_chat / _optimize_booru / _optimize_natural
#   def _to_booru_tags / _strip_weight_syntax / _collapse_ws
#   def resolve_prompting        (thin bundle I/O)
#   def condition_for_model      (resolve → inherit → optimize)
# AGENT_HEADER_END -->
"""Model-conditioned prompt optimization — the apply-time counterpart to
``ingest.py`` (per-model block) and ``family.py`` (inherited conventions).

At invoke time this dispatches on ``prompt_syntax`` and returns a normalized
prompt plan the bridge can feed to a model without hallucinating framing the
model doesn't use. The same intent produces OPPOSITE prompts across families —
Pony gets a mandatory ``score_9`` booru prefix, Flux gets a natural-language
sentence with no negative — driven entirely by the (inherited) knowledge block.

Design invariants (mirror ingest — no fabrication):
- Never invent a system prompt, a stop token, a param, or a tool the block
  doesn't carry.
- Unknown / missing block → treat as ``natural_language`` (assumes nothing
  about templating) and say so in ``notes``.
- Pure over a plain dict so it is unit-testable without a real bundle. Family
  inheritance is applied by ``condition_for_model`` (or the caller) BEFORE
  dispatch, keeping this dispatcher a pure function of the block it is given.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

NATURAL_LANGUAGE = "natural_language"
CHAT_TEMPLATE = "chat_template"
BOORU_TAGS = "booru_tags"


def optimize_prompt(
    intent: str,
    prompting: Optional[dict],
    *,
    system: Optional[str] = None,
) -> dict:
    """Shape ``intent`` for the model described by ``prompting``.

    ``prompting`` is an ``okuro.prompting/v1`` block (family conventions already
    merged in) or None when the bundle was never ingested. Returns a plan dict:
        syntax:      the resolved prompt_syntax actually used
        prompt:      user-role content, ready for invoke(prompt=...)
        system:      system-role content, or None (never fabricated)
        stop:        stop tokens from the block (may be empty)
        negative:    negative prompt (image families), else ""
        params:      model-conditioned generation params (cfg/steps/temp/…)
        constraints: echo of block constraints
        notes:       advisories for the caller (fallbacks, dropped tools, …)
    """
    block = prompting or {}
    syntax = block.get("prompt_syntax") or NATURAL_LANGUAGE
    notes: list[str] = []
    if not prompting:
        notes.append("no prompting block; treated as natural_language")

    if syntax == CHAT_TEMPLATE:
        return _optimize_chat(intent, block, system, notes)
    if syntax == BOORU_TAGS:
        return _optimize_booru(intent, block, notes)
    if syntax != NATURAL_LANGUAGE:
        notes.append(f"unknown prompt_syntax {syntax!r}; treated as natural_language")
    return _optimize_natural(intent, block, system, notes)


def _plan(syntax: str, block: dict, notes: list[str], **over) -> dict:
    """Assemble a plan dict with the common fields, overridable per branch."""
    plan = {
        "syntax": syntax,
        "prompt": "",
        "system": None,
        "stop": list(block.get("stop_tokens", []) or []),
        "negative": "",
        "params": dict(block.get("recommended_params", {}) or {}),
        "constraints": dict(block.get("constraints", {}) or {}),
        "notes": notes,
    }
    plan.update(over)
    return plan


def _optimize_chat(intent: str, block: dict, system: Optional[str], notes: list[str]) -> dict:
    """Chat-templated LLM. The server applies the embedded chat template, so we
    do NOT re-wrap; we surface the levers a raw invoke would miss — stop tokens,
    tool support, sampling params — and flag the family's system-prompt style."""
    constraints = block.get("constraints", {}) or {}
    notes.append("server applies embedded chat template; pass stop + params through")
    if constraints.get("supports_tools") is False:
        notes.append("model does not advertise tool-calling; drop tools from the call")
    if system is None and block.get("system_prompt_style"):
        notes.append(f"family system_prompt_style={block['system_prompt_style']!r}")
    return _plan(CHAT_TEMPLATE, block, notes, prompt=intent.strip(), system=system)


def _optimize_booru(intent: str, block: dict, notes: list[str]) -> dict:
    """Booru-tag image model (Pony / Illustrious / SDXL). Build the tag list in
    the order the family demands: quality/score prefix → rating → user tags.
    Image models ignore a system role, so we emit none; the negative is built
    from the family's ``negative_defaults``."""
    quality = block.get("quality_tags", {}) or {}
    prefix = list(quality.get("prefix", []) or [])
    rating = quality.get("rating") or []
    triggers = list(block.get("trigger_words", []) or [])
    lead = prefix + (rating[:1] if rating else []) + triggers

    seen = {t.lower() for t in lead}
    user_tags = [t for t in _to_booru_tags(intent) if t not in seen]
    tags = lead + user_tags
    if prefix:
        notes.append(f"prepended {len(prefix)} mandatory {block.get('family', 'family')} quality tags")
    if triggers:
        notes.append(f"injected {len(triggers)} checkpoint trigger word(s)")

    return _plan(
        BOORU_TAGS, block, notes,
        prompt=", ".join(tags),
        negative=", ".join(block.get("negative_defaults", []) or []),
    )


def _optimize_natural(intent: str, block: dict, system: Optional[str], notes: list[str]) -> dict:
    """Plain natural-language model (Flux, base LLM, some TTS). No chat framing;
    strip weight/booru syntax the model rejects; emit any family params."""
    text = _collapse_ws(intent)
    if block.get("constraints", {}).get("no_weight_syntax"):
        stripped = _strip_weight_syntax(text)
        if stripped != text:
            notes.append("stripped weight/booru syntax (model is natural-language only)")
        text = stripped
    triggers = list(block.get("trigger_words", []) or [])
    if triggers:
        text = f"{', '.join(triggers)}. {text}" if text else ", ".join(triggers)
        notes.append(f"injected {len(triggers)} checkpoint trigger word(s)")
    return _plan(
        NATURAL_LANGUAGE, block, notes,
        prompt=text,
        system=system,
        negative=", ".join(block.get("negative_defaults", []) or []),
    )


_WS = re.compile(r"\s+")
# (a:1.2) / [tag] emphasis weights and standalone :1.3 multipliers — booru/A1111
# syntax that natural-language models (Flux) reject.
_WEIGHT = re.compile(r"[()\[\]]|:\s*\d+(?:\.\d+)?")


def _collapse_ws(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip."""
    return _WS.sub(" ", text).strip()


def _strip_weight_syntax(text: str) -> str:
    """Remove A1111/booru emphasis weights so prose reaches a natural-lang model."""
    return _collapse_ws(_WEIGHT.sub(" ", text))


def _to_booru_tags(text: str) -> list[str]:
    """Split free text into ordered, deduped, lowercased booru tags.

    Splits on commas and newlines (the natural tag separators). Each fragment
    is whitespace-collapsed; empties dropped; order preserved on first sight so
    emphasis order from the prompt survives.
    """
    seen: set[str] = set()
    tags: list[str] = []
    for frag in re.split(r"[,\n]", text):
        tag = _collapse_ws(frag).lower()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def to_invoke_params(plan: dict) -> dict:
    """Map an optimize plan → kwargs for bridge.local.invoke_local.

    Extracts only the LLM-relevant sampling levers the OpenAI seam accepts:
    ``temperature`` (top-level kwarg), ``stop`` (sequences), and a ``sampling``
    dict of top_p/top_k/min_p. Image params (cfg/steps/…) are intentionally
    dropped — they belong to a diffusion pipeline, not a chat completion. Only
    present keys are returned, so this composes cleanly with defaults.
    """
    params = plan.get("params", {}) or {}
    out: dict = {}
    if params.get("temperature") is not None:
        out["temperature"] = params["temperature"]
    sampling = {k: params[k] for k in ("top_p", "top_k", "min_p") if params.get(k) is not None}
    if sampling:
        out["sampling"] = sampling
    if plan.get("stop"):
        out["stop"] = list(plan["stop"])
    return out


def resolve_prompting(model_id: str, *, root: "str | Path | None" = None) -> Optional[dict]:
    """Load the prompting block for a bundle id, or None if absent/not ingested.

    Thin I/O over the bundle store — kept out of the pure core so callers can
    optimize against an in-memory block in tests without touching disk.
    """
    from okuro.ai_models.bundle import bundles_root, load_bundle

    base = Path(root) if root is not None else bundles_root()
    bundle = load_bundle(base / model_id)
    return bundle.prompting if bundle else None


def condition_for_model(
    intent: str,
    model_id: str,
    *,
    root: "str | Path | None" = None,
    system: Optional[str] = None,
) -> dict:
    """Resolve ``model_id``'s block, inherit family conventions, then optimize.

    The full apply path: raw intent + model id → conditioned prompt plan. Falls
    back to natural_language when the bundle was never ingested (block None).
    """
    from okuro.ai_models.family import inherit

    block = resolve_prompting(model_id, root=root)
    return optimize_prompt(intent, inherit(block) if block else None, system=system)
