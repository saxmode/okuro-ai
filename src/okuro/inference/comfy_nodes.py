# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Custom-node manager for okuro's coupled ComfyUI. A researched workflow
#          often needs custom nodes (class_types not in a stock ComfyUI). This
#          checks what's missing against a live /object_info and — CONSENT-GATED —
#          installs the required custom-node repos into okuro's own ComfyUI
#          (git clone into custom_nodes/ + its requirements in okuro's ComfyUI
#          venv). The workflow-designer orchestration task researches WHICH repos
#          provide the missing nodes; this module just installs the given repos.
# index:
#   errors / def node_classes / def missing_for
#   def install_custom_nodes   (consent-gated clone + requirements)
# AGENT_HEADER_END -->
"""Consent-gated custom-node installation for okuro's coupled ComfyUI.

Flow step (per the generation vision): author a workflow → some node classes are
missing on the ComfyUI → ask the user to install the providing custom nodes →
on yes, install them. Node→repo resolution is the orchestrator's research job;
this module owns the install half. Idempotent and injectable, like comfy_install.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from okuro.inference.comfy_registry import missing_nodes


class ComfyNodeError(RuntimeError):
    """Custom-node install failed."""


class ComfyNodeConsentRequired(ComfyNodeError):
    """The user has not consented to installing custom nodes."""


def node_classes(object_info: dict) -> list[str]:
    """The node class_types a live ComfyUI advertises (its /object_info keys)."""
    return sorted(object_info.keys()) if isinstance(object_info, dict) else []


def missing_for(required: list[str], object_info: dict) -> list[str]:
    """Which of a workflow's required node classes the live ComfyUI is missing."""
    return missing_nodes(required, node_classes(object_info))


def _repo_name(repo: str) -> str:
    return repo.rstrip("/").split("/")[-1].removesuffix(".git")


def _default_run(argv: list[str]) -> None:
    import subprocess
    subprocess.run(argv, check=True)


def install_custom_nodes(
    repos: list[str],
    *,
    consent: bool,
    home: Optional[Path] = None,
    run: Optional[Callable[[list[str]], None]] = None,
    pip: bool = True,
) -> dict:
    """Install custom-node repos into okuro's ComfyUI. Consent-gated, idempotent.

    Clones each repo into ``<home>/custom_nodes/<name>`` (skipping ones already
    present) and, when ``pip``, installs its ``requirements.txt`` into okuro's
    ComfyUI venv. Returns ``{installed, skipped, home}``. The ComfyUI must be
    restarted to load newly-installed nodes (ComfyUI scans custom_nodes at boot).
    """
    from okuro.inference import comfy_install

    if not consent:
        raise ComfyNodeConsentRequired(
            "installing custom nodes needs explicit consent (clones third-party "
            "GPL/other code into okuro's ComfyUI). Re-run with consent=True.")
    home = home or comfy_install.comfy_home()
    runner = run or _default_run
    cn_dir = home / "custom_nodes"
    cn_dir.mkdir(parents=True, exist_ok=True)
    vpy = str(comfy_install.venv_python(home))

    installed, skipped = [], []
    for repo in repos:
        name = _repo_name(repo)
        dst = cn_dir / name
        if dst.exists():
            skipped.append(name)
            continue
        runner(["git", "clone", "--depth", "1", repo, str(dst)])
        req = dst / "requirements.txt"
        if pip and req.exists():
            runner([vpy, "-m", "pip", "install", "-r", str(req)])
        installed.append(name)
    return {"installed": installed, "skipped": skipped, "home": str(home),
            "restart_required": bool(installed)}
