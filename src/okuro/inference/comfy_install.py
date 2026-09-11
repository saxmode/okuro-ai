# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Provision okuro's OWN coupled ComfyUI — a shipped side-service, NOT a
#          reused foreign instance. okuro installs a pinned ComfyUI into an
#          okuro-owned home (its own venv), wires its model dirs to okuro's
#          bundle store, and hands the launch path a known home. GPL-safe: this
#          CLONES an upstream ComfyUI as a separate process at install time with
#          the user's explicit consent — okuro still never imports/forks/vendors
#          it. Edition-gated (advanced/pro) + consent-gated (never install GPL
#          software or download without asking).
# index:
#   errors / MODEL_KINDS / CAP_TO_KIND
#   def comfy_home / venv_python / is_installed / status
#   def wire_model_paths / link_model      (bundle-store <-> ComfyUI model dirs)
#   def install                            (consented, edition-gated provisioner)
# AGENT_HEADER_END -->
"""Provision okuro's coupled ComfyUI side-service.

The product contract (per the owner 2026-07-04): okuro SHIPS ComfyUI as a coupled
headless service — the user consents at install, okuro owns the instance and its
lifecycle (launched + broker-leased by ``inference.comfy``), and it reads okuro's
model store. okuro must never depend on a foreign ComfyUI. This module is the
install half; :mod:`okuro.inference.comfy` is the run half.

Install is idempotent and fully injectable (the subprocess runner is a param) so
the sequence is unit-testable without cloning a repo or building a venv.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional
from okuro.db.engine import okuro_home

COMFY_REPO = "https://github.com/comfyanonymous/ComfyUI"
# Pinned for reproducibility (SYS-VALIDATE); override via OKURO_COMFYUI_REF.
# v0.9.2 is the latest ComfyUI release tag as of 2026-07-05 (verified via
# `git ls-remote --tags`). Bump deliberately, never float on master.
DEFAULT_REF = os.environ.get("OKURO_COMFYUI_REF", "v0.9.2")

# Runtime imports ComfyUI needs but has historically OMITTED from its
# requirements.txt (verified missing on v0.9.2 — server.py → frontend_management
# imports `requests` with no pin). Installed after requirements so a fresh okuro
# ComfyUI actually boots. Additive-only; keep minimal (DP09).
RUNTIME_EXTRAS = ("requests",)

# ComfyUI model-dir kinds okuro wires to its bundle store.
MODEL_KINDS = (
    "checkpoints", "loras", "vae", "text_encoders", "diffusion_models",
    "controlnet", "clip_vision", "upscale_models", "embeddings",
)
# okuro bundle capability -> the ComfyUI model dir its main weight belongs in.
CAP_TO_KIND = {
    "image": "checkpoints",
    "video": "diffusion_models",
    "vae": "vae",
    "controlnet": "controlnet",
    "upscale": "upscale_models",
}


class ComfyInstallError(RuntimeError):
    """Install failed."""


class ComfyConsentRequired(ComfyInstallError):
    """The user has not consented to installing ComfyUI (GPL, external clone)."""


class ComfyEditionError(ComfyInstallError):
    """local inference is off for this edition (air)."""


def comfy_home() -> Path:
    """okuro-owned ComfyUI home (override via OKURO_COMFYUI_HOME)."""
    return Path(os.environ.get(
        "OKURO_COMFYUI_HOME", str(okuro_home() / "comfyui")))


def venv_python(home: Optional[Path] = None) -> Path:
    home = home or comfy_home()
    return home / "venv" / "bin" / "python"


def is_installed(home: Optional[Path] = None) -> bool:
    """True when okuro's own ComfyUI is present + has its venv."""
    home = home or comfy_home()
    return (home / "main.py").exists() and venv_python(home).exists()


def model_root(home: Optional[Path] = None) -> Path:
    return (home or comfy_home()) / "models"


def status(home: Optional[Path] = None) -> dict:
    home = home or comfy_home()
    return {
        "home": str(home),
        "installed": is_installed(home),
        "venv_python": str(venv_python(home)),
        "model_paths_config": str(home / "extra_model_paths.yaml"),
        "model_paths_wired": (home / "extra_model_paths.yaml").exists(),
        "ref": DEFAULT_REF,
    }


