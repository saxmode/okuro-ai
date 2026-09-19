# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Store-aware model inventory — walk the host's configured model
#          stores at UNIT granularity, persist model_units + model_placements,
#          and answer "what is on this box, where, twinned with what, broken".
# index:
#   def configured_stores      (feature gate — absent config means feature off)
#   class ModelStore / ScannedUnit / ScanResult
#   def scan_store             (the walk; mode 'stat' | 'identity')
#   def sync_store             (walk + persist deltas)
#   def refresh                (walk + persist every configured store)
#   def list_units             (cached table, with filters)
#   def twins                  (same identity, or same size, across stores)
#   def inventory              (the one call CLI / API / MCP all render)
# AGENT_HEADER_END -->
"""Store-aware model inventory over the host's configured model stores.

okuro's own model knowledge stopped at its bundle store (``~/.okuro/models/
bundles``) and the ``model_discoveries`` candidate list. The models this host
actually runs live somewhere else entirely — a hot NVMe store and a cold RAID
store, ~3.1 TB across ~582k files — and nothing in okuro could see them.

Three properties shape this module:

**Configured, never assumed.** Store paths are host-specific, so they come from
the same place every other host-specific path in ``ai_models`` comes from: the
``conventions`` block of ``~/.okuro/config.yaml``, read through
:func:`okuro.yu.conventions.get_convention`. No key means no stores means the
feature is off — every entry point returns an explicit *feature off* result and
never raises. okuro does not depend on local inference and must keep working on
a laptop with no model store at all.

**Unit granularity, not file granularity.** A *unit* is what a human calls "a
model": an HF cache dir (``blobs/`` + ``snapshots/``), a self-contained model
dir, a shard group, or one standalone weights file in a shared bucket. See
:func:`_walk` for the rules and why each exists.

**Two modes, because one of them runs on a page load.** ``stat`` mode never
opens a file — it is ``os.scandir`` + ``os.stat`` only, which is what makes it
safe to run live against spinning RAID disks when the inventory page is
opened. ``identity`` mode additionally reads 1 MiB from each end of a unit's
primary file to fingerprint it; that is a background/manual job, never a page
load.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("okuro.ai_models.store_scan")

CONVENTION_KEY = "ai_models.model_stores"
ENV_KEY = "OKURO_MODEL_STORES"

#: Every entry point returns this shape when no store is configured. Explicit,
#: so callers render "not configured" rather than "zero models found" — those
#: are different facts and conflating them is how an empty page gets read as an
#: empty disk.
FEATURE_OFF_REASON = (
    f"no model stores configured — set conventions.{CONVENTION_KEY} in "
    "~/.okuro/config.yaml to a list of {name, path, tier: hot|cold}"
)


def feature_off() -> dict:
    """A FRESH feature-off result each call.

    Built rather than copied from a module constant: callers render it, and a
    shared dict with nested lists is one ``result["stores"].append(...)`` away
    from every later caller seeing the leftovers.
    """
    return {
        "configured": False,
        "reason": FEATURE_OFF_REASON,
        "stores": [],
        "summary": [],
        "units": [],
        "totals": {"units": 0, "size_bytes": 0, "size_gb": 0.0},
    }


MODE_STAT = "stat"
MODE_IDENTITY = "identity"
MODES = (MODE_STAT, MODE_IDENTITY)

# --- What counts as a model file -------------------------------------------
# ``.pt`` is the same torch pickle as ``.pth`` and is recorded as ``pth``;
# the format vocabulary is fixed by migration 150 and is not extended here.
_EXT_FORMAT = {
    ".gguf": "gguf",
    ".safetensors": "safetensors",
    ".bin": "bin",
    ".pth": "pth",
    ".pt": "pth",
    ".onnx": "onnx",
    ".ckpt": "ckpt",
    ".msgpack": "other",
    ".h5": "other",
    ".npz": "other",
    ".gguf.part": "gguf",
}
MODEL_EXTS = frozenset(_EXT_FORMAT)

# A manifest or a component subdir means "these files are ONE model" — the
# dir is a unit and its contents are not independent models.
_MANIFEST_FILES = frozenset({
    "config.json", "model_index.json", "params.json", "configuration.json",
    "bundle.json", "model.safetensors.index.json",
    "pytorch_model.bin.index.json", "diffusion_pytorch_model.safetensors.index.json",
})
_COMPONENT_DIRS = frozenset({
    "vae", "vae_decoder", "vae_encoder", "unet", "transformer", "tokenizer",
    "tokenizer_2", "tokenizer_3", "text_encoder", "text_encoder_2",
    "text_encoder_3", "text_encoders", "diffusion_models", "scheduler",
    "feature_extractor", "image_encoder", "safety_checker", "projector",
    "clip_vision", "controlnet",
})

# Directories that are never model data. ``venvs/`` on the hot store alone is
# 48 GB of torch wheels, and every site-packages dir is full of ``.pth`` files
# that are Python path configs, not torch checkpoints — scanning them would
# invent dozens of phantom units.
#
# ``.no_exist`` earns its place the hard way. HuggingFace writes a ZERO-BYTE
# file there named exactly like the weights it went looking for
# (``.no_exist/<rev>/model.safetensors``) to remember that the file is absent
# upstream. It carries a weights extension, so it entered the weights list, and
# because it is 0 bytes every such unit fingerprinted to sha256("") and 12
# unrelated models collapsed into one "twin group" in the first real run.
_PRUNE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "site-packages", "dist-packages",
    "venv", "venvs", ".venv", "lost+found", ".Trash-1000", ".ipynb_checkpoints",
    ".xet", "xet", ".cache", ".no_exist", ".locks",
})

# Content-addressed blob stores. Neither names its weights: an HF cache's blobs
# are sha256 filenames with no extension, and ollama's are `sha256-<digest>`.
# Without an explicit signature the weights-extension rules see nothing at all
# and the whole store reads as zero bytes of models — the ollama store on this
# host is 103.8 GiB that a first run reported as "outside units".
_HF_CACHE_MARKERS = ("blobs", "snapshots")
_OLLAMA_MARKERS = ("blobs", "manifests")

#: ``model-00002-of-00005.gguf`` → group stem ``model``, index 2, total 5.
_SHARD_RE = re.compile(r"^(?P<stem>.+?)[-_.](?P<idx>\d{1,5})[-_]of[-_](?P<total>\d{1,5})$", re.I)

#: A shard this much smaller than the largest shard of its group is a failed
#: download, not a small tail shard. Measured separator: the Nemotron-3-Super
#: 120B group has a 7.5 MB first shard against a 49.6 GB sibling (0.016%),
#: while a legitimate final shard (qwen2.5-7b q8_0, 175 MB against 3.98 GB)
#: sits at 4.4%.
SHARD_RUNT_RATIO = 0.01

#: A .gguf below this size is a truncated download — the format's own header
#: and metadata block exceed it for any real model. Deliberately NOT applied to
#: other formats; see _classify_health for the five false positives that taught
#: that lesson.
STUB_MAX_BYTES = 1024 * 1024

_IDENTITY_CHUNK = 1024 * 1024  # 1 MiB from each end — never the whole file

#: Every ``*_gb`` field okuro already publishes — sysinfo storage, the bundle
#: registry — divides by 1024**3 and calls the result GB. Dividing by 1e9 here
#: instead would put every number on the inventory page 7.4% away from every
#: other storage number in the product, and from what ``du -h`` prints.
_GiB = 1024 ** 3
_MiB = 1024 ** 2

#: Safety net against symlink loops and pathological trees. Model dirs bottom
#: out around depth 5, but a model shipped as a git checkout drags its own
#: source tree along: one avatar model on this host reaches depth 11 through
#: src/utils/dependencies/…/ops/src/cuda, and a limit of 10 cut it off.
MAX_DEPTH = 16


# --- Configuration ----------------------------------------------------------


@dataclass(frozen=True)
class ModelStore:
    """One configured store: a name, a root path, and a hot/cold tier."""

    name: str
    path: Path
    tier: str = "hot"  # hot | cold

    def free(self) -> tuple[Optional[float], Optional[float]]:
        """``(free GB, total GB)`` of the filesystem holding this store, NOW.

        Measured on every call rather than cached with the unit table: free
        space is the one number on the inventory page that is about the disk
        and not about the scan, and a cached "187 GB free" beside a live unit
        list is how a headroom warning goes stale without looking stale.
        """
        try:
            usage = shutil.disk_usage(str(self.path))
        except OSError:
            return None, None
        return round(usage.free / _GiB, 1), round(usage.total / _GiB, 1)

    def to_dict(self) -> dict:
        free_gb, total_gb = self.free()
        return {"name": self.name, "path": str(self.path), "tier": self.tier,
                "exists": self.path.is_dir(),
                "free_gb": free_gb, "total_gb": total_gb,
                "used_pct": (round((1 - free_gb / total_gb) * 100)
                             if free_gb is not None and total_gb else None)}


def configured_stores() -> list[ModelStore]:
    """Stores declared for this host, or ``[]`` when the feature is off.

    Resolution mirrors :func:`okuro.ai_models.bundle.bundles_root`: the
    ``OKURO_MODEL_STORES`` env var (a JSON list, used by tests and one-off
    runs) wins, then the ``ai_models.model_stores`` convention from
    ``~/.okuro/config.yaml``. Malformed entries are skipped with a warning
    rather than raising — a typo in config must not take okuro down.
    """
    raw: Any = None
    env = os.environ.get(ENV_KEY)
    if env:
        try:
            raw = json.loads(env)
        except ValueError:
            log.warning("%s is not valid JSON — ignoring", ENV_KEY)
            raw = None
    if raw is None:
        from okuro.yu.conventions import get_convention

        raw = get_convention(CONVENTION_KEY, []) or []

    stores: list[ModelStore] = []
    if not isinstance(raw, list):
        log.warning("%s must be a list of {name, path, tier}", CONVENTION_KEY)
        return stores
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning("skipping non-mapping model store entry: %r", entry)
            continue
        path = entry.get("path")
        name = entry.get("name") or (Path(str(path)).name if path else None)
        if not path or not name:
            log.warning("skipping model store without name/path: %r", entry)
            continue
        tier = str(entry.get("tier") or "hot").lower()
        if tier not in ("hot", "cold"):
            log.warning("store %s: tier %r is not hot|cold — treating as hot", name, tier)
            tier = "hot"
        stores.append(ModelStore(str(name), Path(str(path)).expanduser(), tier))
    return stores


def resolve_store(name: str) -> Optional[ModelStore]:
    """One configured store by name, or None."""
    for s in configured_stores():
        if s.name == name:
            return s
    return None


# --- Scan result shapes -----------------------------------------------------


@dataclass
class ScannedUnit:
    """One model unit as found on disk (pre-persistence)."""

    name: str
    store: str
    rel_path: str
    abs_path: str
    size_bytes: int = 0
    alloc_bytes: int = 0
    file_count: int = 0
    format: str = "other"
    layout: str = "file"  # hf-cache | flat-dir | file
    identity: Optional[str] = None
    status: str = "ok"  # ok | broken | missing
    note: Optional[str] = None
    is_symlink: bool = False
    link_target: Optional[str] = None
    target_resolves: bool = True
    #: Largest model file of the unit — what identity mode fingerprints.
    primary_file: Optional[str] = None

    @property
    def unit_id(self) -> str:
        return f"{self.store}:{self.rel_path}"

    def to_dict(self) -> dict:
        d = {
            "unit_id": self.unit_id,
            "name": self.name,
            "store": self.store,
            "rel_path": self.rel_path,
            "abs_path": self.abs_path,
            "size_bytes": self.size_bytes,
            "alloc_bytes": self.alloc_bytes,
            "size_gb": round(self.size_bytes / _GiB, 2),
            "file_count": self.file_count,
            "format": self.format,
            "layout": self.layout,
            "identity": self.identity,
            "status": self.status,
            "note": self.note,
            "is_symlink": self.is_symlink,
            "link_target": self.link_target,
            "target_resolves": self.target_resolves,
        }
        return d


@dataclass
class ScanResult:
    """Everything one store walk produced, plus its own reconciliation."""

    store: str
    path: str
    tier: str
    mode: str
    units: list[ScannedUnit] = field(default_factory=list)
    #: Bytes seen anywhere under the store root, unit or not. The difference
    #: against unit bytes is the honest answer to "did the walk miss anything":
    #: venvs, workflow json, sample outputs and caches all live here.
    tree_bytes: int = 0
    tree_files: int = 0
    #: Bytes behind symlinks that leave the store — counted by neither total.
    linked_out_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def unit_bytes(self) -> int:
        return sum(u.size_bytes for u in self.units)

    def to_dict(self) -> dict:
        return {
            "store": self.store,
            "path": self.path,
            "tier": self.tier,
            "mode": self.mode,
            "units": len(self.units),
            "unit_bytes": self.unit_bytes,
            "unit_gb": round(self.unit_bytes / _GiB, 2),
            "tree_bytes": self.tree_bytes,
            "tree_gb": round(self.tree_bytes / _GiB, 2),
            "tree_files": self.tree_files,
            "non_unit_gb": round((self.tree_bytes - self.unit_bytes) / _GiB, 2),
            "broken": sum(1 for u in self.units if u.status == "broken"),
            "errors": self.errors,
        }


# --- The walk ---------------------------------------------------------------


def _fmt_for(names: Iterable[str]) -> str:
    """Single format if every model file agrees, else ``mixed``/``other``."""
    kinds = {_EXT_FORMAT[e] for e in
             (os.path.splitext(n)[1].lower() for n in names)
             if e in _EXT_FORMAT}
    if not kinds:
        return "other"
    if len(kinds) == 1:
        return kinds.pop()
    return "mixed"


def _is_model_file(name: str) -> bool:
    base = name[:-len(".incomplete")] if name.endswith(".incomplete") else name
    return os.path.splitext(base)[1].lower() in MODEL_EXTS


@dataclass
class _TreeFacts:
    """Everything one subtree walk needs to produce, collected in ONE pass.

    Splitting this into "sum the bytes", "find the biggest weights file" and
    "check the weights for stubs" reads better and costs three walks per unit.
    Against 582k files across the two real stores that is the difference
    between a scan that can run on a page load and one that cannot.
    """

    size: int = 0
    alloc: int = 0
    files: int = 0
    #: (filename, size) for REAL model files only, capped by _MODEL_SAMPLE_CAP.
    #: Symlinks are excluded on purpose — an HF cache's ``snapshots/`` dir is a
    #: farm of links named ``model.safetensors`` pointing at ``blobs/<sha>``,
    #: and each link lstats to ~60 bytes. Feeding those into the stub rule
    #: would report every HF cache dir on the host as broken.
    model_files: list[tuple[str, int]] = field(default_factory=list)
    #: Names carrying a weights extension, links included — format only. This
    #: is the ONLY place an HF cache's format is visible: its blobs are
    #: extension-less by design and only the snapshot links are named.
    format_names: list[str] = field(default_factory=list)
    primary: Optional[str] = None
    #: Starts at 0, not -1: an empty file must never win the "largest weights
    #: file" contest, or it becomes the thing the unit is fingerprinted by.
    primary_size: int = 0
    incomplete: int = 0
    errors: list[str] = field(default_factory=list)


#: Health rules need the shape of a unit's weights, not every name in it. An HF
#: cache dir can hold thousands of blobs; sampling the first few hundred keeps
#: the check O(1) in memory while still seeing every shard of a real model.
_MODEL_SAMPLE_CAP = 500


def _walk_tree(root: str) -> _TreeFacts:
    """One pass over a subtree: bytes, counts, weights shape, primary file.

    Never opens a file and never follows a symlink: a symlinked file counts as
    the link itself (a few bytes), because its real bytes belong to whichever
    store owns the target.
    """
    f = _TreeFacts()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.lstat(p)
            except OSError as exc:  # permission, race, broken mount
                if len(f.errors) < 5:
                    f.errors.append(f"{p}: {exc.strerror}")
                continue
            f.size += st.st_size
            f.alloc += getattr(st, "st_blocks", 0) * 512
            f.files += 1
            if not _is_model_file(fn):
                continue
            if len(f.format_names) < _MODEL_SAMPLE_CAP:
                f.format_names.append(fn)
            if os.path.islink(p):
                continue
            if fn.endswith(".incomplete"):
                f.incomplete += 1
            if len(f.model_files) < _MODEL_SAMPLE_CAP:
                f.model_files.append((fn, st.st_size))
            if st.st_size > f.primary_size:
                f.primary, f.primary_size = p, st.st_size

    # An HF cache dir names nothing: its blobs are content-addressed and
    # extension-less, so the largest blob is the weights file even though no
    # rule above would have picked it.
    if f.primary is None and f.files:
        f.primary = _largest_plain_file(root)
    return f


def _largest_plain_file(root: str) -> Optional[str]:
    """Biggest non-empty regular file under ``root``, whatever it is called."""
    best, best_size = None, 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not os.path.islink(p) and st.st_size > best_size:
                best, best_size = p, st.st_size
    return best


def compute_identity(path: str, unit_size: Optional[int] = None) -> Optional[str]:
    """``"<size>:<sha256(first 1 MiB + last 1 MiB of the primary file)[:32]>"``.

    Never a full-file hash: the largest single file on these stores is 49.6 GB
    and hashing the hot store end to end would be hours of reads to learn what
    a 2 MiB sample plus an exact size already settles.

    ``unit_size`` is the size of the whole UNIT, and passing it is what makes
    this a unit identity rather than a file identity. Without it, a 111 GiB
    blob store and a 42.5 GiB standalone GGUF came out as twins because the
    store's largest blob happened to BE that GGUF — true about one file inside
    them, false about the units, and the twins report is read as a claim about
    units. Two units are twins only if they agree on total bytes and on both
    ends of their primary file.

    Returns None for an empty unit: sha256("") is the same for every empty
    thing on the box, so handing it out as an identity manufactures twins.
    """
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            h.update(fh.read(_IDENTITY_CHUNK))
            if size > _IDENTITY_CHUNK:
                fh.seek(max(0, size - _IDENTITY_CHUNK))
                h.update(fh.read(_IDENTITY_CHUNK))
        total = size if unit_size is None else unit_size
        if total <= 0:
            return None
        return f"{total}:{h.hexdigest()[:32]}"
    except OSError as exc:
        log.warning("identity failed for %s: %s", path, exc)
        return None


def _shard_group(name: str) -> Optional[str]:
    """Group stem for a sharded weights file, else None."""
    base = name[:-len(".incomplete")] if name.endswith(".incomplete") else name
    stem, ext = os.path.splitext(base)
    if ext.lower() not in MODEL_EXTS:
        return None
    m = _SHARD_RE.match(stem)
    return f"{m.group('stem')}{ext}" if m else None


def _blob_store_layout(entries: list[os.DirEntry]) -> Optional[str]:
    """``'hf-cache'`` for an HF cache dir, ``'flat-dir'`` for an ollama store.

    Both are content-addressed, so both need a structural signature rather
    than a filename rule. The ollama store is one unit covering many models —
    coarse, but 103.8 GiB counted beats 103.8 GiB invisible; splitting it by
    its manifests is a P2/P3 concern, not an inventory one.
    """
    names = {e.name for e in entries if e.is_dir(follow_symlinks=False)}
    if all(m in names for m in _HF_CACHE_MARKERS):
        return "hf-cache"
    if all(m in names for m in _OLLAMA_MARKERS):
        return "flat-dir"
    return None


def scan_store(store: ModelStore, mode: str = MODE_STAT) -> ScanResult:
    """Walk one store and return its units.

    ``stat`` mode is dir-stat only — no file is opened, which is what makes it
    safe on the cold RAID store when a page is loaded. ``identity`` mode adds
    the 2 MiB fingerprint per unit on top.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    result = ScanResult(store=store.name, path=str(store.path), tier=store.tier, mode=mode)
    root = str(store.path)
    if not os.path.isdir(root):
        result.errors.append(f"store path does not exist: {root}")
        return result

    real_root = os.path.realpath(root)
    _walk(root, root, real_root, store, result, depth=0)
    del result.errors[20:]

    if mode == MODE_IDENTITY:
        for u in result.units:
            if not _identifiable(u):
                continue
            u.identity = compute_identity(u.primary_file, unit_size=u.size_bytes)
    return result


