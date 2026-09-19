# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Model lineage — parse a model NAME into family / version / size /
#          quant / variant tags / author, and decide whether a release is an
#          update, a re-quant, a variant or nothing to do with an installed unit.
# index:
#   def parse                 (name -> Parsed; the whole naming grammar)
#   class Parsed              (the parsed shape, comparable)
#   def relate                (candidate x installed -> Relation)
#   def relate_release        (a release vs every installed unit of its family)
#   def unit_parse / unit_lineage_rows
#   def persist_unit_lineage  (write the parse onto model_units)
# AGENT_HEADER_END -->
"""Lineage: is release X an update, a re-quant, or a variant of installed Y?

Map A measured that okuro has no such matcher anywhere. ``suggest._known_refs``
compares ``catalog_id`` strings EXACTLY, so a newer quant of a model already on
disk reads as something brand new, and a genuinely newer version of it reads the
same way. This module is the missing half.

**It is a NAMING grammar, not a weights inspector.** Everything here comes from
the string a model is stored under — a filename, a directory, a HuggingFace id,
a tm-inference alias. That is deliberate: the answer has to exist for a release
that is not downloaded yet, so it cannot depend on opening a file. Where a GGUF
header IS available the fit path reads it (see :mod:`okuro.ai_models.fitting`);
lineage stays on names so both sides of a comparison are always available.

**Family is the base LINE, version is separate.** ``Llama-3.3-70B`` parses to
family ``llama`` version ``3.3``, not to a family called ``llama-3.3``. If the
version were welded into the family slug, ``same-family-newer`` could never fire
— which is the one relation the phase exists to produce. ``family_label`` carries
the display form (``llama-3.3``) for anything that wants to print it.

**Specialisations fork the family; variants do not.** ``Qwen3-Coder`` is not a
quant of ``Qwen3`` and must never be offered as an update to it, so ``coder`` is
family-forming: the slug becomes ``qwen-coder``. ``abliterated`` is NOT
family-forming — per the owner's ruling it is a TAG, never a filter — so
``Huihui-Qwen3-32B-abliterated`` stays family ``qwen`` and relates to a stock
Qwen3-32B as ``variant-of``. The two token sets are :data:`_SPECIALISATIONS` and
:data:`_VARIANT_TAGS`, and which set a token lands in is the single most
consequential decision in this file.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

log = logging.getLogger("okuro.ai_models.lineage")


# --- relations --------------------------------------------------------------

REL_NEWER = "same-family-newer"
REL_OTHER_QUANT = "same-family-same-version-other-quant"
REL_OLDER = "same-family-older"
REL_VARIANT = "variant-of"
REL_IDENTICAL = "identical"
REL_UNRELATED = "unrelated"

#: ``identical`` is one relation beyond the brief's five. It earns its place on
#: the P1 twins: the ten cross-store twin groups are the SAME weights on two
#: stores, same version, same quant, same tags. Calling that
#: ``…-other-quant`` would be false — the quant is not other — and calling it
#: ``unrelated`` would be worse. A twin is the commonest real pair on this host,
#: so it gets an honest name rather than the nearest wrong one.
RELATIONS = (REL_NEWER, REL_OTHER_QUANT, REL_OLDER, REL_VARIANT,
             REL_IDENTICAL, REL_UNRELATED)


# --- the naming grammar -----------------------------------------------------

_EXTS = (".gguf", ".safetensors", ".bin", ".pth", ".pt", ".onnx", ".ckpt",
         ".sft", ".msgpack", ".h5")

#: Tokens that FORK the family. A model carrying one of these is a different
#: model line, not a different build of the same line: offering Qwen3-Coder as
#: an update to Qwen3 would swap a general chat model for a code model under
#: the user's feet. The family slug becomes ``<base>-<spec>`` with the
#: specialisations sorted, so the cross-product never has to be enumerated.
_SPECIALISATIONS = {
    "coder": "coder",
    "next": "next",
    "vl": "vl",
    "omni": "omni",
    "math": "math",
    "guard": "guard",
    "embedding": "embedding",
    "reranker": "reranker",
    "rerank": "reranker",
    "scout": "scout",
    "maverick": "maverick",
    "flash": "flash",
    "air": "air",
    "image": "image",
    "moe": None,            # structural, handled as is_moe — never a fork
}

#: Specialisations that change what KIND of model this is, not just which line
#: of it. Only these two: an embedder and a reranker produce vectors and
#: scores, never tokens, and P4 scans `embedding` as its own wanted category.
#:
#: `vl` is deliberately NOT here. A vision-language model reads images and is
#: still a text LLM for every purpose this codebase has — its placement is KV
#: cache arithmetic, not a diffusion estimate — so filing Qwen3VL under
#: `vision` would hand it the media constants and make its fit worse.
_SPECIALISATION_MODALITY = {
    "embedding": "embedding",
    "reranker": "reranker",
}

#: Tokens that describe a BUILD of a model, not a different model. Per ruling 3
#: (abliterated / uncensored / nsfw are tags, never filters) the safety-variant
#: tokens live here, which is what makes ``variant-of`` possible at all.
_VARIANT_TAGS = {
    # safety / alignment variants — ruling 3: tag, never filter
    "abliterated": "abliterated",
    "abliterate": "abliterated",
    "ablit": "abliterated",
    "abl": "abliterated",
    "uncensored": "uncensored",
    "uncen": "uncensored",
    "unaligned": "uncensored",
    "nsfw": "nsfw",
    # Adult-genre and roleplay markers. These lived in the discovery gate's
    # DROP list until P4; ruling 3 says tag, never filter, so they land here
    # with every other build word. One tagger, not two: the gate must describe
    # a candidate in the same vocabulary the inventory describes a unit in, or
    # `variant-of` cannot match the two.
    "hentai": "nsfw",
    "lewd": "nsfw",
    "smut": "nsfw",
    "horny": "nsfw",
    "coomer": "nsfw",
    "taboo": "nsfw",
    "porn": "nsfw",
    "roleplay": "roleplay",
    "erp": "roleplay",
    "waifu": "roleplay",
    # tuning stage
    "instruct": "instruct",
    "inst": "instruct",
    "it": "instruct",
    "chat": "chat",
    "base": "base",
    "pt": "base",
    "thinking": "thinking",
    "reasoning": "thinking",
    "distill": "distilled",
    "distilled": "distilled",
    "qat": "qat",
    "dpo": "dpo",
    # media / component roles
    "i2v": "i2v",
    "t2v": "t2v",
    "ti2v": "ti2v",
    "s2v": "s2v",
    "t2i": "t2i",
    # Component roles, singular AND plural. The plural is not pedantry: these
    # words reach the parser as BUCKET DIRECTORY names, and a store writes
    # buckets in the plural. Without `loras` a text release came back as a
    # newer version of six image LoRAs that merely share the word `qwen`.
    "vae": "vae",
    "vaes": "vae",
    "lora": "lora",
    "loras": "lora",
    "controlnet": "controlnet",
    "controlnets": "controlnet",
    "control": "controlnet",
    "embeddings": "lora",
    "mmproj": "mmproj",
    "upscaler": "upscaler",
    "refiner": "refiner",
    "fill": "fill",
    "inpaint": "fill",
    "turbo": "turbo",
    "schnell": "schnell",
    "dev": "dev",
    "distilled-transformer": "distilled",
}

#: Multi-word variant tags, matched on the normalised (dash-joined) string
#: before token splitting, because ``text_encoders`` is one role and
#: ``text`` + ``encoders`` are two meaningless tokens.
_PHRASE_TAGS = (
    (re.compile(r"role[-_]?play"), "roleplay"),
    (re.compile(r"text[-_]?encoders?"), "text-encoder"),
    (re.compile(r"diffusion[-_]?models?"), "diffusion-model"),
    (re.compile(r"fp8[-_]?mixed"), "fp8mixed"),
    (re.compile(r"high[-_]?noise"), "high-noise"),
    (re.compile(r"low[-_]?noise"), "low-noise"),
)

#: Quanters and well-known publishers, matched as a leading token or as the
#: HuggingFace org. Recorded because "who built this quant" is half of whether
#: two files with the same family are the same thing.
_AUTHORS = {
    "unsloth", "bartowski", "mradermacher", "huihui", "huihui-ai", "thebloke",
    "lmstudio-community", "ggml-org", "quantfactory", "nikolaykozloff",
    "second-state", "mlabonne", "casperhansen", "qwen", "meta-llama", "google",
    "mistralai", "deepseek-ai", "microsoft", "nvidia", "stabilityai",
    "black-forest-labs", "zai-org", "thudm", "moonshotai", "lightricks",
    "tencent", "alibaba-pai", "facebook", "openbmb", "deepreinforce-ai",
}


@dataclass(frozen=True)
class _FamilyRule:
    """One base-family matcher.

    ``pattern`` must capture the version as group ``v`` when the family carries
    one inline. ``compact_two_digit`` handles the families whose names are
    written both ways on this host — ``hunyuanvideo15`` and ``hunyuanvideo1.5``
    are the same model, and ``wan22`` and ``wan2.2`` likewise — by reading a
    bare two-digit run as ``major.minor``. It is opt-in per family because
    ``qwen-image-2512`` is a DATE and must not become version 25.12.
    """

    slug: str
    pattern: re.Pattern
    compact_two_digit: bool = False
    modality: Optional[str] = None


#: The version capture, used by every rule that has one. The trailing ``\b`` is
#: load-bearing: without it ``deepseek-r1-distill-llama-70b`` parses as Llama
#: version 70, because ``70`` is digits sitting right where a version goes. A
#: size is digits glued to a ``b`` with no word boundary between them, so
#: requiring the boundary rejects a size and accepts a version — measured
#: against that exact name on this host's cold store. Defined once so a family
#: added later cannot forget it.
#: A semantic version is one or two digits, optionally with a minor part. The
#: bound is what tells a VERSION from a DATE STAMP: this host carries
#: `qwen-2511` and `qwen-image-2512`, which are release months, and read as
#: versions they made a 0.8 GB LoRA come back as a NEWER release than a 32B
#: model. Four digits are never a version in any family here.
#: The minor part may be separated by a dot OR a dash, because the same model
#: is written both ways on this host: the file is `Llama-3.3-70B` and the
#: tm-inference registry auto-aliases it to `llama-3-3-70b`. One grammar has to
#: read both or the alias and the file it names come back as different models.
#: The trailing \b keeps a SIZE out: `llama-70b` has no boundary between the
#: digits and the `b`, so `70` can never be taken for a version.
_V = r"(?P<v>\d{1,2}(?:[.\-]\d{1,2})?)\b"

#: The version capture for families whose names are written with the version
#: split by whatever separator the author felt like. ``Wan2_2_VAE_bf16``
#: normalises to ``wan2-2-vae``, and read with :data:`_V` that is version 2,
#: not 2.2 — which made the NVMe copy of the Wan VAE come out as an OLDER
#: release than its own byte-identical twin on the cold store. Only the
#: families that actually do this opt in, because ``qwen-image-2512`` must
#: keep its date intact.
_VC = r"(?P<v>\d{1,2}(?:[.\-]\d{1,2})?)\b"


def _rule(slug: str, pattern: str, *, compact: bool = False,
          modality: Optional[str] = None) -> _FamilyRule:
    return _FamilyRule(slug, re.compile(pattern.format(V=_V, VC=_VC), re.I),
                       compact, modality)


#: Base families, MOST SPECIFIC FIRST — the first match wins. Every entry was
#: written against a name that actually exists on this host's stores or in its
#: tm-inference registry; the long tail that matches nothing here parses to
#: family None, which is an honest "I do not know this line" and yields
#: ``unrelated`` rather than a guess.
_FAMILY_RULES: tuple[_FamilyRule, ...] = (
    # --- text LLM (ruling 10: text first) ---
    _rule("nemotron", r"nemotron(?:[-_.]?{V})?"),
    _rule("dolphin", r"dolphin[-_.]?{V}"),
    _rule("glm", r"\bglm[-_.]?{V}"),
    _rule("deepseek", r"deepseek[-_.]?(?:r|v)?{V}"),
    _rule("deepseek", r"deepseek"),
    _rule("qwen", r"\bqwen[-_.]?{V}"),
    _rule("qwen", r"\bqwen"),
    _rule("llama", r"\bllama[-_.]?{V}"),
    _rule("llama", r"\bl(?P<v>\d+\.\d+)\b"),      # L3.2-… shorthand
    _rule("llama", r"\bllama"),
    _rule("gemma", r"\bgemma[-_.]?{V}"),
    _rule("gemma", r"\bgemma"),
    _rule("mixtral", r"\bmixtral"),
    _rule("mistral-small", r"\bmistral[-_.]?small"),
    _rule("mistral", r"\bmistral"),
    _rule("phi", r"\bphi[-_.]?{V}"),
    _rule("command-r", r"\bcommand[-_.]?r"),
    _rule("ornith", r"\bornith[-_.]?{V}"),
    _rule("granite", r"\bgranite[-_.]?{V}"),
    # --- video ---
    _rule("ltx", r"\bltx[-_.]?{V}", modality="video"),
    _rule("ltx", r"\bltx", modality="video"),
    _rule("wan", r"\bwan[-_.]?{VC}", compact=True, modality="video"),
    _rule("hunyuanvideo", r"\bhunyuan[-_.]?video[-_.]?{VC}",
          compact=True, modality="video"),
    _rule("hunyuanvideo", r"\bhunyuan[-_.]?video", modality="video"),
    _rule("longcat", r"\blongcat", modality="video"),
    _rule("mochi", r"\bmochi[-_.]?{V}", modality="video"),
    _rule("mochi", r"\bmochi", modality="video"),
    # --- image ---
    _rule("flux", r"\bflux[-_.]?{V}", modality="image"),
    _rule("flux", r"\bflux", modality="image"),
    _rule("sdxl", r"stable[-_.]?diffusion[-_.]?xl", modality="image"),
    _rule("sdxl", r"\bsdxl", modality="image"),
    _rule("sd", r"stable[-_.]?diffusion[-_.]?{V}", modality="image"),
    _rule("sd", r"\bsd[-_.]?(?P<v>[123](?:\.\d+)?)\b", modality="image"),
    # Bare, no version: `stable-diffusion-x4-upscaler` is a real unit on both
    # stores and parsed to nothing until this line existed.
    _rule("sd", r"stable[-_.]?diffusion", modality="image"),
    _rule("svd", r"stable[-_.]?video[-_.]?diffusion|\bsvd[-_.]", modality="video"),
    _rule("ideogram", r"\bideogram[-_.]?{V}", modality="image"),
    _rule("ideogram", r"\bideogram", modality="image"),
    _rule("z-image", r"\bz[-_.]?image", modality="image"),
    _rule("pony", r"\bpony", modality="image"),
    _rule("juggernaut", r"\bjuggernaut", modality="image"),
    _rule("realvis", r"\brealvis", modality="image"),
    _rule("cyberrealistic", r"\bcyberrealistic", modality="image"),
    _rule("deepfloyd", r"\bdeepfloyd|\bif-i-", modality="image"),
    # --- audio ---
    _rule("musicgen", r"\bmusicgen", modality="audio"),
    _rule("ace-step", r"\bace[-_.]?step", modality="audio"),
    _rule("stable-audio", r"stable[-_.]?audio", modality="audio"),
    _rule("yue", r"\byue[-_.]?s?\d?", modality="audio"),
    _rule("voxcpm", r"\bvoxcpm[-_.]?{V}", modality="audio"),
    _rule("voxcpm", r"\bvoxcpm", modality="audio"),
    _rule("xtts", r"\bxtts[-_.]?v?{V}", modality="audio"),
    _rule("xtts", r"\bxtts", modality="audio"),
    _rule("neutts", r"\bneutts", modality="audio"),
    _rule("moss-ttsd", r"\bmoss[-_.]?ttsd", modality="audio"),
    # --- vision / 3d ---
    _rule("dinov", r"\bdinov{V}", modality="vision"),
    _rule("vitmatte", r"\bvitmatte", modality="vision"),
    _rule("trellis", r"\btrellis[-_.]?{V}", modality="3d"),
    _rule("trellis", r"\btrellis", modality="3d"),
    _rule("rmbg", r"\brmbg[-_.]?{V}", modality="vision"),
    _rule("rmbg", r"\brmbg", modality="vision"),
    _rule("clip", r"\bclip[-_.]?(?:vision|l|g)\b", modality="vision"),
    _rule("umt5", r"\bumt5", modality="text"),
    _rule("t5", r"\bt5[-_.]?(?:xxl|xl|base)", modality="text"),
)

#: Quant tokens, most specific first. ``prefix`` keeps ``i1`` (imatrix) and
#: ``UD`` (unsloth dynamic) attached, because those are genuinely different
#: files at the same nominal bit-width and the user reads them as part of the
#: quant name.
#: Quant detection runs on the NORMALISED name, where ``_`` has already become
#: ``-`` — so ``Q4_K_M`` arrives as ``q4-k-m`` and every separator class here
#: must accept both. Getting this wrong is silent: the first version of this
#: table used ``_`` only and returned quant=None for every GGUF on this host,
#: which reads as "no quant stated" rather than as a bug.
_QUANT_PREFIX = r"(?:(?P<pfx>i1|ud|imat|imatrix)[-_.])?"
_QUANT_RES: tuple[re.Pattern, ...] = (
    re.compile(_QUANT_PREFIX + r"(?P<q>IQ\d(?:[-_][A-Z]{1,3})+)\b", re.I),
    re.compile(_QUANT_PREFIX + r"(?P<q>Q\d(?:[-_][0-9KSMLX]{1,3})+)\b", re.I),
    re.compile(r"(?P<q>mxfp4|fp8mixed|fp8|fp16|fp32|bf16|nf4|int8|int4)", re.I),
    re.compile(r"\b(?P<q>f16|f32)\b", re.I),
    re.compile(r"\b(?P<q>awq|gptq|exl2|exl3)\b", re.I),
)

#: Effective bits per weight, used ONLY when a real file size is unavailable
#: (an undownloaded release whose card carries a param count but no byte size).
#: For anything on disk the weights term is the measured file size — see
#: ``fitting.Subject``. Values are the published llama.cpp/GGUF averages for a
#: 7B-class model; they drift a few percent with vocabulary size, which is why
#: they never override a measurement.
BITS_PER_WEIGHT = {
    "Q2_K": 3.35, "Q3_K_S": 3.50, "Q3_K_M": 3.91, "Q3_K_L": 4.27,
    "Q3_K_XL": 4.05, "Q4_0": 4.55, "Q4_1": 5.0, "Q4_K_S": 4.58,
    "Q4_K_M": 4.85, "Q4_K_XL": 5.0, "Q5_0": 5.54, "Q5_K_S": 5.52,
    "Q5_K_M": 5.69, "Q6_K": 6.56, "Q8_0": 8.50,
    "IQ1_M": 1.75, "IQ2_XXS": 2.06, "IQ2_M": 2.7, "IQ3_XXS": 3.06,
    "IQ3_M": 3.66, "IQ4_XS": 4.25, "IQ4_NL": 4.5,
    "MXFP4": 4.25, "NF4": 4.5, "INT4": 4.0, "INT8": 8.0,
    "FP8": 8.0, "FP8MIXED": 8.5, "AWQ": 4.25, "GPTQ": 4.25,
    "EXL2": 4.25, "EXL3": 4.0,
    "FP16": 16.0, "BF16": 16.0, "F16": 16.0, "FP32": 32.0, "F32": 32.0,
}

_DEFAULT_BPW = 4.85  # Q4_K_M — the commonest quant in this host's stores

_SIZE_ACTIVE_RE = re.compile(
    r"(?P<total>\d+(?:\.\d+)?)\s*b[-_.]a(?P<active>\d+(?:\.\d+)?)\s*b\b", re.I)
_SIZE_MOE_X_RE = re.compile(r"(?P<n>\d+)\s*x\s*(?P<each>\d+(?:\.\d+)?)\s*b\b", re.I)
_SIZE_B_RE = re.compile(r"(?<![a-z0-9.])(?P<n>\d+(?:\.\d+)?)\s*b(?![a-z0-9])", re.I)
_SIZE_M_RE = re.compile(r"(?<![a-z0-9.])(?P<n>\d{2,4})\s*m(?![a-z0-9])", re.I)


@dataclass(frozen=True)
class Parsed:
    """What a model's NAME says about it."""

    raw: str
    family: Optional[str] = None
    family_label: Optional[str] = None
    version: Optional[str] = None
    params_total_b: Optional[float] = None
    params_active_b: Optional[float] = None
    is_moe: bool = False
    quant: Optional[str] = None
    quant_base: Optional[str] = None
    variant_tags: tuple[str, ...] = ()
    author: Optional[str] = None
    modality: Optional[str] = None
    confidence: float = 0.0
    notes: tuple[str, ...] = ()

    # ---- derived ----
    @property
    def version_key(self) -> tuple:
        """Comparable version. ``()`` when absent, which never wins a compare."""
        return _version_key(self.version)

    @property
    def bits_per_weight(self) -> float:
        return BITS_PER_WEIGHT.get((self.quant_base or "").upper(), _DEFAULT_BPW)

    @property
    def identity_tags(self) -> frozenset:
        """Tags that make this a DIFFERENT model rather than another build.

        A re-quant of the same weights keeps these; an abliteration does not.
        ``instruct`` is excluded on purpose — practically every modern release
        is instruct-tuned and half of them simply omit the word, so treating it
        as identity-bearing would make two names for one model look like two
        models.
        """
        return frozenset(t for t in self.variant_tags if t in _IDENTITY_TAGS)

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "family_label": self.family_label,
            "version": self.version,
            "params_total_b": self.params_total_b,
            "params_active_b": self.params_active_b,
            "is_moe": self.is_moe,
            "quant": self.quant,
            "variant_tags": list(self.variant_tags),
            "author": self.author,
            "modality": self.modality,
            "confidence": round(self.confidence, 2),
            "notes": list(self.notes),
        }


