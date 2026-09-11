# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Structure Parsers for AGENT_HEADER
# index:
#   imports
#   class ParseResult
#   class BaseParser
#   class PythonParser
#   class JavaScriptParser
#   class MarkdownParser
#   class ShellParser
#   class ConfigParser
#   class GenericParser
#   class RoleParser
#   def get_parser
#   def parse_file
# AGENT_HEADER_END -->
"""
Structure Parsers for AGENT_HEADER
Extract indexable structures from various file types.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .core import IndexEntry, FileRole


@dataclass
class ParseResult:
    """Result of parsing a file for structure."""

    role: FileRole
    entries: list[IndexEntry]
    purpose: Optional[str] = None
    role_id: Optional[str] = None
    domain: Optional[str] = None
    tier: Optional[str] = None
    model: Optional[str] = None


class BaseParser(ABC):
    priority: int = 0

    @abstractmethod
    def can_parse(self, file_path: Path) -> bool:
        pass

    @abstractmethod
    def parse(self, file_path: Path, content: str) -> ParseResult:
        pass


class PythonParser(BaseParser):
    EXTENSIONS = {".py", ".pyw"}
    priority = 50

    def can_parse(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.EXTENSIONS

    def parse(self, file_path: Path, content: str) -> ParseResult:
        entries = []
        lines = content.split("\n")
        import_start = None
        import_end = None
        current_class = None
        class_start = None

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            if stripped.startswith(("import ", "from ")):
                if import_start is None:
                    import_start = i
                import_end = i
            elif import_start and stripped and not stripped.startswith("#"):
                if import_end and import_end - import_start >= 2:
                    entries.append(IndexEntry(import_start, import_end, "imports"))
                import_start = None
                import_end = None

            class_match = re.match(r"^class\s+(\w+)", line)
            if class_match:
                if current_class and class_start:
                    entries.append(
                        IndexEntry(class_start, i - 1, f"class {current_class}")
                    )
                current_class = class_match.group(1)
                class_start = i

            func_match = re.match(r"^def\s+(\w+)", line)
            if func_match:
                if current_class and class_start:
                    entries.append(
                        IndexEntry(class_start, i - 1, f"class {current_class}")
                    )
                    current_class = None
                    class_start = None

                func_name = func_match.group(1)
                func_start = i
                func_end = i
                for j in range(i + 1, len(lines) + 1):
                    if j <= len(lines):
                        next_line = lines[j - 1]
                        if re.match(r"^(def |class )", next_line):
                            break
                        if next_line.strip():
                            func_end = j
                    else:
                        func_end = len(lines)
                entries.append(IndexEntry(func_start, func_end, f"def {func_name}"))

        if current_class and class_start:
            entries.append(
                IndexEntry(class_start, len(lines), f"class {current_class}")
            )
        if import_start and import_end:
            entries.append(IndexEntry(import_start, import_end, "imports"))

        entries.sort(key=lambda e: e.start_line)

        # Remove overlapping imports inside functions/classes
        filtered = []
        for entry in entries:
            overlaps = False
            for other in entries:
                if other is entry:
                    continue
                if entry.description == "imports" and (
                    "def " in other.description or "class " in other.description
                ):
                    if (
                        other.start_line < entry.start_line
                        and entry.end_line <= other.end_line
                    ):
                        overlaps = True
                        break
            if not overlaps:
                filtered.append(entry)

        role = (
            FileRole.TEST if "test" in file_path.stem.lower() else FileRole.CODE
        )

        # Extract module docstring as purpose
        purpose = None
        doc_match = re.match(r'^(?:#[^\n]*\n)*\s*(?:\'\'\'|""")(.*?)(?:\'\'\'|""")', content, re.DOTALL)
        if doc_match:
            first_line = doc_match.group(1).strip().split("\n")[0].strip()
            if first_line and len(first_line) > 5:
                purpose = first_line[:100]

        return ParseResult(role=role, entries=filtered, purpose=purpose)


