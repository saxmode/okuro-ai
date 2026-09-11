# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.trace — raw execution-trace store backing Meta-Harness P1.
# index: def ingest_all
# AGENT_HEADER_END -->
"""okuro.trace — raw agent execution-trace store.

Backs Meta-Harness P1 (full-trace access): prior agent sessions are
stored as raw events, not summaries, so a future agent can inspect
what actually happened (tool calls, errors, decisions) rather than
rely on the compressed gotchas/decisions in :mod:`okuro.sense`.

Provider-agnostic: tables distinguish sessions via a ``provider``
column. Each provider has its own ingester because CLIs store
transcripts differently on disk, but the query surface (MCP tools
in :mod:`okuro.trace.mcp_tools`) is unified.

Implemented providers:

* ``claude-code`` — reads ``~/.claude/projects/**/*.jsonl``
* ``codex``       — reads ``~/.codex/sessions/**/rollout-*.jsonl``
* ``antigravity`` — reads ``~/.gemini/antigravity-cli/conversations/*.db``
* ``gemini``      — reads ``~/.gemini/tmp/**/chats/session-*.json``
                    (HISTORICAL: the Gemini CLI was retired 2026-07-18; the
                    165 archived files are frozen but still the only record
                    of those 167 sessions, so the ingester stays)
"""

from __future__ import annotations

import logging

from .antigravity import ingest_antigravity
from .claude_code import ingest as ingest_claude_code
from .codex import ingest as ingest_codex
from .gemini import ingest as ingest_gemini

logger = logging.getLogger(__name__)


def ingest_all() -> dict:
    """Run every known provider ingester. Returns a per-provider summary.

    Each provider runs in its own try/except so one failed ingester does
    not silently kill the others — partial coverage is better than none.
    Failures are surfaced as ``{"error": "<msg>"}`` in the result dict and
    logged at WARNING.
    """
    results: dict = {}
    for name, fn in (
        ("claude-code", ingest_claude_code),
        ("codex", ingest_codex),
        ("antigravity", ingest_antigravity),
        ("gemini", ingest_gemini),
    ):
        try:
            results[name] = fn()
        except Exception as e:
            logger.warning("trace: ingester %s failed: %s", name, e)
            results[name] = {"error": str(e)}
    return results


__all__ = [
    "ingest_all",
    "ingest_antigravity",
    "ingest_claude_code",
    "ingest_codex",
    "ingest_gemini",
]