#: Tags whose presence or absence means "a different model", not "another
#: build of the same model". Everything here changes what the weights DO.
_IDENTITY_TAGS = frozenset({
    "abliterated", "uncensored", "nsfw", "roleplay", "distilled", "thinking", "base",
    "i2v", "t2v", "ti2v", "s2v", "t2i", "vae", "text-encoder", "lora",
    "controlnet", "mmproj", "upscaler", "refiner", "fill", "turbo",
    "schnell", "dev", "high-noise", "low-noise", "diffusion-model", "qat",
})

#: The subset of identity tags that mark a PART of a pipeline rather than a
#: model. One of these on exactly one side of a comparison means the two are
#: not substitutable at all, whatever their versions say.
_COMPONENT_TAGS = frozenset({
    "vae", "text-encoder", "lora", "controlnet", "mmproj", "upscaler",
    "refiner", "diffusion-model",
})

#: The identity tags that describe WHO a build is for rather than what it does
#: — the safety/adult axis. Ruling 3 forbids filtering on these, so the
#: discovery feed TAGS them and the Discover panel hides them behind a toggle
#: (``variants=hide`` is the default). Public because the gate, the API and the
#: UI must all agree on the same list; a second copy anywhere is how a row gets
#: hidden by one surface and shown by another.
IDENTITY_VARIANT_TAGS = frozenset({
    "abliterated", "uncensored", "nsfw", "roleplay",
})


