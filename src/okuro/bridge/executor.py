# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Subprocess execution for CLI providers.
# index: imports | def build_command | def execute
# AGENT_HEADER_END -->
"""Subprocess execution for CLI providers."""

import os
import subprocess
import threading
import time

from .config import get_provider

# ---------------------------------------------------------------------------
# Per-provider spawn concurrency limiter
# ---------------------------------------------------------------------------
#
# Codex is spawn-per-turn against a SINGLE ChatGPT subscription: the M3
# reviewer fires critic + scorer as codex bridge calls, and under a busy
# engine those can overlap other codex bridge work (a second phase's review,
# sentinel, the compressor). Concurrent sustained codex inference against one
# account intermittently trips a throttle → the call exits 1 with empty
# stderr → the critic degrades to infra_error → the task lands in
# waiting_user instead of done. Serialize codex bridge spawns so at most
# OKURO_CODEX_MAX_CONCURRENCY run at once (default 1 = fully serial).
#
# This is deadlock-free: the streaming SUBAGENT does NOT go through this
# executor (it uses CodexStreamAdapter), so a long-lived subagent turn never
# holds a slot the reviewer needs. Only bridge_invoke callers (critic /
# scorer / decompose / sentinel / compressor) queue here. Providers absent
# from the cap map are unlimited (claude / gemini are unaffected).

_SPAWN_SEMAPHORES: "dict[str, threading.BoundedSemaphore]" = {}
_SPAWN_SEM_LOCK = threading.Lock()


def _spawn_concurrency_cap(provider_id: str) -> int:
    """Max concurrent bridge spawns for a provider. 0 → unlimited."""
    if provider_id == "codex":
        try:
            return max(1, int(os.environ.get("OKURO_CODEX_MAX_CONCURRENCY", "1")))
        except (TypeError, ValueError):
            return 1
    return 0


def _acquire_spawn_slot(provider_id: str):
    """Block until a spawn slot is free; return the semaphore to release, or
    None when the provider is unlimited."""
    cap = _spawn_concurrency_cap(provider_id)
    if cap <= 0:
        return None
    with _SPAWN_SEM_LOCK:
        sem = _SPAWN_SEMAPHORES.get(provider_id)
        if sem is None:
            sem = threading.BoundedSemaphore(cap)
            _SPAWN_SEMAPHORES[provider_id] = sem
    sem.acquire()
    return sem


PROMPT_ARGV_LIMIT = 96 * 1024
"""Threshold above which the prompt is piped via stdin instead of argv.

Linux ``ARG_MAX`` is reported as 2 MiB by ``getconf`` but the practical
limit (after the env block + argv pointer table) sits near 128 KiB.
Long prompts overflow this and ``execve(2)`` returns ``E2BIG`` (Errno 7)
before the CLI starts. 96 KiB keeps headroom for env vars on every
supported CLI; each one reads the prompt from stdin when no positional
prompt argument is provided.
"""