class JavaScriptParser(BaseParser):
    EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
    priority = 50

    def can_parse(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.EXTENSIONS

    def parse(self, file_path: Path, content: str) -> ParseResult:
        entries = []
        lines = content.split("\n")
        import_start = None
        import_end = None

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            if (
                stripped.startswith(("import ", "const "))
                and "require(" in stripped
            ) or stripped.startswith("import "):
                if import_start is None:
                    import_start = i
                import_end = i
            elif import_start and stripped and not stripped.startswith("//"):
                if import_end and import_end - import_start >= 1:
                    entries.append(IndexEntry(import_start, import_end, "imports"))
                import_start = None

            func_match = re.match(
                r"^export\s+(?:async\s+)?function\s+(\w+)", line
            )
            if func_match:
                entries.append(
                    IndexEntry(i, i, f"export function {func_match.group(1)}")
                )

            class_match = re.match(r"^export\s+class\s+(\w+)", line)
            if class_match:
                entries.append(
                    IndexEntry(i, i, f"export class {class_match.group(1)}")
                )

            const_match = re.match(r"^export\s+const\s+(\w+)", line)
            if const_match:
                entries.append(
                    IndexEntry(i, i, f"export const {const_match.group(1)}")
                )

            plain_func = re.match(r"^(?:async\s+)?function\s+(\w+)", line)
            if plain_func and not stripped.startswith("export"):
                entries.append(
                    IndexEntry(i, i, f"function {plain_func.group(1)}")
                )

            plain_class = re.match(r"^class\s+(\w+)", line)
            if plain_class and not stripped.startswith("export"):
                entries.append(
                    IndexEntry(i, i, f"class {plain_class.group(1)}")
                )

        if import_start and import_end:
            entries.append(IndexEntry(import_start, import_end, "imports"))

        entries.sort(key=lambda e: e.start_line)
        role = (
            FileRole.TEST
            if "test" in file_path.stem.lower()
            or "spec" in file_path.stem.lower()
            else FileRole.CODE
        )
        return ParseResult(role=role, entries=entries)


class MarkdownParser(BaseParser):
    EXTENSIONS = {".md", ".markdown", ".mdx"}
    priority = 10
    NOTE_DIRS = {"tm-notes", "notes", "obsidian"}

    def can_parse(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.EXTENSIONS

    def _is_note(self, file_path: Path, content: str) -> bool:
        path_parts = {p.lower() for p in file_path.parts}
        if path_parts & self.NOTE_DIRS:
            return True
        if content.startswith("---"):
            end_match = re.search(r"\n---\s*\n", content[3:])
            if end_match:
                return True
        return False

    def parse(self, file_path: Path, content: str) -> ParseResult:
        entries = []
        lines = content.split("\n")
        role = FileRole.NOTE if self._is_note(file_path, content) else FileRole.DOC

        current_heading = None
        current_start = None

        for i, line in enumerate(lines, 1):
            heading_match = re.match(r"^(#{1,6})\s+(.+)", line)
            if heading_match:
                if current_heading and current_start:
                    entries.append(
                        IndexEntry(current_start, i - 1, current_heading)
                    )
                current_heading = heading_match.group(2).strip()[:50]
                current_start = i

        if current_heading and current_start:
            entries.append(IndexEntry(current_start, len(lines), current_heading))

        if not entries and lines:
            entries.append(IndexEntry(1, len(lines), "content"))

        return ParseResult(role=role, entries=entries)


class ShellParser(BaseParser):
    EXTENSIONS = {".sh", ".bash", ".zsh"}
    priority = 50

    def can_parse(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.EXTENSIONS

    def parse(self, file_path: Path, content: str) -> ParseResult:
        entries = []
        lines = content.split("\n")

        for i, line in enumerate(lines, 1):
            func_match = re.match(r"^(\w+)\s*\(\s*\)", line)
            if func_match:
                entries.append(
                    IndexEntry(i, i, f"function {func_match.group(1)}")
                )
            func_match2 = re.match(r"^function\s+(\w+)", line)
            if func_match2:
                entries.append(
                    IndexEntry(i, i, f"function {func_match2.group(1)}")
                )

        if not entries and lines:
            entries.append(IndexEntry(1, len(lines), "script"))

        return ParseResult(role=FileRole.CODE, entries=entries)


class ConfigParser(BaseParser):
    EXTENSIONS = {".yaml", ".yml", ".toml", ".ini", ".cfg"}
    priority = 50

    def can_parse(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.EXTENSIONS

    def parse(self, file_path: Path, content: str) -> ParseResult:
        entries = []
        lines = content.split("\n")
        current_key = None
        current_start = None

        for i, line in enumerate(lines, 1):
            key_match = re.match(r"^(\w[\w\-]*)\s*[:\=]", line)
            if key_match:
                if current_key and current_start:
                    entries.append(IndexEntry(current_start, i - 1, current_key))
                current_key = key_match.group(1)
                current_start = i

            section_match = re.match(r"^\[(\w[\w\.\-]*)\]", line)
            if section_match:
                if current_key and current_start:
                    entries.append(IndexEntry(current_start, i - 1, current_key))
                current_key = section_match.group(1)
                current_start = i

        if current_key and current_start:
            entries.append(IndexEntry(current_start, len(lines), current_key))

        return ParseResult(role=FileRole.CONFIG, entries=entries)


class GenericParser(BaseParser):
    priority = 0

    def can_parse(self, file_path: Path) -> bool:
        return True

    def parse(self, file_path: Path, content: str) -> ParseResult:
        lines = content.split("\n")
        if lines:
            return ParseResult(
                role=FileRole.CODE,
                entries=[IndexEntry(1, len(lines), "content")],
            )
        return ParseResult(role=FileRole.CODE, entries=[])


class RoleParser(BaseParser):
    priority = 100

    def can_parse(self, file_path: Path) -> bool:
        return (
            file_path.name == "role.md"
            and "roles/" in str(file_path).replace("\\", "/")
        )

    def parse(self, file_path: Path, content: str) -> ParseResult:
        entries = []
        lines = content.split("\n")
        purpose = None
        role_id = None
        domain = None
        tier = None
        model = None

        for line in lines[:100]:
            m = re.match(r"\*\*Purpose:\*\*\s+(.+)", line)
            if m and not purpose:
                purpose = m.group(1).strip()
            m = re.match(r"\*\*ID:\*\*\s+(.+)", line)
            if m and not role_id:
                role_id = m.group(1).strip()
            m = re.match(r"\*\*Domain:\*\*\s+(.+)", line)
            if m and not domain:
                domain = m.group(1).strip()
            m = re.match(r"\*\*Tier:\*\*\s+(.+)", line)
            if m and not tier:
                tier = m.group(1).strip()
            m = re.match(r"\*\*Model:\*\*\s+(.+)", line)
            if m and not model:
                model = m.group(1).strip()

        current_section = None
        current_start = None
        for i, line in enumerate(lines, 1):
            heading_match = re.match(r"^##\s+(.+)", line)
            if heading_match:
                if current_section and current_start:
                    entries.append(
                        IndexEntry(current_start, i - 1, current_section)
                    )
                current_section = heading_match.group(1).strip()[:50]
                current_start = i

        if current_section and current_start:
            entries.append(
                IndexEntry(current_start, len(lines), current_section)
            )

        if not entries:
            entries = [
                IndexEntry(1, 50, "IDENTITY"),
                IndexEntry(51, 100, "PROTOCOL"),
                IndexEntry(101, 150, "TOOLS"),
            ]

        return ParseResult(
            role=FileRole.ROLE,
            entries=entries,
            purpose=purpose,
            role_id=role_id,
            domain=domain,
            tier=tier,
            model=model,
        )


_PARSER_INSTANCES = [
    RoleParser(),
    PythonParser(),
    JavaScriptParser(),
    MarkdownParser(),
    ShellParser(),
    ConfigParser(),
    GenericParser(),
]

PARSERS = sorted(_PARSER_INSTANCES, key=lambda p: p.priority, reverse=True)


def get_parser(file_path: Path) -> BaseParser:
    for parser in PARSERS:
        if parser.can_parse(file_path):
            return parser
    return GenericParser()


def parse_file(file_path: Path, content: str = None) -> ParseResult:
    if content is None:
        content = file_path.read_text()
    parser = get_parser(file_path)
    return parser.parse(file_path, content)
