# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Provider registry and capability routing.
# index: imports | def resolve_provider | def tier_of_model | def list_providers | def get_routing_table | class NoProviderConfigured
# AGENT_HEADER_END -->
"""Provider registry and capability routing."""

import logging

from .config import (
    _PROVIDER_PRIORITY,
    _detect_provider,
    get_provider,
    get_routing,
    load_config,
    resolve_default_provider,
    validate_binaries,
)


log = logging.getLogger(__name__)


class NoProviderConfigured(RuntimeError):
    """Raised when no detected provider can serve a capability.

    Friendly because the runtime fallback already walks the priority
    list — by the time this fires, every adapter said "not installed".
    The message tells the user exactly what to do (install one of the
    CLIs OR set routing.default explicitly).
    """


def _friendly_no_provider_error(capability: str | None) -> NoProviderConfigured:
    cap = capability or "default"
    return NoProviderConfigured(
        f"No detected provider can serve capability={cap}. "
        f"Install one of claude/codex/antigravity and re-run, or set "
        f"routing.default in `~/.okuro/bridge/config.yaml` to a "
        f"configured provider."
    )


def _select_runtime_provider(
    requested: str,
    capability: str | None,
    providers_map: dict,
) -> str:
    """Walk to a usable provider when ``requested`` is missing or undetected.

    1. If ``requested`` is configured AND its adapter detects it → return it.
    2. Otherwise scan ``_PROVIDER_PRIORITY`` for a configured + detected
       provider, log a warning naming the substitution, return its id.
    3. Nothing detected → raise ``NoProviderConfigured`` with a friendly
       message so the bridge_invoke caller can surface it to the user.
    """
    if requested in providers_map and _detect_provider(requested):
        return requested

    for candidate in _PROVIDER_PRIORITY:
        if candidate == requested:
            continue
        if candidate not in providers_map:
            continue
        if _detect_provider(candidate):
            log.warning(
                "bridge: configured provider %r for capability=%s is not "
                "available; falling through to %r",
                requested, capability, candidate,
            )
            return candidate

    raise _friendly_no_provider_error(capability)


def resolve_provider(
    capability: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """Resolve provider and model from capability/provider/model hints.

    Resolution order:
    1. Explicit provider → use it (no fallback — caller asked by name)
    2. Capability → look up routing table, then runtime-fallback if the
       configured provider isn't detected on this box
    3. Neither → routing.default with the same fallback dance

    Returns:
        (provider_id, model_name)

    Raises:
        ValueError: explicit ``provider`` not in the configured map
        NoProviderConfigured: capability/default lookup fell through
            every priority entry without finding a detected provider
    """
    config = load_config()
    routing = config.get("routing", {})
    providers = config.get("providers", {})

    if provider:
        provider_id = provider
        if provider_id not in providers:
            raise ValueError(f"Unknown provider: {provider_id}")
    else:
        if capability:
            requested = routing.get(
                capability,
                routing.get("default", resolve_default_provider()),
            )
        else:
            requested = routing.get("default", resolve_default_provider())
        provider_id = _select_runtime_provider(requested, capability, providers)

    provider_config = providers[provider_id]
    models = provider_config.get("models", {})

    if model:
        model_name = models.get(model, model)
    else:
        # Infer model tier from capability name. "fast-*" picks the
        # provider's "fast" model (money-efficient per DP01); "deep-*"
        # and "quality" pick "quality"; everything else picks "standard".
        # Falls through gracefully if the provider doesn't declare that
        # tier.
        tier = "standard"
        if capability:
            cap_lower = capability.lower()
            if cap_lower.startswith("fast") or cap_lower.endswith("fast"):
                tier = "fast"
            elif cap_lower.startswith("deep") or cap_lower == "quality":
                tier = "quality"
            elif cap_lower.startswith("long") or cap_lower.endswith("context"):
                tier = "long-ctx"
        model_name = models.get(tier, models.get("standard", next(iter(models.values()), "")))

    return provider_id, model_name


def tier_of_model(provider: str, model: str) -> str | None:
    """Reverse the tier→model map: which tier did *model* come from?

    Deliberately keyed on the model that actually ran, not on the capability
    string. resolve_provider only infers a tier when no explicit `model=` is
    passed (see the branch above), so re-deriving a tier from the capability
    would silently misreport every overridden call. Given codex maps all tiers
    to "" (let the CLI choose), ambiguity is real — hence the first-match walk
    and a None for "can't say", which callers must treat as unknown rather than
    as a mismatch.
    """
    if not model:
        return None
    try:
        provider_config = get_provider(provider) or {}
    except Exception:  # noqa: BLE001 — a config read must never break an alarm
        return None
    models = provider_config.get("models", {}) or {}
    for tier, name in models.items():
        if name and name == model:
            return tier
    return None


def list_providers() -> list[dict]:
    """List all providers with availability info."""
    config = load_config()
    available = validate_binaries()

    return [
        {
            "id": name,
            "type": prov.get("type", "cli"),
            "available": available.get(name, False),
            "models": prov.get("models", {}),
            "capabilities": prov.get("capabilities", []),
            "default_timeout": prov.get("default_timeout", 300),
        }
        for name, prov in config.get("providers", {}).items()
    ]


def get_routing_table() -> dict:
    """Get capability routing table."""
    return load_config().get("routing", {})
