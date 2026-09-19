# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Model acquisition — plan + guards + download into the bundle store.
# index:
#   imports
#   class AcquisitionError
#   class AcquisitionPlan
#   def _slug
#   def bundle_id_for
#   def plan
#   def _free_bytes
#   def check_disk / check_credentials / check_fit
#   def _download / _download_hf / _download_civitai
#   def pull
#   --- P6: the store-aware pull ---
#   def bucket_for / class Destination / def pick_store / def destination_for
#   def headroom_warn_gb / class Headroom / def headroom_for
#   def results_urls / def results_for
#   def resolve_files        (what a pull would fetch, with sizes)
#   def register_unit        (scoped scan -> lineage -> fit)
#   def pull_model           (the one call CLI / API / MCP all render)
# AGENT_HEADER_END -->
"""Acquire a catalog entry into the bundle store: resolve a variant, run
pre-flight guards (disk / credential / fit), download resumably, verify, and
materialize a bundle.

Model-agnostic: works for any CatalogEntry regardless of source or modality.
The byte-transfer sits behind ``_download`` so the plan + guard orchestration
is unit-tested without multi-GB live pulls; the real path delegates to
``huggingface_hub`` (HF) or a ranged urllib stream (Civitai).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .bundle import Bundle, BundleFile, bundles_root, write_bundle
from .catalog import CatalogEntry, CatalogFormat
from .credentials import resolve_token

# fraction of a variant's estimated VRAM the box should have to run it well
_FIT_SAFETY = 0.9
# extra headroom over raw download size before we allow a pull
_DISK_HEADROOM = 1.15


class AcquisitionError(Exception):
    """A pre-flight guard failed; message is user-actionable."""


@dataclass
class AcquisitionPlan:
    entry: CatalogEntry
    variant: CatalogFormat
    bundle_id: str
    est_size_gb: float
    credential_required: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "model"


def bundle_id_for(entry: CatalogEntry, variant: CatalogFormat) -> str:
    """Deterministic bundle id: <modality>.<slug>.<quant|precision|format>."""
    qualifier = variant.quant or variant.precision or variant.format or "default"
    name = entry.display_name or entry.catalog_id.split(":")[-1]
    return f"{entry.modality}.{_slug(name)}.{_slug(qualifier)}"


def plan(entry: CatalogEntry, variant: Optional[CatalogFormat] = None) -> AcquisitionPlan:
    """Resolve the variant (default = smallest runnable) and size the pull."""
    variant = variant or entry.best_runnable_format
    if variant is None:
        raise AcquisitionError(
            f"{entry.catalog_id}: no acquirable variant with a known footprint."
        )
    est = variant.size_gb if variant.size_gb else variant.min_vram_gb
    return AcquisitionPlan(
        entry=entry,
        variant=variant,
        bundle_id=bundle_id_for(entry, variant),
        est_size_gb=round(est or 0.0, 2),
        credential_required=entry.credential_required,
    )


def _free_bytes(path: Path) -> int:
    """Free bytes on the fs holding ``path`` (walk up to an existing parent)."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def check_disk(plan: AcquisitionPlan, root: Optional[Path] = None) -> None:
    base = root or bundles_root()
    required = int(plan.est_size_gb * (1024 ** 3) * _DISK_HEADROOM)
    free = _free_bytes(base)
    if plan.est_size_gb > 0 and free < required:
        raise AcquisitionError(
            f"Insufficient disk for {plan.bundle_id}: need "
            f"~{required / 1024**3:.1f} GB (incl headroom), "
            f"{free / 1024**3:.1f} GB free at {base}."
        )


def check_credentials(plan: AcquisitionPlan, storage=None) -> None:
    if plan.credential_required and not resolve_token(plan.credential_required, storage):
        raise AcquisitionError(
            f"{plan.entry.catalog_id} is gated: a '{plan.credential_required}' "
            f"token is required. Add it to the keyring, then retry."
        )


def check_fit(plan: AcquisitionPlan, detection: Optional[dict]) -> None:
    """Soft guard — appends a warning (never raises) if the box is tight."""
    if not detection:
        return
    from .catalog import usable_vram_gb

    tier = usable_vram_gb(detection)
    need = plan.variant.min_vram_gb
    if need > 0 and tier > 0 and need > tier * _FIT_SAFETY:
        plan.warnings.append(
            f"{plan.bundle_id} needs ~{need:.0f} GB VRAM; usable non-display "
            f"GPU has {tier:.0f} GB — expect CPU offload or OOM."
        )


# Quant preference when a GGUF repo ships many and the variant names none.
# Q4_K_M is the quality/size sweet spot for a mid-size model and fits every
# tier we serve; larger quants follow for boxes with headroom.
_GGUF_QUANT_PREFERENCE = (
    "Q4_K_M", "Q5_K_M", "Q4_K_S", "Q5_K_S", "Q6_K", "Q8_0",
    "Q3_K_M", "Q3_K_L", "Q3_K_S", "Q2_K",
)
_QUANT_RE = re.compile(r"(IQ\d+_[A-Z0-9]+|Q\d+_[A-Z0-9_]+K?[A-Z]*|Q\d+_\d+|F16|BF16|F32)", re.I)


def _quant_of(filename: str) -> Optional[str]:
    """Extract the quant token from a GGUF filename, e.g. ...-Q4_K_M.gguf → Q4_K_M."""
    m = _QUANT_RE.search(filename)
    return m.group(1).upper() if m else None