def _version_key(v: Optional[str]) -> tuple:
    if not v:
        return ()
    parts: list[int] = []
    for chunk in str(v).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            return ()
    return tuple(parts)


#: A shard index, e.g. ``-00001-of-00003``. Stripped before anything else is
#: read off the name: left in place it is five digits sitting where a quant
#: continuation goes, and the Nemotron group really did parse to the quant
#: ``UD-Q3_K_XL_00001`` until it was removed.
_SHARD_SUFFIX_RE = re.compile(r"[-_.]\d{1,5}[-_]of[-_]\d{1,5}$", re.I)


def _strip_ext(name: str) -> str:
    low = name.lower()
    for ext in _EXTS:
        if low.endswith(ext):
            name = name[: -len(ext)]
            break
    return _SHARD_SUFFIX_RE.sub("", name)


def _split_org(name: str) -> tuple[Optional[str], str]:
    """Pull a HuggingFace org off the front. Handles every separator in use.

    Four shapes live on this host: ``Org/Name`` (a card), ``Org--Name`` (a bare
    reference), ``models--Org--Name`` (an HF cache directory) and ``Org_Name``
    (what a downloader writes when it flattens a slash).
    """
    n = name
    if n.lower().startswith("models--"):
        n = n[len("models--"):]
    for sep in ("/", "--"):
        if sep in n:
            head, _, tail = n.partition(sep)
            if head and tail:
                return head, tail
    # ``org_Name`` only when the head is a KNOWN publisher AND the tail still
    # looks like a name. Plenty of real filenames carry an underscore that
    # separates nothing: ``qwen_3_4b.safetensors`` has a publisher's name in
    # front of its own version, and splitting it threw the family away and
    # left the fragment ``3_4b``.
    if "_" in n:
        head, _, tail = n.partition("_")
        if head.lower() in _AUTHORS and tail and tail[0].isalpha():
            return head, tail
    return None, n


