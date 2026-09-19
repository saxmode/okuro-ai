# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Code-derived model consumer map — walk the host's declared consumer
#          roots, extract every model reference with its file:line, resolve it
#          against the model_units inventory, and record whether the consumer
#          that names it can actually reach it.
# index:
#   def configured_roots       (feature gate — absent config means feature off)
#   class ConsumerRoot / Ref / ScannedRef
#   def extract_refs           (the per-kind extractors)
#   class Resolver             (ref -> unit_id, with the token set)
#   def scan_root / scan_all   (walk + resolve)
#   def sync                   (walk + persist deltas)
#   def list_consumers         (cached table, with filters)
#   def unit_consumers         (the JOIN model_inventory renders)
#   def root_for               (the root owning a config file — P5's mount)
#   def consumer_map           (the one call CLI / API / MCP all render)
# AGENT_HEADER_END -->
"""Which tool on this host uses which model, proven by a ``file:line``.

Nothing on this box could answer that. tm-inference's registry carries
filename, alias, size and ``fits_on`` and no consumer field at all; the GPU
lease broker's ``purpose`` is free text cleared the moment the lease ends;
ComfyUI's ``extra_model_paths.yaml`` maps buckets to *paths*, never to *tools*.
The only durable model-to-tool record that existed was a hand-written audit,
which is stale the day after it is written.

Four properties shape this module.

**A citation, not an assertion.** A row is one reference at one ``file:line``.
"tm-mitate uses Llama-3.3-70B" is something a person then has to go and check;
"tm-mitate names it at ``lib/lifecycle.py:50``" is the check. A swap plan has
to rewrite that exact line, so the line number is data.

**An unresolved reference is the finding, not an error.** A consumer pointing
at a file that is not on this host is a dead ref, and dropping those rows would
delete the one thing a consumer map is uniquely able to notice. They are kept
with ``unit_id`` NULL and ``note='dead-ref'``.

**Existing is not reachable.** A unit can sit on the host and be invisible to
the consumer that names it, because the consumer is a container and the file is
behind a symlink that leaves the bind mount. Only the consumer's own mount can
decide that, so reachability is computed per consumer, never per unit.

**Configured, never assumed.** Consumer roots are host-specific and come from
the same place P1's store paths come from: the ``conventions`` block of
``~/.okuro/config.yaml``. No key means the feature is off and every surface
says so.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("okuro.ai_models.consumers")

ROOTS_KEY = "ai_models.consumer_roots"
PROTECTED_KEY = "ai_models.protected_consumers"
ROOTS_ENV = "OKURO_CONSUMER_ROOTS"
PROTECTED_ENV = "OKURO_PROTECTED_CONSUMERS"

FEATURE_OFF_REASON = (
    f"no consumer roots configured — set conventions.{ROOTS_KEY} in "
    "~/.okuro/config.yaml to a list of {name, path, kind: systemd|compose|"
    "workflows|python|yaml|registry}"
)

KINDS = ("systemd", "compose", "workflows", "python", "yaml", "registry")
TIERS = ("PROTECTED", "ACTIVE", "ARCHIVE")
STATES = ("running", "configured", "stopped", "script", "unknown")


def feature_off() -> dict:
    """A FRESH feature-off result each call.

    Built rather than copied from a module constant, for the reason P1 learned
    the hard way: callers render this dict, and one ``result["consumers"]
    .append(...)`` on a shared object leaks into every later caller.
    """
    return {
        "configured": False,
        "reason": FEATURE_OFF_REASON,
        "roots": [],
        "consumers": [],
        "rows": [],
        "totals": {"rows": 0, "consumers": 0, "dead": 0, "unreachable": 0},
    }


# --- What a model reference looks like in a config file ---------------------

#: Weights extensions worth citing. Kept in step with store_scan's vocabulary;
#: ``.pt`` is the same torch pickle as ``.pth``.
_REF_EXTS = ("gguf", "safetensors", "ckpt", "pth", "pt", "onnx", "bin", "msgpack")
_EXT_ALT = "|".join(_REF_EXTS)

#: A path-shaped model reference.
#:
#: The character class ALLOWS ``/``, and that is the whole point. A previous
#: audit used ``[\w.\-]+\.(safetensors|gguf)`` and captured the workflow
#: reference ``Wan2.2-T2V-A14B/Wan2.2_14B_Merged.safetensors`` as the bare
#: ``Wan2.2_14B_Merged.safetensors``, which then matched no unit whose name is
#: the DIRECTORY. 120 GB was falsely reported as unreferenced, including a
#: 52.9 GB unit that six live workflows load.
#: The optional leading ``/`` matters as much as the inner ones. Without it an
#: absolute reference was recorded as ``mnt/raid/.../lora.safetensors`` — the
#: same path with its root removed, which is not the path anyone wrote and not
#: a path that opens. The row is a CITATION; a citation that has silently lost
#: a character is worse than no row.
_PATH_REF = re.compile(
    rf"(?P<ref>/?(?:[A-Za-z0-9_.~+-]+/)*[A-Za-z0-9_.~+-]+\.(?:{_EXT_ALT}))\b")

#: ``Org/Name`` and ``Org--Name`` as HuggingFace writes them. Two segments,
#: no extension, at least one digit-or-case boundary — deliberately narrow,
#: because a two-segment slash path matches half of every source file.
_HF_REF = re.compile(
    r"(?<![\w./-])(?P<org>[A-Za-z0-9][\w.-]{1,38})(?:--|/)"
    r"(?P<name>[A-Za-z0-9][\w.-]{2,60})(?![\w/-])")

#: Loader keys whose value is ALWAYS a local weights file, extension or not.
#: These are ComfyUI node inputs: the value is a name inside a model bucket
#: and it cannot be anything else.
_LOADER_KEYS = (
    "ckpt_name", "unet_name", "vae_name", "lora_name", "gguf_name",
    "control_net_name", "controlnet_name", "style_model_name",
    "clip_name", "clip_name1", "clip_name2", "clip_name3", "clip_name4",
)
#: Keys whose value is SOMETIMES a local model and often an API model name.
#: Measured: one consumer's card data carries `"model": "claude-opus-4-6"`
#: several hundred times — a hosted model, not a file on this disk. Treating
#: these as firm made dead refs the majority of the table and buried the two
#: that matter. They are PROVISIONAL: kept only where they resolve.
_SOFT_MODEL_KEYS = ("model_path", "model_name", "checkpoint", "model")

_LOADER_REF = re.compile(
    r"[\"']?(?P<key>" + "|".join(sorted(_LOADER_KEYS, key=len, reverse=True))
    + r")[\"']?\s*[:=]\s*[\"'](?P<ref>[^\"'\n]{2,200})[\"']")
_SOFT_REF = re.compile(
    r"[\"']?(?P<key>" + "|".join(sorted(_SOFT_MODEL_KEYS, key=len, reverse=True))
    + r")[\"']?\s*[:=]\s*[\"'](?P<ref>[^\"'\n]{2,200})[\"']")

#: ``Environment=LLM_MODEL=dolphin3-0-r1-mistral-24b`` and its relatives. The
#: value is a tm-inference ALIAS, not a filename, which is why the resolver
#: needs the alias map at all.
_ENV_MODEL = re.compile(
    r"(?:^|\s|=)(?P<key>[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*)\s*=\s*"
    r"[\"']?(?P<ref>[^\s\"'#]{2,200})")

#: ``base_path: /models/video/qwen-image-2512`` in extra_model_paths.yaml —
#: a directory a consumer exposes, which resolves to a unit like any other ref.
_BASE_PATH = re.compile(r"base_path\s*:\s*[\"']?(?P<ref>[^\s\"'#]{2,200})")

#: An absolute path with no weights extension — how a service names a model
#: DIRECTORY or an HF home (``HF_HOME=<store>/voice/hf``). Extracted as a
#: PROVISIONAL reference and kept only if it resolves to a unit: a service file
#: is full of absolute paths (``ExecStart=/usr/bin/uvicorn``) and promoting
#: every one of them to a dead ref would bury the two that really are dead.
_ABS_REF = re.compile(
    r"(?<![\w:])(?P<ref>/(?:[A-Za-z0-9_.~+-]+/){1,12}[A-Za-z0-9_.~+-]+)")

#: A bind mount in a compose file or a `docker run` line.
_MOUNT_LINE = re.compile(
    r"[\"'-]?\s*(?P<host>/[^\s:\"',]+):(?P<container>/[^\s:\"',]+)(?::(?P<opts>[a-z,]+))?")

#: Values that are shaped like a model reference and never are one.
_REF_STOPWORDS = frozenset({
    "model", "models", "none", "null", "true", "false", "default", "auto",
    "local-model", "config.json", "index.json", "requirements.txt",
})

#: Path fragments that mark a consumer as archived rather than live.
_ARCHIVE_MARKERS = ("/_archive/", "/archive/", "/.bak/", ".bak.", "/old/")

#: Directories never worth walking for references.
_PRUNE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "venvs",
    "site-packages", "dist-packages", "dist", ".next", "build", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "target", ".cache", "coverage",
})

#: Additionally pruned when walking a SOURCE tree. A vendored upstream
#: checkout names models this host never chose, and its own docs and tests
#: name placeholders; both are references in the literal sense and neither is
#: a statement that THIS machine loads that model. One vendored trainer repo
#: alone contributed 400+ identical placeholder rows on the first real run.
_SOURCE_PRUNE_DIRS = frozenset({
    "repo", "vendor", "third_party", "thirdparty", "examples", "example",
    "docs", "doc", "tests", "test", "fixtures", "_archive", "archive",
    "custom_nodes", "samples",
})

#: Extensions worth reading in a source-tree ("python"-kind) root.
_SOURCE_EXTS = frozenset({
    ".py", ".sh", ".bash", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
    ".env", ".ts", ".tsx", ".js", ".jsx", ".service", ".conf", ".sql",
})

#: Files above this are generated, vendored or data — never a hand-written
#: config, and reading them is how a scan ends up quoting a lockfile.
MAX_FILE_BYTES = 4 * 1024 * 1024

MAX_WALK_DEPTH = 12


# --- Configuration ----------------------------------------------------------


@dataclass(frozen=True)
class ConsumerRoot:
    """One declared place where a consumer names the models it loads.

    ``path`` may be a glob (``~/.config/systemd/user/*.service``); each match
    is scanned. For ``systemd`` roots the unit file's own stem becomes the
    consumer name, because one glob covers many independent services — every
    other kind takes the root's ``name``.

    ``mount`` is the consumer's view of the filesystem: ``{host_path:
    container_path}``. It does two jobs that cannot be done without it. It
    REWRITES a reference written the way the container sees it
    (``/models/text/gguf/x.gguf``) back to a host path, and it decides
    REACHABILITY — a unit behind a symlink that leaves the mount exists on the
    host and does not exist for the process.
    """

    name: str
    path: str
    kind: str = "python"
    mount: Optional[tuple[str, str]] = None   # (host_path, container_path)
    unit: Optional[str] = None                # systemd unit to measure state
    user_unit: bool = True
    container: Optional[str] = None           # docker container to measure state

    def to_dict(self) -> dict:
        return {
            "name": self.name, "path": self.path, "kind": self.kind,
            "mount": ({"host_path": self.mount[0], "container_path": self.mount[1]}
                      if self.mount else None),
            "unit": self.unit, "container": self.container,
            "exists": bool(_expand(self.path)),
        }


def _expand(pattern: str) -> list[Path]:
    """Every existing path a root pattern names, glob or not."""
    raw = os.path.expanduser(os.path.expandvars(pattern))
    if any(ch in raw for ch in "*?["):
        base = Path(raw)
        # Path.glob needs a root and a relative pattern; split at the first
        # magic segment so an absolute pattern works.
        parts = base.parts
        for i, part in enumerate(parts):
            if any(ch in part for ch in "*?["):
                root = Path(*parts[:i]) if i else Path(".")
                rel = str(Path(*parts[i:]))
                try:
                    return sorted(p for p in root.glob(rel) if p.exists())
                except (OSError, ValueError):
                    return []
        return []
    p = Path(raw)
    return [p] if p.exists() else []


def _raw_config(key: str, env: str) -> Any:
    """Config value from the env override, else the convention block."""
    val = os.environ.get(env)
    if val:
        try:
            return json.loads(val)
        except ValueError:
            log.warning("%s is not valid JSON — ignoring", env)
    from okuro.yu.conventions import get_convention

    return get_convention(key, None)


def configured_roots() -> list[ConsumerRoot]:
    """Consumer roots declared for this host, or ``[]`` when the feature is off.

    Mirrors :func:`okuro.ai_models.store_scan.configured_stores` exactly: env
    override first, then the convention. A malformed entry is skipped with a
    warning and never raised — a typo in config must not take okuro down.
    """
    raw = _raw_config(ROOTS_KEY, ROOTS_ENV) or []
    roots: list[ConsumerRoot] = []
    if not isinstance(raw, list):
        log.warning("%s must be a list of {name, path, kind}", ROOTS_KEY)
        return roots
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning("skipping non-mapping consumer root: %r", entry)
            continue
        path = entry.get("path")
        name = entry.get("name") or (Path(str(path)).name if path else None)
        if not path or not name:
            log.warning("skipping consumer root without name/path: %r", entry)
            continue
        kind = str(entry.get("kind") or "python").lower()
        if kind not in KINDS:
            log.warning("consumer root %s: kind %r is not one of %s — treating "
                        "as python", name, kind, KINDS)
            kind = "python"
        mount = None
        m = entry.get("mount")
        if isinstance(m, dict) and m.get("host_path") and m.get("container_path"):
            mount = (os.path.expanduser(str(m["host_path"])).rstrip("/"),
                     str(m["container_path"]).rstrip("/"))
        elif m:
            log.warning("consumer root %s: mount must be {host_path, "
                        "container_path} — ignoring %r", name, m)
        roots.append(ConsumerRoot(
            name=str(name), path=str(path), kind=kind, mount=mount,
            unit=(str(entry["unit"]) if entry.get("unit") else None),
            user_unit=bool(entry.get("user_unit", True)),
            container=(str(entry["container"]) if entry.get("container") else None),
        ))
    return roots


def protected_consumers() -> set[str]:
    """Consumer names the user has ruled PROTECTED.

    Absent is legitimate and means "no consumer is protected" — it is a
    separate key from ``consumer_roots`` so the protection ruling survives a
    change to the root list.
    """
    raw = _raw_config(PROTECTED_KEY, PROTECTED_ENV) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        log.warning("%s must be a list of consumer names", PROTECTED_KEY)
        return set()
    return {str(x) for x in raw if x}


# --- Normalisation ----------------------------------------------------------

_SEP_RE = re.compile(r"[-_. ]+")


def norm(s: str) -> str:
    """Lower-case, with the separators that models disagree about removed.

    The DIRECTORY is ``qwen-image-2512`` and the weights file inside it is
    ``qwen_image_2512_bf16.safetensors``. A matcher that compares those
    literally fails across the ``-``/``_`` boundary, and in one real audit that
    single gap classified a 57 GB unit with 335 live references as movable.
    ``/`` is deliberately KEPT, so path structure survives for segment
    matching.
    """
    return _SEP_RE.sub("", s.lower())


def _segments(p: str) -> list[str]:
    return [norm(s) for s in str(p).strip("/").split("/") if s and s != "."]


def _strip_exts(name: str) -> str:
    """``model.safetensors`` -> ``model``; ``x.i1-Q6_K.gguf`` -> ``x.i1-Q6_K``."""
    stem = name
    for ext in _REF_EXTS:
        if stem.lower().endswith("." + ext):
            return stem[: -(len(ext) + 1)]
    return stem


# --- References -------------------------------------------------------------


@dataclass
class Ref:
    """One model reference found in one file."""

    consumer: str
    run_mode: str
    config_path: str
    line: int
    model_ref: str
    root: Optional[ConsumerRoot] = None
    unit_id: Optional[str] = None
    match_kind: Optional[str] = None
    tier: str = "ACTIVE"
    state: str = "configured"
    reachable: bool = True
    note: Optional[str] = None

    def key(self) -> tuple:
        return (self.consumer, self.config_path, self.line, self.model_ref)

    def to_dict(self) -> dict:
        return {
            "consumer": self.consumer, "run_mode": self.run_mode,
            "config_path": self.config_path, "line": self.line,
            "model_ref": self.model_ref, "unit_id": self.unit_id,
            "match_kind": self.match_kind, "tier": self.tier,
            "state": self.state, "reachable": self.reachable, "note": self.note,
            "location": f"{self.config_path}:{self.line}",
        }


def _line_of(text: str, offset: int) -> int:
    """1-based line number of a character offset.

    Computed from the offset rather than by scanning line by line because a
    ComfyUI UI-format workflow is a SINGLE line of 8 KB — a line-by-line
    reader finds every reference in such a file and a offset-based one reports
    the same truth, but the same code then also works on the pretty-printed
    API-format workflows, which are the ones a person actually edits.
    """
    return text.count("\n", 0, offset) + 1


#: Segments that mark a documentation PLACEHOLDER rather than a real file.
#: Vendored upstream trees are full of ``path/to/ltx-checkpoint.safetensors``;
#: on the first real run one such repo contributed 400+ identical dead refs,
#: which is exactly the noise tier a previous store audit named: upstream
#: examples name hundreds of models nobody here ever chose.
#: ``huggingface.co``, ``cdn.example.org`` — a first segment shaped like a
#: hostname means the reference is a URL that lost its scheme.
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.[a-z]{2,}$")

#: Bucket and container-root words. ``/models`` is the path INSIDE every
#: container here, and on one real run it claimed a unit that happens to be a
#: directory called ``models``. A segment that names a category can never
#: identify a model on its own, at any length.
_GENERIC_SEGMENTS = frozenset({
    "models", "model", "checkpoints", "checkpoint", "loras", "lora", "vae",
    "clip", "clipvision", "textencoders", "textencoder", "diffusionmodels",
    "unet", "controlnet", "embeddings", "upscalemodels", "weights", "ckpts",
    "data", "cache", "output", "outputs", "input", "inputs", "tmp", "temp",
})

_PLACEHOLDER_SEGMENTS = frozenset({
    "path", "to", "your", "yourmodel", "yourname", "example", "examples",
    "somemodel", "some", "xxx", "foo", "bar", "mymodel", "modelname",
})


def _plausible(ref: str) -> bool:
    """Reject the values that are shaped like a reference and never are one."""
    r = ref.strip()
    if len(r) < 3 or len(r) > 200:
        return False
    if r.lower() in _REF_STOPWORDS:
        return False
    if r.startswith(("http://", "https://", "#", "$")):
        return False
    if "{" in r or "}" in r or "*" in r:
        return False
    # Source code, not a value. `LLM_MODEL = os.getenv(` matched the env
    # family and produced the reference `os.getenv(` on the first real run.
    if any(ch in r for ch in "()[]<>|&;,"):
        return False
    segs = [seg.lower() for seg in r.strip("/").split("/")]
    # A URL whose scheme the path regex could not include: `https://hf.co/x`
    # is captured from the second slash as `/hf.co/x`. It is a download
    # source, not a file on this disk, and the leading-slash form slips past
    # the http:// guard above.
    if (len(segs) > 1 and _DOMAIN_RE.match(segs[0])
            and segs[0].rsplit(".", 1)[-1] not in _REF_EXTS):
        return False
    if len(segs) > 1 and segs[0] in _PLACEHOLDER_SEGMENTS:
        return False
    if any(seg in _PLACEHOLDER_SEGMENTS for seg in segs[:-1]):
        return False
    return True


def extract_refs(text: str, *, keyed_only: bool = False) -> list[tuple[int, str]]:
    """Every model reference in one file's text, as ``(line, ref)``.

    Three families, because the host's consumers write references three ways:
    a path with a weights extension, a config key whose value is a model even
    without an extension (``ckpt_name``, ``LLM_MODEL``), and a bare
    HuggingFace ``Org/Name``. ``keyed_only`` drops the loose path and HF
    families, which is what a source tree needs — a ``.py`` file mentions far
    too many two-segment slash paths for the HF pattern to be safe there.
    """
    found: dict[tuple[int, str], None] = {}

    for m in _LOADER_REF.finditer(text):
        ref = m.group("ref").strip()
        if _plausible(ref):
            found[(_line_of(text, m.start("ref")), ref)] = None
    for m in _PATH_REF.finditer(text):
        ref = m.group("ref").strip()
        if _plausible(ref):
            found[(_line_of(text, m.start("ref")), ref)] = None

    if not keyed_only:
        for m in _HF_REF.finditer(text):
            ref = f"{m.group('org')}/{m.group('name')}"
            if _plausible(ref) and _looks_like_hf(ref):
                found[(_line_of(text, m.start()), ref)] = None

    return sorted(found)


def extract_abs_refs(text: str) -> list[tuple[int, str]]:
    """Absolute paths, as PROVISIONAL references.

    Separate from :func:`extract_refs` because these are guesses, not findings.
    A systemd unit names a dozen absolute paths and one of them is a model;
    the caller keeps only those that resolve, so an unresolved one disappears
    instead of becoming a dead ref that nobody can act on.
    """
    found: dict[tuple[int, str], None] = {}
    for m in _ABS_REF.finditer(text):
        ref = m.group("ref").strip()
        if _plausible(ref):
            found[(_line_of(text, m.start("ref")), ref)] = None
    for m in _SOFT_REF.finditer(text):
        ref = m.group("ref").strip()
        if _plausible(ref):
            found[(_line_of(text, m.start("ref")), ref)] = None
    # `base_path: /models/video/qwen-image-2512` names a model directory and
    # is worth a row; `base_path: /models` names the container's whole model
    # root and is not. Provisional tells the two apart without a second rule.
    for m in _BASE_PATH.finditer(text):
        ref = m.group("ref").strip()
        if _plausible(ref):
            found[(_line_of(text, m.start("ref")), ref)] = None
    # `MODEL = "Gemini 3 Pro"` is an env-shaped model name and it is a hosted
    # model, not a file here. The family is provisional for that reason: an
    # alias that resolves (LLM_MODEL=dolphin3-0-r1-mistral-24b) is kept, and
    # one that names somebody else's cloud disappears instead of becoming a
    # dead ref nobody can act on.
    for m in _ENV_MODEL.finditer(text):
        ref = m.group("ref").strip()
        if _plausible(ref):
            found[(_line_of(text, m.start("ref")), ref)] = None
    return sorted(found)


def extract_alias_refs(text: str, aliases: Iterable[str]) -> list[tuple[int, str]]:
    """Occurrences of a KNOWN registry alias, as whole tokens.

    Needed because an alias can appear under a key no pattern can predict.
    okuro's own bridge config lists its local models as
    ``{"fast": "qwen2-5-coder-7b", "standard": ...}`` — the key is a speed
    tier, the value is a model, and no key-based family will ever guess that.
    Matching against the registry's own vocabulary is what makes it findable,
    and it cannot produce a dead ref because every alias in the vocabulary
    came from a file that exists.
    """
    vocab = sorted({a for a in aliases if a and len(a) >= 6}, key=len, reverse=True)
    if not vocab:
        return []
    pattern = re.compile(r"(?<![\w./-])(" + "|".join(re.escape(a) for a in vocab)
                         + r")(?![\w./-])")
    found: dict[tuple[int, str], None] = {}
    for m in pattern.finditer(text):
        found[(_line_of(text, m.start(1)), m.group(1))] = None
    return sorted(found)


def _looks_like_hf(ref: str) -> bool:
    """``org/Model-Name-7B`` yes, ``src/utils`` no.

    A two-segment slash path is the commonest shape in any source tree, so the
    HF family only fires when the second segment carries a model-ish signal: a
    parameter count, a quant tag, or a capitalised multi-word name.
    """
    name = ref.split("/", 1)[1]
    if re.search(r"\d+\.?\d*[bB](?![a-z])", name):        # 7B, 1.5B, 70b
        return True
    if re.search(r"(?i)\b(q\d|fp\d|bf16|instruct|chat|base|dev|xl|v\d)\b", name):
        return True
    return bool(re.match(r"^[A-Z][A-Za-z0-9]*[-_][A-Za-z0-9]", name))


def _read(path: Path) -> Optional[str]:
    try:
        if path.is_symlink() and not path.exists():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


# --- State measurement ------------------------------------------------------


def _run(cmd: list[str], timeout: float = 5.0) -> Optional[str]:
    """A short read-only command, or None if it cannot be run.

    Everything here is a state QUERY: ``systemctl is-active``, ``docker
    inspect``. A scan that cannot run them degrades to ``unknown`` rather than
    guessing, because "the service is stopped" and "I was not allowed to ask"
    are different facts and only one of them is a reason to reclaim a model.
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or "").strip()


def unit_state(unit: str, user: bool = True) -> str:
    """``running`` | ``stopped`` | ``unknown`` for a systemd unit."""
    cmd = ["systemctl"] + (["--user"] if user else []) + ["is-active", unit]
    out = _run(cmd)
    if out is None:
        return "unknown"
    if out == "active":
        return "running"
    if out in ("inactive", "failed", "deactivating", "activating"):
        return "stopped"
    return "unknown"


def container_state(name: str) -> str:
    """``running`` | ``stopped`` | ``unknown`` for a docker container."""
    out = _run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    if out is None or not out:
        return "unknown"
    if out.lower().startswith("true"):
        return "running"
    if out.lower().startswith("false"):
        return "stopped"
    return "unknown"


def _measure_state(root: ConsumerRoot, consumer: str, path: Path) -> str:
    """State of one consumer at scan time."""
    if root.container:
        return container_state(root.container)
    if root.kind == "systemd":
        return unit_state(root.unit or path.name, user=root.user_unit)
    if root.unit:
        return unit_state(root.unit, user=root.user_unit)
    if root.kind == "compose":
        cname = _compose_container_name(path) or consumer
        return container_state(cname)
    if root.kind == "python":
        return "script"
    return "configured"


def _compose_container_name(path: Path) -> Optional[str]:
    text = _read(path) or ""
    m = re.search(r"^\s*container_name:\s*[\"']?([A-Za-z0-9_.-]+)", text, re.M)
    return m.group(1) if m else None


def _tier_for(consumer: str, config_path: str, state: str,
              protected: set[str]) -> str:
    """PROTECTED beats ARCHIVE beats ACTIVE.

    PROTECTED is checked first and is not conditioned on state, because it is
    a standing ruling — a protected model does not stop being protected
    because its service happens to be stopped this afternoon. That ordering IS
    the safety property; inverting it is how a protected model gets proposed
    for deletion during a restart.
    """
    if consumer in protected:
        return "PROTECTED"
    low = "/" + config_path.strip("/").lower() + "/"
    if any(marker in low for marker in _ARCHIVE_MARKERS):
        return "ARCHIVE"
    return "ACTIVE"


# --- Resolution -------------------------------------------------------------


class Resolver:
    """Turns a written reference into a ``model_units`` row, or into a dead ref.

    Five token families, in the order a reference is most likely to be
    written. Each records HOW it matched, because a bare-filename match is
    weaker evidence than an absolute-path match and a later phase that
    proposes moving files needs to know which it has.
    """

    def __init__(self, units: Optional[list[dict]] = None,
                 placements: Optional[dict[str, list[dict]]] = None,
                 file_index: Optional[dict[str, list[str]]] = None,
                 aliases: Optional[dict[str, str]] = None):
        self.units = units if units is not None else _load_units()
        self.placements = (placements if placements is not None
                           else _load_placements())
        self.by_id = {u["unit_id"]: u for u in self.units}
        self.aliases = aliases or {}

        # Longest first: a store's <root>/video/qwen-image-2512 must win over
        # <root>/video when an absolute reference names the deeper one, or
        # every reference collapses onto the shallowest unit.
        self._abs: list[tuple[str, str]] = sorted(
            ((p["abs_path"].rstrip("/"), uid)
             for uid, places in self.placements.items() for p in places),
            key=lambda t: -len(t[0]))

        # Store roots, derived from the units rather than re-read from config:
        # a placement's absolute path minus the unit's relative path IS the
        # root. Needed by the upward containment match, which must never let a
        # reference to the store root itself claim a model.
        self._store_roots: set[str] = set()
        for u in self.units:
            rel = (u.get("rel_path") or "").strip("/")
            for pl in self.placements.get(u["unit_id"], []):
                abs_path = (pl.get("abs_path") or "").rstrip("/")
                if rel and abs_path.endswith("/" + rel):
                    self._store_roots.add(abs_path[: -(len(rel) + 1)] or "/")

        self._segs: list[tuple[list[str], str]] = [
            (_segments(u["rel_path"]), u["unit_id"]) for u in self.units]
        self._segs.sort(key=lambda t: -len(t[0]))

        # Multi-valued on purpose. The SAME model is present twice on this
        # host, once per store, and a bare filename names both. A single-valued
        # index answered with whichever copy the walk reached first, which put
        # a running consumer's models on the store its container does not
        # mount and reported fourteen of its fifteen models unreachable. The
        # candidate the consumer can actually OPEN is the right answer, so the
        # index keeps every candidate and the mount picks.
        self._name: dict[str, list[str]] = {}
        self._hf: dict[str, list[str]] = {}
        for u in self.units:
            base = Path(u["rel_path"]).name
            for token in (base, _strip_exts(base), u.get("name") or ""):
                if token:
                    self._name.setdefault(norm(token), []).append(u["unit_id"])
            for token in _hf_tokens(u["rel_path"]):
                self._hf.setdefault(token, []).append(u["unit_id"])

        self.file_index = (file_index if file_index is not None
                           else build_file_index(self.units, self.placements))

    def resolve(self, ref: str, mount: Optional[tuple[str, str]] = None
                ) -> tuple[Optional[str], Optional[str]]:
        """``(unit_id, match_kind)``, or ``(None, None)`` for a dead ref.

        Candidates are gathered from EVERY family and chosen once, rather than
        the first family that matches winning outright. That ordering mattered
        in real data: a bare filename matched a zero-byte placeholder on the
        cold store by path suffix and returned it, while the healthy 61 GB
        copy the running consumer actually loads was sitting one family lower
        in the list, found by filename. First-family-wins reported fourteen of
        a protected consumer's fifteen models as unreachable.

        The choice is: a healthy unit over a broken one, then one this
        consumer's mount can reach over one it cannot, then the strongest kind
        of match. An absolute path is the exception and short-circuits — it
        names one unit and there is nothing to weigh.
        """
        raw = ref.strip().strip("\"'")
        if not raw:
            return None, None

        host = _to_host_path(raw, mount)
        if host:
            hit = self._match_abs(host)
            if hit:
                return hit

        cands: list[tuple[str, str]] = []
        segs = _segments(raw)
        if segs:
            cands += self._segment_candidates(segs)

        raw_name = Path(raw).name
        if raw_name and _nameable(raw_name):
            base, stem = norm(raw_name), norm(_strip_exts(raw_name))
            pair = self.file_index.get(f"{segs[-2]}/{base}") if len(segs) >= 2 else None
            if pair:
                # The reference carried a directory and a file that really sit
                # together. Strongest filename evidence there is.
                for uid in pair:
                    cands.append((uid, "filename"))
            else:
                loose = (self.file_index.get(base) or self.file_index.get(stem)
                         or []) + (self._name.get(base) or self._name.get(stem) or [])
                for uid in loose:
                    # A reference that NAMED a directory may not be satisfied by
                    # a file of the right name under a different one. A workflow
                    # asking for `Wan2.2-I2V-A14B/Wan2.1_VAE.pth` was answered
                    # with the VAE inside Wan2.2-T2V-A14B — a different model,
                    # and the I2V directory is on neither store, so the honest
                    # answer was a dead ref. Dropping the directory is exactly
                    # the truncation the path regex exists to prevent; it must
                    # not come back in at match time.
                    if len(segs) >= 2:
                        rel = norm(self.by_id.get(uid, {}).get("rel_path") or "")
                        if segs[-2] not in rel.split("/"):
                            continue
                    cands.append((uid, "filename"))

        for token in _hf_tokens(raw):
            for uid in self._hf.get(token, []):
                cands.append((uid, "hf-name"))

        alias = self.aliases.get(norm(raw))
        if alias:
            for uid, _kind in self._segment_candidates(_segments(alias)):
                cands.append((uid, "alias"))
            for uid in (self.file_index.get(norm(Path(alias).name)) or []):
                cands.append((uid, "alias"))

        if not cands:
            return None, None
        return self._choose(cands, mount)

    def _segment_candidates(self, segs: list[str]) -> list[tuple[str, str]]:
        """Units whose path the reference names, in both directions.

        A reference can be SHORTER than the unit path
        (``Wan2.2-T2V-A14B/Wan2.2_14B_Merged.safetensors`` against the unit
        ``video/Wan2.2-T2V-A14B/Wan2.2_14B_Merged.safetensors``) or LONGER
        (``hunyuanvideo15/text_encoders/qwen_2.5_vl_7b.safetensors`` against
        the directory unit ``video/hunyuanvideo15``). Both name the same
        model. A single generic segment is refused, so
        ``loras/x.safetensors`` cannot claim the unit ``image/loras``.
        """
        out: list[tuple[str, str]] = []
        for u_segs, uid in self._segs:
            if not u_segs:
                continue
            if len(segs) <= len(u_segs) and u_segs[-len(segs):] == segs:
                if len(segs) >= 2 or _nameable(segs[0]):
                    out.append((uid, "path-suffix"))
        for u_segs, uid in self._segs:
            n = len(u_segs)
            if n == 0 or n >= len(segs):
                continue
            if n < 2 and not _nameable(u_segs[0]):
                continue
            for i in range(len(segs) - n + 1):
                if segs[i:i + n] == u_segs:
                    out.append((uid, "dir"))
                    break
        return out

    #: Strongest first. Only used to break a tie that health and reachability
    #: could not — it is the weakest of the three signals, not the first.
    _KIND_RANK = ("path-suffix", "filename", "dir", "hf-name", "alias")

    def _choose(self, cands: list[tuple[str, str]],
                mount: Optional[tuple[str, str]]) -> tuple[str, str]:
        seen: dict[str, str] = {}
        for uid, kind in cands:
            if uid not in seen or (self._KIND_RANK.index(kind)
                                   < self._KIND_RANK.index(seen[uid])):
                seen[uid] = kind
        pairs = sorted(seen.items(),
                       key=lambda t: (self._KIND_RANK.index(t[1]), t[0]))

        healthy = [p for p in pairs
                   if self.by_id.get(p[0], {}).get("status") == "ok"
                   and (self.by_id.get(p[0], {}).get("size_bytes") or 0) > 0]
        pairs = healthy or pairs
        if mount and len(pairs) > 1:
            probe = ConsumerRoot(name="_probe", path="", kind="python", mount=mount)
            reach = [p for p in pairs
                     if reachable_for(p[0], probe, self.placements)[0]]
            pairs = reach or pairs
        return pairs[0]

    def _pick(self, candidates: list[str],
              mount: Optional[tuple[str, str]]) -> str:
        """Of several units with the same name, the one this consumer can open.

        Falls back to the first candidate when none is reachable, so the
        reference still resolves and is reported unreachable rather than
        vanishing into the dead-ref pile — "your model is on the wrong store"
        and "your model does not exist" are different problems with different
        fixes.
        """
        ordered = sorted(dict.fromkeys(candidates))
        if len(ordered) == 1:
            return ordered[0]
        # A stub or a dangling copy is never the answer when a healthy one
        # exists: the cold store holds zero-byte placeholders of models the
        # hot store really has.
        healthy = [u for u in ordered
                   if (self.by_id.get(u, {}).get("status") == "ok"
                       and (self.by_id.get(u, {}).get("size_bytes") or 0) > 0)]
        ordered = healthy or ordered
        if not mount or len(ordered) == 1:
            return ordered[0]
        probe = ConsumerRoot(name="_probe", path="", kind="python", mount=mount)
        for uid in ordered:
            ok, _ = reachable_for(uid, probe, self.placements)
            if ok:
                return uid
        return ordered[0]

    def _match_abs(self, host: str) -> Optional[tuple[str, str]]:
        """The unit an absolute path denotes, in both containment directions.

        Downwards is the ordinary case: the path IS the unit, or a file inside
        it. Upwards is a real one too — a service that sets
        ``HF_HOME=<store>/voice/hf`` names a cache ROOT that holds units, and
        without the upward case that service produces no reference at all.

        The upward case is bounded, because unbounded it lets one reference
        claim an entire store: the path must sit at least two segments BELOW a
        configured store root. Depth alone is not that bound — a store root
        can itself be many segments deep, and a compose file's bind mount
        names exactly that root, so a depth rule let a mount line claim the
        store's largest model. Below the bound, the answer is the largest unit
        the directory contains.
        """
        h = host.rstrip("/")
        for abs_path, uid in self._abs:
            if h == abs_path or h.startswith(abs_path + "/"):
                return uid, "abs-path"
        if not self._below_a_store_root(h, depth=2):
            return None
        contained = [(uid, abs_path) for abs_path, uid in self._abs
                     if abs_path.startswith(h + "/")]
        if not contained:
            return None
        sizes = {u["unit_id"]: (u.get("size_bytes") or 0) for u in self.units}
        best = max(contained, key=lambda t: sizes.get(t[0], 0))
        return best[0], "dir-contains"

    def _below_a_store_root(self, path: str, depth: int = 2) -> bool:
        """Is ``path`` at least ``depth`` segments below some store root?"""
        for root in self._store_roots:
            base = root.rstrip("/")
            if path == base or not path.startswith(base + "/"):
                continue
            rel = path[len(base) + 1:].strip("/")
            if rel and len(rel.split("/")) >= depth:
                return True
        return False

    def _match_segments(self, segs: list[str],
                        mount: Optional[tuple[str, str]] = None
                        ) -> Optional[tuple[str, str]]:
        """Match a relative reference against unit paths, both directions.

        A reference can be SHORTER than the unit path (``Wan2.2-T2V-A14B/
        Wan2.2_14B_Merged.safetensors`` against the unit
        ``video/Wan2.2-T2V-A14B/Wan2.2_14B_Merged.safetensors``) or LONGER
        (``hunyuanvideo15/text_encoders/qwen_2.5_vl_7b.safetensors`` against
        the directory unit ``video/hunyuanvideo15``). Both are the same model
        being named, so both resolve; a single generic segment is refused
        because ``loras/x.safetensors`` must not claim the unit ``image/loras``.
        """
        suffix: list[str] = []
        for u_segs, uid in self._segs:
            if not u_segs:
                continue
            if len(segs) <= len(u_segs) and u_segs[-len(segs):] == segs:
                if len(segs) >= 2 or len(segs[0]) >= 6:
                    suffix.append(uid)
        if suffix:
            return self._pick(suffix, mount), "path-suffix"

        inside: list[str] = []
        for u_segs, uid in self._segs:
            n = len(u_segs)
            if n == 0 or n >= len(segs):
                continue
            if n < 2 and len(u_segs[0]) < 6:
                continue
            for i in range(len(segs) - n + 1):
                if segs[i:i + n] == u_segs:
                    inside.append(uid)
                    break
        if inside:
            return self._pick(inside, mount), "dir"
        return None


def _nameable(name: str) -> bool:
    """Is this basename specific enough to identify a model on its own?

    ``/v1/models`` is a URL, and on the first real run its last segment
    matched a unit that happens to be a directory called ``models``. A bare
    name resolves a model only when it carries a weights extension or is long
    and distinctive — a short generic word never does.
    """
    if any(name.lower().endswith("." + e) for e in _REF_EXTS):
        return True
    stem = norm(_strip_exts(name))
    if stem in _GENERIC_SEGMENTS:
        return False
    return len(stem) >= 8


def _hf_tokens(path: str) -> list[str]:
    """``org/name`` tokens for an HF-shaped path, normalised.

    ``models--SG161222--RealVisXL_V4.0`` and ``SG161222/RealVisXL_V4.0`` are
    the same model written two ways; both reduce to the same token so a
    reference in either dialect finds the unit.
    """
    out: list[str] = []
    for seg in str(path).strip("/").split("/"):
        if seg.startswith("models--") and seg.count("--") >= 2:
            _, org, name = seg.split("--", 2)
            out.append(norm(org) + "/" + norm(name))
        elif "--" in seg and not seg.startswith("--"):
            org, _, name = seg.partition("--")
            if org and name:
                out.append(norm(org) + "/" + norm(name))
    parts = str(path).strip("/").split("/")
    if len(parts) >= 2:
        out.append(norm(parts[-2]) + "/" + norm(_strip_exts(parts[-1])))
    return [t for t in out if "/" in t and len(t) > 3]


def _to_host_path(ref: str, mount: Optional[tuple[str, str]]) -> Optional[str]:
    """An absolute reference as the HOST sees it, or None if it is relative.

    tm-mitate writes ``/models/text/gguf/Llama-3.3-70B-Instruct.Q4_K_M.gguf``
    because that is the path inside the llama-cpp container it launches. On
    the host no such directory exists, so without the reverse rewrite every
    one of its references is a dead ref and the two that genuinely ARE dead
    stop being distinguishable from the eight that are fine.
    """
    if not ref.startswith("/"):
        return None
    if mount:
        host_path, container_path = mount
        if ref == container_path or ref.startswith(container_path + "/"):
            return host_path + ref[len(container_path):]
    return ref


def build_file_index(units: list[dict],
                     placements: dict[str, list[dict]]) -> dict[str, list[str]]:
    """``normalised filename -> [unit_id]`` for every weights file on the stores.

    A workflow names ``sd3.5_large.safetensors`` with no directory at all,
    because ComfyUI resolves bare names through its own bucket map. The unit
    that owns it is a DIRECTORY (``image/stable-diffusion-3.5-large``), so no
    amount of comparing the reference to unit names can match it — the only
    thing that can is knowing which files each unit contains.

    The walk is bounded by the same prune list the store scan uses, so it
    visits the thousands of weights files rather than the hundreds of
    thousands of files in the venvs beside them.
    """
    roots: list[tuple[str, str]] = sorted(
        ((p["abs_path"].rstrip("/"), uid)
         for uid, places in placements.items() for p in places),
        key=lambda t: -len(t[0]))
    index: dict[str, list[str]] = {}
    seen_dirs: set[str] = set()

    for abs_path, uid in roots:
        try:
            st = os.stat(abs_path)
        except OSError:
            continue
        if not os.path.isdir(abs_path):
            name = os.path.basename(abs_path)
            parent = os.path.basename(os.path.dirname(abs_path))
            index.setdefault(norm(name), []).append(uid)
            index.setdefault(norm(_strip_exts(name)), []).append(uid)
            if parent:
                index.setdefault(norm(parent) + "/" + norm(name), []).append(uid)
            continue
        real = os.path.realpath(abs_path)
        if real in seen_dirs:
            continue
        seen_dirs.add(real)
        base_depth = abs_path.count("/")
        for dirpath, dirnames, filenames in os.walk(abs_path, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if d not in _PRUNE_DIRS and not d.startswith(".no_exist")]
            if dirpath.count("/") - base_depth > MAX_WALK_DEPTH:
                dirnames[:] = []
                continue
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower().lstrip(".")
                if ext not in _REF_EXTS:
                    continue
                index.setdefault(norm(fn), []).append(uid)
                index.setdefault(norm(_strip_exts(fn)), []).append(uid)
                parent = os.path.basename(dirpath)
                if parent:
                    index.setdefault(norm(parent) + "/" + norm(fn), []).append(uid)
    return index


def _load_units() -> list[dict]:
    from okuro.db import get_db

    return [dict(r) for r in get_db().fetchall(
        "SELECT unit_id, name, store, rel_path, size_bytes, status "
        "FROM model_units")]


def _load_placements() -> dict[str, list[dict]]:
    from okuro.db import get_db

    out: dict[str, list[dict]] = {}
    for r in get_db().fetchall(
            "SELECT unit_id, abs_path, is_symlink, link_target, target_resolves "
            "FROM model_placements"):
        out.setdefault(r["unit_id"], []).append(dict(r))
    return out


# --- Reachability -----------------------------------------------------------


def absolute_for(ref: str, root: ConsumerRoot) -> Optional[str]:
    """The host path a reference denotes, when that is determinable.

    Two ways it is: the reference is already absolute (rewritten back through
    the mount if it was written the container's way), or it is relative to the
    mounted directory, which is how a registry entry is written.
    """
    raw = ref.strip().strip("\"'")
    if not raw:
        return None
    if raw.startswith("/"):
        return _to_host_path(raw, root.mount)
    if root.mount:
        candidate = os.path.join(root.mount[0], raw)
        if os.path.lexists(candidate):
            return candidate
    return None


def reachable_for(unit_id: Optional[str], root: ConsumerRoot,
                  placements: dict[str, list[dict]],
                  ref_abs: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Can THIS consumer open the unit? ``(reachable, why_not)``.

    Three ways the answer is no, and only the third is obvious:

    1. the reference resolves to no unit at all — a dead ref;
    2. the unit's own placement is a dangling symlink on the host;
    3. the unit lives on the host but OUTSIDE this consumer's bind mount, or
       inside it behind a symlink whose target leaves it.

    Case 3 is the one no per-unit check can find. Measured shape: a registry
    entry under the HOT store is a symlink whose target lives on the COLD
    store. Its own path is inside the mounted directory, so a naive mount test
    passes it, and the container that bind-mounts only the hot store still
    cannot open the file.
    """
    if not unit_id:
        return False, "dead-ref"

    # The referenced FILE decides, whenever it is known. A unit's top-level
    # placement cannot: the measured case is a DIRECTORY that is not a symlink
    # holding a FILE that is one, pointing at the other store. The directory
    # passes every mount test and the file the consumer opens does not exist
    # for it. Only resolving the actual path answers the actual question.
    if ref_abs and os.path.lexists(ref_abs):
        if not root.mount:
            return True, None
        host_path = root.mount[0].rstrip("/")
        real = os.path.realpath(ref_abs)
        if real == host_path or real.startswith(host_path + "/"):
            return True, None
        if os.path.islink(ref_abs) or os.path.realpath(ref_abs) != os.path.abspath(ref_abs):
            return False, (f"symlink leaves the mount: {real} is outside "
                           f"{host_path}")
        return False, f"outside the consumer's mount ({host_path})"

    places = placements.get(unit_id) or []
    if not places:
        return True, None

    if all(p.get("is_symlink") and not p.get("target_resolves") for p in places):
        return False, "dangling symlink on the host"

    if not root.mount:
        return True, None

    host_path = root.mount[0].rstrip("/")
    for p in places:
        abs_path = (p.get("abs_path") or "").rstrip("/")
        if not (abs_path == host_path or abs_path.startswith(host_path + "/")):
            continue
        target = p.get("link_target")
        if p.get("is_symlink") and target:
            resolved = (target if target.startswith("/")
                        else os.path.normpath(os.path.join(
                            os.path.dirname(abs_path), target)))
            if not (resolved == host_path or resolved.startswith(host_path + "/")):
                return False, (f"symlink leaves the mount: {resolved} is outside "
                               f"{host_path}")
            return True, None
        # The placement is a DIRECTORY inside the mount, which says nothing
        # about where its contents live. The measured case is a directory
        # holding a single symlinked GGUF that points at the other store: the
        # directory passes every mount test and the container still cannot open
        # a single byte of the model.
        escaped = _all_weights_escape(abs_path, host_path)
        if escaped:
            return False, f"symlink leaves the mount: {escaped} is outside {host_path}"
        return True, None

    return False, f"outside the consumer's mount ({host_path})"


#: (unit path, mount root) -> the escaping target, or None. A unit is asked
#: about once per consumer that names it, and a workflow names some of them
#: twenty times.
_ESCAPE_CACHE: dict[tuple[str, str], Optional[str]] = {}


def _all_weights_escape(abs_path: str, host_path: str) -> Optional[str]:
    """If EVERY weights file under ``abs_path`` resolves outside the mount,
    the target of one of them; otherwise None.

    Every, not any: a directory holding one escaping component and ten local
    ones is still usable, and calling it unreachable would be the same
    overreach in the other direction.
    """
    key = (abs_path, host_path)
    if key in _ESCAPE_CACHE:
        return _ESCAPE_CACHE[key]
    result: Optional[str] = None
    try:
        if not os.path.isdir(abs_path):
            _ESCAPE_CACHE[key] = None
            return None
        inside = escaping = 0
        first: Optional[str] = None
        base_depth = abs_path.count("/")
        for dirpath, dirnames, filenames in os.walk(abs_path, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
            if dirpath.count("/") - base_depth > MAX_WALK_DEPTH:
                dirnames[:] = []
                continue
            for fn in filenames:
                if os.path.splitext(fn)[1].lower().lstrip(".") not in _REF_EXTS:
                    continue
                full = os.path.join(dirpath, fn)
                real = os.path.realpath(full)
                if real == host_path or real.startswith(host_path + "/"):
                    inside += 1
                else:
                    escaping += 1
                    first = first or real
        if escaping and not inside:
            result = first
    except OSError:
        result = None
    _ESCAPE_CACHE[key] = result
    return result


# --- The scan ---------------------------------------------------------------


def _iter_files(root: ConsumerRoot) -> Iterable[Path]:
    """Every file of a root worth reading, glob and tree alike."""
    for base in _expand(root.path):
        if base.is_file():
            yield base
            continue
        if not base.is_dir():
            continue
        base_depth = str(base).count("/")
        prune = _PRUNE_DIRS | (_SOURCE_PRUNE_DIRS if root.kind == "python" else frozenset())
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if d not in prune and not d.endswith(".bak")
                           and ".bak." not in d]
            if dirpath.count("/") - base_depth > MAX_WALK_DEPTH:
                dirnames[:] = []
                continue
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if root.kind == "workflows":
                    if p.suffix.lower() == ".json":
                        yield p
                elif root.kind in ("compose", "yaml"):
                    if p.suffix.lower() in (".yml", ".yaml"):
                        yield p
                elif root.kind == "systemd":
                    if p.suffix.lower() in (".service", ".timer", ".conf"):
                        yield p
                elif p.suffix.lower() in _SOURCE_EXTS:
                    yield p


def _consumer_name(root: ConsumerRoot, path: Path) -> str:
    """The consumer a file belongs to.

    Two kinds split by FILE rather than by root, for the same reason: one glob
    covers many independent consumers, and two services are not the same
    consumer just because their config files sit side by side. A systemd root
    names the consumer after the unit file; a compose or yaml root names it
    after the SERVICE DIRECTORY, which is what puts a compose file and the
    ``extra_model_paths.yaml`` beside it under one consumer instead of two.
    Every other kind is one consumer per root.
    """
    if root.kind == "systemd":
        return path.stem
    if root.kind in ("compose", "yaml") and path.parent.name:
        return path.parent.name
    return root.name


def root_for(consumer: str, config_path: str) -> Optional[ConsumerRoot]:
    """The declared root that owns ``config_path`` for ``consumer``.

    P5 needs it for one thing the row itself cannot carry: the consumer's
    ``mount``. A reference written ``/models/text/x.gguf`` is a CONTAINER
    path, and a swap that rewrote it to a host path would produce a line that
    reads correctly in a shell and is unopenable by the process that loads it.
    The naming rule is :func:`_consumer_name`'s and is not repeated here —
    one glob covers many consumers, so the root alone cannot answer.
    """
    path = Path(config_path)
    for root in configured_roots():
        for base in _expand(root.path):
            if base == path or (base.is_dir() and base in path.parents):
                if _consumer_name(root, path) == consumer:
                    return root
    return None


def scan_root(root: ConsumerRoot, resolver: Resolver,
              protected: set[str],
              alias_vocab: Optional[Iterable[str]] = None) -> list[Ref]:
    """Every model reference one root declares, resolved."""
    refs: list[Ref] = []
    state_cache: dict[str, str] = {}
    vocab = list(alias_vocab or [])

    for path in _iter_files(root):
        text = _read(path)
        if text is None:
            continue
        consumer = _consumer_name(root, path)
        firm = extract_refs(text, keyed_only=(root.kind == "python"))
        # Provisional: absolute paths naming a model DIRECTORY or an HF home,
        # which carry no weights extension. A unit file is full of absolute
        # paths and exactly one of them is a model, so these are kept only
        # where they land on a real unit — see extract_abs_refs.
        if vocab:
            firm = sorted(set(firm) | set(extract_alias_refs(text, vocab)))
        provisional = [h for h in extract_abs_refs(text) if h not in firm]
        if not firm and not provisional:
            continue
        if consumer not in state_cache:
            state_cache[consumer] = _measure_state(root, consumer, path)
        state = state_cache[consumer]
        config_path = str(path)
        tier = _tier_for(consumer, config_path, state, protected)

        for line, raw in firm + provisional:
            unit_id, match_kind = resolver.resolve(raw, root.mount)
            if unit_id is None and (line, raw) in provisional:
                continue
            ref_abs = absolute_for(raw, root)
            ok, why = reachable_for(unit_id, root, resolver.placements, ref_abs)
            refs.append(Ref(
                consumer=consumer, run_mode=root.kind, config_path=config_path,
                line=line, model_ref=raw, root=root, unit_id=unit_id,
                match_kind=match_kind, tier=tier, state=state,
                reachable=ok, note=why,
            ))
    return refs


def scan_registry_root(root: ConsumerRoot, resolver: Resolver,
                       protected: set[str]) -> tuple[list[Ref], dict[str, str]]:
    """A registry consumer: its config file, plus what it auto-discovers.

    tm-inference is the shape this exists for. Its config file is 40 bytes of
    host and port — the registry itself is a live ``rglob`` of every ``.gguf``
    under the model store, aliased on the fly. So the references it holds are
    not written anywhere; they are a RULE, and the honest citation is the
    config file that declares the rule. Every discovered file becomes a row at
    that path, and the alias map it produces is what lets a consumer elsewhere
    that says ``LLM_MODEL=dolphin3-0-r1-mistral-24b`` resolve at all.
    """
    aliases: dict[str, str] = {}
    refs: list[Ref] = []
    config_paths = _expand(root.path)
    config_path = str(config_paths[0]) if config_paths else root.path
    state = _measure_state(root, root.name, Path(config_path))
    tier = _tier_for(root.name, config_path, state, protected)

    mount = root.mount
    if not mount:
        log.warning("registry root %s has no mount — discovery skipped", root.name)
        return refs, aliases

    host_root = mount[0].rstrip("/")
    base_depth = host_root.count("/")
    discovered: list[str] = []
    for dirpath, dirnames, filenames in os.walk(host_root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        if dirpath.count("/") - base_depth > MAX_WALK_DEPTH:
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            if not fn.lower().endswith(".gguf"):
                continue
            # tm-inference's own two exclusions, replicated: a projector is
            # not a model, and a split model is listed once at shard 1.
            if "mmproj" in fn.lower():
                continue
            if "-0000" in fn and "-00001-of-" not in fn:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), host_root)
            discovered.append(rel)

    for rel in sorted(discovered):
        alias = registry_alias(os.path.basename(rel))
        if alias:
            # Keyed NORMALISED, because a consumer writes the alias the way it
            # remembers it: the registry mints `llama-3-1-8b` and a config says
            # `llama-3.1-8b`. Same alias, and an exact-match map misses it —
            # the same dash/dot gap that once classified a 57 GB unit with 335
            # live references as unused.
            aliases.setdefault(norm(alias), rel)
        abs_path = os.path.join(host_root, rel)
        # Resolve on the ABSOLUTE path: two units can share a basename (the
        # same model present twice), and only the path says which one this
        # registry entry is.
        unit_id, match_kind = resolver.resolve(abs_path, mount)
        if unit_id is None:
            unit_id, match_kind = resolver.resolve(rel, mount)
        ok, why = reachable_for(unit_id, root, resolver.placements, abs_path)
        refs.append(Ref(
            consumer=root.name, run_mode="registry", config_path=config_path,
            line=1, model_ref=rel, root=root, unit_id=unit_id,
            match_kind=match_kind, tier=tier, state=state,
            reachable=ok, note=why,
        ))
    return refs, aliases


def registry_alias(filename: str) -> str:
    """The short alias tm-inference generates for a GGUF filename.

    Replicated, not imported: okuro never imports across project boundaries
    (they talk over HTTP or not at all), and this runs when the tm-inference
    daemon may be down. It is a NAMING rule, so replicating it is safe in a way
    that replicating behaviour would not be — and a wrong alias costs one
    unresolved reference, never a wrong action.
    """
    s = os.path.splitext(filename)[0]
    s = re.sub(r"[.\-_]?(i1[.\-_])?[QqFf]\d[\w]*", "", s)
    for noise in ("Instruct", "abliterated", "uncensored", "Thinking",
                  "anneal", "en-cot", "Distill"):
        s = re.sub(rf"[.\-_]?{noise}", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(?:Meta-|Huihui-)", "", s)
    s = re.sub(r"[.\-_]+", "-", s).strip("-").lower()
    return re.sub(r"-+", "-", s)


def scan_all() -> dict:
    """Walk every configured root and resolve every reference found."""
    roots = configured_roots()
    if not roots:
        return feature_off()
    protected = protected_consumers()
    resolver = Resolver()

    # Registry roots first: they mint the alias map every other root's
    # `LLM_MODEL=<alias>` reference needs to resolve against.
    refs: list[Ref] = []
    errors: list[str] = []
    for root in [r for r in roots if r.kind == "registry"]:
        try:
            got, aliases = scan_registry_root(root, resolver, protected)
        except OSError as exc:
            errors.append(f"{root.name}: {exc}")
            continue
        refs.extend(got)
        resolver.aliases.update(aliases)

    # The alias vocabulary the registry roots just minted, in the form a
    # config file writes it — the registry stores it normalised.
    alias_vocab = sorted({registry_alias(os.path.basename(rel))
                          for rel in resolver.aliases.values()} - {""})
    for root in [r for r in roots if r.kind != "registry"]:
        try:
            refs.extend(scan_root(root, resolver, protected, alias_vocab))
        except OSError as exc:
            errors.append(f"{root.name}: {exc}")

    # Re-resolve what the alias map can now answer: a source root scanned
    # before a registry root would otherwise keep a resolvable alias as a dead
    # ref purely because of declaration order in the config file.
    for ref in refs:
        if ref.unit_id is None and ref.root is not None:
            unit_id, match_kind = resolver.resolve(ref.model_ref, ref.root.mount)
            if unit_id:
                ref.unit_id, ref.match_kind = unit_id, match_kind
                ref.reachable, ref.note = reachable_for(
                    unit_id, ref.root, resolver.placements,
                    absolute_for(ref.model_ref, ref.root))

    dedup: dict[tuple, Ref] = {}
    for ref in refs:
        dedup.setdefault(ref.key(), ref)
    rows = list(dedup.values())
    return {
        "configured": True,
        "roots": [r.to_dict() for r in roots],
        "refs": rows,
        "errors": errors,
        "aliases": len(resolver.aliases),
    }


# --- Persistence ------------------------------------------------------------


def sync() -> dict:
    """Scan every root and merge the references into ``model_consumers``.

    A reference that is gone from the code is DELETED rather than kept: unlike
    a model unit, whose disappearance is a fact worth remembering, a line that
    no longer exists cannot be cited and a stale citation is worse than none.
    """
    from okuro.db import get_db

    res = scan_all()
    if not res.get("configured"):
        return res

    db = get_db()
    seen: set[tuple] = set()
    added = updated = 0

    for ref in res["refs"]:
        seen.add(ref.key())
        existed = db.fetchone(
            "SELECT rowid FROM model_consumers WHERE consumer = ? AND "
            "config_path = ? AND line = ? AND model_ref = ?", ref.key())
        db.execute(
            """
            INSERT INTO model_consumers (
                consumer, run_mode, config_path, line, model_ref, unit_id,
                match_kind, tier, state, reachable, note, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      datetime('now'), datetime('now'))
            ON CONFLICT(consumer, config_path, line, model_ref) DO UPDATE SET
                run_mode   = excluded.run_mode,
                unit_id    = excluded.unit_id,
                match_kind = excluded.match_kind,
                tier       = excluded.tier,
                state      = excluded.state,
                reachable  = excluded.reachable,
                note       = excluded.note,
                last_seen  = datetime('now')
            """,
            (ref.consumer, ref.run_mode, ref.config_path, ref.line,
             ref.model_ref, ref.unit_id, ref.match_kind, ref.tier, ref.state,
             1 if ref.reachable else 0, ref.note))
        if existed:
            updated += 1
        else:
            added += 1

    removed = 0
    scanned_consumers = {r.consumer for r in res["refs"]}
    for row in db.fetchall(
            "SELECT consumer, config_path, line, model_ref FROM model_consumers"):
        key = (row["consumer"], row["config_path"], row["line"], row["model_ref"])
        if key in seen or row["consumer"] not in scanned_consumers:
            continue
        db.execute(
            "DELETE FROM model_consumers WHERE consumer = ? AND config_path = ? "
            "AND line = ? AND model_ref = ?", key)
        removed += 1

    dead = sum(1 for r in res["refs"] if r.unit_id is None)
    return {
        "configured": True,
        "roots": len(res["roots"]),
        "rows": len(res["refs"]),
        "added": added,
        "updated": updated,
        "removed": removed,
        "dead": dead,
        "unreachable": sum(1 for r in res["refs"] if not r.reachable),
        "consumers": len(scanned_consumers),
        "errors": res["errors"],
    }


# --- Queries ----------------------------------------------------------------

_COLS = ("consumer, run_mode, config_path, line, model_ref, unit_id, "
         "match_kind, tier, state, reachable, note, first_seen, last_seen")

#: PROTECTED outranks ACTIVE outranks ARCHIVE when a unit has several
#: consumers — the unit inherits the STRONGEST claim on it, never the average.
_TIER_RANK = {"PROTECTED": 3, "ACTIVE": 2, "ARCHIVE": 1}


def list_consumers(*, unit: Optional[str] = None, consumer: Optional[str] = None,
                   tier: Optional[str] = None, dead: bool = False,
                   unreachable: bool = False, limit: int = 1000) -> list[dict]:
    """Cached reference rows."""
    from okuro.db import get_db

    clauses: list[str] = []
    params: list[Any] = []
    if unit:
        clauses.append("(unit_id = ? OR unit_id LIKE ?)")
        params += [unit, f"%{unit}%"]
    if consumer:
        clauses.append("consumer = ?")
        params.append(consumer)
    if tier:
        clauses.append("tier = ?")
        params.append(tier)
    if dead:
        clauses.append("unit_id IS NULL")
    if unreachable:
        clauses.append("reachable = 0")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = get_db().fetchall(
        f"SELECT {_COLS} FROM model_consumers{where} "
        f"ORDER BY tier DESC, consumer, config_path, line LIMIT ?", tuple(params))
    out = []
    for r in rows:
        d = dict(r)
        d["reachable"] = bool(d["reachable"])
        d["location"] = f"{d['config_path']}:{d['line']}"
        out.append(d)
    return out


def consumer_summary() -> list[dict]:
    """One row per consumer: how many references, how many broken."""
    from okuro.db import get_db

    rows = get_db().fetchall(
        """
        SELECT consumer, run_mode, tier,
               MIN(state) AS state,
               COUNT(*) AS refs,
               COUNT(DISTINCT unit_id) AS units,
               SUM(CASE WHEN unit_id IS NULL THEN 1 ELSE 0 END) AS dead,
               SUM(CASE WHEN reachable = 0 THEN 1 ELSE 0 END) AS unreachable,
               MAX(last_seen) AS last_seen
        FROM model_consumers
        GROUP BY consumer, run_mode, tier
        ORDER BY tier DESC, consumer
        """)
    return [dict(r) for r in rows]


def unit_consumers(unit_ids: Optional[Iterable[str]] = None
                   ) -> dict[str, dict]:
    """``unit_id -> {consumers: [...], tier, reachable}`` — one JOIN, no rescan.

    This is what ``model_inventory`` renders on each unit row. It is a query
    against the table the consumer scan already wrote, never a second walk:
    two scans of the same trees producing two answers is how an inventory page
    and a consumer page start disagreeing in front of the user.
    """
    from okuro.db import get_db

    sql = ("SELECT unit_id, consumer, tier, reachable FROM model_consumers "
           "WHERE unit_id IS NOT NULL")
    params: tuple = ()
    ids = list(unit_ids) if unit_ids is not None else None
    if ids:
        sql += f" AND unit_id IN ({','.join('?' * len(ids))})"
        params = tuple(ids)
    out: dict[str, dict] = {}
    for r in get_db().fetchall(sql, params):
        e = out.setdefault(r["unit_id"], {"consumers": [], "tier": None,
                                          "reachable": True})
        if r["consumer"] not in e["consumers"]:
            e["consumers"].append(r["consumer"])
        if _TIER_RANK.get(r["tier"], 0) > _TIER_RANK.get(e["tier"] or "", 0):
            e["tier"] = r["tier"]
        if not r["reachable"]:
            e["reachable"] = False
    for e in out.values():
        e["consumers"].sort()
    return out


def unreferenced_units(*, store: Optional[str] = None, limit: int = 50
                       ) -> list[dict]:
    """Units no configured consumer names, largest first.

    A LEFT JOIN, and it is worth saying plainly what it is not: this is the
    list of models nothing in the DECLARED roots refers to, which is evidence
    for a conversation and never grounds for deleting anything. A model can be
    loaded by a path a program computes at runtime — one 21.7 GB unit on this
    host is read daily and has zero string references anywhere, because its
    loader builds the path from its sibling's.
    """
    from okuro.db import get_db

    clause = " AND u.store = ?" if store else ""
    params: list[Any] = [store] if store else []
    params.append(int(limit))
    rows = get_db().fetchall(
        f"""
        SELECT u.unit_id, u.name, u.store, u.rel_path, u.size_bytes, u.status
        FROM model_units u
        LEFT JOIN model_consumers c ON c.unit_id = u.unit_id
        WHERE c.unit_id IS NULL AND u.status != 'missing'{clause}
        GROUP BY u.unit_id
        ORDER BY u.size_bytes DESC LIMIT ?
        """, tuple(params))
    out = []
    for r in rows:
        d = dict(r)
        d["size_gb"] = round((d.get("size_bytes") or 0) / (1024 ** 3), 2)
        out.append(d)
    return out


def consumer_map(*, unit: Optional[str] = None, consumer: Optional[str] = None,
                 tier: Optional[str] = None, dead: bool = False,
                 unreachable: bool = False, refresh: bool = False,
                 unreferenced: bool = False, limit: int = 1000) -> dict:
    """The one call the CLI, the API and the MCP tool all render."""
    roots = configured_roots()
    if not roots:
        return feature_off()

    scan = sync() if refresh else None
    rows = list_consumers(unit=unit, consumer=consumer, tier=tier, dead=dead,
                          unreachable=unreachable, limit=limit)
    summary = consumer_summary()
    out: dict[str, Any] = {
        "configured": True,
        "roots": [r.to_dict() for r in roots],
        "protected": sorted(protected_consumers()),
        "consumers": summary,
        "rows": rows,
        "totals": {
            "rows": len(rows),
            "consumers": len(summary),
            "dead": sum(1 for r in rows if r["unit_id"] is None),
            "unreachable": sum(1 for r in rows if not r["reachable"]),
        },
    }
    if scan is not None:
        out["scan"] = scan
    if unreferenced:
        out["unreferenced"] = unreferenced_units(limit=limit)
    return out


def scan_task() -> dict:
    """Daemon entry point — runs AFTER the store scan, never as its own task.

    Ordering is the whole reason it is not a second task: a reference resolves
    against ``model_units``, so a consumer scan that overtakes the store scan
    reports last week's units and calls this week's models dead.
    """
    if not configured_roots():
        return {"status": "skipped", **feature_off()}
    return {"status": "ok", **sync()}


__all__ = [
    "ROOTS_KEY", "PROTECTED_KEY", "FEATURE_OFF_REASON", "KINDS", "TIERS",
    "STATES", "feature_off", "ConsumerRoot", "Ref", "Resolver",
    "configured_roots", "protected_consumers", "extract_refs", "norm",
    "registry_alias", "reachable_for", "absolute_for", "extract_abs_refs",
    "extract_alias_refs", "root_for",
    "build_file_index",
    "scan_root", "scan_registry_root", "scan_all", "sync",
    "list_consumers", "consumer_summary", "unit_consumers",
    "unreferenced_units", "consumer_map", "scan_task",
]
