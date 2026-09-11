# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Scanner Module for AGENT_HEADER
# index:
#   imports
#   class UpdateReasonCode
#   class UpdateReason
#   class ScanResult
#   class GitIntegration
#   class UpdateDetector
#   class ScanConfig
#   class Scanner
#   def scan_directory
#   class ScanError
#   class ValidationError
# AGENT_HEADER_END -->
"""
Scanner Module for AGENT_HEADER
Git-aware file discovery and incremental header updates.
"""

import hashlib
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .core import (
    AgentHeader,
    IndexEntry,
    parse_header,
    validate_header,
    HEADER_WRAPPERS,
    FILENAME_WRAPPERS,
)
from .exclusions import ExclusionMatcher, build_matcher
from .parsers import parse_file, ParseResult


class UpdateReasonCode(Enum):
    NO_HEADER = "no_header"
    EMPTY_HEADER = "empty_header"
    COUNT_CHANGED = "count_changed"
    DESC_CHANGED = "desc_changed"
    UP_TO_DATE = "up_to_date"


@dataclass
class UpdateReason:
    needs_update: bool
    reason_code: UpdateReasonCode
    description: str = ""


@dataclass
class ScanResult:
    file_path: Path
    parsed: Optional[ParseResult]
    current_header: Optional[AgentHeader]
    update_reason: UpdateReason
    new_header: Optional[AgentHeader] = None


