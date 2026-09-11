# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro voice CLI — run, devices, test, init, doctor subcommands.
# index:
#   imports
#   class VoiceGroup
#   def voice
#   def voice_run
#   def voice_init
#   def voice_devices
#   def voice_test
#   def voice_doctor
#   def _ensure_keys_or_wizard
#   def _handle_voice_error
# AGENT_HEADER_END -->
"""``okuro voice`` — talk to okuro, listen to okuro's reply.

The CLI is a thin shell over ``okuro.voice.session()`` (subtask 2.1). It
handles three things the library deliberately does not:

1. **First-run wizard** — if either API key is missing, prompt the user
   before the first turn. No keys → no voice, so we fail loud with a
   guided path rather than silently throwing ``MissingKeyError``.
2. **Typed-error presentation** — map ``VoiceError`` subclasses to the
   exit codes specified in ADR 1.3 §4 and print the error's ``hint``
   field instead of a stack trace.
3. **Introspection commands** — ``devices`` / ``test`` / ``doctor`` so
   the user can diagnose audio / keys / connectivity without reading
   source.

Subcommand summary
------------------
- ``okuro voice``            — run a single turn (alias: ``okuro voice run``).
- ``okuro voice run``        — single turn, explicit form. Add ``--continuous``
                               for a rolling session.
- ``okuro voice init``       — re-run the first-run wizard (rotate keys).
- ``okuro voice devices``    — list input/output audio devices.
- ``okuro voice test``       — 5-second round-trip: record → STT → TTS → play.
- ``okuro voice doctor``     — defer to subtask 2.4 (``okuro.voice.doctor``).

Exit codes (ADR 1.3 §4)
-----------------------
0 — success
1 — generic error / unknown exception
2 — ``NoDeviceError`` (no mic/speaker)
3 — ``MissingKeyError`` (keyring incomplete)
4 — ``MicPermissionError`` (OS-level permission denied)
5 — ``STTError`` / ``TTSError`` / ``VADError`` (provider/model failure)
6 — ``BridgeError`` (upstream LLM routing failure)
"""

from __future__ import annotations

import sys
from typing import Optional

import click

from .output import console, ok, warn, fail, info, heading


# Default process title — mac Activity Monitor / `ps` reads argv[0].
# Overridden here so `okuro voice` shows as 'okuro-voice' instead of
# bare 'Okuro' from the root CLI wrapper.
def _set_proc_title() -> None:
    try:
        import setproctitle
        setproctitle.setproctitle("okuro-voice")
    except ImportError:
        pass


class VoiceGroup(click.Group):
    """Click group that runs ``voice run`` when called with no subcommand.

    Rationale: ``okuro voice`` (no args) should Just Work — that is the
    one-command demo path the ADR promises. Forcing the user to type
    ``okuro voice run`` for the default action is friction DP09 forbids.
    """

    def resolve_command(self, ctx, args):  # pragma: no cover - click plumbing
        # If the first token isn't a known subcommand, inject "run" so
        # ``okuro voice --continuous`` also routes to the default action.
        if args and args[0] not in self.commands and args[0].startswith("-"):
            args = ["run", *args]
        return super().resolve_command(ctx, args)


@click.group(cls=VoiceGroup, invoke_without_command=True)
@click.pass_context
def voice(ctx):
    """Talk to okuro, listen to okuro (cloud STT/TTS, no local inference)."""
    _set_proc_title()
    if ctx.invoked_subcommand is None:
        ctx.invoke(voice_run)


@voice.command("run")
@click.option(
    "--continuous", "-c", is_flag=True,
    help="Run a rolling session — loop turns until Ctrl-C or max_turns.",
)
@click.option(
    "--max-turns", type=click.IntRange(min=1, max=500), default=None,
    help="Cap continuous mode at N turns (overrides config).",
)
@click.option(
    "--skip-wizard", is_flag=True,
    help="Fail fast on missing keys instead of running the first-run wizard.",
)
def voice_run(continuous: bool, max_turns: Optional[int], skip_wizard: bool):
    """Run a single voice turn (default). Use --continuous for a session."""
    if not skip_wizard and not _ensure_keys_or_wizard():
        # Wizard aborted — exit code 3 matches MissingKeyError semantics.
        raise SystemExit(3)

    from okuro import voice as ov

    try:
        if continuous:
            heading("okuro voice — continuous session")
            results = ov.run_session(max_turns=max_turns)
            n = len(results)
            console.print()
            ok(f"Session ended — {n} turn{'s' if n != 1 else ''} completed.")
        else:
            result = ov.session()
            _print_turn_result(result)
    except ov.VoiceError as e:
        _handle_voice_error(e)
    except KeyboardInterrupt:
        console.print()
        info("Interrupted.")
        raise SystemExit(130)


@voice.command("init")
@click.option(
    "--force", is_flag=True,
    help="Re-prompt for every key even if already set (rotation).",
)
def voice_init(force: bool):
    """Run the first-run wizard (prompts for API keys, enables voice)."""
    from okuro.voice.wizard import run_first_run_wizard

    heading("okuro voice — setup")
    result = run_first_run_wizard(force=force)

    console.print()
    if result.status == "already_configured":
        ok("All keys present — voice is ready.")
        info("Rotate a key with:  okuro voice init --force")
    elif result.status == "configured":
        ok(f"Voice configured. Keys set: {', '.join(result.keys_set) or 'none new'}")
    else:
        fail("Wizard aborted — voice disabled.")
        raise SystemExit(3)


