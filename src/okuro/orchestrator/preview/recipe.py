# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Load + validate preview.yaml recipes for orchestration task results.
# index:
#   imports
#   class RecipeError
#   class Recipe
#   def load_recipe
#   def resolve_recipe_save_path
#   def _read_task_project_path
#   def _validate
# AGENT_HEADER_END -->
"""Recipe loader and validator for ``preview.yaml``.

Resolution order (first match wins):

1. ``{project_path}/.okuro/preview.yaml`` — project-local declared
   recipe, written by the scaffolder/builder role as part of the
   deliverable. This is the canonical location: the recipe travels
   with the project, survives task archival, and works from any
   orchestrator instance that points at the same project root.
2. ``{task_dir}/preview.yaml`` — legacy per-run location. Used when
   ``task.project_path`` is unset (research deliverables, doc-only
   tasks) or when a user manually dropped a recipe there.

Auto-detect proposer (``okuro.orchestrator.preview.proposer``) fires
only when both locations miss AND the launcher's ``auto_detect=True``
caller asked for it. The proposer scans ``project_path`` first and
falls back to ``task_dir/artifacts`` only when no project_path is
declared — see proposer.propose().
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Optional

import yaml
from okuro.orchestrator.yamlfast import yload


class RecipeError(ValueError):
    """Raised when a preview.yaml is missing or malformed."""


# Kinds the launcher knows how to either serve (web/static/api) or hand
# off to the FastAPI ``/file`` endpoint with a sensible MIME-type. Adding
# audio/video/slides/notebook here means the proposer can land on tasks
# whose deliverable isn't a webapp without falling back to either a
# misclassified image or a hard "no preview" miss.
_VALID_KINDS = {
    "web", "static", "doc", "image", "api", "download", "external",
    "audio", "video", "slides", "notebook",
}
_RECIPE_FILENAME = "preview.yaml"
_PROJECT_LOCAL_DIR = ".okuro"


def _read_task_project_path(task_dir: Path) -> Optional[Path]:
    """Read the ``project_path`` field from ``{task_dir}/task.yaml``.

    Returns the resolved absolute path when set and existing, else
    None. Inlined here so the recipe loader can resolve project-local
    recipes without dragging in launcher imports (avoids a circular).
    """
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        return None
    try:
        data = yload(task_yaml.read_text()) or {}
    except yaml.YAMLError:
        return None
    raw = (data or {}).get("project_path") if isinstance(data, dict) else None
    if not raw:
        return None
    p = Path(str(raw)).expanduser()
    try:
        return p.resolve() if p.exists() and p.is_dir() else None
    except OSError:
        return None


def resolve_recipe_save_path(task_dir: Path) -> Path:
    """Where ``save_recipe`` should write a recipe for this task.

    Mirrors ``load_recipe``'s precedence: when a project_path is
    declared, project-local; otherwise per-task. The directory is
    created if missing so the caller can write unconditionally.
    """
    project_path = _read_task_project_path(task_dir)
    if project_path is not None:
        target_dir = project_path / _PROJECT_LOCAL_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / _RECIPE_FILENAME
    return task_dir / _RECIPE_FILENAME


@dataclasses.dataclass(frozen=True)
class BuildSpec:
    cmd: list[str]
    cache_key_files: list[str] = dataclasses.field(default_factory=list)
    timeout_s: int = 600


@dataclasses.dataclass(frozen=True)
class ReadyCheck:
    http: str = "/"
    status_lt: int = 500
    timeout_s: int = 30


@dataclasses.dataclass(frozen=True)
class ServeSpec:
    cmd: list[str]
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    port: Any = "auto"  # "auto" | int
    ready: ReadyCheck = dataclasses.field(default_factory=ReadyCheck)


@dataclasses.dataclass(frozen=True)
class Recipe:
    """Validated preview recipe.

    ``cwd`` is always an absolute Path. ``source_path`` is the recipe
    file we loaded from, kept for diagnostics.
    """

    version: int
    slug: str
    kind: str
    cwd: Path
    source_path: Path
    build: Optional[BuildSpec] = None
    serve: Optional[ServeSpec] = None
    url: Optional[str] = None
    open: str = "browser"
    file: Optional[str] = None  # for kind=doc/image/download
    ttl_idle: Optional[str] = None  # parsed by S2/launcher; opaque here
    # For kind=external: opaque dict with compose_file + service when the
    # recipe was generated from a docker-compose detection. Used by the
    # launcher to auto-`docker compose up -d` if the URL isn't reachable
    # at start time. None when the external URL is hand-rolled.
    external_compose: Optional[dict] = None

    def expects_serve(self) -> bool:
        return self.kind in {"web", "static", "api"}

    def is_static_artifact(self) -> bool:
        """True for kinds the launcher delivers as a single file via
        ``GET /api/preview/{id}/file`` — no port, no systemd unit, no
        ready check. Includes audio/video/slides/notebook so the new
        renderer kinds funnel through the same /file pipeline as
        doc/image/download.
        """
        return self.kind in {
            "doc", "image", "download", "audio", "video", "slides", "notebook",
        }


def load_recipe(task_dir: Path, slug_default: str) -> Recipe:
    """Load and validate the recipe for a task.

    Resolution: project-local (``{project_path}/.okuro/preview.yaml``)
    when ``task.project_path`` is set, else legacy
    (``{task_dir}/preview.yaml``). When ``project_path`` is set but the
    project-local file is missing, we DO NOT fall back to the task-dir
    file — the task explicitly identified a project, and a stale recipe
    in ``task_dir`` (e.g. an old auto-detected slides deck) must not
    shadow the project's own truth. The launcher then triggers the
    auto-detect proposer against ``project_path`` (vite/next/etc.).

    ``task_dir`` is the orchestration task directory (already validated
    by the caller — we don't repeat the path-traversal check here).
    ``slug_default`` is used as the recipe slug if the file omits it
    (typically the task_id).

    Raises ``RecipeError`` if no recipe is found or it fails validation.
    """
    project_path = _read_task_project_path(task_dir)

    if project_path is not None:
        candidate = project_path / _PROJECT_LOCAL_DIR / _RECIPE_FILENAME
    else:
        candidate = task_dir / _RECIPE_FILENAME

    if not candidate.exists():
        if project_path is not None:
            raise RecipeError(
                f"No preview.yaml at {candidate}. "
                f"task.project_path={project_path} is declared but the "
                f"project-local recipe is missing — auto-detect can "
                f"propose one from the project layout."
            )
        raise RecipeError(
            f"No preview.yaml at {candidate} and no project_path is "
            f"declared. Pick a project for this task via "
            f"PUT /api/tasks/{{id}}/project-path or drop a recipe at "
            f"the legacy path."
        )

    try:
        data = yload(candidate.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RecipeError(f"preview.yaml is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise RecipeError("preview.yaml must be a mapping at the top level.")

    cwd_anchor = candidate.parent if project_path is not None else task_dir
    return _validate(
        data, source_path=candidate, task_dir=task_dir,
        cwd_anchor=cwd_anchor, slug_default=slug_default,
    )


def _validate(
    data: dict[str, Any],
    *,
    source_path: Path,
    task_dir: Path,
    cwd_anchor: Path,
    slug_default: str,
) -> Recipe:
    version = data.get("version", 1)
    if version != 1:
        raise RecipeError(f"Unsupported preview.yaml version: {version!r} (expected 1).")

    kind = data.get("kind", "web")
    if kind not in _VALID_KINDS:
        raise RecipeError(f"Unknown kind: {kind!r}. Allowed: {sorted(_VALID_KINDS)}.")

    slug = (data.get("slug") or slug_default).strip()
    if not slug:
        raise RecipeError("slug is required (or pass a non-empty slug_default).")
    # systemd unit names accept these chars; restrict for safety.
    bad = [c for c in slug if not (c.isalnum() or c in "-_")]
    if bad:
        raise RecipeError(f"slug has invalid chars {bad!r}; use [A-Za-z0-9_-] only.")

    cwd_raw = data.get("cwd", ".")
    cwd_path = Path(cwd_raw)
    if not cwd_path.is_absolute():
        # Relative cwd resolves against the directory holding the recipe
        # (cwd_anchor). For project-local recipes that's the project root
        # (one level up from .okuro/); for legacy recipes that's task_dir.
        cwd_path = (cwd_anchor / cwd_path).resolve()
    if not cwd_path.exists():
        raise RecipeError(f"cwd does not exist: {cwd_path}")

    build = _parse_build(data.get("build"))
    serve = _parse_serve(data.get("serve"))

    expects_serve = kind in {"web", "static", "api"}
    if expects_serve and serve is None:
        raise RecipeError(f"kind={kind!r} requires a `serve:` block.")
    file_kinds = {"doc", "image", "download", "audio", "video", "slides", "notebook"}
    if kind in file_kinds and not data.get("file"):
        raise RecipeError(f"kind={kind!r} requires a `file:` field (path to artifact).")

    url = data.get("url")
    if kind == "external":
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise RecipeError(
                "kind='external' requires `url:` starting with http:// or https:// "
                "(no '{port}' substitution — external services own their port)."
            )

    external_compose = data.get("external_compose")
    if external_compose is not None and not isinstance(external_compose, dict):
        raise RecipeError("`external_compose:` must be a mapping when present.")

    return Recipe(
        version=version,
        slug=slug,
        kind=kind,
        cwd=cwd_path,
        source_path=source_path,
        build=build,
        serve=serve,
        url=url,
        open=data.get("open", "browser"),
        file=data.get("file"),
        ttl_idle=data.get("ttl_idle"),
        external_compose=external_compose,
    )


def _parse_build(raw: Any) -> Optional[BuildSpec]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RecipeError("`build:` must be a mapping.")
    cmd = raw.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(p, str) for p in cmd):
        raise RecipeError("`build.cmd` must be a non-empty list of strings.")
    return BuildSpec(
        cmd=list(cmd),
        cache_key_files=list(raw.get("cache_key_files") or []),
        timeout_s=int(raw.get("timeout_s") or 600),
    )


def _parse_serve(raw: Any) -> Optional[ServeSpec]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RecipeError("`serve:` must be a mapping.")
    cmd = raw.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(p, str) for p in cmd):
        raise RecipeError("`serve.cmd` must be a non-empty list of strings.")

    port = raw.get("port", "auto")
    if port != "auto" and not (isinstance(port, int) and 1 <= port <= 65535):
        raise RecipeError("`serve.port` must be 'auto' or an integer 1..65535.")

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise RecipeError("`serve.env` must be a mapping.")
    env = {str(k): str(v) for k, v in env_raw.items()}

    ready_raw = raw.get("ready") or {}
    if not isinstance(ready_raw, dict):
        raise RecipeError("`serve.ready` must be a mapping.")
    ready = ReadyCheck(
        http=str(ready_raw.get("http", "/")),
        status_lt=int(ready_raw.get("status_lt", 500)),
        timeout_s=int(ready_raw.get("timeout_s", 30)),
    )

    return ServeSpec(cmd=list(cmd), env=env, port=port, ready=ready)
