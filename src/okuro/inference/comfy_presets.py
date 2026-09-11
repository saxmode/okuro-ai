# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Preset/style catalog — the curation layer that makes okuro's Studio
#          OUTCOME-based instead of model-based. A user picks "Photo" or "Anime",
#          NOT "RealVisXL" or "a text-to-image workflow". A preset resolves an
#          outcome -> (modality, model family, task, quality-tier params, style
#          prompt conventions), so the Studio never exposes checkpoints, graphs,
#          samplers, or negatives. This is DATA (curated seed + register()), like
#          family conventions — the keystone the Studio UI/REST builds on.
# index:
#   Preset / TIER_DEFAULTS / PRESETS (curated seed)
#   list_presets / get_preset / register
#   resolve (preset+tier -> spec)  /  apply_style (spec -> prompt/negative)
# AGENT_HEADER_END -->
"""Outcome/style preset catalog for okuro's generation Studio.

The value of okuro here is radical simplicity (DP02): the user selects what they
want to MAKE, not which model runs it. A ``Preset`` is the curated mapping from a
human outcome ("Photo", "Illustration", "Anime", "Logo", "Cinematic") to
everything the generation stack needs — family (graph topology + prompt dialect),
task, quality-tier parameters, and style prompt conventions layered on top of the
model's own. Quality is chosen as ``fast|balanced|high``, never as steps/CFG.

Presets are data: a curated seed plus ``register()`` for extension. Resolution is
pure and testable; the Studio REST layer binds a preset's family to a concrete
installed model and feeds ``resolve()`` output into the generation facade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"

# Quality tiers in human terms → generation params. A preset may override any
# key per tier. Users pick fast/balanced/high; they never see these numbers.
TIER_DEFAULTS: dict[str, dict] = {
    "fast": {"steps": 8, "cfg": 2.0, "sampler": "Euler a", "resolution_buckets": ["768x768"]},
    "balanced": {"steps": 24, "cfg": 6.0, "sampler": "DPM++ 2M", "resolution_buckets": ["1024x1024"]},
    "high": {"steps": 36, "cfg": 6.5, "sampler": "DPM++ 2M", "resolution_buckets": ["1152x1152"]},
}
_DEFAULT_TIER = "balanced"


# Generation engines. The engine decides WHERE the pixels come from; everything
# above it (outcome, style, tier) is identical either way — which is what lets a
# GPU-less box use the same Studio.
#
# ENGINE LIVES ON THE CHOICE, NOT THE PRESET. A style is an OUTCOME ("Photo"),
# and outcomes are engine-agnostic: the same photoreal result can come from a
# local checkpoint or a cloud provider. Putting the engine on the Preset forced a
# second near-duplicate style per engine ("Photo" vs a cloud "Scene"), which made
# the user pick an implementation detail wearing an outcome's name — exactly what
# the outcome-first principle forbids. Choices already spanned model FAMILIES;
# spanning engines is the same idea one level out.
ENGINE_LOCAL = "comfy"   # okuro's own ComfyUI — GPU + edition gated, models on disk
ENGINE_CLOUD = "bridge"  # a bridge media provider (agy) — needs an authed CLI, no GPU

# Cloud choices need no booru tags, quality prefixes or negatives.
CLOUD_FAMILY = "natural"


@dataclass
class ModelChoice:
    """A curated model option for a style — one entry in the per-preset picker.

    Curation is DATA: each preset ranks a few models (possibly across families
    AND engines) with a plain-language rationale, so the Studio can *suggest* the
    right one per output style instead of silently auto-picking. This layer holds
    intent only — concrete availability is resolved at runtime by
    ``gen_tools.candidates_for_preset`` (bundles on disk for local choices, CLI
    install+auth for cloud ones). Progressive disclosure (DP02): the default flow
    never shows this; a power user opens the panel to see and switch.
    """

    family: str                 # graph topology + prompt dialect this choice runs on
    label: str                  # human name shown in the picker ("FLUX.1-dev")
    rationale: str              # why THIS model suits THIS style (one line)
    model_hint: str = ""        # preferred bundle name, fuzzy-matched to installed
    recommended: bool = False   # okuro's default pick for this style
    engine: str = ENGINE_LOCAL  # which engine renders this choice


def cloud_choice(rationale: str, *, recommended: bool = False) -> ModelChoice:
    """The bridge/agy option, as offered under any style."""
    return ModelChoice(CLOUD_FAMILY, "Cloud (Antigravity)", rationale,
                       recommended=recommended, engine=ENGINE_CLOUD)


@dataclass
class Preset:
    """A user-facing outcome/style and what it resolves to under the hood."""

    id: str
    label: str
    description: str
    modality: str          # image | video | audio
    icon: str              # emoji shown in the picker
    family: str            # default family when a choice doesn't override it
    model_hint: Optional[str] = None   # preferred model (for setup/download); not user-visible
    task: str = "text-to-image"
    style_positive: str = ""           # style intent added to the user's prompt
    style_negative: str = ""           # style-level negatives (merged with the family's)
    default_tier: str = _DEFAULT_TIER
    tier_overrides: dict = field(default_factory=dict)  # {tier: {param overrides}}
    choices: list[ModelChoice] = field(default_factory=list)  # curated per-style model picker

    @property
    def engines(self) -> set[str]:
        """Engines that can render this style (from its choices)."""
        return {c.engine for c in self.choices} or {ENGINE_LOCAL}

    @property
    def default_engine(self) -> str:
        """Engine to assume when no choice is bound.

        Deterministic on purpose: ``next(iter(self.engines))`` reads from a SET,
        so a style offering both engines would pick a different one run to run —
        which silently routed a local generation through the cloud once.
        """
        return ENGINE_LOCAL if ENGINE_LOCAL in self.engines else ENGINE_CLOUD

    @property
    def needs_gpu(self) -> bool:
        """True when every way of rendering this style requires local inference."""
        return ENGINE_CLOUD not in self.engines


# Curated seed. Families reference okuro's existing graph builders + family
# conventions; model_hint guides the first download but is not load-bearing.
PRESETS: dict[str, Preset] = {
    "photo": Preset(
        id="photo", label="Photo", description="Photorealistic images", modality=IMAGE,
        icon="📷", family="sdxl", model_hint="RealVisXL",
        style_positive="photorealistic, detailed, natural lighting",
        style_negative="cartoon, anime, illustration, 3d render",
        choices=[
            ModelChoice("sdxl", "RealVisXL", "Sharpest photoreal skin & texture; fast on your GPUs",
                        model_hint="RealVisXL", recommended=True),
            ModelChoice("flux", "FLUX.1-dev", "Best prompt-adherence, coherent hands & signage; slower, ~24GB VRAM",
                        model_hint="FLUX.1-dev"),
            cloud_choice("Strong photoreal with no GPU and nothing to download; daily cap"),
        ]),
    "illustration": Preset(
        id="illustration", label="Illustration", description="Digital painting & illustration",
        modality=IMAGE, icon="🎨", family="sdxl", model_hint="DreamShaperXL",
        style_positive="digital illustration, painterly, concept art",
        style_negative="photo, photograph",
        choices=[
            ModelChoice("sdxl", "DreamShaperXL", "Versatile painterly concept-art look",
                        model_hint="DreamShaper", recommended=True),
            ModelChoice("flux", "FLUX.1-dev", "Cleaner composition from natural-language briefs",
                        model_hint="FLUX.1-dev"),
            cloud_choice("Plain-language briefs, no GPU; daily cap"),
        ]),
    "anime": Preset(
        id="anime", label="Anime", description="Anime & manga style", modality=IMAGE,
        icon="✨", family="illustrious", model_hint="Illustrious",
        style_positive="anime style", style_negative="photo, realistic, 3d",
        choices=[
            ModelChoice("illustrious", "Illustrious", "Booru-native; strongest anime coherence & tags",
                        model_hint="Illustrious", recommended=True),
            ModelChoice("pony", "Pony Diffusion XL", "Fine character & pose control via score tags",
                        model_hint="Pony"),
            cloud_choice("No GPU needed, but weaker anime coherence than the booru models"),
        ]),
    "logo": Preset(
        id="logo", label="Logo", description="Clean vector-style logos & marks", modality=IMAGE,
        icon="🏷️", family="sdxl", model_hint="SDXL",
        style_positive="vector logo, flat design, minimal, clean, centered",
        style_negative="photo, realistic, texture, busy, gradient noise",
        tier_overrides={"balanced": {"resolution_buckets": ["1024x1024"]}},
        choices=[
            ModelChoice("sdxl", "SDXL base", "Flat, clean marks without photo over-detailing",
                        model_hint="SDXL", recommended=True),
            ModelChoice("flux", "FLUX.1-schnell", "Sharper, legible text inside the mark",
                        model_hint="FLUX.1-schnell"),
            cloud_choice("Best legible lettering inside a mark; no GPU, daily cap"),
        ]),
    "cinematic": Preset(
        id="cinematic", label="Cinematic", description="Film-still, natural-language prompts",
        modality=IMAGE, icon="🎬", family="flux", model_hint="FLUX.1-schnell",
        style_positive="cinematic lighting, film still, shallow depth of field",
        default_tier="fast",
        choices=[
            ModelChoice("flux", "FLUX.1-schnell", "Natural-language film prompts, fast turnaround",
                        model_hint="FLUX.1-schnell", recommended=True),
            ModelChoice("sdxl", "Juggernaut XL", "Filmic color grade with tight parameter control",
                        model_hint="Juggernaut"),
            cloud_choice("Filmic look with no GPU; no seed or sampler control"),
        ]),
    # "poster" is cloud-ONLY, and that is a statement about capability, not about
    # engines: the cloud model renders legible, correctly-kerned type (verified —
    # a headline with a correct German umlaut), which no local SDXL/Flux
    # checkpoint reliably does. Offering a local choice here would be offering a
    # worse result under the same promise. Contrast the deleted "scene" preset,
    # which was photoreal-via-cloud and therefore just "photo" with a different
    # renderer — that one was engine leakage and is now a choice under "photo".
    #
    # style_positive fights a measured failure mode: without it the model renders
    # a PHOTOGRAPH of a poster hanging on a wall (paper edges, drop shadow) rather
    # than the artwork. Fixed, verified across live runs. The separate invented-
    # text failure is handled by bridge.media.text_constraint via literal_text.
    "poster": Preset(
        id="poster", label="Poster", description="Posters & covers with real, legible text",
        modality=IMAGE, icon="🖼️", family=CLOUD_FAMILY,
        style_positive=("graphic poster artwork as a flat full-bleed image filling the "
                        "entire frame, not a photograph of a printed poster, no wall, "
                        "no mockup, no drop shadow, no paper edges"),
        default_tier="balanced",
        tier_overrides={
            "fast": {"resolution_buckets": ["1024x1024"]},
            "balanced": {"resolution_buckets": ["1024x1024"]},
            "high": {"resolution_buckets": ["1376x768"]},
        },
        choices=[
            cloud_choice("Renders real, legible type — local checkpoints garble it",
                         recommended=True),
        ]),
}


def list_presets(modality: Optional[str] = None) -> list[Preset]:
    """All presets, optionally filtered by modality (for the Studio picker)."""
    ps = list(PRESETS.values())
    return [p for p in ps if p.modality == modality] if modality else ps


def get_preset(preset_id: str) -> Optional[Preset]:
    return PRESETS.get((preset_id or "").lower())


def list_choices(preset_id: str) -> list[ModelChoice]:
    """Curated model options for a preset (empty if none/unknown). The runtime
    availability + concrete model id are layered on by ``gen_tools``."""
    p = get_preset(preset_id)
    return list(p.choices) if p else []


def register(preset: Preset) -> None:
    """Add/override a preset (extension seam — orchestrator-curated styles)."""
    PRESETS[preset.id.lower()] = preset


def resolve(preset_id: str, *, tier: Optional[str] = None,
            choice: Optional[ModelChoice] = None) -> dict:
    """Resolve a preset (+ quality tier) to a generation spec.

    Returns ``{preset, modality, family, task, model_hint, tier, params, engine,
    style_positive, style_negative}``. ``params`` is the tier default merged with
    the preset's per-tier overrides. Raises ``KeyError`` for an unknown preset.

    ``choice`` binds the resolution to one option: its engine and family win, so
    picking the cloud option under "Photo" yields the natural-language family
    rather than sdxl. Without it, the preset's own defaults apply.

    Note when the resolved engine is cloud: the sampler/steps/cfg in ``params``
    are meaningless to a bridge provider, which exposes no such controls — only
    the resolution bucket carries over (as an aspect ratio). The tier vocabulary
    stays shared so the UI needs no engine-specific branch.
    """
    p = get_preset(preset_id)
    if p is None:
        raise KeyError(f"unknown preset {preset_id!r}")
    t = (tier or p.default_tier)
    if t not in TIER_DEFAULTS:
        t = p.default_tier
    params = {**TIER_DEFAULTS.get(t, {}), **(p.tier_overrides.get(t, {}))}
    return {
        "preset": p.id, "modality": p.modality,
        "family": choice.family if choice else p.family,
        "task": p.task,
        "model_hint": p.model_hint, "tier": t, "params": params,
        "style_positive": p.style_positive, "style_negative": p.style_negative,
        "engine": choice.engine if choice else p.default_engine,
    }


def apply_style(spec: dict, prompt: str, negative: str = "") -> tuple[str, str]:
    """Layer a preset's style onto a raw user prompt + negative.

    The style intent is appended to the user's prompt BEFORE model-conditioned
    optimisation (so the model-prompting-expert renders it in the model's dialect),
    and the style negatives are merged. Pure.
    """
    pos = prompt.strip()
    if spec.get("style_positive"):
        pos = f"{pos}, {spec['style_positive']}" if pos else spec["style_positive"]
    negs = [n.strip() for n in (negative, spec.get("style_negative", "")) if n and n.strip()]
    return pos, ", ".join(negs)
