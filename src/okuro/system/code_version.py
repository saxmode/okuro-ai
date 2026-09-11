# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Code-drift detection — commits landed and files edited since this process booted.
# index: imports | def _repo_root | def _process_start | def _iter_watched | def _ensure_baseline | def _worktree_drift | def detect_code_drift | def format_drift_warning
# AGENT_HEADER_END -->
"""Code-drift detection — has this process fallen behind the repo?

Every agent session spawns its own MCP server process
(``python -m okuro.mcp.server``). Python imports each module exactly once,
okuro is an editable install (``src/okuro`` is resolved at import time),
and MCP tool descriptions are emitted to the client at handshake. So a
session's okuro code *and* its tool descriptions are frozen at process
start, permanently. Committing to the repo does nothing for a session
that is already running, and nothing hot-reloads.

Measured 2026-07-15: six of nine live MCP servers were 6-101 commits
behind HEAD. Concurrently-running sessions served different behaviour
under the same tool name, and nothing anywhere surfaced it — the drift is
invisible from inside the session it damages. okuro ships tens of commits
a day, so a long-lived session rots quickly.

This module answers two questions: which commits landed after this process
booted, and which source files were edited after it booted? Both are, by
construction, changes the process cannot have loaded.

The working-tree half exists because commit-based detection alone was blind
to the dominant dev case. An agent edits ``src/okuro/foo.py``, does not
commit, and "tests" it — the commit check reports "in sync" while the
running code and the file on disk differ. Six untracked role YAMLs under
``src/okuro/roles/catalog/`` were loaded at runtime and invisible to every
drift surface before this.

Changed-since-boot is the criterion, and it is decided on CONTENT, not on
mtime. A file edited BEFORE this process booted was loaded in its edited
form, so it is not drift; a file whose bytes still match what we loaded is
not drift either, however recently something opened it for writing.

Limits, on purpose:
  - Proxies loaded code by commit date. A commit landing seconds after
    boot is still correctly reported as unloaded.
  - Only watches ``src/okuro`` — the importable package. Edits to tests,
    scripts or docs do not change what a running server executes.
  - Content baseline is taken lazily on the first drift check, not at
    import, so a file changed between boot and that first check cannot be
    hashed honestly. Those are reported as drift rather than trusted.
  - Returns None (silent) when okuro is not a git checkout, or when the
    process start time cannot be read. A staleness check must never be
    the reason bootstrap fails.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from okuro.db.engine import okuro_home

# src/okuro/system/code_version.py -> parents[3] == repo root
_REPO_ROOT_DEPTH = 3

# Cap the subjects listed in the warning. The count carries the signal;
# the subjects are only there to make the drift concrete.
_MAX_SUBJECTS = 3

# Same cap, same reason, for edited-file paths.
_MAX_DIRTY = 5

# Only the importable package can change what a running server executes.
_WATCHED_SUBTREE = "src/okuro"

# Extensions whose edit changes runtime behaviour. Role catalogues and
# migrations ship as data but are loaded at runtime, so they count.
_WATCHED_SUFFIXES = (".py", ".sql", ".yaml", ".yml", ".json")


def _repo_root() -> Optional[Path]:
    """Repo root iff okuro is running from a git checkout, else None."""
    try:
        root = Path(__file__).resolve().parents[_REPO_ROOT_DEPTH]
    except IndexError:
        return None
    return root if (root / ".git").exists() else None


def _process_start() -> Optional[float]:
    """Epoch seconds when THIS process started, or None if unknowable."""
    try:
        import psutil

        return psutil.Process().create_time()
    except Exception:
        # Linux fallback — /proc/self mtime is the process start time.
        try:
            return os.stat("/proc/self").st_mtime
        except OSError:
            return None


def _imported_okuro_files(root: Path) -> set[Path]:
    """Files this interpreter has actually LOADED, from ``sys.modules``.

    The gate's question is "is the code running in THIS process older than the
    disk". Only a file this process imported can make that true. Walking the
    whole subtree instead answered a different, much broader question — "did
    anything under src/okuro change" — and every extra file was a chance to
    refuse a session over code the process never executed.

    Measured on this repo: 239 imported files vs 844 walked. Editing a template,
    a role catalog, a migration or an unimported module can no longer strand a
    session, because none of them are inside any running interpreter.

    Returns an empty set if introspection fails, so the caller falls back to the
    subtree walk rather than silently watching nothing.
    """
    import sys as _sys
    out: set[Path] = set()
    try:
        for mod in list(_sys.modules.values()):
            f = getattr(mod, "__file__", None)
            if not f:
                continue
            try:
                p = Path(f).resolve()
            except (OSError, ValueError):
                continue
            if p.suffix not in _WATCHED_SUFFIXES:
                continue
            try:
                p.relative_to(root)
            except ValueError:
                continue  # stdlib / site-packages — not ours to watch
            out.add(p)
    except Exception:  # noqa: BLE001 — introspection must never break the gate
        return set()
    return out


def _iter_watched(path: Path):
    """Yield watched source files at ``path`` — itself, or its tree if a dir.

    ``git status --porcelain`` collapses an untracked directory to a single
    entry (``?? src/okuro/roles/catalog/``), so a directory has to be walked
    or every file inside it is missed.
    """
    if path.is_dir():
        for sub in path.rglob("*"):
            if sub.is_file() and sub.suffix in _WATCHED_SUFFIXES:
                yield sub
    elif path.is_file() and path.suffix in _WATCHED_SUFFIXES:
        yield path


# Content baseline: what every watched file held when this process began
# judging drift. Keyed by root so a relocated checkout — or a test using a
# tmp_path — rebuilds instead of reusing another tree's hashes.
_baseline: dict = {"root": None, "hashes": {}, "predrift": set(),
                   "ignored": frozenset(), "primed_at_boot": False,
                   "watched_paths": frozenset(), "scoped_to_imports": False}


def _ignored_watched(root: Path, git) -> frozenset:
    """Relpaths under the watched subtree that git IGNORES.

    These are generated artifacts, not source — e.g. okuro's own
    `.okuro-index.yaml`, which the indexer rewrites ~90s after boot. Their
    suffix (.yaml) is watched and rglob does not honour .gitignore, so the
    content baseline would hash them and then read the post-boot rewrite as
    drift — false-flagging EVERY long-lived session as stale ~90s in (measured
    2026-07-20: this self-inflicted gate blocked bridge_invoke + artifact_write
    server-wide). One git call, cached with the baseline.
    """
    out = git(
        "ls-files", "--others", "--ignored", "--exclude-standard",
        "--", _WATCHED_SUBTREE,
    )
    return frozenset(ln for ln in (out or "").splitlines() if ln.strip())


def _hash_file(path: Path) -> Optional[str]:
    """sha256 of ``path``, or None if it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _top_up_baseline(root: Path) -> None:
    """Add modules imported AFTER the baseline was primed.

    THE HOLE THIS CLOSES, measured 2026-07-28. Scoping the baseline to the
    import closure was right and deliberate — only loaded code can make this
    process stale, and it cut the false-positive surface 65%. But the SET was
    captured once, at prime time, and never grew. Most of okuro's tool surface
    imports LAZILY, on the first call to a tool that needs it, which is always
    after priming. Such a module is in ``sys.modules`` and therefore frozen and
    staleable — but absent from ``hashes``, so nothing ever compares it.

    Observed live: memory_staleness.py was edited in-session, the MCP tool then
    served pre-fix results for two shipped changes, and the gate did not fire.
    ``memory_stale`` is not allowlisted; the file simply was not watched.
    An agent trusting that answer gets stale results presented as current —
    precisely what the gate exists to make impossible.

    HASHING A NEWCOMER AT FIRST SIGHT IS HONEST ONLY IF FIRST SIGHT IS CLOSE TO
    IMPORT TIME. That is the whole load-bearing assumption, and WHEN this runs
    decides whether it holds:

      * from ``note_imports`` (post_tool_call) it holds. The import happened
        inside the tool call that just returned, microseconds ago, and the agent
        does not get control back until after this runs — so it cannot have
        edited anything in between.

      * from ``_ensure_baseline`` (drift-check time) it does NOT hold, and this
        was measured, not theorised. Lazy imports land INSIDE ``handle_tool``,
        while the check runs in the NEXT call's ``pre_tool_call``. Sequence:
        tool call imports module (bytes B0) -> agent edits it (bytes B1) ->
        next call's check sees the module for the first time and records B1.
        The baseline now holds bytes this process never loaded, drift reads
        clean forever, and the gate serves stale answers as current. That is
        exactly the agent's normal rhythm: call a tool, edit the module it
        used, call it again.

    So ``note_imports`` is the primary path and the check-time call is a
    backstop for imports that happen outside tool dispatch. The backstop still
    hashes rather than flagging, because marking an unverifiable newcomer dirty
    would rebuild ``predrift`` — a permanently-dirty class nothing can clear,
    the bug ``_ensure_baseline``'s docstring exists to warn about. Its residual
    exposure is one edit in one window, and it self-corrects: the NEXT edit
    still changes the hash.

    Cheap by construction: only files absent from the baseline are hashed, so
    the steady state is a set-difference over sys.modules and nothing else.
    """
    known = _baseline.get("hashes") or {}
    if not _baseline.get("scoped_to_imports"):
        return  # subtree-walk fallback already watches everything

    # O(1) FAST PATH. The docstring above calls the steady state "a
    # set-difference over sys.modules and nothing else" — true, and that
    # difference was still 2.8 ms per call with only 106 modules loaded,
    # because building it means a Path and a relative_to() for every okuro
    # module in sys.modules. This runs from post_tool_call on EVERY tool call,
    # so the whole cost was being paid to discover there was nothing to do.
    #
    # sys.modules only grows during normal operation, so an unchanged length
    # means no newcomer can exist and the walk cannot find one.
    #
    # RESIDUAL, stated rather than hidden: a delete plus an import between two
    # calls (importlib.reload, or `del sys.modules[...]`) nets zero change and
    # skips one top-up. It self-corrects — the newcomer is still absent from
    # `known`, so the next import that moves the length finds it — and the
    # check-time backstop still runs regardless.
    import sys as _sys

    n_modules = len(_sys.modules)
    if _baseline.get("modules_len") == n_modules:
        return
    _baseline["modules_len"] = n_modules

    try:
        newcomers = [
            fp for fp in _imported_okuro_files(root)
            if str(fp.relative_to(root)) not in known
        ]
    except (OSError, ValueError):
        return
    if not newcomers:
        return

    ignored = _baseline.get("ignored") or frozenset()
    added: dict[str, str] = {}
    for fp in newcomers:
        try:
            rel = str(fp.relative_to(root))
            if rel in ignored:
                continue
            digest = _hash_file(fp)
            if digest is not None:
                added[rel] = digest
        except (OSError, ValueError):
            continue
    if added:
        known.update(added)
        _baseline["watched_paths"] = frozenset(
            _baseline.get("watched_paths") or frozenset()
        ) | {root / rel for rel in added}


