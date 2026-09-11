# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Install-contract state collector — single source of truth for the
#          post-onboarding install-report.json AND the standalone probe.
# index: dataclass ProbeResult | helpers | probe_1..6 | collect_state | write_report
# AGENT_HEADER_END -->
"""Install-contract state collector.

Tests the 6-link regeneration chain end-to-end against a live okuro install:

  1. profile saved to DB
  2. profile renders fully (8 directive keys minus the optional ``Avoid formats``)
  3. instruction files match the live render
  4. okuro MCP registered with at least one CLI
  5. background services running
  6. (optional) agents reach okuro MCP — `claude mcp list` shows ✓ Connected

Collector returns a structured dict so it can be serialised to JSON,
embedded in an HTTP response, or rendered by the wizard. The wizard's
``/api/onboarding/complete`` calls ``write_report()`` at the end of its
post-completion side-effects so a single canonical artifact exists at
``~/.okuro/install-report.json`` the moment the user clicks Done.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from okuro.db.engine import okuro_home as _okuro_home

# ─── stable paths ─────────────────────────────────────────────────────────
HOME = Path.home()
# okuro STATE resolves through okuro_home() ($OKURO_HOME first, ~/.okuro
# fallback), unlike the provider config paths below, which really do live in the
# user's HOME and must keep following it.
OKURO_HOME = _okuro_home()
OKURO_DB = Path(os.environ.get("OKURO_DB", str(OKURO_HOME / "okuro.db")))
ORCH_PORT = int(os.environ.get("OKURO_PORT", "13333"))

# The wizard writes here at /complete time. Stable path so support requests
# always paste the same file. Overwrites on every /complete run; the install
# dir's per-run copy preserves history.
LATEST_REPORT_PATH = OKURO_HOME / "install-report.json"


@dataclass
class ProbeResult:
    link: int
    name: str
    status: str   # pass | fail | skip
    detail: str
    evidence: dict = field(default_factory=dict)
    fix_hint: Optional[str] = None


# ─── helpers ──────────────────────────────────────────────────────────────
def _load_profile() -> dict:
    if not OKURO_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(str(OKURO_DB))
        try:
            cur = conn.execute("SELECT profile FROM user_profile WHERE id = 1")
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not row or row[0] is None:
        return {}
    if isinstance(row[0], str):
        try:
            return json.loads(row[0])
        except Exception:
            return {}
    return row[0] if isinstance(row[0], dict) else {}


def _safe_get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _render_behavioral() -> str:
    """Return the live behavioral section, or a tagged error string."""
    try:
        from okuro.sense.bootstrap.sections import build_behavioral_section
        return build_behavioral_section()
    except Exception as e:
        return f"<render error: {e.__class__.__name__}: {e}>"


def _path_exists_or_okuro(cmd: str) -> bool:
    if not cmd:
        return False
    if cmd == "okuro":
        return shutil.which("okuro") is not None
    return Path(cmd).exists()


# ─── 1. profile saved ─────────────────────────────────────────────────────
def probe_1_profile_saved() -> ProbeResult:
    if not OKURO_DB.exists():
        return ProbeResult(
            1, "profile_saved", "fail", f"DB missing: {OKURO_DB}",
            evidence={"db_exists": False, "db_path": str(OKURO_DB)},
            fix_hint="Run `okuro init` to bootstrap the DB",
        )
    profile = _load_profile()
    if not profile:
        return ProbeResult(
            1, "profile_saved", "fail", "user_profile row empty or unreadable",
            evidence={"db_exists": True, "db_path": str(OKURO_DB), "profile_chars": 0},
        )
    completed_at = _safe_get(profile, "onboarding", "completed_at")
    required = ["identity", "communication", "cognitive_style", "principles", "boundaries"]
    optional = ["profile_sources", "design", "expertise", "work_style", "decision_style"]
    populated, empty, sizes = [], [], {}
    for s in required + optional:
        v = profile.get(s)
        ok = bool(v)
        if ok and isinstance(v, dict) and not any(v.values()):
            ok = False
        sizes[s] = len(json.dumps(v, default=str)) if v else 0
        (populated if ok else empty).append(s)
    req_empty = [s for s in required if s in empty]
    evidence = {
        "db_path": str(OKURO_DB),
        "db_exists": True,
        "completed_at": completed_at,
        "required_sections": required,
        "optional_sections": optional,
        "populated": populated,
        "empty": empty,
        "section_sizes_chars": sizes,
        "profile_chars": len(json.dumps(profile, default=str)),
    }
    if not completed_at:
        return ProbeResult(
            1, "profile_saved", "fail",
            "onboarding.completed_at not stamped (wizard never finished)",
            evidence,
            fix_hint="Walk the wizard end-to-end — `okuro init`, then click Done",
        )
    if req_empty:
        return ProbeResult(
            1, "profile_saved", "fail",
            f"required sections empty: {', '.join(req_empty)}",
            evidence,
            fix_hint="Re-run wizard step that writes these sections (or `okuro init`)",
        )
    return ProbeResult(
        1, "profile_saved", "pass",
        f"{len(required)}/{len(required)} required sections populated",
        evidence,
    )


# ─── 2. profile renders fully ────────────────────────────────────────────
def probe_2_profile_renders(rendered: str) -> ProbeResult:
    if rendered.startswith("<render error"):
        return ProbeResult(
            2, "profile_renders", "fail", rendered,
            evidence={"render_error": rendered},
            fix_hint="venv python missing okuro module — reinstall okuro",
        )
    # NOTE: "Avoid formats" intentionally excluded — wizard's _map_answers only
    # writes communication.format_preferences.preferred, never .avoid. Only
    # /settings populates avoid; asserting it would fail every fresh wizard run.
    keys: list[Tuple[Any, str]] = [
        ("Response length:",        "Response length"),
        (("Tone:", "Directness:"),  "Tone/Directness"),
        ("Approach:",               "Approach"),
        ("Decision framing:",       "Decision framing"),
        ("Error handling:",         "Error handling"),
        ("Cognitive abstraction:",  "Cognitive abstraction"),
        ("Prefer formats:",         "Prefer formats"),
    ]
    missing, found_lines = [], {}
    for needle, label in keys:
        needles = needle if isinstance(needle, tuple) else (needle,)
        hit = next((n for n in needles if n in rendered), None)
        if hit:
            for line in rendered.splitlines():
                if hit in line:
                    found_lines[label] = line.strip()
                    break
        else:
            missing.append(label)
    evidence = {
        "rendered_chars": len(rendered),
        "rendered_lines": len(rendered.splitlines()),
        "found_keys": found_lines,
        "missing_keys": missing,
    }
    if missing:
        return ProbeResult(
            2, "profile_renders", "fail",
            f"missing keys: {', '.join(missing)}", evidence,
            fix_hint="src/okuro/sense/bootstrap/sections.py:build_behavioral_section — add render block for missing key",
        )
    return ProbeResult(
        2, "profile_renders", "pass",
        f"all {len(keys)} directive keys rendered ({len(rendered)} chars)", evidence,
    )


# ─── 3. instruction files match render ───────────────────────────────────
def probe_3_instruction_files(rendered: str) -> ProbeResult:
    if rendered.startswith("<render error"):
        return ProbeResult(3, "instruction_files", "skip",
                           "cannot render behavioral section to compare")
    distinctive: list[str] = []
    for line in rendered.splitlines():
        for marker in ("**Response length:**", "**Tone:**", "**Approach:**", "**Error handling:**"):
            if marker in line:
                distinctive.append(line.strip())
                break
        if len(distinctive) >= 3:
            break
    files_spec: list[Tuple[Path, Optional[str]]] = [
        (HOME / ".claude" / "CLAUDE.md",        None),
        (HOME / ".codex" / "instructions.md",   "self-contained by design"),
        (HOME / ".codex" / "AGENTS.md",         "self-contained by design"),
        # antigravity reuses ~/.gemini; the retired gemini CLI's GEMINI.md
        # was dropped 2026-07-18.
        (HOME / ".gemini" / "AGENTS.md",        None),
    ]
    files_evidence = []
    for path, note in files_spec:
        ev: dict = {"path": str(path), "exists": path.exists(), "note": note}
        if path.exists():
            try:
                text = path.read_text(errors="replace")
            except Exception as e:
                ev["read_error"] = str(e)
                files_evidence.append(ev); continue
            ev["size_bytes"] = len(text)
            ev["mtime"] = datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat()
            if distinctive:
                missing_lines = [d for d in distinctive if d not in text]
                ev["distinctive_lines_present"] = len(missing_lines) == 0
                ev["missing_distinctive_lines"] = missing_lines
            ev["tool_protocol_ref"] = "TOOL-PROTOCOL" in text  # informational
        files_evidence.append(ev)
    tp_md = OKURO_HOME / "TOOL-PROTOCOL.md"
    tp_evidence = {
        "path": str(tp_md), "exists": tp_md.exists(),
        "size_bytes": tp_md.stat().st_size if tp_md.exists() else 0,
    }
    files_present = [e for e in files_evidence if e["exists"]]
    if not files_present:
        return ProbeResult(
            3, "instruction_files", "fail",
            "no instruction files written (canon_deploy never ran)",
            {"distinctive_lines_grepped": distinctive,
             "files": files_evidence, "tool_protocol_md": tp_evidence},
            fix_hint="Run the canon_deploy wizard step or `okuro canon deploy`",
        )
    content_miss = [e["path"] for e in files_present if e.get("distinctive_lines_present") is False]
    issues = []
    if content_miss:
        issues.append(f"rendered content missing in: {', '.join(content_miss)}")
    if not tp_evidence["exists"]:
        issues.append(f"{tp_md} missing")
    evidence = {
        "distinctive_lines_grepped": distinctive,
        "files": files_evidence,
        "tool_protocol_md": tp_evidence,
    }
    if issues:
        fix = None
        if content_miss:
            fix = "Re-run canon_deploy — instruction files are stale relative to the live profile"
        elif not tp_evidence["exists"]:
            fix = "providers/__init__.py:generate_all calls generate_tool_protocol — verify it ran"
        return ProbeResult(3, "instruction_files", "fail", "; ".join(issues), evidence, fix)
    return ProbeResult(
        3, "instruction_files", "pass",
        f"{len(files_present)} files written, content matches live render",
        evidence,
    )


# ─── 4. MCP registered ───────────────────────────────────────────────────
def probe_4_mcp_registered() -> ProbeResult:
    files_inspected: list[dict] = []
    found: list[dict] = []
    stale: list[str] = []

    def _inspect_json(path: Path, accessor: Callable[[dict], Any], label: str) -> None:
        rec: dict = {"cli": label, "path": str(path), "exists": path.exists()}
        if not path.exists():
            files_inspected.append(rec); return
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            rec["parse_error"] = str(e); files_inspected.append(rec); return
        try:
            entry = accessor(data)
        except Exception:
            entry = None
        rec["okuro_present"] = entry is not None
        if isinstance(entry, dict):
            rec["command"] = entry.get("command")
            rec["args"] = entry.get("args")
            rec["transport"] = entry.get("type") or entry.get("transport")
            cmd = entry.get("command", "") or ""
            rec["command_exists"] = _path_exists_or_okuro(cmd)
            if rec["command_exists"]:
                found.append(rec)
            else:
                stale.append(f"{label}:{cmd or '<no command>'}")
        elif entry is not None:
            rec["raw_value"] = str(entry)[:120]
            found.append(rec)
        files_inspected.append(rec)

    # claude — multiple paths
    _inspect_json(HOME / ".mcp.json",
                  lambda d: (d.get("mcpServers") or {}).get("okuro"), "claude")
    if not any(r.get("cli") == "claude" and r.get("okuro_present") for r in files_inspected):
        _inspect_json(HOME / ".claude.json",
                      lambda d: (d.get("mcpServers") or {}).get("okuro"), "claude")
    if not any(r.get("cli") == "claude" and r.get("okuro_present") for r in files_inspected):
        _inspect_json(HOME / ".claude" / "settings.json",
                      lambda d: (d.get("mcpServers") or {}).get("okuro"), "claude")
    if not any(r.get("cli") == "claude" and r.get("okuro_present") for r in files_inspected):
        _inspect_json(HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
                      lambda d: (d.get("mcpServers") or {}).get("okuro"), "claude")
    if not any(r.get("cli") == "claude" and r.get("okuro_present") for r in files_inspected):
        # claude project-scoped
        proj_path = HOME / ".claude.json"
        rec = {"cli": "claude", "path": f"{proj_path}::projects[*].mcpServers", "exists": proj_path.exists()}
        if proj_path.exists():
            try:
                data = json.loads(proj_path.read_text())
                projects = data.get("projects") or {}
                hit = None
                for proj, cfg in projects.items():
                    s = (cfg or {}).get("mcpServers") or {}
                    if "okuro" in s:
                        hit = (proj, s["okuro"]); break
                rec["okuro_present"] = hit is not None
                if hit:
                    proj, entry = hit
                    rec["project"] = proj
                    if isinstance(entry, dict):
                        rec["command"] = entry.get("command")
                        rec["command_exists"] = _path_exists_or_okuro(entry.get("command", ""))
                        if rec["command_exists"]:
                            found.append(rec)
                        else:
                            stale.append(f"claude:{entry.get('command') or '<none>'}")
            except Exception as e:
                rec["parse_error"] = str(e)
        files_inspected.append(rec)

    # codex (TOML)
    codex = HOME / ".codex" / "config.toml"
    rec = {"cli": "codex", "path": str(codex), "exists": codex.exists()}
    if codex.exists():
        try:
            text = codex.read_text(errors="replace")
            rec["okuro_present"] = ("[mcp_servers.okuro]" in text or "[okuro]" in text)
            if rec["okuro_present"]:
                lines = text.splitlines(); block = []; in_block = False
                for line in lines:
                    if line.strip() in ("[mcp_servers.okuro]", "[okuro]"):
                        in_block = True; block.append(line); continue
                    if in_block:
                        if line.startswith("[") and not line.startswith("[mcp_servers.okuro"):
                            break
                        block.append(line)
                rec["section_block"] = "\n".join(block)[:400]
                found.append(rec)
        except Exception as e:
            rec["read_error"] = str(e)
    files_inspected.append(rec)

    # antigravity (agy) — reads ~/.gemini/config/mcp_config.json
    _inspect_json(HOME / ".gemini" / "config" / "mcp_config.json",
                  lambda d: (d.get("mcpServers") or {}).get("okuro"),
                  "antigravity")
    # cursor
    _inspect_json(HOME / ".cursor" / "mcp.json",
                  lambda d: (d.get("mcpServers") or {}).get("okuro"), "cursor")

    found_summary = sorted({r["cli"] + ":" + Path(r["path"]).name for r in found if r.get("cli")})
    evidence = {
        "found_count": len(found),
        "found_in": found_summary,
        "stale_command_paths": stale,
        "files_inspected": files_inspected,
    }
    if not found:
        return ProbeResult(
            4, "mcp_registered", "fail", "no CLI has okuro MCP registered",
            evidence,
            fix_hint="Run the canon_deploy wizard step or `okuro canon deploy`",
        )
    if stale:
        return ProbeResult(
            4, "mcp_registered", "fail",
            f"registered but command path stale: {', '.join(stale)}",
            evidence,
            fix_hint="Reinstall okuro or re-run canon_deploy after moving the repo",
        )
    return ProbeResult(
        4, "mcp_registered", "pass",
        f"registered with: {', '.join(found_summary)}", evidence,
    )


# ─── 5. services running ─────────────────────────────────────────────────
def probe_5_services_running() -> ProbeResult:
    issues: list[str] = []
    plat = platform.system()
    services_state: list[dict] = []

    if plat == "Linux":
        if shutil.which("systemctl"):
            for svc in ("okuro-orchestrator", "okuro-daemon"):
                rc = subprocess.run(["systemctl", "--user", "is-active", svc],
                                    capture_output=True, text=True)
                state = (rc.stdout or "").strip() or "unknown"
                services_state.append({"name": svc, "backend": "systemd", "state": state})
                if state != "active":
                    issues.append(f"{svc}={state}")
            if shutil.which("loginctl"):
                rc = subprocess.run(
                    ["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
                    capture_output=True, text=True,
                )
                linger_on = "Linger=yes" in (rc.stdout or "")
                services_state.append({"name": "linger", "value": linger_on,
                                       "raw": (rc.stdout or "").strip()})
                if not linger_on:
                    issues.append("linger=off")
        else:
            issues.append("systemctl missing — ForegroundManager fallback")
    elif plat == "Darwin":
        if shutil.which("launchctl"):
            agents_dir = HOME / "Library" / "LaunchAgents"
            uid = os.getuid() if hasattr(os, "getuid") else 0
            for label in ("com.okuro.orchestrator", "com.okuro.daemon"):
                rc = subprocess.run(["launchctl", "list", label],
                                    capture_output=True, text=True)
                loaded = rc.returncode == 0
                plist_path = agents_dir / f"{label}.plist"
                pr = subprocess.run(
                    ["launchctl", "print", f"gui/{uid}/{label}"],
                    capture_output=True, text=True,
                )
                services_state.append({
                    "name": label, "backend": "launchd",
                    "loaded": loaded,
                    "plist_path": str(plist_path),
                    "plist_exists": plist_path.exists(),
                    "plist_size_bytes": plist_path.stat().st_size if plist_path.exists() else 0,
                    "list_stdout_head": (rc.stdout or "")[:200],
                    "list_stderr_head": (rc.stderr or "")[:200],
                    "print_returncode": pr.returncode,
                    "print_stdout_head": (pr.stdout or "")[:600],
                    "print_stderr_head": (pr.stderr or "")[:200],
                })
                if not loaded:
                    if plist_path.exists():
                        issues.append(f"{label}=plist-on-disk-but-not-loaded")
                    else:
                        issues.append(f"{label}=plist-never-written")
        else:
            issues.append("launchctl missing")
    else:
        issues.append(f"unsupported platform: {plat}")

    health_url = f"http://127.0.0.1:{ORCH_PORT}/api/health"
    health: dict = {"url": health_url}
    try:
        import urllib.request
        with urllib.request.urlopen(health_url, timeout=3) as resp:
            body = resp.read(500).decode("utf-8", "replace")
            health["status_code"] = resp.status
            health["body_head"] = body
    except Exception as e:
        health["error"] = f"{e.__class__.__name__}: {e}"
        issues.append("/api/health unreachable")

    evidence = {"platform": plat, "services": services_state, "health": health}
    if issues:
        return ProbeResult(
            5, "services_running", "fail", ", ".join(issues), evidence,
            fix_hint="Re-run `okuro service install okuro-orchestrator okuro-daemon` and confirm port 13333 is owned by the daemon, not the wizard process",
        )
    return ProbeResult(5, "services_running", "pass",
                       "services active, /api/health=200", evidence)


# ─── 6. claude reaches okuro MCP ─────────────────────────────────────────
def probe_6_agents_use_it(live: bool = True) -> ProbeResult:
    """`claude mcp list` — does the okuro MCP server show as connected?"""
    if not live:
        return ProbeResult(6, "agents_use_it", "skip", "live=False")
    if not shutil.which("claude"):
        return ProbeResult(6, "agents_use_it", "skip", "claude not on PATH")
    try:
        rc = subprocess.run(["claude", "mcp", "list"],
                            capture_output=True, text=True, timeout=30)
        out = (rc.stdout or "") + ("\n" + rc.stderr if rc.stderr else "")
    except subprocess.TimeoutExpired as e:
        return ProbeResult(6, "agents_use_it", "fail",
                           f"timeout: {e}", {"timeout": True})
    except Exception as e:
        return ProbeResult(6, "agents_use_it", "fail",
                           f"<spawn error: {e.__class__.__name__}: {e}>", {})

    okuro_line = next((ln for ln in out.splitlines() if "okuro" in ln.lower()), "")
    connected = "okuro" in out.lower() and (
        "✓" in okuro_line or "connected" in okuro_line.lower()
    )
    evidence = {
        "command": "claude mcp list",
        "okuro_line": okuro_line.strip(),
        "connected": connected,
        "stdout_head": out[:800],
    }
    if connected:
        return ProbeResult(6, "agents_use_it", "pass",
                           "claude reaches okuro MCP (connected)", evidence)
    if "okuro" in out.lower():
        return ProbeResult(6, "agents_use_it", "fail",
                           "okuro MCP listed but not connected", evidence,
                           fix_hint="check `claude mcp get okuro` for the error")
    return ProbeResult(6, "agents_use_it", "fail",
                       "okuro MCP not registered with claude", evidence,
                       fix_hint="run canon_deploy onboarding step or `okuro canon deploy`")


# ─── environment block ───────────────────────────────────────────────────
def _okuro_meta() -> dict:
    out = {"version": None, "repo": None}
    try:
        import okuro
        out["version"] = getattr(okuro, "__version__", None)
        out["repo"] = str(Path(okuro.__file__).resolve().parent.parent.parent)
    except Exception as e:
        out["import_error"] = f"{e.__class__.__name__}: {e}"
    return out


def _install_run_tail(n: int = 50) -> Optional[dict]:
    install_dir = OKURO_HOME / "install"
    if not install_dir.exists():
        return None
    runs = sorted(install_dir.glob("install-*"), reverse=True)
    if not runs:
        return None
    latest = runs[0]
    out: dict = {"dir": str(latest)}
    jsonl = latest / "install.jsonl"
    if jsonl.exists():
        events = []
        for line in jsonl.read_text(errors="replace").splitlines()[-n:]:
            try:
                events.append(json.loads(line))
            except Exception:
                events.append({"raw": line[:200]})
        out["jsonl_tail"] = events
    log_file = latest / "install.log"
    if log_file.exists():
        out["log_size_bytes"] = log_file.stat().st_size
    return out


def _collect_environment() -> dict:
    return {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "okuro_db": str(OKURO_DB),
        "okuro_db_size_bytes": OKURO_DB.stat().st_size if OKURO_DB.exists() else 0,
        "okuro_home": str(OKURO_HOME),
        "okuro_orch_port": ORCH_PORT,
        "okuro": _okuro_meta(),
        "latest_install_run": _install_run_tail(),
        "shell": os.environ.get("SHELL", ""),
        "PATH_has_okuro": shutil.which("okuro") is not None,
        "PATH_has_claude": shutil.which("claude") is not None,
        "PATH_has_codex": shutil.which("codex") is not None,
        "PATH_has_agy": shutil.which("agy") is not None,
    }


# ─── public API ──────────────────────────────────────────────────────────
def collect_state(live: bool = True) -> dict:
    """Run every probe and return a JSON-serialisable dict.

    ``live=True`` runs probe 6 (claude mcp list). Set False from the wizard
    process if you don't want to spawn the claude subprocess.
    """
    rendered = _render_behavioral()
    results = [
        probe_1_profile_saved(),
        probe_2_profile_renders(rendered),
        probe_3_instruction_files(rendered),
        probe_4_mcp_registered(),
        probe_5_services_running(),
        probe_6_agents_use_it(live=live),
    ]
    counts = {"pass": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status] += 1
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return {
        "meta": {
            "ts": now_utc.isoformat(timespec="seconds") + "Z",
            "host": socket.gethostname(),
            "user": os.environ.get("USER", ""),
            "live_probe": live,
            "schema_version": 3,
        },
        "environment": _collect_environment(),
        "rendered_behavioral_section": rendered,
        "summary": {**counts, "total": sum(counts.values())},
        "probes": [asdict(r) for r in results],
    }


def write_report(
    *,
    live: bool = True,
    extra: Optional[dict] = None,
    out_path: Optional[Path] = None,
    archive_to_install_run: bool = True,
) -> Path:
    """Collect state and persist to ``~/.okuro/install-report.json``.

    Args:
        live: pass-through to ``collect_state`` — set False from the wizard
            uvicorn process to skip the claude subprocess spawn.
        extra: optional fields to merge into the JSON (e.g. wizard
            services_install results).
        out_path: override the canonical path. Default: ``LATEST_REPORT_PATH``.
        archive_to_install_run: also write a copy under
            ``~/.okuro/install/install-<latest>/install-report.json`` so each
            install run keeps a frozen copy alongside its install.jsonl.

    Returns the canonical path that was written. Always overwrites.
    """
    state = collect_state(live=live)
    if extra:
        state["extra"] = extra
    target = out_path or LATEST_REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, indent=2, default=str))
    if archive_to_install_run:
        install_dir = OKURO_HOME / "install"
        if install_dir.exists():
            runs = sorted(install_dir.glob("install-*"), reverse=True)
            if runs:
                archive = runs[0] / "install-report.json"
                try:
                    archive.write_text(json.dumps(state, indent=2, default=str))
                except Exception:
                    pass  # archive is best-effort
    return target
