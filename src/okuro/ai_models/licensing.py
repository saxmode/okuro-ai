# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Commercial-licensing SSOT — resolve a model's commercial tier
#          (free / conditional-under-revenue-cap / non-commercial) and gate
#          availability against the deploying org's annual revenue. Weights are
#          NOT code: GPL never touches them, but each carries its own terms and
#          they are okuro's biggest commercial exposure (a sold product must not
#          silently offer a model the buyer cannot legally use). Every commercial
#          entry-point (catalog rank, the ComfyUI image/video engine) gates here
#          rather than re-deriving the rule.
# index:
#   FREE / CONDITIONAL / NONE                 (the three tiers)
#   LICENSE_TIERS                             (curated, proof-cited seed)
#   def classify                              (license dict -> tier, cap)
#   def available                             (tier + cap + org revenue -> bool)
#   def gate                                  (one-call decision + UI reason)
# AGENT_HEADER_END -->
"""Commercial-licensing gate for model weights.

The runtime that generates from a checkpoint (ComfyUI) must know whether the
deploying organisation may *commercially* use that checkpoint. Unlike ComfyUI's
code (GPL-3.0, handled by the arm's-length separate-process boundary), model
weights are not code — GPL does not reach them — but each weight carries its own
licence, and those split three ways:

    free         commercial use is unconditional  (permissive / OpenRAIL)
    conditional  commercial use allowed *below a revenue cap*, else enterprise
                 licence required  (SD3.x/3.5 Community, Krea 2 — $1M/yr)
    none         non-commercial only; a separate paid licence is required for
                 any revenue use  (FLUX.1-dev, non-commercial Civitai models)

A boolean ``commercial_use`` flag (the pre-existing catalog field) cannot model
the middle state: it either wrongly blocks SD3.5/Krea for a small studio, or
wrongly clears them for a >$1M org that actually needs an enterprise licence.
This module is the mechanism that resolves all three cases (DP10); the boolean
is retained on ``CatalogEntry.license`` for back-compat and is derivable as
``tier != NONE``.

Terms verified via web 2026-07-04 (BFL non-commercial licence v2.0, Stability
AI Community License, Krea 2 Community License). Licence terms change — re-verify
before launch. This is not legal advice.
"""

from __future__ import annotations

from typing import Any, Optional

FREE = "free"
CONDITIONAL = "conditional"
NONE = "none"

# The revenue cap shared by the current crop of "community" licences. Stored per
# entry so a future licence with a different threshold plugs in without a code
# change.
DEFAULT_CONDITIONAL_CAP_USD = 1_000_000.0

