# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Provider-agnostic detector — codebase-intelligence bypass (search/absence-claim without cortex_scope).
# index: imports | events | detection core | claude normalizer | run_stop_hook
# AGENT_HEADER_END -->
"""Codebase-intelligence bypass detector.

Catches the failure mode where an agent concludes "the code does not exist"
after searching, without ever calling ``cortex_scope`` to learn which repos
are indexed — the exact miss that lets a scoped-empty search read as absence
while the real code sits in an unsearched managed clone.

Design — provider-agnostic core + per-provider plumbing:

- ``detect_codebase_intel_bypass(events)`` is PURE and provider-neutral. It
  operates on a normalized event stream (``kind`` in {"user","text","tool"}),
  so ANY provider that can supply that stream reuses it verbatim. This is the
  reusable half — the day a Gemini/Codex transcript adapter captures tool
  calls, the same detector lights up with zero changes.
- ``normalize_claude_transcript`` + ``run_stop_hook`` are the Claude-specific
  plumbing (Claude Code is the only provider whose hook sees tool calls today;
  gemini=final-text-only, codex/cursor/antigravity=no hook). They translate
  Claude's transcript JSONL into the neutral event stream and append any
  violation to the shared ``profile-violations.jsonl`` using the same schema
  the profile-compliance detectors write, so it surfaces in the existing
  "Frequent failure modes" telemetry.

Log-only by construction — never blocks, never raises out of the hook.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

# Bucket key used in the violations log + failure-modes block. Registered in
# _profile_compliance._BUCKET_DIRECTIVES so it renders with a directive.
BUCKET = "codebase_intel_bypass"

# Tool names that constitute a "code search" action, across providers. Cortex
# semantic/literal search, plus the generic built-in search tools different
# CLIs expose.
_SEARCH_TOOL_NAMES: frozenset[str] = frozenset({
    "cortex_search", "cortex_search_code", "cortex_route",
    "Grep", "Glob", "search_file_content", "codebase_search", "file_search",
})

# Generic shell/exec tool names across providers; a shell call only counts as a
# search when its command actually runs a search utility (below).
_SHELL_TOOL_NAMES: frozenset[str] = frozenset({
    "Bash", "shell", "run_terminal_cmd", "execute_command", "run_command",
})

_SHELL_SEARCH_RE = re.compile(r"\b(grep|rg|ripgrep|find|ag|ack|fd)\b")

# The tool that, when present anywhere in the session, means the agent DID
# orient itself — its presence suppresses the detector entirely.
_SCOPE_TOOL_NAME = "cortex_scope"

# Absence-of-code claims. Each pattern carries a code/impl/source noun (or is
# an unambiguous code idiom like "docs-only") so ordinary "no X" sentences
# don't trip it. Matched case-insensitively against assistant text only.
_ABSENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bno\s+[\w\-]{0,20}\s*code\b", re.I),
    re.compile(r"\bcode\b[^.\n]{0,30}\b(?:does\s*n[o']?t|do\s+not|doesn'?t)\s+exist", re.I),
    re.compile(r"\b(?:does\s*n[o']?t|doesn'?t)\s+exist[^.\n]{0,30}\bcode\b", re.I),
    re.compile(r"\bdocs?[-\s]only\b", re.I),
    re.compile(r"\bspec[-\s]only\b", re.I),
    re.compile(r"\bnot\s+implemented\b", re.I),
    re.compile(r"\bno\s+(?:implementation|source\s+code|application\s+code|backend|actual\s+code)\b", re.I),
    re.compile(r"\b(?:there\s+(?:is|are)\s+no|there'?s\s+no|isn'?t\s+any)\b[^.\n]{0,30}\b(?:code|implementation|source)\b", re.I),
)


# ── detection core (PURE, provider-agnostic) ─────────────────────────────────


def _is_search_event(ev: dict) -> bool:
    """True when a tool event represents a code-search action."""
    if ev.get("kind") != "tool":
        return False
    name = ev.get("name") or ""
    if name in _SEARCH_TOOL_NAMES:
        return True
    if name in _SHELL_TOOL_NAMES:
        return bool(_SHELL_SEARCH_RE.search(ev.get("input_text") or ""))
    return False


def _absence_claim(text: str) -> str:
    """Return the matched absence-claim snippet, or '' if none."""
    for pat in _ABSENCE_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(0).strip()
    return ""


def detect_codebase_intel_bypass(events: list[dict]) -> list[str]:
    """Detect a codebase-intelligence bypass in a normalized event stream.

    Fires when, in the CURRENT turn (the events after the last ``user`` event),
    the assistant makes a code-absence claim that is preceded by a code-search
    action, while ``cortex_scope`` was NEVER called anywhere in the session.

    Per-turn attribution (claim scoped to the current turn; scope/search checked
    across the whole session) means a Stop hook re-scanning the full transcript
    every turn logs the violation at most once — on the turn the claim is made.

    Returns a list with a single ``"codebase_intel_bypass=true but …"`` string
    on a hit, else ``[]``. The string shape matches the profile-compliance
    detectors so the shared bucketing (_bucket_for_violation) recognizes it.
    """
    if not events:
        return []

    # Whole-session signals.
    scope_called = any(
        ev.get("kind") == "tool" and ev.get("name") == _SCOPE_TOOL_NAME
        for ev in events
    )
    if scope_called:
        return []

    first_search_idx = next(
        (i for i, ev in enumerate(events) if _is_search_event(ev)), None
    )
    if first_search_idx is None:
        return []

    # Current turn = events after the last user message.
    last_user_idx = max(
        (i for i, ev in enumerate(events) if ev.get("kind") == "user"),
        default=-1,
    )

    for i in range(max(last_user_idx + 1, 0), len(events)):
        ev = events[i]
        if ev.get("kind") != "text":
            continue
        if i <= first_search_idx:
            continue  # claim must follow a search
        snippet = _absence_claim(ev.get("text") or "")
        if snippet:
            return [
                f"{BUCKET}=true but claimed code absent (\"{snippet}\") after "
                "searching without calling cortex_scope"
            ]
    return []


# ── Claude transcript normalizer (provider-specific plumbing) ────────────────


def normalize_claude_transcript(transcript_path: str) -> list[dict]:
    """Translate a Claude Code transcript JSONL into the neutral event stream.

    Record shape (see cortex ClaudeJSONLAdapter): each line is a JSON object
    with ``type`` and ``message.content`` — a string (user) or a list of blocks
    (assistant) where blocks are ``text`` / ``tool_use`` / ``tool_result`` /
    ``thinking``. Emits, in order:
      - ``{"kind": "user"}``                      per user turn (boundary marker)
      - ``{"kind": "text", "text": str}``         per assistant text block
      - ``{"kind": "tool", "name", "input_text"}`` per tool_use block

    tool_result and thinking blocks are ignored (a tool_result echoing the word
    "code" must never be read as the assistant's own claim). Best-effort:
    malformed lines are skipped; never raises.
    """
    events: list[dict] = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if rtype not in ("user", "assistant"):
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content")

                if rtype == "user":
                    events.append({"kind": "user"})
                    continue

                # assistant: content is normally a list of blocks; tolerate str.
                if isinstance(content, str):
                    if content.strip():
                        events.append({"kind": "text", "text": content})
                    continue
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        txt = block.get("text", "")
                        if txt.strip():
                            events.append({"kind": "text", "text": txt})
                    elif btype == "tool_use":
                        events.append({
                            "kind": "tool",
                            "name": block.get("name", ""),
                            "input_text": json.dumps(
                                block.get("input", {}), ensure_ascii=False
                            ),
                        })
                    # tool_result / thinking: intentionally ignored.
    except OSError:
        return events
    return events


# ── Claude Stop-hook entry point ─────────────────────────────────────────────


def _violations_log_path() -> str:
    # Reuse the shared path so this detector feeds the same telemetry pipeline.
    from ._profile_compliance import _violations_log_path as _p
    return _p()


def run_stop_hook(data: dict[str, Any]) -> None:
    """Claude Stop-hook entry: detect + append a violation to the shared log.

    ``data`` is the hook's stdin payload (Claude Code passes ``transcript_path``
    + ``stop_hook_active``). Log-only and fully defensive: any failure is
    swallowed so the hook always exits clean.
    """
    try:
        transcript_path = data.get("transcript_path")
        if not transcript_path:
            return
        import os
        if not os.path.isfile(transcript_path):
            return

        events = normalize_claude_transcript(transcript_path)
        violations = detect_codebase_intel_bypass(events)
        if not violations:
            return

        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "detector": BUCKET,
            "violations": violations,
        }
        log_path = _violations_log_path()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


__all__ = [
    "BUCKET",
    "detect_codebase_intel_bypass",
    "normalize_claude_transcript",
    "run_stop_hook",
]