def _identifiable(u: "ScannedUnit") -> bool:
    """Whether fingerprinting this unit would mean anything.

    A unit that is a symlink OUT of the store owns no bytes here. Following it
    would fingerprint the other store's content and file it under this store,
    which is the same double-count the walk already refuses.
    """
    return bool(u.primary_file) and u.status != "missing" and u.size_bytes > 0 \
        and not (u.is_symlink and u.layout == "file" and u.size_bytes == 0)


def _emit_dir_unit(entry_path: str, store: ModelStore, root: str,
                   layout: str, result: ScanResult,
                   is_symlink: bool = False) -> Optional[ScannedUnit]:
    """Build one unit covering a whole directory subtree, or None if empty.

    An empty leftover directory is not a model in a broken state, it is
    nothing — six of them (abandoned ``split_files`` and ``_dl`` scaffolding)
    showed up as broken units on the real stores and were pure noise. A
    DANGLING SYMLINK is also zero bytes but is emitted, by a different path:
    that one is a consumer pointing at something that is gone, which is a
    finding.
    """
    facts = _walk_tree(entry_path)
    result.errors.extend(facts.errors)
    if facts.size == 0 and not facts.model_files:
        return None
    unit = ScannedUnit(
        name=os.path.basename(entry_path),
        store=store.name,
        rel_path=os.path.relpath(entry_path, root),
        abs_path=entry_path,
        size_bytes=facts.size,
        alloc_bytes=facts.alloc,
        file_count=facts.files,
        format=_fmt_for(facts.format_names),
        layout=layout,
        primary_file=facts.primary,
        is_symlink=is_symlink,
    )
    _classify_health(unit, facts.model_files, facts.incomplete)
    return unit


