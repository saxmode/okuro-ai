# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Launch/stop/inspect preview processes via systemd-user transient units.
# index:
#   imports
#   class LauncherError
#   class PreviewState
#   def status
#   def start
#   def start_background
#   def stop
#   def logs_tail
#   def _runtime_path
#   def _read_runtime
#   def _write_runtime
#   def _unit_name
#   def _is_active
#   def _pick_port
#   def _build_if_needed
#   def _ready_check
#   def _journal_tail
#   def _build_log_path
#   def _append_build_log
# AGENT_HEADER_END -->
"""Preview launcher.

The launcher is the single source of truth for preview lifecycle. Both
the REST router and the MCP tools call into this module — they own no
state of their own.

Process model:
  * Each preview is a transient ``systemd --user`` unit named
    ``tm-preview-{slug}``.
  * Started with ``systemd-run --user --collect``; killed via
    ``systemctl --user stop``. The ``--collect`` flag means failed units
    auto-clear so a second start doesn't trip on residue.
  * State that must survive an okuro restart lives in
    ``{task_dir}/preview.runtime.json``. We re-attach to a still-running
    unit by name on next ``status()`` call.

S1 explicitly **does not** implement:
  * TTL idle reaper (needs frontend heartbeat plumbing — S2).
  * Auto-detect / propose-recipe (S3).
  * Inline-iframe / signed file URLs for kind=doc/image (S2).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

from okuro.orchestrator.preview.proposer import (
    KNOWN_TEMPLATES,
    ProposerMiss,
    propose,
)
from okuro.orchestrator.preview.recipe import Recipe, RecipeError, load_recipe

logger = logging.getLogger("okuro.orchestrator.preview.launcher")

UNIT_PREFIX = "tm-preview-"
DEFAULT_PORT_RANGE = (3000, 3099)
RUNTIME_FILE = "preview.runtime.json"
BUILD_CACHE_FILE = "preview.build_cache.json"
# Single human-readable log written by the launcher itself: build subprocess
# output (npm ci, vite build, nbconvert, ffmpeg, …) plus phase markers
# (▸ install / ▸ build / ▸ serve / ▸ ready / ✗ failed). Concatenated into
# the journal tail by ``logs_tail`` so callers see one merged stream
# regardless of recipe kind. Bounded by truncation on each new start.
BUILD_LOG_FILE = "preview.build.log"

# Orchestrator task directory naming convention shared across orchestrators
# (this codebase, its predecessor, and any future ones). Used by suggest_project_paths
# to reject bookkeeping dirs of any orchestrator's tasks, not just our own.
import re as _re
from okuro.orchestrator.yamlfast import yload
from okuro.db.engine import okuro_home
_TASK_SLUG_RE = _re.compile(r"^task-\d{8}-\d{6}$")

# Build markers: filenames whose presence in a directory indicates it is
# a runnable/buildable project root rather than a source subdir. Used by
# ``suggest_project_paths`` to rank candidates. The lists are kept here
# (not split per-ecosystem) so the ranking function stays a single pass.
_BUILD_MARKERS: tuple[str, ...] = (
    # Language ecosystems
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "build.gradle", "pom.xml", "Gemfile", "requirements.txt",
    "setup.py", "Pipfile",
    # Container / orchestration
    "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml", "Dockerfile",
    # Vanilla web / generic build entrypoints
    "index.html", "serve.py", "Makefile", "makefile",
)


def suggest_project_paths(task_dir: Path, *, top_n: int = 3) -> list[dict]:
    """Rank candidate ``project_path`` values for a task and return the
    top ``top_n`` for the UI's project-path picker.

    NOT called automatically from any preview start path — this is a
    user-facing suggestion endpoint. Auto-resolution of ``project_path``
    is now declarative (engine reads role-handover briefs); this function
    exists only so the UI can offer "did you mean X / Y / Z?" when a
    legacy task has no declared path.

    Each result is a dict::

        {
            "path": str,            # absolute, exists+is_dir
            "hits": int,            # frequency in task signals
            "build_markers": list,  # marker filenames present at path
            "has_git": bool,        # .git dir present
        }

    Ranking: ``(build_markers > 0, hits, path length)`` — same biases
    as the previous heuristic, but the result is a SHORTLIST exposed
    to the user, never silently picked.
    """
    import re
    from collections import Counter

    blob = _gather_path_blob(task_dir)
    if not blob:
        return []

    pattern = re.compile(r"(?:/Users/|/home/|/mnt/|/media/|/opt/|/tmp/|/var/)[\w./@+-]+")
    okuro_data = okuro_home().resolve()
    noise_parts = {".venv", "venv", "node_modules", "__pycache__",
                   ".cache", "site-packages", ".git", ".tox", ".mypy_cache"}
    shallow_roots = {"/", "/home", "/Users", "/tmp", "/var", "/opt",
                     "/mnt", "/media"}
    home = Path.home().resolve()
    task_resolved = task_dir.resolve()

    counts: Counter[str] = Counter()
    for raw in pattern.findall(blob):
        clean = raw.rstrip(".,;:)`'\"")
        p = Path(clean)
        for _ in range(8):
            if p.exists() and p.is_dir():
                break
            if str(p) in ("/", ".", ""):
                p = None  # type: ignore[assignment]
                break
            p = p.parent
        if p is None or not p.exists() or not p.is_dir():
            continue
        try:
            resolved = p.resolve()
        except Exception:  # noqa: BLE001
            continue
        rs = str(resolved)
        if rs.startswith(str(task_resolved)):
            continue
        if rs == str(okuro_data) or rs.startswith(str(okuro_data) + "/"):
            continue
        if any(part in noise_parts for part in resolved.parts):
            continue
        if any(_TASK_SLUG_RE.match(part) for part in resolved.parts):
            continue
        if rs in shallow_roots:
            continue
        if resolved == home:
            continue
        counts[rs] += 1

    if not counts:
        return []

    def _markers(path: str) -> list[str]:
        return [m for m in _BUILD_MARKERS if (Path(path) / m).exists()]

    ranked = sorted(
        counts.items(),
        key=lambda kv: (
            1 if _markers(kv[0]) else 0,
            kv[1],
            len(kv[0]),
        ),
        reverse=True,
    )

    out: list[dict] = []
    for path_str, hits in ranked[:max(top_n, 0)]:
        markers = _markers(path_str)
        out.append({
            "path": path_str,
            "hits": int(hits),
            "build_markers": markers,
            "has_git": (Path(path_str) / ".git").is_dir(),
        })
    return out


def _gather_path_blob(task_dir: Path) -> str:
    """Concatenate every text source under ``task_dir`` that might
    contain absolute paths — input for ``suggest_project_paths``.

    Sources: ``task.yaml.description``, ``plan.yaml`` subtask
    descriptions/output_summaries, and ``.activity.jsonl``. Failure-
    tolerant: missing/corrupt sources contribute nothing.
    """

    chunks: list[str] = []

    task_yaml = task_dir / "task.yaml"
    if task_yaml.exists():
        try:
            td = yload(task_yaml.read_text()) or {}
            desc = td.get("description")
            if isinstance(desc, str):
                chunks.append(desc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read %s: %s", task_yaml, exc)

    plan_path = task_dir / "plan.yaml"
    if plan_path.exists():
        try:
            plan = yload(plan_path.read_text()) or {}
            for phase in plan.get("phases", []) or []:
                for st in phase.get("subtasks", []) or []:
                    for key in ("description", "output_summary"):
                        v = st.get(key)
                        if isinstance(v, str):
                            chunks.append(v)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read %s: %s", plan_path, exc)

    activity_path = task_dir / ".activity.jsonl"
    if activity_path.exists():
        try:
            chunks.append(activity_path.read_text(errors="ignore"))
        except OSError as exc:
            logger.warning("could not read %s: %s", activity_path, exc)

    return "\n".join(chunks)


def _read_task_project_path(task_dir: Path) -> Optional[Path]:
    """Pull the ``project_path`` field from ``task.yaml`` if set.

    Re-exports the helper from ``recipe.py`` so launcher-only callers
    don't need to know which module owns it. Missing file / missing
    field both return None.
    """
    from okuro.orchestrator.preview.recipe import (
        _read_task_project_path as _impl,
    )
    return _impl(task_dir)


class LauncherError(RuntimeError):
    """Raised when the launcher cannot complete the requested action."""


@dataclasses.dataclass
class PreviewState:
    """Current state of a preview, as reported to callers."""

    state: str  # idle | building | ready | failed | stopped
    slug: str
    unit: Optional[str] = None
    port: Optional[int] = None
    url: Optional[str] = None
    started_at: Optional[str] = None
    recipe_present: bool = False
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ── Public API ──────────────────────────────────────────────────────


def status(task_dir: Path, slug_default: str) -> PreviewState:
    """Report current state without side-effects.

    Reads the on-disk runtime.json (if any), checks the systemd unit's
    actual state, and reconciles. A unit that died externally will be
    reported as ``failed`` with the last error from runtime.json (or
    ``stopped`` if the runtime file says it was stopped intentionally).
    """
    try:
        recipe = load_recipe(task_dir, slug_default)
        recipe_present = True
        slug = recipe.slug
    except Exception:
        recipe = None
        recipe_present = False
        slug = slug_default

    # External recipes don't use systemd. Reachability is the truth.
    if recipe is not None and recipe.kind == "external" and recipe.url:
        runtime = _read_runtime(task_dir) or {}
        if _http_ping(recipe.url, timeout_s=2.0):
            return PreviewState(
                state="ready", slug=slug, url=recipe.url,
                started_at=runtime.get("started_at"), recipe_present=True,
            )
        return PreviewState(
            state=runtime.get("state", "idle"), slug=slug, url=recipe.url,
            recipe_present=True, last_error=runtime.get("last_error"),
        )

    # Non-serve recipes (kind=image/doc/download) are static artifacts:
    # no supervisor needed, no port to bind, no systemd unit. Trust the
    # runtime cache. WITHOUT this short-circuit, hosts without systemd
    # (macOS) reported idle for these too, causing the SPA's PreviewButton
    # to flicker — setQueryData after a successful start briefly showed
    # ready, then the next /status refetch hit the systemctl gate below
    # and reverted to idle on every poll.
    if recipe is not None and not recipe.expects_serve():
        runtime = _read_runtime(task_dir) or {}
        cached_state = runtime.get("state")
        if cached_state == "ready":
            return PreviewState(
                state="ready", slug=slug, recipe_present=True,
                started_at=runtime.get("started_at"),
            )
        return PreviewState(
            state=cached_state or "idle", slug=slug, recipe_present=True,
            last_error=runtime.get("last_error"),
        )

    if shutil.which("systemctl") is None:
        # Non-systemd host. Only reached for serve-kind recipes that
        # actually need systemd — static artifact recipes already handled
        # above. Serve recipes still won't work on macOS (systemd-run /
        # systemctl --user not available); that's a separate, larger fix.
        return PreviewState(state="idle", slug=slug_default, recipe_present=_has_recipe(task_dir),
                            last_error="systemctl not found on PATH")

    runtime = _read_runtime(task_dir)
    unit = _unit_name(slug)
    active = _is_active(unit)

    if runtime and runtime.get("unit") == unit and active:
        return PreviewState(
            state="ready",
            slug=slug,
            unit=unit,
            port=runtime.get("port"),
            url=runtime.get("url"),
            started_at=runtime.get("started_at"),
            recipe_present=recipe_present,
        )

    if runtime and not active:
        # Unit died or was stopped. Report whichever the runtime says.
        return PreviewState(
            state=runtime.get("state", "stopped"),
            slug=slug,
            unit=unit,
            port=runtime.get("port"),
            url=runtime.get("url"),
            started_at=runtime.get("started_at"),
            recipe_present=recipe_present,
            last_error=runtime.get("last_error"),
        )

    return PreviewState(state="idle", slug=slug, recipe_present=recipe_present)


# Callback type for live state pushes. The launcher invokes it on every
# transition (building → starting → ready/failed) so the API layer can
# broadcast a WS preview_state_changed event without having to poll.
# None disables push (preserves the original sync API).
StateCallback = Optional[callable]  # type: ignore[valid-type]


def _emit_state(
    on_state: "Optional[callable]",  # type: ignore[name-defined]
    state: "PreviewState",
) -> None:
    """Best-effort callback: never let a callback failure break the launch."""
    if on_state is None:
        return
    try:
        on_state(state)
    except Exception as exc:  # noqa: BLE001 — caller's bug, not ours
        logger.warning("preview state callback raised: %s", exc)


def start(
    task_dir: Path,
    slug_default: str,
    *,
    force_rebuild: bool = False,
    auto_detect: bool = True,
    on_state=None,
) -> PreviewState:
    """Build (if needed) → serve → ready-check.

    Idempotent: if the unit is already active, returns the cached URL
    without relaunching. To rebuild, call ``stop()`` then ``start()``.

    With ``auto_detect=True`` (default), if no preview.yaml exists the
    proposer is invoked. On a successful match the proposed recipe is
    written to ``{task_dir}/preview.yaml`` and the start proceeds normally.
    On a proposer miss the original ``RecipeError`` is re-raised, augmented
    with a ``known_templates`` attribute the API layer can surface to the user.
    """
    # Truncate the build log at the start of every fresh launch so the
    # drawer doesn't show stale output from the previous run. The file is
    # appended to as the build progresses (see ``_build_if_needed`` and
    # ``_append_build_log``); we only reset it here, never mid-run.
    _build_log_path(task_dir).write_text("")
    # Regenerate the recipe from the CURRENT artifact set before resolving
    # it, so an extended task opens its newest deliverable rather than the
    # file frozen at first discovery. Gated by auto_detect: callers that
    # opt out (e.g. the background runner, which already rediscovered in
    # start_background) keep the resolved recipe. No-op on proposer miss.
    if auto_detect:
        _rediscover_recipe(task_dir, slug_default)
    _append_build_log(task_dir, "▸ resolving recipe")
    try:
        recipe = load_recipe(task_dir, slug_default)
    except RecipeError as exc:
        if not auto_detect:
            raise
        recipe = _autoproposed_recipe(task_dir, slug_default, original_error=exc)
    _append_build_log(task_dir, f"  recipe kind={recipe.kind} cwd={recipe.cwd}")

    # External recipes (e.g. docker-compose-managed services) don't
    # touch systemd at all. The service owns its own runtime; we only
    # verify it's reachable and, when we know the compose file, we'll
    # `docker compose up -d` if it isn't.
    if recipe.kind == "external":
        _emit_state(on_state, PreviewState(
            state="starting", slug=recipe.slug, url=recipe.url,
            recipe_present=True,
        ))
        result = _start_external(task_dir, recipe)
        _emit_state(on_state, result)
        return result

    unit = _unit_name(recipe.slug)

    # Idempotency
    if _is_active(unit):
        runtime = _read_runtime(task_dir) or {}
        return PreviewState(
            state="ready",
            slug=recipe.slug,
            unit=unit,
            port=runtime.get("port"),
            url=runtime.get("url"),
            started_at=runtime.get("started_at"),
            recipe_present=True,
        )

    # Mark "building" before the long-running build subprocess so SSE
    # log tailers and /status callers see progress. The runtime row is
    # the cross-thread source of truth: status() reads it on every poll.
    _write_runtime(task_dir, {
        "state": "building", "unit": unit, "slug": recipe.slug,
        "kind": recipe.kind,
    })
    _emit_state(on_state, PreviewState(
        state="building", slug=recipe.slug, unit=unit, recipe_present=True,
    ))

    # Build (sync, blocking — but stdout/stderr stream into preview.build.log
    # via _build_if_needed so the UI can tail progress live).
    try:
        _build_if_needed(task_dir, recipe, force=force_rebuild)
    except Exception as exc:
        _append_build_log(task_dir, f"✗ build failed: {exc}")
        _write_runtime(task_dir, {
            "state": "failed", "unit": unit, "slug": recipe.slug,
            "last_error": f"build failed: {exc}",
        })
        _emit_state(on_state, PreviewState(
            state="failed", slug=recipe.slug, unit=unit, recipe_present=True,
            last_error=f"build failed: {exc}",
        ))
        raise LauncherError(f"build failed: {exc}") from exc

    if not recipe.expects_serve():
        # kind=doc/image/audio/video/slides/notebook/download — no serve.
        # Caller serves the file directly via the REST file endpoint.
        _append_build_log(task_dir, f"▸ ready (file: {recipe.file})")
        _write_runtime(task_dir, {
            "state": "ready", "unit": None, "slug": recipe.slug,
            "file": recipe.file, "url": None, "kind": recipe.kind,
        })
        ready = PreviewState(state="ready", slug=recipe.slug, recipe_present=True)
        _emit_state(on_state, ready)
        return ready

    assert recipe.serve is not None
    port = _pick_port(recipe.serve.port)

    # Materialise {port} placeholders in cmd + url. Default host comes from
    # the user's LAN domain convention; localhost when none is configured.
    from okuro.yu.conventions import get_convention

    cmd = [_interp(part, port) for part in recipe.serve.cmd]
    default_host = get_convention("host.domain") or "localhost"
    url = _interp(recipe.url or f"http://{default_host}:{{port}}/", port)

    # Build systemd-run command
    sd_cmd = [
        "systemd-run", "--user",
        f"--unit={unit}",
        f"--working-directory={recipe.cwd}",
        "--collect",
        "--quiet",
    ]
    for k, v in recipe.serve.env.items():
        sd_cmd.append(f"--setenv={k}={_interp(v, port)}")
    sd_cmd.append(f"--setenv=PORT={port}")
    sd_cmd.append("--")
    sd_cmd.extend(cmd)

    proc = subprocess.run(sd_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        _write_runtime(task_dir, {
            "state": "failed", "unit": unit, "slug": recipe.slug,
            "last_error": f"systemd-run failed: {msg}",
        })
        raise LauncherError(f"systemd-run failed: {msg}")

    started_at = datetime.now(timezone.utc).isoformat()
    runtime = {
        "state": "starting", "unit": unit, "slug": recipe.slug,
        "port": port, "url": url, "started_at": started_at,
        "kind": recipe.kind,
    }
    _write_runtime(task_dir, runtime)
    _append_build_log(task_dir, f"▸ serving on port {port} → {url}")
    _emit_state(on_state, PreviewState(
        state="starting", slug=recipe.slug, unit=unit, port=port, url=url,
        started_at=started_at, recipe_present=True,
    ))

    # Ready check
    ready = recipe.serve.ready
    if not _ready_check(url, ready.timeout_s, ready.status_lt, ready.http):
        # Capture last log lines for the error report.
        tail = "\n".join(_journal_tail(unit, lines=40))
        # Stop the unit so we don't leak a half-broken process.
        try:
            subprocess.run(["systemctl", "--user", "stop", unit],
                           capture_output=True, text=True, check=False)
        except Exception:
            pass
        err = f"ready check timed out\n{tail}"
        _append_build_log(task_dir, f"✗ {err}")
        runtime.update({"state": "failed", "last_error": err})
        _write_runtime(task_dir, runtime)
        _emit_state(on_state, PreviewState(
            state="failed", slug=recipe.slug, unit=unit, port=port, url=url,
            started_at=started_at, recipe_present=True, last_error=err,
        ))
        raise LauncherError(f"ready check timed out for {url}\n--- last log lines ---\n{tail}")

    runtime["state"] = "ready"
    _write_runtime(task_dir, runtime)
    _append_build_log(task_dir, "✓ ready")
    final = PreviewState(
        state="ready", slug=recipe.slug, unit=unit,
        port=port, url=url, started_at=started_at, recipe_present=True,
    )
    _emit_state(on_state, final)
    return final


def start_background(
    task_dir: Path,
    slug_default: str,
    *,
    force_rebuild: bool = False,
    auto_detect: bool = True,
    on_state=None,
) -> PreviewState:
    """Spawn ``start()`` in a daemon thread; return ``state="building"`` immediately.

    The HTTP layer uses this so ``POST /api/preview/{id}/start`` returns
    in <100 ms instead of blocking for the full build (npm ci can take
    minutes; the SPA's preview button gets stuck on "Starting…" the whole
    time when start is sync). The thread writes runtime.json transitions
    that subsequent ``status()`` calls reflect, and emits ``on_state``
    callbacks for live WS pushes — so even without polling the SPA sees
    every state change.

    The MCP entrypoint keeps using sync ``start()`` because MCP callers
    intentionally block until ready (the contract is "give me the URL").

    On a recipe-resolution error we still raise synchronously — that's
    a 4xx the caller needs in the response body, not a background async
    failure.
    """
    import threading

    # Rediscover the recipe from the current artifact set BEFORE the
    # pre-flight load so the HTTP path (POST /start → here) opens the
    # task's newest deliverable, not the one frozen at first discovery.
    # Must run before load_recipe below — and the background start() is
    # called with auto_detect=False so it won't rediscover a second time.
    if auto_detect:
        _rediscover_recipe(task_dir, slug_default)

    # Pre-flight: validate recipe synchronously so a bad task config
    # surfaces as a 409, not a silent background failure. The recipe
    # validation in start() is cheap; we duplicate it just to fail fast
    # before promising the caller "building".
    try:
        recipe = load_recipe(task_dir, slug_default)
    except RecipeError as exc:
        if not auto_detect:
            raise
        recipe = _autoproposed_recipe(task_dir, slug_default, original_error=exc)

    unit = _unit_name(recipe.slug)
    # Write the initial state row so the caller's first /status poll
    # already shows "building" (the thread may not have ticked by the
    # time /status is called). The build log is truncated and seeded
    # by ``start()`` itself once the thread runs — keeping log
    # lifecycle in one place avoids the double-truncate where the
    # thread wipes the marker the foreground just wrote.
    initial = {
        "state": "building", "unit": unit, "slug": recipe.slug,
        "kind": recipe.kind,
    }
    if recipe.url:
        initial["url"] = recipe.url
    _write_runtime(task_dir, initial)
    pre_state = PreviewState(
        state="building", slug=recipe.slug, unit=unit,
        url=recipe.url, recipe_present=True,
    )
    _emit_state(on_state, pre_state)

    def _runner() -> None:
        try:
            start(
                task_dir, slug_default,
                force_rebuild=force_rebuild,
                auto_detect=False,  # already validated above
                on_state=on_state,
            )
        except Exception as exc:  # noqa: BLE001 — terminal log + state are already written
            logger.warning("preview background start failed for %s: %s", recipe.slug, exc)

    threading.Thread(target=_runner, name=f"tm-preview-{recipe.slug}", daemon=True).start()
    return pre_state


def stop(task_dir: Path, slug_default: str) -> PreviewState:
    """Stop the unit if running. Always succeeds; idempotent.

    For ``kind=external`` recipes there is nothing for us to stop —
    the user (or compose) owns the lifecycle. We just clear runtime
    state so the UI reflects "stopped".
    """
    # external recipes: clear runtime, never touch systemd or docker.
    try:
        recipe = load_recipe(task_dir, slug_default)
    except RecipeError:
        recipe = None
    if recipe is not None and recipe.kind == "external":
        _write_runtime(task_dir, {
            "state": "stopped", "unit": None, "slug": recipe.slug,
            "url": recipe.url, "port": None,
        })
        return PreviewState(
            state="stopped", slug=recipe.slug, url=recipe.url,
            recipe_present=True,
        )

    runtime = _read_runtime(task_dir) or {}
    slug = runtime.get("slug") or slug_default
    unit = runtime.get("unit") or _unit_name(slug)

    if _is_active(unit):
        proc = subprocess.run(["systemctl", "--user", "stop", unit],
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise LauncherError(f"systemctl stop failed: {err}")

    runtime.update({"state": "stopped", "unit": unit, "slug": slug})
    _write_runtime(task_dir, runtime)
    return PreviewState(
        state="stopped", slug=slug, unit=unit,
        port=runtime.get("port"), url=runtime.get("url"),
        started_at=runtime.get("started_at"),
        recipe_present=_has_recipe(task_dir),
    )


def propose_recipe(task_dir: Path, slug_default: str) -> dict:
    """Run the proposer and return the YAML body **without saving**.

    The API layer uses this for ``GET /recipe/proposal`` so the user can
    review a draft before saving. Saving happens via ``PUT /recipe`` or
    is implicit when ``start()`` runs with ``auto_detect=True``.

    Raises ``ProposerMiss`` (untouched) so the caller can return its
    structured diagnostic info.
    """
    proposal = propose(
        task_dir, slug=slug_default,
        project_path=_read_task_project_path(task_dir),
    )
    return {
        "template": proposal.template,
        "confidence": proposal.confidence,
        "reason": proposal.reason,
        "cwd": str(proposal.cwd),
        "recipe": proposal.body,
    }


def save_recipe(task_dir: Path, body: dict) -> Path:
    """Persist a recipe body next to where ``load_recipe`` will look.

    When ``task.project_path`` is set, writes to
    ``{project_path}/.okuro/preview.yaml``; otherwise writes the legacy
    ``{task_dir}/preview.yaml``. The target directory is created if
    missing. Validation is delegated to ``load_recipe`` after the
    write — if the body is malformed, the exception surfaces to the
    caller and the invalid file is removed so subsequent reads don't
    see a corrupt recipe.
    """
    import yaml as _yaml
    from okuro.orchestrator.preview.recipe import resolve_recipe_save_path

    if not isinstance(body, dict):
        raise RecipeError("recipe body must be a mapping")
    path = resolve_recipe_save_path(task_dir)
    path.write_text(_yaml.safe_dump(body, sort_keys=False))
    try:
        load_recipe(task_dir, slug_default=task_dir.name)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def logs_tail(task_dir: Path, slug_default: str, *, lines: int = 200) -> list[str]:
    """One-shot tail of the unified preview log.

    Merges two sources so callers see a single coherent stream regardless
    of recipe kind:

      1. ``preview.build.log`` — written by the launcher itself: phase
         markers (▸ install / ▸ build / ▸ ready / ✗ failed) plus the
         streamed stdout/stderr of every build subprocess (npm ci,
         vite build, nbconvert, ffmpeg…). This is the only log surface
         non-systemd kinds (doc/image/audio/video/slides/notebook/
         external) ever produce — without it those kinds rendered an
         empty drawer in the SPA.

      2. ``journalctl --user-unit tm-preview-{slug}`` — populated only
         while a serve unit is running (kind=web/static/api). Appended
         after the build log so the user reads top-down: build first,
         server logs after.

    Trims to the last ``lines`` entries combined. Per-source ordering is
    preserved (build log lines are file-order, journal lines are time-order).
    For SSE streaming see api/preview.py.
    """
    out: list[str] = []
    log_path = _build_log_path(task_dir)
    if log_path.exists():
        try:
            out.extend(log_path.read_text(errors="replace").splitlines())
        except OSError as exc:
            logger.warning("could not read %s: %s", log_path, exc)

    runtime = _read_runtime(task_dir) or {}
    slug = runtime.get("slug") or slug_default
    unit = _unit_name(slug)
    # Only consult the journal when there's actually a unit to consult —
    # otherwise we'd push noise like "(journalctl exit 1: …)" into the
    # drawer for every static-artifact preview.
    if shutil.which("systemctl") is not None and shutil.which("journalctl") is not None and _unit_known(unit):
        journal = _journal_tail(unit, lines=lines)
        # Dedupe the "(journalctl exit 1: …)" placeholder that fires when
        # the unit hasn't logged anything yet.
        journal = [ln for ln in journal if not (
            ln.startswith("(journalctl exit") or ln == "(journalctl not available on this host)"
        )]
        if journal:
            out.append("")
            out.append("--- service log ---")
            out.extend(journal)

    return out[-lines:] if lines and len(out) > lines else out


def _build_log_path(task_dir: Path) -> Path:
    return task_dir / BUILD_LOG_FILE


def _append_build_log(task_dir: Path, line: str) -> None:
    """Append a single annotated line (timestamp + arrow marker) to the
    build log. Tolerant of write failures — losing a log line is not
    worth aborting a launch.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with _build_log_path(task_dir).open("a") as f:
            f.write(f"[{ts}] {line}\n")
    except OSError as exc:
        logger.debug("append_build_log failed: %s", exc)


