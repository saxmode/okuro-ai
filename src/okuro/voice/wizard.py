# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: First-run voice wizard — prompts for provider API keys and flips voice.enabled.
# index:
#   imports
#   def is_first_run
#   def run_first_run_wizard
#   def _prompt_key
#   def _enable_voice_in_config
#   def _already_has_keys
# AGENT_HEADER_END -->
"""First-run voice wizard.

Entered by ``okuro voice`` / ``okuro voice init`` when either
``deepgram_api_key`` or ``elevenlabs_api_key`` is missing from the keyring.

Behaviour — deliberately boring so it reads the same on mac and linux:

1. Detect missing keys via ``KeyringStorage.list_keys()``.
2. For each missing key: print signup URL, prompt with hidden input, store
   via ``KeyringStorage.add_key()`` (no ``.env``, no disk leakage).
3. Flip ``voice.enabled: true`` in ``~/.okuro/config.yaml`` (merge, no
   clobber) so subsequent ``okuro voice`` runs skip the wizard.
4. Return a summary dict so the caller can print a follow-up hint
   (``okuro voice test`` / ``okuro voice``).

The wizard is re-entrant: running it a second time with all keys present
returns immediately (``status="already_configured"``). Callers can force
a re-prompt with ``force=True`` when the user explicitly wants to rotate
a key.

Design notes (per ADR 1.3 §6):

- No env-var fallback. Keyring-or-fail. Matches okuro's global
  secrets convention.
- Keys live in the OS keyring behind Fernet (``KeyringStorage``); the
  wizard never echoes the value back.
- Config writes are atomic (``tmp`` + ``rename``) and preserve any
  existing top-level keys (``bridge``, ``llm``, …) — the YAML file is
  shared with the bridge and must not be clobbered.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml

from .config import default_config_path

# The two provider keys okuro voice needs. Adding a third provider later =
# extend this table. Signup URLs are printed verbatim so the user can
# click through a terminal that supports link detection.
REQUIRED_KEYS: list[tuple[str, str, str]] = [
    (
        "deepgram_api_key",
        "Deepgram (speech-to-text, Nova-3)",
        "https://console.deepgram.com/signup",
    ),
    (
        "elevenlabs_api_key",
        "ElevenLabs (text-to-speech, Flash v2.5)",
        "https://elevenlabs.io/app/sign-up",
    ),
]


@dataclass
class WizardResult:
    status: str          # "configured" | "already_configured" | "aborted"
    keys_set: list[str]  # keys that were newly written this run
    keys_present: list[str]  # all keys present at end of run
    config_path: Path    # path the wizard wrote to


def _already_has_keys(storage) -> list[str]:
    """Return the subset of REQUIRED_KEYS that is already in the keyring."""
    existing = set(storage.list_keys())
    return [name for name, _label, _url in REQUIRED_KEYS if name in existing]


def is_first_run(storage=None) -> bool:
    """True when any required voice key is missing from the keyring.

    Cheap check — safe to call from ``okuro voice`` hot path.
    """
    if storage is None:
        from okuro.keyring.storage import KeyringStorage
        storage = KeyringStorage()
    try:
        have = set(storage.list_keys())
    except Exception:
        return True
    return any(name not in have for name, _label, _url in REQUIRED_KEYS)


def _prompt_key(
    label: str,
    url: str,
    *,
    prompt_fn: Callable[[str], str],
    echo_fn: Callable[[str], None],
) -> Optional[str]:
    """Prompt the user for a single API key.

    Returns the key string, or ``None`` if the user entered nothing
    (treated as abort for this key — wizard surfaces it upstream).
    Hidden input is the caller's responsibility (see ``run_first_run_wizard``
    defaults); this function only formats the pre-prompt context.
    """
    echo_fn("")
    echo_fn(f"  {label}")
    echo_fn(f"  Sign up / manage keys: {url}")
    echo_fn("  Paste the API key (input hidden, Enter to skip):")
    try:
        val = prompt_fn("  > ")
    except (EOFError, KeyboardInterrupt):
        return None
    val = (val or "").strip()
    return val or None


def _enable_voice_in_config(path: Path) -> None:
    """Set ``voice.enabled: true`` in ``~/.okuro/config.yaml``, atomic.

    Preserves every other top-level block. Creates the file with a
    minimal ``voice:`` block if it doesn't yet exist. Never raises on a
    readable-but-malformed YAML — treats that as "start fresh for voice
    only" rather than ask the user to fix their config mid-wizard.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            data = yaml.safe_load(path.read_text()) or {}
            if not isinstance(data, dict):
                data = {}
        except yaml.YAMLError:
            # User has a broken config — surface that via `okuro doctor`
            # not here. Back up the broken file and start fresh only for
            # the voice block so we don't clobber any recoverable state.
            backup = path.with_suffix(path.suffix + ".broken")
            shutil.copy2(path, backup)
            data = {}
    else:
        data = {}

    voice_block = data.get("voice")
    if not isinstance(voice_block, dict):
        voice_block = {}
    voice_block["enabled"] = True
    data["voice"] = voice_block

    # Atomic write — tempfile in same dir, then rename. Preserves the
    # file's inode-target on the overwhelming-majority case where
    # ~/.okuro is on a single filesystem.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".config.", suffix=".yaml.tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def run_first_run_wizard(
    *,
    storage=None,
    config_path: Optional[Path] = None,
    prompt_fn: Optional[Callable[[str], str]] = None,
    echo_fn: Optional[Callable[[str], None]] = None,
    force: bool = False,
) -> WizardResult:
    """Run the first-run wizard.

    Parameters
    ----------
    storage
        ``KeyringStorage`` instance. Defaults to a fresh one.
    config_path
        Override ``~/.okuro/config.yaml`` — used by unit tests.
    prompt_fn
        Hidden-input prompt. Defaults to ``getpass.getpass`` so the
        key never appears on screen or in shell history.
    echo_fn
        Line printer. Defaults to ``click.echo``.
    force
        Re-prompt for keys even if all are present (rotation path).

    Returns
    -------
    WizardResult
        ``status`` is one of:
        - ``"already_configured"`` — no action taken, all keys present
          and ``force`` is False.
        - ``"configured"`` — at least one key was set and
          ``voice.enabled: true`` was written.
        - ``"aborted"`` — user skipped a required key; ``voice.enabled``
          is NOT flipped; partial writes are kept (skipping one provider
          doesn't invalidate the other).
    """
    if storage is None:
        from okuro.keyring.storage import KeyringStorage
        storage = KeyringStorage()
    if prompt_fn is None:
        import getpass
        prompt_fn = getpass.getpass
    if echo_fn is None:
        import click
        echo_fn = click.echo

    cfg_path = config_path or default_config_path()

    present_before = _already_has_keys(storage)
    all_names = [name for name, _label, _url in REQUIRED_KEYS]

    if not force and sorted(present_before) == sorted(all_names):
        return WizardResult(
            status="already_configured",
            keys_set=[],
            keys_present=present_before,
            config_path=cfg_path,
        )

    echo_fn("")
    echo_fn("okuro voice — first-run setup")
    echo_fn("")
    echo_fn("Voice uses two cloud providers. No local inference, no GPU required.")
    echo_fn("Keys are stored in your OS keyring (encrypted at rest via Fernet).")

    keys_set: list[str] = []
    aborted = False

    for name, label, url in REQUIRED_KEYS:
        if not force and name in present_before:
            echo_fn(f"  [=] {name} already set — keeping existing value.")
            continue

        value = _prompt_key(label, url, prompt_fn=prompt_fn, echo_fn=echo_fn)
        if value is None:
            echo_fn(f"  [!] {name} skipped — voice will not work without it.")
            aborted = True
            continue

        storage.add_key(name, value)
        keys_set.append(name)
        echo_fn(f"  [ok] {name} stored in keyring.")

    present_after = storage.list_keys()
    have_all = all(name in present_after for name in all_names)

    if have_all:
        _enable_voice_in_config(cfg_path)
        echo_fn("")
        echo_fn(f"voice.enabled: true  written to {cfg_path}")
        echo_fn("Try it:  okuro voice test")
        return WizardResult(
            status="configured",
            keys_set=keys_set,
            keys_present=[k for k in all_names if k in present_after],
            config_path=cfg_path,
        )

    echo_fn("")
    echo_fn("Wizard aborted — voice left disabled. Rerun:  okuro voice init")
    return WizardResult(
        status="aborted",
        keys_set=keys_set,
        keys_present=[k for k in all_names if k in present_after],
        config_path=cfg_path,
    )