def note_imports() -> bool:
    """Baseline any module that loaded during the tool call that just ended.

    Call from ``post_tool_call``. This is the moment the drift baseline can be
    honest about lazily-imported code: the import happened microseconds ago
    inside the handler that just returned, and the agent has not been given
    control back, so the bytes on disk are still the bytes the interpreter
    compiled. See ``_top_up_baseline`` for what goes wrong when the same work
    is left to drift-check time instead.

    No-op until a baseline exists (``prime_baseline`` at server boot) — with no
    baseline there is nothing to top up, and priming later hashes all of
    sys.modules anyway.

    Best-effort and silent: a failure here must never break tool dispatch, and
    the check-time backstop still runs.

    Returns True if the top-up ran, for tests.
    """
    try:
        if not _baseline.get("root"):
            return False
        root = _repo_root()
        if root is None or _baseline["root"] != str(root):
            return False
        _top_up_baseline(root)
        return True
    except Exception:  # noqa: BLE001 — never break dispatch over bookkeeping
        return False


def _ensure_baseline(root: Path, started: float, git=None) -> None:
    """Record the content of every watched file, once per process.

    mtime alone cannot answer the question this module asks. ``touch`` moves
    mtime without changing a byte, and the interpreter's loaded code is then
    still identical to disk — but the gate this feeds was sticky, so
    believing mtime turned a no-op into a permanently refused session
    (measured 2026-07-19: a touch with an unchanged sha256 refused every
    tool for the life of the process, and reverting the mtime did not clear
    it). Hashing makes drift mean "disk differs from what we loaded".

    Built lazily on the first check, not at import: it costs one subtree
    walk (~250ms over the 1.9k watched files here, ~25ms of that hashing)
    and most processes never evaluate drift at all.

    Every watched file is HASHED, including one whose mtime is newer than
    ``started``. There used to be a ``predrift`` class for those — recorded as
    permanently dirty on the theory that the bytes on disk were already the new
    ones. It was the module's own bug: ``predrift`` seeds ``dirty`` on every
    later check and nothing can ever clear it, so a file merely *touched* while
    the process booted refused the session for its whole life. That is exactly
    the sticky-mtime failure the paragraph above says this design exists to
    prevent, reintroduced one level down.

    Hashing them instead is honest here because of WHEN priming runs: about
    0.3s after interpreter start and BEFORE the tool modules are imported (they
    load lazily at the first ``tools/list``). Bytes read at prime time are
    therefore at least as fresh as anything this process will go on to import.
    The residual exposure is a file edited inside that ~0.3s window, which is
    far smaller than the windows the gate is meant to catch — and unlike
    ``predrift`` it self-corrects, because a later real edit still changes the
    hash.
    """
    if _baseline["root"] == str(root):
        _top_up_baseline(root)
        return

    # Git-ignored artifacts (okuro's generated .okuro-index.yaml, etc.) are not
    # source and must never count as drift. Computed once here so the per-check
    # paths below can skip them cheaply.
    ignored = _ignored_watched(root, git) if git is not None else frozenset()

    # Prefer the IMPORT CLOSURE — only loaded code can make this process stale.
    # Falls back to the subtree walk if introspection yields nothing, so the
    # gate is never accidentally watching an empty set.
    imported = _imported_okuro_files(root)
    source = imported or _iter_watched(root / _WATCHED_SUBTREE)

    hashes: dict[str, str] = {}
    try:
        for fp in source:
            try:
                rel = str(fp.relative_to(root))
                if rel in ignored:
                    continue
                # No mtime shortcut — hash unconditionally. See the docstring:
                # the old `predrift` bucket was unrecoverable by construction.
                digest = _hash_file(fp)
                if digest is not None:
                    hashes[rel] = digest
            except (OSError, ValueError):
                continue
    except OSError:
        pass

    # `predrift` retained as an always-empty key so any external reader that
    # still indexes it keeps working instead of raising KeyError.
    # `watched_paths` / `scoped_to_imports` let the porcelain sweep below stay
    # inside the same scope this baseline used.
    # `modules_len` is reset, not carried: it is _top_up_baseline's O(1) guard
    # meaning "sys.modules has not grown since the last top-up against THIS
    # baseline". A rebuilt baseline has had no top-up, so a value inherited
    # from the previous root would skip the first one.
    _baseline.update(
        {"root": str(root), "hashes": hashes, "predrift": set(),
         "ignored": ignored,
         "watched_paths": frozenset(imported),
         "scoped_to_imports": bool(imported),
         "modules_len": None}
    )