def _classify_health(unit: ScannedUnit, model_files: list[tuple[str, int]],
                     incomplete: int) -> None:
    """Mark a unit broken when its weights are stubs, runts or partials.

    Three distinct failures, all of which leave a unit that LOOKS present:

    * every weights file is an HF ``.incomplete`` partial — a download that
      died and was never retried;
    * a weights file under 1 MiB beside a multi-GB sibling — a stub;
    * a shard under 1% of its group's largest shard — a partial shard set.
      That last one is what catches the 7.5 MB first shard of the 49.6 GB
      Nemotron-3-Super group while leaving a legitimate 4.4% tail shard alone.
    """
    if not model_files:
        return
    if incomplete and incomplete >= len(model_files):
        unit.status = "broken"
        unit.note = f"all {incomplete} weight files are .incomplete partials"
        return
    shard_runt = _runt_shards(model_files)
    if shard_runt:
        unit.status = "broken"
        unit.note = shard_runt
        return
    # A TINY GGUF, and only a GGUF. Two broader readings of "a model file under
    # 1 MiB is a stub" were each run against the real stores and each produced
    # only false positives — five in total, no true ones. A deepfloyd
    # watermarker is 16 KiB, an XTTS mel-stats tensor smaller still, an ONNX
    # graph whose tensors live in a sidecar is just under 1 MiB, a speechbrain
    # label encoder is 120 KiB, and an ai-toolkit keymap named .safetensors
    # holds no tensors at all. Every one of those is a working file, and
    # BROKEN is a label someone deletes on.
    #
    # GGUF is the exception that survives contact with the data: the format's
    # own header and metadata block exceed 1 MiB for any real model, so a
    # sub-1-MiB .gguf is a truncated download with no benign reading. The
    # damaged shard this rule was written for (7.5 MiB against a 49.6 GiB
    # sibling) is caught above, by ratio, and does not depend on this.
    name, biggest = max(model_files, key=lambda kv: kv[1])
    if 0 < biggest < STUB_MAX_BYTES and name.lower().endswith(".gguf"):
        unit.status = "broken"
        unit.note = (f"{name} is {biggest / _MiB:.2f} MiB — a GGUF that small "
                     f"is a truncated download, not a model")


