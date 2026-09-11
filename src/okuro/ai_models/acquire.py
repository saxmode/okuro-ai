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
from typing import Callable, Optional

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
