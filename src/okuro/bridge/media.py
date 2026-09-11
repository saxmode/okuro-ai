# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The bridge's MEDIA seam — provider-routed generation that returns
#          FILES, not text. bridge.invoke() carries a string and can never carry
#          bytes, so every modality that produces a file (cloud image today; TTS,
#          video later) had no seam and lived outside the bridge. This is that
#          seam, with the Antigravity CLI (`agy`) as its first image provider.
# index:
#   imports
#   class MediaUnavailable
#   def providers_for / def availability
#   def generate_image        (prompt -> saved file paths + meta)
#   def _run_tool_prompt      (mechanism: steer a CLI's built-in tool)
#   def _harvest / def _build_instruction / def _nearest_aspect
# AGENT_HEADER_END -->
"""Provider-routed media generation — the bridge seam that returns files.

``okuro.bridge.invoke`` returns ``{"output": str}``. That is the whole reason
image/audio/video generation has never routed through the bridge: there is no
place for bytes in its contract. This module adds the missing seam, keeping the
bridge's shape — resolve a provider for a *modality*, run it, return a dict —
while carrying ``paths`` instead of ``output``.

Mechanisms
==========
A provider serves a modality through a declared *mechanism*, so adding one is
data plus one adapter function rather than a new call path per vendor:

* ``tool-prompt`` — the CLI is an *agent* with a built-in generation tool. We
  steer it with an instruction, then harvest the files it wrote. This is how
  ``agy`` works: it has no image flag and no image model in ``agy models``; the
  capability lives behind its ``generate_image`` tool.

Availability
============
A cloud CLI is only usable if the user installed AND authenticated it. That is
exactly ``cli_probe.detect(...).ready``, the same SSOT the wizard, dashboard and
bridge router consult — never a bare ``which``, which is green for a signed-out
binary. Callers should gate on :func:`availability` and degrade gracefully;
:func:`generate_image` raises :class:`MediaUnavailable` if they don't.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .config import get_provider, load_config

log = logging.getLogger("okuro.bridge.media")

IMAGE = "image"

# agy writes generated files into its own per-conversation scratch dir and gives
# no flag to redirect them. We harvest from there and copy out — anything left
# behind is agy's to garbage-collect.
_AGY_BRAIN = Path.home() / ".gemini" / "antigravity-cli" / "brain"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

# Aspect ratios agy's generate_image accepts (self-reported by the tool; 16:9
# and the 1:1 default are the two verified against real output).
_ASPECTS: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0), ("2:3", 2 / 3), ("3:2", 1.5), ("3:4", 0.75),
    ("4:3", 4 / 3), ("9:16", 9 / 16), ("16:9", 16 / 9),
)

_DEFAULT_TIMEOUT = 300
_MAX_ATTEMPTS = 2


class MediaUnavailable(RuntimeError):
    """No installed + authenticated provider can serve this modality."""


def providers_for(modality: str = IMAGE) -> list[str]:
    """Provider ids declaring support for ``modality``, in priority order."""
    config = load_config()
    out = []
    for pid, pconf in (config.get("providers") or {}).items():
        if (pconf.get("media") or {}).get(modality):
            out.append(pid)
    return out


def availability(modality: str = IMAGE) -> dict:
    """Readiness for ``modality`` in the shape a UI can render directly.

    Returns ``{available, provider, state, message, providers}``. ``state`` is
    the ``cli_probe`` auth state of the chosen provider, so the caller can tell
    "not installed" from "installed but signed out" — two different fixes.
    """
    from okuro.system import cli_probe

    candidates = providers_for(modality)
    checked: list[dict] = []
    for pid in candidates:
        try:
            state = cli_probe.detect(pid)
        except ValueError:  # provider outside cli_probe's canonical set
            continue
        entry = {
            "provider": pid, "installed": state.on_path,
            "authenticated": state.auth.ready, "ready": state.ready,
            "detail": state.auth.detail,
        }
        checked.append(entry)
        if state.ready:
            return {"available": True, "provider": pid, "state": "ready",
                    "message": "Ready to generate", "providers": checked}

    if not candidates:
        message = "No provider supports image generation"
        state = "unsupported"
    elif any(c["installed"] for c in checked):
        message = "Sign in to the Antigravity CLI — run `agy` and complete sign-in"
        state = "signed_out"
    else:
        message = ("Install the Antigravity CLI to generate images without a GPU — "
                   "`curl -fsSL https://antigravity.google/cli/install.sh | bash`")
        state = "not_installed"
    return {"available": False, "provider": None, "state": state,
            "message": message, "providers": checked}


def _nearest_aspect(width: Optional[int], height: Optional[int]) -> Optional[str]:
    """Map a pixel bucket to the closest aspect ratio the provider accepts.

    Studio speaks in resolution buckets (a ComfyUI notion); agy speaks in aspect
    ratios and picks its own pixel dimensions. Translating keeps one user-facing
    vocabulary instead of adding a cloud-only knob.
    """
    if not width or not height:
        return None
    target = width / height
    return min(_ASPECTS, key=lambda a: abs(a[1] - target))[0]


def text_constraint(literal_text: Optional[str]) -> str:
    """The clause that stops the image model inventing text. Empty when unwanted.

    LAYERING — this is the whole point, and the earlier version got it wrong:
    the constraint MUST end up inside the ``Prompt`` argument, because ``Prompt``
    is the only string the image model ever sees. Putting it in the surrounding
    instruction addresses the CLI's *text* agent, which then dutifully passes the
    prompt through verbatim and drops the constraint on the floor. Two escalating
    wordings in the instruction changed nothing, for exactly that reason.

    Two shapes, both measured:

    * ``literal_text=""`` → a text-free plate (nothing rendered at all).
    * ``literal_text="STUDIO"`` → allowlist + enumeration. The allowlist sentence
      ALONE stops invented copy but corrupts the glyphs (mirrored duplicates);
      the enumeration is what keeps them clean. Content obedience and glyph
      correctness are separate failure modes, so both sentences stay.

    ``None`` returns "" — no constraint. Photographic prompts want no such clause,
    and applying it unconditionally is noise.
    """
    if literal_text is None:
        return ""
    forbidden = (
        "Do not add a subtitle, a date, a year, a venue, an address, opening "
        "hours, a curator credit, a designer credit, a logo, a caption or any "
        "small print."
    )
    if not literal_text.strip():
        return (
            " There is NO text anywhere in the image — no letters, numbers, words "
            f"or characters of any kind. {forbidden}"
        )
    word = literal_text.strip()
    return (
        f' The ONLY text anywhere in the image is "{word}". No other letters, '
        f"numbers, words or characters appear anywhere. {forbidden} Every part of "
        f'the image that is not "{word}" is empty colour or abstract geometric shape.'
    )


def _build_instruction(prompt: str, name: str, aspect: Optional[str],
                       references: list[str]) -> str:
    """Instruction that pins the agent to one tool call with unaltered arguments.

    Note what is NOT here: any constraint on what the image should contain. Those
    belong in ``prompt`` (see :func:`text_constraint`) — the agent forwards
    ``Prompt`` to the image model and keeps everything else to itself, so a
    picture-level rule written here would never reach the model that draws.
    """
    args = [f"  ImageName: {name}", f"  Prompt: {prompt}"]
    if aspect:
        args.append(f"  AspectRatio: {aspect}")
    if references:
        args.append("  ImagePaths: " + json.dumps(references))
    return (
        "Call the generate_image tool exactly once, with exactly these arguments:\n"
        + "\n".join(args)
        + "\n\nDo not rewrite, translate, shorten or embellish the Prompt — pass it "
        "through verbatim, including every constraint it states. Call no other "
        "tool. When the tool returns, reply with the absolute file path it "
        "produced and nothing else."
    )


def _harvest(conversation_id: str, since: float) -> list[Path]:
    """Files the run produced, newest last.

    Reads the conversation's scratch dir rather than parsing the reply, because
    the directory is ground truth while the reply is model-authored prose that
    may wrap, annotate, or omit the path. ``since`` guards against picking up a
    file from a reused conversation id.
    """
    d = _AGY_BRAIN / conversation_id
    if not d.is_dir():
        return []
    found = [
        f for f in d.iterdir()
        if f.is_file()
        and f.suffix.lower() in _IMAGE_SUFFIXES
        and f.stat().st_mtime >= since
    ]
    return sorted(found, key=lambda f: f.stat().st_mtime)


def _run_tool_prompt(
    binary: str, spec: dict, instruction: str, *, timeout: int,
) -> tuple[list[Path], dict]:
    """Mechanism ``tool-prompt``: run the agent once and harvest what it wrote.

    Uses the flat ``--output-format json`` envelope (NOT ``stream-json``, whose
    terminal event nests the same fields under ``result``) purely to recover the
    conversation id, which names the scratch dir to harvest.
    """
    cmd = [binary, "-p", instruction, *spec.get("flags", [])]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [], {"error": f"provider exceeded {timeout}s"}
    if proc.returncode != 0:
        return [], {"error": (proc.stderr or proc.stdout or "").strip()[:400]
                    or f"provider exited {proc.returncode}"}
    try:
        envelope = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return [], {"error": "provider did not return the expected JSON envelope"}
    # stream-json nests under `result`; json is flat. Accept either so a flag
    # change upstream degrades to a parse, not a silent empty harvest.
    envelope = envelope.get("result", envelope)
    conversation_id = envelope.get("conversation_id") or ""
    reply = (envelope.get("response") or "").strip()
    meta = {
        "provider_status": envelope.get("status"),
        "duration": envelope.get("duration_seconds"),
        "conversation_id": conversation_id,
        "reply": reply,
        "usage": envelope.get("usage") or {},
    }
    # A refusal exits 0 with status=ERROR and the REASON in `response` — quota
    # exhaustion, a safety block, a provider outage. Surfacing "try rephrasing
    # the prompt" over the top of "Quota exhausted, resets in ~3.5 hours" sends
    # the user to fix something that isn't broken.
    if str(envelope.get("status", "")).upper() == "ERROR":
        detail = reply or envelope.get("error") or "provider reported an error"
        return [], {**meta, "error": detail, "terminal": True}
    if not conversation_id:
        return [], {**meta, "error": "provider returned no conversation id"}
    return _harvest(conversation_id, started), meta


_MECHANISMS: dict[str, Callable] = {"tool-prompt": _run_tool_prompt}


def generate_image(
    prompt: str,
    *,
    dest_dir: str | Path,
    name: str = "okuro_image",
    width: Optional[int] = None,
    height: Optional[int] = None,
    aspect_ratio: Optional[str] = None,
    reference_images: Optional[list[str]] = None,
    literal_text: Optional[str] = None,
    provider: Optional[str] = None,
    timeout: Optional[int] = None,
    on_progress: Optional[Callable[[str, str, Optional[int]], None]] = None,
) -> dict:
    """Generate an image through a CLI provider; return the saved file paths.

    ``reference_images`` turns this into an edit: the provider is given the
    source files and the prompt describes the change. ``aspect_ratio`` wins over
    ``width``/``height``, which are otherwise mapped to the nearest ratio the
    provider accepts.

    ``literal_text`` pins what may be rendered — a word/phrase to allow, ``""``
    for a text-free image, or ``None`` (default) for no constraint. Without it
    the model invents captions, dates, venues and plausible real credits; see
    :func:`text_constraint`.

    Returns ``{paths, count, provider, aspect_ratio, prompt_used, reply, meta}``.

    Raises:
        MediaUnavailable: no installed + authenticated provider for images.
        RuntimeError: the provider ran but produced no image.
    """
    if not (prompt or "").strip():
        raise ValueError("generate_image requires a non-empty prompt")

    avail = availability(IMAGE)
    provider_id = provider or avail.get("provider")
    if not provider_id:
        raise MediaUnavailable(avail["message"])
    if provider and not avail["available"]:
        # An explicitly named provider still has to be usable.
        raise MediaUnavailable(avail["message"])

    pconf = get_provider(provider_id) or {}
    spec = (pconf.get("media") or {}).get(IMAGE)
    if not spec:
        raise MediaUnavailable(f"provider {provider_id!r} does not serve images")
    runner = _MECHANISMS.get(spec.get("mechanism", ""))
    if runner is None:
        raise MediaUnavailable(
            f"provider {provider_id!r} declares unknown mechanism "
            f"{spec.get('mechanism')!r}")

    from okuro.system import cli_probe

    binary = cli_probe.detect(provider_id).path
    if not binary:
        raise MediaUnavailable(avail["message"])

    aspect = aspect_ratio or _nearest_aspect(width, height)
    refs = [str(Path(r).expanduser()) for r in (reference_images or [])]
    if refs and not spec.get("supports_reference_images"):
        log.info("media: provider %s ignores reference images", provider_id)
        refs = []
    # Into the PROMPT, not the instruction — the image model reads only this.
    effective_prompt = prompt + text_constraint(literal_text)
    instruction = _build_instruction(effective_prompt, name, aspect, refs)
    budget = timeout or spec.get("timeout") or _DEFAULT_TIMEOUT

    # The provider is an agent, not an API: it can answer in prose instead of
    # calling its tool. One retry converts that flake into a slow success.
    produced: list[Path] = []
    meta: dict = {}
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if on_progress:
            on_progress("generate", "Generating image…" if attempt == 1
                        else "Retrying — the provider skipped the image tool", None)
        produced, meta = runner(binary, spec, instruction, timeout=budget)
        if produced:
            break
        if meta.get("error"):
            log.warning("media: %s attempt %d failed: %s",
                        provider_id, attempt, meta["error"])
        if meta.get("terminal"):
            # The provider explained itself (quota, safety, outage). Retrying
            # cannot help and costs another minute.
            break
    if not produced:
        raise RuntimeError(
            meta.get("error")
            or "the provider returned no image — try rephrasing the prompt")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "image"
    paths = []
    for i, src in enumerate(produced):
        out = dest / f"agy_{stamp}_{safe}_{i}{src.suffix.lower()}"
        shutil.copy2(src, out)
        paths.append(str(out))
        # The provider's scratch dir is its own to prune; leaving the source
        # avoids racing whatever else that conversation is still writing.
    if on_progress:
        on_progress("generate", "Image ready", 100)
    return {
        "paths": paths, "count": len(paths), "provider": provider_id,
        "aspect_ratio": aspect, "prompt_used": effective_prompt,
        "literal_text": literal_text,
        "reply": meta.get("reply", ""), "meta": meta,
    }
