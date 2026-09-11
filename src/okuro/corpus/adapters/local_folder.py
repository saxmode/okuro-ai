# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Local-folder corpus adapter — register a directory of documents as a
#   cortex project in place. Non-materializing: nothing is copied, the user's
#   folder IS the corpus path.
# index: class LocalFolderAdapter | class LocalFolderAdapter
# AGENT_HEADER_END -->
"""Make a folder of documents searchable without moving it.

This adapter is deliberately NON-MATERIALIZING (``materializes = False``).
Copying a user's Obsidian vault or docs tree into ``corpora/`` would create a
second copy that goes stale the moment they edit the original, and would make
remove_corpus capable of deleting files okuro does not own. Instead the corpus
``path`` points at the user's own directory and cortex indexes it in place —
the same arrangement as a locally-registered project.

``enumerate`` therefore exists only to report what WOULD be indexed and to
supply the delta tokens that make ``corpus_sync`` able to say "6 changed, 340
unchanged" instead of shrugging. The actual read path is cortex's own
directory walk, which already handles content hashing, exclusions, and binary
detection.

Version token is ``mtime_ns:size``. Not a content hash: hashing every file on
every listing pass would cost a full read of the tree, which is exactly the
expense the two-phase interface exists to avoid. cortex re-hashes content
anyway before it re-embeds, so a false "changed" from a touched-but-identical
file costs one hash, not one embedding.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Iterable

from okuro.corpus.adapters.base import CorpusAdapter, CorpusItem, ItemRef

log = logging.getLogger(__name__)

# Mirrors the text-ish half of cortex's own extension set. Anything cortex
# cannot index is pointless to enumerate — it would inflate item_count with
# files that never become searchable.
_DEFAULT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".org",
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".yaml", ".yml", ".toml", ".json", ".csv",
    ".sh", ".bash", ".sql", ".css", ".scss", ".html",
}

_DEFAULT_EXCLUDES = [
    "**/.git/**", "**/node_modules/**", "**/.venv/**", "**/venv/**",
    "**/__pycache__/**", "**/.obsidian/**", "**/.trash/**",
    "**/dist/**", "**/build/**", "**/.next/**",
]


class LocalFolderAdapter(CorpusAdapter):
    source_type = "local_folder"
    materializes = False

    def __init__(
        self,
        path: str,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        extensions: list[str] | None = None,
    ) -> None:
        """
        Args:
            path:       absolute path to the folder to index.
            include:    optional glob allowlist, relative to the folder. When
                        given, a file must match one of these to be included.
            exclude:    additional glob denylist, merged with the defaults.
            extensions: override the indexed extension set (with or without dot).
        """
        self.root = Path(path).expanduser().resolve()
        self.include = include or []
        self.exclude = [*_DEFAULT_EXCLUDES, *(exclude or [])]
        if extensions:
            self.extensions = {e if e.startswith(".") else f".{e}" for e in extensions}
        else:
            self.extensions = set(_DEFAULT_EXTENSIONS)

    @classmethod
    def from_url(cls, url: str) -> dict | None:
        """Accept a filesystem path or a file:// url.

        Runs LAST in detection order — an absolute path is unambiguous, but a
        bare string is not, so this only claims input that actually resolves to
        a directory on disk.
        """
        u = (url or "").strip()
        if not u:
            return None
        if u.startswith("file://"):
            from urllib.parse import unquote, urlparse as _p

            u = unquote(_p(u).path)
        if "://" in u:
            return None
        p = Path(u).expanduser()
        return {"path": str(p.resolve())} if p.is_dir() else None

    def describe(self) -> dict:
        return {
            "source_type": self.source_type,
            "path": str(self.root),
            "extensions": sorted(self.extensions),
            "include": self.include,
            "exclude_count": len(self.exclude),
        }

    def validate(self) -> None:
        """Fail early with a message that names the actual problem."""
        if not self.root.exists():
            raise ValueError(f"folder does not exist: {self.root}")
        if not self.root.is_dir():
            raise ValueError(f"not a directory: {self.root}")

    @property
    def index_extensions(self) -> set[str]:
        """Extensions the corpus layer asks cortex to index for this folder.

        Passed through to VectorStore.index_directory so cortex indexes exactly
        what this adapter counted. Without it, cortex falls back to its own
        code-oriented default set, which omits .txt/.rst/.org/.csv — document
        types that are the whole point of a docs folder. They would be counted
        here and silently absent from search.
        """
        return set(self.extensions)

    def _cortex_matcher(self):
        """Cortex's own exclusion matcher, not a parallel reimplementation.

        The first live run counted 9 items where cortex indexed 8: the extra
        was a dotfile cortex excludes and this adapter did not. Two definitions
        of "what counts" drift by construction, and the count is what the UI
        reports as searchable — so the authority has to be one object, and it
        has to be cortex's, since cortex does the indexing.
        """
        from okuro.cortex.exclusions import build_matcher

        return build_matcher(self.root, extra_deny=list(self.exclude))

    def enumerate(self) -> Iterable[ItemRef]:
        self.validate()
        matcher = self._cortex_matcher()
        from okuro.cortex.exclusions import is_data_exhaust

        refs: list[ItemRef] = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            if p.suffix.lower() not in self.extensions:
                continue
            rel_path = p.relative_to(self.root)
            rel = rel_path.as_posix()
            if matcher.is_excluded(rel_path):
                continue
            # Machine-generated blobs (minified bundles, data-lake JSON) are
            # skipped by cortex at index time; counting them here would inflate
            # item_count with rows that never become searchable.
            try:
                if is_data_exhaust(p):
                    continue
            except Exception:  # noqa: BLE001 — heuristic, never fatal to a listing
                pass
            if self.include and not any(fnmatch.fnmatch(rel, g) for g in self.include):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            refs.append(
                ItemRef(
                    key=rel,
                    version=f"{st.st_mtime_ns}:{st.st_size}",
                    title=p.stem,
                    segments=tuple(p.relative_to(self.root).parts[:-1]),
                    url=p.as_uri(),
                )
            )
        log.info("local_folder: enumerated %d files under %s", len(refs), self.root)
        return refs

    def fetch(self, ref: ItemRef) -> CorpusItem:
        """Read one file.

        Only used when something asks for content explicitly — the indexing
        path never calls this, because cortex reads the folder directly.
        """
        p = self.root / ref.key
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"cannot read {p}: {exc}") from exc
        return CorpusItem(ref=ref, body=body, metadata={"path": str(p)})
