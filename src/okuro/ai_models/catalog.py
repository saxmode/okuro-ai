# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Model catalog (okuro.catalog/v1) — discoverable OSS models + ranking.
# index:
#   imports
#   class CatalogFormat
#   class CatalogEntry
#   def derive_purpose_summary
#   def _norm_log
#   def score_quality
#   def score_potential
#   def usable_vram_gb
#   def variant_tags | def identity_variants   (P4 — ruling 3: tag, never filter)
#   def rank
#   def from_hf_model
#   def from_civitai_model
# AGENT_HEADER_END -->
"""Model catalog — the discoverable-but-not-downloaded layer above the bundle
store. A catalog entry is an internet-researched pointer (HF / Civitai /
Ollama); once acquired it carries the resulting ``bundle_id``.

Ranking rule (model-agnostic): **hard-gate runnability first** — never surface
a model this box can't run — then rank the survivors, rewarding headroom so a
48 GB box prefers a bigger runnable variant over a tiny quant. Benchmarks are
self-reported (no neutral API) and are never used to rank.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import licensing
from .fitting import estimate_vram

SCHEMA = "okuro.catalog/v1"

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")


def _infer_param_b(name: str) -> Optional[float]:
    """Best-effort billions-of-params from a model name (e.g. '32B' → 32.0)."""
    m = _PARAM_RE.search(name or "")
    return float(m.group(1)) if m else None


# Image diffusion checkpoints have NO param count in their name and the search
# API returns no file sizes, so — unlike LLMs — they'd otherwise carry no
# runnable variant and get filtered out of discovery. Footprint by architecture
# instead: (family, approx checkpoint size GB). min_vram_gb derives from size,
# which for a diffusion checkpoint (file ≈ working set) is a fair runnability
# proxy. Family also feeds the graph builder + the Studio preset auto-solver.
_IMAGE_FOOTPRINTS: tuple[tuple[tuple[str, ...], str, float], ...] = (
    (("flux",),                                   "flux",        12.0),
    (("stable-diffusion-3", "sd3", "sd35"),       "sd35",         8.0),
    (("illustrious", "noob", "noobai"),           "illustrious",  6.5),
    (("pony",),                                   "pony",         6.5),
    (("sdxl", "stable-diffusion-xl", "xl-base"),  "sdxl",         6.5),
    (("sd-1.5", "sd15", "v1-5", "sd-v1"),         "sd15",         2.0),
)
_DEFAULT_IMAGE_FOOTPRINT = ("sdxl", 6.5)


def _image_footprint(*parts: Optional[str]) -> tuple[str, float]:
    """(family, size_gb) for an image checkpoint from its id/tags/base_model.
    Order matters: illustrious/pony before the generic sdxl catch."""
    hay = " ".join(p for p in parts if p).lower()
    for needles, family, size_gb in _IMAGE_FOOTPRINTS:
        if any(n in hay for n in needles):
            return family, size_gb
    return _DEFAULT_IMAGE_FOOTPRINT


# Bytes per weight for HuggingFace's safetensors dtype labels. Used to turn a
# published parameter count into an on-disk footprint.
_DTYPE_BYTES = {
    "F64": 8.0, "I64": 8.0,
    "F32": 4.0, "I32": 4.0,
    "BF16": 2.0, "F16": 2.0, "FP16": 2.0, "I16": 2.0,
    "F8_E4M3": 1.0, "F8_E5M2": 1.0, "FP8": 1.0, "I8": 1.0, "U8": 1.0, "BOOL": 1.0,
    "U4": 0.5, "I4": 0.5, "F4": 0.5,
}
_DEFAULT_DTYPE_BYTES = 2.0  # bf16 — what almost every modern release ships as


def _safetensors_footprint(
    data: dict,
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """(params_b, size_gb, precision) from HuggingFace's safetensors index.

    MEASURED, not declared. HF publishes the exact parameter count and the
    dtype breakdown on the LIST endpoint under ``expand=safetensors``, so a
    release whose NAME carries no ``B`` still gets a real footprint. This is
    what makes the vision / 3d / music / video / embedding categories rankable
    at all: every one of them is full of models named without a size, and the
    runnability gate in :func:`rank` drops anything with no known footprint.

    Returns ``(None, None, None)`` when the repo publishes no index — GGUF
    repos and single-blob checkpoints do not, and those fall back to the name
    or to a declared modality footprint.
    """
    st = data.get("safetensors")
    if not isinstance(st, dict):
        return None, None, None
    total = st.get("total")
    try:
        total = float(total or 0)
    except (TypeError, ValueError):
        return None, None, None
    if total <= 0:
        return None, None, None

    by_dtype = st.get("parameters") if isinstance(st.get("parameters"), dict) else {}
    dominant, bytes_total = None, 0.0
    best_count = 0.0
    for dtype, count in (by_dtype or {}).items():
        try:
            n = float(count or 0)
        except (TypeError, ValueError):
            continue
        bytes_total += n * _DTYPE_BYTES.get(str(dtype).upper(), _DEFAULT_DTYPE_BYTES)
        if n > best_count:
            best_count, dominant = n, str(dtype).upper()
    if bytes_total <= 0:
        bytes_total = total * _DEFAULT_DTYPE_BYTES
    return (
        round(total / 1e9, 2),
        round(bytes_total / (1024 ** 3), 2),
        dominant.lower() if dominant else None,
    )


# Declared footprints for a media release that publishes NEITHER a parameter
# count in its name NOR a safetensors index — a single-blob checkpoint. These
# are ESTIMATES, in the same sense P3 labels its media constants: they exist so
# a real release is rankable instead of invisible, not because anyone measured
# this particular repo. A candidate placed on one of these carries
# ``footprint_basis='modality-default'`` so the number can be told from a
# measured one.
_MEDIA_FOOTPRINTS_GB = {
    "video": 16.0,    # the Wan / LTX transformer class this host already runs
    "3d": 5.0,        # TRELLIS / stable-fast-3d class
    "vision": 8.0,    # a small VLM plus its projector
    "audio": 4.0,     # the MusicGen / stable-audio class
}

# Civitai's own `baseModel` strings for the video families. Civitai has no
# video TYPE — a video checkpoint is filed as `Checkpoint` like an SDXL merge —
# so the base model is the only place the source states the modality, and
# without this every video candidate arrives labelled `image`. Substring match,
# because the strings carry version and variant ("Wan Video 2.2 I2V-A14B").
# Verified against the live API 2026-09-15.
_CIVITAI_VIDEO_BASE = (
    "wan video", "hunyuan video", "ltx", "kling", "vidu", "mochi",
    "cogvideo", "animatediff", "svd", "stable video",
)

# Civitai model type → (modality, task)
_CIVITAI_TYPE_MAP = {
    "Checkpoint": ("image", "text-to-image"),
    "LORA": ("image", "image-adapter"),
    "LoCon": ("image", "image-adapter"),
    "TextualInversion": ("image", "embedding"),
    "VAE": ("image", "vae"),
    "Controlnet": ("image", "controlnet"),
}

# HF pipeline_tag → (modality, task). Every tag name below was verified against
# the live https://huggingface.co/api/models?pipeline_tag=<tag> endpoint on
# 2026-09-15; each returned models.
#
# The vision / 3d / video rows are new in P4 and close interpretation gap (e):
# until now every one of these tags fell through to the ("text", "chat")
# default, so a 3D generator was ranked as a chat model and a vision-language
# model was indistinguishable from an LLM.
_HF_PIPELINE_MAP = {
    "text-generation": ("text", "chat"),
    "text2text-generation": ("text", "chat"),
    "text-to-image": ("image", "text-to-image"),
    "image-to-image": ("image", "image-to-image"),
    "automatic-speech-recognition": ("audio", "stt"),
    "text-to-speech": ("audio", "tts"),
    "text-to-audio": ("audio", "music"),
    "audio-to-audio": ("audio", "audio-to-audio"),
    "feature-extraction": ("embedding", "embedding"),
    "sentence-similarity": ("embedding", "embedding"),
    # vision-language: HF renamed this from image-to-text; both are live
    "image-text-to-text": ("vision", "vlm"),
    "image-to-text": ("vision", "captioning"),
    "visual-question-answering": ("vision", "vqa"),
    # video
    "text-to-video": ("video", "text-to-video"),
    "image-to-video": ("video", "image-to-video"),
    # 3d
    "image-to-3d": ("3d", "image-to-3d"),
    "text-to-3d": ("3d", "text-to-3d"),
}


@dataclass
class CatalogFormat:
    """One acquirable artifact variant of a catalog entry."""

    format: str  # gguf | safetensors | ...
    quant: Optional[str] = None
    precision: Optional[str] = None
    param_b: Optional[float] = None
    size_gb: Optional[float] = None
    engine: Optional[str] = None

    @property
    def min_vram_gb(self) -> float:
        """Estimated VRAM to run this variant.

        LLMs: derived from params+quant. Non-LLM (image/audio/video) have no
        "B" param → the on-disk size is the best available VRAM proxy.
        """
        if self.param_b is not None:
            return estimate_vram(f"{self.param_b}B", self.quant or self.precision)
        return round(self.size_gb or 0.0, 1)


@dataclass
class CatalogEntry:
    """A discoverable OSS model (okuro.catalog/v1)."""

    catalog_id: str  # "<source>:<repo_id>"
    source: str  # huggingface | civitai | ollama | modelscope
    source_ref: dict = field(default_factory=dict)
    display_name: str = ""
    provenance: dict = field(default_factory=dict)
    modality: str = "text"
    task: str = "chat"
    capabilities: list[str] = field(default_factory=list)
    base_model: Optional[str] = None
    family: Optional[str] = None
    purpose_summary: str = ""
    formats: list[CatalogFormat] = field(default_factory=list)
    quality_signals: dict = field(default_factory=dict)
    license: dict = field(default_factory=dict)
    gated: bool = False
    nsfw: bool = False
    credential_required: Optional[str] = None
    acquired: bool = False
    bundle_id: Optional[str] = None

    @property
    def best_runnable_format(self) -> Optional[CatalogFormat]:
        """Smallest-VRAM variant with a known footprint (None if unknown)."""
        known = [f for f in self.formats if f.min_vram_gb > 0]
        return min(known, key=lambda f: f.min_vram_gb) if known else None

    @property
    def min_vram_gb(self) -> float:
        best = self.best_runnable_format
        return best.min_vram_gb if best else 0.0

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "catalog_id": self.catalog_id,
            "source": self.source,
            "source_ref": self.source_ref,
            "display_name": self.display_name,
            "provenance": self.provenance,
            "modality": self.modality,
            "task": self.task,
            "capabilities": self.capabilities,
            "base_model": self.base_model,
            "family": self.family,
            "purpose_summary": self.purpose_summary,
            "formats": [vars(f) for f in self.formats],
            "quality_signals": self.quality_signals,
            "license": self.license,
            "gated": self.gated,
            "nsfw": self.nsfw,
            "credential_required": self.credential_required,
            "acquired": self.acquired,
            "bundle_id": self.bundle_id,
            "min_vram_gb": self.min_vram_gb,
        }


def derive_purpose_summary(entry: CatalogEntry) -> str:
    """Deterministic one-liner from task + family + size + standout capability.

    No LLM call — purpose is a projection of metadata, per stream E.
    """
    parts: list[str] = []
    if entry.task:
        parts.append(entry.task.replace("-", " "))
    fam = entry.family or entry.base_model
    if fam:
        parts.append(f"({fam})")
    best = entry.best_runnable_format
    if best and best.param_b:
        parts.append(f"{best.param_b:g}B")
    standout = next(
        (c for c in entry.capabilities if c in ("tools", "reasoning", "vision", "code")),
        None,
    )
    if standout:
        parts.append(standout)
    return " · ".join(parts) if parts else (entry.display_name or entry.catalog_id)


def _norm_log(value: float, ceiling: float) -> float:
    """log-normalize a count into 0..1 against a ceiling (e.g. 1e6 downloads)."""
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1 + value) / math.log10(1 + ceiling))


def score_quality(entry: CatalogEntry) -> float:
    """0..1 popularity signal. Never uses self-reported benchmarks."""
    q = entry.quality_signals
    downloads = _norm_log(float(q.get("downloads", 0) or 0), 1_000_000)
    likes = _norm_log(float(q.get("likes", 0) or 0), 10_000)
    trending = min(1.0, float(q.get("trending_score", 0) or 0) / 100.0)
    return round(0.5 * downloads + 0.3 * likes + 0.2 * trending, 4)


def recency_score(entry: CatalogEntry, *, now=None, half_life_days: float = 90.0) -> float:
    """0..1 freshness from the model's publish date (exp decay, 0.5 at the
    half-life). 0 when no date is known — so a missing timestamp never fakes
    freshness. Fuels the 'what's new' discovery mode and score_potential."""
    from datetime import datetime, timezone

    ts = (entry.quality_signals or {}).get("created_at")
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return round(0.5 ** (age_days / half_life_days), 4)


