# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: cAST AST-aware code chunking (arXiv 2506.15655) over tree-sitter — split-then-merge, lossless, language-agnostic.
# index:
#   imports
#   EXT_TO_LANGUAGE
#   def language_for_path
#   def nonws_len
#   def cast_chunk_code
#   def _chunk_nodes
# AGENT_HEADER_END -->
"""cAST — Chunking via Abstract Syntax Trees (audit F10).

Faithful implementation of the cAST split-then-merge algorithm
(arXiv 2506.15655 / EMNLP 2025, Algorithm 1) on tree-sitter grammars from
``tree-sitter-language-pack``.

Properties (all enforced by tests):
  * SIZE METRIC = number of NON-WHITESPACE characters (the paper's metric —
    not tokens, not lines).
  * LOSSLESS — concatenating the emitted chunks reproduces the original source
    byte-for-byte. We chunk on contiguous byte ranges that tile the whole file
    (including the inter-node whitespace/gaps), so nothing is dropped or
    duplicated.
  * LANGUAGE-AGNOSTIC — the only per-language input is the grammar; there are
    no language-specific node-type heuristics. Recursion is purely structural
    (a node's ``children``).
  * GRACEFUL — callers fall back to the line/token chunker on unknown language,
    missing grammar, or any parse error (handled by the integration site, but
    ``cast_chunk_code`` also returns ``None`` to signal "cannot AST-chunk").

The recursion mirrors Algorithm 1: ChunkNode greedily accumulates sibling
spans; a span that alone exceeds the budget is recursed into its children; a
leaf bigger than the budget is emitted whole (a single token can't be split
without breaking losslessness — the paper keeps such atomic spans intact).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Extension → tree-sitter-language-pack grammar name. Only languages with a
# grammar in the pack; everything else falls back to the line chunker.
EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".scala": "scala",
    ".kt": "kotlin",
    ".swift": "swift",
    ".lua": "lua",
}


def language_for_path(file_path: Path) -> Optional[str]:
    """Grammar name for a file's extension, or None if not an AST-chunk target."""
    return EXT_TO_LANGUAGE.get(file_path.suffix.lower())


def nonws_len(text: str) -> int:
    """The cAST size metric: count of non-whitespace characters."""
    return sum(1 for ch in text if not ch.isspace())


def _get_parser(language: str):
    """Build a tree-sitter Parser for ``language``.

    Uses ``get_parser`` — the SAME stack and API that cortex.codegraph.ingestor
    uses. Prefers the pinned ``tree_sitter_languages`` (0.21 API) and falls back
    to ``tree_sitter_language_pack`` (0.25 API) on hosts where the former is
    absent (newer Pythons). ``get_parser(name).parse(bytes)`` returns a tree
    whose nodes expose ``children`` / ``start_byte`` / ``end_byte`` / ``type`` —
    identical traversal across 0.21 and 0.25; only parser construction differs.
    Raises on a missing grammar; the caller treats any exception as "fall back to
    the line chunker".
    """
    import warnings

    try:
        from tree_sitter_languages import get_parser
    except ImportError:
        from tree_sitter_language_pack import get_parser

    with warnings.catch_warnings():
        # tree-sitter-languages triggers a Language(path,name) deprecation
        # FutureWarning internally on 0.21; mirror ingestor.py and suppress it.
        warnings.simplefilter("ignore", FutureWarning)
        return get_parser(language)


