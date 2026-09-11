# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: AGENT_HEADER v3 Core Module
# index:
#   imports
#   class FileRole
#   class IndexEntry
#   def resolve_index_lines
#   class AgentHeader
#   def get_wrapper
#   def parse_header
#   def generate_header
#   class ValidationResult
#   def validate_header
#   def extract_header_bounds
# AGENT_HEADER_END -->
"""
AGENT_HEADER v3 Core Module
Parser, generator, and validator for the universal header format.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class FileRole(Enum):
    """Valid role values for AGENT_HEADER."""

    CODE = "code"
    DOC = "doc"
    CONFIG = "config"
    DATA = "data"
    TEMPLATE = "template"
    TEST = "test"
    ROLE = "role"
    NOTE = "note"


@dataclass
class IndexEntry:
    """Single entry in the header index."""

    start_line: int
    end_line: int
    description: str

    def to_string(self) -> str:
        """Format as description only (line numbers stored in vector DB)."""
        return self.description

    @classmethod
    def from_string(cls, s: str) -> Optional["IndexEntry"]:
        """Parse from header text."""
        s = s.strip()
        if not s:
            return None
        # Old format: L0010-L0025 description
        pattern = r"L(\d+)-L(\d+)\s+(.+)"
        match = re.match(pattern, s)
        if match:
            return cls(
                start_line=int(match.group(1)),
                end_line=int(match.group(2)),
                description=match.group(3).strip(),
            )
        # New format: description only
        return cls(start_line=0, end_line=0, description=s)


_DEF_RE = re.compile(r"^(?P<indent>[ \t]*)(?:async[ \t]+)?(?P<kind>def|class)[ \t]+(?P<name>[A-Za-z_]\w*)")
_SYMBOL_IN_DESC = re.compile(r"^(?:async[ \t]+)?(?:def|class)[ \t]+(?P<name>[A-Za-z_]\w*)")


def resolve_index_lines(index: list["IndexEntry"], content: str) -> list["IndexEntry"]:
    """Fill in start/end lines for header index entries by locating them in ``content``.

    The v3 header format stores index entries as bare descriptions ("def foo",
    "imports"), so :meth:`IndexEntry.from_string` has nothing to parse and emits
    0/0. Nothing else stores them either — ``cortex_docs`` has no line columns —
    so the documented ``cortex_read_header`` -> ``cortex_read_section`` chain
    could never work: ``cortex_read_section`` requires ``start_line >= 1``.

    Resolution is best-effort and positional: entries naming a ``def``/``class``
    are located in the source, each resolved entry runs until the next one
    starts, and prose entries ("row builders") that name no symbol keep 0/0
    rather than guessing.
    """
    if not index:
        return index

    lines = content.split("\n")
    total = len(lines)

    # First top-level definition per name — nested helpers must not shadow the
    # module-level symbol the header is pointing at.
    symbol_line: dict[str, int] = {}
    first_def_line: Optional[int] = None
    for i, line in enumerate(lines, start=1):
        m = _DEF_RE.match(line)
        if not m:
            continue
        if first_def_line is None:
            first_def_line = i
        if m.group("indent"):
            continue
        symbol_line.setdefault(m.group("name"), i)

    def _locate(description: str) -> Optional[int]:
        d = description.strip()
        m = _SYMBOL_IN_DESC.match(d)
        if m:
            return symbol_line.get(m.group("name"))
        if d.lower() == "imports":
            # The import block runs from the top of the file to the first
            # definition; 1 is correct even when a licence header precedes it.
            # Checked before the identifier lookup — "imports" is itself a
            # valid identifier and would otherwise miss and return None.
            return 1 if first_def_line else None
        if d.isidentifier():
            return symbol_line.get(d)
        return None

    starts = [_locate(e.description) for e in index]

    resolved = []
    for pos, (entry, start) in enumerate(zip(index, starts)):
        if not start:
            resolved.append(IndexEntry(0, 0, entry.description))
            continue
        # Next boundary in FILE order, not index order — a header whose index
        # is listed out of order must still yield non-overlapping ranges.
        later = [s for s in starts if s and s > start]
        nxt = min(later) if later else None
        end = (nxt - 1) if nxt else total
        resolved.append(IndexEntry(start, max(start, end), entry.description))
    return resolved


@dataclass
class AgentHeader:
    """Parsed AGENT_HEADER structure."""

    role: FileRole
    purpose: str
    index: list[IndexEntry] = field(default_factory=list)

    # Role-specific fields
    id: Optional[str] = None
    domain: Optional[str] = None
    tier: Optional[str] = None
    model: Optional[str] = None

    # Navigation fields (v3)
    loc: Optional[str] = None
    up: Optional[str] = None

    def is_empty_index(self) -> bool:
        return len(self.index) == 0

    def is_role_file(self) -> bool:
        return self.role == FileRole.ROLE and self.id is not None

    def to_string(
        self, multiline_threshold: int = 5, include_wrapper: bool = True
    ) -> str:
        lines = []
        if include_wrapper:
            lines.append("<!-- AGENT_HEADER")

        if self.is_role_file():
            lines.append(f"id: {self.id}")
            if self.domain:
                lines.append(f"domain: {self.domain}")
            if self.tier:
                lines.append(f"tier: {self.tier}")
            if self.model:
                lines.append(f"model: {self.model}")
        else:
            lines.append(f"role: {self.role.value}")

        lines.append(f"purpose: {self.purpose}")

        if self.loc:
            lines.append(f"loc: {self.loc}")
        if self.up:
            lines.append(f"up: {self.up}")

        if self.is_empty_index():
            lines.append("index: none")
        elif len(self.index) <= multiline_threshold:
            index_str = " | ".join(e.to_string() for e in self.index)
            lines.append(f"index: {index_str}")
        else:
            lines.append("index:")
            for entry in self.index:
                lines.append(f"  {entry.to_string()}")

        if include_wrapper:
            lines.append("AGENT_HEADER_END -->")
        return "\n".join(lines)


# Header wrapper patterns by file extension
HEADER_WRAPPERS = {
    ".md": ("<!-- AGENT_HEADER\n", "AGENT_HEADER_END -->"),
    ".html": ("<!-- AGENT_HEADER\n", "AGENT_HEADER_END -->"),
    ".xml": ("<!-- AGENT_HEADER\n", "AGENT_HEADER_END -->"),
    ".vue": ("<!-- AGENT_HEADER\n", "AGENT_HEADER_END -->"),
    ".svelte": ("<!-- AGENT_HEADER\n", "AGENT_HEADER_END -->"),
    ".js": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".ts": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".jsx": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".tsx": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".mjs": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".cjs": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".py": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".sh": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".bash": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".zsh": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".yaml": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".yml": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".toml": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".css": ("/* AGENT_HEADER\n", "AGENT_HEADER_END */"),
    ".scss": ("/* AGENT_HEADER\n", "AGENT_HEADER_END */"),
    ".less": ("/* AGENT_HEADER\n", "AGENT_HEADER_END */"),
    ".sql": ("-- <!-- AGENT_HEADER\n", "-- AGENT_HEADER_END -->"),
    ".go": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".rs": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".java": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".c": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".cpp": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    ".h": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
}

FILENAME_WRAPPERS = {
    "dockerfile": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    "caddyfile": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    "makefile": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    "gnumakefile": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    "procfile": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".gitignore": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".dockerignore": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    ".editorconfig": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    "jenkinsfile": ("// <!-- AGENT_HEADER\n", "// AGENT_HEADER_END -->"),
    "vagrantfile": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    "rakefile": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
    "gemfile": ("# <!-- AGENT_HEADER\n", "# AGENT_HEADER_END -->"),
}

def get_wrapper(file_path: Path) -> tuple[str, str]:
    ext = file_path.suffix.lower()
    if ext in HEADER_WRAPPERS:
        return HEADER_WRAPPERS[ext]
    filename = file_path.name.lower()
    if filename in FILENAME_WRAPPERS:
        return FILENAME_WRAPPERS[filename]
    return ("<!-- AGENT_HEADER\n", "AGENT_HEADER_END -->")


def parse_header(
    content: str, file_path: Optional[Path] = None
) -> Optional[AgentHeader]:
    """Parse AGENT_HEADER from file content."""
    pattern = r"(?:<!--|#\s*<!--|/\*|//\s*<!--)\s*AGENT_HEADER\s*\n(.*?)(?:(?:#|//|--)\s*)?AGENT_HEADER_END\s*(?:-->|\*/)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return None

    header_content = match.group(1)

    lines = []
    for line in header_content.split("\n"):
        line = re.sub(r"^#\s*", "", line)
        line = re.sub(r"^//\s*", "", line)
        line = re.sub(r"^\s*\*\s*", "", line)
        lines.append(line)

    header_text = "\n".join(lines)

    id_match = re.search(r"id:\s*(\S+)", header_text)
    domain_match = re.search(r"domain:\s*(\S+)", header_text)
    tier_match = re.search(r"tier:\s*(\S+)", header_text)
    model_match = re.search(r"model:\s*(\S+)", header_text)
    role_match = re.search(r"role:\s*(\w+)", header_text)
    purpose_match = re.search(r"purpose:\s*(.+?)(?:\n|$)", header_text)
    loc_match = re.search(r"loc:\s*(\S+)", header_text)
    up_match = re.search(r"up:\s*(\S+)", header_text)

    is_role_by_field = id_match is not None
    is_role_by_filename = (
        file_path
        and file_path.name == "role.md"
        and "roles/" in str(file_path).replace("\\", "/")
    )

    if is_role_by_field or is_role_by_filename:
        role = FileRole.ROLE
        role_id = id_match.group(1).strip() if id_match else None
        domain = domain_match.group(1).strip() if domain_match else None
        tier = tier_match.group(1).strip() if tier_match else None
        model = model_match.group(1).strip() if model_match else None

        if purpose_match:
            purpose = purpose_match.group(1).strip()
        else:
            header_end_pos = match.end()
            search_window = content[header_end_pos : header_end_pos + 2000]
            purpose_line_match = re.search(
                r"\*\*Purpose:\*\*\s*(.+?)(?:\n|$)", search_window
            )
            if purpose_line_match:
                purpose = purpose_line_match.group(1).strip()
            elif role_id:
                purpose = f"role: {role_id}"
            elif file_path:
                purpose = f"role: {file_path.stem}"
            else:
                purpose = "role"
    else:
        if not role_match or not purpose_match:
            return None
        try:
            role = FileRole(role_match.group(1).lower())
        except ValueError:
            return None
        purpose = purpose_match.group(1).strip()
        role_id = None
        domain = None
        tier = None
        model = None

    loc = loc_match.group(1).strip() if loc_match else None
    up = up_match.group(1).strip() if up_match else None

    # Parse index
    index = []
    if re.search(r"index:\s*none", header_text, re.IGNORECASE):
        pass
    else:
        single_match = re.search(r"index:\s*([^\n]+)", header_text)
        if single_match and "|" in single_match.group(1):
            parts = single_match.group(1).split("|")
            for part in parts:
                entry = IndexEntry.from_string(part.strip())
                if entry:
                    index.append(entry)
        else:
            in_index = False
            for line in header_text.split("\n"):
                if "index:" in line.lower():
                    in_index = True
                    after_colon = line.split(":", 1)[-1].strip()
                    if after_colon and after_colon.lower() != "none":
                        entry = IndexEntry.from_string(after_colon)
                        if entry:
                            index.append(entry)
                elif in_index:
                    line = line.strip()
                    if line and not any(
                        line.startswith(f"{k}:")
                        for k in (
                            "role",
                            "purpose",
                            "loc",
                            "up",
                            "id",
                            "domain",
                            "tier",
                            "model",
                        )
                    ):
                        entry = IndexEntry.from_string(line)
                        if entry:
                            index.append(entry)
                    elif line and ":" in line and not line.startswith("L"):
                        in_index = False

    return AgentHeader(
        role=role,
        purpose=purpose,
        index=index,
        id=role_id,
        domain=domain,
        tier=tier,
        model=model,
        loc=loc,
        up=up,
    )


def generate_header(
    role: FileRole,
    purpose: str,
    index: list[IndexEntry],
    file_path: Optional[Path] = None,
    multiline_threshold: int = 5,
    loc: Optional[str] = None,
    up: Optional[str] = None,
) -> str:
    """Generate AGENT_HEADER string with appropriate wrapper."""
    header = AgentHeader(role=role, purpose=purpose, index=index, loc=loc, up=up)

    if file_path:
        start, end = get_wrapper(file_path)
        ext = file_path.suffix.lower()
        content_lines = header.to_string(
            multiline_threshold, include_wrapper=False
        ).split("\n")

        if ext in {".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml"}:
            prefixed = [start.rstrip()]
            for line in content_lines:
                prefixed.append(f"# {line}")
            prefixed.append(end)
            return "\n".join(prefixed)

        elif ext in {
            ".go", ".rs", ".java", ".c", ".cpp", ".h",
            ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
        }:
            prefixed = [start.rstrip()]
            for line in content_lines:
                prefixed.append(f"// {line}")
            prefixed.append(end)
            return "\n".join(prefixed)

        elif ext == ".sql":
            prefixed = [start.rstrip()]
            for line in content_lines:
                prefixed.append(f"-- {line}")
            prefixed.append(end)
            return "\n".join(prefixed)

        elif ext in {".css", ".scss", ".less"}:
            prefixed = [start.rstrip()]
            for line in content_lines:
                prefixed.append(line)
            prefixed.append(end)
            return "\n".join(prefixed)

        filename = file_path.name.lower()
        if filename in FILENAME_WRAPPERS:
            fw_start, fw_end = FILENAME_WRAPPERS[filename]
            prefix = fw_start.split("<")[0].strip()
            if prefix:
                prefixed = [fw_start.rstrip()]
                for line in content_lines:
                    prefixed.append(f"{prefix} {line}")
                prefixed.append(fw_end)
                return "\n".join(prefixed)

        return header.to_string(multiline_threshold)
    return header.to_string(multiline_threshold)


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_header(
    header: AgentHeader, file_path: Optional[Path] = None
) -> ValidationResult:
    """Validate AGENT_HEADER."""
    result = ValidationResult(valid=True)

    if len(header.purpose) > 100:
        result.warnings.append(
            f"Purpose exceeds 100 chars ({len(header.purpose)})"
        )
    if not header.purpose:
        result.errors.append("Purpose is empty")
        result.valid = False

    if header.loc and not header.loc.strip():
        result.errors.append("loc field is empty")
        result.valid = False
    if header.up and not header.up.strip():
        result.errors.append("up field is empty")
        result.valid = False

    if header.is_role_file():
        if not header.id or not header.id.strip():
            result.errors.append("Role file missing 'id' field")
            result.valid = False
        elif not re.match(r"^[a-z][a-z0-9-]*$", header.id):
            result.errors.append(
                f"Invalid id format: '{header.id}' (must be kebab-case)"
            )
            result.valid = False

    return result


def extract_header_bounds(content: str) -> Optional[tuple[int, int]]:
    """Find start and end positions of header in content."""
    pattern = r"(?:<!--|#\s*<!--|/\*|//\s*<!--)\s*AGENT_HEADER.*?(?:(?:#|//|--)\s*)?AGENT_HEADER_END\s*(?:-->|\*/)"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.start(), match.end()
    return None