def usable_vram_gb(detection: dict) -> float:
    """Largest non-display GPU VRAM from a capabilities() dict (0 if none)."""
    best = 0.0
    for g in detection.get("gpus", []):
        if g.get("is_display"):
            continue
        best = max(best, float(g.get("vram_gb", 0) or 0))
    return best


def score_potential(entry: CatalogEntry, tier_vram_gb: float, recency: float = 0.5) -> float:
    """fit × (0.7·quality + 0.3·recency). Unrunnable ⇒ 0."""
    fits = entry.min_vram_gb > 0 and entry.min_vram_gb <= tier_vram_gb * 0.9
    if not fits:
        return 0.0
    return round(0.7 * score_quality(entry) + 0.3 * recency, 4)


# Names that mark a THROWAWAY upload rather than a model anyone should be
# offered: CI fixtures, tutorial output, sanity-check repos.
#
# WHAT LEFT THIS LIST IN P4, AND WHY. Until P4 it also held `uncensored`,
# `nsfw`, `abliterated`, `unaligned` and the adult-genre words
# (`taboo`, `roleplay`, `role-play`, `erp`, `waifu`, `horny`, `smut`, `lewd`,
# `hentai`, `coomer`). Ruling 3 is tag, never filter — and the measurement
# behind it is that 7 of this host's 25 live inference aliases are abliterated
# or uncensored builds, so the gate was rejecting the running fleet. Those
# words are now VARIANT TAGS (`lineage._VARIANT_TAGS`, the same table that
# labels installed units) and the Discover feed hides them behind a toggle
# instead of pretending they do not exist.
#
# The reputation gate below, not this list, is what keeps the feed clean: an
# unknown author needs adoption whatever their model is called.
_NOISE_PATTERNS = (
    "tiny-random", "tiny-dummy", "tiny-model", "dummy-model", "test-model",
    "my-first", "hf-internal-testing", "debug-model", "placeholder-model",
    "sanity-check", "random-weights", "untrained",
)