def build_command(
    provider_id: str,
    model: str,
    prompt: str,
    system_prompt: str | None = None,
    mcp_config: str | None = None,
    isolated: bool = False,
    tool: bool = False,
    images: list[str] | None = None,
) -> tuple[list[str], str | None]:
    """Build CLI command for subprocess.run().

    Returns ``(cmd, stdin_payload)``. When the prompt is small enough to
    fit in argv it is appended (or paired with the configured
    ``prompt_flag``) and ``stdin_payload`` is ``None``. When the prompt
    exceeds :data:`PROMPT_ARGV_LIMIT` it is omitted from ``cmd`` and
    returned as ``stdin_payload`` instead — every supported CLI reads
    the prompt from stdin when no positional/flag prompt is given,
    which sidesteps the ``execve`` ``E2BIG`` (Errno 7) failure mode on
    long continuation prompts.

    Prompt delivery is otherwise CLI-specific:

    - Claude and Codex take the prompt as a positional trailing argument;
      ``-p`` / ``exec`` are mode switches, not prompt flags.
    - Gemini's ``-p`` / ``--prompt`` IS the prompt flag — the value must
      follow it immediately and is NOT a trailing positional. Providers
      declare this via ``prompt_flag``: when set, we emit
      ``[prompt_flag, prompt]`` in-order and skip the trailing append.
      Without this, gemini exits with
      "not enough arguments following: p" because the flag sits at
      position index 1 with model/system/mcp flags interleaved between
      it and the prompt at the end.

    ``images`` attaches image INPUT (see :mod:`okuro.bridge.vision`). Paths
    must already be validated by ``vision.normalize_images``. Delivery is
    per-provider, read from the provider's ``vision`` block:

    - ``mechanism: flag`` — emits the provider's image flag. Codex's ``-i``
      is VARIADIC, so ``needs_separator`` inserts ``--`` before the trailing
      positional prompt; without it codex parses the prompt as one more
      filename and dies with the misleading "No prompt provided via stdin".
      No separator is emitted when the prompt goes to stdin — there is no
      trailing positional to protect, and a dangling ``--`` is noise.
    - ``mechanism: path-in-prompt`` — the caller (``invoke``) has already
      folded the paths into ``prompt``; nothing to do here.
    """
    provider = get_provider(provider_id)
    if not provider:
        raise ValueError(f"Unknown provider: {provider_id}")
    if provider.get("type") == "local-http":
        raise ValueError("Local provider uses HTTP API, not subprocess")

    binary = provider["binary"]
    flags = list(provider.get("autonomous_flags", []))
    model_flag = provider.get("model_flag", "--model")
    extra_flags = provider.get("extra_flags", {})
    prompt_flag = provider.get("prompt_flag")

    cmd = [binary, *flags]

    # `tool` is the trimmed-for-speed superset of `isolated` (no MCP + no
    # CLAUDE.md/memory + no dynamic env). Mutually exclusive; tool wins.
    if tool:
        cmd.extend(provider.get("tool_flags", provider.get("isolated_flags", [])))
    elif isolated:
        cmd.extend(provider.get("isolated_flags", []))

    if model_flag and model:
        cmd.extend([model_flag, model])

    if system_prompt and extra_flags.get("system_prompt"):
        cmd.extend([extra_flags["system_prompt"], system_prompt])

    if mcp_config and extra_flags.get("mcp_config"):
        cmd.extend([extra_flags["mcp_config"], mcp_config])

    # Image attachment for `flag`-mechanism providers. path-in-prompt
    # providers were handled upstream in invoke(); content-parts never
    # reaches this function (local-http takes the HTTP path).
    vision_block = provider.get("vision") or {}
    needs_separator = False
    if images and vision_block.get("mechanism") == "flag":
        from .vision import build_image_argv

        cmd.extend(build_image_argv(vision_block, images))
        needs_separator = bool(vision_block.get("needs_separator"))

    # A prompt that fits argv still goes to STDIN when it begins with '-': a
    # leading dash is parsed as an (unknown) option by the trailing-positional
    # CLIs (Claude/Codex) and as a stray flag value by prompt_flag CLIs (gemini).
    # stdin sidesteps option parsing entirely — the same path the E2BIG guard
    # uses. Fixes Resonance research bullets ("- fact… (source: url)") failing
    # claim extraction with `error: unknown option '- …'` (0 triples ingested).
    if len(prompt) > PROMPT_ARGV_LIMIT or prompt.lstrip().startswith("-"):
        return cmd, prompt

    if prompt_flag:
        cmd.extend([prompt_flag, prompt])
    else:
        if needs_separator:
            cmd.append("--")
        cmd.append(prompt)
    return cmd, None


