# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Provider configuration loader.
# index:
#   imports
#   def load_config
#   def get_provider
#   def get_routing
#   def validate_binaries
#   def _check_local_provider
#   def resolve_default_provider
#   def seed_inference_defaults
# AGENT_HEADER_END -->
"""Provider configuration loader.

Reads from ~/.okuro/config.yaml under the 'inference' key.
"""

import copy
import logging
import os
import shutil
import urllib.request
from pathlib import Path

import yaml
from okuro.db.engine import okuro_home

CONFIG_PATH = okuro_home() / "config.yaml"

# okuro-reserved local-inference endpoint. Deliberately OUTSIDE tm-inference's
# 1320x cluster (tm-inference owns 0.0.0.0:13207) so that when the engine
# registry has no live entry for a model, the local provider fails with
# connection-refused rather than silently routing okuro's request into
# tm-inference's server. In normal operation resolve_endpoint() returns the
# registry's per-model port (each engine binds its own); this is the fallback.
LOCAL_FALLBACK_ENDPOINT = "http://127.0.0.1:13337"

# Canonical preference order for default provider selection. Picks the
# first entry whose adapter `detect()` returns True. Order matches the
# routing table convention (claude is the historical default; codex is
# the next-best general-purpose CLI; antigravity follows — it inherited
# gemini's slot when the Gemini CLI was retired for individuals on
# 2026-06-18; local-http is the free-but-restricted fallback when no CLI
# exists).
_PROVIDER_PRIORITY = ("claude", "codex", "antigravity", "local")

# Last-resort literal when no provider is detected — preserves the legacy
# "claude default" behaviour on a fresh box so existing routing tables
# stay byte-identical until at least one provider is installed.
_LAST_RESORT_DEFAULT = "claude"

log = logging.getLogger(__name__)

_config_cache = None
_config_mtime = 0.0