# Orgs whose releases are trustworthy even at low download counts — a week-1
# release from a real lab is "greatly reviewed" before downloads accrue. Also
# the reputable GGUF re-quanters the local stack actually pulls from.
_REPUTABLE_ORGS = frozenset({
    "qwen", "meta-llama", "google", "mistralai", "deepseek-ai", "microsoft",
    "nvidia", "ibm-granite", "allenai", "tiiuae", "cohereforai", "openai",
    "zai-org", "thudm", "huggingfacetb", "internlm", "01-ai", "stabilityai",
    "black-forest-labs", "unsloth", "bartowski", "thebloke", "mradermacher",
    "ggml-org", "lmstudio-community", "qwen2", "moonshotai",
    # Added in P4 — the identity-variant publishers this host's own fleet is
    # built from. Each is justified by a file in the live inference registry,
    # not by reputation in the abstract: without them the abliterated
    # categories admit nothing at week-1 download counts, which is the gate
    # rejecting the running fleet all over again under a different rule.
    #   huihui-ai  -> three aliases (Qwen3-8B, Qwen3.6-35B-A3B, Qwen3-VL-30B)
    #   davidau    -> the `_D_AU` suffix on the 8x3B MoE alias
    #   mlabonne   -> the gemma-3-27b-it-abliterated alias
    "huihui-ai", "davidau", "mlabonne",
})
_MIN_DOWNLOADS = 1000  # reputation floor for unknown-org models