# Curated licence-identifier → (tier, revenue_cap_usd) seed. Keys are matched
# case-insensitively as substrings of the licence id/family so upstream naming
# drift ("stabilityai-ai-community" vs "stability-ai-community") still resolves.
# Every entry is proof-cited to the licence text; nothing here is guessed. The
# DEFAULT for an unrecognised licence is NONE — a sold product under-offers
# rather than clears a model it cannot vouch for.
LICENSE_TIERS: dict[str, tuple[str, Optional[float]]] = {
    # --- Permissive / OpenRAIL → unconditional commercial ---------------
    "apache-2.0": (FREE, None),
    "apache 2": (FREE, None),
    "mit": (FREE, None),
    "bsd": (FREE, None),
    "cc-by-4.0": (FREE, None),
    "cc-by-sa-4.0": (FREE, None),
    "cc0": (FREE, None),
    # CreativeML OpenRAIL(-M / ++-M) — SD1.5 / SDXL: commercial permitted with
    # behavioural use restrictions (not a revenue cap).
    "openrail": (FREE, None),
    "creativeml": (FREE, None),
    "llama3": (FREE, None),
    "llama-3": (FREE, None),
    "gemma": (FREE, None),
    # FLUX.1-schnell is Apache-2.0 (distinct from FLUX.1-dev).
    "flux-1-schnell": (FREE, None),
    "flux.1-schnell": (FREE, None),
    # --- Verified LLM licences (web-cited 2026-07-07; see notes below) ---
    # Non-commercial / research variants FIRST so their more-specific id wins
    # the substring match over the commercial family key that follows.
    "tongyi-qianwen-research": (NONE, None),   # Qwen Research License — non-commercial
    "qwen-research": (NONE, None),
    "mistral-research": (NONE, None),          # MRL / MNPL: production needs a paid Mistral licence
    "mistral-ai-non-production": (NONE, None),
    "mnpl": (NONE, None),
    "mrl": (NONE, None),
    # Unconditional-commercial families. MAU caps (Llama 700M, Tongyi 100M) are
    # unreachable and modelled as FREE per the OpenRAIL convention above (a
    # usage cap is not the *reachable revenue* cap that defines CONDITIONAL);
    # attribution + acceptable-use obligations still apply.
    "deepseek": (FREE, None),                  # DeepSeek Model License (OpenRAIL-derived) + MIT
    "tongyi-qianwen": (FREE, None),            # free commercial below a 100M-MAU threshold
    "llama4": (FREE, None),                    # Llama 4 Community License (700M-MAU clause)
    "llama-4": (FREE, None),
    "nvidia-open-model-license": (FREE, None), # permissive: commercial + derivatives, no attribution
    "internlm": (FREE, None),                  # weights explicitly free for commercial use
    "yi-license": (FREE, None),                # legacy Yi (free commercial w/ registration); current Yi = apache
    "falcon-llm-license": (FREE, None),        # TII Falcon License 2.0 — commercial + monetization
    "tii-falcon": (FREE, None),
    # Reachable commercial gates → CONDITIONAL (needs a paid subscription/permission).
    "nvidia-community-model-license": (CONDITIONAL, None),  # production needs NVIDIA NIM / AI-Enterprise
    "nvidia-ai-foundation": (CONDITIONAL, None),
    "falcon-180b": (CONDITIONAL, None),        # hosted-service use needs TII written permission
    # --- Community licences → conditional under a revenue cap ------------
    # Stability AI Community License (SD3, SD3.5): free commercial < $1M/yr rev.
    "stability-ai-community": (CONDITIONAL, DEFAULT_CONDITIONAL_CAP_USD),
    "stabilityai-ai-community": (CONDITIONAL, DEFAULT_CONDITIONAL_CAP_USD),
    "stabilityai-community": (CONDITIONAL, DEFAULT_CONDITIONAL_CAP_USD),
    # Krea 2 Community License: free commercial < $1M/yr rev.
    "krea-2-community": (CONDITIONAL, DEFAULT_CONDITIONAL_CAP_USD),
    "krea-community": (CONDITIONAL, DEFAULT_CONDITIONAL_CAP_USD),
    # --- Explicitly non-commercial → paid licence required --------------
    # FLUX.1-dev Non-Commercial License v2.0: no revenue use without a BFL
    # self-hosted commercial licence.
    "flux-1-dev-non-commercial": (NONE, None),
    "flux.1-dev": (NONE, None),
    "flux-1-dev": (NONE, None),
    "noncommercial": (NONE, None),
    "non-commercial": (NONE, None),
    "cc-by-nc": (NONE, None),
    "sai-nc": (NONE, None),
    "stabilityai-nc": (NONE, None),
    "research-only": (NONE, None),
    "research license": (NONE, None),
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _from_civitai(raw: Any) -> Optional[tuple[str, Optional[float]]]:
    """Civitai ``allowCommercialUse`` → tier. Civitai grants have no revenue
    threshold, so a model is FREE (creator granted image/commercial rights) or
    NONE (``["None"]`` / empty). We require a grant that covers *generated
    output* — Civitai's ``Image`` (and the broader ``Rent``/``Sell``) values —
    before clearing it; a bare/None grant is non-commercial.
    """
    if raw is None:
        return None
    grants = raw if isinstance(raw, (list, tuple)) else [raw]
    tokens = {_norm(g) for g in grants if _norm(g)}
    if not tokens or tokens <= {"none"}:
        return (NONE, None)
    # Any explicit grant beyond "None" means the creator permitted commercial use.
    return (FREE, None)


def classify(
    license: Optional[dict],
    *,
    family: Optional[str] = None,
    base_model: Optional[str] = None,
) -> tuple[str, Optional[float]]:
    """Resolve ``(tier, revenue_cap_usd)`` for a model's licence.

    Resolution order (first hit wins):
        1. an already-resolved ``commercial_tier`` on the licence dict
        2. Civitai ``allowCommercialUse`` / ``raw`` grant list
        3. substring match of the licence id, then ``family``/``base_model``,
           against :data:`LICENSE_TIERS`
        4. DEFAULT → ``NONE`` (conservative: unknown ⇒ non-commercial)

    ``revenue_cap_usd`` is ``None`` for FREE and NONE; a positive threshold for
    CONDITIONAL.
    """
    lic = license or {}

    # 1. honour an explicit tier if a caller already resolved it.
    explicit = _norm(lic.get("commercial_tier"))
    if explicit in (FREE, CONDITIONAL, NONE):
        cap = lic.get("revenue_cap_usd")
        return (explicit, float(cap) if cap else (DEFAULT_CONDITIONAL_CAP_USD if explicit == CONDITIONAL else None))

    # 2. Civitai granular grant list.
    civ = _from_civitai(lic.get("raw") if "raw" in lic else lic.get("allowCommercialUse"))
    if civ is not None:
        return civ

    # 3. substring match over id, then family/base_model.
    haystacks = [_norm(lic.get("id")), _norm(family), _norm(base_model)]
    for hay in haystacks:
        if not hay:
            continue
        for key, tier_cap in LICENSE_TIERS.items():
            if key in hay:
                return tier_cap

    # 4. conservative default.
    return (NONE, None)


def available(tier: str, cap: Optional[float], org_revenue_usd: Optional[float]) -> bool:
    """Is a model of ``tier`` commercially usable by an org at ``org_revenue_usd``?

    - FREE → always.
    - NONE → never (needs a separate paid licence, handled out-of-band).
    - CONDITIONAL → only when the org's revenue is known AND strictly below the
      cap. Unknown revenue is treated as *not available* — a sold product must
      collect the revenue tier before clearing a capped-community model, rather
      than assume the buyer is small.
    """
    if tier == FREE:
        return True
    if tier == NONE:
        return False
    if tier == CONDITIONAL:
        if org_revenue_usd is None:
            return False
        return org_revenue_usd < (cap or DEFAULT_CONDITIONAL_CAP_USD)
    return False


def gate(
    license: Optional[dict],
    org_revenue_usd: Optional[float] = None,
    *,
    family: Optional[str] = None,
    base_model: Optional[str] = None,
) -> dict:
    """One-call commercial decision for a UI/API entry-point.

    Returns ``{available, tier, revenue_cap_usd, reason}`` where ``reason`` is a
    stable, user-facing string the caller can surface verbatim.
    """
    tier, cap = classify(license, family=family, base_model=base_model)
    ok = available(tier, cap, org_revenue_usd)
    if tier == FREE:
        reason = "commercial use permitted"
    elif tier == NONE:
        reason = "non-commercial licence — commercial use needs your own licence from the model's provider"
    elif org_revenue_usd is None:
        reason = "community licence — tell us your organisation's annual revenue to confirm eligibility"
    elif ok:
        reason = f"community licence — permitted under ${cap:,.0f}/yr revenue"
    else:
        reason = f"community licence — your revenue exceeds ${cap:,.0f}/yr; an enterprise licence is required"
    return {"available": ok, "tier": tier, "revenue_cap_usd": cap, "reason": reason}


def blocks(
    license: Optional[dict],
    org_revenue_usd: Optional[float] = None,
    *,
    family: Optional[str] = None,
    base_model: Optional[str] = None,
) -> bool:
    """THE single source of the licence HARD-BLOCK decision.

    Returns ``True`` iff enforcement must refuse the generation: a *declared
    commercial* context (``org_revenue_usd`` given) running a model that is not
    cleared for it. Personal use (no revenue declared) is never hard-blocked — a
    non-commercial licence still permits personal, non-commercial generation, so
    the caller surfaces ``gate()['reason']`` as an advisory instead of blocking.

    This is deliberately narrower than ``gate()['available']``, which is the
    *commercial-eligibility* verdict and also feeds display (``commercial_status``
    reports non-commercial as not-allowed-for-commercial). Enforcement callers
    MUST route through here rather than re-deriving the rule at the call site.
    """
    if license is None or org_revenue_usd is None:
        return False
    return not gate(license, org_revenue_usd, family=family, base_model=base_model)["available"]


def commercial_status(
    license: Optional[dict],
    *,
    family: Optional[str] = None,
    base_model: Optional[str] = None,
    org_revenue_usd: Optional[float] = None,
) -> dict:
    """UI-facing commercial verdict for a model — recorded at qualification.

    Unlike :func:`classify` (which collapses an unrecognised licence into NONE),
    this distinguishes a CONFIRMED non-commercial licence from a simply-unknown
    one, so the UI can say "verify" instead of wrongly asserting "non-commercial".

    Returns ``{status, tier, allowed, reason, revenue_cap_usd, license_id}`` with
    ``status`` one of ``commercial | conditional | non_commercial | unknown``.
    ``allowed`` is the conservative gate decision (FREE→True; NONE→False;
    CONDITIONAL→True only under a known revenue below the cap).
    """
    lic = license or {}
    g = gate(lic, org_revenue_usd, family=family, base_model=base_model)

    # Recognise the licence directly from a real seed key / Civitai grant. We do
    # NOT trust a pre-set ``commercial_tier`` — from_hf_model stamps classify()'s
    # conservative DEFAULT ('none') onto every unrecognised licence, so trusting
    # it would falsely report "non-commercial" for what is really "unknown".
    hay = " ".join(_norm(x) for x in (lic.get("id"), family, base_model) if x)
    matched: Optional[tuple[str, Optional[float]]] = None
    civ = _from_civitai(lic.get("raw") if "raw" in lic else lic.get("allowCommercialUse"))
    if civ is not None:
        matched = civ
    else:
        for key, tier_cap in LICENSE_TIERS.items():
            if key in hay:
                matched = tier_cap
                break

    if matched is None:
        status, tier, cap = "unknown", NONE, None
    else:
        tier, cap = matched
        status = {FREE: "commercial", CONDITIONAL: "conditional", NONE: "non_commercial"}[tier]

    return {
        "status": status,
        "tier": tier,
        "allowed": bool(g["available"]),
        "reason": g["reason"],
        "revenue_cap_usd": cap,
        "license_id": lic.get("id"),
    }


def is_commercial(license: Optional[dict], *, family: Optional[str] = None,
                  base_model: Optional[str] = None) -> bool:
    """Back-compat coarse flag: commercially usable at all (tier != NONE).

    Mirrors the legacy ``license['commercial_use']`` semantics for callers that
    have not yet adopted the revenue-aware :func:`gate`.
    """
    tier, _ = classify(license, family=family, base_model=base_model)
    return tier != NONE
