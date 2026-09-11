# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Compute + cache per-project sidecar coverage (file-level) for the Health/Cortex tab, shared by the web endpoint and the scan hook.
# index:
#   CACHE_PATH
#   def compute_coverage
#   def save
#   def load_fresh
#   def invalidate
# AGENT_HEADER_END -->
"""Sidecar coverage: how many eligible source files an agent can orient on
cheaply (sidecar entry or inline AGENT_HEADER) per registered root.

The walk hashes thousands of files across every root and is far too slow to run
on every Health-page load (it made the tab look broken). This module owns the
computation once, plus a disk-backed cache so the number survives process
restarts and is invalidated when a scan changes sidecars.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from okuro.db.engine import okuro_home

CACHE_PATH = okuro_home() / "cortex" / "coverage.json"


def compute_coverage() -> dict:
    """Full walk: eligible vs covered (+ stale) files per registered root.

    Covered = has a sidecar entry OR an inline AGENT_HEADER. Stale = sidecar's
    recorded content_hash no longer matches the file on disk.
    """
    from okuro.cortex.core import HEADER_WRAPPERS, FILENAME_WRAPPERS, parse_header
    from okuro.cortex.exclusions import build_matcher
    from okuro.cortex.roots import registered_roots
    from okuro.cortex.scanner import GitIntegration
    from okuro.cortex.sidecar import (
        SIDECAR_FILENAME,
        content_hash,
        load as load_sidecar,
    )

    exts = set(HEADER_WRAPPERS.keys())
    filenames_supported = set(FILENAME_WRAPPERS.keys())

    rows: list[dict] = []
    tot_files = 0
    tot_covered = 0
    tot_stale = 0
    now_ts = time.time()

    for r in registered_roots():
        root = Path(str(r.path))
        if not root.is_dir():
            continue

        matcher = build_matcher(root)
        eligible = 0
        covered = 0
        stale = 0
        try:
            for dirpath, dirnames, fnames in os.walk(root, onerror=lambda _e: None):
                dirnames[:] = [d for d in dirnames if not matcher.is_pruned_dir(d)]
                d_path = Path(dirpath)
                entries = load_sidecar(d_path) if SIDECAR_FILENAME in fnames else {}
                for fname in fnames:
                    fp = d_path / fname
                    ext = fp.suffix.lower()
                    if ext not in exts and fname.lower() not in filenames_supported:
                        continue
                    try:
                        rel = fp.relative_to(root)
                    except ValueError:
                        continue
                    if matcher.is_excluded(rel):
                        continue
                    eligible += 1
                    entry = entries.get(fname)
                    if entry is not None:
                        covered += 1
                        if entry.content_hash and entry.content_hash != content_hash(fp):
                            stale += 1
                        continue
                    # No sidecar entry — accept an inline AGENT_HEADER as equally
                    # valid coverage. Agent-owned files (okuro source, tm-*
                    # projects) keep inline headers by design.
                    try:
                        head = fp.read_text(errors="replace")[:4096]
                    except OSError:
                        continue
                    if parse_header(head, file_path=fp) is not None:
                        covered += 1
                if eligible > 50000:
                    break
        except (OSError, PermissionError):
            pass

        try:
            marker = root / GitIntegration.MARKER_FILE
            last_scan_age_s = (
                now_ts - marker.stat().st_mtime if marker.is_file() else None
            )
        except OSError:
            last_scan_age_s = None

        tot_files += eligible
        tot_covered += covered
        tot_stale += stale
        pct = (covered / eligible * 100) if eligible else 0.0
        rows.append({
            "slug": r.project or "__unassigned__",
            "path": str(root),
            "eligible": eligible,
            "covered": covered,
            "stale": stale,
            "pct": round(pct, 1),
            "last_scan_age_s": (
                round(last_scan_age_s) if last_scan_age_s is not None else None
            ),
        })

    rows.sort(key=lambda r: r["eligible"], reverse=True)
    overall = (tot_covered / tot_files * 100) if tot_files else 0.0
    return {
        "projects": rows,
        "totals": {
            "eligible": tot_files,
            "covered": tot_covered,
            "stale": tot_stale,
            "pct": round(overall, 1),
        },
        "computed_at": now_ts,
    }


def save(result: dict) -> None:
    """Persist a computed coverage result to disk (best-effort)."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result))
        tmp.replace(CACHE_PATH)
    except OSError:
        pass


def load_fresh(ttl_s: float) -> dict | None:
    """Return the disk-cached result if younger than ``ttl_s``, else None."""
    try:
        data = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return None
    ts = data.get("computed_at", 0)
    if not ts or (time.time() - ts) >= ttl_s:
        return None
    return data


def invalidate() -> None:
    """Drop the disk cache so the next read recomputes (called after a scan)."""
    try:
        CACHE_PATH.unlink()
    except OSError:
        pass
