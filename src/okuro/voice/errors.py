# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Typed voice errors surfaced to the CLI with actionable guidance.
# index:
#   class VoiceError
#   class MissingKeyError
#   class NoDeviceError
#   class MicPermissionError
#   class STTError
#   class TTSError
#   class VADError
#   class BridgeError
# AGENT_HEADER_END -->
"""Typed errors for the voice pipeline.

Every error carries:
- ``hint`` — a short, user-facing next-step (e.g. "okuro keys set …").
- ``exit_code`` — the process exit code the CLI should use on uncaught raise.

This mirrors the failure-mode table in ADR 1.3 §4: each upstream failure must
print guidance and exit with a distinct code so install-doctor-type tooling
can interpret what happened.
"""

from __future__ import annotations


class VoiceError(Exception):
    """Base class for all voice errors.

    Attributes
    ----------
    hint : str
        Plain-English next-step. Printed by ``okuro voice`` on uncaught raise.
    exit_code : int
        Process exit code. ``2`` is reserved for "no audio device", ``3`` for
        "missing API key", ``4`` for permission issues, ``5`` for provider
        protocol errors, ``6`` for bridge failures, ``1`` for everything else.
    """

    hint: str = ""
    exit_code: int = 1

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class MissingKeyError(VoiceError):
    exit_code = 3

    def __init__(self, key_name: str) -> None:
        super().__init__(
            f"API key '{key_name}' not found in okuro keyring.",
            hint=f"Run: okuro keys set {key_name}",
        )
        self.key_name = key_name


class NoDeviceError(VoiceError):
    exit_code = 2
    hint = "Run: okuro voice devices  — to list what the system sees."


class MicPermissionError(VoiceError):
    """Raised when the OS refuses microphone access.

    On macOS this is the TCC sandbox denying the parent terminal.
    On Linux this is almost always a missing `audio` group membership
    or PulseAudio/PipeWire not being reachable in this session.
    """

    exit_code = 4
    hint = (
        "macOS: System Settings → Privacy & Security → Microphone → enable "
        "for your terminal app.\nLinux: verify the user is in the 'audio' "
        "group and PulseAudio/PipeWire is running."
    )


class STTError(VoiceError):
    exit_code = 5


class TTSError(VoiceError):
    exit_code = 5


class VADError(VoiceError):
    exit_code = 5


class BridgeError(VoiceError):
    exit_code = 6