def wire_model_paths(home: Optional[Path] = None,
                     *, bundles_root: Optional[Path] = None) -> Path:
    """Create the ComfyUI model dirs + an extra_model_paths.yaml pointing at them.

    Weight files are linked into ``<home>/models/<kind>/`` from okuro's bundle
    store (see :func:`link_model`), so ComfyUI sees exactly the models okuro
    manages — never a foreign box's library.
    """
    home = home or comfy_home()
    mroot = model_root(home)
    for kind in MODEL_KINDS:
        (mroot / kind).mkdir(parents=True, exist_ok=True)
    lines = ["okuro:", f"    base_path: {mroot}/"]
    for kind in MODEL_KINDS:
        lines.append(f"    {kind}: {kind}")
    # ComfyUI expects a `clip` key; map it to text_encoders (same dir).
    lines.append("    clip: text_encoders")
    cfg = home / "extra_model_paths.yaml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


def link_model(src: Path, capability: str, *, home: Optional[Path] = None,
               kind: Optional[str] = None) -> Path:
    """Symlink a bundle weight into okuro's ComfyUI model dir (acquire's seam).

    ``kind`` overrides the capability→dir mapping. Returns the link path.
    Idempotent: an existing correct link is left as-is.
    """
    home = home or comfy_home()
    dst_kind = kind or CAP_TO_KIND.get(capability, "checkpoints")
    dst_dir = model_root(home) / dst_kind
    dst_dir.mkdir(parents=True, exist_ok=True)
    link = dst_dir / Path(src).name
    if link.is_symlink() or link.exists():
        if link.is_symlink() and Path(os.readlink(link)) == Path(src):
            return link
        link.unlink()
    link.symlink_to(src)
    return link


def _default_run(argv: list[str]) -> None:
    subprocess.run(argv, check=True)


def install(
    *,
    consent: bool,
    home: Optional[Path] = None,
    ref: str = DEFAULT_REF,
    detection: Optional[dict] = None,
    run: Optional[Callable[[list[str]], None]] = None,
    bundles_root: Optional[Path] = None,
) -> dict:
    """Provision okuro's coupled ComfyUI. Idempotent; returns :func:`status`.

    Gates: ``consent`` must be True (the user agreed to install ComfyUI — GPL,
    cloned as a separate process), and the edition must not be air. Steps: clone
    the pinned ComfyUI into ``home`` → create its venv → install torch + its
    requirements → wire the model dirs to okuro's bundle store.
    """
    home = home or comfy_home()
    runner = run or _default_run

    if not consent:
        raise ComfyConsentRequired(
            "installing ComfyUI needs explicit consent (it clones GPL-3.0 ComfyUI "
            "as a separate side-service). Re-run with consent=True.")
    from okuro.ai_models.edition import detect_edition, local_inference_enabled
    det = detection if detection is not None else _detect()
    if not local_inference_enabled(det):
        raise ComfyEditionError(
            f"local inference is off on okuro-{detect_edition(det)}; ComfyUI is not installed here")

    if not (home / "main.py").exists():
        home.parent.mkdir(parents=True, exist_ok=True)
        runner(["git", "clone", COMFY_REPO, str(home)])
        if ref and ref != "master":
            runner(["git", "-C", str(home), "checkout", ref])
    if not venv_python(home).exists():
        runner([sys.executable, "-m", "venv", str(home / "venv")])
        vpy = str(venv_python(home))
        runner([vpy, "-m", "pip", "install", "--upgrade", "pip"])
        runner([vpy, "-m", "pip", "install", "torch", "torchvision", "torchaudio"])
        req = home / "requirements.txt"
        if req.exists():
            runner([vpy, "-m", "pip", "install", "-r", str(req)])
        if RUNTIME_EXTRAS:
            runner([vpy, "-m", "pip", "install", *RUNTIME_EXTRAS])
    wire_model_paths(home, bundles_root=bundles_root)
    return status(home)


def _detect() -> dict:
    from okuro.capability import capabilities
    return capabilities()
