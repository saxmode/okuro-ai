# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Studio setup orchestration (Gap A) — the "yes → ready" spine. Given a
#          preset/family + consent, chains: install okuro's ComfyUI → find +
#          download a model for the family (discovery → rank → acquire.pull) →
#          stamp the family so auto-solve finds it → link its weights into
#          ComfyUI. Emits human progress at each phase. Transport-agnostic: the
#          REST layer runs this on a studio_jobs Job and streams the progress
#          (DP10 — one orchestration, any caller). All I/O is injectable so the
#          chain is unit-tested without a git clone or a multi-GB download.
# index:
#   SetupError
#   def pick_entry            (family/hint -> best runnable+licensed CatalogEntry)
#   def _ensure_family        (stamp prompting.family on a freshly-pulled bundle)
#   def run_setup             (install -> download -> link, with progress)
# AGENT_HEADER_END -->
"""Studio setup orchestration — install → model → link, so a fresh box becomes
generation-ready from one consent.

The user consents once ("enable local image generation"); okuro does the rest.
This module is that "rest", composed once so the MCP tool and the REST setup
job call the same chain. Each phase reports a human message (never ComfyUI/model
jargon) through a ``progress(phase, message, pct)`` callback. The heavy steps
(install, download) are injected in tests; production wires the real
``comfy_install.install`` and ``acquire.pull``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

Progress = Callable[[str, str, Optional[int]], None]


class SetupError(RuntimeError):
    """Setup could not complete (message is user-actionable)."""


def _noop(phase: str, message: str, pct: Optional[int] = None) -> None:  # pragma: no cover
    pass


def pick_entry(
    family: Optional[str],
    model_hint: Optional[str],
    *,
    detection: Optional[dict] = None,
    org_revenue_usd: Optional[float] = None,
    discover_fn: Optional[Callable[..., list]] = None,
    storage=None,
):
    """Find the best downloadable image model for a style.

    Searches by the preset's ``model_hint`` (else the family name), then ranks
    survivors by runnability + licence (``catalog.rank``) so the box never picks
    a model it can't run or legally use. Returns a ``CatalogEntry`` or None.
    """
    from okuro.ai_models import discovery
    from okuro.ai_models.catalog import rank

    discover = discover_fn or discovery.discover
    query = model_hint or family or ""
    entries = discover(query, modality="image", storage=storage) if query else []
    if not entries:
        return None
    ranked = rank(entries, detection or {}, modality="image",
                  commercial=org_revenue_usd is not None,
                  org_revenue_usd=org_revenue_usd)
    return ranked[0] if ranked else None


def _ensure_family(bundle, family: Optional[str], bundles_root: Optional[Path]) -> None:
    """Stamp ``prompting.family`` onto a freshly-pulled image bundle.

    ``acquire.pull`` ingests prompting from GGUF metadata — an image checkpoint
    has none, so ``prompting.family`` comes out unset and the family auto-solver
    (gen_tools.resolve_model_for_family) can't find it. The preset already knows
    the family; persist it so the model is discoverable next time.
    """
    if not family:
        return
    from okuro.ai_models.bundle import write_bundle

    prompting = dict(bundle.prompting or {})
    if prompting.get("family"):
        return
    prompting["family"] = family
    bundle.prompting = prompting
    write_bundle(bundle, root=bundles_root)


def run_setup(
    *,
    family: str,
    model_hint: Optional[str] = None,
    consent: bool,
    progress: Progress = _noop,
    home: Optional[Path] = None,
    bundles_root: Optional[Path] = None,
    detection: Optional[dict] = None,
    org_revenue_usd: Optional[float] = None,
    install_fn: Optional[Callable[[], object]] = None,
    resolve_installed: Optional[Callable[..., Optional[str]]] = None,
    discover_fn: Optional[Callable[..., list]] = None,
    pull_fn: Optional[Callable[..., object]] = None,
) -> dict:
    """Make a box generation-ready for a style. Idempotent + resumable.

    Chain: install okuro's ComfyUI (if absent) → if no installed model for the
    family, discover + download one and stamp its family → link the weight(s)
    into ComfyUI. Emits progress per phase. Returns
    ``{family, model_id, ckpt_name, loader, downloaded}``. Raises
    :class:`SetupError` with an actionable message on any hard failure.
    """
    from okuro.inference import comfy_install, gen_tools

    resolve_installed = resolve_installed or gen_tools.resolve_model_for_family

    # 1) Engine — install okuro's own ComfyUI (GPL-safe separate process).
    progress("engine", "Setting up the generation engine…", 5)
    if not comfy_install.is_installed(home):
        try:
            if install_fn is not None:
                install_fn()
            else:
                comfy_install.install(consent=consent, home=home,
                                      bundles_root=bundles_root)
        except comfy_install.ComfyConsentRequired as exc:
            raise SetupError(str(exc)) from exc
        except Exception as exc:  # clone/venv/torch failure
            raise SetupError(f"could not set up the engine: {exc}") from exc
    progress("engine", "Engine ready", 40)

    # 2) Model — reuse an installed one for the family, else download.
    downloaded = False
    model_id = resolve_installed(family, bundles_root=bundles_root)
    if model_id is None:
        progress("model", "Finding a model for this style…", 45)
        entry = pick_entry(family, model_hint, detection=detection,
                           org_revenue_usd=org_revenue_usd, discover_fn=discover_fn)
        if entry is None:
            raise SetupError(
                f"no downloadable model found for the '{family}' style")
        label = getattr(entry, "display_name", None) or family
        progress("model", f"Downloading {label}… (this can take a few minutes)", 55)
        from okuro.ai_models import acquire

        pull = pull_fn or acquire.pull
        try:
            bundle = pull(entry, root=bundles_root, detection=detection)
        except Exception as exc:
            raise SetupError(f"could not download the model: {exc}") from exc
        _ensure_family(bundle, family, bundles_root)
        model_id = bundle.id
        downloaded = True
        progress("model", "Model ready", 85)
    else:
        progress("model", "Model already installed", 85)

    # 3) Link — wire the model's weights into okuro's ComfyUI dirs.
    progress("link", "Preparing the model…", 90)
    resolved = gen_tools.resolve_model(model_id, home=home, bundles_root=bundles_root)
    if resolved is None:
        raise SetupError("the model could not be prepared for generation")
    progress("workflow", "Finishing up…", 98)
    return {
        "family": family,
        "model_id": model_id,
        "ckpt_name": resolved.get("ckpt_name"),
        "loader": resolved.get("loader"),
        "downloaded": downloaded,
    }
