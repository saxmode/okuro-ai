# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Dataclasses for Tree-sitter code-graph facts.
# index:
#   class Symbol
#   class ImportEdge
#   class CallEdge
#   class InheritEdge
#   class CodeFacts
# AGENT_HEADER_END -->
"""Stable JSON-serialisable fact records emitted by the ingestor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


SymbolKind = Literal["function", "method", "class", "variable"]


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: SymbolKind
    line: int
    end_line: int
    parent: str | None = None


@dataclass(frozen=True)
class ImportEdge:
    target: str
    line: int
    alias: str | None = None
    is_relative: bool = False


@dataclass(frozen=True)
class CallEdge:
    callee: str
    line: int
    caller: str | None = None


@dataclass(frozen=True)
class InheritEdge:
    child: str
    parent: str
    line: int


@dataclass
class CodeFacts:
    file: str
    language: str
    sha256: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportEdge] = field(default_factory=list)
    calls: list[CallEdge] = field(default_factory=list)
    inherits: list[InheritEdge] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