def execute(
    cmd: list[str],
    provider_id: str,
    cwd: str | None = None,
    timeout: int = 300,
    resolved_binary: str | None = None,
    stdin_input: str | None = None,
) -> dict:
    """Execute CLI command via subprocess.

    When ``resolved_binary`` is set, ``cmd[0]`` is replaced with that
    absolute path before spawn. This is the contract that lets cli_probe
    (SSOT for "where does the user's CLI live") flow through to actual
    execution. Without it, subprocess inherits the okuro process PATH,
    which often lacks the user's interactive-shell shims (volta, nvm,
    asdf, .npm-global) — surfaced as "Binary not found: gemini" on PDF
    upload even though the wizard's CLI status board reported gemini
    authenticated. Finding #15.

    ``stdin_input`` carries the prompt when ``build_command`` decided it
    was too large to fit in argv (see :data:`PROMPT_ARGV_LIMIT`).
    """
    provider = get_provider(provider_id)
    env_strip = set(provider.get("env_strip", [])) if provider else set()

    # Strip env vars (explicit list + CLAUDE* to prevent nested detection)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in env_strip and not k.startswith("CLAUDE")
    }

    if resolved_binary:
        cmd = [resolved_binary, *cmd[1:]]

    start_time = time.time()

    # Serialize codex (single-account, spawn-per-turn) bridge spawns; no-op
    # for claude/gemini. Acquired OUTSIDE the try so a failure to spawn still
    # releases in the finally only when actually held.
    _slot = _acquire_spawn_slot(provider_id)
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_input,
            # When the prompt rides argv (stdin_input is None), the child's
            # stdin would otherwise inherit the parent's fd. codex exec then
            # prints "Reading additional input from stdin..." and BLOCKS on it
            # unless that fd is already EOF/closed — so a codex bridge_invoke
            # (the M3 reviewer's critic + scorer) hangs to the timeout whenever
            # the parent stdin is a live pipe rather than /dev/null. DEVNULL
            # gives the child immediate EOF. Harmless for claude/gemini (they
            # don't read stdin with a positional prompt). When stdin_input is
            # set, subprocess.run owns stdin (PIPE) to deliver it — leave it.
            stdin=(subprocess.DEVNULL if stdin_input is None else None),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or os.getcwd(),
            env=env,
        )
        duration = time.time() - start_time

        if proc.returncode == 0:
            return {
                "success": True,
                "output": proc.stdout,
                "duration": round(duration, 2),
                "error": None,
            }
        return {
            "success": False,
            "output": proc.stdout or proc.stderr,
            "duration": round(duration, 2),
            "error": f"Exit code {proc.returncode}: {proc.stderr[:500] if proc.stderr else ''}",
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False, "output": "", "error": f"Timed out after {timeout}s",
            "duration": round(time.time() - start_time, 2),
        }
    except FileNotFoundError:
        return {
            "success": False, "output": "", "error": f"Binary not found: {cmd[0]}",
            "duration": 0.0,
        }
    except Exception as e:
        return {
            "success": False, "output": "", "error": str(e),
            "duration": round(time.time() - start_time, 2),
        }
    finally:
        if _slot is not None:
            _slot.release()