def _runt_shards(sizes: list[tuple[str, int]]) -> Optional[str]:
    """A shard under 1% of its group's largest shard is a failed download."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for fn, size in sizes:
        g = _shard_group(fn)
        if g:
            groups.setdefault(g, []).append((fn, size))
    for g, members in groups.items():
        if len(members) < 2:
            continue
        biggest = max(s for _, s in members)
        for fn, size in members:
            if biggest > 0 and size < biggest * SHARD_RUNT_RATIO:
                return (f"shard {fn} is {size / _MiB:.1f} MiB, "
                        f"{size / biggest * 100:.2f}% of the largest shard in "
                        f"{g} — failed download")
    return None


def _emit_file_units(dirpath: str, files: list[os.DirEntry], store: ModelStore,
                     root: str, result: ScanResult) -> list[ScannedUnit]:
    """Standalone weights in a shared bucket — one unit per model, shards joined."""
    groups: dict[str, list[os.DirEntry]] = {}
    for e in files:
        key = _shard_group(e.name) or e.name
        groups.setdefault(key, []).append(e)

    units: list[ScannedUnit] = []
    for key, members in groups.items():
        size = alloc = 0
        sizes: list[tuple[str, int]] = []
        primary: Optional[str] = None
        primary_size = -1
        symlinked = False
        link_target: Optional[str] = None
        resolves = True
        for e in members:
            p = e.path
            try:
                st = os.lstat(p)
            except OSError as exc:
                result.errors.append(f"{p}: {exc.strerror}")
                continue
            if e.is_symlink():
                symlinked = True
                try:
                    link_target = os.readlink(p)
                except OSError:
                    link_target = None
                resolves = os.path.exists(p)
                if resolves:
                    try:
                        tst = os.stat(p)
                        sizes.append((e.name, tst.st_size))
                        if tst.st_size > primary_size:
                            primary, primary_size = p, tst.st_size
                    except OSError:
                        pass
                # Bytes behind the link belong to whatever store owns the
                # target — never added to this store's totals.
                continue
            size += st.st_size
            alloc += getattr(st, "st_blocks", 0) * 512
            sizes.append((e.name, st.st_size))
            if st.st_size > primary_size:
                primary, primary_size = p, st.st_size

        anchor = members[0].path
        rel = os.path.relpath(anchor, root)
        if len(members) > 1:
            rel = os.path.join(os.path.relpath(dirpath, root), key)
        unit = ScannedUnit(
            name=key,
            store=store.name,
            rel_path=rel,
            abs_path=anchor if len(members) == 1 else os.path.join(dirpath, key),
            size_bytes=size,
            alloc_bytes=alloc,
            file_count=len(members),
            format=_fmt_for([e.name for e in members]),
            layout="file",
            primary_file=primary,
            is_symlink=symlinked,
            link_target=link_target,
            target_resolves=resolves,
        )
        if symlinked and not resolves:
            unit.status = "broken"
            unit.note = f"dangling symlink -> {link_target}"
        else:
            _classify_health(
                unit, sizes,
                sum(1 for e in members if e.name.endswith(".incomplete")))
        units.append(unit)
    return units


def _walk(dirpath: str, root: str, real_root: str, store: ModelStore,
          result: ScanResult, depth: int) -> None:
    """Classify one directory, emit its units, and descend where warranted.

    Decision order, and why each rule is there:

    1. **Dangling symlink** — a link whose target is gone is neither a dir nor
       a file, so without an explicit branch it vanishes from the inventory
       silently. A link written the way a CONTAINER sees its mount is exactly
       that: it resolves nowhere on the host, and this host has one.
    2. **Symlink leaving the store** — recorded as a unit, never walked for
       size. A hot-store entry pointing into the cold store is real (one on
       this host is 104 GB); following it would count those bytes twice as
       soon as both stores are configured.
    3. **HF cache dir** (``blobs/`` + ``snapshots/``) — one unit; its blobs are
       shards of one model and its snapshots are symlink farms into them.
    4. **Manifest or component subdir** — ``config.json``, ``model_index.json``
       or a ``vae/``/``text_encoders/`` child means these files are ONE model
       (``video/qwen-image-2512`` has four component dirs and no loose weights).
    5. **Bucket** — direct weights with no manifest and no component dir are
       independent models: ``text/gguf/`` holds 20+ unrelated GGUFs. Emit one
       unit per file (shards joined), then keep descending, because a bucket
       can also hold subdirs that are units in their own right.
    6. **Leaf dir with weights** — one unit.
    """
    if depth > MAX_DEPTH:
        result.errors.append(f"depth limit at {dirpath}")
        return
    try:
        entries = list(os.scandir(dirpath))
    except OSError as exc:
        result.errors.append(f"{dirpath}: {exc.strerror}")
        return

    subdirs: list[os.DirEntry] = []
    files: list[os.DirEntry] = []
    model_files: list[os.DirEntry] = []

    for e in entries:
        if e.name in _PRUNE_DIRS or e.name.startswith(".Trash"):
            continue
        if e.is_symlink() and not os.path.exists(e.path):
            result.units.append(_dangling_unit(e, store, root))
            continue
        if e.is_dir(follow_symlinks=True):
            if e.is_symlink():
                target = os.path.realpath(e.path)
                if not target.startswith(real_root + os.sep) and target != real_root:
                    result.units.append(_linked_out_unit(e, store, root))
                    continue
            subdirs.append(e)
        else:
            files.append(e)
            if _is_model_file(e.name):
                model_files.append(e)

    # --- Is this whole directory ONE unit? Decided on names alone, before any
    # stat, so the subtree is walked exactly once either way.
    names = [e.name for e in files]
    layout: Optional[str] = None
    if depth > 0:
        blob_layout = _blob_store_layout(entries)
        if blob_layout:
            layout = blob_layout
        elif any(n in _MANIFEST_FILES for n in names):
            layout = "flat-dir"
        elif depth > 1 and subdirs and all(
                d.name.lower() in _COMPONENT_DIRS for d in subdirs):
            # EVERY subdir is a component, not merely one of them. Half these
            # names (vae, controlnet, text_encoders, diffusion_models) are also
            # ComfyUI BUCKET names, so `any` collapsed a whole 419 GB category
            # directory into a single "model" the first time this ran — the
            # category happened to contain a controlnet/ bucket. A dir whose
            # subdirs are *only* components is one model split into its parts;
            # a dir that also holds loras/ or checkpoints/ is a bucket tree.
            # The depth guard keeps a top-level category out of it either way.
            layout = "flat-dir"
        elif model_files and not subdirs:
            groups = {(_shard_group(e.name) or e.name) for e in model_files}
            if len(groups) == 1:
                # A leaf dir holding exactly one model — the dir is the unit,
                # which is how every prior store audit named it.
                layout = "flat-dir"

    if layout:
        unit = _emit_dir_unit(dirpath, store, root, layout, result)
        if unit is not None:
            result.units.append(unit)
            result.tree_bytes += unit.size_bytes
            result.tree_files += unit.file_count
        return

    # Not a unit: count this level's own files toward the store total (they are
    # the workflow json, READMEs and caches that live between units), then let
    # the bucket rule turn any loose weights into units, then descend.
    for e in files:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError as exc:
            result.errors.append(f"{e.path}: {exc.strerror}")
            continue
        result.tree_bytes += st.st_size
        result.tree_files += 1

    if model_files:
        result.units.extend(_emit_file_units(dirpath, model_files, store, root, result))

    for sub in subdirs:
        _walk(sub.path, root, real_root, store, result, depth + 1)


def _dangling_unit(e: os.DirEntry, store: ModelStore, root: str) -> ScannedUnit:
    try:
        target = os.readlink(e.path)
    except OSError:
        target = None
    return ScannedUnit(
        name=e.name,
        store=store.name,
        rel_path=os.path.relpath(e.path, root),
        abs_path=e.path,
        layout="file",
        status="broken",
        note=f"dangling symlink -> {target}",
        is_symlink=True,
        link_target=target,
        target_resolves=False,
    )


def _linked_out_unit(e: os.DirEntry, store: ModelStore, root: str) -> ScannedUnit:
    """A symlink to a directory outside this store — recorded, never summed."""
    try:
        target = os.readlink(e.path)
    except OSError:
        target = None
    return ScannedUnit(
        name=e.name,
        store=store.name,
        rel_path=os.path.relpath(e.path, root),
        abs_path=e.path,
        size_bytes=0,
        layout="file",
        status="ok",
        note=f"symlink out of store -> {target} (bytes counted where they live)",
        is_symlink=True,
        link_target=target,
        target_resolves=True,
    )


# --- Persistence ------------------------------------------------------------


def persist_unit(u: ScannedUnit, db=None) -> bool:
    """Upsert ONE scanned unit + its placement. True when the row is new.

    Factored out of :func:`sync_store` because P6 pulls one model into one
    directory and needs exactly this write and nothing else. The alternative —
    a whole-store walk after every download — would also run the MISSING sweep,
    and a scoped scan has no business declaring anything missing: it looked at
    one path, so the only thing it can honestly say is what it found there.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()
    existed = db.fetchone(
        "SELECT unit_id FROM model_units WHERE unit_id = ?", (u.unit_id,))
    db.execute(
        """
        INSERT INTO model_units (
            unit_id, name, store, rel_path, size_bytes, alloc_bytes,
            file_count, format, layout, identity, status, note,
            first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  datetime('now'), datetime('now'))
        ON CONFLICT(unit_id) DO UPDATE SET
            name        = excluded.name,
            size_bytes  = excluded.size_bytes,
            alloc_bytes = excluded.alloc_bytes,
            file_count  = excluded.file_count,
            format      = excluded.format,
            layout      = excluded.layout,
            -- a stat-mode rescan must not erase an identity a previous
            -- identity pass computed
            identity    = COALESCE(excluded.identity, model_units.identity),
            status      = excluded.status,
            note        = excluded.note,
            last_seen   = datetime('now')
        """,
        (u.unit_id, u.name, u.store, u.rel_path, u.size_bytes, u.alloc_bytes,
         u.file_count, u.format, u.layout, u.identity, u.status, u.note),
    )
    db.execute(
        """
        INSERT INTO model_placements (
            unit_id, store, abs_path, is_symlink, link_target, target_resolves
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(unit_id, abs_path) DO UPDATE SET
            is_symlink      = excluded.is_symlink,
            link_target     = excluded.link_target,
            target_resolves = excluded.target_resolves
        """,
        (u.unit_id, u.store, u.abs_path, 1 if u.is_symlink else 0,
         u.link_target, 1 if u.target_resolves else 0),
    )
    return not existed