def _entry_org(entry: CatalogEntry) -> str:
    """Publishing org from the catalog_id (huggingface:owner/repo → owner)."""
    ref = (entry.catalog_id or "").split(":", 1)[-1]
    return ref.split("/", 1)[0].lower() if "/" in ref else ""


def _is_noise(entry: CatalogEntry) -> bool:
    """True for roleplay/uncensored finetunes — filtered from the quality feed."""
    hay = f"{entry.catalog_id} {entry.display_name}".lower()
    return any(p in hay for p in _NOISE_PATTERNS)


def _is_reputable(entry: CatalogEntry, min_downloads: Optional[int] = None) -> bool:
    """Admit reputable-org releases at any download count; otherwise require a
    minimum adoption floor. Keeps week-1 lab releases while dropping obscure
    one-off finetunes from unknown authors.

    ``min_downloads`` overrides the global floor for one category — a bucket
    like ``3d`` has two orders of magnitude fewer downloads across the whole
    modality than ``text``, so the same absolute floor means "reputable" in one
    and "nothing exists" in the other.
    """
    if _entry_org(entry) in _REPUTABLE_ORGS:
        return True
    floor = _MIN_DOWNLOADS if min_downloads is None else int(min_downloads)
    dl = float((entry.quality_signals or {}).get("downloads", 0) or 0)
    return dl >= floor