def execute_streaming(
    cmd: list[str],
    provider_id: str,
    cwd: str | None = None,
    timeout: int = 300,
    resolved_binary: str | None = None,
    stdin_input: str | None = None,
    activity_sink: "Path | None" = None,
    subtask_id: str | None = None,
    role: str | None = None,
) -> dict:
    """Streaming variant of `execute` — pipes each stdout line into the
    task's `.activity.jsonl` as it arrives.

    Used by the M3 reviewer (Critic + Scorer) so multi-minute LLM calls
    are visible in the activity feed instead of silent until completion.
    Falls back to the non-streaming `execute()` for providers other than
    claude (others don't speak --output-format stream-json).

    activity_sink: path to `.activity.jsonl` (the task dir's existing
    file). subtask_id + role are tagged onto every emitted row so the
    UI groups them with the right card/panel — e.g. subtask_id =
    'reviewer:phase1:critic', role = 'critic'.
    """
    if activity_sink is None or provider_id != "claude":
        return execute(cmd, provider_id, cwd=cwd, timeout=timeout,
                       resolved_binary=resolved_binary, stdin_input=stdin_input)

    import json as _json
    from datetime import datetime as _dt
    provider = get_provider(provider_id)
    env_strip = set(provider.get("env_strip", [])) if provider else set()
    env = {
        k: v for k, v in os.environ.items()
        if k not in env_strip and not k.startswith("CLAUDE")
    }
    if resolved_binary:
        cmd = [resolved_binary, *cmd[1:]]

    # Inject claude streaming flags if not already present.
    if "--output-format" not in cmd:
        cmd = cmd + ["--output-format", "stream-json", "--verbose"]

    start_time = time.time()
    try:
        af = open(activity_sink, "a") if activity_sink else None
    except OSError:
        af = None

    def _emit(row: dict) -> None:
        if af is None:
            return
        if subtask_id is not None:
            row["subtask_id"] = subtask_id
        if role is not None:
            row["role"] = role
        try:
            af.write(_json.dumps(row) + "\n")
            af.flush()
        except OSError:
            pass

    _emit({"ts": _dt.utcnow().isoformat(), "type": "subtask_start",
           "cmd": (cmd[0] or "").split("/")[-1] if cmd else ""})

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_input else None,
            text=True, cwd=cwd or os.getcwd(), env=env,
            start_new_session=True,
        )
        if stdin_input and proc.stdin:
            try:
                proc.stdin.write(stdin_input)
                proc.stdin.close()
            except (OSError, BrokenPipeError):
                pass

        all_stdout: list[str] = []
        # Accumulate the model's final assistant text across stream-json
        # events so callers (decomposer.parse_plan, critic + scorer
        # parsers) receive the same concatenated text the non-streaming
        # `execute()` path returned before. Without this the streaming
        # path returned raw stream-json envelopes, which yaml/json
        # parsers downstream silently fail to parse.
        text_chunks: list[str] = []
        deadline = start_time + timeout
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                all_stdout.append(line)
                # Lazy import — avoid cycle with dispatcher when executor
                # is imported during cold-start.
                try:
                    from okuro.orchestrator.dispatcher import _normalize_event_claude as _norm  # type: ignore
                    obj = _json.loads(line)
                    row = _norm(obj)
                    if row:
                        _emit(row)
                    if isinstance(obj, dict) and obj.get("type") == "assistant":
                        for block in obj.get("message", {}).get("content", []) or []:
                            if block.get("type") == "text":
                                txt = block.get("text") or ""
                                if txt:
                                    text_chunks.append(txt)
                except Exception:
                    pass
                if time.time() > deadline:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    raise subprocess.TimeoutExpired(cmd, timeout)
        finally:
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except Exception:
                pass

        try:
            stderr = (proc.stderr.read() if proc.stderr else "") or ""
        except Exception:
            stderr = ""
        proc.wait(timeout=10)
        duration = time.time() - start_time
        stdout_full = "".join(text_chunks) if text_chunks else "".join(all_stdout)
        if proc.returncode == 0:
            return {"success": True, "output": stdout_full,
                    "duration": round(duration, 2), "error": None}
        return {"success": False, "output": stdout_full or stderr,
                "duration": round(duration, 2),
                "error": f"Exit code {proc.returncode}: {stderr[:500]}"}

    except subprocess.TimeoutExpired:
        return {"success": False, "output": "",
                "error": f"Timed out after {timeout}s",
                "duration": round(time.time() - start_time, 2)}
    except FileNotFoundError:
        return {"success": False, "output": "",
                "error": f"Binary not found: {cmd[0]}", "duration": 0.0}
    except Exception as e:
        return {"success": False, "output": "", "error": str(e),
                "duration": round(time.time() - start_time, 2)}
    finally:
        if af is not None:
            try:
                af.close()
            except Exception:
                pass