# Default provider config (embedded, no external file needed)
_DEFAULT_CONFIG = {
    "providers": {
        "claude": {
            "binary": "claude",
            "type": "cli",
            "autonomous_flags": [
                "-p", "--permission-mode", "bypassPermissions",
                "--no-session-persistence",
            ],
            # Appended only when invoke(isolated=True): use ONLY MCP servers
            # passed via --mcp-config (none here), ignoring the global okuro
            # config — so the spawned CLI has no bootstrap tool and skips the
            # per-turn bootstrap. Used by the fast pulse chat.
            "isolated_flags": ["--strict-mcp-config"],
            # invoke(tool=True) — the "tooling bridge": a stateless tool-function
            # call that needs NO agent context. Strips everything the per-turn
            # bootstrap adds: MCP servers (--strict-mcp-config), CLAUDE.md + user
            # memory auto-discovery (--setting-sources ""), per-machine env/git/
            # memory blocks (--exclude-dynamic-system-prompt-sections), and the
            # Chrome integration probe (--no-chrome). Pair with a minimal
            # --system-prompt. ~15s → ~5s on haiku. Non-claude providers ignore.
            "tool_flags": [
                "--strict-mcp-config",
                "--setting-sources", "",
                "--exclude-dynamic-system-prompt-sections",
                "--no-chrome",
            ],
            "model_flag": "--model",
            "env_strip": ["CLAUDECODE"],
            "extra_flags": {
                "system_prompt": "--system-prompt",
                "mcp_config": "--mcp-config",
            },
            "models": {"fast": "haiku", "standard": "sonnet", "quality": "opus"},
            "capabilities": [
                "text-generation", "code-generation", "analysis", "tool-use",
                "vision",
            ],
            "default_timeout": 300,
            # VISION — image INPUT (see okuro.bridge.vision; the mirror of the
            # `media` block, which is image OUTPUT). `claude --help` declares no
            # image flag: --file is for downloading remote file resources, not
            # attaching a local one. The agent's built-in Read tool handles
            # images, so the mechanism is to name absolute paths in the prompt.
            # Measured 2026-09-06: reads a token AND a shape out of a PNG even
            # under invoke(tool=True), because Read is built in, not MCP, so
            # --strict-mcp-config does not remove it.
            "vision": {"mechanism": "path-in-prompt", "max_images": 8},
        },
        # NOTE: the `gemini` provider was retired on 2026-07-18. The Gemini
        # CLI sunset for individuals on 2026-06-18 and Google's sanctioned
        # successor is the Antigravity CLI (`agy`), which reaches the same
        # Google models — so antigravity below inherited its slot outright.
        # Gemini stays readable as a HISTORICAL provider (okuro.trace.gemini
        # ingests past ~/.gemini transcripts; telemetry still recognises the
        # tag on ~300 existing session rows) but is no longer dispatchable.
        "antigravity": {
            "binary": "agy",
            "type": "cli",
            # The sanctioned Gemini CLI successor, and the individual's path to
            # Google models — `agy models` also lists Claude and GPT-OSS, so one
            # binary reaches three vendors on a consumer subscription.
            # --dangerously-skip-permissions auto-approves tool calls in print
            # mode (-p), the same role --yolo played for the gemini CLI. Prompt
            # goes last via prompt_flag; build_command already emits model before
            # -p, verified working against agy 1.1.3 (2026-07-17).
            "autonomous_flags": ["--dangerously-skip-permissions"],
            "prompt_flag": "-p",
            "model_flag": "--model",
            "env_strip": [],
            "extra_flags": {},
            # Use agy's model IDs (left column of `agy models`), NOT the display
            # strings in its right column. The display names were configured on
            # 2026-07-17 and had rotted by 2026-09-06: `agy models` served
            # 3.8/3.7/3.6 Flash and "Gemini 3.5 Flash (High)" no longer existed,
            # so fast and standard both died with "invalid model selection" —
            # check_model_drift() had been reporting all three tiers missing.
            # IDs are stable, hyphenated and quoting-proof; each one below was
            # invoked successfully on 2026-09-06.
            "models": {
                "fast": "gemini-3.8-flash-low",
                "standard": "gemini-3.8-flash-high",
                "quality": "gemini-3.1-pro-high",
            },
            "capabilities": [
                "text-generation", "code-generation", "analysis", "tool-use",
                "vision",
            ],
            "default_timeout": 300,
            # VISION — image INPUT. `agy --help` declares no image flag, and
            # BEWARE: its `-i` is --prompt-interactive, NOT --image. A shared
            # flag spelling copied from codex would hang the session waiting on
            # a terminal. The agent reads local files, so path-in-prompt it is —
            # measured 2026-09-06 against gemini-3.8-flash-high.
            "vision": {"mechanism": "path-in-prompt", "max_images": 8},
            # MEDIA — modalities this provider serves as FILES, not text (see
            # okuro.bridge.media). agy has no image flag and no image model in
            # `agy models`: the capability lives behind its built-in
            # `generate_image` tool, so the mechanism is to steer the agent
            # rather than to pass a flag. Verified end-to-end against agy 1.1.10
            # on 2026-08-08 — text-to-image, reference-image editing, and
            # AspectRatio all produce real files. No video/audio tool exists.
            "media": {
                "image": {
                    "mechanism": "tool-prompt",
                    "tool": "generate_image",
                    # Flat JSON envelope; --dangerously-skip-permissions is what
                    # lets the tool call proceed unattended in print mode.
                    "flags": ["--output-format", "json",
                              "--dangerously-skip-permissions"],
                    "supports_reference_images": True,
                    # Measured: 42-54s text-to-image, ~21s for an edit. The
                    # ceiling is generous because the agent loads its full
                    # toolset per spawn.
                    "timeout": 420,
                },
            },
        },
        "codex": {
            "binary": "codex",
            "type": "cli",
            "autonomous_flags": [
                "exec", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
            ],
            # Codex (ChatGPT subscription) tiers by REASONING EFFORT on the
            # account's model (from ~/.codex/config.toml, auto-updates on
            # upgrade), NOT by model name — those are account-gated and churn.
            # So tier value = `model_reasoning_effort=<level>` passed via -c.
            "model_flag": "-c",
            "env_strip": [],
            "extra_flags": {},
            "models": {
                "fast": "model_reasoning_effort=low",
                "standard": "model_reasoning_effort=medium",
                "quality": "model_reasoning_effort=high",
            },
            "capabilities": [
                "text-generation", "code-generation", "deep-research", "vision",
            ],
            "default_timeout": 600,
            # VISION — the only provider with a REAL image flag:
            # `-i, --image <FILE>...`. It is VARIADIC, so a trailing positional
            # prompt is swallowed as another filename and codex then reports
            # "No prompt provided via stdin" — a silent, misleading failure
            # measured 2026-09-06. `needs_separator` makes build_command emit
            # `--` before the prompt, which fixes it.
            "vision": {
                "mechanism": "flag",
                "flag": "-i",
                "needs_separator": True,
                "max_images": 8,
            },
        },
        "local": {
            "type": "local-http",
            "endpoint": LOCAL_FALLBACK_ENDPOINT,
            "models": {
                "fast": "qwen2-5-coder-7b",
                "standard": "qwen3-6-35b-a3b-ud",
                "quality": "glm-4-7-flash",
                "long-ctx": "qwen3-next-80b-a3b",
            },
            "capabilities": ["text-generation", "code-generation", "long-context"],
            "default_timeout": 300,
            # VISION — OpenAI-compatible image_url content parts. NOT in
            # `capabilities` on purpose: routing must never pick local for
            # vision, because the served model is a text model unless someone
            # has deliberately loaded a VLM (Qwen3-VL et al). The block exists
            # so `provider="local"` works once one IS loaded; reaching it is
            # an explicit act, never a routing accident.
            "vision": {"mechanism": "content-parts", "max_images": 8},
        },
    },
    # Capability → provider routing. Model tier within a provider is
    # inferred from the capability name by resolve_provider() — 'fast-*'
    # picks the provider's "fast" model (DP01 money-efficient), 'deep-*'
    # / 'quality' picks "quality", everything else uses "standard".
    #
    # fast-draft routes to claude (haiku) on cost grounds; revisit once
    # antigravity's tiers have been benchmarked against it.
    "routing": {
        "deep-research": "codex",
        "fast-draft": "claude",
        "quality": "claude",
        "code-generation": "claude",
        "tool-use": "claude",
        # Was "gemini" until 2026-07-17, when the Gemini CLI's individual
        # tier went dead (IneligibleTierError) and every analysis call
        # spawned a doomed process. antigravity now declares the analysis
        # capability too — revisit this once its quality tier has been
        # benchmarked against claude.
        "analysis": "claude",
        "long-context": "local",
        # Image INPUT. A PLACEHOLDER route, not a deliberate one — all three
        # CLIs were measured reading a PNG on 2026-09-06, so the value is
        # "claude" precisely so _build_dynamic_inference_defaults() rewrites
        # it to whatever CLI this box actually has. Never point it at "local":
        # that provider serves a text model unless a VLM was deliberately
        # loaded, and a judge scoring a picture it never saw returns
        # confident noise.
        "vision": "claude",
        "default": "claude",
    },
}


