# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.cortex (皮質) — codebase navigation.
# index: none
# AGENT_HEADER_END -->
"""okuro.cortex (皮質) — codebase navigation."""

from typing import TYPE_CHECKING

from okuro._lazy import install as _install

if TYPE_CHECKING:  # static analysers still see the full surface
    from .core import (
        AgentHeader,
        FileRole,
        IndexEntry,
        generate_header,
        parse_header,
        validate_header,
    )
    from .parsers import ParseResult, parse_file
    from .roots import (
        Root,
        project_for_path,
        register_root,
        registered_roots,
        resolve_path,
        resolve_under_roots,
    )
    from .scanner import (
        ScanConfig,
        Scanner,
        UpdateReason,
        UpdateReasonCode,
        scan_directory,
    )
    from .sidecar import (
        SIDECAR_FILENAME,
        SidecarEntry,
        entry_from_header,
        entry_to_header,
        invalidate as invalidate_sidecar,
        load as load_sidecar,
        save as save_sidecar,
    )
    from .vectorstore import SearchResult, VectorConfig, VectorStore

# Deferred: this package is the parent of okuro.cortex.mcp_tools, so eagerly
# re-exporting here loaded 16 files — including vectorstore, whose own comment
# claimed it was lazy while the try/except imported it every time. See
# okuro._lazy.
_EXPORTS = {
    **{n: ("okuro.cortex.core", n) for n in (
        "AgentHeader", "IndexEntry", "FileRole", "parse_header",
        "generate_header", "validate_header")},
    **{n: ("okuro.cortex.sidecar", n) for n in (
        "SidecarEntry", "SIDECAR_FILENAME", "entry_from_header",
        "entry_to_header")},
    # aliases — sidecar.load is exposed as load_sidecar
    "load_sidecar": ("okuro.cortex.sidecar", "load"),
    "save_sidecar": ("okuro.cortex.sidecar", "save"),
    "invalidate_sidecar": ("okuro.cortex.sidecar", "invalidate"),
    **{n: ("okuro.cortex.parsers", n) for n in ("parse_file", "ParseResult")},
    **{n: ("okuro.cortex.scanner", n) for n in (
        "Scanner", "ScanConfig", "scan_directory", "UpdateReason",
        "UpdateReasonCode")},
    **{n: ("okuro.cortex.roots", n) for n in (
        "Root", "registered_roots", "project_for_path", "register_root",
        "resolve_path", "resolve_under_roots")},
    # optional — resolve to None when the vector extras are absent, which is
    # what the old try/except did
    **{n: ("okuro.cortex.vectorstore", n) for n in (
        "VectorStore", "VectorConfig", "SearchResult")},
}

_lazy_getattr, __dir__ = _install(
    globals(), _EXPORTS,
    optional=("VectorStore", "VectorConfig", "SearchResult"),
)


def __getattr__(name: str):
    """Adds HAS_VECTOR, which is a probe rather than a re-export.

    It used to be computed by an eager ``try: from .vectorstore import ...``
    at package import — the single most expensive line here. Computing it on
    access keeps the exact same value and the same False-on-ImportError
    semantics, without paying for it in processes that never touch vectors.
    """
    if name == "HAS_VECTOR":
        try:
            from importlib import import_module

            import_module("okuro.cortex.vectorstore")
            value = True
        except ImportError:
            value = False
        globals()["HAS_VECTOR"] = value
        return value
    return _lazy_getattr(name)


__version__ = "1.0.0"

__all__ = [
    "AgentHeader",
    "IndexEntry",
    "FileRole",
    "parse_header",
    "generate_header",
    "validate_header",
    "SidecarEntry",
    "SIDECAR_FILENAME",
    "load_sidecar",
    "save_sidecar",
    "invalidate_sidecar",
    "entry_from_header",
    "entry_to_header",
    "parse_file",
    "ParseResult",
    "Scanner",
    "ScanConfig",
    "scan_directory",
    "UpdateReason",
    "UpdateReasonCode",
    "VectorStore",
    "VectorConfig",
    "SearchResult",
    "HAS_VECTOR",
    "Root",
    "registered_roots",
    "project_for_path",
    "register_root",
    "resolve_path",
    "resolve_under_roots",
]