def _worktree_drift(root: Path, started: float, git) -> list[str]:
    """Watched files whose bytes differ from what this process loaded.

    Two discovery paths, because neither alone is complete:
      - the content baseline, stat-swept (~4ms for 1.9k files). Covers every
        file that existed at boot INCLUDING those git reports as clean — the
        porcelain-only design was structurally blind to that set.
      - ``git status --porcelain``, for paths created after the baseline was
        taken, which by construction cannot be in it.

    A candidate is drift only when its hash differs from the baseline, so a
    no-op touch, or an edit reverted to the loaded bytes, reads as clean.
    """
    _ensure_baseline(root, started, git)
    hashes: dict[str, str] = _baseline["hashes"]
    dirty: set[str] = set(_baseline["predrift"])
    ignored: frozenset = _baseline["ignored"]

    # Path 1 — known files whose mtime moved since boot. The stat is cheap;
    # only the handful that actually moved pay for a hash.
    for rel, digest in hashes.items():
        fp = root / rel
        try:
            if fp.stat().st_mtime <= started:
                continue
        except OSError:
            # Deleted after boot — the loaded module no longer exists on disk.
            dirty.add(rel)
            continue
        if _hash_file(fp) != digest:
            dirty.add(rel)

    # Path 2 — paths git knows about that the baseline does not.
    out = git("status", "--porcelain", "--", _WATCHED_SUBTREE)
    for line in (out or "").splitlines():
        # Split on the status field rather than slicing a fixed offset. A
        # porcelain line is "XY path", but an unstaged edit renders as
        # " M path" and the caller strips the line, so the path does NOT
        # sit at a constant column. Slicing [3:] silently mangled every
        # modified file into a non-existent path while untracked entries
        # ("?? path", two non-space chars) came through fine.
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        entry = parts[1]
        # Renames render as "old -> new"; the new path is the live one.
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        # Paths with spaces or non-ASCII come back quoted.
        entry = entry.strip().strip('"')
        if not entry:
            continue
        try:
            for fp in _iter_watched(root / entry):
                rel = str(fp.relative_to(root))
                # Git-ignored generated artifacts are not source drift.
                # (porcelain hides ignored files by default, but be explicit.)
                if rel in ignored:
                    continue
                # Baselined paths were already judged on content above;
                # re-judging them here on mtime would reinstate the bug.
                if rel in hashes or rel in dirty:
                    continue
                # A file git reports but that this process never imported
                # cannot have made it stale. Without this, the porcelain sweep
                # re-widens the gate to the whole subtree and cancels the
                # import-closure narrowing done in _ensure_baseline. A NEW file
                # (not yet imported anywhere) is by definition not loaded here.
                if _baseline.get("scoped_to_imports") and fp not in _baseline.get(
                    "watched_paths", frozenset()
                ):
                    continue
                if fp.stat().st_mtime > started:
                    dirty.add(rel)
        except (OSError, ValueError):
            continue
    return sorted(dirty)


