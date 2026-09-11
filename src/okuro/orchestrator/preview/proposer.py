# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Auto-detect preview recipe by inspecting an artifact directory.
# index:
#   imports
#   class Proposal
#   class ProposerMiss
#   def propose
#   _detect_*
#   _scan
#   _read_json
# AGENT_HEADER_END -->
"""Preview recipe auto-detector.

Pure module: scan a directory, return a draft recipe matching the first
template that fits, or raise ``ProposerMiss`` with diagnostic info.

Detection order matters — the first match wins, so more specific
templates (Next.js) come before more generic ones (any package.json
serving on `npm run dev`).

Templates supported in v1:
  vite, next, react-scripts, static-html, fastapi, django, pdf, image

Output is a recipe **body** (dict) suitable for ``yaml.safe_dump`` and
loading via :mod:`okuro.orchestrator.preview.recipe`. The launcher
saves it as the per-task ``preview.yaml`` and proceeds normally.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Optional

import yaml
from okuro.orchestrator.yamlfast import yload


KNOWN_TEMPLATES = (
    "docker-compose",
    "next",
    "vite",
    "react-scripts",
    "static-html",
    "fastapi",
    "django",
    "notebook",       # Jupyter notebook → nbconvert (build) → kind=doc HTML
    "slides",         # marp / reveal-md markdown deck → kind=slides HTML
    "pdf",
    "video",          # mp4/webm/mov → kind=video, served inline
    "audio",          # mp3/wav/flac/m4a/opus → kind=audio, served inline (transcoded with ffmpeg when not browser-native)
    "image",
    "markdown",
    "html-file",      # arbitrary standalone .html → kind=doc (or kind=static when sibling assets exist)
)


@dataclasses.dataclass(frozen=True)
class Proposal:
    template: str         # one of KNOWN_TEMPLATES
    cwd: Path             # absolute — where the recipe should run
    body: dict            # recipe body, ready to yaml.safe_dump
    confidence: str       # 'high' | 'medium' | 'low'
    reason: str           # one-line "why this template"


class ProposerMiss(LookupError):
    """No detector matched. Carries diagnostic info for the UI."""

    def __init__(
        self,
        scanned: list[str],
        known: tuple[str, ...] = KNOWN_TEMPLATES,
        candidates: Optional[list[dict]] = None,
    ):
        self.scanned = scanned
        self.known = known
        # ROCK-SOLID v5 P5.6 — what the scan DID find.
        #
        # The miss used to carry only where it looked and which templates
        # exist, and the UI turned that into "add a preview.yaml in the task
        # dir to override" — telling a user who wanted to see their result to
        # go and write a config file. In practice the scan has usually found
        # several perfectly openable files and simply had no rule that fit.
        # Handing those back turns a dead end into a choice.
        self.candidates = candidates or []
        super().__init__(
            "Could not auto-detect a preview recipe. "
            f"Recognised templates: {', '.join(known)}."
        )


# ── Declared-deliverable resolution ─────────────────────────────────
#
# The task's plan names the exact file(s) it set out to produce
# (``subtask.target_paths`` / ``subtask.outputs``). That is GROUND TRUTH
# and must beat the directory-grep detectors below — those pick the first
# type-matching file in the tree and silently shadow the real deliverable
# when a project dir holds several (the regression: a stray
# ``meridian-priority-roadmap.md`` won over the declared
# ``presentations/meridian-mission-deck.html``). ``target_paths`` (an
# explicit file the planner committed to) outranks ``outputs``; among a
# tier, the latest subtask (final pipeline stage) and a rendered/binary
# extension win, then most-recent mtime.


@dataclasses.dataclass(frozen=True)
class _DeclaredFile:
    path: Path
    tier: int          # 0 = target_paths (explicit), 1 = outputs
    order: int         # plan order; higher = later subtask = more final
    mtime: float
    source: str        # "target_paths" | "outputs"
    subtask_id: str


# Rendered/binary artifacts are more likely the showcase deliverable than
# their raw source — rank them ahead of plain markdown on tiebreak.
_EXT_PRIORITY = {
    ".html": 0, ".htm": 0, ".pdf": 1, ".ipynb": 2,
    ".png": 3, ".jpg": 3, ".jpeg": 3, ".svg": 3, ".webp": 3,
    ".mp4": 4, ".webm": 4, ".mov": 4, ".m4v": 4,
    ".mp3": 5, ".wav": 5, ".ogg": 5, ".m4a": 5,
    ".flac": 5, ".opus": 5, ".aac": 5, ".wma": 5,
    ".md": 9,
}


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _resolve_declared_path(
    raw: str, project_path: Optional[Path], task_dir: Path,
) -> Optional[Path]:
    """Resolve a declared output string to an existing FILE on disk.

    Mirrors the resolution order used by the deterministic reviewer's
    ``_deliverable_paths``: absolute as-is, else project-relative, else
    under the task's ``artifacts/``, else task-relative. Directory targets
    (common for ``target_paths`` that name a dir, not a file) resolve to
    None — only concrete files are deliverables we can preview.
    """
    p = Path(raw)
    cands: list[Path] = []
    if p.is_absolute():
        cands.append(p)
    else:
        if project_path is not None:
            cands.append(project_path / p)
        cands.append(task_dir / "artifacts" / p)
        cands.append(task_dir / p)
    for c in cands:
        try:
            if c.exists() and c.is_file():
                return c.resolve()
        except OSError:
            continue
    return None


def _declared_deliverable_files(
    task_dir: Path, project_path: Optional[Path],
) -> list[_DeclaredFile]:
    """Read ``plan.yaml`` and return every declared output that resolves
    to an existing file. Failure-tolerant: missing/corrupt plan → []."""
    plan_path = task_dir / "plan.yaml"
    if not plan_path.exists():
        return []
    try:
        data = yload(plan_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[_DeclaredFile] = []
    order = 0
    for phase in data.get("phases", []) or []:
        if not isinstance(phase, dict):
            continue
        for st in phase.get("subtasks", []) or []:
            if not isinstance(st, dict):
                continue
            order += 1
            sid = str(st.get("id") or "")
            for tier, key in ((0, "target_paths"), (1, "outputs")):
                for raw in st.get(key) or []:
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    f = _resolve_declared_path(raw.strip(), project_path, task_dir)
                    if f is not None:
                        out.append(_DeclaredFile(
                            path=f, tier=tier, order=order,
                            mtime=_safe_mtime(f), source=key, subtask_id=sid,
                        ))
    return out


def _detect_declared_output(
    task_dir: Path, *, slug: str, project_path: Optional[Path] = None,
) -> Optional[Proposal]:
    """Build a recipe from the task's DECLARED deliverable, if any resolves.

    Returns the highest-ranked declared file that maps to a previewable
    recipe (see :func:`_recipe_for_file`), or None to fall through to the
    directory-grep detectors. Candidates that map to an unrecognised
    extension are skipped, not fatal.
    """
    candidates = _declared_deliverable_files(task_dir, project_path)
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda c: (c.tier, -c.order, _EXT_PRIORITY.get(c.path.suffix.lower(), 7), -c.mtime),
    )
    for c in ranked:
        rec = _recipe_for_file(c.path, slug=slug, source=c.source, subtask_id=c.subtask_id)
        if rec is not None:
            return rec
    return None


def _recipe_for_file(
    path: Path, *, slug: str, source: str = "", subtask_id: str = "",
) -> Optional[Proposal]:
    """Build a recipe Proposal for a SPECIFIC file, routed by extension.

    Mirrors the per-kind body shapes the directory detectors emit, but
    targets one known file instead of scanning. Returns None for
    extensions no kind handles (caller falls through to grep).
    """
    import shutil as _sh

    ext = path.suffix.lower()
    parent = path.parent
    why = f"declared deliverable ({source} of subtask {subtask_id}) at {path}"

    # Markdown — a slide deck (build to HTML) or a plain doc.
    if ext == ".md":
        if _looks_like_slide_deck(path):
            output = f"{path.stem}.html"
            body: dict = {
                "version": 1, "kind": "slides", "slug": slug,
                "cwd": str(parent), "file": output,
            }
            if _sh.which("marp") is not None:
                body["build"] = {
                    "cmd": ["marp", "--output", output, path.name],
                    "cache_key_files": [path.name], "timeout_s": 180,
                }
            elif _sh.which("npx") is not None:
                body["build"] = {
                    "cmd": ["npx", "--yes", "@marp-team/marp-cli@latest",
                            "--output", output, path.name],
                    "cache_key_files": [path.name], "timeout_s": 240,
                }
            else:
                body["file"] = path.name
            return Proposal(template="slides", cwd=parent, body=body,
                            confidence="high", reason=why)
        body = {
            "version": 1, "kind": "doc", "slug": slug,
            "cwd": str(parent), "file": path.name,
        }
        return Proposal(template="markdown", cwd=parent, body=body,
                        confidence="high", reason=why)

    # Standalone HTML — served as kind=doc through the okuro API. The
    # /serve/{token}/ endpoint serves the whole parent dir, so relative
    # CSS/JS/iframe refs resolve under the same Caddy-proxied, bearer-gated
    # path. No loopback http.server (which a remote viewer can't reach) and
    # no LAN port exposure.
    if ext in (".html", ".htm"):
        body = {
            "version": 1, "kind": "doc", "slug": slug,
            "cwd": str(parent), "file": path.name,
        }
        return Proposal(template="html-file", cwd=parent, body=body,
                        confidence="high", reason=why)

    if ext == ".pdf":
        body = {"version": 1, "kind": "doc", "slug": slug,
                "cwd": str(parent), "file": path.name}
        return Proposal(template="pdf", cwd=parent, body=body,
                        confidence="high", reason=why)

    if ext == ".ipynb":
        output = f"{path.stem}.html"
        body = {"version": 1, "kind": "notebook", "slug": slug,
                "cwd": str(parent), "file": output}
        if _sh.which("jupyter") is not None:
            body["build"] = {
                "cmd": ["jupyter", "nbconvert", "--to", "html",
                        "--output", output, path.name],
                "cache_key_files": [path.name], "timeout_s": 300,
            }
        else:
            body["file"] = path.name
        return Proposal(template="notebook", cwd=parent, body=body,
                        confidence="high", reason=why)

    if ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
        body = {"version": 1, "kind": "image", "slug": slug,
                "cwd": str(parent), "file": path.name}
        return Proposal(template="image", cwd=parent, body=body,
                        confidence="high", reason=why)

    if ext in (".mp4", ".webm", ".mov", ".m4v"):
        body = {"version": 1, "kind": "video", "slug": slug,
                "cwd": str(parent), "file": path.name}
        return Proposal(template="video", cwd=parent, body=body,
                        confidence="high", reason=why)

    if ext in (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".aac", ".wma"):
        body = {"version": 1, "kind": "audio", "slug": slug,
                "cwd": str(parent), "file": path.name}
        if ext in (".flac", ".opus", ".aac", ".wma") and _sh.which("ffmpeg") is not None:
            out = f"{path.stem}.mp3"
            body["file"] = out
            body["build"] = {
                "cmd": ["ffmpeg", "-y", "-i", path.name,
                        "-codec:a", "libmp3lame", "-qscale:a", "2", out],
                "cache_key_files": [path.name], "timeout_s": 600,
            }
        return Proposal(template="audio", cwd=parent, body=body,
                        confidence="high", reason=why)

    return None


# ── Public ──────────────────────────────────────────────────────────


def propose(
    task_dir: Path,
    *,
    slug: str,
    project_path: Optional[Path] = None,
) -> Proposal:
    """Inspect the task and return a draft recipe.

    Resolution order:
      0. The task's DECLARED deliverable — ``subtask.target_paths`` /
         ``subtask.outputs`` from ``plan.yaml`` (see
         :func:`_detect_declared_output`). Ground truth; beats every
         heuristic below.

    Then, when no declared file resolves, scan by directory:
      1. ``project_path`` when set (the realized project — pnpm/vite SPA,
         next app, fastapi server, etc.). This is THE place to look for
         a runnable webapp; without it the proposer falls through to
         orchestrator bookkeeping.
      2. ``{task_dir}/artifacts/`` — orchestrator bookkeeping. Holds
         markdown reports, stdout dumps, debug screenshots. Useful only
         for tasks whose actual deliverable is a doc or image.
      3. ``{task_dir}`` itself (fallback for legacy tasks that put files
         directly under the task root).

    The first matching template wins. ``project_path`` is checked first
    so that a webapp-shaped project beats a stray PNG sitting in
    artifacts (which previously caused every webapp task to misclassify
    as ``kind=image``). Raises ``ProposerMiss`` if nothing matches.
    """
    # 0. Declared deliverable wins over directory-grep heuristics.
    declared = _detect_declared_output(
        task_dir, slug=slug, project_path=project_path,
    )
    if declared is not None:
        return declared

    # 1. P5.1 — the brain artifact, ABOVE every directory scan. A task whose
    # deliverable lives only in the brain used to fall through to
    # orchestrator bookkeeping and preview a stdout dump or a stray PDF.
    brain = _detect_brain_artifact(task_dir, slug=slug)
    if brain is not None:
        return brain

    candidates: list[Path] = []
    if project_path is not None and project_path.exists() and project_path.is_dir():
        candidates.append(project_path)
    for root in (task_dir / "artifacts", task_dir):
        if not root.exists() or not root.is_dir():
            continue
        candidates.append(root)

    detectors = (
        # docker-compose first: when the project is containerized, the
        # compose file is the authoritative runtime declaration. The
        # in-repo source code (next/vite/fastapi) is just an
        # implementation detail behind the container — auto-launching
        # it directly would shadow the real deployment.
        _detect_docker_compose,
        _detect_next,
        _detect_vite,
        _detect_react_scripts,
        _detect_django,
        _detect_fastapi,
        _detect_static_html,
        # Rich-media kinds before generic doc/image fallbacks so a task
        # whose deliverable is a notebook or slide deck doesn't get
        # misclassified as a markdown report (the deck IS markdown, but
        # the builder turns it into a presentable HTML — falling back
        # would render the raw .md text).
        _detect_notebook,
        _detect_slides,
        _detect_pdf,
        _detect_video,
        _detect_audio,
        _detect_markdown,
        _detect_image,
        # Final fallback: arbitrary standalone .html (research reports,
        # comparison tables, dashboards). Runs LAST so more specific
        # detectors (slides, notebook, pdf, markdown) keep priority over
        # generic html — only fires when nothing else claims the dir.
        _detect_html_file,
    )

    scanned: list[str] = []
    for root in candidates:
        scanned.append(str(root))
        for det in detectors:
            try:
                hit = det(root, slug=slug)
            except Exception:
                hit = None
            if hit is not None:
                return hit

    raise ProposerMiss(scanned=scanned, candidates=_miss_candidates(task_dir, project_path))


# ── Detectors ───────────────────────────────────────────────────────


def _miss_candidates(
    task_dir: Path, project_path: Optional[Path] = None, *, limit: int = 25,
) -> list[dict]:
    """Openable files the scan found but no detector claimed (P5.6).

    Ordered newest-first by ``_scan``, so the top entry is the most likely
    thing the user meant. Brain artifacts come first regardless: if a task
    HAS a user-facing artifact and still missed, that artifact is almost
    certainly the deliverable and something upstream is wrong.
    """
    out: list[dict] = []

    try:
        from okuro.sense.artifacts import artifact_list

        for row in artifact_list(
            task_id=task_dir.name, audience="user",
            include_superseded=False, limit=10,
        ) or []:
            out.append({
                "source": "artifact",
                "id": str(row.get("id") or ""),
                "label": str(row.get("title") or "untitled"),
                "kind": str(row.get("kind") or ""),
            })
    except Exception:
        pass

    seen: set[str] = set()
    roots = [r for r in (project_path, task_dir / "artifacts", task_dir) if r]
    openable = (".html", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".svg",
                ".webp", ".ipynb", ".csv", ".json", ".txt")
    for root in roots:
        try:
            if not root.exists() or not root.is_dir():
                continue
            for f in _scan(root, suffixes=openable, max_depth=3):
                key = str(f)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "source": "file",
                    "path": key,
                    "label": f.name,
                    "kind": f.suffix.lstrip("."),
                })
                if len(out) >= limit:
                    return out
        except OSError:
            continue
    return out


def _detect_brain_artifact(task_dir: Path, *, slug: str) -> Optional[Proposal]:
    """The task's deliverable IS a brain artifact (ROCK-SOLID v5 P5.1).

    Rank 1: below the DECLARED output (in-code ground truth, unchanged) and
    ABOVE every directory scan. That ordering is the whole fix. A research or
    deliberation task writes its report to the brain and nothing to disk, so
    the scans below found only orchestrator bookkeeping — a stdout dump, a
    debug screenshot, a stray PDF — and previewed that. The report the task
    was FOR was never a candidate, which is the "standard PDF" complaint.

    Newest ``audience="user"`` artifact wins. Process and agent artifacts are
    excluded on purpose: they are traces, and previewing one would answer
    "what did the machine do" when the user asked "what did I get".

    The body is rendered to a file under ``.preview/`` because the preview
    server serves files. Written on every propose() so an artifact edited or
    refreshed since the last preview is not served from a stale copy.
    """
    try:
        from okuro.sense.artifacts import artifact_list
    except Exception:
        return None

    task_id = task_dir.name
    try:
        rows = artifact_list(
            task_id=task_id, audience="user", include_superseded=False, limit=25,
        )
    except Exception:
        return None
    if not rows:
        return None

    # artifact_list is newest-first; take the first with a usable body.
    row = next((r for r in rows if (r or {}).get("id")), None)
    if row is None:
        return None

    try:
        from okuro.sense.artifacts import artifact_get

        full = artifact_get(str(row["id"]), include_body=True) or {}
    except Exception:
        return None
    body_text = str(full.get("body") or "")
    if not body_text.strip():
        return None

    media = str(full.get("media_type") or "").lower()
    ext = ".html" if "html" in media else ".md"
    out_dir = task_dir / ".preview"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"artifact-{row['id']}{ext}"
        target.write_text(body_text, encoding="utf-8")
    except OSError:
        return None

    title = str(full.get("title") or "deliverable")
    return Proposal(
        template="html_file" if ext == ".html" else "markdown",
        cwd=out_dir,
        body={
            "version": 1,
            "kind": "doc",
            "slug": slug,
            "cwd": str(out_dir),
            "file": target.name,
        },
        confidence="high",
        reason=f"brain artifact {title!r} (audience=user) is this task's deliverable",
    )


def _detect_docker_compose(root: Path, *, slug: str) -> Optional[Proposal]:
    """Detect a docker-compose.yml with at least one host port mapping.

    Returns a ``kind=external`` recipe pointing at the first host port
    found. The launcher will ``docker compose up -d`` from the compose
    file's directory if the URL isn't reachable. We pick the first
    service with a port mapping under the assumption that compose files
    list the user-facing service first; consumers can edit the recipe
    if a different service should be the target.
    """
    import yaml as _yaml

    compose_file = None
    for fname in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        hit = _find_first(root, fname, max_depth=3)
        if hit is not None:
            compose_file = hit
            break
    if compose_file is None:
        return None

    try:
        data = yload(compose_file.read_text()) or {}
    except (OSError, _yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None

    services = data.get("services") or {}
    if not isinstance(services, dict):
        return None

    host_port: Optional[int] = None
    service_name: Optional[str] = None
    for sname, svc in services.items():
        if not isinstance(svc, dict):
            continue
        ports = svc.get("ports") or []
        if not isinstance(ports, list):
            continue
        for entry in ports:
            hp = _parse_compose_port(entry)
            if hp is not None:
                host_port = hp
                service_name = str(sname)
                break
        if host_port is not None:
            break

    if host_port is None:
        return None

    cwd = compose_file.parent
    body = {
        "version": 1,
        "kind": "external",
        "slug": slug,
        "cwd": str(cwd),
        "url": f"http://localhost:{host_port}",
        "external_compose": {
            "compose_file": str(compose_file),
            "service": service_name,
        },
    }
    return Proposal(
        template="docker-compose", cwd=cwd, body=body, confidence="high",
        reason=f"docker-compose service '{service_name}' maps host port {host_port} ({compose_file})",
    )


def _parse_compose_port(entry) -> Optional[int]:
    """Extract the host port from a compose ``ports:`` entry.

    Compose accepts several shapes:
      - ``"8091"``                      → published == container == 8091
      - ``"8091:8091"``                 → host:container
      - ``"127.0.0.1:8091:8091"``       → ip:host:container
      - ``"8091:8091/tcp"``             → with protocol suffix
      - ``8091`` (int)                  → published == container
      - ``{published: 8091, target: 8091}`` (long form)

    Returns the host-side port, or None when the entry is malformed
    or uses a port range we shouldn't pick from.
    """
    if isinstance(entry, int):
        return entry if 1 <= entry <= 65535 else None
    if isinstance(entry, str):
        s = entry.split("/", 1)[0]  # drop /tcp /udp
        parts = s.split(":")
        if any("-" in p for p in parts):  # reject ranges
            return None
        try:
            if len(parts) == 1:
                return int(parts[0])
            if len(parts) == 2:
                return int(parts[0])
            if len(parts) == 3:
                return int(parts[1])
        except ValueError:
            return None
        return None
    if isinstance(entry, dict):
        pub = entry.get("published")
        try:
            return int(pub) if pub is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _detect_next(root: Path, *, slug: str) -> Optional[Proposal]:
    pkg_path, pkg = _find_package_json_with_dep(root, ("next",))
    if not pkg:
        return None
    cwd = pkg_path.parent
    body = {
        "version": 1,
        "kind": "web",
        "slug": slug,
        "cwd": str(cwd),
        "build": {
            "cmd": ["npm", "ci"],
            "cache_key_files": ["package-lock.json", "package.json"],
        },
        "serve": {
            "cmd": ["npx", "next", "dev", "--port", "{port}"],
            "port": "auto",
            "ready": {"http": "/", "status_lt": 500, "timeout_s": 60},
        },
        "url": "http://127.0.0.1:{port}/",
    }
    return Proposal(template="next", cwd=cwd, body=body, confidence="high",
                    reason=f"package.json depends on next ({pkg_path})")


def _detect_vite(root: Path, *, slug: str) -> Optional[Proposal]:
    pkg_path, pkg = _find_package_json_with_dep(
        root, ("vite", "@vitejs/plugin-react"),
    )
    if not pkg:
        return None
    cwd = pkg_path.parent
    # Prefer the locally-installed vite binary over npx — npx fails
    # silently under systemd-run when PATH lacks the node_modules/.bin
    # entry, and pnpm workspaces commonly hide the binary in a parent's
    # node_modules. _resolve_local_bin walks up to find it.
    vite_bin = _resolve_local_bin(cwd, "vite")
    serve_cmd = (
        [str(vite_bin), "--port", "{port}", "--host", "127.0.0.1"]
        if vite_bin
        else ["npx", "vite", "--port", "{port}", "--host", "127.0.0.1"]
    )
    # Skip `npm ci` build step when node_modules already exist (workspace
    # installs are typically pre-populated). Otherwise run the right
    # package manager based on lockfile presence.
    build_cmd = _detect_build_cmd(cwd) if not (cwd / "node_modules").exists() else None
    body: dict = {
        "version": 1,
        "kind": "web",
        "slug": slug,
        "cwd": str(cwd),
        "serve": {
            "cmd": serve_cmd,
            "port": "auto",
            "ready": {"http": "/", "status_lt": 500, "timeout_s": 45},
        },
        "url": "http://127.0.0.1:{port}/",
    }
    if build_cmd is not None:
        body["build"] = build_cmd
    return Proposal(template="vite", cwd=cwd, body=body, confidence="high",
                    reason=f"package.json depends on vite ({pkg_path})")


def _detect_react_scripts(root: Path, *, slug: str) -> Optional[Proposal]:
    pkg_path, pkg = _find_package_json_with_dep(root, ("react-scripts",))
    if not pkg:
        return None
    cwd = pkg_path.parent
    body = {
        "version": 1,
        "kind": "web",
        "slug": slug,
        "cwd": str(cwd),
        "build": {"cmd": ["npm", "ci"], "cache_key_files": ["package-lock.json", "package.json"]},
        "serve": {
            "cmd": ["npx", "react-scripts", "start"],
            "env": {"PORT": "{port}", "BROWSER": "none", "HOST": "127.0.0.1"},
            "port": "auto",
            "ready": {"http": "/", "status_lt": 500, "timeout_s": 60},
        },
        "url": "http://127.0.0.1:{port}/",
    }
    return Proposal(template="react-scripts", cwd=cwd, body=body, confidence="high",
                    reason=f"package.json depends on react-scripts ({pkg_path})")


def _detect_django(root: Path, *, slug: str) -> Optional[Proposal]:
    manage = _find_first(root, "manage.py")
    if manage is None:
        return None
    cwd = manage.parent
    body = {
        "version": 1,
        "kind": "api",
        "slug": slug,
        "cwd": str(cwd),
        "serve": {
            "cmd": ["python", "manage.py", "runserver", "127.0.0.1:{port}", "--noreload"],
            "port": "auto",
            "ready": {"http": "/", "status_lt": 500, "timeout_s": 30},
        },
        "url": "http://127.0.0.1:{port}/",
    }
    return Proposal(template="django", cwd=cwd, body=body, confidence="high",
                    reason=f"manage.py at {manage}")


def _detect_fastapi(root: Path, *, slug: str) -> Optional[Proposal]:
    """Heuristic: a *.py file under root that contains `FastAPI(`.

    Walks up to depth 4 — orchestration outputs are usually shallow.
    Returns module:attr that uvicorn can import, computed from the file path.
    """
    for py in _scan(root, suffixes=(".py",), max_depth=4):
        try:
            text = py.read_text(errors="ignore")
        except OSError:
            continue
        if "FastAPI(" not in text:
            continue
        # Find the variable assigned to FastAPI() — default to "app".
        attr = "app"
        for line in text.splitlines():
            line = line.strip()
            if "FastAPI(" in line and "=" in line:
                attr = line.split("=", 1)[0].strip()
                break
        # Resolve module path relative to cwd.
        cwd = py.parent
        module = py.stem
        body = {
            "version": 1,
            "kind": "api",
            "slug": slug,
            "cwd": str(cwd),
            "serve": {
                "cmd": ["uvicorn", f"{module}:{attr}", "--host", "127.0.0.1", "--port", "{port}"],
                "port": "auto",
                "ready": {"http": "/", "status_lt": 500, "timeout_s": 30},
            },
            "url": "http://127.0.0.1:{port}/",
        }
        return Proposal(template="fastapi", cwd=cwd, body=body, confidence="medium",
                        reason=f"FastAPI() construct found in {py}")
    return None


def _detect_static_html(root: Path, *, slug: str) -> Optional[Proposal]:
    # Skip if a package.json exists — JS toolchain detectors run first; if
    # they didn't match, falling back to a plain http.server is still wrong.
    if _find_package_json(root)[0] is not None:
        return None
    index = _find_first(root, "index.html")
    if index is None:
        return None
    cwd = index.parent
    body = {
        "version": 1,
        "kind": "static",
        "slug": slug,
        "cwd": str(cwd),
        "serve": {
            "cmd": ["python3", "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
            "port": "auto",
            "ready": {"http": "/index.html", "status_lt": 400, "timeout_s": 8},
        },
        "url": "http://127.0.0.1:{port}/index.html",
    }
    return Proposal(template="static-html", cwd=cwd, body=body, confidence="high",
                    reason=f"index.html at {index} (no JS toolchain)")


def _detect_pdf(root: Path, *, slug: str) -> Optional[Proposal]:
    pdfs = [p for p in _scan(root, suffixes=(".pdf",), max_depth=3)]
    if not pdfs:
        return None
    target = pdfs[0]
    body = {
        "version": 1,
        "kind": "doc",
        "slug": slug,
        "cwd": str(target.parent),
        "file": target.name,
    }
    return Proposal(template="pdf", cwd=target.parent, body=body, confidence="high",
                    reason=f"PDF found at {target}")


def _detect_image(root: Path, *, slug: str) -> Optional[Proposal]:
    imgs = [p for p in _scan(root, suffixes=(".png", ".jpg", ".jpeg", ".svg", ".webp"), max_depth=3)]
    if not imgs:
        return None
    target = imgs[0]
    body = {
        "version": 1,
        "kind": "image",
        "slug": slug,
        "cwd": str(target.parent),
        "file": target.name,
    }
    return Proposal(template="image", cwd=target.parent, body=body, confidence="medium",
                    reason=f"Image found at {target}")


def _detect_html_file(root: Path, *, slug: str) -> Optional[Proposal]:
    """Standalone HTML deliverable — final-fallback detector.

    Picks up arbitrary ``.html`` files whose names don't match the
    earlier conventional detectors (``index.html`` via static-html, or
    ``slides.html``/``presentation.html``/``deck.html`` via slides).
    Covers the common shape of agent-generated single-file HTML
    reports: research write-ups, comparison tables, dashboards inlined
    into one document (e.g. ``sdk-comparison-report.html``).

    Routing: ``kind=doc`` served through the okuro API. The
    ``/serve/{token}/`` endpoint serves the HTML's whole parent dir, so
    relative CSS/JS refs and sibling ``.html`` iframes resolve under the
    same Caddy-proxied, bearer-gated path — no loopback ``http.server``
    (unreachable from a remote viewer) and no LAN port exposure.

    Skipped when a ``package.json`` is present in the scan root: the JS
    toolchain detectors should have already claimed it, and silently
    falling back to a plain file serve would mask the real intent
    (mirrors the same guard in ``_detect_static_html``).
    """
    # Same guard as _detect_static_html: a present package.json means
    # the deliverable is a JS app whose framework detector should fire,
    # not an arbitrary HTML file the user wants served raw.
    if _find_package_json(root)[0] is not None:
        return None
    htmls = sorted(
        _scan(root, suffixes=(".html", ".htm"), max_depth=3),
        # Prefer index.html when present (defensive — _detect_static_html
        # already takes index.html earlier, but ordering matters if this
        # detector is ever called in isolation), then NEWEST first so an
        # extended task whose later run wrote a fresh report wins over the
        # first run's artifact. Alphabetical is the final tiebreak only.
        key=lambda p: (p.name.lower() != "index.html", -_safe_mtime(p), p.name.lower()),
    )
    if not htmls:
        return None
    target = htmls[0]
    parent = target.parent

    body = {
        "version": 1,
        "kind": "doc",
        "slug": slug,
        "cwd": str(parent),
        "file": target.name,
    }
    return Proposal(
        template="html-file", cwd=parent, body=body, confidence="high",
        reason=f"Standalone HTML at {target}",
    )


def _detect_markdown(root: Path, *, slug: str) -> Optional[Proposal]:
    """Detect markdown deliverables (research reports, design docs, READMEs).

    Skips ``*.stdout.md`` files — those are orchestrator stdout dumps from
    save_artifact's fallback path, not user-facing deliverables. Also
    skips files whose first non-blank lines look like a slide deck
    (``---`` front matter with ``marp: true`` or ``theme:``, or a
    repeating ``\\n---\\n`` slide separator) — those route to
    ``_detect_slides`` instead of being served as plain markdown.
    """
    md_files = [
        p for p in _scan(root, suffixes=(".md",), max_depth=3)
        if not p.name.endswith(".stdout.md") and not _looks_like_slide_deck(p)
    ]
    if not md_files:
        return None
    target = md_files[0]
    body = {
        "version": 1,
        "kind": "doc",
        "slug": slug,
        "cwd": str(target.parent),
        "file": target.name,
    }
    return Proposal(template="markdown", cwd=target.parent, body=body, confidence="medium",
                    reason=f"Markdown found at {target}")


def _detect_notebook(root: Path, *, slug: str) -> Optional[Proposal]:
    """Jupyter ``.ipynb`` notebooks → kind=notebook (HTML build).

    Builds with ``jupyter nbconvert --to html`` when the binary is on
    PATH, then serves the resulting HTML through the static /file
    endpoint (browser-native). When nbconvert is missing the recipe
    still loads — kind=notebook with no build cmd — and the file
    endpoint serves the raw .ipynb (browsers download it; user can open
    it in their notebook viewer of choice). The launcher's build log
    will note the missing converter so the user knows what to install.

    Build output filename is derived from the input stem so multiple
    notebooks under the same dir don't collide.
    """
    import shutil as _sh

    notebooks = [p for p in _scan(root, suffixes=(".ipynb",), max_depth=3)
                 if not p.parent.name == ".ipynb_checkpoints"]
    if not notebooks:
        return None
    target = notebooks[0]
    cwd = target.parent
    output = f"{target.stem}.html"

    body: dict = {
        "version": 1,
        "kind": "notebook",
        "slug": slug,
        "cwd": str(cwd),
        # Always advertise the rendered HTML name as the file the /file
        # endpoint will return; when nbconvert is absent the launcher
        # falls back to the raw .ipynb (see launcher._build_if_needed
        # tolerance for missing build deps).
        "file": output,
    }
    if _sh.which("jupyter") is not None:
        body["build"] = {
            "cmd": ["jupyter", "nbconvert", "--to", "html",
                    "--output", output, target.name],
            "cache_key_files": [target.name],
            "timeout_s": 300,
        }
    else:
        # No converter — serve raw .ipynb.
        body["file"] = target.name
    return Proposal(
        template="notebook", cwd=cwd, body=body,
        confidence="high" if _sh.which("jupyter") else "medium",
        reason=(
            f"Jupyter notebook at {target}"
            + (" (nbconvert)" if _sh.which("jupyter") else " — install jupyter for HTML render")
        ),
    )


def _detect_slides(root: Path, *, slug: str) -> Optional[Proposal]:
    """Slide decks → kind=slides (HTML build).

    Recognises three deck shapes (in priority order):
      1. ``marp:`` front-matter or `marp.config.*` in the dir → marp build
      2. ``reveal-md`` markdown (``--- slide ---`` separators) → reveal-md
      3. existing ``slides.html`` / ``presentation.html`` next to a deck
         source — already-built deck, serve as-is.

    Build commands depend on which CLI is on PATH; when neither marp
    nor reveal-md is installed the recipe still loads as kind=slides
    serving the raw markdown (browsers render it as text, but at least
    the recipe doesn't silently fall through to kind=doc which would
    pretend the deck is a generic report).
    """
    import shutil as _sh

    md_decks = [p for p in _scan(root, suffixes=(".md",), max_depth=3)
                if _looks_like_slide_deck(p)]
    if not md_decks:
        # Pre-built HTML deck (output of marp / reveal): treat as static.
        for cand in ("slides.html", "presentation.html", "deck.html"):
            hit = _find_first(root, cand, max_depth=3)
            if hit is not None:
                return Proposal(
                    template="slides", cwd=hit.parent,
                    body={
                        "version": 1, "kind": "slides", "slug": slug,
                        "cwd": str(hit.parent), "file": hit.name,
                    },
                    confidence="high",
                    reason=f"Pre-built slide deck at {hit}",
                )
        return None

    target = md_decks[0]
    cwd = target.parent
    output = f"{target.stem}.html"
    body: dict = {
        "version": 1,
        "kind": "slides",
        "slug": slug,
        "cwd": str(cwd),
        "file": output,
    }

    if _sh.which("marp") is not None or _sh.which("npx") is not None:
        # Prefer system marp; fall back to npx (which fetches on demand).
        if _sh.which("marp") is not None:
            body["build"] = {
                "cmd": ["marp", "--output", output, target.name],
                "cache_key_files": [target.name],
                "timeout_s": 180,
            }
        else:
            body["build"] = {
                "cmd": ["npx", "--yes", "@marp-team/marp-cli@latest",
                        "--output", output, target.name],
                "cache_key_files": [target.name],
                "timeout_s": 240,  # first-run fetch
            }
        return Proposal(
            template="slides", cwd=cwd, body=body, confidence="high",
            reason=f"Marp deck at {target}",
        )

    # Neither tool available — serve raw markdown so the recipe still
    # produces a result. The launcher's build log will flag this.
    body["file"] = target.name
    return Proposal(
        template="slides", cwd=cwd, body=body, confidence="medium",
        reason=f"Slide-shaped markdown at {target} (install marp or @marp-team/marp-cli to render)",
    )


def _detect_video(root: Path, *, slug: str) -> Optional[Proposal]:
    """Video deliverable → kind=video. Browser-native formats only — no
    transcode hook here (the proposer's contract is to produce a recipe
    fast; ffmpeg work belongs on the audio path where format support is
    spottier). Mp4/webm/mov/m4v cover ~99 % of what tasks emit.
    """
    vids = [p for p in _scan(root, suffixes=(".mp4", ".webm", ".mov", ".m4v"), max_depth=3)]
    if not vids:
        return None
    target = vids[0]
    body = {
        "version": 1,
        "kind": "video",
        "slug": slug,
        "cwd": str(target.parent),
        "file": target.name,
    }
    return Proposal(
        template="video", cwd=target.parent, body=body, confidence="high",
        reason=f"Video at {target}",
    )


def _detect_audio(root: Path, *, slug: str) -> Optional[Proposal]:
    """Audio deliverable → kind=audio. Browser-native (mp3/wav/ogg/m4a)
    served as-is. Non-native formats (.flac / .opus / .aac) get a
    transcode build step when ffmpeg is on PATH; without ffmpeg they
    fall through to raw delivery (Chrome plays .opus natively, Firefox
    plays .flac, but Safari doesn't).
    """
    import shutil as _sh

    NATIVE = (".mp3", ".wav", ".ogg", ".m4a")
    NEEDS_TRANSCODE = (".flac", ".opus", ".aac", ".wma")
    suffixes = NATIVE + NEEDS_TRANSCODE
    audios = [p for p in _scan(root, suffixes=suffixes, max_depth=3)]
    if not audios:
        return None
    target = audios[0]
    cwd = target.parent

    body: dict = {
        "version": 1,
        "kind": "audio",
        "slug": slug,
        "cwd": str(cwd),
        "file": target.name,
    }

    if target.suffix.lower() in NEEDS_TRANSCODE and _sh.which("ffmpeg") is not None:
        out = f"{target.stem}.mp3"
        body["file"] = out
        body["build"] = {
            "cmd": ["ffmpeg", "-y", "-i", target.name,
                    "-codec:a", "libmp3lame", "-qscale:a", "2", out],
            "cache_key_files": [target.name],
            "timeout_s": 600,
        }
        reason = f"Audio at {target} → mp3 (ffmpeg transcode)"
    else:
        reason = f"Audio at {target}"

    return Proposal(template="audio", cwd=cwd, body=body, confidence="high", reason=reason)


def _looks_like_slide_deck(md_path: Path) -> bool:
    """Cheap content sniff: read the first ~2 KB and check for marp
    front matter or reveal-md slide separators. Mirrors the simplest
    upstream conventions; deeper detection (theme files, build configs)
    isn't worth the I/O for a proposer.
    """
    try:
        head = md_path.read_text(errors="ignore")[:2048]
    except OSError:
        return False
    if "marp: true" in head.lower():
        return True
    if "\nmarp:" in head:
        return True
    # Reveal/marp slide separator — a line of three dashes at start of line,
    # appearing more than once (otherwise it's just a horizontal rule in a
    # regular markdown report).
    return head.count("\n---\n") >= 2


# ── Helpers ─────────────────────────────────────────────────────────


def _find_first(root: Path, name: str, *, max_depth: int = 4) -> Optional[Path]:
    for p in _scan(root, names=(name,), max_depth=max_depth):
        return p
    return None


def _find_package_json(root: Path) -> tuple[Optional[Path], Optional[dict]]:
    p = _find_first(root, "package.json", max_depth=3)
    if p is None:
        return None, None
    return p, _read_json(p)


def _find_package_json_with_dep(
    root: Path, deps: tuple[str, ...], *, max_depth: int = 4,
) -> tuple[Optional[Path], Optional[dict]]:
    """Find the first package.json whose deps include any of ``deps``.

    pnpm/npm/yarn workspaces hide the actual app under ``apps/<name>``
    or ``packages/<name>`` while the root package.json is just a tooling
    wrapper. ``_find_package_json`` returns whichever one rglob finds
    first (usually the root) — wrong for these layouts. This walker
    enumerates every package.json under ``root`` and returns the first
    that actually declares the framework dependency, so detectors land
    on the runnable app instead of the wrapper.

    Order is deterministic (rglob) and depth-bounded; ``max_depth=4``
    covers the common ``apps/<name>/package.json`` two-level pattern
    plus a layer of slack.
    """
    for pkg_path in _scan(root, names=("package.json",), max_depth=max_depth):
        pkg = _read_json(pkg_path)
        if pkg is None:
            continue
        present = _collect_deps(pkg)
        if any(d in present for d in deps):
            return pkg_path, pkg
    return None, None


def _collect_deps(pkg: dict) -> set[str]:
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        d = pkg.get(key)
        if isinstance(d, dict):
            out.update(d.keys())
    return out


def _resolve_local_bin(cwd: Path, name: str, *, max_climb: int = 5) -> Optional[Path]:
    """Find ``cwd/node_modules/.bin/{name}`` walking up to a workspace root.

    pnpm workspaces hoist binaries to the WORKSPACE root's
    ``node_modules/.bin`` rather than the per-app node_modules. When the
    binary isn't directly under ``cwd``, climb up to ``max_climb``
    parents and check each one. Returns the resolved absolute path on
    hit, or None — caller falls back to ``npx`` in that case.
    """
    here = cwd.resolve()
    for _ in range(max_climb + 1):
        candidate = here / "node_modules" / ".bin" / name
        if candidate.exists():
            return candidate.resolve()
        if here.parent == here:  # reached filesystem root
            break
        here = here.parent
    return None


def _detect_build_cmd(cwd: Path) -> Optional[dict]:
    """Pick the right package-manager install command for ``cwd``.

    Recipe schema requires ``cmd`` + ``cache_key_files``. Lockfile
    presence is the canonical signal: ``pnpm-lock.yaml`` → pnpm,
    ``yarn.lock`` → yarn, ``package-lock.json`` → npm. When no lockfile
    exists we fall back to npm install (no ``ci``: ``npm ci`` requires
    a lockfile and would error out). For workspaces, climb to the root
    that owns the lockfile so the correct PM bootstraps the whole tree.
    """
    here = cwd.resolve()
    for _ in range(5):
        if (here / "pnpm-lock.yaml").exists():
            return {
                "cmd": ["pnpm", "install", "--frozen-lockfile"],
                "cache_key_files": ["pnpm-lock.yaml", "package.json"],
            }
        if (here / "yarn.lock").exists():
            return {
                "cmd": ["yarn", "install", "--frozen-lockfile"],
                "cache_key_files": ["yarn.lock", "package.json"],
            }
        if (here / "package-lock.json").exists():
            return {
                "cmd": ["npm", "ci"],
                "cache_key_files": ["package-lock.json", "package.json"],
            }
        if here.parent == here:
            break
        here = here.parent
    return {
        "cmd": ["npm", "install"],
        "cache_key_files": ["package.json"],
    }


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _scan(
    root: Path,
    *,
    names: tuple[str, ...] = (),
    suffixes: tuple[str, ...] = (),
    max_depth: int = 4,
):
    """Files under ``root`` matching ``names`` or ``suffixes``, NEWEST FIRST.

    Depth-bounded to keep proposer cheap. Skips common noise dirs.

    ROCK-SOLID v5 P5.2 — the ordering is the fix, and it belongs HERE.

    This used to yield in ``rglob`` order, which is filesystem order: not
    sorted, not stable, and unrelated to which file the run just produced.
    Fourteen call sites across the seventeen ``_detect_*`` functions then took
    ``[0]`` of that. So a task whose deliverable was a fresh report could
    preview a stray PDF that happened to be enumerated first — and the fix had
    already been applied per-instance to the html detector, leaving the class
    open behind it.

    Sorting in the SCAN rather than at fourteen call sites means a detector
    added later inherits it. Returns a list, not a generator: an ordering
    guarantee you have to remember to apply is the thing that just failed.

    Ties broken by path so the result is deterministic — two files written in
    the same mtime granularity must not reorder between runs, or a preview
    flips target for no reason.
    """
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "dist", "build", ".next", ".cache", "target"}
    root = root.resolve()
    base_depth = len(root.parts)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob("*"):
        try:
            depth = len(path.parts) - base_depth
        except Exception:
            continue
        if depth > max_depth:
            continue
        # Skip if any parent in skip_dirs
        if any(part in skip_dirs for part in path.parts[base_depth:-1]):
            continue
        if not path.is_file():
            continue
        if names and path.name in names:
            found.append(path)
            continue
        if suffixes and path.suffix.lower() in suffixes:
            found.append(path)

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(found, key=lambda p: (-_mtime(p), str(p)))