def select_gguf_patterns(gguf_files: list[str], preferred: Optional[str] = None) -> list[str]:
    """Choose the allow_patterns that fetch exactly ONE quant from a repo.

    A multi-quant GGUF repo (unsloth ships ~15) must not be pulled wholesale —
    that is 100GB+ of quants we will never load. Prefer ``preferred`` (the
    catalog variant's quant), else the first available from the preference
    order; return a glob that also captures shard files (``*-00001-of-000NN``)
    and naturally excludes vision ``mmproj-*`` projectors.
    """
    ggufs = [f for f in gguf_files if f.lower().endswith(".gguf")]
    if not ggufs:
        return ["*.gguf"]
    avail: dict[str, list[str]] = {}
    for f in ggufs:
        q = _quant_of(Path(f).name)
        if q:
            avail.setdefault(q, []).append(f)
    order = ([preferred.upper()] if preferred else []) + list(_GGUF_QUANT_PREFERENCE)
    for q in order:
        if q and q in avail:
            return [f"*{q}*.gguf"]
    # No recognizable quant token (single unlabeled .gguf repo) — take it all;
    # if there are several, take the smallest by name to avoid a blind bulk pull.
    if len(ggufs) == 1:
        return ["*.gguf"]
    return [f"*{Path(sorted(ggufs)[0]).name}"]


def select_image_checkpoint(repo_files: list[str]) -> Optional[str]:
    """Pick the single all-in-one diffusion checkpoint from a HF repo listing.

    Image repos often ship BOTH a root-level all-in-one ``.safetensors`` (what
    ComfyUI's CheckpointLoaderSimple loads) AND a diffusers split
    (``unet/…``, ``vae/…``, ``text_encoder/…`` sub-files). Downloading the whole
    tree yields an ambiguous multi-file bundle; this selects the one all-in-one
    file so the pull produces a clean checkpoint. Prefers root-level files,
    excludes component/aux weights, and de-prioritises guided/refiner/inpaint
    variants. Returns None when the repo has no usable single-file checkpoint.
    """
    st = [f for f in repo_files if f.lower().endswith(".safetensors")]
    pool = [f for f in st if "/" not in f] or st
    aux = ("vae", "text_encoder", "encoder", "unet", "tokenizer", "controlnet")
    main = [f for f in pool if not any(k in f.lower() for k in aux)] or pool

    def key(f: str) -> tuple:
        low = f.lower()
        penalty = sum(k in low for k in ("guided", "refiner", "inpaint", "turbo"))
        return (penalty, len(f))

    return sorted(main, key=key)[0] if main else None


def _download_hf(entry: CatalogEntry, variant: CatalogFormat, dest: Path, storage) -> list[BundleFile]:
    from huggingface_hub import HfApi, snapshot_download

    repo_id = entry.source_ref.get("repo_id")
    revision = entry.source_ref.get("revision", "main")
    token = resolve_token("huggingface", storage)
    if variant.format == "gguf":
        repo_files = HfApi().list_repo_files(repo_id, revision=revision, token=token)
        patterns = select_gguf_patterns(repo_files, preferred=variant.quant)
    elif entry.modality == "image":
        # Fetch ONLY the all-in-one checkpoint, not the whole diffusers tree.
        repo_files = HfApi().list_repo_files(repo_id, revision=revision, token=token)
        ckpt = select_image_checkpoint(repo_files)
        patterns = [ckpt] if ckpt else ["*.safetensors"]
    else:
        patterns = ["*.safetensors", "*.json", "*.model"]
    local = snapshot_download(
        repo_id=repo_id,
        revision=revision,
        allow_patterns=patterns,
        local_dir=str(dest),
        token=token,
    )
    files: list[BundleFile] = []
    for f in sorted(Path(local).rglob("*")):
        if f.is_file():
            role = "weights" if f.suffix in (".gguf", ".safetensors", ".bin") else "config"
            files.append(BundleFile(name=f.name, role=role, size_bytes=f.stat().st_size))
    return files


_CIVITAI_VERSION_API = "https://civitai.com/api/v1/model-versions"


def select_civitai_file(files: list[dict], variant: CatalogFormat) -> Optional[dict]:
    """Pick which version file to download.

    Prefer the version's ``primary`` file; if the variant names a format,
    prefer a file whose metadata format matches; else the first file.
    """
    if not files:
        return None
    if variant and variant.format:
        fmt = variant.format.lower()
        for f in files:
            if ((f.get("metadata") or {}).get("format") or "").lower() == fmt:
                return f
    for f in files:
        if f.get("primary"):
            return f
    return files[0]