def _detect_provider(name: str) -> bool:
    """Return True if a provider can be invoked from this machine.

    Delegates to ``okuro.system.cli_probe.detect`` — the single source of
    truth across wizard / dashboard / bridge / doctor. ``cli_probe`` asks
    the user's shell where the binary is (no hardcoded install-dir list)
    and asks the CLI itself for its auth state (no file-existence proxy).

    For routing we require both: binary invokable AND auth confirmed. A
    binary on PATH with stale credentials would route here, then fail at
    spawn time with the CLI's "not logged in" message — surfacing the
    truth at routing time prevents that.

    Local-http providers go through ``_check_local_provider`` for the
    HTTP health check. CLIs not registered in cli_probe (anything outside
    the canonical claude/codex/antigravity/cursor set) are unsupported here
    and return False.
    """
    if name == "local":
        local_cfg = _DEFAULT_CONFIG.get("providers", {}).get("local", {})
        return _check_local_provider(local_cfg)
    try:
        from okuro.system.cli_probe import detect as _cli_detect
    except Exception:
        return False
    try:
        return _cli_detect(name).ready
    except ValueError:
        # Unknown CLI in cli_probe registry. Fail closed — the operator
        # needs to register a probe before bridge can route here.
        return False
    except Exception:
        return False


def resolve_default_provider() -> str:
    """Return the first provider in priority order whose detect() succeeds.

    Walks ``_PROVIDER_PRIORITY`` and returns the first id for which the
    adapter reports the provider is installed/reachable. Falls back to
    ``_LAST_RESORT_DEFAULT`` when nothing is detected so behaviour
    matches today on a fresh machine where every CLI is missing.

    This is the building block for both the seed-on-first-run path
    (``seed_inference_defaults``) and the runtime fallback in
    ``providers.resolve_provider`` — both call here so provider
    selection follows one rule.
    """
    for name in _PROVIDER_PRIORITY:
        if _detect_provider(name):
            return name
    return _LAST_RESORT_DEFAULT


