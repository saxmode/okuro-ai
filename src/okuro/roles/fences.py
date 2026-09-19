# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: One fence engine for the whole roles subsystem — the character
#   ranges of fenced code blocks, and a search that refuses to match inside
#   one.
# index: _FENCE | def fence_spans | def search_unfenced
# AGENT_HEADER_END -->
"""Text a role is told to PRODUCE is not text the role HAS.

Ten role bodies carry an OUTPUT TEMPLATE inside a fenced code block, and those
templates contain markdown the role is instructed to emit — ``## RESEARCH
SUMMARY``, ``## GAP``, ``**Archetype:**``. Measured 2026-09-17: compressor,
documenter, interface-specialist, role-researcher, system-documenter,
business-researcher, orchestrator, phase-summarizer, sys-engineer,
sys-researcher.

Any tool that reads a role body by pattern has to skip those ranges, and
before this module there were two answers to that in one package: the repair
engine skipped them (``split_h2``) and the GATE did not. So
``role-researcher`` passed the structure audit on an ``**Archetype:**`` and a
``Neurotype Balance`` that exist only inside the template it hands its
adopter, while having no persona block of its own — and the repair engine,
asked to fix it, agreed with the gate's mistake because ``missing_full`` read
the same raw text.

A second fence detector would have made that divergence permanent instead of
accidental. This module is the one detector, imported by both.

WHAT IS NOT HANDLED, DELIBERATELY. An UNCLOSED fence is not a fence here:
:data:`_FENCE` requires the closing delimiter, so a body that opens a block
and never closes it is read as ordinary text to the end. That is the
behaviour ``repair_plan`` has always had, and migration 156's emitted SQL is
asserted byte-identical against it — changing the rule here would silently
re-compute a shipped migration. If an unclosed fence ever matters, it is its
own change with its own measurement.
"""

from __future__ import annotations

import re

#: A fenced code block, either delimiter, closing on the one it opened with.
_FENCE = re.compile(r"(?ms)^(```|~~~).*?^\1[ \t]*$")


def fence_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges of fenced code blocks, delimiters included.

    Ten roles embed an OUTPUT TEMPLATE in a fence, and those templates contain
    ``## ...`` lines — compressor's "## Locked ADRs", documenter's "## GAP",
    interface-specialist's "## CLARIFICATION NEEDED", role-researcher's
    "## RESEARCH SUMMARY". Those are the text the role is told to PRODUCE, not
    sections of the role. Reading them as sections made the repair reflow a
    template, rank an inserted section among fake siblings, and report a body
    as structurally different from what it is.
    """
    return [(m.start(), m.end()) for m in _FENCE.finditer(text or "")]


def in_fence(position: int, spans: list[tuple[int, int]]) -> bool:
    """Whether ``position`` falls inside one of ``spans``."""
    return any(start <= position < end for start, end in spans)


def search_unfenced(pattern, text: str, flags: int = 0):
    """First match of ``pattern`` in ``text`` that does NOT start in a fence.

    A drop-in replacement for ``re.search`` wherever the text is a role body.
    The match START is what decides: a section heading begins a line, and a
    heading whose line begins inside a fenced template belongs to the
    template.

    Returns the match object so a caller can read its span, or ``None`` — the
    same contract as ``re.search``, so ``if not search_unfenced(...)`` reads
    the way the code it replaces read.
    """
    body = text or ""
    spans = fence_spans(body)
    if not spans:
        return re.search(pattern, body, flags)
    for match in re.finditer(pattern, body, flags):
        if not in_fence(match.start(), spans):
            return match
    return None