def _stream_to_file(url: str, dest_path: Path, headers: dict, expected_sha256: Optional[str] = None) -> None:
    """Resumable streamed download to ``dest_path``.

    Resumes a partial file via a ``Range`` request; if the server ignores the
    range (200 instead of 206) the file is rewritten from scratch. Verifies
    SHA256 when the source provides one. Civitai's ``downloadUrl`` 307-redirects
    to a presigned CDN URL, so the auth header is only needed on the first hop.
    """
    import hashlib
    import urllib.request

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_suffix(dest_path.suffix + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0

    req_headers = dict(headers)
    if have:
        req_headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        mode = "ab" if (have and resp.status == 206) else "wb"
        if mode == "wb":
            have = 0  # server ignored the range → start over
        with open(tmp, mode) as fh:
            shutil.copyfileobj(resp, fh, length=1024 * 1024)

    if expected_sha256:
        h = hashlib.sha256()
        with open(tmp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest().lower() != expected_sha256.lower():
            tmp.unlink(missing_ok=True)
            raise AcquisitionError(
                f"Civitai download hash mismatch for {dest_path.name} "
                f"(expected {expected_sha256[:12]}…)"
            )
    tmp.replace(dest_path)


def _download_civitai(entry: CatalogEntry, variant: CatalogFormat, dest: Path, storage) -> list[BundleFile]:
    from .credentials import auth_header
    from .discovery import _get_json

    version_id = entry.source_ref.get("civitai_version_id")
    if not version_id:
        raise AcquisitionError(f"{entry.catalog_id}: no civitai_version_id to download")
    meta = _get_json(f"{_CIVITAI_VERSION_API}/{version_id}", headers=auth_header("civitai", storage))
    if not isinstance(meta, dict):
        raise AcquisitionError(f"Civitai version {version_id} metadata fetch failed")
    chosen = select_civitai_file(meta.get("files") or [], variant)
    if not chosen or not chosen.get("downloadUrl"):
        raise AcquisitionError(f"Civitai version {version_id}: no downloadable file")

    name = chosen.get("name") or f"civitai-{version_id}.safetensors"
    sha = (chosen.get("hashes") or {}).get("SHA256")
    _stream_to_file(chosen["downloadUrl"], dest / name, auth_header("civitai", storage), sha)
    size = (dest / name).stat().st_size
    role = "weights" if Path(name).suffix in (".safetensors", ".gguf", ".bin", ".ckpt", ".pt") else "config"
    return [BundleFile(name=name, role=role, size_bytes=size)]


def _download(plan: AcquisitionPlan, dest: Path, storage) -> list[BundleFile]:
    dest.mkdir(parents=True, exist_ok=True)
    if plan.entry.source == "huggingface":
        return _download_hf(plan.entry, plan.variant, dest, storage)
    if plan.entry.source == "civitai":
        return _download_civitai(plan.entry, plan.variant, dest, storage)
    raise AcquisitionError(f"Unsupported source: {plan.entry.source}")


def pull(
    entry: CatalogEntry,
    variant: Optional[CatalogFormat] = None,
    *,
    root: Optional[Path] = None,
    storage=None,
    detection: Optional[dict] = None,
    allow_oversize: bool = False,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> Bundle:
    """Acquire an entry into the bundle store; returns the materialized Bundle.

    Order: plan → credential guard → disk guard → soft fit guard → download →
    materialize (write_bundle). Guards raise AcquisitionError with actionable
    messages before any bytes move.

    ``progress_cb`` (optional) is called ~1×/s with
    ``{"downloaded_bytes", "total_bytes"}`` during the download. Both HF
    (snapshot_download local_dir) and Civitai (streamed) write into the bundle
    files dir, so we report progress uniformly by polling that dir's size
    against the planned total — no per-backend hook needed.
    """
    base = Path(root) if root is not None else bundles_root()
    acq = plan(entry, variant)

    check_credentials(acq, storage)
    check_disk(acq, base)
    check_fit(acq, detection)
    if acq.warnings and not allow_oversize:
        raise AcquisitionError(" ; ".join(acq.warnings) + " (pass allow_oversize=True to force)")

    files_dir = base / acq.bundle_id / "files"
    total_bytes = int((acq.est_size_gb or 0) * (1024 ** 3))

    stop = None
    if progress_cb is not None:
        import threading

        stop = threading.Event()

        def _poll() -> None:
            while not stop.is_set():
                try:
                    dl = sum(
                        f.stat().st_size
                        for f in files_dir.rglob("*")
                        if f.is_file()
                    )
                    progress_cb({"downloaded_bytes": dl, "total_bytes": total_bytes})
                except Exception:  # noqa: BLE001 — progress is best-effort
                    pass
                stop.wait(1.0)

        threading.Thread(target=_poll, name="pull-progress", daemon=True).start()

    try:
        files = _download(acq, files_dir, storage)
    finally:
        if stop is not None:
            stop.set()

    if progress_cb is not None:  # final authoritative 100% tick
        actual = sum(f.size_bytes for f in files)
        progress_cb({"downloaded_bytes": actual, "total_bytes": total_bytes or actual})

    bundle = Bundle(
        id=acq.bundle_id,
        capability={"text": "llm", "image": "image", "audio": "tts"}.get(entry.modality, entry.modality),
        path=base / acq.bundle_id,
        display_name=entry.display_name,
        engine=acq.variant.engine,
        format=acq.variant.format,
        quant=acq.variant.quant,
        parameters=(f"{acq.variant.param_b:g}B" if acq.variant.param_b else None),
        files=files,
        source={**entry.provenance, "catalog_id": entry.catalog_id, "source": entry.source},
        license=entry.license or None,
    )

    # Ingest the model's prompting manual from the artifact we just downloaded
    # (offline, from GGUF metadata). Best-effort: a pull must not fail on it.
    try:
        from .ingest import build_prompting_block

        block = build_prompting_block(bundle, entry=entry, storage=storage)
        if block:
            bundle.prompting = block
            if bundle.context_length is None:
                bundle.context_length = block.get("constraints", {}).get("context_length")
    except Exception as exc:  # pragma: no cover - defensive
        import logging

        logging.getLogger("okuro.ai_models.acquire").warning(
            "ingest failed for %s: %s", bundle.id, exc
        )

    write_bundle(bundle, root=base)
    return bundle


# ---------------------------------------------------------------------------
# P6 — the store-aware pull
#
# Everything above this line writes into okuro's OWN bundle store
# (``~/.okuro/models/bundles``), which is where a model okuro itself serves
# belongs. Everything below writes into a DECLARED MODEL STORE — the hot NVMe
# or cold archive this host actually keeps its models on — and registers the
# result as a unit so `model_inventory` can see it.
#
# The two coexist rather than one replacing the other: a bundle is okuro's
# serving format and `inference/engine.py` reads it, while a store unit is what
# every OTHER tool on the box reads. A host that declares no store keeps the
# bundle behaviour it has today, unchanged.
# ---------------------------------------------------------------------------

import fnmatch
import logging
import os
from dataclasses import asdict

log = logging.getLogger("okuro.ai_models.acquire")

HEADROOM_CONVENTION_KEY = "ai_models.headroom_warn_gb"
RESULTS_URLS_CONVENTION_KEY = "ai_models.results_urls"

#: Below this much free space AFTER the pull, every surface says so — and the
#: pull proceeds anyway. Ruling 8, 2026-09-15: there is NO headroom floor and a
#: download is never refused for space. Overridable per host.
DEFAULT_HEADROOM_WARN_GB = 50.0

#: modality -> the store bucket a model of that kind is filed under. This is
#: the INVERSE of ``fitting.modality_dirs``, which reads a bucket name back
#: into a modality; the two must agree or a model lands somewhere the scanner
#: then re-reads as a different kind of thing.
#:
#: Only generic category words, the rule P1 set for store paths and P3 for GPU
#: names: a host's own project buckets are host data and live in config.
_MODALITY_BUCKET = {
    "text": "text",
    "embedding": "text",
    "image": "image",
    "video": "video",
    "audio": "audio",
    "vision": "vision",
    "3d": "3d",
}

#: Shipped ``results_urls``. One entry, and it is okuro's OWN route — relative,
#: so it is correct on every host and names nothing host-specific. Loopback
#: ports belong to the host, not to the repo, and go in config beside
#: ``model_stores``.
_DEFAULT_RESULTS_URLS = {
    "image": "/studio",
    "video": "/studio",
}


def bucket_for(modality: Optional[str], fmt: Optional[str] = None) -> str:
    """The store subdirectory a model of this modality and format belongs in.

    GGUF gets its own subdir under the modality bucket because that is how a
    store that holds both a GGUF and a HuggingFace checkout of the same family
    keeps them apart, and because the loose-weights bucket rule in P1's scanner
    is what turns ``text/gguf/`` into one unit per file.
    """
    base = _MODALITY_BUCKET.get((modality or "text").lower(), "text")
    return f"{base}/gguf" if (fmt or "").lower() == "gguf" else base


@dataclass
class Destination:
    """Where a pull will put a model, and which store owns that path."""

    store: str
    store_path: Path
    tier: str
    bucket: str
    leaf: str
    rel_path: str
    path: Path
    why: str = ""

    def to_dict(self) -> dict:
        return {"store": self.store, "store_path": str(self.store_path),
                "tier": self.tier, "bucket": self.bucket, "leaf": self.leaf,
                "rel_path": self.rel_path, "path": str(self.path),
                "why": self.why}


def _leaf_of(ref: str) -> str:
    """The directory name a reference becomes on disk.

    Sanitised, because a Civitai model name can carry anything a person typed
    into a web form, including path separators and non-ASCII scripts.
    """
    raw = str(ref or "").strip().strip("/")
    if ":" in raw and "/" not in raw.split(":", 1)[0]:
        raw = raw.split(":", 1)[1]          # huggingface:Org/Name -> Org/Name
    leaf = raw.split("/")[-1].strip() or "model"
    leaf = re.sub(r"[^A-Za-z0-9._+-]+", "-", leaf).strip("-._")
    return leaf or "model"


def _inside(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` is ``root`` or lies under it, textually.

    Textual on purpose: ``os.path.realpath`` would follow a symlink out of the
    store and then judge the TARGET, and a store whose bucket is a link to
    another disk is a normal thing. The question this answers is whether the
    path okuro is about to create is addressed inside a declared store.
    """
    r = os.path.normpath(str(root))
    c = os.path.normpath(str(candidate))
    return c == r or c.startswith(r.rstrip("/") + os.sep)


def pick_store(store: Optional[str] = None):
    """The store a pull lands in — ``(ModelStore, why)`` or ``(None, reason)``.

    THE RULE, and it is the same one ``swap._predicted_destination`` states for
    an undownloaded release: an explicitly named store wins, otherwise the
    first HOT store, otherwise the first declared store. Never the cold archive
    by default — pointing a hot consumer at the archive is the one thing the
    storage policy forbids outright.
    """
    from .store_scan import FEATURE_OFF_REASON, configured_stores

    stores = configured_stores()
    if not stores:
        return None, FEATURE_OFF_REASON
    if store:
        for s in stores:
            if s.name == store:
                return s, f"store named on the command line: {s.name}"
        return None, (f"unknown store: {store} — declared stores are "
                      f"{', '.join(s.name for s in stores)}")
    hot = [s for s in stores if s.tier == "hot"]
    if hot:
        return hot[0], f"the first hot store ({hot[0].name}); no --store given"
    return stores[0], (f"no hot store is declared, so the first declared store "
                       f"({stores[0].name}) is the destination")


def destination_for(ref: str, *, modality: Optional[str] = None,
                    fmt: Optional[str] = None, store: Optional[str] = None,
                    to: Optional[str] = None, bucket: Optional[str] = None,
                    name_hint: Optional[str] = None
                    ) -> tuple[Optional[Destination], str]:
    """Where a model goes — ``(Destination, why)`` or ``(None, reason)``.

    ONE function, and it is the only one. ``swap._predicted_destination``
    calls it with the bucket of the unit being replaced, a pull calls it with
    the release's modality; both get the same store, the same containment
    check and the same shape of answer, because a predicted destination that
    did not match the real one would make every swap diff a lie.

    ``to`` is a path RELATIVE to the store root and overrides the bucket rule.
    A path that would land outside the chosen store is refused — the one hard
    refusal in this phase, and it is about writing outside a declared store,
    not about space.
    """
    chosen, why = pick_store(store)
    if chosen is None:
        return None, why

    leaf = _leaf_of(ref)
    # A Civitai reference is an opaque NUMBER, and a directory called 133005
    # tells a person nothing. Where the source publishes a name, the id is
    # kept as a suffix so the directory stays unique and traceable.
    if leaf.isdigit() and name_hint:
        leaf = f"{_leaf_of(name_hint)}-{leaf}"
    if to:
        rel = os.path.normpath(str(to).strip())
        if os.path.isabs(rel):
            return None, (f"--to takes a path RELATIVE to the store root; "
                          f"{to} is absolute")
        rel = rel.strip("/")
        buck = os.path.dirname(rel)
        leaf = os.path.basename(rel) or leaf
        why = f"{why}; path given with --to"
    else:
        buck = (bucket if bucket is not None
                else bucket_for(modality, fmt)).strip("/")
        rel = f"{buck}/{leaf}" if buck else leaf
        why = (f"{why}; bucket {buck or '(store root)'} from "
               + ("the unit being replaced" if bucket is not None
                  else f"modality {modality or 'text'}"
                       + (f" + format {fmt}" if fmt else "")))

    path = Path(os.path.normpath(os.path.join(str(chosen.path), rel)))
    if not _inside(chosen.path, path):
        return None, (f"{path} is outside the {chosen.name} store "
                      f"({chosen.path}) — a pull never writes outside a "
                      f"declared store")
    return Destination(store=chosen.name, store_path=chosen.path,
                       tier=chosen.tier, bucket=buck, leaf=leaf,
                       rel_path=rel, path=path, why=why), why


# --- headroom: a warning with numbers, never a refusal (ruling 8) -----------


def headroom_warn_gb() -> float:
    """How little free space after a pull is worth saying out loud."""
    try:
        from okuro.yu.conventions import get_convention

        raw = get_convention(HEADROOM_CONVENTION_KEY, DEFAULT_HEADROOM_WARN_GB)
        return float(raw)
    except Exception:          # a bad convention must never block a download
        return DEFAULT_HEADROOM_WARN_GB


@dataclass
class Headroom:
    """Free space on the destination store against what the pull will add."""

    store: str
    free_gb: Optional[float]
    total_gb: Optional[float]
    size_gb: Optional[float]
    after_gb: Optional[float]
    margin_gb: float
    warn: bool
    size_known: bool
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


def headroom_for(dest: Destination, size_gb: Optional[float],
                 free_gb: Optional[float] = None,
                 total_gb: Optional[float] = None) -> Headroom:
    """The headroom line every surface prints — and it NEVER refuses.

    Ruling 8, 2026-09-15: there is no headroom floor. A pull that would fill
    the disk is still a pull; what okuro owes the person is the numbers, on
    every surface, before the bytes move.

    ``free_gb`` is injectable so the warning can be tested and demonstrated
    without filling a real disk. Left out, it is measured live through the
    same ``ModelStore.free()`` the inventory store bar reads, so a warning and
    a bar can never disagree about one disk.
    """
    from .store_scan import resolve_store

    if free_gb is None or total_gb is None:
        store = resolve_store(dest.store)
        measured = store.free() if store else (None, None)
        free_gb = free_gb if free_gb is not None else measured[0]
        total_gb = total_gb if total_gb is not None else measured[1]

    margin = headroom_warn_gb()
    known = size_gb is not None and size_gb > 0
    after = (round(free_gb - size_gb, 1)
             if known and free_gb is not None else None)
    warn = after is not None and after < margin

    if free_gb is None:
        text = (f"headroom unknown — free space on the {dest.store} store "
                f"could not be read")
    elif not known:
        text = (f"size unknown — the source publishes no size for this "
                f"model, so the {free_gb:.1f} GB free on {dest.store} cannot "
                f"be checked against it. Proceeding.")
    elif warn:
        text = (f"HEADROOM WARNING — {dest.store} has {free_gb:.1f} GB free, "
                f"this pull adds {size_gb:.1f} GB, leaving {after:.1f} GB "
                f"against a {margin:.0f} GB margin. Downloading anyway: there "
                f"is no headroom floor.")
    else:
        text = (f"headroom ok — {dest.store} has {free_gb:.1f} GB free, this "
                f"pull adds {size_gb:.1f} GB, leaving {after:.1f} GB "
                f"(margin {margin:.0f} GB).")
    return Headroom(store=dest.store, free_gb=free_gb, total_gb=total_gb,
                    size_gb=round(size_gb, 2) if known else None,
                    after_gb=after, margin_gb=margin, warn=warn,
                    size_known=bool(known), text=text)


# --- where to see the results ----------------------------------------------


def results_urls() -> dict:
    """``{consumer_or_modality: url}`` for this host.

    The shipped default carries okuro's own relative route and nothing else.
    A host's loopback ports are host data — they belong beside ``model_stores``
    in ``~/.okuro/config.yaml``, not in this repo, which is the rule P1 set for
    store paths and P3 for GPU names.
    """
    out = dict(_DEFAULT_RESULTS_URLS)
    try:
        from okuro.yu.conventions import get_convention

        extra = get_convention(RESULTS_URLS_CONVENTION_KEY, {}) or {}
        if isinstance(extra, dict):
            out.update({str(k).lower(): str(v) for k, v in extra.items()})
        else:
            log.warning("%s must be a mapping of {name: url}",
                        RESULTS_URLS_CONVENTION_KEY)
    except Exception:
        pass
    return out


def results_for(modality: Optional[str] = None,
                consumers: Optional[Iterable[str]] = None) -> list[dict]:
    """Where a person goes to SEE what this model produces.

    A declared CONSUMER wins over the modality: the tool that actually loads
    this model is a better answer than the kind of model it is. Nothing
    declared is an empty list, not a guess.
    """
    table = results_urls()
    seen: set[str] = set()
    out: list[dict] = []
    for name in list(consumers or []):
        url = table.get(str(name).lower())
        if url and url not in seen:
            seen.add(url)
            out.append({"label": str(name), "url": url, "via": "consumer"})
    if modality:
        url = table.get(str(modality).lower())
        if url and url not in seen:
            seen.add(url)
            out.append({"label": str(modality), "url": url, "via": "modality"})
    return out


# --- what a pull would actually fetch ---------------------------------------


@dataclass
class ResolvedFiles:
    """The file list a pull would fetch, and how it was narrowed."""

    source: str
    files: list[dict]                     # [{name, size_bytes|None}]
    patterns: list[str]
    total_bytes: Optional[int]
    sizes_known: bool
    why: str
    civitai_file: Optional[dict] = None   # the chosen version file, civitai only

    @property
    def total_gb(self) -> Optional[float]:
        return (round(self.total_bytes / (1024 ** 3), 2)
                if self.total_bytes else None)

    def to_dict(self) -> dict:
        return {"source": self.source, "files": self.files,
                "patterns": self.patterns, "total_bytes": self.total_bytes,
                "total_gb": self.total_gb, "sizes_known": self.sizes_known,
                "why": self.why}


def _hf_repo_files(repo_id: str, revision: str, token) -> list[dict]:
    """``[{name, size_bytes}]`` for a HuggingFace repo, sizes where published.

    ``model_info(files_metadata=True)`` is the only listing that carries a
    size, and the size is what the headroom line needs — the catalog's
    ``size_gb`` is a card figure that may be absent or an estimate, while this
    is the byte count of the files this pull will actually request.
    """
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo_id, revision=revision, files_metadata=True,
                              token=token)
    out: list[dict] = []
    for sib in (getattr(info, "siblings", None) or []):
        name = getattr(sib, "rfilename", None)
        if not name:
            continue
        out.append({"name": name, "size_bytes": getattr(sib, "size", None)})
    return out


def resolve_files(entry: CatalogEntry, variant: CatalogFormat, *,
                  file: Optional[str] = None, storage=None,
                  lister: Optional[Callable[[], list[dict]]] = None,
                  version_fetcher: Optional[Callable[[], dict]] = None
                  ) -> ResolvedFiles:
    """Decide WHICH files a pull fetches, before fetching any of them.

    ``lister`` / ``version_fetcher`` are injection points, not decoration: the
    dry-run must be provable to make no download, and a test that injects the
    listing proves the whole path with no network at all.

    ``file`` is an exact name or a glob. It is the answer to a 27-file GGUF
    repo: ``--file '*Q4_K_M*'`` fetches one quant, not every quant.
    """
    if entry.source == "civitai":
        fetch = version_fetcher
        if fetch is None:
            from .credentials import auth_header
            from .discovery import _get_json

            version_id = entry.source_ref.get("civitai_version_id")
            if not version_id:
                raise AcquisitionError(
                    f"{entry.catalog_id}: no civitai_version_id to download")

            def fetch() -> dict:                      # noqa: E306
                meta = _get_json(f"{_CIVITAI_VERSION_API}/{version_id}",
                                 headers=auth_header("civitai", storage))
                if not isinstance(meta, dict):
                    raise AcquisitionError(
                        f"Civitai version {version_id} metadata fetch failed")
                return meta

        meta = fetch()
        all_files = meta.get("files") or []
        chosen = None
        if file:
            for f in all_files:
                if fnmatch.fnmatch(str(f.get("name") or ""), file):
                    chosen = f
                    break
            if chosen is None:
                raise AcquisitionError(
                    f"no file in civitai version "
                    f"{entry.source_ref.get('civitai_version_id')} matches "
                    f"{file!r} — available: "
                    f"{', '.join(str(f.get('name')) for f in all_files) or '(none)'}")
        else:
            chosen = select_civitai_file(all_files, variant)
        if not chosen:
            raise AcquisitionError(
                f"{entry.catalog_id}: civitai version has no downloadable file")
        size_kb = chosen.get("sizeKB")
        size_bytes = int(float(size_kb) * 1024) if size_kb else None
        return ResolvedFiles(
            source="civitai",
            files=[{"name": chosen.get("name"), "size_bytes": size_bytes}],
            patterns=[str(chosen.get("name") or "")],
            total_bytes=size_bytes, sizes_known=size_bytes is not None,
            why=("named with --file" if file else
                 "the version's primary file" if chosen.get("primary") else
                 "the first file of the version"),
            civitai_file=chosen)

    # --- HuggingFace ---
    repo_id = entry.source_ref.get("repo_id")
    revision = entry.source_ref.get("revision", "main")
    if lister is None:
        token = resolve_token("huggingface", storage)

        def lister() -> list[dict]:                   # noqa: E306
            return _hf_repo_files(repo_id, revision, token)

    listing = lister()
    names = [f["name"] for f in listing]

    if file:
        patterns = [file]
        why = f"named with --file ({file})"
    elif (variant.format or "").lower() == "gguf":
        patterns = select_gguf_patterns(names, preferred=variant.quant)
        why = (f"one GGUF quant out of {len([n for n in names if n.lower().endswith('.gguf')])} "
               f"in the repo — a multi-quant repo is never pulled wholesale")
    elif entry.modality == "image":
        ckpt = select_image_checkpoint(names)
        patterns = [ckpt] if ckpt else ["*.safetensors"]
        why = "the single all-in-one checkpoint, not the diffusers split"
    else:
        patterns = ["*.safetensors", "*.json", "*.model"]
        why = "the snapshot: weights plus the config files that describe them"

    picked = [f for f in listing
              if any(fnmatch.fnmatch(f["name"], p) for p in patterns)]
    if not picked:
        raise AcquisitionError(
            f"{entry.catalog_id}: no file matches {patterns} — the repo holds "
            f"{len(names)} file(s)")
    sizes = [f["size_bytes"] for f in picked]
    known = all(s is not None for s in sizes) and bool(sizes)
    return ResolvedFiles(source="huggingface", files=picked, patterns=patterns,
                         total_bytes=(sum(s for s in sizes if s) if known
                                      else None),
                         sizes_known=known, why=why)


# --- registration: a downloaded model is not installed until okuro can see it


def units_under(store: str, rel_path: str) -> list[dict]:
    """The unit rows at or below one store-relative path, enriched.

    Enriched the same way the inventory page enriches them — consumers, tier
    and the placement space — because "the unit is registered" has to mean the
    same thing on the pull result as on the Inventory tab.
    """
    from .store_scan import (_COLS, _attach_consumers, _attach_fits,
                             _attach_results, _shape)

    from okuro.db import get_db

    rel = str(rel_path or "").strip("/")
    rows = get_db().fetchall(
        f"SELECT {', '.join(_COLS)} FROM model_units "
        f"WHERE store = ? AND (rel_path = ? OR rel_path LIKE ?) "
        f"ORDER BY size_bytes DESC",
        (store, rel, rel + "/%"))
    units = [_shape(dict(r)) for r in rows]
    _attach_consumers(units)
    _attach_fits(units)
    _attach_results(units)
    return units


def register_unit(dest: Destination) -> dict:
    """Scoped scan -> lineage -> fit, for the ONE path a pull just wrote.

    The order is the one ``store_scan.scan_task`` already settled and for the
    same reasons: lineage parses a name and needs the unit row to exist, fit
    reads the lineage columns for params and quant and needs the parse. Run
    the other way round, a freshly pulled model is placed as if its size and
    quant were unknown.

    Scoped, not store-wide: a whole-store walk after a 2 GB download re-stats
    a terabyte to learn one thing, and its MISSING sweep would speak about
    paths this call never looked at.
    """
    from .fitting import detect_hardware, fit, persist_fits, subject_from_unit
    from .lineage import persist_unit_lineage
    from .store_scan import resolve_store, sync_path

    store = resolve_store(dest.store)
    if store is None:
        return {"ok": False, "units": [],
                "reason": f"store {dest.store} is no longer declared"}

    scan = sync_path(store, dest.rel_path)
    units = units_under(dest.store, dest.rel_path)
    if not units:
        return {"ok": False, "scan": scan, "units": [],
                "reason": (f"the scan found no model unit under "
                           f"{dest.rel_path} — the download produced files "
                           f"the unit rules do not read as a model")}

    lineage = persist_unit_lineage([dict(u) for u in units])

    hw = detect_hardware(now=False)
    fitted = 0
    for row in units_under(dest.store, dest.rel_path):
        try:
            persist_fits("unit", row["unit_id"], fit(subject_from_unit(row), hw))
            fitted += 1
        except Exception as exc:          # one bad unit never fails a pull
            log.debug("fit failed for %s: %s", row.get("unit_id"), exc)

    fresh = units_under(dest.store, dest.rel_path)
    return {"ok": True, "scan": scan, "lineage": lineage, "fits": fitted,
            "units": fresh, "unit_ids": [u["unit_id"] for u in fresh]}


# --- the one call every P6 surface renders ----------------------------------


def pull_model(ref: str, *, store: Optional[str] = None,
               to: Optional[str] = None, file: Optional[str] = None,
               dry_run: bool = False, storage=None,
               progress_cb: Optional[Callable[[dict], None]] = None,
               free_gb: Optional[float] = None,
               size_gb: Optional[float] = None,
               entry: Optional[CatalogEntry] = None,
               lister: Optional[Callable[[], list[dict]]] = None,
               version_fetcher: Optional[Callable[[], dict]] = None) -> dict:
    """Pull ``ref`` into a declared model store and register the result.

    ``ref`` is a catalog id the discovery layer already understands — a bare
    HuggingFace repo id, ``huggingface:Org/Name``, or ``civitai:<id>``.

    Order: resolve -> destination -> files -> headroom (WARN, never refuse)
    -> download -> scoped scan -> lineage -> fit -> mark the discovery
    installed. ``dry_run`` stops after headroom and is the only mode that
    reaches no ``_download`` call at all.

    ``free_gb`` and ``size_gb`` are injection points for the headroom
    demonstration and its test: a warning about a disk that is not full has to
    be producible without filling a disk.
    """
    result: dict = {"ok": False, "ref": ref, "dry_run": bool(dry_run)}

    if entry is None:
        from .discovery import resolve_entry

        entry = resolve_entry(ref, storage)
    if entry is None:
        result["reason"] = (f"could not resolve {ref!r} — use '<source>:<ref>' "
                            f"or a HuggingFace repo id")
        return result
    result["catalog_id"] = entry.catalog_id
    result["display_name"] = entry.display_name or entry.catalog_id
    result["modality"] = entry.modality

    try:
        acq = plan(entry)
    except AcquisitionError as exc:
        result["reason"] = str(exc)
        return result
    result["format"] = acq.variant.format
    result["quant"] = acq.variant.quant

    check_credentials(acq, storage)

    dest, why = destination_for(ref, modality=entry.modality,
                                fmt=acq.variant.format, store=store, to=to,
                                name_hint=entry.display_name)
    if dest is None:
        result["reason"] = why
        return result
    result["destination"] = dest.to_dict()

    resolved = resolve_files(entry, acq.variant, file=file, storage=storage,
                             lister=lister, version_fetcher=version_fetcher)
    result["files"] = resolved.to_dict()

    size = (size_gb if size_gb is not None
            else resolved.total_gb
            if resolved.total_gb else (acq.variant.size_gb or None))
    head = headroom_for(dest, size, free_gb=free_gb)
    result["headroom"] = head.to_dict()
    # Logged only for a real pull. A dry run RETURNS the line to whoever asked
    # for it, and logging it as well prints it twice on the command line.
    if head.warn and not dry_run:
        log.warning("%s", head.text)

    if dry_run:
        result["ok"] = True
        result["state"] = "dry-run"
        result["results"] = results_for(entry.modality)
        return result

    dest.path.mkdir(parents=True, exist_ok=True)
    stop = None
    if progress_cb is not None:
        import threading

        stop = threading.Event()
        total = resolved.total_bytes or 0

        def _poll() -> None:
            while not stop.is_set():
                try:
                    dl = sum(f.stat().st_size for f in dest.path.rglob("*")
                             if f.is_file())
                    progress_cb({"downloaded_bytes": dl, "total_bytes": total})
                except Exception:      # progress is best-effort, always
                    pass
                stop.wait(1.0)

        threading.Thread(target=_poll, name="store-pull-progress",
                         daemon=True).start()
    try:
        files = _download_to(entry, acq.variant, dest.path, resolved, storage)
    finally:
        if stop is not None:
            stop.set()

    result["downloaded"] = [{"name": f.name, "size_bytes": f.size_bytes}
                            for f in files]
    result["downloaded_gb"] = round(
        sum(f.size_bytes for f in files) / (1024 ** 3), 2)

    reg = register_unit(dest)
    result["registration"] = reg
    result["units"] = reg.get("units") or []

    installed = False
    try:
        from .discoveries import mark_installed

        installed = bool(mark_installed(entry.catalog_id))
    except Exception as exc:
        log.debug("mark_installed failed for %s: %s", entry.catalog_id, exc)
    result["discovery_marked_installed"] = installed

    consumers: list[str] = []
    for u in result["units"]:
        consumers.extend([c.get("consumer") for c in (u.get("consumers") or [])
                          if c.get("consumer")])
    result["results"] = results_for(entry.modality, consumers)
    result["ok"] = bool(reg.get("ok"))
    result["state"] = "installed" if result["ok"] else "downloaded"
    return result


def _download_to(entry: CatalogEntry, variant: CatalogFormat, dest: Path,
                 resolved: ResolvedFiles, storage) -> list[BundleFile]:
    """Fetch the resolved files into ``dest``.

    Both backends resume. HuggingFace does it itself — ``snapshot_download``
    writes through ``.incomplete`` part files and continues them on a rerun.
    Civitai resumes through :func:`_stream_to_file`'s ``Range`` request, and
    when the server answers 200 instead of 206 the partial is discarded and
    the file restarts rather than being silently corrupted by an append.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if entry.source == "huggingface":
        from huggingface_hub import snapshot_download

        local = snapshot_download(
            repo_id=entry.source_ref.get("repo_id"),
            revision=entry.source_ref.get("revision", "main"),
            allow_patterns=resolved.patterns,
            local_dir=str(dest),
            token=resolve_token("huggingface", storage),
        )
        out: list[BundleFile] = []
        for f in sorted(Path(local).rglob("*")):
            if f.is_file():
                role = ("weights" if f.suffix in (".gguf", ".safetensors", ".bin")
                        else "config")
                out.append(BundleFile(name=f.name, role=role,
                                      size_bytes=f.stat().st_size))
        return out

    if entry.source == "civitai":
        from .credentials import auth_header

        chosen = resolved.civitai_file or {}
        url = chosen.get("downloadUrl")
        if not url:
            raise AcquisitionError(
                f"{entry.catalog_id}: civitai file has no downloadUrl")
        name = chosen.get("name") or f"{_leaf_of(entry.catalog_id)}.safetensors"
        _stream_to_file(url, dest / name, auth_header("civitai", storage),
                        (chosen.get("hashes") or {}).get("SHA256"))
        size = (dest / name).stat().st_size
        role = ("weights" if Path(name).suffix in
                (".safetensors", ".gguf", ".bin", ".ckpt", ".pt") else "config")
        return [BundleFile(name=name, role=role, size_bytes=size)]

    raise AcquisitionError(f"Unsupported source: {entry.source}")


__all__ = [
    "AcquisitionError", "AcquisitionPlan", "Destination", "Headroom",
    "ResolvedFiles", "bucket_for", "bundle_id_for", "check_credentials",
    "check_disk", "check_fit", "destination_for", "headroom_for",
    "headroom_warn_gb", "pick_store", "plan", "pull", "pull_model",
    "register_unit", "resolve_files", "results_for", "results_urls",
    "units_under",
    "select_civitai_file", "select_gguf_patterns", "select_image_checkpoint",
]