#: Splits a specialisation that a publisher glued straight onto the version:
#: ``Qwen3VL`` and ``Qwen3-VL`` are one model line written two ways, and
#: without this the first loses its version entirely — the family pattern needs
#: a word boundary after the digits and there is none before a letter.
_GLUED_SPEC_RE = None  # built after _SPECIALISATIONS, see _build_glued_re()


def _build_glued_re() -> re.Pattern:
    toks = sorted((t for t, s in _SPECIALISATIONS.items() if s),
                  key=len, reverse=True)
    return re.compile(rf"(?<=\d)({'|'.join(map(re.escape, toks))})(?![a-z])",
                      re.I)


def _normalise(name: str) -> str:
    """Lowercase with separators unified to ``-``, so one grammar covers all."""
    global _GLUED_SPEC_RE
    s = name.lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    if _GLUED_SPEC_RE is None:
        _GLUED_SPEC_RE = _build_glued_re()
    s = _GLUED_SPEC_RE.sub(r"-\1", s)
    return s.strip("-")


def _detect_quant(norm: str) -> tuple[Optional[str], Optional[str]]:
    """``(quant_as_written, quant_base)`` or ``(None, None)``."""
    for rx in _QUANT_RES:
        m = rx.search(norm)
        if not m:
            continue
        # Canonicalise back to the form people write and read: the GGUF quant
        # names use ``_`` internally (Q4_K_M), whatever separator the file used.
        base = m.group("q").upper().replace("-", "_")
        pfx = (m.groupdict().get("pfx") or "").upper()
        if pfx in ("IMAT", "IMATRIX"):
            pfx = "I1"
        full = f"{pfx}-{base}" if pfx else base
        return full, base
    return None, None


def _detect_sizes(norm: str) -> tuple[Optional[float], Optional[float], bool, list[str]]:
    """``(total_b, active_b, is_moe, notes)`` from the size tokens in a name."""
    notes: list[str] = []
    total: Optional[float] = None
    active: Optional[float] = None
    moe = False

    m = _SIZE_ACTIVE_RE.search(norm)
    if m:
        total = float(m.group("total"))
        active = float(m.group("active"))
        moe = True
        return total, active, moe, notes

    mx = _SIZE_MOE_X_RE.search(norm)
    if mx:
        moe = True
        active = float(mx.group("each"))
        notes.append(f"{mx.group('n')}x{mx.group('each')}B expert layout")

    # The largest plain B figure is the total. ``8x3B … 18.4B`` writes both the
    # expert shape and the real total, and the total is the bigger number.
    plains = [float(g.group("n")) for g in _SIZE_B_RE.finditer(norm)]
    plains = [p for p in plains if 0.05 <= p <= 3000]
    if plains:
        total = max(plains)
    elif not total:
        mm = _SIZE_M_RE.search(norm)
        if mm:
            total = round(float(mm.group("n")) / 1000.0, 3)

    if moe and total and active and active >= total:
        # An ``NxMB`` name whose only plain figure IS the per-expert size.
        total = None
        notes.append("total params not stated; only the expert size is in the name")
    return total, active, moe, notes