def variant_tags(entry: CatalogEntry) -> list[str]:
    """Variant tags for a candidate, from the ONE tagger.

    ``lineage.parse`` is what labels installed units, so a release and the unit
    it relates to are described in the same words — that is what makes
    ``variant-of`` reachable at all. Both halves of the catalog id are parsed
    because a repo owner sometimes carries the word the model name omits
    (``huihui-ai/Qwen3-8B`` is an abliterated line by publisher).

    The source's own nsfw flag is folded in: Civitai states it as a field
    rather than in the name, and a fact from the source outranks a guess.
    """
    from . import lineage

    tags: set[str] = set()
    ref = (entry.catalog_id or "").split(":", 1)[-1]
    for name in (ref, entry.display_name or ""):
        if name:
            tags.update(lineage.parse(str(name)).variant_tags)
    if entry.nsfw:
        tags.add("nsfw")
    return sorted(tags)


def identity_variants(entry: CatalogEntry) -> list[str]:
    """The subset of a candidate's tags the Discover feed hides by default.

    Ruling 3: tag, never filter. Nothing here removes a row — it decides which
    side of the ``variants=hide|show`` toggle the row lands on.
    """
    from .lineage import IDENTITY_VARIANT_TAGS

    return [t for t in variant_tags(entry) if t in IDENTITY_VARIANT_TAGS]


def rank(
    entries: list[CatalogEntry],
    detection: dict,
    *,
    modality: Optional[str] = None,
    tasks: Optional[list[str]] = None,
    commercial: bool = False,
    org_revenue_usd: Optional[float] = None,
    strict: bool = True,
    min_downloads: Optional[int] = None,
) -> list[CatalogEntry]:
    """Hard-filter runnability + intent, then rank survivors.

    Rewards headroom: on a big GPU a larger runnable variant scores above a
    tiny quant. Never surfaces a model the box can't run.

    Commercial gating is 3-state (licensing.py): when ``org_revenue_usd`` is
    given, a community-licensed model (SD3.5/Krea) is admitted only below its
    revenue cap and a non-commercial model (FLUX.1-dev) is excluded. Without a
    revenue figure it falls back to the coarse ``commercial_use`` boolean.
    """
    tier_vram = usable_vram_gb(detection)
    out: list[tuple[float, CatalogEntry]] = []
    for e in entries:
        if modality and e.modality != modality:
            continue
        if tasks and e.task not in tasks:
            continue
        # Quality gate — keep the feed to broadly-reviewed releases: drop
        # throwaway uploads and obscure low-adoption one-offs.
        #
        # `e.nsfw` USED TO BE A THIRD DROP CONDITION HERE and is deliberately
        # gone: it is Civitai's own flag, and dropping on it made the
        # image-nsfw and video-nsfw categories structurally unreachable — the
        # only source that serves them marks every row with it. Ruling 3: the
        # flag becomes a tag (see `variant_tags`), the feed hides it by
        # default, and the toggle shows it.
        if strict and (_is_noise(e) or not _is_reputable(e, min_downloads)):
            continue
        if commercial:
            if org_revenue_usd is None:
                if not e.license.get("commercial_use", False):
                    continue
            elif not licensing.gate(e.license, org_revenue_usd,
                                    family=e.family, base_model=e.base_model)["available"]:
                continue
        best = e.best_runnable_format
        if best is None or best.min_vram_gb > tier_vram * 0.9:
            continue  # hard runnability gate
        # headroom: reward using more of the available VRAM
        fit_quality = min(1.0, best.min_vram_gb / (tier_vram or 1.0))
        score = 0.40 * fit_quality + 0.35 * score_quality(e) + 0.15 * 0.5 + 0.10 * (
            1.0 if (modality and e.modality == modality) else 0.0
        )
        out.append((score, e))
    out.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in out]