def scan_path(store: ModelStore, rel_path: str,
              mode: str = MODE_IDENTITY) -> ScanResult:
    """Walk ONE subtree of a store and return the units under it.

    The scoped half of :func:`scan_store`: same ``_walk``, same unit rules,
    same health rules, started lower down the tree. ``depth`` is seeded with
    the subtree's own depth so the store-wide depth limit still means what it
    says.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    result = ScanResult(store=store.name, path=str(store.path), tier=store.tier,
                        mode=mode)
    root = str(store.path)
    rel = str(rel_path or "").strip("/")
    target = os.path.join(root, rel) if rel else root
    if not os.path.isdir(target):
        result.errors.append(f"path is not a directory: {target}")
        return result

    real_root = os.path.realpath(root)
    depth = len([p for p in rel.split("/") if p])
    _walk(target, root, real_root, store, result, depth=depth)
    del result.errors[20:]

    if mode == MODE_IDENTITY:
        for u in result.units:
            if not _identifiable(u):
                continue
            u.identity = compute_identity(u.primary_file, unit_size=u.size_bytes)
    return result


def sync_path(store: ModelStore, rel_path: str,
              mode: str = MODE_IDENTITY) -> dict:
    """Scan ONE subtree and persist exactly the units it found.

    No MISSING sweep, by design — see :func:`persist_unit`.
    """
    res = scan_path(store, rel_path, mode=mode)
    added = updated = 0
    for u in res.units:
        if persist_unit(u):
            added += 1
        else:
            updated += 1
    out = res.to_dict()
    out.update({"added": added, "updated": updated, "missing": 0,
                "scoped_to": str(rel_path or "").strip("/")})
    return out


def sync_store(store: ModelStore, mode: str = MODE_STAT) -> dict:
    """Walk one store and merge the deltas into ``model_units``.

    Units that were there and are not any more become ``status='missing'``
    rather than disappearing: a model that vanished is a fact worth keeping,
    and a consumer map (P2) will want to point at it.
    """
    from okuro.db import get_db

    res = scan_store(store, mode=mode)
    db = get_db()
    seen: list[str] = []
    added = updated = 0

    for u in res.units:
        seen.append(u.unit_id)
        if persist_unit(u, db=db):
            added += 1
        else:
            updated += 1

    missing = 0
    known = db.fetchall(
        "SELECT unit_id FROM model_units WHERE store = ? AND status != 'missing'",
        (store.name,))
    seen_set = set(seen)
    for row in known:
        if row["unit_id"] not in seen_set:
            db.execute(
                "UPDATE model_units SET status = 'missing', note = ?, "
                "last_seen = datetime('now') WHERE unit_id = ?",
                ("not present in the last scan", row["unit_id"]))
            missing += 1

    out = res.to_dict()
    out.update({"added": added, "updated": updated, "missing": missing})
    return out


def refresh(mode: str = MODE_STAT, store: Optional[str] = None,
            new_only: bool = False) -> dict:
    """Walk every configured store (or one) and persist the deltas.

    ``new_only`` is what the daemon task uses for identity mode: fingerprinting
    is the expensive half, and a unit's bytes do not change under it, so only
    units with no identity yet are worth the reads.
    """
    stores = configured_stores()
    if not stores:
        return feature_off()
    if store:
        stores = [s for s in stores if s.name == store]
        if not stores:
            return {"configured": True, "error": f"unknown store: {store}",
                    "stores": [s.name for s in configured_stores()]}

    results = []
    for s in stores:
        if new_only and mode == MODE_IDENTITY:
            results.append(_identity_pass_new_only(s))
        else:
            results.append(sync_store(s, mode=mode))
    return {"configured": True, "mode": mode, "stores": results}


def _identity_pass_new_only(store: ModelStore) -> dict:
    """Fingerprint only the units of ``store`` that have no identity yet."""
    from okuro.db import get_db

    db = get_db()
    # size_bytes > 0 skips empty units AND the symlinks that leave the store:
    # both would otherwise be fingerprinted by content they do not own.
    rows = db.fetchall(
        "SELECT unit_id, rel_path, size_bytes FROM model_units "
        "WHERE store = ? AND identity IS NULL AND status != 'missing' "
        "AND size_bytes > 0",
        (store.name,))
    done = skipped = 0
    for row in rows:
        abs_path = str(store.path / row["rel_path"])
        if os.path.islink(abs_path):
            skipped += 1
            continue
        primary = (_walk_tree(abs_path).primary if os.path.isdir(abs_path)
                   else abs_path)
        if not primary or not os.path.exists(primary):
            skipped += 1
            continue
        ident = compute_identity(primary, unit_size=row["size_bytes"])
        if not ident:
            skipped += 1
            continue
        db.execute("UPDATE model_units SET identity = ? WHERE unit_id = ?",
                   (ident, row["unit_id"]))
        done += 1
    return {"store": store.name, "mode": MODE_IDENTITY, "new_only": True,
            "identified": done, "skipped": skipped, "candidates": len(rows)}


# --- Queries ----------------------------------------------------------------

_COLS = ("unit_id", "name", "store", "rel_path", "size_bytes", "alloc_bytes",
         "file_count", "format", "layout", "identity", "status", "note",
         "first_seen", "last_seen",
         # P3 lineage (migration 152). Read, never written here: the parse is
         # owned by ai_models.lineage, and a second writer is how two answers
         # to "what family is this" start existing.
         "family", "version", "params_total_b", "params_active_b", "quant",
         "variant_tags", "author")


def list_units(*, store: Optional[str] = None, fmt: Optional[str] = None,
               status: Optional[str] = None, limit: int = 1000) -> list[dict]:
    """Cached unit rows, largest first."""
    from okuro.db import get_db

    clauses: list[str] = []
    params: list[Any] = []
    if store:
        clauses.append("store = ?")
        params.append(store)
    if fmt:
        clauses.append("format = ?")
        params.append(fmt)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = get_db().fetchall(
        f"SELECT {', '.join(_COLS)} FROM model_units{where} "
        f"ORDER BY size_bytes DESC LIMIT ?", tuple(params))
    return [_shape(dict(r)) for r in rows]


def _shape(row: dict) -> dict:
    row["size_gb"] = round((row.get("size_bytes") or 0) / _GiB, 2)
    tags = row.get("variant_tags")
    if isinstance(tags, str):
        try:
            row["variant_tags"] = json.loads(tags)
        except ValueError:
            row["variant_tags"] = []
    elif tags is None:
        row["variant_tags"] = []
    return row


def twins(by: str = "identity") -> list[dict]:
    """Units that appear on more than one store.

    ``by='identity'`` is the proof: same size AND same 2 MiB fingerprint. It
    only finds a pair when BOTH sides have been through identity mode, which
    is why ``by='size'`` exists — a filename+size join across stores is the
    same PROBABLE-duplicate evidence the manual store audits used, available
    from a stat-only scan, and it is labelled probable rather than proven.
    """
    from okuro.db import get_db

    db = get_db()
    if by == "identity":
        rows = db.fetchall(
            """
            SELECT identity, COUNT(*) AS copies,
                   GROUP_CONCAT(store) AS stores,
                   GROUP_CONCAT(unit_id, ' | ') AS unit_ids,
                   MAX(size_bytes) AS size_bytes
            FROM model_units
            WHERE identity IS NOT NULL AND status != 'missing'
            GROUP BY identity
            HAVING COUNT(DISTINCT store) > 1
            ORDER BY size_bytes DESC
            """)
        return [dict(r) | {"evidence": "identity", "size_gb": round((r["size_bytes"] or 0) / _GiB, 2)}
                for r in rows]
    if by == "size":
        rows = db.fetchall(
            """
            SELECT name, size_bytes, COUNT(*) AS copies,
                   GROUP_CONCAT(store) AS stores,
                   GROUP_CONCAT(unit_id, ' | ') AS unit_ids
            FROM model_units
            WHERE size_bytes > 0 AND status != 'missing'
            GROUP BY name, size_bytes
            HAVING COUNT(DISTINCT store) > 1
            ORDER BY size_bytes DESC
            """)
        return [dict(r) | {"evidence": "probable (name+size, not fingerprinted)",
                           "size_gb": round((r["size_bytes"] or 0) / _GiB, 2)}
                for r in rows]
    raise ValueError("by must be 'identity' or 'size'")


def store_summary() -> list[dict]:
    """Per-store counts, bytes and last scan time, from the cached table."""
    from okuro.db import get_db

    rows = get_db().fetchall(
        """
        SELECT store,
               COUNT(*) AS units,
               SUM(size_bytes) AS size_bytes,
               SUM(CASE WHEN status = 'broken' THEN 1 ELSE 0 END) AS broken,
               SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing,
               SUM(CASE WHEN identity IS NOT NULL THEN 1 ELSE 0 END) AS identified,
               MAX(last_seen) AS last_scanned
        FROM model_units GROUP BY store ORDER BY store
        """)
    out = []
    for r in rows:
        d = dict(r)
        d["size_gb"] = round((d.get("size_bytes") or 0) / _GiB, 2)
        out.append(d)
    return out


def _attach_consumers(units: list[dict]) -> None:
    """Add ``consumers`` and ``tier`` to each unit row, in ONE join.

    P2 built the consumer map; this is the only place the inventory reads it.
    It is a query against the table the consumer scan already wrote, never a
    second walk — two scans of the same trees producing two answers is how an
    inventory page and a consumer page start disagreeing in front of the user.

    A unit with no consumer row gets ``consumers: []`` and ``tier: None``,
    which says "no DECLARED consumer names it" and never "nothing uses it":
    the consumer feature may simply be off, and a loader that builds its path
    at runtime leaves no reference to find either way.
    """
    if not units:
        return
    try:
        from okuro.ai_models.consumers import unit_consumers
    except ImportError:  # pragma: no cover — consumers.py ships beside this
        return
    try:
        owners = unit_consumers([u["unit_id"] for u in units])
    except Exception as exc:  # a missing table must not break the inventory
        log.debug("consumer join unavailable: %s", exc)
        owners = {}
    for u in units:
        entry = owners.get(u["unit_id"])
        u["consumers"] = entry["consumers"] if entry else []
        u["tier"] = entry["tier"] if entry else None
        u["consumer_reachable"] = entry["reachable"] if entry else None


def _attach_fits(units: list[dict]) -> None:
    """Add ``best_fit`` to each unit row, in ONE join.

    P3 computed the placement space; this reads what it stored. Same contract
    as the consumer join above and for the same reason: one query for the whole
    page, never a fit per row, and never a second computation that could
    disagree with the stored one.

    A unit with no stored fit gets ``best_fit: None``, which says "not computed
    yet" — the scan may not have run — and never "does not run here". Those are
    different facts and conflating them is how a page tells the user to delete
    something that works.
    """
    if not units:
        return
    try:
        from okuro.ai_models.fitting import best_fits
    except ImportError:  # pragma: no cover — fitting.py ships beside this
        return
    try:
        fits = best_fits([u["unit_id"] for u in units])
    except Exception as exc:  # a missing table must not break the inventory
        log.debug("fit join unavailable: %s", exc)
        fits = {}
    for u in units:
        u["best_fit"] = fits.get(u["unit_id"])


def _attach_results(units: list[dict]) -> None:
    """Add ``results`` — where a person goes to SEE what this model produces.

    A declared CONSUMER answers it better than the modality does, so the
    consumer join has to have run first. An empty list means the host has
    declared no URL for either, which is the honest answer and not a guess at
    a port.
    """
    if not units:
        return
    try:
        from okuro.ai_models.acquire import results_for
        from okuro.ai_models.fitting import unit_modality
    except ImportError:  # pragma: no cover — both ship beside this
        return
    for u in units:
        try:
            u["results"] = results_for(unit_modality(u), u.get("consumers") or [])
        except Exception as exc:  # a missing URL table never breaks a page
            log.debug("results link unavailable for %s: %s",
                      u.get("unit_id"), exc)
            u["results"] = []


def inventory(*, store: Optional[str] = None, fmt: Optional[str] = None,
              status: Optional[str] = None, refresh_mode: Optional[str] = None,
              want_twins: bool = False, limit: int = 1000) -> dict:
    """The one call the CLI, the API and the MCP tool all render.

    ``refresh_mode='stat'`` runs the live dir-stat walk first and merges the
    deltas — this is the page-load path for the cold store. With no
    ``refresh_mode`` the cached table is returned with ``last_scanned`` per
    store, so opening the page costs one query.
    """
    stores = configured_stores()
    if not stores:
        return feature_off()

    scan: Optional[dict] = None
    if refresh_mode:
        scan = refresh(mode=refresh_mode, store=store)

    units = list_units(store=store, fmt=fmt, status=status, limit=limit)
    _attach_consumers(units)
    _attach_fits(units)
    _attach_results(units)
    out: dict[str, Any] = {
        "configured": True,
        "stores": [s.to_dict() for s in stores],
        "summary": store_summary(),
        "units": units,
        "totals": {
            "units": len(units),
            "size_bytes": sum(u["size_bytes"] for u in units),
            "size_gb": round(sum(u["size_bytes"] for u in units) / _GiB, 2),
        },
    }
    if scan is not None:
        out["scan"] = scan
    if want_twins:
        out["twins"] = {"identity": twins("identity"), "size": twins("size")}
    return out


def scan_task() -> dict:
    """Daemon entry point — hot stores stat+identity, cold stores identity.

    New units only in both cases: the fingerprint of a unit that already has
    one does not change, and re-reading 1.5 TB weekly to confirm that would be
    the most expensive no-op in the system.
    """
    stores = configured_stores()
    if not stores:
        return {"status": "skipped", **feature_off()}

    from datetime import datetime

    weekly = datetime.now().weekday() == 0  # Monday carries the cold pass
    results = []
    for s in stores:
        if s.tier == "hot":
            results.append(sync_store(s, mode=MODE_STAT))
            results.append(_identity_pass_new_only(s))
        elif weekly:
            results.append(sync_store(s, mode=MODE_STAT))
            results.append(_identity_pass_new_only(s))
        else:
            results.append({"store": s.name, "skipped": "cold store — weekly (Monday) only"})

    # The consumer scan runs HERE, inside this task and after the store walk —
    # not as a second daemon task. Ordering is the reason: a consumer
    # reference resolves against `model_units`, so a consumer scan that
    # overtook the store scan would resolve against last week's units and
    # report this week's models as dead refs. One task, one order, no race.
    consumers_result: dict
    try:
        from okuro.ai_models.consumers import scan_task as consumer_scan_task

        consumers_result = consumer_scan_task()
    except Exception as exc:  # a consumer walk must never fail the store scan
        log.exception("consumer scan failed")
        consumers_result = {"status": "error", "error": str(exc)}

    # Then lineage, then fit — THIRD and FOURTH in one task, same reasoning as
    # the consumer pass. Lineage parses a unit's name, so it has to see the
    # units this walk just added; fit reads the lineage columns for params and
    # quant, so it has to run after lineage or it places a model whose size it
    # does not know yet. Three separate daemon tasks could interleave in any
    # order and produce exactly those two wrong answers.
    lineage_result: dict
    try:
        from okuro.ai_models.lineage import persist_unit_lineage

        lineage_result = {"status": "ok", **persist_unit_lineage()}
    except Exception as exc:
        log.exception("lineage pass failed")
        lineage_result = {"status": "error", "error": str(exc)}

    fits_result: dict
    try:
        from okuro.ai_models.fitting import fit_all_units

        # Idle fit only, and only for units without one. `now` is a question
        # about this instant and belongs to a page load or a CLI flag, not to
        # a row written at 04:30 and read at noon.
        fits_result = {"status": "ok", **fit_all_units(now=False, only_new=True)}
    except Exception as exc:
        log.exception("fit pass failed")
        fits_result = {"status": "error", "error": str(exc)}

    return {"status": "ok", "stores": results, "consumers": consumers_result,
            "lineage": lineage_result, "fits": fits_result}


__all__ = [
    "CONVENTION_KEY",
    "FEATURE_OFF_REASON",
    "feature_off",
    "MODE_STAT",
    "MODE_IDENTITY",
    "ModelStore",
    "ScannedUnit",
    "ScanResult",
    "compute_identity",
    "configured_stores",
    "inventory",
    "list_units",
    "refresh",
    "resolve_store",
    "persist_unit",
    "scan_path",
    "scan_store",
    "sync_path",
    "scan_task",
    "store_summary",
    "sync_store",
    "twins",
]
