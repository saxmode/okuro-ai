# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Single source of truth for cortex file/dir exclusion — gitignore-correct semantics
# index:
#   imports
#   DEFAULT_DIR_DENY
#   DEFAULT_FILE_DENY_PATTERNS
#   DEFAULT_KEEP_OVERRIDES
#   class ExclusionMatcher
#   def build_matcher
# AGENT_HEADER_END -->
"""Single source of truth for cortex file/directory exclusion.

Previous behaviour used ``fnmatch`` with patterns like ``**/node_modules/**``.
``fnmatch`` does NOT honour ``**`` the way gitignore / globstar do, so any
top-level ``node_modules`` / ``.git`` / ``dist`` was leaking through and
getting enqueued for LLM enrichment.

This module replaces every ad-hoc exclusion site (scanner.should_scan,
vectorstore.index_directory) with one matcher that:

1. Prunes by directory part-name (fast, dir-walk friendly).
2. Applies gitignore-correct globs via ``pathspec`` for everything else.
3. Honours the repo's own ``.gitignore`` files when present.
4. Lets callers re-include high-signal files (README, docs, configs) via
   an explicit allowlist that wins over the deny list.

Keeping this in one place means future drift can't silently degrade
coverage: change the rule here, every scanner picks it up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

import pathspec


# Directory part-names that should never be descended into. Cheap to check
# against ``path.parts``. Root-level matches are honoured (the original
# fnmatch bug).
DEFAULT_DIR_DENY: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    ".cache",
    "dist",
    "build",
    "coverage",
    "vendor",
    "site-packages",
    ".pnpm",
    ".pnpm-store",
    ".github",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
    ".claude",
    ".codex",
    ".gemini",
    ".cursor",
    ".worktrees",
    "release",
    "out",
    ".turbo",
    ".parcel-cache",
    ".svelte-kit",
})

# File-level deny patterns (gitignore semantics). These run via pathspec.
DEFAULT_FILE_DENY_PATTERNS: tuple[str, ...] = (
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/*.lock",
    "**/.DS_Store",
    "**/._*",
    "**/*.egg-info/**",
    # cortex's own metadata is never an indexable target.
    "**/.okuro-index.yaml",
    "**/.agent_scan_marker",
    # Agent worktree pollution (1.2 / audit F2). ``.claude`` is already a
    # part-name deny above, which transitively prunes ``.claude/worktrees``
    # — but the audit (2026-06-01) found 28,739 docs already indexed there
    # because they were ingested *before* ``.claude`` was denied (worktree
    # docs indexed 2026-05-11..2026-05-19 16:07; ``.claude`` denied
    # 2026-05-19 20:36). The deny only prevents *future* indexing; existing
    # rows are purged retroactively by `okuro cortex reclaim`
    # (reconcile-on-exclude). These patterns add the non-``.claude`` worktree
    # layouts. Kept as gitignore patterns (not part-name denies) so they
    # stay overridable by the keep-list and per-root ``.okuro/cortex-exclude``.
    "**/.worktrees/**",
    ".worktrees/**",
    "**/.claude/worktrees/**",
)

# High-signal files we explicitly KEEP even if a broader rule would deny
# them. Pathspec semantics — these are evaluated last, override = True.
DEFAULT_KEEP_OVERRIDES: tuple[str, ...] = (
    "README.md",
    "**/README.md",
    "pyproject.toml",
    "**/pyproject.toml",
    "package.json",
    "**/package.json",
    "Dockerfile",
    "**/Dockerfile",
    "compose.yaml",
    "**/compose.yaml",
    "docker-compose.yml",
    "**/docker-compose.yml",
    "vite.config.ts",
    "vite.config.js",
    "**/vite.config.*",
    "tsconfig.json",
    "**/tsconfig.json",
)

# Optional per-root override file. Lives at <root>/.okuro/cortex-exclude
# (gitignore syntax, one pattern per line). Empty / missing is fine.
PER_ROOT_OVERRIDE_RELPATH = Path(".okuro") / "cortex-exclude"


# ---------------------------------------------------------------------------
# Data-exhaust heuristic (F3)
# ---------------------------------------------------------------------------
# Data-format files (``.json`` etc.) are real source on many installs
# (``package.json``, ``tsconfig.json``, ``*.config.json``, schemas) but are
# *also* the shape generated-data exhaust takes (a directory of 15k machine-
# written JSON blobs). A path glob can't tell them apart install-generically,
# so we use HEURISTIC signals over the file + its directory instead. All
# thresholds are module constants so a per-root override (the ``.okuro/
# cortex-exclude`` file already handles path-based escapes) or a future config
# surface can tune them without code edits.

# Extensions treated as *data* formats — candidates for the exhaust heuristic.
# Code/markup extensions (.py/.ts/.md/...) are never data-exhaust by this rule.
DATA_EXHAUST_EXTENSIONS: frozenset[str] = frozenset({
    ".json",
    ".ndjson",
    ".jsonl",
    ".geojson",
})

# Hand-written config/manifest JSON we must NEVER skip, regardless of size or
# directory density. Matched on the lowercased filename (basename).
DATA_EXHAUST_KEEP_FILENAMES: frozenset[str] = frozenset({
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "tsconfig.base.json",
    "composer.json",
    "manifest.json",
    "components.json",
    "deno.json",
    "deno.jsonc",
    "biome.json",
    "renovate.json",
    "lerna.json",
    "nx.json",
    "turbo.json",
    "vercel.json",
    "now.json",
    "angular.json",
    "app.json",
    "babel.config.json",
    "jsconfig.json",
})

# Per-file byte ceiling. Above this a data-format file looks like a dump, not a
# hand-authored manifest. 256 KB comfortably clears real configs (the largest
# package-lock.json in the wild is ~hundreds of KB but those are KEEP-listed).
DATA_EXHAUST_MAX_BYTES: int = 256 * 1024

# Per-directory density: a directory holding more than this many files of the
# SAME data extension looks machine-generated (a data lake), not a config dir.
DATA_EXHAUST_DIR_COUNT: int = 50

# Minified / single-giant-line detector: if the first line alone exceeds this
# many bytes the file is almost certainly a minified blob or a one-line giant
# array, not something a human edits.
DATA_EXHAUST_MAX_FIRST_LINE_BYTES: int = 100 * 1024


def _is_kept_data_filename(name: str) -> bool:
    """True for hand-written config JSON we always keep.

    Covers the explicit allowlist plus the ``*.config.json`` convention
    (``vite.config.json``, ``jest.config.json``, …) which is human-authored.
    """
    low = name.lower()
    if low in DATA_EXHAUST_KEEP_FILENAMES:
        return True
    if low.endswith(".config.json"):
        return True
    return False


def is_data_exhaust(
    file_path: Path,
    *,
    sibling_same_ext_count: Optional[int] = None,
    max_bytes: int = DATA_EXHAUST_MAX_BYTES,
    dir_count_threshold: int = DATA_EXHAUST_DIR_COUNT,
    max_first_line_bytes: int = DATA_EXHAUST_MAX_FIRST_LINE_BYTES,
) -> bool:
    """Heuristic: is ``file_path`` generated data-exhaust (skip) vs source (keep)?

    Returns ``True`` only for *data-format* files (see
    :data:`DATA_EXHAUST_EXTENSIONS`) that trip at least one generated-data
    signal AND are not on the hand-written keep-list. Non-data extensions and
    keep-listed configs always return ``False``.

    Signals (any one is sufficient):
      * file size  > ``max_bytes``                          (a dump, not a config)
      * directory has > ``dir_count_threshold`` same-ext    (a data lake)
        files — caller supplies the count via ``sibling_same_ext_count``
        so we don't re-stat the directory per file
      * first line > ``max_first_line_bytes``               (minified / giant array)

    The function is filesystem-tolerant: any ``OSError`` while statting/reading
    falls through to "keep" so a transient error never silently drops a file.
    Path-based escapes (always-keep / always-skip a directory) stay the job of
    the per-root ``.okuro/cortex-exclude`` override.
    """
    ext = file_path.suffix.lower()
    if ext not in DATA_EXHAUST_EXTENSIONS:
        return False
    if _is_kept_data_filename(file_path.name):
        return False

    # Signal 2 — directory density (cheapest when caller pre-counted).
    if (
        sibling_same_ext_count is not None
        and sibling_same_ext_count > dir_count_threshold
    ):
        return True

    # Signal 1 — file size.
    try:
        if file_path.stat().st_size > max_bytes:
            return True
    except OSError:
        return False

    # Signal 3 — minified / single giant line.
    try:
        with open(file_path, "rb") as fh:
            first = fh.readline(max_first_line_bytes + 1)
        if len(first) > max_first_line_bytes:
            return True
    except OSError:
        return False

    return False


@dataclass
class ExclusionMatcher:
    """Decides whether a file or directory should be skipped by cortex.

    Built once per root via :func:`build_matcher`. Holds compiled pathspecs
    so the hot path is a few cheap part-checks + one match call.
    """

    root: Path
    dir_deny: frozenset[str] = field(default_factory=lambda: DEFAULT_DIR_DENY)
    deny_spec: pathspec.PathSpec = field(default_factory=lambda: pathspec.PathSpec.from_lines("gitignore",[]))
    # keep_spec = default high-signal keeps; overrides the deny-spec but NOT the
    # directory denylist. override_spec = caller's explicit extra_keep; the
    # strongest allow — wins over the directory denylist too.
    keep_spec: pathspec.PathSpec = field(default_factory=lambda: pathspec.PathSpec.from_lines("gitignore",[]))
    override_spec: pathspec.PathSpec = field(default_factory=lambda: pathspec.PathSpec.from_lines("gitignore",[]))

    def is_pruned_dir(self, dir_name: str) -> bool:
        """True if a directory name should be pruned during os.walk.

        Use this in ``dirnames[:] = [d for d in dirnames if not matcher.is_pruned_dir(d)]``
        to avoid descending into noise.
        """
        return dir_name in self.dir_deny or (dir_name.startswith(".") and dir_name not in {".github"})

    def is_excluded(self, rel_path: str | Path) -> bool:
        """True if ``rel_path`` (relative to root) should be skipped.

        Order:
          1. The explicit override-allowlist matches → keep (wins over ALL).
          2. Any path part matches the directory denylist → exclude.
          3. The default keep-allowlist matches → keep (overrides deny-spec).
          4. The deny-spec matches → exclude.
          5. Otherwise → keep.

        Two keep tiers: the caller's ``extra_keep`` (override_spec) is the
        strongest allow and re-includes a path even under a denied directory
        (e.g. a per-root ``.worktrees/keep/**``); the default high-signal keep
        list (keep_spec) only overrides the deny-spec, NOT the directory
        denylist — so a ``tsconfig.json`` inside ``.claude/worktrees`` stays
        excluded.

        Accepts strings or Paths. Always normalised to forward-slash posix.
        """
        if isinstance(rel_path, Path):
            rel = rel_path.as_posix()
            parts = rel_path.parts
        else:
            rel = rel_path.replace("\\", "/")
            parts = tuple(PurePosixPath(rel).parts)

        if self.override_spec.match_file(rel):
            return False

        for part in parts:
            if part in self.dir_deny:
                return True

        if self.keep_spec.match_file(rel):
            return False

        if self.deny_spec.match_file(rel):
            return True

        return False


def _segment_is_pruned(segment: str) -> bool:
    """Mirror of ExclusionMatcher.is_pruned_dir for a single path segment.

    Kept in sync with that method so gitignore ingest and the walk agree on
    what counts as a pruned directory.
    """
    return segment in DEFAULT_DIR_DENY or (
        segment.startswith(".") and segment != ".github"
    )


def _gitignore_lines(root: Path) -> list[str]:
    """Read every ``.gitignore`` found under root — except those living inside
    directories the walk prunes anyway.

    Nested ``.gitignore`` files inside pruned trees (``node_modules``, ``.git``,
    build dirs, …) contribute patterns that can never match a walked path, yet
    each one bloats the single deny-spec and every ``is_excluded`` call pays
    O(patterns). On a large monorepo root this was 785 files → ~20k patterns →
    ~4ms/call. Skipping pruned-tree gitignores cut it ~5x with zero change to
    the eligible-file set (the walk never descends there).

    Patterns are anchored at the file's directory by gitignore convention.
    pathspec handles re-anchoring when we feed prefixed lines.
    """
    lines: list[str] = []
    for gi in root.rglob(".gitignore"):
        try:
            rel_dir = gi.parent.relative_to(root)
        except ValueError:
            continue
        if any(_segment_is_pruned(part) for part in rel_dir.parts):
            continue
        prefix = rel_dir.as_posix()
        try:
            for raw in gi.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Re-anchor non-rooted patterns under their gitignore's dir.
                if prefix and prefix != ".":
                    if line.startswith("/"):
                        line = f"{prefix}{line}"
                    else:
                        line = f"{prefix}/{line}"
                lines.append(line)
        except OSError:
            continue
    return lines


def _per_root_override_lines(root: Path) -> list[str]:
    override = root / PER_ROOT_OVERRIDE_RELPATH
    if not override.is_file():
        return []
    try:
        return [
            ln.strip()
            for ln in override.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    except OSError:
        return []


def build_matcher(
    root: Path,
    *,
    extra_deny: Iterable[str] = (),
    extra_keep: Iterable[str] = (),
    use_gitignore: bool = True,
) -> ExclusionMatcher:
    """Build an :class:`ExclusionMatcher` for a project root.

    ``use_gitignore`` defaults to True — every ``.gitignore`` under the root
    contributes to the deny set, which keeps cortex's exclusions aligned with
    what git itself ignores. Pass False for tests / synthetic trees.
    """
    deny_lines: list[str] = list(DEFAULT_FILE_DENY_PATTERNS)
    if use_gitignore:
        deny_lines.extend(_gitignore_lines(root))
    deny_lines.extend(_per_root_override_lines(root))
    deny_lines.extend(extra_deny)

    keep_lines: list[str] = list(DEFAULT_KEEP_OVERRIDES)
    override_lines: list[str] = list(extra_keep)

    return ExclusionMatcher(
        root=root,
        deny_spec=pathspec.PathSpec.from_lines("gitignore",deny_lines),
        keep_spec=pathspec.PathSpec.from_lines("gitignore",keep_lines),
        override_spec=pathspec.PathSpec.from_lines("gitignore",override_lines),
    )