def from_hf_model(data: dict) -> CatalogEntry:
    """Normalize a HuggingFace Hub model record → CatalogEntry."""
    repo_id = data.get("id") or data.get("modelId") or ""
    pipeline = data.get("pipeline_tag") or ""
    modality, task = _HF_PIPELINE_MAP.get(pipeline, ("text", "chat"))
    card = data.get("cardData") or {}
    tags = data.get("tags") or []
    caps = [t for t in ("tools", "reasoning", "vision", "code", "moe") if t in tags]
    param_b = _infer_param_b(repo_id)
    is_gguf = "gguf" in tags or (data.get("library_name") == "gguf")
    hf_base = card.get("base_model") if isinstance(card.get("base_model"), str) else None
    image_family: Optional[str] = None
    formats: list[CatalogFormat] = []

    # Measured first: HF's own safetensors index beats a name and beats a
    # declared constant. It also supplies the param count for a model whose
    # name has no `B`, which is most of the non-text modalities.
    st_params, st_size_gb, st_precision = _safetensors_footprint(data)
    if param_b is None:
        param_b = st_params

    if modality == "image" and not is_gguf:
        # Diffusion checkpoint: footprint by architecture so it's runnable +
        # rankable (the search API gives no size and there's no param count).
        image_family, size_gb = _image_footprint(repo_id, " ".join(tags), hf_base)
        formats.append(CatalogFormat(
            format="safetensors", precision="fp16",
            size_gb=st_size_gb or size_gb, engine="comfyui"))
    elif modality in _MEDIA_FOOTPRINTS_GB and not is_gguf:
        # vision / video / 3d / audio: the working set is the checkpoint, so
        # size is the VRAM proxy — the same rule the image path already used.
        # LLM param arithmetic (KV cache per token) does not describe a
        # diffusion or 3D pipeline, so size_gb is passed and param_b is not.
        size_gb = st_size_gb
        if size_gb is None and param_b is not None:
            # No index, but the name carries a size — bf16 is what a modern
            # release ships as, and a derived number beats a declared one.
            size_gb = round(param_b * _DEFAULT_DTYPE_BYTES * 1e9 / (1024 ** 3), 2)
        formats.append(CatalogFormat(
            format="safetensors", precision=st_precision or "bf16",
            size_gb=size_gb or _MEDIA_FOOTPRINTS_GB[modality],
            engine="comfyui"))
    elif param_b is not None:
        formats.append(
            CatalogFormat(
                format="gguf" if is_gguf else "safetensors",
                precision=None if is_gguf else (st_precision or "bf16"),
                param_b=param_b,
                engine="llama-server" if is_gguf else "vllm",
            )
        )
    elif is_gguf and modality in _MEDIA_FOOTPRINTS_GB:
        # A GGUF media repo with no size anywhere in the name. Declared.
        formats.append(CatalogFormat(
            format="gguf", size_gb=_MEDIA_FOOTPRINTS_GB[modality],
            engine="comfyui"))
    entry = CatalogEntry(
        catalog_id=f"huggingface:{repo_id}",
        source="huggingface",
        source_ref={"repo_id": repo_id, "revision": data.get("sha") or "main"},
        display_name=repo_id.split("/")[-1] if repo_id else "",
        provenance={
            "url": f"https://huggingface.co/{repo_id}",
            "publisher": repo_id.split("/")[0] if "/" in repo_id else None,
            "last_modified": data.get("lastModified"),
        },
        modality=modality,
        task=task,
        capabilities=caps,
        base_model=hf_base,
        family=image_family,
        formats=formats,
        quality_signals={
            "downloads": data.get("downloads", 0),
            "likes": data.get("likes", 0),
            # The API field is camelCase. Reading only `trending_score` meant
            # this signal was silently 0 for every HF candidate ever ranked,
            # so 20% of score_quality never fired — measured against the live
            # endpoint 2026-09-15, where `full=true` returns `trendingScore`
            # and no `trending_score`. The snake_case read is kept so a canned
            # payload written against the old shape still works.
            "trending_score": data.get("trendingScore",
                                       data.get("trending_score", 0)) or 0,
            "created_at": data.get("createdAt"),
        },
        license={"id": card.get("license"), "commercial_use": _hf_commercial(card.get("license")),
                 **_tier_fields({"id": card.get("license")})},
        gated=bool(data.get("gated")),
        # HuggingFace states this as a tag rather than a field; Civitai as a
        # field. Both end up on the same attribute so one gate reads one thing.
        nsfw="not-for-all-audiences" in tags,
        credential_required="hf" if data.get("gated") else None,
    )
    entry.purpose_summary = derive_purpose_summary(entry)
    return entry