def _detect_tags(norm: str, tokens: list[str]) -> tuple[list[str], list[str]]:
    """``(variant_tags, specialisations)`` found in a normalised name."""
    tags: set[str] = set()
    specs: set[str] = set()

    for rx, tag in _PHRASE_TAGS:
        if rx.search(norm):
            tags.add(tag)

    for tok in tokens:
        if tok in _VARIANT_TAGS:
            tags.add(_VARIANT_TAGS[tok])

    # Specialisations are matched on the STRING, not on split tokens, because
    # publishers glue them to the family: `Qwen3VL` and `Qwen3-VL` are the same
    # model line. Token membership missed the glued form, and a multimodal
    # projector then landed in the plain `qwen` family and came back as a
    # newer release of a 32B chat model. The guards allow a digit or a
    # separator before the token but never a letter, so `repair` cannot yield
    # `air` and `nextgen` cannot yield `next`.
    for tok, spec in _SPECIALISATIONS.items():
        if not spec:
            continue
        if re.search(rf"(?<![a-z]){re.escape(tok)}(?![a-z])", norm):
            specs.add(spec)
    return sorted(tags), sorted(specs)


def _family_label(slug: str, version: Optional[str]) -> str:
    """Display form. ``qwen``+``3`` is written ``qwen3``; ``llama``+``3.3`` is
    written ``llama-3.3``. The split is not cosmetic pedantry — these are the
    strings the user sees on a card and recognises or does not."""
    if not version:
        return slug
    base, _, rest = slug.partition("-")
    if base in _DOT_FAMILIES:
        joined = f"{base}.{version}"
    elif base in _NO_DASH_FAMILIES:
        joined = f"{base}{version}"
    else:
        joined = f"{base}-{version}"
    return f"{joined}-{rest}" if rest else joined


_NO_DASH_FAMILIES = frozenset({"qwen", "sd", "wan", "dolphin",
                               "hunyuanvideo", "phi", "dinov"})
#: Families the world writes with a dot between name and version: FLUX.1,
#: FLUX.2. Cosmetic, and it matters — the label is what the user reads on a
#: card and either recognises or does not.
_DOT_FAMILIES = frozenset({"flux"})


def parse(name: str) -> Parsed:
    """Parse one model NAME into its lineage facts.

    Accepts anything that names a model: a filename with or without extension,
    a directory, ``Org/Name``, ``Org--Name``, ``models--Org--Name``, or a
    tm-inference alias. Never raises and never guesses a family it cannot
    match — an unknown line comes back with ``family=None`` and
    ``confidence=0``, which downstream reads as "no relation can be claimed".
    """
    raw = (name or "").strip()
    if not raw:
        return Parsed(raw="")

    org, rest = _split_org(_strip_ext(raw))
    norm = _normalise(rest)
    tokens = [t for t in re.split(r"[-.]", norm) if t]

    notes: list[str] = []

    # --- family ---------------------------------------------------------
    # "X-Distill-Y" means Y's architecture distilled FROM X, so the family is
    # Y. Measured: `casperhansen--deepseek-r1-distill-llama-70b-awq` IS a
    # Llama 70B, and calling it a DeepSeek would offer it as an update to a
    # DeepSeek the user does not have.
    search_space = norm
    mdis = re.search(r"distill(?:ed)?-(?P<after>.+)$", norm)
    if mdis:
        after = mdis.group("after")
        if any(r.pattern.search(after) for r in _FAMILY_RULES):
            search_space = after
            notes.append("family taken from the token after 'distill' "
                         "(X-Distill-Y is Y's architecture)")

    family = version = None
    modality = None
    family_at_start = False
    for rule in _FAMILY_RULES:
        m = rule.pattern.search(search_space)
        if not m:
            continue
        family = rule.slug
        modality = rule.modality
        family_at_start = m.start() == 0 and search_space is norm
        v = m.groupdict().get("v") if m.groupdict() else None
        if v:
            original = v
            # A dash-separated minor is universal (registry aliases write it
            # that way). The two-digit reading — `hunyuanvideo15` meaning 1.5 —
            # stays opt-in per family, because `qwen-image-2512` is a date and
            # must not become version 25.12.
            if "-" in v:
                v = v.replace("-", ".")
            elif rule.compact_two_digit and "." not in v and len(v) == 2:
                v = f"{v[0]}.{v[1]}"
            if v != original:
                notes.append(f"compact version {original} read as {v}")
            version = v
        break

    # --- tags, size, quant ----------------------------------------------
    tags, specs = _detect_tags(norm, tokens)
    total_b, active_b, moe, size_notes = _detect_sizes(norm)
    notes.extend(size_notes)
    quant, quant_base = _detect_quant(norm)

    if family and specs:
        # A specialisation already inside the family slug must not be added
        # twice: `z-image` carries `image`, and appending it produced the
        # family `z-image-image`.
        have = set(family.split("-"))
        extra = [s for s in specs if s not in have]
        if extra:
            family = "-".join([family] + extra)

    # A specialisation can change the MODALITY, and two of them do. The base
    # rule for `qwen` declares none, so Qwen3-Embedding-0.6B came back as a
    # text LLM — which is how the embedding category read "nothing installed"
    # while okuro's own embedder was running on this box. See
    # :data:`_SPECIALISATION_MODALITY` for why only these two are listed.
    if family:
        for spec in family.split("-"):
            if spec in _SPECIALISATION_MODALITY:
                modality = _SPECIALISATION_MODALITY[spec]
                break

    # An org prefix is authoritative. A BARE leading token is only an author
    # when it is not already the family: ``qwen-image-2512`` starts with a
    # token that is both a publisher and this model's own family, and reading
    # it as the publisher put "qwen" in the author column for a model nobody
    # re-published.
    author = None
    if org:
        author = org.lower()
    elif not family_at_start:
        if tokens and tokens[0] in _AUTHORS:
            author = tokens[0]
        elif len(tokens) > 1 and f"{tokens[0]}-{tokens[1]}" in _AUTHORS:
            author = f"{tokens[0]}-{tokens[1]}"

    if moe:
        pass
    elif active_b is not None and total_b is not None and active_b < total_b:
        moe = True

    # --- confidence ------------------------------------------------------
    conf = 0.0
    if family:
        conf = 0.55
        if version:
            conf += 0.15
        if total_b:
            conf += 0.15
        if quant:
            conf += 0.15
    conf = min(conf, 1.0)

    return Parsed(
        raw=raw,
        family=family,
        family_label=_family_label(family, version) if family else None,
        version=version,
        params_total_b=total_b,
        params_active_b=active_b,
        is_moe=moe,
        quant=quant,
        quant_base=quant_base,
        variant_tags=tuple(tags),
        author=author,
        modality=modality,
        confidence=conf,
        notes=tuple(notes),
    )


# --- relation ---------------------------------------------------------------