def _git_runner(root: Path):
    """A git callable bound to ``root``: stripped stdout, or None on any
    failure. Shared by prime_baseline and detect_code_drift so both talk to git
    identically."""
    def _git(*args: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=root,
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None
    return _git


def prime_baseline() -> bool:
    """Snapshot watched-file content at process start — call once, at server boot.

    Drift is then decided on content alone (current src/okuro bytes vs what this
    process loaded), with no date-based commit proxy. Without priming, the
    baseline is captured lazily on the first check — which cannot honestly
    represent boot-time code — so detect_code_drift falls back to the coarse
    "any commit after boot" rule that fires on docs-only, tests-only, no-op and
    already-loaded commits. Priming removes every one of those false positives.

    Idempotent (root-match guard in _ensure_baseline). Best-effort: returns True
    when the boot baseline is in place, and never raises into server startup.
    Cost: one subtree walk (~250ms) at boot.
    """
    root = _repo_root()
    started = _process_start()
    if root is None or started is None:
        return False
    try:
        _ensure_baseline(root, started, _git_runner(root))
        _baseline["primed_at_boot"] = True
        return True
    except Exception:  # noqa: BLE001 — priming must never break server startup
        return False


def _drift_result(started: float, git, dirty: list, behind: int, subjects: list,
                  since: Optional[str] = None) -> dict:
    """Assemble the drift dict. ``loaded`` is the sha this process booted on
    (via --until=boot) when we can date it, else current HEAD."""
    loaded = (git("log", f"--until={since}", "-1", "--pretty=%h")
              if since else None) or git("rev-parse", "--short", "HEAD") or "unknown"
    return {
        "behind": behind,
        "booted": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M"),
        "head": git("rev-parse", "--short", "HEAD") or "unknown",
        "loaded": loaded,
        "subjects": subjects,
        "dirty": dirty,
        "dirty_count": len(dirty),
    }


def detect_code_drift() -> Optional[dict]:
    """Code this process cannot have loaded, or None when in sync.

    ``dirty`` (list[str]) — watched files under src/okuro whose current bytes
    differ from what this process loaded. With a boot-primed baseline
    (prime_baseline, called from the server factory) this is the WHOLE story: a
    post-boot commit still leaves the file with mtime > boot and a hash != the
    loaded baseline, so committed and uncommitted drift are caught alike, and a
    commit that changes no watched bytes — docs, tests, no-op, already-loaded —
    is correctly clean.

    Fallback path (baseline NOT primed at boot): the lazy baseline is blind to
    code that was committed and then made clean, so the old date-based
    ``behind`` proxy is retained as the backstop it was designed to be.

    Also returns ``booted``, ``head`` and ``loaded``. Returns None only when the
    active surface is clean, so callers that treat a dict as "stale" stay correct.
    """
    root = _repo_root()
    started = _process_start()
    if root is None or started is None:
        return None

    git = _git_runner(root)

    # Content drift — computed once, authoritative in both paths.
    try:
        dirty = _worktree_drift(root, started, git)
    except Exception:  # noqa: BLE001 — silent beats fatal in a guard
        dirty = []

    primed = _baseline.get("primed_at_boot") and _baseline.get("root") == str(root)

    if primed:
        # Loaded bytes are honestly recorded, so content is the entire question.
        if not dirty:
            return None
        return _drift_result(started, git, dirty, behind=0, subjects=[])

    # Fallback — no boot baseline. Retain the date-based commit backstop.
    # Explicit UTC ISO instant: locale-formatted timestamps silently misparse
    # (a German "Mi Jul 15" defeated git date parsing while diagnosing this very
    # bug), and an unparseable date makes git return everything → fire forever.
    since = datetime.fromtimestamp(started, timezone.utc).isoformat()
    log = git("log", f"--since={since}", "--pretty=%h %s")
    if log is None:
        # git unavailable — content drift still stands on its own.
        return _drift_result(started, git, dirty, behind=0, subjects=[]) if dirty else None

    lines = [ln for ln in log.splitlines() if ln.strip()]
    if not lines and not dirty:
        return None
    return _drift_result(
        started, git, dirty, behind=len(lines),
        subjects=[ln.split(" ", 1)[-1] for ln in lines[:_MAX_SUBJECTS]],
        since=since,
    )


_loaded_version_cache: dict = {"resolved": False, "value": None}


def loaded_code_version() -> Optional[str]:
    """Short git SHA of the commit this process booted on, or None.

    Stamped onto rows this process writes (see migration 104) so a later
    measurement can tell which interpreter produced them. Deliberately NOT
    ``okuro.__version__``: that is a static release string and cannot
    distinguish a server 6 commits behind from one at HEAD, which is the
    staleness this codebase actually suffers from.

    Uses the same commit-date proxy as ``detect_code_drift``: the last commit
    at or before process start is the newest one this interpreter can have
    loaded. Resolved once — a process cannot change the code it booted on.
    """
    if _loaded_version_cache["resolved"]:
        return _loaded_version_cache["value"]

    value = None
    root = _repo_root()
    started = _process_start()
    if root is not None and started is not None:
        since = datetime.fromtimestamp(started, timezone.utc).isoformat()
        try:
            proc = subprocess.run(
                ["git", "log", f"--until={since}", "-1", "--pretty=%h"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                value = proc.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            value = None

    _loaded_version_cache.update({"resolved": True, "value": value})
    return value


def strict_freshness_enabled() -> bool:
    """True when ~/.okuro/config.yaml has `dev.strict_freshness: true`.

    Gates the hard staleness reject in the MCP middleware. Deliberately
    mirrors `restart_guard.dev_restart_enabled` rather than reading OKURO_DEV:
    OKURO_DEV is read once at import and travels by env inheritance to every
    child, so it could not be turned off in a running server — which is the
    exact class of problem this module exists to fix. A file is read fresh,
    can be flipped without a restart, and can be read back.

    Opt-in and absent by default. On a box doing real work, refusing every
    tool call because a commit landed is hostile; on a dev box editing okuro
    hourly, it is the only thing that stops an agent testing old code.

    CACHED ON (mtime, size), NOT on first read. The live-flip property above is
    the whole reason this reads a file instead of an env var, so a plain
    lru_cache would delete the feature. But the uncached version read AND
    yaml-parsed the file on every non-allowlisted tool call — measured 2.269 ms
    each, paid by every session whether the flag is set or not. Re-parsing only
    when the file's stat changes keeps the flip observable within one tool call
    and drops the steady-state cost to a single stat.
    """
    path = okuro_home() / "config.yaml"
    try:
        st = path.stat()
        stamp = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        # Missing or unreadable — the answer is False, and cache that too so a
        # box with no config.yaml does not stat-and-miss on every call.
        _strict_freshness_cache.update({"stamp": None, "value": False})
        return False

    if _strict_freshness_cache.get("stamp") == stamp:
        return _strict_freshness_cache["value"]

    value = False
    try:
        import yaml

        data = yaml.safe_load(path.read_text())
        if isinstance(data, dict):
            dev = data.get("dev")
            if isinstance(dev, dict):
                value = dev.get("strict_freshness") is True
    except Exception:  # noqa: BLE001 — an unreadable config means "no"
        value = False

    _strict_freshness_cache.update({"stamp": stamp, "value": value})
    return value


#: (mtime_ns, size) of the config.yaml the answer above was parsed from, so a
#: hand-edit is picked up on the next tool call without re-parsing on every one.
#: `stamp=None` caches the "no config file" answer.
_strict_freshness_cache: dict = {"stamp": 0, "value": False}


# Cache for the per-tool-call gate.
#
# Sticky for COMMITS only. A landed commit cannot un-land, so re-checking
# can never change that verdict and caching it costs one git call ever.
#
# Worktree drift is NOT sticky, because it is genuinely reversible: revert a
# file and the interpreter's loaded bytes match disk again. Making it sticky
# meant one stray edit — or a touch that changed nothing — refused every tool
# for the life of the process, with no recovery short of respawning the
# server and re-bootstrapping. Measured 2026-07-19.
_FRESH_TTL_SECONDS = 5.0
_drift_cache: dict = {"checked_at": 0.0, "drift": None, "sticky": False}


def cached_drift() -> Optional[dict]:
    """`detect_code_drift()` memoised for use on every tool call.

    Sticky once commits have landed — see `_drift_cache`. Never raises: a
    guard that can fail the call it guards is worse than no guard.
    """
    import time

    if _drift_cache["sticky"]:
        return _drift_cache["drift"]

    now = time.monotonic()
    if now - _drift_cache["checked_at"] < _FRESH_TTL_SECONDS:
        return _drift_cache["drift"]

    try:
        drift = detect_code_drift()
    except Exception:  # noqa: BLE001
        drift = None

    _drift_cache["drift"] = drift
    _drift_cache["checked_at"] = now
    if drift is not None and drift.get("behind"):
        _drift_cache["sticky"] = True
    return drift


def format_drift_warning(drift: dict) -> str:
    """Render the bootstrap-packet warning block for detected drift."""
    n = drift["behind"]
    dirty = drift.get("dirty") or []

    lines = [
        f"## STALE CODE — this session is running okuro from {drift['booted']}",
        "",
    ]

    if n:
        plural = "commit" if n == 1 else "commits"
        lines += [
            f"**{n} {plural}** landed after this MCP server booted. This process "
            f"cannot have loaded any of them: it is running `{drift['loaded']}`, "
            f"the repo is at `{drift['head']}`.",
            "",
        ]
    if dirty:
        d = len(dirty)
        plural = "file" if d == 1 else "files"
        lines += [
            f"**{d} uncommitted {plural}** under `{_WATCHED_SUBTREE}` changed on "
            f"disk after this server booted. The running interpreter holds the "
            f"older version — editing a file does not reload it.",
            "",
        ]

    lines += [
        "Python imports once at startup and MCP tool descriptions are sent at "
        "handshake — so this session's okuro code *and* its tool behaviour are "
        "frozen at boot. Nothing hot-reloads. Tools may behave differently here "
        "than their source on disk says, and differently than in a session "
        "started later.",
        "",
        "**Fix: restart this session.** Restarting the okuro daemon does not "
        "help — this server is a child of your client.",
    ]

    if n:
        lines += [
            "",
            f"Missed most recently ({min(n, _MAX_SUBJECTS)} of {n}):",
        ]
        lines += [f"  - {s}" for s in drift["subjects"]]
    if dirty:
        lines += [
            "",
            f"Edited on disk ({min(len(dirty), _MAX_DIRTY)} of {len(dirty)}):",
        ]
        lines += [f"  - {p}" for p in dirty[:_MAX_DIRTY]]
    return "\n".join(lines)
