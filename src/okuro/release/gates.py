# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: blocking exit gates over a staged export tree.
# index:
#   imports
#   class Finding
#   def _is_binary / def _text_files
#   def gate_zero_tests
#   def gate_binaries
#   def gate_content
#   def gate_dev_slug
#   def gate_required_files
#   def gate_installer_url
#   def gate_version_consistency
#   def owner_terms / def owner_findings
#   def read_owner_baseline / def write_owner_baseline
#   def gate_owner
#   def gate_install_smoke
#   GATES / def run_gates
# AGENT_HEADER_END -->
"""Every gate is BLOCKING: one finding anywhere and the release stops.

The gates re-verify what the manifest already promised (zero test files,
no research docs) on purpose — the manifest is a selection, the gates are
the proof, and a selection bug must fail loudly at the boundary rather
than ship. Fail-closed throughout: a gate that cannot run is a refusal,
not a pass (the CONTENT guard's stale-beats-absent lenience is for daily
commits; a release gets no such grace).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from okuro.cli.guard_content import (
    MIN_TOKEN_LEN,
    compile_phrase_exceptions,
    compile_tokens,
    find_tokens,
    load_scoped_exceptions,
    load_tokens,
    refresh_tokens,
)

from .manifest import (
    BINARY_ALLOWLIST,
    DEV_SLUG,
    OWNER_BASELINE,
    RELEASE_REPO_URL,
    REQUIRED_FILES,
)
from .version import SITES, SSOT, read_versions, residue


@dataclass
class Finding:
    gate: str
    path: str
    detail: str


#: Anything matching one of these is a test file, wherever it sits.
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"[^/]+_test\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"\.test\.[jt]sx?$"),
    re.compile(r"\.spec\.[jt]sx?$"),
)

#: Machine-written text the token scan must not read (same reasoning as the
#: CONTENT guard: base64 hashes and minified bundles collide with anything).
_SCAN_EXCLUDED_SUFFIXES = (".lock", ".min.js", ".map", ".svg", ".ico")
_SCAN_EXCLUDED_BASENAMES = (
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "requirements-lock.txt",
)


def _relfiles(root: Path) -> list[str]:
    """Every file under ``root`` except git's own object store.

    The gates run over a staged export (no ``.git``) AND, as the pre-push
    tier, over the committed release clone — where ``.git/objects`` is a
    forest of NUL-carrying files. Measured 2026-09-12: 1572 "binary"
    findings, every one under ``.git/``. The store is git's, not the
    release's; a gate that walks it verifies the wrong artifact.
    """
    return sorted(
        str(p.relative_to(root))
        for p in Path(root).rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    )


def _is_binary(path: Path) -> bool:
    """NUL in the first 8 KiB — the same heuristic git itself uses."""
    with open(path, "rb") as fh:
        return b"\0" in fh.read(8192)


def gate_zero_tests(root: Path) -> list[Finding]:
    return [
        Finding("zero-tests", rel, "test file in export")
        for rel in _relfiles(root)
        if any(rx.search(rel) for rx in _TEST_PATTERNS)
    ]


def gate_binaries(root: Path) -> list[Finding]:
    """Enumerate EVERY binary; each must be individually allowlisted."""
    return [
        Finding("binaries", rel, "binary not in BINARY_ALLOWLIST")
        for rel in _relfiles(root)
        if _is_binary(Path(root) / rel) and rel not in BINARY_ALLOWLIST
    ]


def gate_content(root: Path, rx: re.Pattern[str] | None = None) -> list[Finding]:
    """Token scan of every text file against the LIVE generated list.

    Unlike the commit-time guard, the refresh failure is itself a finding:
    scanning a release against a stale list is exactly the surname gap that
    hid the worst string of the 2026-08 audit.
    """
    if rx is None:
        try:
            refresh_tokens(force=True)
        except Exception as exc:
            return [Finding("content", "~/.okuro/guard/tokens.txt",
                            f"token generation failed — refusing to scan stale: {exc}")]
        rx = compile_tokens(load_tokens())
    if rx is None:
        return [Finding("content", "~/.okuro/guard/tokens.txt",
                        "no token list on a machine with a CRM — refusing blind release")]
    return _scan_tree(root, "content", rx, compile_phrase_exceptions(), load_scoped_exceptions())


def _scan_tree(
    root: Path,
    gate: str,
    rx: re.Pattern[str],
    ignore_rx: re.Pattern[str] | None,
    scoped: dict[str, tuple[str, ...]] | None,
) -> list[Finding]:
    """Every text file, every line, against ``rx`` — honouring phrase
    exceptions and the PATH-SCOPED exceptions from tokens-ignore.txt, so an
    attribution permitted in LICENSE is still a finding in a prompt."""
    findings: list[Finding] = []
    for rel in _relfiles(root):
        base = rel.rsplit("/", 1)[-1]
        if base in _SCAN_EXCLUDED_BASENAMES or rel.endswith(_SCAN_EXCLUDED_SUFFIXES):
            continue
        path = Path(root) / rel
        if _is_binary(path):
            continue  # gate_binaries owns binaries
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            for m in find_tokens(line, rx, ignore_rx, path=rel, scoped=scoped):
                findings.append(
                    Finding(gate, f"{rel}:{lineno}", f"carries {m.group(0).lower()!r}")
                )
    return findings


def gate_dev_slug(root: Path) -> list[Finding]:
    rx = re.compile(re.escape(DEV_SLUG), re.IGNORECASE)
    findings: list[Finding] = []
    for rel in _relfiles(root):
        path = Path(root) / rel
        if _is_binary(path):
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if rx.search(line):
                findings.append(Finding("dev-slug", f"{rel}:{lineno}",
                                        f"names the private dev repo {DEV_SLUG!r}"))
    return findings


def gate_required_files(root: Path) -> list[Finding]:
    return [
        Finding("required-files", name, "required file missing from export")
        for name in REQUIRED_FILES
        if not (Path(root) / name).is_file()
    ]


def gate_installer_url(root: Path) -> list[Finding]:
    """bootstrap.sh/.ps1 must clone the RELEASE repo — verified, not rewritten."""
    findings: list[Finding] = []
    for name in ("bootstrap.sh", "bootstrap.ps1"):
        path = Path(root) / name
        if not path.is_file():
            findings.append(Finding("installer-url", name, "installer script missing"))
            continue
        if RELEASE_REPO_URL not in path.read_text(encoding="utf-8", errors="replace"):
            findings.append(Finding("installer-url", name,
                                    f"does not clone {RELEASE_REPO_URL}"))
    return findings


def gate_version_consistency(root: Path) -> list[Finding]:
    """Every site in the export states the SAME version, and no stale
    ``v0.x`` marketing residue survives in the prose a visitor reads first.

    The sites come from okuro.release.version — the same list the bump
    command writes — so the check cannot drift from the writer. Fails on the
    exact defect that shipped in release commit 3d602af6: a README badge
    reading v0.1.0 under a commit message saying okuro 3.0.0.
    """
    findings: list[Finding] = []
    seen = read_versions(root)
    for site in SITES:
        if site.path not in seen:
            findings.append(Finding("version-consistency", site.path, "version site missing from export"))
        elif seen[site.path] is None:
            findings.append(Finding("version-consistency", site.path, "states no recognisable version"))
    values = {v for v in seen.values() if v is not None}
    if len(values) > 1:
        truth = seen.get(SSOT)
        for path, value in sorted(seen.items()):
            if value is not None and value != truth:
                findings.append(Finding(
                    "version-consistency", path,
                    f"states {value} but {SSOT} states {truth} — run `okuro release bump {truth}`",
                ))
    for rel, lineno, text in residue(root):
        findings.append(Finding("version-consistency", f"{rel}:{lineno}", f"stale version residue {text!r}"))
    return findings


def owner_terms(root: Path) -> list[str]:
    """The operator's identifying strings, DERIVED — never a literal here.

    This module ships in the export, so a literal owner name in it would be
    the gate's first, self-inflicted finding (the same reason DEV_SLUG is
    concatenated). Instead:

    * the export's own ``pyproject.toml`` ``[project] authors`` — the release
      DECLARES who wrote it, and that name and email may appear nowhere but
      the attribution files the scoped exceptions permit;
    * the machine conventions (``~/.okuro/config.yaml``): host name and LAN
      domain, GPU names and hardware strings, fixed ports;
    * the operator's home directory.

    Every operator gets a gate that knows THEIR machine, with no per-owner
    configuration. Terms shorter than MIN_TOKEN_LEN are dropped — they
    would false-positive on ordinary text.
    """
    from okuro.yu.conventions import get_conventions

    terms: list[str] = []
    pyproject = Path(root) / "pyproject.toml"
    if pyproject.is_file():
        with open(pyproject, "rb") as fh:
            for author in tomllib.load(fh).get("project", {}).get("authors", []):
                terms += [author.get("name", ""), author.get("email", "")]
    # The bare FIRST name too: the 2026-09-11 sweep took the owner's name
    # out of 143 source lines, and two new ones arrived from a parallel
    # session the same night — quoted remarks in comments carry the first
    # name alone, which the full-name term never sees. Same scoped
    # exception as the full name (inherited in owner_findings), so the
    # attribution files stay permitted.
    terms += [t.split()[0] for t in list(terms) if t and " " in t.strip()]
    conv = get_conventions() or {}
    host = conv.get("host") or {}
    terms += [host.get("name", ""), host.get("domain", "")]
    for gpu in conv.get("gpus") or []:
        terms += [gpu.get("name", ""), gpu.get("hardware", "")]
    for entry in (conv.get("ports") or {}).get("fixed") or []:
        terms.append(str(entry.get("port", "")))
    terms.append(str(Path.home()))
    out: dict[str, None] = {}
    for t in terms:
        t = (t or "").strip().lower()
        if len(t) >= MIN_TOKEN_LEN:
            out.setdefault(t)
    return list(out)


def owner_findings(
    root: Path,
    terms: list[str] | None = None,
    scoped: dict[str, tuple[str, ...]] | None = None,
) -> list[Finding]:
    """Every owner-term hit in the export, after scoped exceptions.

    Separate from :func:`gate_owner` so the ratchet can be measured and
    re-pinned (``okuro release owner-baseline``) without the pass/fail
    wrapping. ``terms``/``scoped`` are injectable for offline tests.
    """
    if terms is None:
        terms = owner_terms(root)
    if scoped is None:
        scoped = load_scoped_exceptions()
    # A first name derived from a scoped full name inherits that scope:
    # "jane doe @ LICENSE,NOTICE,pyproject.toml" permits "jane" in exactly
    # those files and nowhere else.
    scoped = dict(scoped)
    for full, paths in list(scoped.items()):
        first = full.split()[0] if " " in full.strip() else None
        if first and first in terms:
            scoped.setdefault(first, paths)
    rx = compile_tokens(terms)
    if rx is None:
        return [Finding("owner", "pyproject.toml", "no owner terms derivable — refusing blind release")]
    return _scan_tree(root, "owner", rx, compile_phrase_exceptions(), scoped)


#: Where the ratchet lives on the DEV side — next to this module, so it is
#: found in any install mode. Module-level so tests can point it elsewhere.
OWNER_BASELINE_PATH = Path(__file__).with_name(OWNER_BASELINE.rsplit("/", 1)[-1])


def read_owner_baseline() -> dict | None:
    """The pinned ratchet, or None when no baseline has been written."""
    if not OWNER_BASELINE_PATH.is_file():
        return None
    return json.loads(OWNER_BASELINE_PATH.read_text(encoding="utf-8"))


def write_owner_baseline(findings: list[Finding]) -> Path:
    """Pin the current residue: a total plus per-file counts. Paths only —
    the baseline names WHERE, never WHAT, so it carries no owner term."""
    per_file: dict[str, int] = {}
    for f in findings:
        rel = f.path.rsplit(":", 1)[0]
        per_file[rel] = per_file.get(rel, 0) + 1
    OWNER_BASELINE_PATH.write_text(
        json.dumps({"total": len(findings), "files": dict(sorted(per_file.items()))}, indent=2) + "\n",
        encoding="utf-8",
    )
    return OWNER_BASELINE_PATH


def gate_owner(
    root: Path,
    terms: list[str] | None = None,
    scoped: dict[str, tuple[str, ...]] | None = None,
    baseline: dict | None = None,
) -> list[Finding]:
    """Owner residue may only ever FALL — a ratchet, not a zero.

    The content gate protects customers with a CRM-generated list; nothing
    protected the owner once the attribution exception retracted the name
    and email globally (2026-09-09). This gate has its own derived term list
    (:func:`owner_terms`) and a committed baseline count. Hits within the
    baseline pass — they are known, functional residue being swept file by
    file — and ONE hit above it fails the release, listing every hit so the
    new one is visible. No baseline at all is a refusal: an unpinned ratchet
    is not a ratchet. The baseline file itself must not ship.
    """
    if baseline is None:
        baseline = read_owner_baseline()
    if baseline is None:
        return [Finding("owner", OWNER_BASELINE,
                        "no owner baseline pinned — run `okuro release owner-baseline <export>`")]
    findings = owner_findings(root, terms, scoped)
    out: list[Finding] = []
    if (Path(root) / OWNER_BASELINE).exists():
        out.append(Finding("owner", OWNER_BASELINE, "the ratchet baseline shipped — manifest exclusion rotted"))
    if len(findings) > baseline["total"]:
        out.append(Finding("owner", "(ratchet)",
                           f"owner residue rose to {len(findings)} — baseline pins {baseline['total']}; "
                           "sweep the new hit, never raise the baseline"))
        out.extend(findings)
    return out


def gate_install_smoke(root: Path) -> list[Finding]:
    """Fresh venv → pip install → ``okuro --version``. Catches allowlist rot
    before a public user does. Slow by nature; run it last.

    Installs from a THROWAWAY COPY, never from ``root``: pip's build (and
    okuro's own build hook) writes into the source tree — ``__pycache__``,
    downloaded embed models — and a gate must never mutate the artifact it
    verifies. Proven live: the first release commit copied smoke droppings
    into the release repo and its own .gitignore refused them.
    """
    with tempfile.TemporaryDirectory(prefix="okuro-release-smoke-") as tmp:
        venv = Path(tmp) / "venv"
        workcopy = Path(tmp) / "tree"
        shutil.copytree(root, workcopy)
        steps: list[list[str]] = [
            [sys.executable, "-m", "venv", str(venv)],
            [str(venv / "bin" / "pip"), "install", "--quiet", "-e", str(workcopy)],
            [str(venv / "bin" / "okuro"), "--version"],
        ]
        for cmd in steps:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-15:]
                return [Finding("install-smoke", " ".join(cmd[-2:]),
                                "failed:\n    " + "\n    ".join(tail))]
    return []


#: Cheap and deterministic first; the network-touching smoke last.
GATES = (
    gate_required_files,
    gate_installer_url,
    gate_version_consistency,
    gate_zero_tests,
    gate_binaries,
    gate_dev_slug,
    gate_content,
    gate_owner,
    gate_install_smoke,
)


def run_gates(root: Path, *, smoke: bool = True) -> dict[str, list[Finding]]:
    """Run every gate; the dict carries ONE key per gate that ran.

    ``smoke=False`` skips only install-smoke for fast iteration — publish
    refuses a verdict produced that way, so the skip cannot leak a release.
    """
    results: dict[str, list[Finding]] = {}
    for gate in GATES:
        if gate is gate_install_smoke and not smoke:
            continue
        name = gate.__name__.removeprefix("gate_").replace("_", "-")
        results[name] = gate(Path(root))
    return results


def verdict(results: dict[str, list[Finding]], *, smoke_ran: bool) -> bool:
    """True only when every gate ran and none found anything."""
    return smoke_ran and all(not f for f in results.values())