#: Two param counts this close are the same model size. Names round
#: inconsistently (``30B`` and ``30.5B`` for one model, ``18.4B`` for an 8x3B),
#: so an exact compare would split a family that a human reads as one size.
SIZE_TOLERANCE = 0.08


@dataclass(frozen=True)
class Relation:
    relation: str
    target_unit_id: Optional[str]
    confidence: float
    why: str
    same_family: bool = False
    candidate: Optional[Parsed] = None
    installed: Optional[Parsed] = None

    def to_dict(self) -> dict:
        return {
            "relation": self.relation,
            "target_unit_id": self.target_unit_id,
            "confidence": round(self.confidence, 2),
            "why": self.why,
            "same_family": self.same_family,
        }


def _same_size(a: Optional[float], b: Optional[float]) -> Optional[bool]:
    """True / False / None when either side does not state a size."""
    if a is None or b is None:
        return None
    if a <= 0 or b <= 0:
        return None
    return abs(a - b) / max(a, b) <= SIZE_TOLERANCE


def relate(candidate: Any, installed: Any, *,
           target_unit_id: Optional[str] = None,
           candidate_date: Optional[str] = None,
           installed_date: Optional[str] = None) -> Relation:
    """How does ``candidate`` stand to ``installed``?

    Both arguments may be a name or an already-:func:`parse`\\ d value. The
    decision order below is the whole contract, and each step exists because
    the step after it would otherwise give a confidently wrong answer:

    1. **Family must match.** Without it nothing but ``unrelated`` is possible.
       ``Qwen3-Next`` against ``Qwen3`` stops here — Next is a different
       architecture line, not a newer Qwen3.
    2. **Size must match.** ``Llama-3.4-8B`` is not an update to your 70B.
    3. **Version decides direction.** Higher is newer, lower is older. With no
       version on either side, a release date decides if the candidate carries
       one; with neither, nothing can be claimed.
    4. **Same version: identity tags decide.** ``abliterated`` against stock is
       ``variant-of``.
    5. **Same version, same tags: quant decides.** Different quant is a
       re-quant; the same quant is ``identical``.
    """
    c = candidate if isinstance(candidate, Parsed) else parse(str(candidate))
    i = installed if isinstance(installed, Parsed) else parse(str(installed))

    if not c.family or not i.family:
        missing = "candidate" if not c.family else "installed"
        if not c.family and not i.family:
            missing = "both sides"
        return Relation(
            REL_UNRELATED, None, 0.0,
            f"family unknown for {missing} — no relation can be claimed from "
            f"the name alone",
            candidate=c, installed=i)

    if c.family != i.family:
        return Relation(
            REL_UNRELATED, None, 0.9,
            f"different family: {c.family} vs {i.family}",
            candidate=c, installed=i)

    conf = min(c.confidence, i.confidence)
    size_ok = _same_size(c.params_total_b, i.params_total_b)
    if size_ok is False:
        return Relation(
            REL_UNRELATED, None, max(conf, 0.6),
            f"same family {c.family} but a different size "
            f"({_fmt_b(c.params_total_b)} vs {_fmt_b(i.params_total_b)}) — "
            f"not a replacement for this unit",
            same_family=True, candidate=c, installed=i)
    if size_ok is None:
        conf *= 0.8

    ck, ik = c.version_key, i.version_key
    tag_delta = c.identity_tags.symmetric_difference(i.identity_tags)

    # A COMPONENT is not a candidate to replace a model. A projector, a VAE, a
    # text encoder, a LoRA and a ControlNet are parts of a pipeline; they share
    # a family with the model they serve and can never be an update to it.
    # Checked before the version compare, because the version is exactly what
    # made a 1 GB projector report as a newer release of an 18 GB chat model.
    comp_delta = (c.identity_tags & _COMPONENT_TAGS) ^ (i.identity_tags & _COMPONENT_TAGS)
    if comp_delta:
        return Relation(
            REL_VARIANT, target_unit_id, conf * 0.9,
            f"same family {c.family}, but these are different KINDS of "
            f"artifact ({_fmt_tags(comp_delta)}) — a component of a pipeline is "
            f"never an update to the model it serves",
            same_family=True, candidate=c, installed=i)

    if ck and ik and ck != ik:
        rel = REL_NEWER if ck > ik else REL_OLDER
        extra = (f"; variant tags also differ ({_fmt_tags(tag_delta)})"
                 if tag_delta else "")
        return Relation(
            rel, target_unit_id, conf,
            f"same family {c.family}, version {c.version} vs {i.version}"
            f"{extra}",
            same_family=True, candidate=c, installed=i)

    # Exactly ONE side states a version. The unversioned name is the line's
    # FIRST release — that is why it had no version to distinguish itself
    # from — so it is read as 1. Measured case: LTX-Video against LTX-2.5,
    # where the installed unit genuinely is the older model and neither the
    # tag nor the quant path would ever say so. The assumption is stated in
    # `why` and costs confidence, because a publisher who simply stopped
    # writing the version would break it.
    if bool(ck) != bool(ik):
        assumed = (1,)
        ck2, ik2 = (ck or assumed), (ik or assumed)
        if ck2 != ik2:
            rel = REL_NEWER if ck2 > ik2 else REL_OLDER
            bare = "candidate" if not ck else "installed unit"
            return Relation(
                rel, target_unit_id, conf * 0.75,
                f"same family {c.family}, version "
                f"{c.version or '(none stated)'} vs "
                f"{i.version or '(none stated)'} — the {bare} states no "
                f"version, read as the line's first release (1)",
                same_family=True, candidate=c, installed=i)

    if not ck and not ik and candidate_date and installed_date and \
            candidate_date != installed_date:
        rel = REL_NEWER if candidate_date > installed_date else REL_OLDER
        return Relation(
            rel, target_unit_id, conf * 0.7,
            f"same family {c.family}, neither name carries a version — "
            f"decided on release date {candidate_date} vs {installed_date}",
            same_family=True, candidate=c, installed=i)

    if tag_delta:
        return Relation(
            REL_VARIANT, target_unit_id, conf,
            f"same family {c.family} and size {_fmt_b(c.params_total_b)}, "
            f"differing variant tags ({_fmt_tags(tag_delta)})",
            same_family=True, candidate=c, installed=i)

    if c.quant and i.quant and c.quant != i.quant:
        return Relation(
            REL_OTHER_QUANT, target_unit_id, conf,
            f"same family {c.family} {c.version or '(no version)'} at "
            f"{_fmt_b(c.params_total_b)}, quant {c.quant} vs {i.quant}",
            same_family=True, candidate=c, installed=i)

    if c.quant and i.quant and c.quant == i.quant:
        return Relation(
            REL_IDENTICAL, target_unit_id, conf,
            f"same family, version, size and quant ({c.quant}) — the same "
            f"build by name",
            same_family=True, candidate=c, installed=i)

    return Relation(
        REL_IDENTICAL, target_unit_id, conf * 0.6,
        f"same family {c.family} {c.version or '(no version)'} at "
        f"{_fmt_b(c.params_total_b)}; neither name states a quant",
        same_family=True, candidate=c, installed=i)


