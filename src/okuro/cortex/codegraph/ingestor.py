# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Tree-sitter ingestor — walks a project and emits CodeFacts per file. Idempotent via sha256.
# index:
#   imports
#   LANGUAGE_MAP
#   AVAILABLE_LANGUAGES
#   class TreesitterIngestor
#   def ingest_file
#   def ingest_project
# AGENT_HEADER_END -->
"""Tree-sitter ingestor.

Designed to plug into the existing cortex Scanner: callers pass discovered
file paths; the ingestor parses each with the appropriate Tree-sitter
grammar and emits a CodeFacts record. Outputs are JSON-serialisable so the
KG indexer (sense/kg.py) can fan them into entities + triples.

Idempotency: each CodeFacts carries the source file sha256. Callers compare
against a stored hash and skip parsing when unchanged.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path
from typing import Iterable, Iterator

from ..exclusions import build_matcher
from .models import CallEdge, CodeFacts, ImportEdge, InheritEdge, Symbol


LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
}

AVAILABLE_LANGUAGES = tuple(sorted(set(LANGUAGE_MAP.values())))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TreesitterIngestor:
    """Lazy-init wrapper around tree-sitter-languages parsers."""

    def __init__(self) -> None:
        self._parsers: dict[str, object] = {}
        self._available: bool | None = None
        self._get_parser = None  # resolved provider callable

    def _ensure_available(self) -> bool:
        if self._available is not None:
            return self._available
        # Prefer the pinned tree-sitter-languages (0.21 API); fall back to
        # tree-sitter-language-pack (0.25 API) which ships wheels for newer
        # Pythons where tree-sitter-languages is unmaintained. Node traversal
        # is identical across both — only parser construction differs.
        try:
            from tree_sitter_languages import get_parser
            self._get_parser = get_parser
            self._available = True
        except ImportError:
            try:
                from tree_sitter_language_pack import get_parser
                self._get_parser = get_parser
                self._available = True
            except ImportError:
                self._available = False
        return self._available

    def _parser(self, language: str):
        if language not in self._parsers:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                self._parsers[language] = self._get_parser(language)
        return self._parsers[language]

    @staticmethod
    def language_for(path: Path) -> str | None:
        return LANGUAGE_MAP.get(path.suffix.lower())

    def parse(self, path: Path, content: bytes | None = None) -> CodeFacts:
        if content is None:
            content = path.read_bytes()
        language = self.language_for(path) or "unknown"
        facts = CodeFacts(file=str(path), language=language, sha256=_sha256(content))

        if not self._ensure_available():
            facts.error = "no tree-sitter grammar provider (install tree-sitter-languages or tree-sitter-language-pack)"
            return facts
        if language == "unknown":
            facts.error = f"unsupported extension: {path.suffix}"
            return facts

        try:
            parser = self._parser(language)
            tree = parser.parse(content)
        except Exception as exc:  # noqa: BLE001 — surface anything as a fact
            facts.error = f"parse error: {exc.__class__.__name__}: {exc}"
            return facts

        if language == "python":
            _extract_python(tree.root_node, content, facts)
        elif language in ("javascript", "typescript", "tsx"):
            _extract_js_like(tree.root_node, content, facts)
        elif language == "java":
            _extract_java(tree.root_node, content, facts)
        # Other languages: only sha256 + language are emitted until queries land.
        return facts


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _line(node) -> int:
    return node.start_point[0] + 1


def _end_line(node) -> int:
    return node.end_point[0] + 1


def _extract_python(root, src: bytes, facts: CodeFacts) -> None:
    """Walk a Python AST and append facts."""

    def visit(node, scope_class: str | None = None, scope_func: str | None = None) -> None:
        kind = node.type

        if kind == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    facts.imports.append(
                        ImportEdge(target=_text(child, src), line=_line(node))
                    )
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node:
                        facts.imports.append(
                            ImportEdge(
                                target=_text(name_node, src),
                                alias=_text(alias_node, src) if alias_node else None,
                                line=_line(node),
                            )
                        )

        elif kind == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            module = _text(module_node, src) if module_node else ""
            # Relative imports (from . import x) have leading dots
            is_relative = module.startswith(".") if module else any(
                c.type == "import_prefix" for c in node.children
            )
            for child in node.children:
                if child.type in ("dotted_name", "aliased_import") and child is not module_node:
                    if child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        name = _text(name_node, src) if name_node else ""
                        alias = _text(alias_node, src) if alias_node else None
                    else:
                        name = _text(child, src)
                        alias = None
                    target = f"{module}.{name}" if module else name
                    facts.imports.append(
                        ImportEdge(
                            target=target,
                            alias=alias,
                            line=_line(node),
                            is_relative=is_relative,
                        )
                    )

        elif kind == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(name_node, src)
                facts.symbols.append(
                    Symbol(
                        name=name,
                        kind="class",
                        line=_line(node),
                        end_line=_end_line(node),
                        parent=scope_class,
                    )
                )
                # superclass list
                superclasses = node.child_by_field_name("superclasses")
                if superclasses:
                    for sc in superclasses.children:
                        if sc.type in ("identifier", "attribute"):
                            facts.inherits.append(
                                InheritEdge(
                                    child=name,
                                    parent=_text(sc, src),
                                    line=_line(node),
                                )
                            )
                # recurse into class body
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        visit(child, scope_class=name, scope_func=scope_func)
                return  # body already walked

        elif kind == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                fname = _text(name_node, src)
                facts.symbols.append(
                    Symbol(
                        name=fname,
                        kind="method" if scope_class else "function",
                        line=_line(node),
                        end_line=_end_line(node),
                        parent=scope_class,
                    )
                )
                # Recurse into the body with this function as the enclosing scope
                body = node.child_by_field_name("body")
                if body:
                    new_scope = f"{scope_class}.{fname}" if scope_class else fname
                    for child in body.children:
                        visit(child, scope_class=scope_class, scope_func=new_scope)
                return

        elif kind == "call":
            func_node = node.child_by_field_name("function")
            if func_node:
                callee = _normalize_py_callee(_text(func_node, src), scope_class)
                facts.calls.append(
                    CallEdge(
                        callee=callee,
                        line=_line(node),
                        caller=scope_func,
                    )
                )

        for child in node.children:
            visit(child, scope_class=scope_class, scope_func=scope_func)

    visit(root)


def _normalize_py_callee(callee: str, scope_class: str | None) -> str:
    """Rewrite 'self.x' / 'cls.x' to '<scope_class>.x' when inside a method."""
    if not scope_class:
        return callee
    for prefix in ("self.", "cls."):
        if callee.startswith(prefix):
            return f"{scope_class}.{callee[len(prefix):]}"
    return callee


def _extract_js_like(root, src: bytes, facts: CodeFacts) -> None:
    """Walk a JS/TS AST and append facts.

    Covers ES module imports, function/class declarations, method
    definitions, calls, and `extends` clauses. Designed to be tolerant
    across javascript / typescript / tsx grammars (they share most node
    types).
    """

    def visit(node, scope_class: str | None = None, scope_func: str | None = None) -> None:
        kind = node.type

        if kind == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node:
                # source text includes the quotes; strip them
                target = _text(source_node, src).strip("\"'`")
                facts.imports.append(
                    ImportEdge(
                        target=target,
                        line=_line(node),
                        is_relative=target.startswith("."),
                    )
                )

        elif kind == "lexical_declaration" or kind == "variable_declaration":
            # const x = require('y')
            for child in node.children:
                if child.type == "variable_declarator":
                    value = child.child_by_field_name("value")
                    if value and value.type == "call_expression":
                        callee = value.child_by_field_name("function")
                        if callee and _text(callee, src) == "require":
                            args = value.child_by_field_name("arguments")
                            if args:
                                for a in args.children:
                                    if a.type == "string":
                                        target = _text(a, src).strip("\"'`")
                                        facts.imports.append(
                                            ImportEdge(
                                                target=target,
                                                line=_line(node),
                                                is_relative=target.startswith("."),
                                            )
                                        )

        elif kind in ("class_declaration", "class"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(name_node, src)
                facts.symbols.append(
                    Symbol(
                        name=name,
                        kind="class",
                        line=_line(node),
                        end_line=_end_line(node),
                        parent=scope_class,
                    )
                )
                heritage = node.child_by_field_name("heritage") or _find_child(
                    node, "class_heritage"
                )
                if heritage:
                    for parent_text in _iter_heritage_parents(heritage, src):
                        facts.inherits.append(
                            InheritEdge(
                                child=name,
                                parent=parent_text,
                                line=_line(node),
                            )
                        )
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        visit(child, scope_class=name, scope_func=scope_func)
                return

        elif kind in ("function_declaration", "function"):
            name_node = node.child_by_field_name("name")
            if name_node:
                fname = _text(name_node, src)
                facts.symbols.append(
                    Symbol(
                        name=fname,
                        kind="function",
                        line=_line(node),
                        end_line=_end_line(node),
                        parent=scope_class,
                    )
                )
                body = node.child_by_field_name("body")
                if body:
                    new_scope = f"{scope_class}.{fname}" if scope_class else fname
                    for child in body.children:
                        visit(child, scope_class=scope_class, scope_func=new_scope)
                return

        elif kind == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node and scope_class:
                mname = _text(name_node, src)
                facts.symbols.append(
                    Symbol(
                        name=mname,
                        kind="method",
                        line=_line(node),
                        end_line=_end_line(node),
                        parent=scope_class,
                    )
                )
                body = node.child_by_field_name("body")
                if body:
                    new_scope = f"{scope_class}.{mname}"
                    for child in body.children:
                        visit(child, scope_class=scope_class, scope_func=new_scope)
                return

        elif kind == "call_expression":
            func_node = node.child_by_field_name("function")
            if func_node:
                callee = _normalize_js_callee(_text(func_node, src), scope_class)
                facts.calls.append(
                    CallEdge(
                        callee=callee,
                        line=_line(node),
                        caller=scope_func,
                    )
                )

        for child in node.children:
            visit(child, scope_class=scope_class, scope_func=scope_func)

    visit(root)


def _normalize_js_callee(callee: str, scope_class: str | None) -> str:
    """Rewrite 'this.x' to '<scope_class>.x' when inside a method."""
    if scope_class and callee.startswith("this."):
        return f"{scope_class}.{callee[len('this.'):]}"
    return callee


def _find_child(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _iter_heritage_parents(node, src: bytes):
    """Yield parent-class text from a JS/TS class_heritage subtree.

    Handles three shapes:
      - JS: class_heritage > identifier (parent directly under heritage)
      - TS: class_heritage > extends_clause > identifier
      - TS: class_heritage > implements_clause > type_identifier
    """
    PARENT_TYPES = {
        "identifier",
        "type_identifier",
        "member_expression",
        "nested_identifier",
        "generic_type",
    }
    WRAPPER_TYPES = {"extends_clause", "implements_clause", "extends_type_clause"}

    def visit(n):
        if n.type in PARENT_TYPES:
            yield _text(n, src)
            return
        if n.type in WRAPPER_TYPES or n.type == "class_heritage":
            for c in n.children:
                yield from visit(c)

    yield from visit(node)


_JAVA_TYPE_DECLS = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
)


def _java_type_names(node, src: bytes) -> list[str]:
    """Collect referenced type names under a supertype clause.

    Handles ``type_identifier`` (``Base``), ``scoped_type_identifier``
    (``com.foo.Base``) and ``generic_type`` (``List<String>`` → ``List``).
    """
    out: list[str] = []

    def rec(n) -> None:
        if n.type in ("type_identifier", "scoped_type_identifier"):
            out.append(_text(n, src))
            return
        if n.type == "generic_type":
            for c in n.children:
                if c.type in ("type_identifier", "scoped_type_identifier"):
                    out.append(_text(c, src))
                    return
            return
        for c in n.children:
            rec(c)

    rec(node)
    return out


def _java_supertypes(node, src: bytes) -> list[str]:
    """extends + implements type names for a class/interface declaration."""
    names: list[str] = []
    for c in node.children:
        # class extends X → `superclass`; implements → `super_interfaces`;
        # interface extends → `extends_interfaces` (a plain child, not a field).
        if c.type in ("superclass", "super_interfaces", "extends_interfaces"):
            names.extend(_java_type_names(c, src))
    return names


def _extract_java(root, src: bytes, facts: CodeFacts) -> None:
    """Walk a Java AST and append facts.

    Covers imports (incl. static + wildcard), class/interface/enum/record
    declarations, methods + constructors, extends/implements, method
    invocations, and ``new Type(...)`` instantiations (a real cross-file
    dependency signal Java has few alternatives for). ``this.x()`` is rewritten
    to ``<class>.x`` so in-file calls resolve, matching the python/js walkers.
    """

    def visit(node, scope_class: str | None = None, scope_func: str | None = None) -> None:
        kind = node.type

        if kind == "import_declaration":
            # scoped_identifier holds the dotted path; a trailing `.*` shows as
            # an `asterisk` child (kept out of the target).
            for child in node.children:
                if child.type in ("scoped_identifier", "identifier"):
                    facts.imports.append(
                        ImportEdge(target=_text(child, src), line=_line(node))
                    )
                    break
            return

        if kind in _JAVA_TYPE_DECLS:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(name_node, src)
                facts.symbols.append(
                    Symbol(
                        name=name,
                        kind="class",
                        line=_line(node),
                        end_line=_end_line(node),
                        parent=scope_class,
                    )
                )
                for parent_text in _java_supertypes(node, src):
                    facts.inherits.append(
                        InheritEdge(child=name, parent=parent_text, line=_line(node))
                    )
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        visit(child, scope_class=name, scope_func=scope_func)
                return

        if kind in ("method_declaration", "constructor_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                mname = _text(name_node, src)
                facts.symbols.append(
                    Symbol(
                        name=mname,
                        kind="method" if scope_class else "function",
                        line=_line(node),
                        end_line=_end_line(node),
                        parent=scope_class,
                    )
                )
                body = node.child_by_field_name("body")
                if body:
                    new_scope = f"{scope_class}.{mname}" if scope_class else mname
                    for child in body.children:
                        visit(child, scope_class=scope_class, scope_func=new_scope)
                return

        if kind == "method_invocation":
            name_node = node.child_by_field_name("name")
            obj_node = node.child_by_field_name("object")
            if name_node:
                mname = _text(name_node, src)
                if obj_node is not None and obj_node.type == "this":
                    callee = f"{scope_class}.{mname}" if scope_class else mname
                elif obj_node is not None and obj_node.type == "identifier":
                    callee = f"{_text(obj_node, src)}.{mname}"
                else:
                    # complex or absent receiver → bare method name (resolution
                    # keys on the final segment anyway).
                    callee = mname
                facts.calls.append(
                    CallEdge(callee=callee, line=_line(node), caller=scope_func)
                )
            # fall through: recurse into arguments / receiver for nested calls.

        elif kind == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node:
                facts.calls.append(
                    CallEdge(
                        callee=_text(type_node, src),
                        line=_line(node),
                        caller=scope_func,
                    )
                )
            # fall through to recurse into constructor arguments.

        for child in node.children:
            visit(child, scope_class=scope_class, scope_func=scope_func)

    visit(root)


def ingest_file(path: Path, ingestor: TreesitterIngestor | None = None) -> CodeFacts:
    """Parse a single file. Convenience wrapper."""
    ingestor = ingestor or TreesitterIngestor()
    return ingestor.parse(path)


def discover_project_files(root: Path, include_hidden: bool = False) -> set[str]:
    """Fast filesystem walk — return supported-language relpaths only.

    No parsing, no Tree-sitter. Used as pass-1 of streaming ingest so
    pass-2 can iterate facts without materializing them in memory.
    """
    return {str(p.relative_to(root)) for p in _discover(root, include_hidden)}


def _discover(root: Path, include_hidden: bool = False) -> Iterator[Path]:
    """Walk root using the cortex exclusion matcher so we never recurse into
    node_modules / .git / .venv / etc."""
    import os

    matcher = build_matcher(root, use_gitignore=True)
    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=lambda _e: None
    ):
        dirnames[:] = [d for d in dirnames if not matcher.is_pruned_dir(d)]
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            path = Path(dirpath) / fname
            if TreesitterIngestor.language_for(path) is None:
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if matcher.is_excluded(rel):
                continue
            yield path


def ingest_project(
    root: Path,
    *,
    hashes: dict[str, str] | None = None,
    paths: Iterable[Path] | None = None,
) -> Iterator[CodeFacts]:
    """Parse every supported file under root.

    Args:
        root: project root.
        hashes: optional map of relative-path -> last-known sha256. Files
            whose current sha256 matches are skipped (idempotency).
        paths: optional explicit file list. When given, discovery is
            bypassed — useful for incremental updates driven by a Scanner.

    Yields CodeFacts in discovery order.
    """
    ingestor = TreesitterIngestor()
    iterable = paths if paths is not None else _discover(root)
    for path in iterable:
        try:
            content = path.read_bytes()
        except (OSError, PermissionError):
            continue
        sha = _sha256(content)
        if hashes is not None:
            rel = str(path.relative_to(root)) if path.is_absolute() else str(path)
            if hashes.get(rel) == sha:
                continue
        facts = ingestor.parse(path, content=content)
        yield facts


def facts_to_jsonl(facts_iter: Iterable[CodeFacts], out_path: Path) -> int:
    """Write facts as JSON-lines. Returns count written."""
    count = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for f in facts_iter:
            fh.write(json.dumps(f.to_dict()) + "\n")
            count += 1
    return count