class GitIntegration:
    MARKER_FILE = ".agent_scan_marker"

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.marker_path = repo_root / self.MARKER_FILE

    def is_git_repo(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False

    def get_last_scan_commit(self) -> Optional[str]:
        if not self.marker_path.exists():
            return None
        return self.marker_path.read_text().strip()

    def set_scan_marker(self, commit: Optional[str] = None):
        if commit is None:
            commit = self.get_current_commit()
        if commit:
            self.marker_path.write_text(commit)

    def get_current_commit(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            pass
        return None

    def get_changed_files(self, since_commit: Optional[str] = None) -> list[Path]:
        try:
            names: set[str] = set()
            if since_commit:
                # marker -> working tree (includes committed + staged + unstaged)
                diff = subprocess.run(
                    ["git", "diff", "--name-only", since_commit],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                )
                if diff.returncode == 0:
                    names.update(line for line in diff.stdout.strip().split("\n") if line)
                # untracked
                untracked = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                )
                if untracked.returncode == 0:
                    names.update(line for line in untracked.stdout.strip().split("\n") if line)
            else:
                tracked = subprocess.run(
                    ["git", "ls-files"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                )
                if tracked.returncode == 0:
                    names.update(line for line in tracked.stdout.strip().split("\n") if line)
                untracked = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                )
                if untracked.returncode == 0:
                    names.update(line for line in untracked.stdout.strip().split("\n") if line)
            return [self.repo_root / n for n in sorted(names)]
        except FileNotFoundError:
            pass
        return []


class UpdateDetector:
    def needs_update(
        self, current: Optional[AgentHeader], new_entries: list[IndexEntry]
    ) -> UpdateReason:
        if current is None:
            return UpdateReason(
                needs_update=True,
                reason_code=UpdateReasonCode.NO_HEADER,
                description="No existing header",
            )

        if not current.role and not current.purpose and not current.index:
            return UpdateReason(
                needs_update=True,
                reason_code=UpdateReasonCode.EMPTY_HEADER,
                description="Header wrapper present but body empty",
            )

        if len(current.index) != len(new_entries):
            return UpdateReason(
                needs_update=True,
                reason_code=UpdateReasonCode.COUNT_CHANGED,
                description=f"Entry count: {len(current.index)} -> {len(new_entries)}",
            )

        for i, (curr, new) in enumerate(zip(current.index, new_entries)):
            curr_desc = " ".join(curr.description.split())
            new_desc = " ".join(new.description.split())
            if curr_desc != new_desc:
                return UpdateReason(
                    needs_update=True,
                    reason_code=UpdateReasonCode.DESC_CHANGED,
                    description=f"Entry {i + 1}: '{curr_desc}' -> '{new_desc}'",
                )

        return UpdateReason(
            needs_update=False,
            reason_code=UpdateReasonCode.UP_TO_DATE,
            description="Header is current",
        )


@dataclass
class ScanConfig:
    # Retained for back-compat with callers that pass custom patterns. When
    # non-empty, these are appended to the per-root deny list. The hard-coded
    # baseline now lives in okuro.cortex.exclusions so root-level dir names
    # (node_modules, .git, dist, …) are reliably pruned — fnmatch did not
    # honour ``**/X/**`` for root-relative paths and silently leaked them.
    include_patterns: list[str] = field(default_factory=lambda: ["**/*"])
    exclude_patterns: list[str] = field(default_factory=list)
    keep_overrides: list[str] = field(default_factory=list)
    multiline_threshold: int = 5
    max_index_entries: int = 50
    generate_loc: bool = True
    generate_up: bool = True
    infer_parent: bool = True
    use_gitignore: bool = True


class Scanner:
    SUPPORTED_EXTENSIONS = set(HEADER_WRAPPERS.keys())
    SUPPORTED_FILENAMES = set(FILENAME_WRAPPERS.keys())

    def __init__(self, root: Path, config: Optional[ScanConfig] = None):
        self.root = root
        self.config = config or ScanConfig()
        self.git = GitIntegration(root)
        self.detector = UpdateDetector()
        self.matcher: ExclusionMatcher = build_matcher(
            root,
            extra_deny=self.config.exclude_patterns,
            extra_keep=self.config.keep_overrides,
            use_gitignore=self.config.use_gitignore,
        )

    def should_scan(self, file_path: Path) -> bool:
        ext = file_path.suffix.lower()
        filename = file_path.name.lower()
        if (
            ext not in self.SUPPORTED_EXTENSIONS
            and filename not in self.SUPPORTED_FILENAMES
        ):
            return False

        try:
            rel = file_path.relative_to(self.root)
        except ValueError:
            return False
        return not self.matcher.is_excluded(rel)

    def infer_parent_doc(self, file_path: Path) -> Optional[str]:
        rel = file_path.relative_to(self.root)
        parts = rel.parts
        for i in range(len(parts) - 1, 0, -1):
            candidate = self.root / Path(*parts[:i]) / "docs" / "start.md"
            if candidate.exists() and candidate != file_path:
                return str(Path(*parts[:i]) / "docs" / "start.md")
            candidate = self.root / Path(*parts[:i]) / "start.md"
            if candidate.exists() and candidate != file_path:
                return str(Path(*parts[:i]) / "start.md")
        return None

    def discover_files(self, incremental: bool = True) -> list[Path]:
        if incremental and self.git.is_git_repo():
            last_commit = self.git.get_last_scan_commit()
            files = self.git.get_changed_files(last_commit)
            discovered: list[Path] = []
            for file_path in files:
                try:
                    if file_path.is_file() and self.should_scan(file_path):
                        discovered.append(file_path)
                except (PermissionError, OSError):
                    continue
            return discovered

        # Full scan: os.walk with directory pruning so we never recurse into
        # node_modules / .git / .venv / etc. — pruning at the dir level is
        # an order-of-magnitude faster than rglob + per-file matcher and is
        # the only path that was bitten by the fnmatch ** bug.
        import os

        discovered = []
        for dirpath, dirnames, filenames in os.walk(
            self.root, topdown=True, followlinks=False, onerror=lambda _e: None
        ):
            dirnames[:] = [d for d in dirnames if not self.matcher.is_pruned_dir(d)]
            for fname in filenames:
                file_path = Path(dirpath) / fname
                try:
                    if self.should_scan(file_path):
                        discovered.append(file_path)
                except (PermissionError, OSError):
                    continue
        return discovered

    def scan_file(self, file_path: Path) -> ScanResult:
        try:
            content = file_path.read_text()
        except (PermissionError, UnicodeDecodeError):
            return ScanResult(
                file_path=file_path,
                parsed=None,
                current_header=None,
                update_reason=UpdateReason(
                    needs_update=False,
                    reason_code=UpdateReasonCode.UP_TO_DATE,
                    description="Skipped",
                ),
            )

        current_header = parse_header(content, file_path=file_path)
        parsed = parse_file(file_path)
        entries = parsed.entries[: self.config.max_index_entries]
        update_reason = self.detector.needs_update(current_header, entries)

        new_header = None
        if update_reason.needs_update:
            if parsed.purpose:
                purpose = parsed.purpose
            elif current_header and current_header.purpose:
                purpose = current_header.purpose
            else:
                purpose = f"{file_path.stem} module"

            new_header = AgentHeader(
                role=parsed.role,
                purpose=purpose,
                index=entries,
                id=current_header.id if current_header else parsed.role_id,
                domain=current_header.domain if current_header else parsed.domain,
                tier=current_header.tier if current_header else parsed.tier,
                model=current_header.model if current_header else parsed.model,
            )
            if self.config.generate_loc:
                new_header.loc = str(file_path.relative_to(self.root))
            if self.config.generate_up and self.config.infer_parent:
                new_header.up = self.infer_parent_doc(file_path)

        return ScanResult(
            file_path=file_path,
            parsed=parsed,
            current_header=current_header,
            update_reason=update_reason,
            new_header=new_header,
        )

    def update_sidecars(self, results: list[ScanResult]) -> int:
        """Write scan results to per-directory .okuro-index.yaml files."""
        from . import sidecar

        # Group by directory
        by_dir: dict[Path, list[ScanResult]] = {}
        for result in results:
            parent = result.file_path.parent
            by_dir.setdefault(parent, []).append(result)

        updated = 0
        for directory, dir_results in by_dir.items():
            existing = sidecar.load(directory)
            dir_changed = False
            for result in dir_results:
                if result.new_header is None:
                    continue
                try:
                    file_hash = hashlib.md5(
                        result.file_path.read_bytes()
                    ).hexdigest()[:6]
                except (OSError, PermissionError):
                    continue
                entry = sidecar.entry_from_header(result.new_header, file_hash)
                prior = existing.get(result.file_path.name)
                # Preserve an LLM-enriched purpose when the file itself has not
                # changed.
                #
                # `entry_from_header` rebuilds the entry from the file's parsed
                # header, which for a file with no usable docstring yields a
                # weak purpose ("index module", a 3-word path fragment). Writing
                # that over an enriched purpose made scan and enrich fight each
                # other forever: scan reset the purpose to weak, enrich paid for
                # a model call to fix it, and the next tick reset it again.
                #
                # Measured before this fix: 742 of 785 daily enrichments were
                # the same 6 directories. `nav-concepts` holds ONE indexable
                # file and was enriched 126 times in 24 hours, each call
                # succeeding — so FAILURE_RETRY_BUDGET, which only counts
                # failures, could never stop it. That single loop was ~94% of
                # cortex's model spend and cortex is ~89% of all daemon compute.
                #
                # The content hash is what makes this safe: once the file
                # changes, the enriched purpose is genuinely stale and the
                # parser's version correctly wins.
                if (
                    prior is not None
                    and prior.generated_by.startswith("bridge/")
                    and prior.content_hash == file_hash
                    and not _is_weak(prior.purpose)
                ):
                    entry.purpose = prior.purpose
                    entry.generated_by = prior.generated_by
                # Only count as updated if the entry actually differs
                if prior != entry:
                    existing[result.file_path.name] = entry
                    updated += 1
                    dir_changed = True
            if not dir_changed:
                continue
            # Skip dirs we can't write to (root-owned docker volumes,
            # read-only mounts, foreign-owned subtrees) instead of
            # killing the whole scan with a PermissionError.
            try:
                sidecar.save(directory, existing)
            except (PermissionError, OSError) as exc:
                # Foreign-owned subtrees (root-owned docker volumes, read-only
                # mounts) can't be written to. Surface the failure so the
                # Health page can show it instead of silently leaving the
                # directory at 0% coverage forever.
                try:
                    from .purpose import log_failure
                    log_failure(directory, "write", str(exc))
                except Exception:
                    pass
                continue

        return updated


def scan_directory(
    root: Path,
    incremental: bool = True,
    dry_run: bool = False,
    config: Optional[ScanConfig] = None,
) -> tuple[list[ScanResult], int]:
    """Scan directory and update headers."""
    scanner = Scanner(root, config)
    files = scanner.discover_files(incremental)

    results = []
    updates = []

    for file_path in files:
        try:
            result = scanner.scan_file(file_path)
            results.append(result)
            if result.update_reason.needs_update:
                updates.append(result)
        except Exception as e:
            raise ScanError(f"Scan failed on {file_path}: {e}")

    # Validate
    for result in updates:
        if result.new_header:
            validation = validate_header(result.new_header, result.file_path)
            if not validation.valid:
                errors = "; ".join(validation.errors)
                raise ValidationError(
                    f"Invalid header for {result.file_path}: {errors}"
                )

    updated = 0
    if updates and not dry_run:
        updated = scanner.update_sidecars(updates)
        if updated:
            # Sidecars changed — drop the cached coverage so the Health/Cortex
            # tab recomputes fresh numbers on next load. Best-effort.
            try:
                from . import coverage as _coverage
                _coverage.invalidate()
            except Exception:
                pass

    if not dry_run and scanner.git.is_git_repo():
        scanner.git.set_scan_marker()

    return results, updated


def _is_weak(purpose: str) -> bool:
    """Would the enricher consider this purpose weak?

    Imported lazily and defensively: `purpose` pulls in the bridge, and a
    scanner that cannot import it must still scan. Failing closed (treating
    the purpose as weak) keeps the old behaviour rather than preserving
    something the enricher would reject.
    """
    try:
        from .purpose import is_weak_purpose

        return is_weak_purpose(purpose)
    except Exception:
        return True


class ScanError(Exception):
    pass


class ValidationError(Exception):
    pass