def _unit_known(unit: str) -> bool:
    """True when systemd has *any* record of the unit (active OR failed),
    which is the gate for whether journalctl --user-unit will produce
    anything useful. Without this, journalctl on a never-launched unit
    returns "(no entries)" which would leak into the drawer.
    """
    if shutil.which("systemctl") is None:
        return False
    proc = subprocess.run(
        ["systemctl", "--user", "show", "-p", "LoadState", "--value", unit],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() in {"loaded", "stub"}


# ── Internals ───────────────────────────────────────────────────────


def _has_recipe(task_dir: Path) -> bool:
    return (task_dir / "preview.yaml").exists()


def _runtime_path(task_dir: Path) -> Path:
    return task_dir / RUNTIME_FILE


def _read_runtime(task_dir: Path) -> Optional[dict]:
    p = _runtime_path(task_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("preview runtime.json unreadable at %s: %s", p, exc)
        return None


def _write_runtime(task_dir: Path, data: dict) -> None:
    data = dict(data)  # don't mutate caller's dict
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _runtime_path(task_dir).write_text(json.dumps(data, indent=2))


def _unit_name(slug: str) -> str:
    return f"{UNIT_PREFIX}{slug}"


def _is_active(unit: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", unit],
        capture_output=True, text=True, check=False,
    )
    # is-active returns 0 when active, non-zero otherwise; output is the state name.
    return proc.stdout.strip() == "active"


def _start_external(task_dir: Path, recipe: Recipe) -> PreviewState:
    """Bring up an external (compose-managed) preview.

    Flow:
      1. Quick HEAD/GET on ``recipe.url`` (3s). If reachable → ready.
      2. If a compose file is known and the URL is down, run
         ``docker compose -f <file> up -d`` from the compose dir.
      3. Re-poll the URL up to ~30s.
      4. On still-down: write a ``failed`` runtime entry whose
         ``last_error`` tells the user the manual command to run.

    Idempotent: re-running on a healthy stack is a no-op (step 1 short-
    circuits). We never call ``compose down`` — users own the lifecycle.
    """
    assert recipe.kind == "external" and recipe.url, "expects validated external recipe"
    url = recipe.url

    if _http_ping(url, timeout_s=3.0):
        _write_runtime(task_dir, {
            "state": "ready", "unit": None, "slug": recipe.slug,
            "url": url, "port": None,
        })
        return PreviewState(state="ready", slug=recipe.slug, url=url, recipe_present=True)

    compose = recipe.external_compose or {}
    compose_file = compose.get("compose_file")
    cwd = Path(compose.get("cwd") or recipe.cwd)
    if compose_file:
        compose_cmd = _resolve_compose_cmd()
        if compose_cmd is None:
            err = (
                f"{url} is not reachable and no docker-compose binary is on PATH. "
                f"Install docker (and the compose plugin), or start the service manually."
            )
            _write_runtime(task_dir, {
                "state": "failed", "unit": None, "slug": recipe.slug,
                "url": url, "last_error": err,
            })
            raise LauncherError(err)
        proc = subprocess.run(
            compose_cmd + ["-f", compose_file, "up", "-d"],
            cwd=str(cwd), capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            err = (
                f"`docker compose up -d` failed in {cwd}:\n"
                f"{(proc.stderr or proc.stdout).strip()}"
            )
            _write_runtime(task_dir, {
                "state": "failed", "unit": None, "slug": recipe.slug,
                "url": url, "last_error": err,
            })
            raise LauncherError(err)
        if _http_poll(url, timeout_s=30.0):
            _write_runtime(task_dir, {
                "state": "ready", "unit": None, "slug": recipe.slug,
                "url": url, "port": None,
            })
            return PreviewState(state="ready", slug=recipe.slug, url=url, recipe_present=True)

    # No compose hook OR compose came up but the URL still doesn't answer.
    err = (
        f"{url} is not reachable. "
        + (
            f"Check the container logs: `docker compose -f {compose_file} logs --tail=80`."
            if compose_file
            else "Start the external service manually."
        )
    )
    _write_runtime(task_dir, {
        "state": "failed", "unit": None, "slug": recipe.slug,
        "url": url, "last_error": err,
    })
    raise LauncherError(err)


def _resolve_compose_cmd() -> Optional[list[str]]:
    """Return the compose invocation as an argv prefix, or None.

    Modern docker (20.10+) ships compose as a plugin: ``docker compose``.
    Older installs ship the standalone ``docker-compose`` binary. Prefer
    the plugin when present so we don't accidentally pick up a stale v1.
    """
    if shutil.which("docker") is not None:
        # Probe whether the compose plugin is installed.
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose") is not None:
        return ["docker-compose"]
    return None


def _http_ping(url: str, *, timeout_s: float) -> bool:
    """One-shot reachability check. Any HTTP response (even 4xx/5xx)
    counts as 'service is up' — the URL might require auth or a path
    we don't know about, but the listening socket is what matters.
    """
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return 100 <= resp.status < 600
    except urllib.error.HTTPError:
        return True  # service answered, just not 2xx — still 'up'
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _http_poll(url: str, *, timeout_s: float, interval_s: float = 1.0) -> bool:
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _http_ping(url, timeout_s=2.0):
            return True
        time.sleep(interval_s)
    return False


def _rediscover_recipe(task_dir: Path, slug_default: str) -> None:
    """Re-run the proposer and overwrite the saved recipe so a rebuilt or
    extended task surfaces the NEWEST previewable artifact instead of the
    one frozen at first discovery.

    This is the fix for "the button only ever shows the first run's
    result": discovery used to happen once and freeze into ``preview.yaml``;
    every later build loaded that stale file. Now each explicit build click
    regenerates the recipe from the current artifact set.

    Best-effort and non-destructive on miss: if the proposer can't infer
    anything we KEEP the existing recipe (never destroy a working one on a
    failed re-scan). Recipes in this system are agent-generated, not
    hand-authored, so regenerating on each build is derived-state refresh,
    not user-data loss.
    """
    import yaml as _yaml
    from okuro.orchestrator.preview.recipe import resolve_recipe_save_path

    try:
        proposal = propose(
            task_dir, slug=slug_default,
            project_path=_read_task_project_path(task_dir),
        )
    except ProposerMiss:
        return  # nothing better to offer — keep whatever recipe exists
    except Exception as exc:  # noqa: BLE001 — discovery must never break a build
        logger.warning("preview rediscover failed for %s: %s", slug_default, exc)
        return

    save_path = resolve_recipe_save_path(task_dir)
    save_path.write_text(_yaml.safe_dump(proposal.body, sort_keys=False))
    _append_build_log(task_dir, f"▸ rediscovered recipe (template={proposal.template})")
    logger.info(
        "preview rediscovered template=%s for %s (cwd=%s) → %s",
        proposal.template, slug_default, proposal.cwd, save_path,
    )


def _autoproposed_recipe(task_dir: Path, slug_default: str, *, original_error: RecipeError) -> Recipe:
    """Run the proposer; on hit, persist the YAML and load it back.

    Persists to the canonical save path resolved from ``task.yaml``:
    ``{project_path}/.okuro/preview.yaml`` when project_path is set,
    else ``{task_dir}/preview.yaml``. This matches what ``load_recipe``
    will read on the next call.

    On miss, raise a fresh ``RecipeError`` whose attributes the API layer
    surfaces to the user — ``detected=False``, ``scanned`` (paths walked),
    ``known_templates`` (so the toast can say "we recognise vite/next/...").
    """
    import yaml as _yaml  # local import to keep recipe.py the canonical yaml entry
    from okuro.orchestrator.preview.recipe import resolve_recipe_save_path

    try:
        proposal = propose(
            task_dir, slug=slug_default,
            project_path=_read_task_project_path(task_dir),
        )
    except ProposerMiss as miss:
        err = RecipeError(
            f"No preview.yaml could be resolved for {task_dir.name} and "
            f"auto-detect could not infer one. Recognised templates: "
            f"{', '.join(miss.known)}. Scanned: {', '.join(miss.scanned) or '(empty)'}."
        )
        err.detected = False  # type: ignore[attr-defined]
        err.scanned = miss.scanned  # type: ignore[attr-defined]
        err.known_templates = list(miss.known)  # type: ignore[attr-defined]
        # P5.6 — carry the openable files the scan found, so the API can
        # offer a chooser rather than a dead end.
        err.candidates = list(getattr(miss, "candidates", []))  # type: ignore[attr-defined]
        err.proposer_miss = True  # type: ignore[attr-defined]
        raise err from miss

    recipe_path = resolve_recipe_save_path(task_dir)
    recipe_path.write_text(_yaml.safe_dump(proposal.body, sort_keys=False))
    logger.info(
        "preview auto-detected template=%s for %s (cwd=%s) — recipe saved to %s",
        proposal.template, slug_default, proposal.cwd, recipe_path,
    )
    return load_recipe(task_dir, slug_default)


def _pick_port(spec) -> int:
    if isinstance(spec, int):
        return spec
    # spec == "auto"
    from okuro.system.ports import get_port_status
    info = get_port_status(range_start=DEFAULT_PORT_RANGE[0], range_end=DEFAULT_PORT_RANGE[1])
    port = info.get("next_available")
    if not port:
        raise LauncherError(
            f"No available ports in {DEFAULT_PORT_RANGE[0]}..{DEFAULT_PORT_RANGE[1]}."
        )
    return int(port)


def _interp(s: str, port: int) -> str:
    return s.replace("{port}", str(port))


def _build_if_needed(task_dir: Path, recipe: Recipe, *, force: bool) -> None:
    if recipe.build is None:
        return

    cache_path = task_dir / BUILD_CACHE_FILE
    fingerprint = _build_fingerprint(recipe)

    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("fingerprint") == fingerprint:
                logger.info("preview build skipped (cache hit) for slug=%s", recipe.slug)
                _append_build_log(task_dir, "▸ build cache hit — skipping install")
                return
        except (json.JSONDecodeError, OSError):
            pass

    logger.info("preview build running for slug=%s: %s", recipe.slug, recipe.build.cmd)
    _append_build_log(task_dir, f"▸ build: {' '.join(recipe.build.cmd)}")

    # Stream subprocess output line-by-line into preview.build.log so SSE
    # tailers see progress in real time. capture_output (the previous
    # implementation) buffered everything in memory until the process
    # finished — fine for status reporting, useless for live UX.
    log_path = _build_log_path(task_dir)
    proc = subprocess.Popen(
        recipe.build.cmd,
        cwd=str(recipe.cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge so the order matches what the user would see in a terminal
        text=True,
        bufsize=1,  # line-buffered
    )

    last_lines: list[str] = []
    try:
        with log_path.open("a", buffering=1) as logf:
            assert proc.stdout is not None
            for line in proc.stdout:
                logf.write(line)
                # Keep the last ~80 lines in memory so a non-zero exit can
                # surface a useful tail in the LauncherError message even
                # without re-reading the file.
                last_lines.append(line.rstrip("\n"))
                if len(last_lines) > 80:
                    last_lines.pop(0)
        rc = proc.wait(timeout=recipe.build.timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise LauncherError(
            f"build cmd {recipe.build.cmd!r} timed out after {recipe.build.timeout_s}s"
        )

    if rc != 0:
        tail = "\n".join(last_lines)[-2000:]
        raise LauncherError(
            f"build cmd {recipe.build.cmd!r} exited {rc}\n--- output (tail) ---\n{tail}"
        )

    cache_path.write_text(json.dumps({
        "fingerprint": fingerprint,
        "cmd": recipe.build.cmd,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def _build_fingerprint(recipe: Recipe) -> str:
    """Hash of build cmd + the contents of every ``cache_key_files`` entry."""
    h = hashlib.sha256()
    h.update(json.dumps(recipe.build.cmd if recipe.build else []).encode())  # type: ignore[union-attr]
    for rel in (recipe.build.cache_key_files if recipe.build else []):
        p = recipe.cwd / rel
        if p.exists():
            try:
                h.update(rel.encode())
                h.update(p.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def _ready_check(url: str, timeout_s: int, status_lt: int, http_path: str) -> bool:
    """Poll the URL until it returns a status < ``status_lt`` or we time out.

    ``http_path`` is appended to the URL only if the URL doesn't already
    end with a path. The recipe's ``url:`` is the canonical client-facing
    URL; ``ready.http`` is just the path we probe.
    """
    if "://" not in url:
        return False
    # Probe a separate path if specified and the canonical URL has no path.
    probe = url
    try:
        # Always probe the ready.http path against the URL's origin.
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        if http_path:
            probe = urlunparse(parsed._replace(path=http_path, query="", fragment=""))
    except Exception:
        pass

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen(probe, timeout=2) as resp:
                if resp.status < status_lt:
                    return True
        except URLError:
            pass
        except Exception as exc:  # noqa: BLE001 — we want to keep polling on any transient error
            logger.debug("ready probe transient: %s", exc)
        time.sleep(0.5)
    return False


def _journal_tail(unit: str, *, lines: int) -> list[str]:
    """Return the last N journal lines for ``unit`` (oldest first)."""
    if shutil.which("journalctl") is None:
        return ["(journalctl not available on this host)"]
    proc = subprocess.run(
        [
            "journalctl", "--user-unit", unit,
            "-n", str(lines), "--no-pager", "-o", "cat",
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return [f"(journalctl exit {proc.returncode}: {proc.stderr.strip()})"]
    return [ln for ln in proc.stdout.splitlines() if ln]
