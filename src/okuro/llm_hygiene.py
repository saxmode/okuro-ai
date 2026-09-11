# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: LLM output hygiene — strip known contaminants from one-shot CLI replies.
# index: def strip_bootstrap_greeting | _CLI_NOISE_PATTERNS
# AGENT_HEADER_END -->
"""LLM output hygiene — strip known contaminants from one-shot CLI replies.

Both the orchestrator's ``decomposer.call_llm`` path and the ``bridge.invoke``
path eventually ``subprocess.run`` a CLI (claude/gemini/codex) that reads
``~/.claude/CLAUDE.md`` (or equivalent) on every invocation. Two classes of
contamination get prepended to the actual model answer:

1. The OKURO bootstrap greeting block — instructed by CLAUDE.md, every
   first reply leads with ``**OKURO 3.0**\\n<provider> is bootstrapped\\n…\\n---``.
2. CLI-emitted status preambles — e.g. gemini CLI prints
   ``MCP issues detected. Run /mcp list for status.`` directly before the
   model output when any configured MCP server has a warning.

Downstream parsers (YAML, JSON, structured suggestion extractors) choke on
either prefix, producing silent parse failures or fields like
``continuation_suggestion`` that literally store the greeting as their
value. This module is the single choke-point that fixes the contamination
for every consumer, so removing it from one caller automatically removes
it from all of them.
"""

from __future__ import annotations

import re

# Matches everything from the opening ``**OKURO ...**`` banner through the
# first ``---`` separator line that closes the greeting block. DOTALL so
# the inner profile summary (which spans several lines) is eaten too.
_BOOTSTRAP_PREFIX_RE = re.compile(
    r"\A\s*\*\*OKURO[^\n]*\*\*.*?\n---\s*\n",
    re.DOTALL,
)

# CLI-emitted status preambles. Each pattern is matched at the head of the
# string (after any prior bootstrap-greeting strip) and removed if present.
# Add new patterns here when a CLI grows a new always-on banner — that is
# the systematic place to handle them, not the call site.
_CLI_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # gemini CLI: prints when any MCP server has a warning. Whole-line match
    # because gemini emits it without a trailing newline before model output.
    re.compile(r"\A\s*MCP issues detected\. Run /mcp list for status\.\s*"),
)


def strip_bootstrap_greeting(text: str) -> str:
    """Strip OKURO bootstrap greeting + known CLI noise preambles from ``text``.

    Returns the original string unchanged when nothing matches, so it is
    safe to wrap every CLI call unconditionally. Name kept for backward
    compatibility — the function now also strips CLI status preambles
    listed in ``_CLI_NOISE_PATTERNS``.
    """
    if not text:
        return text

    # Step 1 — bootstrap greeting block.
    match = _BOOTSTRAP_PREFIX_RE.search(text)
    if match:
        text = text[match.end():].lstrip()
    else:
        # Fallback heuristic — the greeting format may drift (different CLI
        # skipped the trailing ``---``, or a model paraphrased). If "is
        # bootstrapped" shows up in the first 1000 characters, drop
        # everything up to the next blank line.
        head = text[:1000]
        if "is bootstrapped" in head:
            anchor = head.find("is bootstrapped")
            blank = text.find("\n\n", anchor)
            if blank != -1 and blank < 2000:
                text = text[blank + 2:].lstrip()

    # Step 2 — CLI noise preambles. Apply each pattern once at the head;
    # iterate in case a CLI ever emits multiple stacked banners.
    changed = True
    while changed:
        changed = False
        for pattern in _CLI_NOISE_PATTERNS:
            stripped = pattern.sub("", text, count=1)
            if stripped != text:
                text = stripped.lstrip()
                changed = True

    return text
