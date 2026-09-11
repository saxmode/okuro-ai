# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Generation service facade — the ONE call the MCP tool + web UI share
#          for "generate with a model". Ties the pieces together: prompt
#          optimisation (ai_models.optimize.condition_for_model — the model-
#          prompting-expert seam), workflow resolution (comfy_registry), the
#          edition + 3-state commercial gate, and execution (comfy.ComfyAdapter).
#          Also exposes the UI readiness check ("no workflow for SD3.5-large yet
#          — create one?"). Transport-agnostic: no HTTP/MCP here, just the
#          orchestration, so every entry-point wraps the same logic (DP10).
# index:
#   GenerationRequest / Readiness / GenerationOutcome   (contracts)
#   class GenerationService
#     readiness / generate
# AGENT_HEADER_END -->
"""Generation service facade.

The product flow is: choose a model → (workflow ready?) → optimise the prompt for
that model → generate → result. This module is the seam where those steps are
composed once, so the MCP tool, the Models-page UI, and the workflow-designer
orchestration task all call the same thing rather than re-implementing the glue.

Collaborators are injected (adapter, registry, prompt optimiser) so the facade
is unit-testable without a live ComfyUI, and so the prompt optimiser can be the
real ``condition_for_model`` in production or a stub in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from okuro.inference.comfy import ComfyAdapter, ComfyEndpoint
from okuro.inference.comfy_registry import GenerativeWorkflowRegistry

TEXT_TO_IMAGE = "text-to-image"


@dataclass
class GenerationRequest:
    """A user's request to generate with a chosen model."""

    prompt: str
    model_id: str                       # okuro model id — prompting knowledge + registry key
    ckpt_name: str                      # checkpoint filename as ComfyUI addresses it
    family: Optional[str] = None        # sdxl | pony | flux | … (graph topology + prompt dialect)
    task: str = TEXT_TO_IMAGE
    license: Optional[dict] = None      # gate input (None = skip commercial gate)
    org_revenue_usd: Optional[float] = None
    base_model: Optional[str] = None
    seed: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    params_override: dict = field(default_factory=dict)  # preset tier params (steps/cfg/sampler/…)
    negative_extra: str = ""                             # preset style negatives, merged into the plan
    components: Optional[dict] = None                     # split-weight model {unet, clip:[…], vae} (Flux/SD3.5)
    endpoint: Optional[ComfyEndpoint] = None


@dataclass
class Readiness:
    """Whether a model+task can be generated now, and why not if not."""

    model_id: str
    task: str
    workflow_ready: bool
    workflow_id: Optional[str]
    reason: str  # user-facing: "ready" | "no workflow yet — create one?"


@dataclass
class GenerationOutcome:
    images: list[bytes]
    prompt_plan: dict
    workflow_id: Optional[str]
    license: dict
    seed: int
    endpoint: str
    meta: dict = field(default_factory=dict)


class GenerationService:
    """Compose prompt-optimise → resolve-workflow → gate → execute for one model."""

    def __init__(
        self,
        adapter: ComfyAdapter,
        registry: GenerativeWorkflowRegistry,
        *,
        optimize: Optional[Callable[..., dict]] = None,
    ):
        self._adapter = adapter
        self._registry = registry
        # Default optimiser = the real model-conditioned prompt planner (loads
        # the bundle's prompting block, inherits family conventions, optimises).
        self._optimize = optimize or self._default_optimize

    @staticmethod
    def _default_optimize(prompt: str, model_id: str) -> dict:
        from okuro.ai_models.optimize import condition_for_model
        return condition_for_model(prompt, model_id)

    def readiness(self, model_id: str, task: str = TEXT_TO_IMAGE) -> Readiness:
        """The UI's pre-generation check. When no workflow exists, the caller
        offers to launch the workflow-designer orchestration task."""
        wf = self._registry.get(model_id, task)
        if wf is not None:
            return Readiness(model_id, task, True, wf.get("workflow_id"),
                             "ready")
        return Readiness(model_id, task, False, None,
                         f"no ComfyUI workflow for {model_id} / {task} yet — create one?")

    def generate(self, req: GenerationRequest,
                 *, on_progress: Optional[Callable[[dict], None]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None) -> GenerationOutcome:
        """Optimise the prompt for the model, resolve its workflow (registry,
        else the family builder fallback inside the adapter), gate, and execute.

        ``on_progress`` (optional) receives ComfyUI's live step progress as
        ``{phase, message, pct}`` events (Gap C) for a UI progress bar.

        Raises whatever :meth:`ComfyAdapter.generate` raises (edition / licence /
        not-installed / generation errors) — the facade adds no new failure mode.
        """
        plan = self._optimize(req.prompt, req.model_id)
        # Layer preset intent onto the model-conditioned plan: tier params
        # override the family defaults; style negatives merge with the family's.
        if req.params_override:
            plan["params"] = {**(plan.get("params") or {}), **req.params_override}
        if req.negative_extra:
            plan["negative"] = ", ".join(
                s for s in (plan.get("negative", ""), req.negative_extra) if s)
        workflow = self._registry.get(req.model_id, req.task)
        res = self._adapter.generate(
            req.family, plan,
            ckpt_name=req.ckpt_name,
            seed=req.seed,
            width=req.width, height=req.height,
            license=req.license, org_revenue_usd=req.org_revenue_usd,
            base_model=req.base_model,
            workflow=workflow,
            components=req.components,
            endpoint=req.endpoint,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
        return GenerationOutcome(
            images=res.images,
            prompt_plan=plan,
            workflow_id=res.meta.get("workflow_id"),
            license=res.meta.get("license", {}),
            seed=res.meta.get("seed", req.seed or 0),
            endpoint=res.endpoint,
            meta=res.meta,
        )