def modality_for_family(family: Optional[str]) -> Optional[str]:
    """The modality a family slug implies, or None when the family is unknown.

    Lets a caller holding a PERSISTED family column get the same answer the
    parser would give, without re-parsing a name or walking a directory. A
    known family that declares no modality is a text LLM — the media rules all
    declare one so the text rules do not have to.
    """
    if not family:
        return None
    # A specialisation wins over the base rule, and it has to: `qwen` declares
    # no modality, so `qwen-embedding` resolved to "text" and an installed
    # embedder was invisible to the embedding category. Same table the parser
    # uses, so a persisted family and a freshly parsed name cannot disagree.
    for spec in family.split("-"):
        if spec in _SPECIALISATION_MODALITY:
            return _SPECIALISATION_MODALITY[spec]
    base = family.split("-")[0]
    for rule in _FAMILY_RULES:
        if rule.slug == family or rule.slug.split("-")[0] == base:
            if rule.modality:
                return rule.modality
    return "text"


def _fmt_b(v: Optional[float]) -> str:
    if v is None:
        return "size unknown"
    return f"{v:g}B"


def _fmt_tags(tags: Iterable[str]) -> str:
    t = sorted(tags)
    return ", ".join(t) if t else "none"


# --- units --------------------------------------------------------------

def name_candidates(unit: dict) -> list[str]:
    """Names to try for a unit, best first.

    A unit's own ``name`` is not always the model's name. Two measured shapes
    on this host defeat it: a shard group is named after its common stem, and
    the Nemotron group's stem is ``UD-Q3_K_XL`` — the QUANT, because that is
    all the shard filenames share once the index is removed. And a directory
    unit is often named for its slot (``download/assistant-models/qwen3``)
    while the weights inside carry the real name. So the primary FILE name is
    tried first and the directory name second.
    """
    out: list[str] = []
    primary = unit.get("primary_file")
    if primary:
        out.append(os.path.basename(str(primary)))
    name = unit.get("name")
    if name:
        out.append(str(name))
    rel = unit.get("rel_path")
    if rel:
        parts = [p for p in str(rel).split("/") if p]
        for p in reversed(parts[-3:]):
            if p not in out:
                out.append(p)
    return out


def unit_parse(unit: dict) -> Parsed:
    """Parse a ``model_units`` row, trying each name until a family is found."""
    best: Optional[Parsed] = None
    for cand in name_candidates(unit):
        p = parse(cand)
        if p.family:
            # A directory name may carry the family while the file carries the
            # quant, or the other way round. Merge: the first family wins, and
            # missing facts are filled from the later names.
            return _merge_from_others(p, unit, cand)
        if best is None:
            best = p
    return best or Parsed(raw=str(unit.get("name") or ""))


def _merge_from_others(p: Parsed, unit: dict, used: str) -> Parsed:
    """Fill absent facts from the unit's other names, never overriding."""
    for cand in name_candidates(unit):
        if cand == used:
            continue
        o = parse(cand)
        if o.family and o.family != p.family:
            continue
        kw: dict[str, Any] = {}
        if p.version is None and o.version:
            kw["version"] = o.version
            kw["family_label"] = _family_label(p.family or "", o.version)
        if p.params_total_b is None and o.params_total_b:
            kw["params_total_b"] = o.params_total_b
        if p.params_active_b is None and o.params_active_b:
            kw["params_active_b"] = o.params_active_b
            kw["is_moe"] = True
        if p.quant is None and o.quant:
            kw["quant"] = o.quant
            kw["quant_base"] = o.quant_base
        if o.variant_tags:
            kw["variant_tags"] = tuple(sorted(set(p.variant_tags) | set(o.variant_tags)))
        if kw:
            from dataclasses import replace
            p = replace(p, **kw)
    return p


def _primary_file(unit: dict) -> Optional[str]:
    """The unit's biggest weights file, for the name and for the GGUF header.

    Reads the directory, never a model file. Returns None when the store is
    not mounted, which is the normal answer on a host that lost a disk.
    """
    from okuro.ai_models import store_scan

    store = store_scan.resolve_store(str(unit.get("store") or ""))
    if store is None:
        return None
    path = store.path / str(unit.get("rel_path") or "")
    try:
        if path.is_file():
            return str(path)
        if not path.is_dir():
            return None
        best: Optional[tuple[int, str]] = None
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in store_scan._PRUNE_DIRS]
            for f in files:
                if not store_scan._is_model_file(f):
                    continue
                fp = os.path.join(root, f)
                try:
                    size = os.stat(fp).st_size
                except OSError:
                    continue
                if size > 0 and (best is None or size > best[0]):
                    best = (size, fp)
        return best[1] if best else None
    except OSError:
        return None


def enrich_unit(unit: dict) -> dict:
    """Add ``primary_file`` to a unit row (one directory read, no file opens)."""
    if "primary_file" not in unit:
        unit["primary_file"] = _primary_file(unit)
    return unit


# --- persistence ------------------------------------------------------------

_UNIT_LINEAGE_COLS = ("family", "version", "params_total_b", "params_active_b",
                      "quant", "variant_tags", "author")


def persist_unit_lineage(units: Optional[list[dict]] = None) -> dict:
    """Parse every unit's name and write the result onto ``model_units``.

    Cheap and idempotent: one parse and one UPDATE per unit, no file opened
    except the directory read that finds the primary filename.
    """
    from okuro.db import get_db

    db = get_db()
    if units is None:
        rows = db.fetchall(
            "SELECT unit_id, name, store, rel_path FROM model_units")
        units = [dict(r) for r in rows]

    known = unknown = 0
    for u in units:
        enrich_unit(u)
        p = unit_parse(u)
        if p.family:
            known += 1
        else:
            unknown += 1
        db.execute(
            "UPDATE model_units SET family = ?, version = ?, "
            "params_total_b = ?, params_active_b = ?, quant = ?, "
            "variant_tags = ?, author = ? WHERE unit_id = ?",
            (p.family, p.version, p.params_total_b, p.params_active_b,
             p.quant, json.dumps(list(p.variant_tags)), p.author,
             u["unit_id"]),
        )
    return {"units": len(units), "family_known": known,
            "family_unknown": unknown}


