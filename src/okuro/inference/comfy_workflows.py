# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Plan -> ComfyUI API-format workflow graph. Turns an optimizer plan
#          (prompt/negative/params + a checkpoint filename) into the node-graph
#          JSON ComfyUI's POST /prompt consumes. Family-keyed builders because
#          the graph topology differs by family: booru/SDXL uses cfg + a real
#          negative; Flux uses FluxGuidance (no cfg) + an empty negative + the
#          SD3 latent. Video families are registered as an explicit unsupported
#          seam so the runtime can execute a video graph the day the upstream
#          video model-knowledge pipeline (family/ingest/optimize) exists — no
#          refactor here, just a builder registration.
# index:
#   ComfyWorkflowUnsupported                  (raised for un-built families)
#   IMAGE_FAMILIES / VIDEO_FAMILIES
#   def build_workflow                        (family -> graph JSON)
#   def register / family_supported           (extension seam)
# AGENT_HEADER_END -->
"""Plan → ComfyUI API-format workflow graph.

The ComfyUI ``POST /prompt`` body is ``{"prompt": <graph>, "client_id": ...}``
where ``<graph>`` maps ``node_id -> {"class_type", "inputs"}`` and an input that
references another node is ``[node_id, output_slot]``. This module is the pure
mapper from an ``ai_models.optimize`` plan to that graph; the adapter
(``inference.comfy``) owns the HTTP.

Topologies differ by family (proof: the FLUX vs SDXL graph difference is real —
FLUX has no CFG-scale negative and takes guidance via a FluxGuidance node):

    booru / sdxl / sd15 / pony / illustrious
        CheckpointLoaderSimple → CLIPTextEncode ×2 (pos, neg)
        → KSampler(cfg,steps,sampler) → VAEDecode → SaveImage
    flux
        CheckpointLoaderSimple → CLIPTextEncode(pos) → FluxGuidance
        → CLIPTextEncode(empty neg) → KSampler(cfg=1.0) on an SD3 latent
        → VAEDecode → SaveImage

All-in-one checkpoints (Civitai/HF single-file safetensors) load through
``CheckpointLoaderSimple``, which is how okuro's bundle store lays them out.
"""

from __future__ import annotations

from typing import Callable, Optional

Graph = dict[str, dict]
Builder = Callable[..., Graph]


class ComfyWorkflowUnsupported(RuntimeError):
    """No workflow builder is registered for this family (e.g. video, today)."""


# Families whose builders exist now. Booru families all share the SDXL topology.
IMAGE_FAMILIES = ("sdxl", "pony", "illustrious", "sd15", "flux")
# Named but not yet buildable — upstream plan producer (family/ingest/optimize)
# does not emit video plans yet. Registered for a clear error, not a crash.
VIDEO_FAMILIES = ("wan", "wan2", "svd", "animatediff", "hunyuan", "ltx", "mochi", "cogvideo")


# A1111/family sampler label → (ComfyUI sampler_name, scheduler). Family params
# carry human sampler names; ComfyUI wants its own ids.
_SAMPLER_MAP: dict[str, tuple[str, str]] = {
    "euler": ("euler", "normal"),
    "euler a": ("euler_ancestral", "normal"),
    "euler ancestral": ("euler_ancestral", "normal"),
    "heun": ("heun", "normal"),
    "dpm++ 2m": ("dpmpp_2m", "karras"),
    "dpm++ 2m karras": ("dpmpp_2m", "karras"),
    "dpm++ 2m sde": ("dpmpp_2m_sde", "karras"),
    "dpm++ sde": ("dpmpp_sde", "karras"),
    "dpm++ 3m sde": ("dpmpp_3m_sde", "karras"),
    "unipc": ("uni_pc", "normal"),
    "ddim": ("ddim", "normal"),
    "lms": ("lms", "normal"),
}
_DEFAULT_SAMPLER = ("euler", "normal")


def _map_sampler(name: Optional[str]) -> tuple[str, str]:
    return _SAMPLER_MAP.get(str(name or "").strip().lower(), _DEFAULT_SAMPLER)


def _resolution(params: dict, width: Optional[int], height: Optional[int]) -> tuple[int, int]:
    """Resolve WxH: explicit args win, else the family's first resolution bucket,
    else a safe 1024² (SDXL-native)."""
    if width and height:
        return int(width), int(height)
    buckets = params.get("resolution_buckets") or []
    if buckets:
        try:
            w, h = str(buckets[0]).lower().split("x")
            return int(w), int(h)
        except (ValueError, IndexError):
            pass
    return 1024, 1024


def _sdxl_graph(plan: dict, *, ckpt_name: str, seed: int, width: Optional[int],
                height: Optional[int], batch_size: int, filename_prefix: str) -> Graph:
    params = plan.get("params") or {}
    w, h = _resolution(params, width, height)
    sampler_name, scheduler = _map_sampler(params.get("sampler"))
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": plan.get("prompt", ""), "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": plan.get("negative", ""), "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": batch_size}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed,
            "steps": int(params.get("steps", 28)),
            "cfg": float(params.get("cfg", 6.5)),
            "sampler_name": sampler_name,
            "scheduler": scheduler,
            "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": filename_prefix}},
    }