def cast_chunk_code(
    source: str,
    language: str,
    max_chars: Optional[int] = None,
) -> Optional[list[str]]:
    """cAST-chunk ``source`` (a code string) for ``language``.

    Returns a list of chunk strings whose concatenation == ``source`` exactly
    (lossless), each with non-whitespace size ≤ ``max_chars`` where the AST
    permits. Returns ``None`` if AST chunking is unavailable (no grammar / parse
    failure) so the caller can fall back. An empty/whitespace-only source
    returns ``[source]`` unchanged.

    ``max_chars`` defaults to ``OKURO_CORTEX_CAST_MAX_CHARS`` (4000 — the paper
    used a 4000-char budget for RepoEval, and it fits the embed window:
    ≈ chunk_tokens 1024 × 4 chars/token ≤ OKURO_EMBED_MAX_SEQ 2048 tokens).
    """
    if max_chars is None:
        max_chars = _default_max_chars()

    if not source:
        return [source]
    if nonws_len(source) <= max_chars:
        # Whole file fits — one chunk (Algorithm 1 base case).
        return [source]

    try:
        parser = _get_parser(language)
        data = source.encode("utf-8")
        tree = parser.parse(data)
    except Exception:
        return None

    root = tree.root_node
    # Recurse over the top-level children, tiling [0, len(data)] losslessly.
    spans = _chunk_nodes(
        list(root.children), data, 0, len(data), max_chars
    )
    if not spans:
        return [source]

    # Decode each byte span back to text. Concatenation is lossless because the
    # spans tile the whole buffer with no gaps/overlaps (see _chunk_nodes).
    chunks = [data[s:e].decode("utf-8", errors="replace") for (s, e) in spans]
    return chunks


def _chunk_nodes(
    nodes: list,
    data: bytes,
    region_start: int,
    region_end: int,
    max_chars: int,
) -> list[tuple[int, int]]:
    """Greedy split-then-merge over ``nodes`` within ``[region_start, region_end)``.

    Returns (start_byte, end_byte) spans that TILE the region EXACTLY — a strict
    forward cursor guarantees losslessness: ``cursor`` is the next unemitted
    byte, every emitted span is ``[chunk_start, cursor)``, and the cursor only
    moves forward and never repeats, so spans are contiguous, gap-free, and
    non-overlapping.

    Algorithm 1 semantics:
      * accumulate sibling nodes into the current chunk (chunk extends to the
        node's end_byte, absorbing inter-node whitespace/punctuation);
      * if adding a node would push the current chunk over ``max_chars``
        (non-whitespace size) → flush ``[chunk_start, node.start)`` and start a
        fresh chunk at the node;
      * if a SINGLE node alone exceeds ``max_chars`` → flush the pending chunk,
        recurse into the node's exact byte sub-region ``[node.start, node.end)``
        (tiled by its children), then continue after it. A childless oversized
        leaf is emitted whole (atomic span — can't split losslessly).
    """
    spans: list[tuple[int, int]] = []
    if region_start >= region_end:
        return spans

    def _size(a: int, b: int) -> int:
        if b <= a:
            return 0
        return nonws_len(data[a:b].decode("utf-8", errors="replace"))

    cursor = region_start       # next unemitted byte (only moves forward)
    chunk_start = region_start  # start of the current (open) chunk

    for node in nodes:
        n_start, n_end = node.start_byte, node.end_byte
        if n_end <= cursor:
            continue  # fully behind the cursor (already emitted) — skip

        node_alone = _size(max(n_start, cursor), n_end)

        if node_alone > max_chars and node.children:
            # Oversized node → flush the pending chunk up to the node start,
            # then recurse over the node's own sub-region.
            if n_start > chunk_start:
                spans.append((chunk_start, n_start))
            sub = _chunk_nodes(
                list(node.children), data, max(n_start, cursor), n_end, max_chars
            )
            spans.extend(sub)
            cursor = n_end
            chunk_start = n_end
            continue

        # Would extending the current chunk to this node's end overflow it?
        if cursor > chunk_start and _size(chunk_start, n_end) > max_chars:
            spans.append((chunk_start, n_start))
            chunk_start = n_start  # fresh chunk begins at this node

        cursor = max(cursor, n_end)

    # Trailing region (whitespace / text after the last node) closes the chunk.
    if region_end > chunk_start:
        spans.append((chunk_start, region_end))

    return spans


def _default_max_chars() -> int:
    raw = os.environ.get("OKURO_CORTEX_CAST_MAX_CHARS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 4000