def _tier_fields(lic: dict) -> dict:
    """3-state commercial fields (licensing.py SSOT) for an entry's license dict."""
    tier, cap = licensing.classify(lic)
    return {"commercial_tier": tier, "revenue_cap_usd": cap}


def _hf_commercial(license_id: Any) -> bool:
    """Permissive-license heuristic (verify per-model before commercial use)."""
    if not isinstance(license_id, str):
        return False
    permissive = {"apache-2.0", "mit", "bsd", "bsd-3-clause", "cc-by-4.0", "llama3", "gemma"}
    return license_id.lower() in permissive


def from_civitai_model(data: dict, version: Optional[dict] = None) -> CatalogEntry:
    """Normalize a Civitai model (+ optional version) → CatalogEntry."""
    mid = data.get("id")
    version = version or (data.get("modelVersions") or [{}])[0]
    modality, task = _CIVITAI_TYPE_MAP.get(data.get("type", ""), ("image", "text-to-image"))
    base_model = version.get("baseModel") or ""
    if any(v in base_model.lower() for v in _CIVITAI_VIDEO_BASE):
        modality = "video"
        task = "image-adapter" if task == "image-adapter" else "text-to-video"
    stats = data.get("stats") or {}
    commercial = data.get("allowCommercialUse") not in (None, "None", [], ["None"])
    formats: list[CatalogFormat] = []
    for f in version.get("files") or []:
        size_kb = f.get("sizeKB") or 0
        meta = f.get("metadata") or {}
        formats.append(
            CatalogFormat(
                format=(meta.get("format") or "safetensors").lower(),
                precision=meta.get("fp"),
                size_gb=round(size_kb / (1024 ** 2), 2) if size_kb else None,
                engine="comfyui",
            )
        )
    entry = CatalogEntry(
        catalog_id=f"civitai:{mid}",
        source="civitai",
        source_ref={"civitai_model_id": mid, "civitai_version_id": version.get("id")},
        display_name=data.get("name", ""),
        provenance={
            "url": f"https://civitai.com/models/{mid}",
            "publisher": (data.get("creator") or {}).get("username"),
        },
        modality=modality,
        task=task,
        capabilities=[w for w in (version.get("trainedWords") or []) if w][:8],
        base_model=version.get("baseModel"),
        family=(version.get("baseModel") or "").split(" ")[0].lower() or None,
        formats=formats,
        quality_signals={
            "downloads": stats.get("downloadCount", 0),
            "likes": stats.get("thumbsUpCount", 0),
            "created_at": (version.get("publishedAt") or data.get("publishedAt")),
        },
        license={"commercial_use": commercial, "raw": data.get("allowCommercialUse"),
                 **_tier_fields({"raw": data.get("allowCommercialUse")})},
        nsfw=bool(data.get("nsfw")),
        gated=False,
    )
    entry.purpose_summary = derive_purpose_summary(entry)
    return entry
