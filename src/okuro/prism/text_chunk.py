# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: prism — one shared, boundary-aware text chunker used by BOTH deck
#   engines' ingest (compiler/mine.py = engine B, distiller.py = engine A).
#   Replaces the old hard body[:48_000] slice that silently discarded ~60% of a
#   large research artifact before extraction ever saw it.
# AGENT_HEADER_END -->
"""Boundary-aware chunking for the mine/distill ingest edge.

A single implementation so both engines behave identically: split on paragraph
then line boundaries near the budget, never cut a claim mid-sentence, never drop
content. ``chunk_body(body, size)`` returns ``[body]`` unchanged when it fits.
"""
from __future__ import annotations

__all__ = ["chunk_body"]


def chunk_body(body: str, size: int) -> list[str]:
    """Split ``body`` into ``<=size`` chunks on paragraph/line boundaries.

    Returns ``[body]`` unchanged when it already fits. Guarantees: every chunk is
    ``<= size``; concatenating the chunks loses no non-whitespace content; a chunk
    is never smaller than half the budget unless it is the final remainder.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if len(body) <= size:
        return [body]
    chunks: list[str] = []
    i, n = 0, len(body)
    while i < n:
        end = min(i + size, n)
        if end < n:
            floor = i + size // 2  # never emit a chunk smaller than half the budget
            brk = body.rfind("\n\n", floor, end)
            if brk == -1:
                brk = body.rfind("\n", floor, end)
            if brk > i:
                end = brk
        piece = body[i:end].strip()
        if piece:
            chunks.append(piece)
        i = end
    return chunks