def _flux_graph(plan: dict, *, ckpt_name: str, seed: int, width: Optional[int],
                height: Optional[int], batch_size: int, filename_prefix: str) -> Graph:
    params = plan.get("params") or {}
    w, h = _resolution(params, width, height)
    sampler_name, scheduler = _map_sampler(params.get("sampler"))
    # Flux: guidance replaces CFG; the KSampler runs at cfg=1.0 with an empty
    # negative. SD3 latent (EmptySD3LatentImage) is the flux-family latent.
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": plan.get("prompt", ""), "clip": ["4", 1]}},
        "25": {"class_type": "FluxGuidance", "inputs": {
            "conditioning": ["6", 0], "guidance": float(params.get("guidance", 3.5))}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": batch_size}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed,
            "steps": int(params.get("steps", 28)),
            "cfg": 1.0,
            "sampler_name": sampler_name,
            "scheduler": scheduler,
            "denoise": 1.0,
            "model": ["4", 0], "positive": ["25", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": filename_prefix}},
    }


def _flux_multifile_graph(plan: dict, *, components: dict, seed: int,
                          width: Optional[int], height: Optional[int],
                          batch_size: int, filename_prefix: str) -> Graph:
    """Flux from SEPARATE component files (UNET + dual CLIP + VAE).

    Flux.1 dev/schnell and SD3.5 ship the diffusion model, the two text encoders
    (clip_l + t5xxl), and the VAE as distinct files — NOT an all-in-one
    checkpoint. That needs UNETLoader + DualCLIPLoader + VAELoader instead of
    CheckpointLoaderSimple (the V4 fix: resolve_model links all components and
    hands them here). ``components`` = ``{unet, clip:[clip_l, t5xxl], vae}``.
    """
    params = plan.get("params") or {}
    w, h = _resolution(params, width, height)
    sampler_name, scheduler = _map_sampler(params.get("sampler"))
    clips = components.get("clip") or []
    clip1 = clips[0] if len(clips) > 0 else ""
    clip2 = clips[1] if len(clips) > 1 else clip1
    return {
        "10": {"class_type": "UNETLoader", "inputs": {
            "unet_name": components["unet"], "weight_dtype": "default"}},
        "11": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": clip1, "clip_name2": clip2, "type": "flux"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": components["vae"]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": plan.get("prompt", ""), "clip": ["11", 0]}},
        "25": {"class_type": "FluxGuidance", "inputs": {
            "conditioning": ["6", 0], "guidance": float(params.get("guidance", 3.5))}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["11", 0]}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": batch_size}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed,
            "steps": int(params.get("steps", 20)),
            "cfg": 1.0,
            "sampler_name": sampler_name,
            "scheduler": scheduler,
            "denoise": 1.0,
            "model": ["10", 0], "positive": ["25", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["12", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": filename_prefix}},
    }


# family slug → all-in-one checkpoint builder. Booru families share SDXL topology.
_BUILDERS: dict[str, Builder] = {
    "sdxl": _sdxl_graph,
    "pony": _sdxl_graph,
    "illustrious": _sdxl_graph,
    "sd15": _sdxl_graph,
    "flux": _flux_graph,
}

# family slug → multi-file (component) builder. Only families that ship split
# weights need one; today just flux/sd3.5 (SD3.5 shares the flux component graph).
_MULTIFILE_BUILDERS: dict[str, Builder] = {
    "flux": _flux_multifile_graph,
    "sd3": _flux_multifile_graph,
    "sd35": _flux_multifile_graph,
}


def register(family: str, builder: Builder) -> None:
    """Register/override a family's workflow builder (the video seam plugs in here)."""
    _BUILDERS[family.lower()] = builder


def family_supported(family: Optional[str]) -> bool:
    return str(family or "").lower() in _BUILDERS


def build_workflow(
    family: Optional[str],
    plan: dict,
    *,
    ckpt_name: str,
    seed: int,
    width: Optional[int] = None,
    height: Optional[int] = None,
    batch_size: int = 1,
    filename_prefix: str = "okuro",
) -> Graph:
    """Build a ComfyUI API-format graph for ``family`` from an optimizer ``plan``.

    ``ckpt_name`` is the checkpoint filename as ComfyUI addresses it (relative to
    its ``checkpoints`` model dir). Raises :class:`ComfyWorkflowUnsupported` for
    a family with no builder — with a pointed message for known video families.
    """
    fam = str(family or "").lower()
    builder = _BUILDERS.get(fam)
    if builder is None:
        # An unknown *image* plan still generates — fall back to the SDXL
        # topology (a generic checkpoint graph) rather than failing the user.
        if fam in VIDEO_FAMILIES:
            raise ComfyWorkflowUnsupported(
                f"video family {fam!r}: okuro has no video plan producer yet "
                f"(family/ingest/optimize are image-only). The ComfyUI runtime "
                f"can execute a video graph once that upstream pipeline exists."
            )
        builder = _sdxl_graph
    return builder(
        plan, ckpt_name=ckpt_name, seed=seed, width=width, height=height,
        batch_size=batch_size, filename_prefix=filename_prefix,
    )


def multifile_supported(family: Optional[str]) -> bool:
    return str(family or "").lower() in _MULTIFILE_BUILDERS


def build_multifile_workflow(
    family: Optional[str],
    plan: dict,
    *,
    components: dict,
    seed: int,
    width: Optional[int] = None,
    height: Optional[int] = None,
    batch_size: int = 1,
    filename_prefix: str = "okuro",
) -> Graph:
    """Build a graph for a model whose weights are SPLIT across component files.

    ``components`` = ``{unet, clip:[...], vae}`` (as ``gen_tools.resolve_model``
    returns for a multi-file bundle). Raises :class:`ComfyWorkflowUnsupported`
    for a family with no multi-file builder — callers gate on
    :func:`multifile_supported` first.
    """
    fam = str(family or "").lower()
    builder = _MULTIFILE_BUILDERS.get(fam)
    if builder is None:
        raise ComfyWorkflowUnsupported(
            f"no multi-file workflow builder for family {fam!r}; okuro linked "
            f"the components but cannot assemble a graph for a split-weight "
            f"model of this family yet")
    return builder(
        plan, components=components, seed=seed, width=width, height=height,
        batch_size=batch_size, filename_prefix=filename_prefix,
    )