def _build_dynamic_inference_defaults() -> dict:
    """Return a deep copy of ``_DEFAULT_CONFIG`` with claude-literal routes
    rewritten to whatever ``resolve_default_provider()`` returns.

    Only routes whose value equals ``_LAST_RESORT_DEFAULT`` ("claude")
    in the static defaults get rewritten — explicit non-claude routes
    (deep-research → codex) stay put. This is what "fresh defaults adapt to
    the detected stack" means: we change the placeholder, not the deliberate
    routing.

    Note the consequence of that rule: "claude" in the table IS the
    placeholder, so a route cannot say "deliberately claude". When analysis
    moved off the retired gemini CLI on 2026-07-17 it therefore became a
    placeholder route and now follows the detected stack — which is what we
    want for it (no surviving provider is a deliberate analysis specialist),
    but is worth knowing before pointing another deliberate route at claude.
    """
    dynamic = copy.deepcopy(_DEFAULT_CONFIG)
    chosen = resolve_default_provider()
    if chosen == _LAST_RESORT_DEFAULT:
        return dynamic
    routing = dynamic.get("routing", {})
    for key, value in list(routing.items()):
        if value == _LAST_RESORT_DEFAULT:
            routing[key] = chosen
    return dynamic


def seed_inference_defaults(*, force: bool = False) -> Path | None:
    """Write a freshly-resolved ``inference`` block into ``CONFIG_PATH``.

    Called from install/onboarding paths so a brand-new okuro install on
    a box without claude doesn't ship with `routing.default = "claude"`
    and immediately fail every ``bridge_invoke``. ``force=True``
    overwrites an existing inference section; default behaviour is to
    leave any pre-existing ``inference`` block untouched (user
    customisations are sacrosanct).

    Returns the config path on write, or None if nothing changed.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if CONFIG_PATH.exists():
        try:
            existing = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        except yaml.YAMLError:
            existing = {}
    if "inference" in existing and not force:
        return None
    existing["inference"] = _build_dynamic_inference_defaults()
    CONFIG_PATH.write_text(yaml.safe_dump(existing, sort_keys=False))
    # Invalidate cache so next load picks up the freshly-written file.
    global _config_cache, _config_mtime
    _config_cache = None
    _config_mtime = 0.0
    return CONFIG_PATH


def load_config() -> dict:
    """Load and cache inference config, reloading if file changed.

    Resolution order:
    1. ``CONFIG_PATH`` exists with an ``inference`` section → use it
       verbatim (user-customised configs are never overwritten).
    2. ``CONFIG_PATH`` exists without an ``inference`` section → return
       in-memory dynamic defaults (claude-literal routes rewritten to
       whichever provider is detected). Not persisted — call
       ``seed_inference_defaults()`` from an install path to persist.
    3. ``CONFIG_PATH`` missing → in-memory dynamic defaults, same as #2.
    """
    global _config_cache, _config_mtime

    if not CONFIG_PATH.exists():
        return _build_dynamic_inference_defaults()

    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        return _build_dynamic_inference_defaults()

    if _config_cache is None or mtime > _config_mtime:
        with open(CONFIG_PATH) as f:
            full_config = yaml.safe_load(f) or {}
        if "inference" in full_config:
            _config_cache = full_config["inference"]
        else:
            # File exists but the user hasn't customised inference yet —
            # synthesise dynamic defaults each load so detection changes
            # (a CLI being installed mid-session) take effect on the
            # next call. Cheap because detect() is shutil.which.
            _config_cache = _build_dynamic_inference_defaults()
        _config_mtime = mtime

    return _config_cache


def get_provider(name: str) -> dict | None:
    """Get provider config by name."""
    return load_config().get("providers", {}).get(name)


def get_routing() -> dict:
    """Get capability routing table."""
    return load_config().get("routing", {})


def validate_binaries() -> dict[str, bool]:
    """Return ``{provider_id: ready}`` for every configured provider.

    Goes through ``okuro.system.cli_probe`` (single source of truth) for
    CLI providers — ``ready`` means the binary is on the user's shell
    PATH AND auth is verified by the CLI itself. The dashboard's
    ``/api/bridge/providers`` lights now reflect the same state the
    bridge router uses for capability dispatch, so a green badge can
    never coexist with a "Binary not found" error at invoke time.

    Local-http providers retain the HTTP health-check path.
    """
    from okuro.system.cli_probe import detect as _cli_detect

    config = load_config()
    results = {}
    for name, provider in config.get("providers", {}).items():
        ptype = provider.get("type", "cli")
        if ptype == "local-http":
            results[name] = _check_local_provider(provider)
            continue
        try:
            results[name] = _cli_detect(name).ready
        except ValueError:
            # Provider declared in the loaded config but not registered
            # in cli_probe (operator added a custom CLI). Fail closed —
            # add it to cli_probe._KNOWN_CLIS to enable detection.
            results[name] = False
        except Exception:
            results[name] = False
    return results


def _check_local_provider(provider: dict) -> bool:
    """Check if local inference daemon is reachable."""
    endpoint = provider.get("endpoint", LOCAL_FALLBACK_ENDPOINT)
    try:
        req = urllib.request.Request(f"{endpoint}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False