@voice.command("devices")
def voice_devices():
    """List audio input/output devices."""
    from okuro.voice.audio import list_devices

    heading("Audio devices")
    try:
        devs = list_devices()
    except Exception as e:
        fail(f"Could not enumerate devices: {e}")
        info("Linux: sudo apt install libportaudio2 libasound2")
        info("Mac:   grant mic permission in System Settings -> Privacy -> Microphone")
        raise SystemExit(2)

    if not devs:
        warn("No audio devices detected.")
        raise SystemExit(2)

    for d in devs:
        marker = " *" if d.get("default") else "  "
        kind = d.get("kind", "?")
        console.print(f" {marker} [{kind:>6}] #{d['index']:>2}  {d['name']}")
    console.print()
    info("'*' marks the system default for each direction.")


@voice.command("test")
def voice_test():
    """5-second round-trip — record → transcribe → speak → play."""
    if not _ensure_keys_or_wizard():
        raise SystemExit(3)

    from okuro import voice as ov

    heading("okuro voice — round-trip test")
    try:
        result = ov.session()
    except ov.VoiceError as e:
        _handle_voice_error(e)
        return

    console.print()
    ok("Round-trip complete.")
    _print_turn_result(result)


@voice.command("doctor")
def voice_doctor():
    """Diagnose voice install — portaudio, keys, mic, first-turn ping.

    Full implementation lands in subtask 2.4 (``okuro.voice.doctor``).
    This shim runs the minimum viable probes available today so
    ``okuro voice doctor`` is not a dead command.
    """
    from okuro.voice.wizard import is_first_run

    heading("okuro voice — diagnostics (v1 subset)")

    # Probe 1 — PortAudio present / sounddevice importable.
    try:
        from okuro.voice.audio import list_devices
        list_devices()
        ok("PortAudio / sounddevice usable.")
    except Exception as e:
        fail(f"Audio stack broken: {e}")
        info("Linux: sudo apt install libportaudio2 libasound2")
        info("Mac:   reinstall okuro wheel — sounddevice bundles PortAudio on arm64/x86_64.")

    # Probe 2 — required keys present.
    if is_first_run():
        fail("Missing API keys — run: okuro voice init")
    else:
        ok("API keys present (deepgram + elevenlabs).")

    # Probe 3 — config loadable.
    try:
        from okuro.voice import load_voice_config
        cfg = load_voice_config()
        ok(f"Config valid — voice.enabled={cfg.enabled}")
    except Exception as e:
        fail(f"Config invalid: {e}")

    console.print()
    info("Full diagnostic (mic permission, silero model, bridge ping) lands with subtask 2.4.")


# ----------------------------- helpers -----------------------------


def _ensure_keys_or_wizard() -> bool:
    """Return True if keys are present (directly or after wizard)."""
    from okuro.voice.wizard import is_first_run, run_first_run_wizard

    if not is_first_run():
        return True

    warn("Voice isn't set up yet.")
    info("Running first-run wizard — Ctrl-C to abort.")
    result = run_first_run_wizard()
    return result.status == "configured"


def _print_turn_result(result: dict) -> None:
    """Render a turn result dict consistently."""
    transcript = result.get("transcript", "").strip()
    reply = result.get("reply", "").strip()
    duration = result.get("duration")
    provider = result.get("provider")

    console.print()
    if transcript:
        console.print(f"  [bold]you[/bold]    {transcript}")
    if reply:
        console.print(f"  [bold]okuro[/bold]  {reply}")
    if duration is not None and provider:
        info(f"{duration:.2f}s via {provider}")


def _handle_voice_error(e) -> None:
    """Print a typed voice error and exit with its registered code."""
    from okuro.voice.errors import (
        BridgeError, MicPermissionError, MissingKeyError,
        NoDeviceError, STTError, TTSError, VADError,
    )

    # Exit-code table lives on the exception itself (ADR §4). Fall back
    # to 1 for a generic VoiceError that hasn't been re-classified.
    exit_code = getattr(e, "exit_code", 1) or 1

    fail(str(e))
    hint = getattr(e, "hint", None)
    if hint:
        info(f"Fix: {hint}")

    # Known error types → exit codes already aligned; the isinstance
    # chain below is defensive in case an adapter raised a VoiceError
    # without populating `exit_code` properly.
    if isinstance(e, MissingKeyError) and exit_code == 1:
        exit_code = 3
    elif isinstance(e, NoDeviceError) and exit_code == 1:
        exit_code = 2
    elif isinstance(e, MicPermissionError) and exit_code == 1:
        exit_code = 4
    elif isinstance(e, (STTError, TTSError, VADError)) and exit_code == 1:
        exit_code = 5
    elif isinstance(e, BridgeError) and exit_code == 1:
        exit_code = 6

    sys.exit(exit_code)
