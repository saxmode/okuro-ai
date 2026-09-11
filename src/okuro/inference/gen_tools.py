# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: MCP/CLI-facing thin layer over the generation stack. Constructs the
#          default GenerationService (Broker + ComfyAdapter + workflow registry),
#          runs a generation, and persists the result to the generations dir —
#          so sense/mcp_tools.py just delegates + formats (mirrors how the
#          resonance tools delegate to okuro.resonance). Applies family prompting
#          conventions even when the checkpoint isn't an ingested okuro bundle,
#          so the tool is useful with just (prompt, ckpt, family).
# index:
#   def _default_service / _optimize_for / _output_dir
#   def run_generation      (prompt -> saved image paths + meta)
#   def workflow_status     (readiness check)
# AGENT_HEADER_END -->
"""Thin service layer the ComfyUI MCP tools delegate to.

Endpoint config: the adapter discovers a live ComfyUI from
``OKURO_COMFYUI_ENDPOINTS`` (comma-separated), else launches one. On the reference host
that is ``http://127.0.0.1:13003`` (the containerized ComfyUI). Output lands in
``OKURO_GENERATIONS_DIR`` (default ``~/.okuro/generations``) — a stand-in until
the media-library filestore exists.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("okuro.inference.gen_tools")

from okuro.inference.comfy import ComfyAdapter
from okuro.inference.comfy_registry import GenerativeWorkflowRegistry
from okuro.inference.generation import GenerationRequest, GenerationService
from okuro.db.engine import okuro_home


def _output_dir() -> Path:
    return Path(os.environ.get(
        "OKURO_GENERATIONS_DIR", str(okuro_home() / "generations")))


def _optimize_for(prompt: str, model_id: Optional[str], family: Optional[str]) -> dict:
    """Model-conditioned prompt plan. Uses the ingested bundle block when the
    model is an okuro bundle, else synthesises a family-only block so family
    conventions (booru quality tags / negatives / natural-language) still apply."""
    from okuro.ai_models.optimize import optimize_prompt, resolve_prompting
    from okuro.ai_models.family import inherit

    block = None
    if model_id:
        try:
            block = resolve_prompting(model_id)
        except Exception:
            block = None
    if block is None and family:
        block = {"family": family}
    return optimize_prompt(prompt, inherit(block) if block else None)


def _default_service(family: Optional[str]) -> GenerationService:
    from okuro.inference.broker import Broker
    adapter = ComfyAdapter(Broker())
    registry = GenerativeWorkflowRegistry()
    return GenerationService(
        adapter, registry,
        optimize=lambda p, m: _optimize_for(p, m, family))


# BundleFile.role → the component slot it fills for a diffusion model.
_VAE_ROLES = {"vae"}
_CLIP_ROLES = {"text_encoder", "text_encoders", "clip", "encoder"}
_UNET_ROLES = {"weights", "unet", "diffusion", "diffusion_model"}
_WEIGHT_EXTS = (".safetensors", ".sft")


def _classify_diffusion_files(bundle) -> tuple[list, list, list]:
    """Partition a bundle's weight files into (primary/unet, vae, text-encoders).

    Only real weight files (.safetensors/.sft) count — tokenizer/config/template
    files are ignored. Role wins; an unroled weight is treated as primary.
    """
    primary, vae, clips = [], [], []
    for f in bundle.files:
        if not f.name.lower().endswith(_WEIGHT_EXTS):
            continue
        role = (f.role or "").lower()
        if role in _VAE_ROLES:
            vae.append(f)
        elif role in _CLIP_ROLES:
            clips.append(f)
        else:
            primary.append(f)
    primary.sort(key=lambda f: (f.role != "weights", f.name))  # weights-role first
    clips.sort(key=lambda f: f.name)
    return primary, vae, clips


def resolve_model(model_id: str, *, home=None, bundles_root=None) -> Optional[dict]:
    """Resolve an installed okuro model to what ComfyUI needs.

    Loads the bundle and links its weight(s) into okuro's ComfyUI model dirs, so
    ComfyUI sees exactly okuro's managed models. Two shapes (V4 fix — previously
    only the first .safetensors was linked, silently breaking split-weight
    models):

    * **all-in-one checkpoint** (one weight, no separate CLIP/VAE) → linked into
      ``checkpoints/``; returns ``loader="checkpoint"`` + ``ckpt_name``.
    * **split weights** (Flux/SD3.5: UNET + text-encoders + VAE as separate
      files) → each linked into its dir (``diffusion_models``/``text_encoders``/
      ``vae``); returns ``loader="components"`` + ``components={unet, clip:[…],
      vae}``.

    Returns None when ``model_id`` is not an okuro bundle (caller then supplies
    an explicit ckpt_name + family).
    """
    from okuro.ai_models.bundle import bundles_root as _broot, load_bundle
    from okuro.inference import comfy_install

    base = Path(bundles_root) if bundles_root is not None else _broot()
    bundle = load_bundle(base / model_id)
    if bundle is None:
        return None
    primary, vae, clips = _classify_diffusion_files(bundle)
    if not primary:
        return None
    prompting = bundle.prompting or {}
    files_dir = bundle.path / "files"
    common = {
        "family": prompting.get("family"),
        "license": bundle.license,
        "capability": bundle.capability,
        "vram_gb": bundle.vram_gb,
        "base_model": prompting.get("base_model"),
    }
    # Split-weight model: separate text-encoders (Flux/SD3.5) → link every
    # component into its ComfyUI dir, not just the first file.
    if clips:
        unet_link = comfy_install.link_model(
            files_dir / primary[0].name, bundle.capability, home=home,
            kind="diffusion_models")
        clip_links = [comfy_install.link_model(
            files_dir / c.name, bundle.capability, home=home, kind="text_encoders")
            for c in clips]
        vae_link = comfy_install.link_model(
            files_dir / vae[0].name, bundle.capability, home=home, kind="vae"
        ) if vae else None
        return {
            **common,
            "loader": "components",
            "ckpt_name": unet_link.name,   # for meta/labelling only
            "components": {
                "unet": unet_link.name,
                "clip": [l.name for l in clip_links],
                "vae": vae_link.name if vae_link else "",
            },
        }
    # All-in-one checkpoint (SDXL/Pony/Illustrious/SD1.5 + all-in-one Flux).
    link = comfy_install.link_model(files_dir / primary[0].name, bundle.capability, home=home)
    return {**common, "loader": "checkpoint", "ckpt_name": link.name}


def resolve_model_for_family(
    family: Optional[str], *, capability: str = "image", bundles_root=None
) -> Optional[str]:
    """Gap B — auto-pick an INSTALLED okuro model whose family matches.

    The Studio's presets give a *family* (sdxl/illustrious/flux), never a model.
    This lets ``/generate`` run from a preset without the user (or caller) naming
    a checkpoint: scan installed bundles, keep those matching ``capability`` +
    ``family``, and return the best (largest → most capable) bundle id. Returns
    None when nothing is installed for the family (the caller then triggers the
    setup/download flow — Gap A).
    """
    from okuro.ai_models.bundle import scan_bundles

    fam = (family or "").lower()
    cands = []
    for b in scan_bundles(bundles_root):
        if b.capability != capability:
            continue
        bfam = ((b.prompting or {}).get("family") or "").lower()
        if fam and bfam != fam:
            continue
        cands.append(b)
    if not cands:
        return None
    cands.sort(key=lambda b: (b.size_gb, b.id), reverse=True)
    return cands[0].id


def _best_installed_for(family, hint, bundles, *, capability="image"):
    """Pick the best installed bundle for a curated choice's family: prefer one
    whose id matches the ``hint`` token, else the largest (most capable)."""
    fam = (family or "").lower()
    h = (hint or "").lower()
    cands = [b for b in bundles
             if b.capability == capability
             and ((b.prompting or {}).get("family") or "").lower() == fam]
    if not cands:
        return None
    if h:
        hit = [b for b in cands if h in b.id.lower()]
        if hit:
            hit.sort(key=lambda b: (b.size_gb, b.id), reverse=True)
            return hit[0]
    cands.sort(key=lambda b: (b.size_gb, b.id), reverse=True)
    return cands[0]


def _cloud_available() -> bool:
    """Is a bridge media provider installed AND authenticated right now."""
    try:
        from okuro.bridge import media

        return bool(media.availability(media.IMAGE)["available"])
    except Exception:  # noqa: BLE001 — availability must never break a picker
        return False


def _resolve_choices(preset, bundles, *, cloud_ok: bool) -> list[dict]:
    """Every curated choice for a preset, with runtime availability filled in.

    Local choices intersect with installed bundles; the cloud choice's
    "installed" is whether its CLI is usable. One list, one shape, so callers
    never branch on engine to read availability.
    """
    from okuro.inference import comfy_presets as cp

    out = []
    for ch in (preset.choices or []):
        if ch.engine == cp.ENGINE_CLOUD:
            out.append({
                "family": ch.family, "label": ch.label, "rationale": ch.rationale,
                "recommended": ch.recommended, "installed": cloud_ok,
                "model_id": _CLOUD_MODEL_ID if cloud_ok else None,
                "vram_gb": None, "source": "cloud", "engine": ch.engine,
            })
            continue
        b = _best_installed_for(ch.family, ch.model_hint, bundles)
        out.append({
            "family": ch.family, "label": ch.label, "rationale": ch.rationale,
            "recommended": ch.recommended, "installed": b is not None,
            "model_id": b.id if b else None,
            "vram_gb": b.vram_gb if b else None, "source": "curated",
            "engine": ch.engine,
        })
    return out


# What a cloud choice reports as its "model". The provider owns the model and
# never names it, so the provider id is the honest answer.
_CLOUD_MODEL_ID = "antigravity"


def choice_for(preset_id: str, model_id: Optional[str] = None, *, bundles_root=None):
    """THE resolution SSOT: which curated choice will actually render this style.

    Order: an explicit ``model_id`` the user picked → the recommended choice if
    it is available → the first available choice → None. Local wins by curation
    order, not by rule: the recommended entry is a local checkpoint everywhere it
    exists, so a GPU box keeps its free local path and a GPU-less box falls
    through to the cloud entry automatically.

    Returns ``(choice, resolved_dict)`` or ``(None, None)``. Both
    ``candidates_for_preset`` (what the UI shows) and ``run_from_preset`` (what
    actually runs) go through here, so the panel can never disagree with reality.
    """
    from okuro.ai_models.bundle import scan_bundles
    from okuro.inference import comfy_presets as cp

    preset = cp.get_preset(preset_id)
    if preset is None:
        raise KeyError(f"unknown preset {preset_id!r}")
    bundles = list(scan_bundles(bundles_root))
    resolved = _resolve_choices(preset, bundles, cloud_ok=_cloud_available())

    picked = None
    if model_id:
        picked = next((r for r in resolved if r["model_id"] == model_id), None)
        if picked is None and model_id != _CLOUD_MODEL_ID:
            # An installed bundle the curation never listed — run it locally.
            return None, {"model_id": model_id, "label": model_id,
                          "family": preset.family, "installed": True,
                          "engine": cp.ENGINE_LOCAL}
    if picked is None:
        avail = [r for r in resolved if r["installed"]]
        picked = next((r for r in avail if r["recommended"]), None) or (
            avail[0] if avail else None)
    if picked is None:
        return None, None
    choice = next(c for c in preset.choices
                  if c.family == picked["family"] and c.engine == picked["engine"])
    return choice, {"model_id": picked["model_id"], "label": picked["label"],
                    "family": picked["family"], "installed": True,
                    "engine": picked["engine"]}


def candidates_for_preset(preset_id: str, *, bundles_root=None) -> dict:
    """Curated per-style model picker, with runtime availability resolved.

    Powers the Studio's "Model & prompt" disclosure (feature C): every curated
    choice across BOTH engines, plus any installed image model the curation
    didn't list (so nothing runnable is hidden — DP10), plus the default
    ``resolved`` pick. The pick comes from ``choice_for``, the same function
    ``run_from_preset`` dispatches on, so the panel cannot claim one model while
    another renders. Raises ``KeyError`` for an unknown preset.
    """
    from okuro.ai_models.bundle import scan_bundles
    from okuro.inference import comfy_presets as cp

    preset = cp.get_preset(preset_id)
    if preset is None:
        raise KeyError(f"unknown preset {preset_id!r}")
    bundles = list(scan_bundles(bundles_root))
    out = _resolve_choices(preset, bundles, cloud_ok=_cloud_available())
    covered = {c["family"].lower() for c in out}
    # Any installed image model whose family isn't curated → still offer it,
    # so nothing runnable is hidden (DP10).
    #
    # EXCEPT on a style with no local choice at all. Such a style is a CAPABILITY
    # claim — "poster" is cloud-only because local checkpoints garble type — and
    # appending arbitrary local models to it offers a renderer that cannot
    # produce the promised outcome. It also made the UI's engine toggle appear on
    # a style with only one engine.
    local_curated = cp.ENGINE_LOCAL in preset.engines
    for b in bundles if local_curated else []:
        if b.capability != "image":
            continue
        fam = ((b.prompting or {}).get("family") or "").lower()
        if not fam or fam in covered:
            continue
        covered.add(fam)
        out.append({
            "family": fam, "label": b.id,
            "rationale": f"Installed {fam} model", "recommended": False,
            "installed": True, "model_id": b.id, "vram_gb": b.vram_gb,
            "source": "installed", "engine": cp.ENGINE_LOCAL,
        })
    _choice, resolved = choice_for(preset_id, bundles_root=bundles_root)
    if resolved is None:
        # Nothing curated is available; an uncurated installed model still runs.
        auto = resolve_model_for_family(preset.family, bundles_root=bundles_root)
        if auto:
            resolved = {"model_id": auto, "label": auto, "family": preset.family,
                        "installed": True, "engine": cp.ENGINE_LOCAL}
    return {"preset": preset.id, "resolved": resolved, "choices": out}


def run_generation(
    prompt: str,
    ckpt_name: Optional[str] = None,
    family: Optional[str] = None,
    *,
    task: str = "text-to-image",
    model_id: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    seed: Optional[int] = None,
    license_id: Optional[str] = None,
    org_revenue_usd: Optional[float] = None,
    params_override: Optional[dict] = None,
    negative_extra: str = "",
    service: Optional[GenerationService] = None,
    on_progress: Optional[callable] = None,
    on_cancel: Optional[callable] = None,
    preset: Optional[str] = None,
    tier: Optional[str] = None,
) -> dict:
    """Generate + persist. Pass an installed ``model_id`` (okuro resolves its
    checkpoint, family, and licence) OR an explicit ``ckpt_name`` + ``family``.
    ``on_progress`` receives live step progress (Gap C). Returns saved paths +
    generation metadata."""
    lic = {"id": license_id} if license_id else None
    components = None
    # Gap B: no explicit model at all — auto-pick an installed one for the
    # family (the preset gives a family, never a checkpoint).
    if not model_id and not ckpt_name and family:
        model_id = resolve_model_for_family(family)
    # Resolve an installed okuro model unless the caller gave both explicit.
    if model_id and not (ckpt_name and family):
        resolved = resolve_model(model_id)
        if resolved:
            ckpt_name = ckpt_name or resolved["ckpt_name"]
            family = family or resolved["family"]
            components = resolved.get("components")
            if lic is None and resolved.get("license"):
                lic = resolved["license"]
    if not (ckpt_name and family):
        raise ValueError(
            "no model available: pass model_id / ckpt_name+family, or install a "
            f"model for family {family!r} first (Studio setup)")
    svc = service or _default_service(family)
    req = GenerationRequest(
        prompt=prompt, model_id=model_id or ckpt_name, ckpt_name=ckpt_name,
        family=family, task=task, license=lic,
        org_revenue_usd=org_revenue_usd, seed=seed, width=width, height=height,
        params_override=params_override or {}, negative_extra=negative_extra,
        components=components)
    out = svc.generate(req, on_progress=on_progress, should_cancel=on_cancel)
    d = _output_dir()
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, img in enumerate(out.images):
        p = d / f"gen_{out.seed}_{i}.png"
        p.write_bytes(img)
        paths.append(str(p))
    plan = out.prompt_plan or {}
    result = {
        "paths": paths, "count": len(paths), "seed": out.seed,
        "workflow_id": out.workflow_id, "license": out.license,
        "prompt_used": plan.get("prompt"), "endpoint": out.endpoint,
        # Feature C — transparency: what actually ran, and how the prompt was shaped.
        "model_id": model_id or ckpt_name, "family": family,
        "preset": preset, "tier": tier,
        "prompt_plan": {
            "syntax": plan.get("syntax"), "prompt": plan.get("prompt"),
            "negative": plan.get("negative"), "notes": plan.get("notes") or [],
        },
    }
    # Studio write path: index every generated file into the unified media
    # bucket (source=studio) so the Studio + assets UIs read one filestore.
    # Best-effort — a store/index failure must never fail a generation.
    try:
        from okuro.assets import store as _assets_store
        rows = _assets_store.register_studio_output(paths, result)
        # Keep the ids: they are how every consumer should address these bytes.
        # Discarding them forced callers to rebuild a filesystem path from the
        # filename, which only ever finds the generations dir.
        result["asset_ids"] = [r["id"] for r in rows if r.get("id")]
    except Exception as _exc:  # pragma: no cover - defensive
        logger.warning("asset bucket register failed: %s", _exc)
    return result


def _bucket_wh(params: dict) -> tuple[Optional[int], Optional[int]]:
    """First resolution bucket as (width, height), or (None, None)."""
    buckets = params.get("resolution_buckets") or []
    if not buckets:
        return None, None
    try:
        w, h = (int(x) for x in str(buckets[0]).lower().split("x"))
        return w, h
    except (ValueError, TypeError):
        return None, None


def run_from_cloud_preset(
    prompt: str,
    spec: dict,
    *,
    reference_images: Optional[list] = None,
    literal_text: Optional[str] = None,
    on_progress: Optional[callable] = None,
) -> dict:
    """Cloud-engine generation: a bridge media provider renders the image.

    No GPU, no ComfyUI, no downloaded checkpoint — the provider is a CLI the user
    already installed and signed into. The prompt goes across as plain language
    (no booru tags, no quality prefixes, no negative), because that is what these
    models want; ``style_negative`` is therefore dropped rather than mistranslated
    into a syntax the provider does not accept.

    Returns the same result shape as :func:`run_generation`, so the Studio REST
    layer, the asset bucket and the library are engine-agnostic.
    """
    from okuro.bridge import media
    from okuro.inference import comfy_presets

    pos, _negative_dropped = comfy_presets.apply_style(spec, prompt)
    w, h = _bucket_wh(spec["params"])

    if on_progress:
        on_progress({"phase": "generate", "message": "Sending to the image model…"})
    r = media.generate_image(
        pos, dest_dir=_output_dir(), name=spec["preset"],
        width=w, height=h, reference_images=reference_images,
        literal_text=literal_text,
        on_progress=lambda phase, message, pct: on_progress(
            {"phase": phase, "message": message, "pct": pct}) if on_progress else None,
    )
    result = {
        "paths": r["paths"], "count": r["count"], "seed": None,
        "workflow_id": None, "license": None,
        "prompt_used": r["prompt_used"], "endpoint": r["provider"],
        "model_id": r["provider"], "family": spec["family"],
        "preset": spec["preset"], "tier": spec["tier"],
        "engine": spec["engine"],
        "prompt_plan": {
            "syntax": "natural-language", "prompt": r["prompt_used"],
            "negative": None,
            "notes": [f"Rendered by {r['provider']} (no local GPU used)"]
                     + ([f"aspect ratio {r['aspect_ratio']}"] if r.get("aspect_ratio") else [])
                     + (["edited from a reference image"] if reference_images else [])
                     + ([f'text pinned to "{literal_text.strip()}"'] if (literal_text or "").strip()
                        else (["text suppressed — image renders no text"]
                              if literal_text == "" else [])),
        },
    }
    try:
        from okuro.assets import store as _assets_store
        _assets_store.register_studio_output(r["paths"], result)
    except Exception as _exc:  # pragma: no cover - defensive
        logger.warning("asset bucket register failed: %s", _exc)
    return result


def run_from_preset(
    prompt: str,
    preset_id: str,
    *,
    tier: Optional[str] = None,
    model_id: Optional[str] = None,
    ckpt_name: Optional[str] = None,
    seed: Optional[int] = None,
    org_revenue_usd: Optional[float] = None,
    service: Optional[GenerationService] = None,
    on_progress: Optional[callable] = None,
    on_cancel: Optional[callable] = None,
    reference_images: Optional[list] = None,
    literal_text: Optional[str] = None,
) -> dict:
    """Studio entry: generate from an OUTCOME preset + a plain prompt.

    Resolves the preset's CHOICE first (``choice_for``), because the engine is a
    property of the choice, not the style: "Photo" can render on a local
    checkpoint or on the cloud provider, and which one is available decides. Then
    resolves the tier params + style and dispatches on that choice's engine.

    ``model_id``/``ckpt_name`` pin a specific model; without them okuro picks the
    recommended available choice, falling through to the cloud entry when no local
    model is installed — which is what makes a GPU-less box work.
    """
    from okuro.inference import comfy_presets

    choice = None
    if not ckpt_name:  # an explicit checkpoint means the caller drives the local path
        choice, resolved = choice_for(preset_id, model_id)
        if resolved and resolved.get("engine") == comfy_presets.ENGINE_CLOUD:
            spec = comfy_presets.resolve(preset_id, tier=tier, choice=choice)
            return run_from_cloud_preset(
                prompt, spec, reference_images=reference_images,
                literal_text=literal_text, on_progress=on_progress)
        if resolved and not model_id:
            model_id = resolved["model_id"]

    spec = comfy_presets.resolve(preset_id, tier=tier, choice=choice)
    pos, neg = comfy_presets.apply_style(spec, prompt)
    params = spec["params"]
    w, h = _bucket_wh(params)
    result = run_generation(
        pos, ckpt_name=ckpt_name, family=spec["family"], task=spec["task"],
        model_id=model_id, width=w, height=h, seed=seed,
        org_revenue_usd=org_revenue_usd,
        params_override=params, negative_extra=neg, service=service,
        on_progress=on_progress, on_cancel=on_cancel,
        preset=spec["preset"], tier=spec["tier"])
    result["preset"] = spec["preset"]
    result["tier"] = spec["tier"]
    result["engine"] = spec["engine"]
    return result


def workflow_status(model_key: str, task: str = "text-to-image",
                    *, service: Optional[GenerationService] = None) -> dict:
    """UI/agent readiness check: is a ComfyUI workflow registered for this model?"""
    svc = service or _default_service(None)
    r = svc.readiness(model_key, task)
    return {"model": model_key, "task": task, "ready": r.workflow_ready,
            "workflow_id": r.workflow_id, "reason": r.reason}