def find_unit(ident: str) -> tuple[Optional[dict], Optional[str]]:
    """Resolve a unit by whatever a person or another system calls it.

    ``(row, reason)`` — exactly one is None. Five lookups, in order of how
    strong the evidence is:

    1. ``unit_id``, ``rel_path`` or ``name`` exactly.
    2. The basename of a path.
    3. The SHARD-GROUP STEM of a path. This one is not cosmetic: P1 stores a
       shard group as one unit named for the stem its filenames share, while
       the tm-inference registry names the same model by its FIRST SHARD. Two
       of this host's 27 live aliases are written that way, and without this
       step they resolve to nothing at all.
    4. A unique substring.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT * FROM model_units WHERE unit_id = ? OR rel_path = ? OR name = ?",
        (ident, ident, ident))
    if row is not None:
        return dict(row), None

    base = os.path.basename(str(ident))
    if base != ident:
        row = db.fetchone(
            "SELECT * FROM model_units WHERE rel_path = ? OR name = ? "
            "OR rel_path LIKE ?", (base, base, f"%/{base}"))
        if row is not None:
            return dict(row), None

    # A shard file inside its group's DIRECTORY. P1 emits the directory as the
    # unit; the tm-inference registry names the file. Measured: the registry
    # lists `text/gguf/UD-Q3_K_XL/NVIDIA-Nemotron-…-00001-of-00003.gguf` while
    # the unit is `text/gguf/UD-Q3_K_XL`.
    parent = os.path.dirname(str(ident))
    if parent:
        row = db.fetchone(
            "SELECT * FROM model_units WHERE rel_path = ? OR unit_id LIKE ?",
            (parent, f"%:{parent}"))
        if row is not None:
            return dict(row), None

    stem = _SHARD_SUFFIX_RE.sub("", _strip_ext(base))
    if stem and stem != base:
        row = db.fetchone(
            "SELECT * FROM model_units WHERE name = ? OR rel_path LIKE ? "
            "OR rel_path LIKE ?", (stem, f"%/{stem}", f"%/{stem}.%"))
        if row is not None:
            return dict(row), None
        # The group may be named for whatever its filenames share AFTER the
        # model name — the Nemotron group is stored as its quant.
        rows = db.fetchall(
            "SELECT * FROM model_units WHERE rel_path LIKE ? LIMIT 2",
            (f"%{stem.split('-')[0]}%",))
        if len(rows) == 1:
            return dict(rows[0]), None

    rows = db.fetchall(
        "SELECT * FROM model_units WHERE unit_id LIKE ? OR rel_path LIKE ? "
        "LIMIT 2", (f"%{ident}%", f"%{ident}%"))
    if len(rows) == 1:
        return dict(rows[0]), None
    if len(rows) > 1:
        return None, ("more than one unit matches that id — "
                      "`okuro models inventory` lists the exact rel_path")
    return None, ("no unit matches that id — "
                  "`okuro models inventory` lists the exact rel_path")


def installed_units(family: Optional[str] = None) -> list[dict]:
    """Units from the cached table, optionally one family only."""
    from okuro.db import get_db

    sql = ("SELECT unit_id, name, store, rel_path, size_bytes, format, status, "
           "family, version, params_total_b, params_active_b, quant, "
           "variant_tags, author FROM model_units")
    params: tuple = ()
    if family:
        sql += " WHERE family = ?"
        params = (family,)
    sql += " ORDER BY size_bytes DESC"
    out = []
    for r in get_db().fetchall(sql, params):
        d = dict(r)
        try:
            d["variant_tags"] = json.loads(d.get("variant_tags") or "[]")
        except ValueError:
            d["variant_tags"] = []
        out.append(d)
    return out


def _parsed_from_row(row: dict) -> Parsed:
    """Rebuild a :class:`Parsed` from a persisted unit row, without re-parsing."""
    fam = row.get("family")
    tags = row.get("variant_tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = []
    quant = row.get("quant")
    base = None
    if quant:
        base = quant.split("-", 1)[-1].upper()
    return Parsed(
        raw=str(row.get("name") or ""),
        family=fam,
        family_label=_family_label(fam, row.get("version")) if fam else None,
        version=row.get("version"),
        params_total_b=row.get("params_total_b"),
        params_active_b=row.get("params_active_b"),
        is_moe=bool(row.get("params_active_b")),
        quant=quant,
        quant_base=base,
        variant_tags=tuple(tags),
        author=row.get("author"),
        confidence=0.85 if fam else 0.0,
    )


def relate_release(candidate: Any, *, db: Any = None,
                   candidate_date: Optional[str] = None,
                   include_unrelated: bool = False) -> dict:
    """A release against every installed unit of its family.

    This is P4's entry point: ``suggest.py`` hands it a candidate name (an HF
    id is fine) and gets back the relation to each installed unit, best first.
    ``db`` is accepted and ignored for call-site symmetry with the rest of
    ai_models, which all reach the handle through ``okuro.db.get_db``.
    """
    c = candidate if isinstance(candidate, Parsed) else parse(str(candidate))
    out: dict[str, Any] = {"candidate": c.to_dict(), "relations": []}
    if not c.family:
        out["reason"] = ("family unknown — no installed unit can be related to "
                         "this name")
        return out

    rows = installed_units(family=c.family)
    if not rows and include_unrelated:
        rows = installed_units()

    rels = []
    for row in rows:
        i = _parsed_from_row(row)
        r = relate(c, i, target_unit_id=row["unit_id"],
                   candidate_date=candidate_date)
        if r.relation == REL_UNRELATED and not include_unrelated:
            continue
        d = r.to_dict()
        d["unit"] = {
            "unit_id": row["unit_id"], "store": row["store"],
            "rel_path": row["rel_path"], "name": row["name"],
            "size_gb": round((row.get("size_bytes") or 0) / (1024 ** 3), 2),
            "family_label": i.family_label, "quant": row.get("quant"),
            "status": row.get("status"),
        }
        rels.append(d)

    order = {REL_NEWER: 0, REL_VARIANT: 1, REL_OTHER_QUANT: 2,
             REL_IDENTICAL: 3, REL_OLDER: 4, REL_UNRELATED: 5}
    rels.sort(key=lambda d: (order.get(d["relation"], 9), -d["confidence"]))
    out["relations"] = rels
    return out


def unit_lineage(unit_id: str) -> dict:
    """One installed unit's own lineage plus its siblings in the same family."""
    row, reason = find_unit(unit_id)
    if row is None:
        return {"found": False, "unit": unit_id, "reason": reason}

    d = dict(row)
    me = _parsed_from_row(d)
    sibs = []
    for other in installed_units(family=me.family) if me.family else []:
        if other["unit_id"] == d["unit_id"]:
            continue
        o = _parsed_from_row(other)
        r = relate(o, me, target_unit_id=other["unit_id"])
        if r.relation == REL_UNRELATED and not r.same_family:
            continue
        item = r.to_dict()
        item["unit"] = {
            "unit_id": other["unit_id"], "store": other["store"],
            "rel_path": other["rel_path"],
            "size_gb": round((other.get("size_bytes") or 0) / (1024 ** 3), 2),
            "quant": other.get("quant"),
        }
        sibs.append(item)
    return {
        "found": True,
        "unit": {
            "unit_id": d["unit_id"], "store": d["store"],
            "rel_path": d["rel_path"], "name": d["name"],
            "size_gb": round((d.get("size_bytes") or 0) / (1024 ** 3), 2),
            "status": d.get("status"),
        },
        "lineage": me.to_dict(),
        "siblings": sibs,
    }


__all__ = [
    "BITS_PER_WEIGHT",
    "IDENTITY_VARIANT_TAGS",
    "Parsed",
    "Relation",
    "RELATIONS",
    "REL_NEWER", "REL_OTHER_QUANT", "REL_OLDER", "REL_VARIANT",
    "REL_IDENTICAL", "REL_UNRELATED",
    "SIZE_TOLERANCE",
    "enrich_unit",
    "find_unit",
    "modality_for_family",
    "installed_units",
    "name_candidates",
    "parse",
    "persist_unit_lineage",
    "relate",
    "relate_release",
    "unit_lineage",
    "unit_parse",
]
