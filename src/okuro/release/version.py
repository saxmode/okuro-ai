# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: the release version — one writer, every site derived from pyproject.
# index:
#   imports
#   SSOT / class Site / SITES
#   def read_versions
#   def bump
#   RESIDUE_RX / def residue
# AGENT_HEADER_END -->
"""One version, one writer.

Measured 2026-09-08: five shipped claims about the version carried three
values (pyproject 3.0.0, ``__init__`` 3.0.0, README ``v0.1.0``, the FastAPI
app ``0.1.0`` served on /openapi.json, the frontend package ``0.1.0``), and
the release commit's own message read pyproject while ``okuro --version``
read ``__init__``. No command wrote them together and no gate compared them.

The single source of truth is ``pyproject.toml`` — hatch builds from it and
the release commit names it. Every other site is DERIVED, in one of two ways:

* at runtime, by reading ``okuro.__version__`` (the FastAPI app does this);
* at bump time, by ``okuro release bump X.Y.Z`` rewriting every entry in
  :data:`SITES` from one argument.

``__init__.__version__`` stays a literal on purpose. ``importlib.metadata``
would report whatever pyproject said at INSTALL time, so an editable dev
install goes stale on every bump until it is reinstalled — and ``install.sh``
prints ``okuro.__version__`` as its success line. A literal the bump command
rewrites and the version gate pins is the form that survives both a fresh
``pip install -e .`` and a dev tree that is never reinstalled.

The gate (``gates.gate_version_consistency``) reads the same :data:`SITES`
over the staged export, so the writer and the check cannot drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: The one site whose value is authoritative. Everything else follows it.
SSOT = "pyproject.toml"


@dataclass(frozen=True)
class Site:
    """A file that states the version, and the regex that isolates it.

    ``rx`` has exactly one capture group — the version string. The bump
    rewrites only that group, so surrounding text (a README badge, a JSON
    key) is preserved byte for byte.
    """

    path: str
    rx: re.Pattern[str]


SITES: tuple[Site, ...] = (
    Site(SSOT, re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)),
    Site("src/okuro/__init__.py", re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)),
    Site("README.md", re.compile(r"<code>ALPHA · v([0-9][^<\s]*)</code>")),
    Site("src/okuro/web/frontend/package.json", re.compile(r'^\s*"version":\s*"([^"]+)"', re.MULTILINE)),
)

#: A stale marketing version left behind by hand-editing — the exact residue
#: README.md:6 carried for a month. Anchored on the ``v`` so a dependency pin
#: like ``>=0.27`` is not a hit.
RESIDUE_RX = re.compile(r"(?<![A-Za-z0-9])v0\.\d[0-9.]*(?:\.x)?")
RESIDUE_FILES: tuple[str, ...] = ("README.md", "SECURITY.md")


def read_versions(root: Path) -> dict[str, str | None]:
    """The version each site states, keyed by path. ``None`` = file present
    but the pattern did not match; a missing file is simply absent from the
    dict so the caller can tell the two apart."""
    out: dict[str, str | None] = {}
    for site in SITES:
        path = Path(root) / site.path
        if not path.is_file():
            continue
        m = site.rx.search(path.read_text(encoding="utf-8"))
        out[site.path] = m.group(1) if m else None
    return out


def bump(root: Path, new_version: str) -> list[str]:
    """Rewrite every site to ``new_version``; return the paths that changed.

    Refuses a version that is not ``MAJOR.MINOR.PATCH`` with an optional
    pre-release tag — the same shape hatch accepts. Every site must exist
    and match: a site that silently fails to match is how the README drifted
    in the first place.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", new_version):
        raise ValueError(f"not a version: {new_version!r} (expected MAJOR.MINOR.PATCH)")
    changed: list[str] = []
    for site in SITES:
        path = Path(root) / site.path
        if not path.is_file():
            raise FileNotFoundError(f"version site missing: {site.path}")
        text = path.read_text(encoding="utf-8")
        m = site.rx.search(text)
        if not m:
            raise ValueError(f"version site {site.path} does not match its pattern")
        if m.group(1) == new_version:
            continue
        text = text[: m.start(1)] + new_version + text[m.end(1):]
        path.write_text(text, encoding="utf-8")
        changed.append(site.path)
    return changed


def residue(root: Path) -> list[tuple[str, int, str]]:
    """``(path, line, text)`` for every stale ``v0.x`` mention in the
    prose files a visitor reads first."""
    hits: list[tuple[str, int, str]] = []
    for rel in RESIDUE_FILES:
        path = Path(root) / rel
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = RESIDUE_RX.search(line)
            if m:
                hits.append((rel, lineno, m.group(0)))
    return hits
