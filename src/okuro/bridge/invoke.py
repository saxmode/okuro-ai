# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Unified invoke: routes to CLI or local HTTP based on provider type.
# index: imports | def invoke
# AGENT_HEADER_END -->
"""Unified invoke: routes to CLI or local HTTP based on provider type."""

from okuro.llm_hygiene import strip_bootstrap_greeting

from .config import get_provider
from .providers import NoProviderConfigured, resolve_provider
from .tracker import log_invocation


def _friendly_error_message(capability: str | None, provider_id: str) -> str:
    cap = capability or "default"
    return (
        f"No detected provider can serve capability={cap}. "
        f"Install one of claude/codex/antigravity and re-run, or set "
        f"routing.default in `~/.okuro/bridge/config.yaml` to a "
        f"configured provider. (last attempted: {provider_id!r})"
    )


def invoke(
    prompt: str | None = None,
    capability: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt_file: str | None = None,
    strip_greeting: bool = True,
    system_prompt: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    activity_sink: "Path | None" = None,
    subtask_id: str | None = None,
    role: str | None = None,
    isolated: bool = False,
    tool: bool = False,
    response_format: str = "text",
    json_schema: dict | None = None,
    tools: list | None = None,
    temperature: float | None = None,
    condition: bool = False,
    images: "list[str] | str | None" = None,
) -> dict:
    """Invoke an LLM via the bridge.

    Routes to the appropriate provider based on capability/provider/model.
    Logs usage after invocation.

    ``tool=True`` is the "tooling bridge": a stateless tool-function call that
    needs no agent context. For CLI providers it strips MCP servers, CLAUDE.md /
    user-memory auto-discovery, and per-machine system-prompt sections (~3x
    faster on claude). Pair with a minimal ``system_prompt``. Local-http
    providers ignore it (they never load that context).

    ``response_format`` / ``json_schema`` / ``tools`` / ``temperature`` are
    honored by local-http (OpenAI-compatible) providers only. CLI providers
    (claude/codex/gemini) parse text and ignore them — callers relying on
    JSON mode must route to a local provider.

    ``prompt_file`` loads the prompt from a file instead of an inline string —
    the transport already routes any prompt over 96 KiB to the child's stdin, so
    this is the ergonomic entry point for testing full bootstrap packets (~31 KB)
    without inlining them into a tool call. When given, it takes precedence over
    ``prompt``. Exactly one of ``prompt`` / ``prompt_file`` must be provided.

    ``images`` attaches local image files as INPUT — the model looks at them
    and answers in text. Route with ``capability="vision"`` (or name a
    provider). All three CLIs were measured reading a PNG on 2026-09-06;
    each uses a different mechanism, declared in its ``vision`` config block
    and applied by :mod:`okuro.bridge.vision`.

    Images are validated before anything spawns, and a provider that
    declares no vision block is an ERROR, never a silent drop — a model
    scoring a picture it never saw returns confident noise, which is worse
    than a failure. Do not confuse this with :mod:`okuro.bridge.media`,
    which is the opposite direction (prompt in, image files out).

    Returns:
        dict with success, output, duration, error, provider, model
        (local-http also returns tool_calls and, for JSON formats, parsed)
    """
    if prompt_file:
        from pathlib import Path
        try:
            prompt = Path(prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "success": False, "output": "", "duration": 0.0,
                "error": f"prompt_file unreadable ({prompt_file!r}): {exc}",
                "provider": provider or "", "model": model or "",
            }
    if not prompt:
        return {
            "success": False, "output": "", "duration": 0.0,
            "error": "invoke() requires a non-empty prompt or prompt_file",
            "provider": provider or "", "model": model or "",
        }

    # Validate image paths BEFORE routing or spawning: a bad path reaching a
    # CLI becomes an opaque provider-specific error, and reaching a model
    # becomes a confident answer about nothing.
    from .vision import VisionUnsupported, normalize_images

    try:
        image_paths = normalize_images(images)
    except ValueError as exc:
        return {
            "success": False, "output": "", "duration": 0.0,
            "error": f"invalid images: {exc}",
            "provider": provider or "", "model": model or "",
        }

    try:
        provider_id, model_name = resolve_provider(capability, provider, model)
    except NoProviderConfigured as exc:
        # Runtime fallback exhausted every detected provider — surface the
        # friendly message in the standard dict contract so existing
        # callers (auto_enrich_async, profile_sources, decomposer) keep
        # their `result.get("success")` / `result.get("error")` flow.
        return {
            "success": False,
            "output": "",
            "duration": 0.0,
            "error": str(exc),
            "provider": provider or "",
            "model": model or "",
        }
    provider_config = get_provider(provider_id)

    if not provider_config:
        return {
            "success": False,
            "output": "",
            "duration": 0.0,
            "error": _friendly_error_message(capability, provider_id),
            "provider": provider_id,
            "model": model_name,
        }

    # The provider is now known — can it actually take the images? A provider
    # with no vision block fails loudly here rather than answering blind.
    try:
        from .vision import check_supported

        vision_block = check_supported(provider_id, image_paths)
    except VisionUnsupported as exc:
        return {
            "success": False, "output": "", "duration": 0.0,
            "error": str(exc), "provider": provider_id, "model": model_name,
        }

    ptype = provider_config.get("type", "cli")
    effective_timeout = timeout or provider_config.get("default_timeout", 300)

    if ptype == "local-http":
        from .local import invoke_local, resolve_endpoint

        # Prefer a live engine registered for this model (the runner may have
        # bound a different port); fall back to the static config endpoint.
        from .config import LOCAL_FALLBACK_ENDPOINT

        static_endpoint = provider_config.get("endpoint", LOCAL_FALLBACK_ENDPOINT)
        endpoint = resolve_endpoint(model_name, static_endpoint)

        # Opt-in model-conditioning: rewrite the prompt for THIS model and pull
        # its tuned stop/sampling from the ingested prompting block. Off by
        # default (zero behavior change); never fatal — a missing block or any
        # error degrades to the raw call.
        cond_stop = None
        cond_sampling = None
        cond_temp = None
        if condition and model_name:
            try:
                from okuro.ai_models.optimize import condition_for_model, to_invoke_params

                plan = condition_for_model(prompt, model_name, system=system_prompt)
                if plan.get("prompt"):
                    prompt = plan["prompt"]
                if plan.get("system"):
                    system_prompt = plan["system"]
                p = to_invoke_params(plan)
                cond_temp = p.get("temperature")
                cond_stop = p.get("stop")
                cond_sampling = p.get("sampling")
            except Exception:  # pragma: no cover - defensive
                pass

        # Precedence: an explicit caller temperature wins; else the model's
        # conditioned value; else the 0.7 default.
        if temperature is not None:
            eff_temp = temperature
        elif cond_temp is not None:
            eff_temp = cond_temp
        else:
            eff_temp = 0.7

        result = invoke_local(
            endpoint=endpoint,
            model=model_name,
            prompt=prompt,
            system_prompt=system_prompt,
            timeout=effective_timeout,
            temperature=eff_temp,
            response_format=response_format,
            json_schema=json_schema,
            tools=tools,
            stop=cond_stop,
            sampling=cond_sampling,
            images=image_paths,
        )
    else:
        from .executor import build_command, execute, execute_streaming

        # Tooling bridge: a mechanical tool-function needs no extended thinking.
        # execute() builds the subprocess env from os.environ, so setting it here
        # (restored below) reaches the CLI. Halves flow-gen latency on haiku.
        _prev_think = None
        if tool:
            import os as _os
            _prev_think = _os.environ.get("MAX_THINKING_TOKENS")
            _os.environ["MAX_THINKING_TOKENS"] = "0"

        # path-in-prompt providers carry the image paths in the PROMPT, so
        # the fold has to happen before build_command measures its length
        # against PROMPT_ARGV_LIMIT. flag providers are handled inside
        # build_command from the same vision block.
        if image_paths and (vision_block or {}).get("mechanism") == "path-in-prompt":
            from .vision import apply_path_in_prompt

            prompt = apply_path_in_prompt(prompt, image_paths, vision_block)

        cmd, stdin_payload = build_command(
            provider_id, model_name, prompt, system_prompt=system_prompt,
            isolated=isolated, tool=tool, images=image_paths,
        )
        # cli_probe is SSOT for binary resolution. Without threading
        # state.path through to execute(), subprocess.run falls back to
        # the okuro process PATH, which can disagree with the user's
        # interactive shell (volta/nvm/asdf shims). Finding #15.
        resolved_binary: str | None = None
        try:
            from okuro.system import cli_probe

            resolved_binary = cli_probe.detect(provider_id).path
        except ValueError:
            # provider_id outside cli_probe's canonical CLI set (e.g. a
            # custom registered provider) — leave bare-name spawn so
            # subprocess PATH lookup still applies.
            pass

        if activity_sink is not None and provider_id == "claude":
            result = execute_streaming(
                cmd,
                provider_id,
                cwd=cwd,
                timeout=effective_timeout,
                resolved_binary=resolved_binary,
                stdin_input=stdin_payload,
                activity_sink=activity_sink,
                subtask_id=subtask_id,
                role=role,
            )
        else:
            result = execute(
                cmd,
                provider_id,
                cwd=cwd,
                timeout=effective_timeout,
                resolved_binary=resolved_binary,
                stdin_input=stdin_payload,
            )

        if tool:
            import os as _os
            if _prev_think is None:
                _os.environ.pop("MAX_THINKING_TOKENS", None)
            else:
                _os.environ["MAX_THINKING_TOKENS"] = _prev_think

    # Log usage
    log_invocation(
        provider=provider_id,
        model=model_name,
        duration=result.get("duration", 0.0),
        success=result.get("success", False),
        capability=capability,
    )

    # Strip the OKURO bootstrap greeting that CLAUDE.md / equivalents
    # force CLIs to emit on every one-shot call. Safe no-op when absent
    # (local-http providers never emit it).
    #
    # ``strip_greeting=False`` keeps the raw reply. Needed when the prompt
    # itself instructs the model to quote the greeting as its first block
    # (e.g. testing a full bootstrap packet) — the strip's blank-line
    # fallback would otherwise remove the model's first real answer along
    # with the greeting.
    if strip_greeting and isinstance(result.get("output"), str):
        result["output"] = strip_bootstrap_greeting(result["output"])

    result["provider"] = provider_id
    result["model"] = model_name
    return result
